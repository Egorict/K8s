"""ТС №3: импульсный пробой уровня. Только лонги.

Главное отличие от обычного «пробойника»: решение принимает АКТИВНОСТЬ у
уровня, а не факт пересечения цены.

Почему так. Само по себе пересечение уровня не значит ничего - цена может
переползти его на трёх сделках и сползти обратно. Толкает дальше именно
торговля у уровня: летит лента принтов, ставятся и снимаются заявки, цена
дёргается туда-сюда. Она же первой и заканчивается, когда импульс выдохся, -
раньше, чем развернётся котировка.

Отсюда правила:

  * вход - когда у уровня кипит торговля. Цена при этом может быть и ПОД
    уровнем (заходим в преддверии пробоя), и уже НАД ним; важно, что она в
    зоне уровня и там есть активность;
  * выход - как только активность упала относительно своего пика. Цена в этом
    решении не участвует вовсе: выходим и в плюс, и в минус;
  * тейк и денежный стоп остаются, но это предохранители, а не основной сценарий.

Устройство по циклам (важно для лимитов API - замер активности дорогой):

  scan_loop      раз в 4 минуты  - монеты в восходящем тренде;
  level_loop     раз в 45 секунд - свечи, явные уровни, кто «горячий»
                                   (цена подошла к уровню);
  activity_loop  раз в 3 секунды - ТОЛЬКО по горячим монетам: лента принтов и
                                   стакан, вход и ведение позиции.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

from .activity import ActivityTracker, measure
from .config import Config
from .exchange import BybitPublic
from .indicators import safe_mean, scale
from .journal import Journal
from .levels import HorizontalLevel, find_levels, nearest_level
from .models import ActivitySnapshot, Candle, PlainSetup, Position, Ticker
from .paper import PaperBroker

log = logging.getLogger("breakout")


# --------------------------------------------------------------------- отбор

class TrendScanner:
    """Шаг 1: монеты в восходящем тренде. Пробой торгуем только по тренду."""

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

    def _uptrend(self, candles: List[Candle]) -> Tuple[bool, str]:
        """Восходящий тренд - три условия сразу, а не одна средняя.

        Только так отсеивается отскок внутри падения: у него цена бывает выше
        быстрой средней, но медленная всё ещё смотрит вниз.
        """
        c = self.cfg
        if len(candles) < c.trend_slow_sma + c.trend_slope_candles:
            return False, "мало свечей"

        closes = [k.close for k in candles]
        fast = safe_mean(closes[-c.trend_fast_sma:])
        slow = safe_mean(closes[-c.trend_slow_sma:])
        slow_before = safe_mean(
            closes[-(c.trend_slow_sma + c.trend_slope_candles): -c.trend_slope_candles])

        if closes[-1] <= fast:
            return False, "цена под быстрой средней"
        if fast <= slow:
            return False, "быстрая средняя под медленной"
        if slow_before <= 0:
            return False, "нет истории"
        slope = (slow - slow_before) / slow_before
        if slope < c.min_trend_slope:
            return False, f"медленная средняя почти не растёт ({slope * 100:+.2f}%)"
        return True, f"тренд вверх, наклон {slope * 100:+.1f}%"

    async def scan(self) -> List[Ticker]:
        instruments = await self._get_instruments()
        tickers = await self.client.tickers()
        prefiltered = [t for t in tickers if self._tradable(t, instruments.get(t.symbol))]
        log.info("Прошли базовый фильтр: %d из %d", len(prefiltered), len(tickers))

        prefiltered.sort(key=lambda t: t.change_24h, reverse=True)
        out: List[Ticker] = []
        need = self.cfg.trend_slow_sma + self.cfg.trend_slope_candles + 5
        for t in prefiltered[: self.cfg.max_watchlist * 2]:
            try:
                candles = await self.client.klines(t.symbol, self.cfg.timeframe, limit=need)
            except Exception as exc:  # noqa: BLE001
                log.debug("Нет свечей по %s: %s", t.symbol, exc)
                continue
            ok, why = self._uptrend(candles)
            if not ok:
                log.debug("[%s] не берём: %s", t.symbol, why)
                continue
            out.append(t)
            if len(out) >= self.cfg.max_watchlist:
                break

        if out:
            log.info("Кандидаты (%d): %s", len(out),
                     ", ".join(f"{t.symbol} {t.change_24h * 100:+.1f}%" for t in out))
        else:
            log.info("Монет в восходящем тренде с явными уровнями сейчас нет")
        return out


# --------------------------------------------------------------------- уровни

def pick_level(candles: List[Candle], price: float, cfg: Config) -> Optional[HorizontalLevel]:
    """Явный уровень рядом с ценой: касания разнесены, отбои были.

    Уровень ищем и выше цены (ещё не пробит), и ниже (только что перешли) -
    вход разрешён по обе стороны, решает активность.
    """
    b = cfg.breakout
    if len(candles) < b.lookback_candles // 2:
        return None
    window = candles[-b.lookback_candles:]

    levels = find_levels(window, side="resistance",
                         tolerance=b.level_tolerance,
                         min_touches=b.min_touches,
                         window=b.pivot_window,
                         min_spacing=b.min_touch_spacing,
                         min_rejection=b.min_rejection_pct)
    if not levels:
        return None

    best: Optional[HorizontalLevel] = None
    for level in levels:
        if abs(level.distance_pct(price)) > b.max_level_distance:
            continue
        if best is None or abs(level.distance_pct(price)) < abs(best.distance_pct(price)):
            best = level
    return best


def in_zone(price: float, level: HorizontalLevel, cfg: Config) -> bool:
    """Цена в рабочей зоне уровня - с любой его стороны."""
    return abs(level.distance_pct(price)) <= cfg.breakout.level_zone_pct


# --------------------------------------------------------------------- сетап

def build_setup(ticker: Ticker, level: HorizontalLevel, snapshot: ActivitySnapshot,
                ratio: float, cfg: Config) -> Optional[PlainSetup]:
    """Вход: у уровня есть активность. Где именно цена - неважно."""
    b = cfg.breakout
    price = ticker.last_price

    if snapshot.score < b.min_entry_score:
        return None
    if ratio < b.min_activity_ratio:
        return None
    if snapshot.trades_per_min < b.min_trades_per_min:
        return None
    if snapshot.volume_per_min < b.min_volume_per_min:
        return None
    if snapshot.near_share < b.min_near_share:
        return None
    if snapshot.book_notional < b.min_book_notional:
        return None
    if snapshot.swings < b.min_swings:
        return None
    # Импульс должен быть покупательским: пробой вверх делают те, кто берёт
    # по рынку, а не те, кто разгружается в стакан.
    if snapshot.buy_ratio < b.min_buy_ratio:
        return None

    side_note = "над уровнем" if price >= level.price else "под уровнем"
    score = round(min(1.0, 0.6 * snapshot.score + 0.4 * scale(ratio, b.min_activity_ratio, 4.0)), 3)

    return PlainSetup(
        symbol=ticker.symbol,
        side="long",
        price=price,
        timeframe=b.timeframe,
        score=score,
        ticker=ticker,
        book=snapshot.describe(),
        notes=[
            f"уровень {level.describe()}, цена {side_note} "
            f"({level.distance_pct(price) * -100:+.2f}%)",
            f"активность {snapshot.score:.2f} при фоне x{ratio:.1f}",
            snapshot.describe(),
        ],
        extra={"level": level.price, "touches": level.touches,
               "rejection": round(level.rejection, 5),
               "activity": snapshot.score, "activity_ratio": round(ratio, 2)},
    )


# --------------------------------------------------------------------- движок

class BreakoutEngine:
    def __init__(self, cfg: Config, client: BybitPublic):
        self.cfg = cfg
        self.client = client
        self.scanner = TrendScanner(client, cfg)
        self.broker = PaperBroker(cfg)
        self.journal = Journal(cfg.trades_csv, cfg.signals_csv, cfg.state_json)

        self.watchlist: Dict[str, Ticker] = {}
        self.levels: Dict[str, HorizontalLevel] = {}     # монета -> её уровень
        self.hot: Dict[str, HorizontalLevel] = {}        # цена подошла к уровню
        self.trackers: Dict[str, ActivityTracker] = {}
        self._entry_levels: Dict[str, float] = {}        # уровень открытой сделки
        self._last: Dict[str, ActivitySnapshot] = {}     # последний замер, для панели
        self._running = True

    def tracker(self, symbol: str) -> ActivityTracker:
        if symbol not in self.trackers:
            self.trackers[symbol] = ActivityTracker(self.cfg.breakout)
        return self.trackers[symbol]

    # ------------------------------------------------------------------ замер

    async def _snapshot(self, symbol: str, level: HorizontalLevel
                        ) -> Optional[Tuple[ActivitySnapshot, float]]:
        """Один замер активности: лента принтов + стакан."""
        prints = await self.client.recent_trades(symbol, self.cfg.breakout.trade_fetch_limit)
        if not prints:
            return None
        bids, asks = await self.client.orderbook(symbol, self.cfg.orderbook.depth)
        snapshot = measure(prints, bids, asks, level.price, self.cfg.breakout)
        tracker = self.tracker(symbol)
        ratio = tracker.ratio(snapshot) if tracker.ready else 0.0
        tracker.add(snapshot)
        self._last[symbol] = snapshot
        return snapshot, ratio

    # ------------------------------------------------------------------ вход

    async def _try_enter(self, symbol: str, level: HorizontalLevel,
                         snapshot: ActivitySnapshot, ratio: float) -> None:
        allowed, why = self.broker.can_open(symbol)
        if not allowed:
            return
        ticker = await self.client.ticker(symbol)
        if ticker is None or not in_zone(ticker.last_price, level, self.cfg):
            return

        setup = build_setup(ticker, level, snapshot, ratio, self.cfg)
        if setup is None:
            return

        b = self.cfg.breakout
        self.journal.log_plain_signal(setup, "вход в лонг: активность у уровня")
        # Технический стоп - глубокий возврат под уровень: там идея сделки
        # перестаёт существовать. Денежный остаётся предохранителем.
        stop_price_limit = level.price * (1.0 - b.invalidation_pct)
        self.broker.open_market(setup, stop_usd=b.stop_loss_usd,
                                take_usd=b.take_profit_usd,
                                stop_price_limit=stop_price_limit)
        self._entry_levels[symbol] = level.price
        tracker = self.tracker(symbol)
        tracker.reset_peak()
        tracker.note_peak(snapshot)

    # ------------------------------------------------------------------ выход

    def _exit_reason(self, position: Position, price: float,
                     snapshot: ActivitySnapshot, tracker: ActivityTracker) -> Optional[str]:
        b = self.cfg.breakout

        # 1. ГЛАВНОЕ ПРАВИЛО: импульс кончился. Цена здесь не участвует - она
        #    разворачивается позже, чем затихает торговля, и ждать разворота
        #    значит отдавать прибыль.
        faded = tracker.faded(snapshot)
        if faded:
            return faded

        # 2. Тейк: импульс отработал, фиксируем.
        if price >= position.take_price:
            return f"тейк-профит +{b.take_profit_usd:.0f}$"

        # 3. Предохранители по цене.
        if price <= position.stop_price:
            level = self._entry_levels.get(position.symbol)
            if level is not None and price <= level * (1.0 - b.invalidation_pct):
                return f"цена ушла под уровень {level:.8g}"
            return "стоп-лосс"

        age_min = position.age_sec() / 60.0
        if position.best_pnl_usd <= 0 and age_min >= b.no_progress_minutes:
            return f"импульс не начался за {age_min:.0f} мин"
        if age_min >= b.max_hold_minutes:
            return f"истекло время удержания ({age_min:.0f} мин)"
        return None

    async def _manage(self, symbol: str, position: Position,
                      snapshot: ActivitySnapshot) -> None:
        ticker = await self.client.ticker(symbol)
        if ticker is None:
            return
        price = ticker.last_price
        tracker = self.tracker(symbol)
        tracker.note_peak(snapshot)
        self.broker.track(position, price)

        reason = self._exit_reason(position, price, snapshot, tracker)
        if reason:
            trade = self.broker.close(position, price, reason)
            self.journal.log_trade(trade)
            self._entry_levels.pop(symbol, None)
            tracker.reset_peak()

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
                for symbol in list(self.trackers):
                    if symbol not in new:
                        self.trackers.pop(symbol, None)
                        self.levels.pop(symbol, None)
                        self._last.pop(symbol, None)
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка сканирования: %s", exc)
            await asyncio.sleep(self.cfg.breakout.rescan_interval_sec)

    async def level_loop(self) -> None:
        """Уровни и отбор «горячих» монет - тех, у чьего уровня стоит цена.

        Дешёвая часть: свечи и один тикер на монету. Дорогой замер активности
        делается только по горячим, иначе лимитов биржи не хватит.
        """
        while self._running:
            hot: Dict[str, HorizontalLevel] = {}
            for symbol, ticker in list(self.watchlist.items()):
                if not self._running:
                    break
                try:
                    candles = await self.client.klines(
                        symbol, self.cfg.breakout.timeframe,
                        limit=self.cfg.breakout.lookback_candles + 5)
                    fresh = await self.client.ticker(symbol)
                    price = fresh.last_price if fresh else ticker.last_price
                    level = pick_level(candles, price, self.cfg)
                    if level is None:
                        self.levels.pop(symbol, None)
                        continue
                    self.levels[symbol] = level
                    if in_zone(price, level, self.cfg) or symbol in self.broker.positions:
                        hot[symbol] = level
                except Exception as exc:  # noqa: BLE001
                    log.debug("[%s] ошибка расчёта уровня: %s", symbol, exc)
            if hot != self.hot and hot:
                log.info("У уровня (%d): %s", len(hot),
                         ", ".join(f"{s} @ {l.describe()}" for s, l in hot.items()))
            self.hot = hot
            await asyncio.sleep(self.cfg.breakout.level_interval_sec)

    async def activity_loop(self) -> None:
        while self._running:
            started = time.monotonic()
            targets = dict(self.hot)
            # Монета с открытой позицией остаётся под замером, даже если цена
            # ушла из зоны: выход считается по активности, а не по цене.
            for symbol in self.broker.positions:
                if symbol not in targets and symbol in self.levels:
                    targets[symbol] = self.levels[symbol]

            for symbol, level in targets.items():
                if not self._running:
                    break
                try:
                    result = await self._snapshot(symbol, level)
                    if result is None:
                        continue
                    snapshot, ratio = result
                    position = self.broker.positions.get(symbol)
                    if position is not None:
                        await self._manage(symbol, position, snapshot)
                    elif self.tracker(symbol).ready:
                        await self._try_enter(symbol, level, snapshot, ratio)
                except Exception as exc:  # noqa: BLE001
                    log.debug("[%s] ошибка замера активности: %s", symbol, exc)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.5, self.cfg.breakout.activity_interval_sec - elapsed))

    def _hot_view(self) -> List[Dict]:
        """Что показывать на панели: за чем бот следит прямо сейчас."""
        out: List[Dict] = []
        for symbol, level in self.hot.items():
            snapshot = self._last.get(symbol)
            tracker = self.trackers.get(symbol)
            out.append({
                "symbol": symbol,
                "level": level.price,
                "touches": level.touches,
                "activity": round(snapshot.score, 3) if snapshot else 0.0,
                "trades_per_min": round(snapshot.trades_per_min) if snapshot else 0,
                "near_share_pct": round(snapshot.near_share * 100) if snapshot else 0,
                "ratio": round(tracker.ratio(snapshot), 2) if (tracker and snapshot and tracker.ready) else 0.0,
            })
        return out

    async def report_loop(self, interval: float = 60.0) -> None:
        while self._running:
            await asyncio.sleep(interval)
            stats = self.broker.stats()
            self.journal.save_state(list(self.broker.positions.values()), stats,
                                    list(self.watchlist), strategy="breakout",
                                    pending=self._hot_view(), version=self.cfg.version)
            log.info(
                "СТАТУС | наблюдаю %d монет, у уровня %d | сделок %d (W%d/L%d, winrate %.0f%%) "
                "| открыто %d | итог %+.2f$",
                len(self.watchlist), len(self.hot), stats["trades"], stats["wins"],
                stats["losses"], stats["winrate"], stats["open"], stats["net_pnl_usd"],
            )

    # ------------------------------------------------------------------ запуск

    async def run(self) -> None:
        b = self.cfg.breakout
        log.info("Режим: %s | ТС импульсного пробоя (только лонги) | размер сделки %.0f$ x%.0f "
                 "= %.0f$ | вход по активности у уровня | выход при её падении до %.0f%% от пика",
                 self.cfg.mode.upper(), self.cfg.risk.margin_usd, self.cfg.risk.leverage,
                 self.cfg.risk.notional_usd, b.activity_drop_ratio * 100)
        tasks = [
            asyncio.create_task(self.scan_loop(), name="scan"),
            asyncio.create_task(self.level_loop(), name="levels"),
            asyncio.create_task(self.activity_loop(), name="activity"),
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
                                list(self.watchlist), strategy="breakout",
                                pending=self._hot_view(), version=self.cfg.version)
        log.info("=" * 70)
        log.info("ИТОГ ДЕМО-СЕССИИ (ТС импульсного пробоя)")
        log.info("  сделок: %d | прибыльных: %d | убыточных: %d | winrate: %.1f%%",
                 stats["trades"], stats["wins"], stats["losses"], stats["winrate"])
        log.info("  чистый результат: %+.2f$", stats["net_pnl_usd"])
        log.info("  журнал сделок: %s", self.cfg.trades_csv)
        log.info("=" * 70)
