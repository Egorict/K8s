"""ТС №3: пробой уровня по тренду. Только лонги.

Идея. Горизонтальный уровень с несколькими касаниями видят все участники: под
ним копятся продавцы, над ним - стопы тех, кто шортил отбой. Когда цена всё же
выходит выше, эти стопы срабатывают и толкают её дальше - это и есть импульс
пробоя, ради которого сделка открывается. Работает такое в первую очередь по
тренду, поэтому монеты берутся только растущие: пробой против нисходящего
движения чаще всего оказывается ловушкой.

Что важно в реализации:

  * входим, когда уровень ТОЛЬКО ЧТО перешли. Вход вдогонку, когда цена уже
    улетела на несколько процентов, портит соотношение риска к прибыли:
    стоп остаётся у уровня, а он уже далеко внизу;
  * пробой без объёма не считается: тихий выход за уровень - обычно ложный;
  * выход не по фиксированной цели, а по затуханию импульса: пробой либо
    переходит в движение, либо выдыхается, и второе видно по откату от лучшей
    точки сделки;
  * если цена вернулась под уровень - идея сделки умерла, ждать нечего.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from .config import Config
from .exchange import BybitPublic
from .indicators import safe_mean, scale
from .journal import Journal
from .levels import HorizontalLevel, find_levels, nearest_level
from .models import Candle, OrderbookView, PlainSetup, Position, Ticker
from .orderbook import BookManager
from .paper import PaperBroker

log = logging.getLogger("breakout")


# --------------------------------------------------------------------- отбор

class TrendScanner:
    """Шаг 1: монеты, которые растут. Пробой торгуем только по тренду."""

    def __init__(self, client: BybitPublic, cfg: Config):
        self.client = client
        self.cfg = cfg.breakout
        self._instruments: Dict[str, Dict] = {}
        self._instruments_ts: float = 0.0

    async def _get_instruments(self) -> Dict[str, Dict]:
        if not self._instruments or time.time() - self._instruments_ts > 3600:
            self._instruments = await self.client.instruments()
            self._instruments_ts = time.time()
            log.info("Загружено инструментов: %d", len(self._instruments))
        return self._instruments

    def _tradable(self, ticker: Ticker, info: Optional[Dict]) -> bool:
        if not info or info.get("status") != "Trading" or info.get("quote") != "USDT":
            return False
        if (info.get("base") or "").upper() in {b.upper() for b in self.cfg.excluded_bases}:
            return False
        if not (self.cfg.min_change_24h <= ticker.change_24h <= self.cfg.max_change_24h):
            return False
        return self.cfg.min_turnover_24h <= ticker.turnover_24h <= self.cfg.max_turnover_24h

    async def scan(self) -> List[Ticker]:
        instruments = await self._get_instruments()
        tickers = await self.client.tickers()
        prefiltered = [t for t in tickers if self._tradable(t, instruments.get(t.symbol))]
        log.info("Прошли базовый фильтр: %d из %d", len(prefiltered), len(tickers))

        prefiltered.sort(key=lambda t: t.change_24h, reverse=True)
        out: List[Ticker] = []
        for t in prefiltered[: self.cfg.max_watchlist * 2]:
            try:
                candles = await self.client.klines(
                    t.symbol, self.cfg.timeframe, limit=self.cfg.trend_sma_candles + 5)
            except Exception as exc:  # noqa: BLE001
                log.debug("Нет свечей по %s: %s", t.symbol, exc)
                continue
            if len(candles) < self.cfg.trend_sma_candles:
                continue
            # Цена выше своей средней - тренд вверх, а не отскок в падении.
            sma = safe_mean(c.close for c in candles[-self.cfg.trend_sma_candles:])
            if candles[-1].close <= sma:
                continue
            out.append(t)
            if len(out) >= self.cfg.max_watchlist:
                break

        if out:
            log.info("Кандидаты (%d): %s", len(out),
                     ", ".join(f"{t.symbol} {t.change_24h * 100:+.1f}%" for t in out))
        else:
            log.info("Растущих монет с уровнями сейчас нет")
        return out


# --------------------------------------------------------------------- сетап

def find_breakout(ticker: Ticker, candles: List[Candle], book: Optional[OrderbookView],
                  cfg: Config) -> Optional[PlainSetup]:
    """Шаг 2-3: уровень с касаниями, который цена только что перешла вверх."""
    bcfg = cfg.breakout
    if len(candles) < bcfg.lookback_candles // 2:
        return None

    window = candles[-bcfg.lookback_candles:]
    last = window[-1]
    price = last.close

    levels = find_levels(window[:-1], side="resistance",
                         tolerance=bcfg.level_tolerance,
                         min_touches=bcfg.min_touches,
                         window=bcfg.pivot_window)
    if not levels:
        return None

    # Нужен уровень, который цена уже перешла: он ниже текущей цены, но недалеко.
    level = nearest_level(levels, price, above=False, max_distance=bcfg.max_level_distance)
    if level is None:
        return None

    break_pct = (price - level.price) / level.price
    if not (bcfg.min_break_pct <= break_pct <= bcfg.max_break_pct):
        return None

    # Перед пробоем цена обязана быть под уровнем - иначе это не пробой,
    # а продолжение движения, которое началось раньше.
    before = window[-1 - bcfg.require_below_before:-1]
    if not before or any(c.close > level.price for c in before):
        return None

    # Пробой на объёме. Фон берём по свечам до пробойной.
    baseline = safe_mean(c.volume for c in window[-bcfg.volume_baseline_candles - 1:-1])
    volume_ratio = last.volume / baseline if baseline > 0 else 0.0
    if volume_ratio < bcfg.min_volume_ratio:
        return None

    # Плотность прямо над входом съест импульс - такой пробой не наш.
    book_note = ""
    if book is not None and book.genuine_ask_wall is not None:
        wall = book.genuine_ask_wall
        if 0 <= wall.distance_pct <= bcfg.ask_wall_block_pct:
            return None
        book_note = book.summary()

    # Скор: чаще касались - заметнее уровень; больше объём - честнее пробой;
    # ближе к уровню - лучше риск.
    score = round(
        0.4 * scale(level.touches, bcfg.min_touches, 5)
        + 0.35 * scale(volume_ratio, bcfg.min_volume_ratio, 3.0)
        + 0.25 * (1.0 - scale(break_pct, bcfg.min_break_pct, bcfg.max_break_pct)),
        3)

    return PlainSetup(
        symbol=ticker.symbol,
        side="long",
        price=price,
        timeframe=bcfg.timeframe,
        score=score,
        ticker=ticker,
        book=book_note,
        notes=[
            f"уровень {level.describe()} пробит на {break_pct * 100:+.2f}%",
            f"объём пробойной свечи x{volume_ratio:.1f} к фону",
            f"тренд: сутки {ticker.change_24h * 100:+.1f}%",
        ],
        extra={"level": level.price, "touches": level.touches,
               "break_pct": round(break_pct, 5), "volume_ratio": round(volume_ratio, 2)},
    )


# --------------------------------------------------------------------- движок

class BreakoutEngine:
    def __init__(self, cfg: Config, client: BybitPublic):
        self.cfg = cfg
        self.client = client
        self.scanner = TrendScanner(client, cfg)
        self.books = BookManager(client, cfg.orderbook)
        self.broker = PaperBroker(cfg)
        self.journal = Journal(cfg.trades_csv, cfg.signals_csv, cfg.state_json)

        self.watchlist: Dict[str, Ticker] = {}
        self._levels: Dict[str, float] = {}     # symbol -> уровень открытой сделки
        self._candles: Dict[str, tuple] = {}
        self._running = True

    async def _candles_for(self, symbol: str) -> List[Candle]:
        # 15-минутки не нужно перекачивать чаще раза в полторы минуты.
        cached = self._candles.get(symbol)
        if cached and time.monotonic() - cached[0] < 90.0:
            return cached[1]
        candles = await self.client.klines(
            symbol, self.cfg.breakout.timeframe, limit=self.cfg.breakout.lookback_candles + 5)
        self._candles[symbol] = (time.monotonic(), candles)
        return candles

    # ------------------------------------------------------------------ вход

    async def _check_symbol(self, symbol: str, ticker: Ticker) -> None:
        allowed, why = self.broker.can_open(symbol)
        if not allowed:
            return
        candles = await self._candles_for(symbol)
        book = await self.books.poll(symbol)
        setup = find_breakout(ticker, candles, book, self.cfg)
        if setup is None:
            return

        self.journal.log_plain_signal(setup, "вход в лонг на пробое")
        level = setup.extra["level"]
        # Технический стоп - возврат под уровень: там идея сделки перестаёт
        # существовать. Денежный остаётся предохранителем в open_market.
        stop_price_limit = level * (1.0 - self.cfg.breakout.invalidation_pct)
        position = self.broker.open_market(
            setup,
            stop_usd=self.cfg.breakout.stop_loss_usd,
            # Жёсткого тейка у ТС нет, выходим по затуханию импульса. Этот
            # потолок - лишь страховка на случай вертикального ухода цены.
            take_usd=self.cfg.breakout.stop_loss_usd * 4,
            stop_price_limit=stop_price_limit,
        )
        self._levels[symbol] = level

    # ------------------------------------------------------------------ выход

    def _exit_reason(self, position: Position, price: float) -> Optional[str]:
        bcfg = self.cfg.breakout
        level = self._levels.get(position.symbol)

        if price >= position.take_price:
            return "тейк-профит (потолок)"
        if price <= position.stop_price:
            # Стоп у нас всегда ниже уровня, поэтому различаем формулировку:
            # цене важно, вернулась ли она под уровень или просто съела деньги.
            if level is not None and price <= level:
                return f"ложный пробой: цена вернулась под уровень {level:.8g}"
            return "стоп-лосс"

        pnl = self.broker.net_pnl(position, price)

        # Импульс от пробоя утих: сделка была в плюсе и отдала заметную часть
        # достигнутого. Это и есть штатный тейк этой ТС.
        if position.best_pnl_usd >= bcfg.min_progress_usd:
            giveback_level = position.best_pnl_usd * (1.0 - bcfg.fade_giveback)
            if pnl <= giveback_level:
                return (f"импульс пробоя утих: откат с {position.best_pnl_usd:.2f}$ "
                        f"до {pnl:.2f}$")

        age_min = position.age_sec() / 60.0
        # Импульса не случилось вовсе - пробой оказался вялым.
        if position.best_pnl_usd < bcfg.min_progress_usd and age_min >= bcfg.no_progress_minutes:
            return f"пробой без импульса за {age_min:.0f} мин"
        if age_min >= bcfg.max_hold_minutes:
            return f"истекло время удержания ({age_min:.0f} мин)"
        return None

    # ------------------------------------------------------------------ циклы

    async def scan_loop(self) -> None:
        while self._running:
            try:
                found = await self.scanner.scan()
                new = {t.symbol: t for t in found}
                for symbol in self.broker.positions:
                    if symbol not in new and symbol in self.watchlist:
                        new[symbol] = self.watchlist[symbol]
                self.watchlist = new
                self.books.keep_only(list(new))
                for symbol in list(self._candles):
                    if symbol not in new:
                        self._candles.pop(symbol, None)
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка сканирования: %s", exc)
            await asyncio.sleep(self.cfg.breakout.rescan_interval_sec)

    async def setup_loop(self) -> None:
        while self._running:
            started = time.monotonic()
            for symbol, ticker in list(self.watchlist.items()):
                if not self._running:
                    break
                try:
                    await self._check_symbol(symbol, ticker)
                except Exception as exc:  # noqa: BLE001
                    log.debug("[%s] ошибка проверки пробоя: %s", symbol, exc)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self.cfg.breakout.setup_interval_sec - elapsed))

    async def position_loop(self) -> None:
        while self._running:
            for symbol, position in list(self.broker.positions.items()):
                try:
                    ticker = await self.client.ticker(symbol)
                    if ticker is None:
                        continue
                    price = ticker.last_price
                    self.broker.track(position, price)
                    reason = self._exit_reason(position, price)
                    if reason:
                        trade = self.broker.close(position, price, reason)
                        self.journal.log_trade(trade)
                        self._levels.pop(symbol, None)
                except Exception as exc:  # noqa: BLE001
                    log.debug("[%s] ошибка ведения позиции: %s", symbol, exc)
            await asyncio.sleep(self.cfg.position_interval_sec)

    async def report_loop(self, interval: float = 60.0) -> None:
        while self._running:
            await asyncio.sleep(interval)
            stats = self.broker.stats()
            self.journal.save_state(list(self.broker.positions.values()), stats,
                                    list(self.watchlist), strategy="breakout")
            log.info(
                "СТАТУС | наблюдаю %d монет | сделок %d (W%d/L%d, winrate %.0f%%) "
                "| открыто %d | итог %+.2f$",
                len(self.watchlist), stats["trades"], stats["wins"], stats["losses"],
                stats["winrate"], stats["open"], stats["net_pnl_usd"],
            )

    # ------------------------------------------------------------------ запуск

    async def run(self) -> None:
        log.info("Режим: %s | ТС пробоя уровня (только лонги) | размер сделки %.0f$ x%.0f = %.0f$ "
                 "| стоп -%.0f$ | выход по затуханию импульса",
                 self.cfg.mode.upper(), self.cfg.risk.margin_usd, self.cfg.risk.leverage,
                 self.cfg.risk.notional_usd, self.cfg.breakout.stop_loss_usd)
        tasks = [
            asyncio.create_task(self.scan_loop(), name="scan"),
            asyncio.create_task(self.setup_loop(), name="setup"),
            asyncio.create_task(self.position_loop(), name="positions"),
            asyncio.create_task(self.report_loop(), name="report"),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def shutdown_report(self) -> None:
        stats = self.broker.stats()
        self.journal.save_state(list(self.broker.positions.values()), stats,
                                list(self.watchlist), strategy="breakout")
        log.info("=" * 70)
        log.info("ИТОГ ДЕМО-СЕССИИ (ТС пробоя уровня)")
        log.info("  сделок: %d | прибыльных: %d | убыточных: %d | winrate: %.1f%%",
                 stats["trades"], stats["wins"], stats["losses"], stats["winrate"])
        log.info("  чистый результат: %+.2f$", stats["net_pnl_usd"])
        log.info("  журнал сделок: %s", self.cfg.trades_csv)
        log.info("=" * 70)
