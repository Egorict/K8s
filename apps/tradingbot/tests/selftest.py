"""Самопроверка без сети: синтетические свечи и стакан.

Запуск: python run.py selftest
Проверяет, что стратегия действительно ловит импульс + затуп, что стакан
отсеивает спуферов, и что демо-исполнение корректно считает стоп/тейк/PnL.
"""
from __future__ import annotations

import time
from typing import List

from bot.config import Config
from bot.models import Candle, Level, Ticker
from bot.orderbook import BookTracker
from bot.paper import PaperBroker
from bot.strategy import build_setup, find_impulse, find_stall


def _candles(flat: int, impulse: int, stall: int, start: float = 1.0) -> List[Candle]:
    """Фон -> импульсный рост без просадок -> затухание (затуп)."""
    out: List[Candle] = []
    ts = int(time.time() * 1000) - (flat + impulse + stall) * 60_000
    price = start

    for _ in range(flat):
        o = price
        c = price * 1.0005
        out.append(Candle(ts, o, max(o, c) * 1.001, min(o, c) * 0.999, c, 1000.0))
        price, ts = c, ts + 60_000

    for i in range(impulse):
        o = price
        c = price * 1.012                      # +1.2% за свечу, без откатов
        high = c * 1.002
        low = o * 0.9995
        out.append(Candle(ts, o, high, low, c, 4000.0 - i * 100))
        price, ts = c, ts + 60_000

    peak = price
    for i in range(stall):
        o = price
        c = price * 0.9995                     # рост встал, пошли верхние тени
        high = peak * 1.0002
        low = c * 0.999
        out.append(Candle(ts, o, high, low, c, 900.0))
        price, ts = c, ts + 60_000

    return out


def _check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")
    return ok


def run_selftest(cfg: Config) -> None:
    print("Самопроверка стратегии (синтетические данные, сеть не нужна)\n")
    results: List[bool] = []

    # --- 1. Импульс ---------------------------------------------------------
    candles = _candles(flat=60, impulse=8, stall=3)
    imp = find_impulse(candles, "1", cfg)
    results.append(_check("импульс найден на 1m", imp.found, imp.reason))
    results.append(_check("окно импульса 4-12 свечей",
                          cfg.impulse.min_candles <= imp.candles <= cfg.impulse.max_candles,
                          f"{imp.candles} свечей"))
    results.append(_check("просадка внутри импульса мала",
                          imp.max_retrace_ratio <= cfg.impulse.max_retrace_ratio,
                          f"{imp.max_retrace_ratio:.2f}"))

    # Ровный боковик импульсом считаться не должен.
    flat_only = _candles(flat=80, impulse=0, stall=0)
    results.append(_check("на боковике импульса нет", not find_impulse(flat_only, "1", cfg).found))

    # --- 2. Затуп -----------------------------------------------------------
    stall = find_stall(candles, imp, cfg)
    results.append(_check("затуп распознан", stall.stalled, stall.reason))

    still_running = _candles(flat=60, impulse=10, stall=0)
    imp2 = find_impulse(still_running, "1", cfg)
    results.append(_check("на разгоне входа нет", not find_stall(still_running, imp2, cfg).stalled))

    # --- 3. Стакан и спуфинг -------------------------------------------------
    ocfg = cfg.orderbook
    tracker = BookTracker(cfg=ocfg, symbol="TESTUSDT")
    view = None
    for step in range(ocfg.min_persist_snapshots + 3):
        bids = [Level(100.0 - i * 0.05, 100.0) for i in range(20)]
        asks = [Level(100.05 + i * 0.05, 100.0) for i in range(20)]
        asks[6] = Level(asks[6].price, 100.0 * ocfg.density_multiplier * 3)   # настоящая стена
        if step < 2:
            bids[6] = Level(bids[6].price, 100.0 * ocfg.density_multiplier * 3)  # спуфер, потом снимет
        view = tracker.update(bids, asks)

    results.append(_check("настоящая ask-стена найдена", view.genuine_ask_wall is not None,
                          view.genuine_ask_wall.describe() if view.genuine_ask_wall else "нет"))
    results.append(_check("спуферская bid-стена отсеяна", view.genuine_bid_wall is None))
    results.append(_check("спуфинг зафиксирован", view.spoof_count > 0, f"событий: {view.spoof_count}"))

    # --- 4. Полный сетап -----------------------------------------------------
    ticker = Ticker(symbol="TESTUSDT", last_price=candles[-1].close, change_24h=0.25,
                    turnover_24h=20_000_000, volume_24h=1_000_000)
    candles_by_tf = {"1": candles, "5": candles, "15": candles}
    setup = build_setup("TESTUSDT", ticker, candles_by_tf, view, cfg)
    results.append(_check("сетап собран целиком", setup is not None,
                          f"скор {setup.score:.2f}" if setup else "сетапа нет"))

    # --- 5. Демо-исполнение --------------------------------------------------
    if setup is not None:
        broker = PaperBroker(cfg)
        pos = broker.open_short(setup)
        risk = cfg.risk
        results.append(_check("размер позиции = маржа * плечо",
                              abs(pos.qty * pos.entry_price - risk.notional_usd) < 0.01,
                              f"{pos.qty * pos.entry_price:.2f}$"))
        results.append(_check("стоп выше входа (шорт)", pos.stop_price > pos.entry_price))
        results.append(_check("тейк ниже входа (шорт)", pos.take_price < pos.entry_price))

        loss = broker.net_pnl(pos, pos.stop_price)
        profit = broker.net_pnl(pos, pos.take_price)
        results.append(_check("убыток по стопу ≈ -5$", abs(loss + risk.stop_loss_usd) < 0.05,
                              f"{loss:+.2f}$"))
        results.append(_check("прибыль по тейку ≈ +15$", abs(profit - risk.take_profit_usd) < 0.05,
                              f"{profit:+.2f}$"))

        # Цена пошла вниз, потом вернулась - "прогноз не сбылся".
        broker.evaluate(pos, pos.entry_price * 0.985, None)
        reason = broker.evaluate(pos, pos.entry_price * 0.999, None)
        results.append(_check("выход по несостоявшемуся падению", bool(reason), reason or "нет"))
        if reason:
            trade = broker.close(pos, pos.entry_price * 0.999, reason)
            results.append(_check("сделка записана в журнал брокера",
                                  len(broker.closed) == 1, f"PnL {trade.net_pnl_usd:+.2f}$"))

    passed = sum(1 for r in results if r)
    print(f"\nИтог: {passed}/{len(results)} проверок пройдено")
    if passed != len(results):
        raise SystemExit(1)
