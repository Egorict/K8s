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

    # --- 6. ТС плотностей ----------------------------------------------------
    results.extend(run_density_selftest(cfg))

    passed = sum(1 for r in results if r)
    print(f"\nИтог: {passed}/{len(results)} проверок пройдено")
    if passed != len(results):
        raise SystemExit(1)


def _book_snapshot(wall_index: int = 6, wall_mult: float = 24.0, with_wall: bool = True,
                   wall_shrink: float = 1.0) -> tuple:
    """Стакан вокруг 100$: ровные уровни плюс одна плотность на стороне bid.

    wall_shrink=0.1 - от плотности осталось 10% (её едят);
    with_wall=False - уровень исчез из стакана совсем (заявку сняли).
    """
    bids = [Level(100.0 - i * 0.05, 100.0) for i in range(20)]
    asks = [Level(100.05 + i * 0.05, 100.0) for i in range(20)]
    if with_wall:
        bids[wall_index] = Level(bids[wall_index].price, 100.0 * wall_mult * wall_shrink)
    else:
        del bids[wall_index]
    return bids, asks


def run_density_selftest(cfg: Config) -> List[bool]:
    """ТС плотностей: поиск стены, лимитка перед ней, выходы по судьбе стены."""
    import dataclasses
    import tempfile

    from bot.density import DensityEngine, find_wall_setup
    from bot.models import PendingOrder

    print("\nТС плотностей (синтетический стакан)")
    results: List[bool] = []
    dcfg = cfg.density

    # Движок пишет журнал, поэтому на время проверки уводим data_dir во временный
    # каталог - настоящие journals бота самопроверка портить не должна.
    original_dir = cfg.data_dir
    cfg.data_dir = tempfile.mkdtemp(prefix="selftest-density-")
    try:
        engine = DensityEngine(cfg, client=None)  # сеть не понадобится
        tracker = engine.books.tracker("TESTUSDT")

        # Плотность стоит много снапшотов подряд - анти-спуфинг её пропускает.
        view = None
        for _ in range(dcfg.min_persist_snapshots + 2):
            view = tracker.update(*_book_snapshot())
        results.append(_check("настоящая bid-плотность найдена", view.genuine_bid_wall is not None,
                              view.genuine_bid_wall.describe() if view.genuine_bid_wall else "нет"))

        ticker = Ticker(symbol="TESTUSDT", last_price=view.mid, change_24h=0.02,
                        turnover_24h=150_000_000, volume_24h=1_500_000)
        setup = find_wall_setup(ticker, view, cfg)
        results.append(_check("сетап от плотности собран", setup is not None,
                              f"скор {setup.score:.2f}" if setup else "нет"))

        if setup is not None:
            results.append(_check("сторона сделки от bid-стены - покупка", setup.side == "long",
                                  setup.side))
            results.append(_check("лимитка стоит перед плотностью, а не в ней",
                                  setup.entry_price > setup.wall.price,
                                  f"{setup.entry_price:.4f} > {setup.wall.price:.4f}"))
            results.append(_check("лимитка ниже рынка (осталась лимиткой)",
                                  setup.entry_price < view.best_ask,
                                  f"{setup.entry_price:.4f} < {view.best_ask:.4f}"))

        # Короткоживущая стена (спуфер) сетапа давать не должна.
        spoof_tracker = BookTracker(cfg=engine.books.cfg, symbol="SPOOFUSDT")
        spoof_view = None
        for _ in range(3):
            spoof_view = spoof_tracker.update(*_book_snapshot())
        results.append(_check("спуферская плотность сетапа не даёт",
                              find_wall_setup(ticker, spoof_view, cfg) is None))

        if setup is None:
            return results

        # --- исполнение лимитки ---------------------------------------------
        order = PendingOrder(symbol=setup.symbol, side=setup.side, price=setup.entry_price,
                             qty=cfg.risk.notional_usd / setup.entry_price,
                             placed_at=time.time(), setup=setup)
        results.append(_check("лимитка не исполняется, пока рынок выше",
                              not engine._filled(order, view)))
        touched = dataclasses.replace(view, best_ask=order.price, best_bid=order.price - 0.01)
        results.append(_check("лимитка исполняется, когда рынок дошёл до цены",
                              engine._filled(order, touched)))

        position = engine.broker.open_from_limit(order)
        results.append(_check("вход строго по цене лимитки (без проскальзывания)",
                              abs(position.entry_price - order.price) < 1e-12))
        results.append(_check("тейк выше входа (лонг)", position.take_price > position.entry_price))
        results.append(_check("стоп ниже входа (лонг)", position.stop_price < position.entry_price))

        profit = engine.broker.net_pnl(position, position.take_price)
        results.append(_check(f"прибыль по тейку ≈ +{dcfg.take_profit_usd:.0f}$",
                              abs(profit - dcfg.take_profit_usd) < 0.05, f"{profit:+.2f}$"))
        loss = engine.broker.net_pnl(position, position.stop_price)
        results.append(_check("стоп за плотностью ближе денежного",
                              abs(loss) < dcfg.stop_loss_usd, f"{loss:+.2f}$"))

        # --- выходы по судьбе плотности --------------------------------------
        hold = engine._exit_reason(position, view, position.entry_price)
        results.append(_check("пока плотность цела - держим", hold is None, hold or "держим"))

        # Плотность едят: осталось 10% от максимума.
        eaten_view = None
        for _ in range(2):
            eaten_view = tracker.update(*_book_snapshot(wall_shrink=0.10))
        reason = engine._exit_reason(position, eaten_view, position.entry_price)
        results.append(_check(f"выход при съедании {dcfg.wall_eaten_ratio * 100:.0f}% плотности",
                              bool(reason) and "съедена" in (reason or ""), reason or "нет"))

        # Заявку сняли: уровень пропал из стакана.
        gone_tracker = BookTracker(cfg=engine.books.cfg, symbol="TESTUSDT")
        for _ in range(dcfg.min_persist_snapshots + 2):
            gone_tracker.update(*_book_snapshot())
        gone_view = None
        for _ in range(3):
            gone_view = gone_tracker.update(*_book_snapshot(with_wall=False))
        engine.books._trackers["TESTUSDT"] = gone_tracker
        reason = engine._exit_reason(position, gone_view, position.entry_price)
        results.append(_check("выход, когда плотность сняли",
                              bool(reason) and "снята" in (reason or ""), reason or "нет"))
    finally:
        cfg.data_dir = original_dir

    return results
