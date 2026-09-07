"""ТС №4: откуп просадки биткоина. Только лонги, только BTCUSDT.

Идея. BTC регулярно даёт короткие проливы на пару процентов, после которых цена
возвращается: инструмент слишком ликвиден, чтобы падать бесконечно без причины.
Покупаем такой пролив с целью +5$ и стопом -2$.

Ключевое ограничение - НЕ покупать в свободном падении. Поэтому под ценой нужна
опора, и годится любая из двух:

  * горизонтальная поддержка с несколькими касаниями (bot/levels.py) - место,
    откуда цену уже разворачивало;
  * настоящая плотность на покупку в стакане (bot/orderbook.py) - крупная
    лимитка, прошедшая анти-спуфинг.

Без опоры бот просто ждёт: пропущенный вход стоит ноль, а пойманный нож - деньги.
Мониторить одну монету дёшево, поэтому здесь нет ни сканера, ни watchlist -
только цикл по BTCUSDT.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from .config import Config
from .exchange import BybitPublic
from .journal import Journal
from .levels import find_levels, nearest_level
from .models import Candle, OrderbookView, PlainSetup, Position, Ticker
from .orderbook import BookManager
from .paper import PaperBroker

log = logging.getLogger("btc")


def find_dip(ticker: Ticker, candles: List[Candle], book: Optional[OrderbookView],
             cfg: Config) -> Optional[PlainSetup]:
    """Просадка на пару процентов от максимума окна, с опорой под ценой."""
    bcfg = cfg.btc
    if len(candles) < bcfg.dip_window_candles:
        return None

    price = ticker.last_price
    window = candles[-bcfg.dip_window_candles:]
    high = max(c.high for c in window)
    if high <= 0 or price <= 0:
        return None

    dip = (high - price) / high
    if not (bcfg.min_dip_pct <= dip <= bcfg.max_dip_pct):
        return None

    # Опора №1: горизонтальная поддержка с касаниями.
    supports = find_levels(candles[-bcfg.lookback_candles:], side="support",
                           tolerance=bcfg.level_tolerance,
                           min_touches=bcfg.min_touches,
                           window=bcfg.pivot_window)
    level = nearest_level(supports, price, above=False, max_distance=bcfg.support_distance)

    # Опора №2: настоящая плотность на покупку под ценой.
    wall = None
    if book is not None and book.genuine_bid_wall is not None:
        candidate = book.genuine_bid_wall
        if -bcfg.wall_distance <= candidate.distance_pct < 0:
            wall = candidate

    if bcfg.require_support and level is None and wall is None:
        return None

    notes = [f"просадка {dip * 100:.2f}% от максимума {high:.8g} за "
             f"{bcfg.dip_window_candles * int(bcfg.timeframe) // 60}ч"]
    score = 0.4 + 0.3 * min(1.0, dip / bcfg.max_dip_pct)
    if level is not None:
        notes.append(f"поддержка {level.describe()} на {abs(level.distance_pct(price)) * 100:.2f}% ниже")
        score += 0.15
    if wall is not None:
        notes.append(f"плотность на покупку: {wall.describe()}")
        score += 0.15

    return PlainSetup(
        symbol=ticker.symbol,
        side="long",
        price=price,
        timeframe=bcfg.timeframe,
        score=round(min(score, 1.0), 3),
        ticker=ticker,
        book=book.summary() if book is not None else "",
        notes=notes,
        extra={"dip_pct": round(dip, 5), "window_high": high,
               "support": level.price if level else None,
               "wall": wall.price if wall else None},
    )


class BtcEngine:
    """Один символ, один цикл: тикер -> свечи -> стакан -> вход/выход."""

    def __init__(self, cfg: Config, client: BybitPublic):
        self.cfg = cfg
        self.client = client
        self.books = BookManager(client, cfg.orderbook)
        self.broker = PaperBroker(cfg)
        self.journal = Journal(cfg.trades_csv, cfg.signals_csv, cfg.state_json)
        self.symbol = cfg.btc.symbol
        self._candles: tuple = (0.0, [])
        self._running = True

    async def _candles_now(self) -> List[Candle]:
        # 5-минутки чаще раза в полминуты перекачивать бессмысленно.
        ts, cached = self._candles
        if cached and time.monotonic() - ts < 30.0:
            return cached
        candles = await self.client.klines(
            self.symbol, self.cfg.btc.timeframe, limit=self.cfg.btc.lookback_candles + 5)
        self._candles = (time.monotonic(), candles)
        return candles

    def _exit_reason(self, position: Position, price: float) -> Optional[str]:
        bcfg = self.cfg.btc
        if price >= position.take_price:
            return f"тейк-профит +{bcfg.take_profit_usd:.0f}$"
        if price <= position.stop_price:
            return f"стоп-лосс -{bcfg.stop_loss_usd:.0f}$"
        age_min = position.age_sec() / 60.0
        if age_min >= bcfg.max_hold_minutes:
            return f"истекло время удержания ({age_min:.0f} мин)"
        return None

    async def _tick(self) -> None:
        ticker = await self.client.ticker(self.symbol)
        if ticker is None:
            return
        price = ticker.last_price
        book = await self.books.poll(self.symbol)

        position = self.broker.positions.get(self.symbol)
        if position is not None:
            self.broker.track(position, price)
            reason = self._exit_reason(position, price)
            if reason:
                trade = self.broker.close(position, price, reason)
                self.journal.log_trade(trade)
            return

        allowed, why = self.broker.can_open(self.symbol)
        if not allowed:
            return
        candles = await self._candles_now()
        setup = find_dip(ticker, candles, book, self.cfg)
        if setup is None:
            return

        self.journal.log_plain_signal(setup, "вход в лонг на просадке")
        self.broker.open_market(setup,
                                stop_usd=self.cfg.btc.stop_loss_usd,
                                take_usd=self.cfg.btc.take_profit_usd)

    async def trade_loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                log.debug("Ошибка цикла: %s", exc)
            await asyncio.sleep(self.cfg.btc.poll_interval_sec)

    async def report_loop(self, interval: float = 60.0) -> None:
        while self._running:
            await asyncio.sleep(interval)
            stats = self.broker.stats()
            self.journal.save_state(list(self.broker.positions.values()), stats,
                                    [self.symbol], strategy="btc")
            log.info("СТАТУС | %s | сделок %d (W%d/L%d, winrate %.0f%%) | открыто %d | итог %+.2f$",
                     self.symbol, stats["trades"], stats["wins"], stats["losses"],
                     stats["winrate"], stats["open"], stats["net_pnl_usd"])

    async def run(self) -> None:
        bcfg = self.cfg.btc
        # Кулдаун по символу у этой ТС свой: монета одна, и общий получасовой
        # простой из RiskConfig оставил бы бота почти без сделок.
        self.cfg.risk.symbol_cooldown_sec = bcfg.cooldown_sec
        log.info("Режим: %s | ТС биткоина (только лонги) | %s | размер сделки %.0f$ x%.0f = %.0f$ "
                 "| вход от просадки %.0f%% с опорой | тейк +%.0f$ | стоп -%.0f$",
                 self.cfg.mode.upper(), bcfg.symbol, self.cfg.risk.margin_usd,
                 self.cfg.risk.leverage, self.cfg.risk.notional_usd,
                 bcfg.min_dip_pct * 100, bcfg.take_profit_usd, bcfg.stop_loss_usd)
        tasks = [
            asyncio.create_task(self.trade_loop(), name="trade"),
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
                                [self.symbol], strategy="btc")
        log.info("=" * 70)
        log.info("ИТОГ ДЕМО-СЕССИИ (ТС биткоина)")
        log.info("  сделок: %d | прибыльных: %d | убыточных: %d | winrate: %.1f%%",
                 stats["trades"], stats["wins"], stats["losses"], stats["winrate"])
        log.info("  чистый результат: %+.2f$", stats["net_pnl_usd"])
        log.info("  журнал сделок: %s", self.cfg.trades_csv)
        log.info("=" * 70)
