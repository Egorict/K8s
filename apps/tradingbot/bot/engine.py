"""Движок: связывает сканер, стратегию, стакан и демо-исполнение.

Три независимых цикла:
  * scan_loop      - раз в несколько минут обновляет список кандидатов (шаг 1 ТС);
  * setup_loop     - гоняет по кандидатам, ищет импульс и затуп (шаги 2-3 ТС);
  * position_loop  - ведёт открытые демо-позиции: стоп, тейк, откат, стакан (шаг 4-5).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

from .config import Config
from .exchange import BybitPublic
from .journal import Journal
from .models import Candle, Ticker
from .orderbook import BookManager
from .paper import PaperBroker, fmt_profit_factor
from .scanner import Candidate, Scanner
from .strategy import build_setup, find_impulse

log = logging.getLogger("engine")

# Как часто перекачивать свечи по каждому таймфрейму, секунд.
KLINE_TTL = {"1": 15.0, "5": 45.0, "15": 90.0}
# Сколько секунд монета остаётся "горячей" после подтверждения импульса.
HOT_TTL = 180.0


class Engine:
    def __init__(self, cfg: Config, client: BybitPublic):
        self.cfg = cfg
        self.client = client
        self.scanner = Scanner(client, cfg.screener)
        self.books = BookManager(client, cfg.orderbook)
        self.broker = PaperBroker(cfg)
        self.journal = Journal(cfg.trades_csv, cfg.signals_csv, cfg.state_json)

        self.watchlist: Dict[str, Candidate] = {}
        self._candles: Dict[Tuple[str, str], Tuple[float, List[Candle]]] = {}
        self._hot: Dict[str, float] = {}
        self._running = True

    # ------------------------------------------------------------------ данные

    async def _candles_for(self, symbol: str, timeframe: str) -> List[Candle]:
        key = (symbol, timeframe)
        ttl = KLINE_TTL.get(timeframe, 60.0)
        cached = self._candles.get(key)
        if cached and time.monotonic() - cached[0] < ttl:
            return cached[1]
        limit = self.cfg.impulse.volume_baseline_candles + self.cfg.impulse.max_candles + 10
        candles = await self.client.klines(symbol, timeframe, limit=limit)
        self._candles[key] = (time.monotonic(), candles)
        return candles

    def _forget(self, symbol: str) -> None:
        for tf in self.cfg.impulse.timeframes:
            self._candles.pop((symbol, tf), None)
        self._hot.pop(symbol, None)

    # ------------------------------------------------------------------ циклы

    async def scan_loop(self) -> None:
        while self._running:
            try:
                candidates = await self.scanner.scan()
                new = {c.symbol: c for c in candidates}
                # Монеты с открытой позицией не выбрасываем из наблюдения.
                for symbol in self.broker.positions:
                    if symbol not in new and symbol in self.watchlist:
                        new[symbol] = self.watchlist[symbol]
                for symbol in list(self.watchlist):
                    if symbol not in new:
                        self._forget(symbol)
                self.watchlist = new
                self.books.keep_only(list(new) + list(self.broker.positions))
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка сканирования: %s", exc)
            await asyncio.sleep(self.cfg.screener.rescan_interval_sec)

    async def _check_symbol(self, symbol: str, candidate: Candidate) -> None:
        candles_by_tf: Dict[str, List[Candle]] = {}
        for tf in self.cfg.impulse.timeframes:
            candles_by_tf[tf] = await self._candles_for(symbol, tf)

        # Быстрая проверка: есть ли вообще импульс, чтобы зря не дёргать стакан.
        confirmed = 0
        for tf, candles in candles_by_tf.items():
            if find_impulse(candles, tf, self.cfg).found:
                confirmed += 1
        if confirmed >= self.cfg.impulse.min_confirmed_timeframes:
            if symbol not in self._hot:
                log.info("[%s] импульс подтверждён на %d ТФ - беру стакан под наблюдение (%s)",
                         symbol, confirmed, candidate.reason)
            self._hot[symbol] = time.time()
        elif time.time() - self._hot.get(symbol, 0.0) > HOT_TTL:
            self._hot.pop(symbol, None)
            return

        # Копим историю стакана - без неё анти-спуфинг не работает.
        await self.books.poll(symbol)
        if self.cfg.orderbook.enabled and not self.books.ready(symbol):
            return

        allowed, why = self.broker.can_open(symbol)

        setup = build_setup(
            symbol=symbol,
            ticker=candidate.ticker,
            candles_by_tf=candles_by_tf,
            book=self.books.view(symbol),
            cfg=self.cfg,
        )
        if setup is None:
            return

        if not allowed:
            log.info("[%s] сетап есть (скор %.2f), но вход запрещён: %s", symbol, setup.score, why)
            self.journal.log_signal(setup, f"пропуск: {why}")
            return

        self.journal.log_signal(setup, "вход в шорт")
        self.broker.open_short(setup)

    async def setup_loop(self) -> None:
        while self._running:
            started = time.monotonic()
            for symbol, candidate in list(self.watchlist.items()):
                if not self._running:
                    break
                try:
                    await self._check_symbol(symbol, candidate)
                except Exception as exc:  # noqa: BLE001
                    log.debug("[%s] ошибка проверки сетапа: %s", symbol, exc)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self.cfg.setup_interval_sec - elapsed))

    async def position_loop(self) -> None:
        while self._running:
            for symbol, position in list(self.broker.positions.items()):
                try:
                    ticker: Optional[Ticker] = await self.client.ticker(symbol)
                    if ticker is None:
                        continue
                    book = await self.books.poll(symbol)
                    reason = self.broker.evaluate(position, ticker.last_price, book)
                    if reason:
                        trade = self.broker.close(position, ticker.last_price, reason)
                        self.journal.log_trade(trade)
                        self._forget(symbol)
                except Exception as exc:  # noqa: BLE001
                    log.debug("[%s] ошибка ведения позиции: %s", symbol, exc)
            await asyncio.sleep(self.cfg.position_interval_sec)

    async def report_loop(self, interval: float = 60.0) -> None:
        while self._running:
            await asyncio.sleep(interval)
            stats = self.broker.stats()
            self.journal.save_state(list(self.broker.positions.values()), stats,
                                    list(self.watchlist), strategy="impulse",
                                    version=self.cfg.version)
            log.info(
                "СТАТУС | наблюдаю %d монет, горячих %d | сделок %d (W%d/L%d, winrate %.0f%%) "
                "| открыто %d | итог %+.2f$",
                len(self.watchlist), len(self._hot), stats["trades"], stats["wins"],
                stats["losses"], stats["winrate"], stats["open"], stats["net_pnl_usd"],
            )
            for p in self.broker.positions.values():
                pnl = self.broker.net_pnl(p, p.last_price or p.entry_price)
                log.info("   %s шорт от %.8g, сейчас %.8g, PnL %+.2f$, %.0f мин",
                         p.symbol, p.entry_price, p.last_price, pnl, p.age_sec() / 60)

    # ------------------------------------------------------------------ запуск

    async def run(self) -> None:
        log.info("Режим: %s | размер сделки %.0f$ x%.0f = %.0f$ | стоп -%.0f$ | тейк +%.0f$",
                 self.cfg.mode.upper(), self.cfg.risk.margin_usd, self.cfg.risk.leverage,
                 self.cfg.risk.notional_usd, self.cfg.risk.stop_loss_usd,
                 self.cfg.risk.take_profit_usd)
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
        self.journal.save_state(list(self.broker.positions.values()), stats, list(self.watchlist))
        log.info("=" * 70)
        log.info("ИТОГ ДЕМО-СЕССИИ")
        log.info("  сделок: %d | прибыльных: %d | убыточных: %d | winrate: %.1f%%",
                 stats["trades"], stats["wins"], stats["losses"], stats["winrate"])
        log.info("  средняя прибыль: %+.2f$ | средний убыток: %+.2f$ | профит-фактор: %s",
                 stats["avg_win"], stats["avg_loss"], fmt_profit_factor(stats["profit_factor"]))
        log.info("  чистый результат: %+.2f$", stats["net_pnl_usd"])
        log.info("  открытых позиций осталось: %d", stats["open"])
        log.info("  журнал сделок: %s", self.cfg.trades_csv)
        log.info("=" * 70)
