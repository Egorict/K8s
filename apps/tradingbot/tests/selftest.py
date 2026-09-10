"""Самопроверка без сети: синтетические свечи и стакан.

Запуск: python run.py selftest
Проверяет, что стратегия действительно ловит импульс + затуп, что стакан
отсеивает спуферов, и что демо-исполнение корректно считает стоп/тейк/PnL.
"""
from __future__ import annotations

import time
from typing import List

from bot.config import Config
from bot.models import Candle, Level, PlainSetup, Ticker, TradePrint
from bot.orderbook import BookTracker
from bot.paper import PaperBroker
from bot.breakout import build_setup, in_zone
from bot.strategy import build_setup as build_impulse_setup, find_impulse, find_stall


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
    setup = build_impulse_setup("TESTUSDT", ticker, candles_by_tf, view, cfg)
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

    # --- 7. ТС пробоя уровня -------------------------------------------------
    results.extend(run_breakout_selftest(cfg))

    # --- 8. ТС биткоина ------------------------------------------------------
    results.extend(run_btc_selftest(cfg))

    # --- 9. Журнал: state.json обязан быть валидным JSON ---------------------
    results.extend(run_json_selftest(cfg))

    # --- 10. Реестр версий ---------------------------------------------------
    results.extend(run_versions_selftest(cfg))

    # --- 11. ТС пробоя, версия 0.2 -------------------------------------------
    results.extend(run_breakout_v2_selftest(cfg))

    # --- 12. ТС плотностей, версия 0.2 ---------------------------------------
    results.extend(run_density_v2_selftest(cfg))

    # --- 13. Бэктест: реплей истории -----------------------------------------
    results.extend(run_backtest_selftest(cfg))

    passed = sum(1 for r in results if r)
    print(f"\nИтог: {passed}/{len(results)} проверок пройдено")
    if passed != len(results):
        raise SystemExit(1)


def _book_snapshot(wall_index: int = 6, wall_mult: float = 24.0, with_wall: bool = True,
                   wall_shrink: float = 1.0, wall_side: str = "bid") -> tuple:
    """Стакан вокруг 100$: ровные уровни плюс одна плотность.

    wall_side - на какой стороне стоит плотность (нужно версии 0.2, которая
    торгует те же стены в обратную сторону);
    wall_shrink=0.1 - от плотности осталось 10% (её едят);
    with_wall=False - уровень исчез из стакана совсем (заявку сняли).
    """
    bids = [Level(100.0 - i * 0.05, 100.0) for i in range(20)]
    asks = [Level(100.05 + i * 0.05, 100.0) for i in range(20)]
    book = bids if wall_side == "bid" else asks
    if with_wall:
        book[wall_index] = Level(book[wall_index].price, 100.0 * wall_mult * wall_shrink)
    else:
        del book[wall_index]
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


def _level_candles(touches: int = 4, level: float = 100.0, spacing: int = 14,
                   breakout: bool = True, rejection: bool = True) -> List[Candle]:
    """Свечи с явным уровнем: несколько разнесённых касаний и отбои вниз.

    rejection=False - цена упирается в уровень, но не отбивается (полка).
    Такой уровень явным считаться не должен.
    """
    out: List[Candle] = []
    ts = int(time.time() * 1000) - 400 * 900_000
    price = level * 0.94

    for _ in range(touches):
        for _ in range(spacing // 2):                      # подход к уровню
            o = price
            c = price * 1.004
            out.append(Candle(ts, o, max(o, c) * 1.0008, min(o, c) * 0.9992, c, 1000.0))
            price, ts = c, ts + 900_000
        o = price                                          # касание
        c = level * 0.995
        out.append(Candle(ts, o, level, min(o, c) * 0.999, c, 1400.0))
        price, ts = c, ts + 900_000
        # Отбой вниз: без него уровень не «явный».
        step = 0.996 if rejection else 0.9998
        for _ in range(spacing // 2):
            o = price
            c = price * step
            out.append(Candle(ts, o, max(o, c) * 1.0008, min(o, c) * 0.9992, c, 900.0))
            price, ts = c, ts + 900_000

    while price < level * 0.999:                           # возврат к уровню
        o = price
        c = min(price * 1.005, level * 0.999)
        out.append(Candle(ts, o, max(o, c), min(o, c) * 0.999, c, 1000.0))
        price, ts = c, ts + 900_000

    o = price
    if breakout:
        c = level * 1.004
        out.append(Candle(ts, o, c * 1.0005, o * 0.999, c, 4000.0))
    else:
        c = level * 0.998
        out.append(Candle(ts, o, level, o * 0.995, c, 1200.0))
    return out


def _shelf_candles(level: float = 100.0, n: int = 120) -> List[Candle]:
    """Полка: цена липнет под уровнем и касается его, но НЕ отбивается.

    Формально касаний много, а уровня как такового нет - рынок на него никак
    не реагирует. Такое сопротивление ТС брать не должна.
    """
    out: List[Candle] = []
    ts = int(time.time() * 1000) - n * 900_000
    for i in range(n):
        o = level * 0.9990
        c = level * 0.9992
        high = level if i % 6 == 0 else level * 0.9995   # регулярные касания
        low = level * 0.9985                             # отбой всего 0.15%
        out.append(Candle(ts, o, high, low, c, 1000.0))
        ts += 900_000
    return out


def _trend_candles(up: bool = True, n: int = 90, start: float = 100.0) -> List[Candle]:
    """Ровный тренд вверх или вниз - для проверки фильтра тренда."""
    out: List[Candle] = []
    ts = int(time.time() * 1000) - n * 900_000
    price = start
    step = 1.004 if up else 0.996
    for _ in range(n):
        o = price
        c = price * step
        out.append(Candle(ts, o, max(o, c) * 1.001, min(o, c) * 0.999, c, 1000.0))
        price, ts = c, ts + 900_000
    return out


def _bounce_in_downtrend(n: int = 90, start: float = 100.0) -> List[Candle]:
    """Падение, а в конце короткий отскок - классическая ловушка для пробоя."""
    out = _trend_candles(up=False, n=n - 12, start=start)
    price = out[-1].close
    ts = out[-1].ts + 900_000
    for _ in range(12):
        o = price
        c = price * 1.006
        out.append(Candle(ts, o, max(o, c) * 1.001, min(o, c) * 0.999, c, 1500.0))
        price, ts = c, ts + 900_000
    return out


def _prints(count: int, level: float, span_sec: float, near_share: float = 0.8,
            swings: bool = True, buy_share: float = 0.7, notional: float = 900.0
            ) -> List[TradePrint]:
    """Синтетическая лента принтов вокруг уровня.

    count за span_sec задаёт темп; near_share - какая доля сделок прошла в зоне
    уровня; swings - чередуются ли стороны (цена «колеблется»).
    """
    out: List[TradePrint] = []
    now_ms = int(time.time() * 1000)
    step_ms = max(1.0, span_sec * 1000 / max(count, 1))
    for i in range(count):
        ts = int(now_ms - i * step_ms)
        if i < count * near_share:
            # В зоне уровня: чередуем стороны, чтобы получились колебания.
            offset = 0.0008 if (swings and i % 2 == 0) else -0.0008
            price = level * (1 + offset)
        else:
            price = level * 0.985          # заметно в стороне от уровня
        side = "Buy" if i % 10 < buy_share * 10 else "Sell"
        out.append(TradePrint(ts=ts, price=price, size=notional / price, side=side))
    return out


def run_breakout_selftest(cfg: Config) -> List[bool]:
    """ТС импульсного пробоя: тренд, явный уровень, вход и выход по активности."""
    import tempfile

    from bot.activity import ActivityTracker, measure
    from bot.breakout import BreakoutEngine, TrendScanner, build_setup, in_zone, pick_level
    from bot.levels import find_levels

    print("\nТС импульсного пробоя (синтетические свечи и лента)")
    results: List[bool] = []
    b = cfg.breakout
    level_price = 100.0

    # --- тренд ---------------------------------------------------------------
    scanner = TrendScanner(client=None, cfg=cfg)
    ok_up, why_up = scanner._uptrend(_trend_candles(up=True))
    results.append(_check("восходящий тренд распознан", ok_up, why_up))
    ok_down, why_down = scanner._uptrend(_trend_candles(up=False))
    results.append(_check("нисходящий тренд отсеян", not ok_down, why_down))
    ok_bounce, why_bounce = scanner._uptrend(_bounce_in_downtrend())
    results.append(_check("отскок внутри падения отсеян", not ok_bounce, why_bounce))

    # --- явный уровень -------------------------------------------------------
    candles = _level_candles()
    levels = find_levels(candles, side="resistance", tolerance=b.level_tolerance,
                         min_touches=b.min_touches, window=b.pivot_window,
                         min_spacing=b.min_touch_spacing, min_rejection=b.min_rejection_pct)
    results.append(_check("явный уровень найден", bool(levels),
                          levels[0].describe() if levels else "нет"))
    if levels:
        results.append(_check("касаний 2 и больше", levels[0].touches >= b.min_touches,
                              str(levels[0].touches)))
        results.append(_check("отбои от уровня зафиксированы",
                              levels[0].rejection >= b.min_rejection_pct,
                              f"{levels[0].rejection * 100:.1f}%"))

    shelf = _shelf_candles()
    weak = find_levels(shelf, side="resistance", tolerance=b.level_tolerance,
                       min_touches=b.min_touches, window=b.pivot_window,
                       min_spacing=b.min_touch_spacing, min_rejection=b.min_rejection_pct)
    results.append(_check("полка без отбоев явным уровнем не считается", not weak,
                          weak[0].describe() if weak else "отсеяна"))
    # ...а без требования отбоя тот же набор свечей уровень бы дал - значит
    # отсеивает именно проверка реакции цены, а не нехватка касаний.
    naive = find_levels(shelf, side="resistance", tolerance=b.level_tolerance,
                        min_touches=b.min_touches, window=b.pivot_window)
    results.append(_check("без проверки отбоя та же полка проходит - фильтр работает",
                          bool(naive), naive[0].describe() if naive else "нет"))

    level = pick_level(candles, candles[-1].close, cfg)
    results.append(_check("уровень выбран для работы", level is not None,
                          level.describe() if level else "нет"))
    if level is None:
        return results

    # --- зона входа: обе стороны уровня --------------------------------------
    results.append(_check("вход разрешён НАД уровнем",
                          in_zone(level.price * 1.002, level, cfg)))
    results.append(_check("вход разрешён ПОД уровнем",
                          in_zone(level.price * 0.998, level, cfg)))
    results.append(_check("далеко от уровня входа нет",
                          not in_zone(level.price * 1.05, level, cfg)))

    # --- замер активности ----------------------------------------------------
    bids = [Level(level.price * (1 - 0.0005 * i), 60.0) for i in range(1, 12)]
    asks = [Level(level.price * (1 + 0.0005 * i), 60.0) for i in range(1, 12)]

    live = measure(_prints(400, level.price, span_sec=60), bids, asks, level.price, b)
    results.append(_check("активная лента даёт высокий скор", live.score >= b.min_entry_score,
                          f"{live.score:.2f} | {live.describe()}"))
    results.append(_check("колебания у уровня посчитаны", live.swings >= b.min_swings,
                          str(live.swings)))
    results.append(_check("доля оборота у уровня посчитана", live.near_share >= b.min_near_share,
                          f"{live.near_share * 100:.0f}%"))

    quiet = measure(_prints(12, level.price, span_sec=60, near_share=0.2, swings=False),
                    bids, asks, level.price, b)
    results.append(_check("вялая лента скор не даёт", quiet.score < b.min_entry_score,
                          f"{quiet.score:.2f}"))

    # Активность в стороне от уровня входом считаться не должна.
    aside = measure(_prints(400, level.price, span_sec=60, near_share=0.0),
                    bids, asks, level.price, b)
    results.append(_check("торговля в стороне от уровня не считается активностью",
                          aside.near_share < b.min_near_share,
                          f"{aside.near_share * 100:.0f}%"))

    # --- вход ----------------------------------------------------------------
    ticker = Ticker(symbol="TESTUSDT", last_price=level.price * 0.999, change_24h=0.12,
                    turnover_24h=50_000_000, volume_24h=1_000_000)
    setup = build_setup(ticker, level, live, ratio=2.5, cfg=cfg)
    results.append(_check("сетап собран по активности", setup is not None,
                          f"скор {setup.score:.2f}" if setup else "нет"))
    if setup is not None:
        results.append(_check("сделка только в лонг", setup.side == "long", setup.side))
        results.append(_check("вход возможен ДО пробоя (цена под уровнем)",
                              setup.price < level.price,
                              f"{setup.price:.4f} < {level.price:.4f}"))

    results.append(_check("без превышения фона входа нет",
                          build_setup(ticker, level, live, ratio=1.0, cfg=cfg) is None))
    results.append(_check("на вялой активности входа нет",
                          build_setup(ticker, level, quiet, ratio=3.0, cfg=cfg) is None))

    sellers = measure(_prints(400, level.price, span_sec=60, buy_share=0.2),
                      bids, asks, level.price, b)
    results.append(_check("при продавцах-агрессорах входа нет",
                          build_setup(ticker, level, sellers, ratio=3.0, cfg=cfg) is None))

    if setup is None:
        return results

    # --- выход по падению активности -----------------------------------------
    original_dir = cfg.data_dir
    cfg.data_dir = tempfile.mkdtemp(prefix="selftest-breakout-")
    try:
        engine = BreakoutEngine(cfg, client=None)
        position = engine.broker.open_market(
            setup, stop_usd=b.stop_loss_usd, take_usd=b.take_profit_usd,
            stop_price_limit=level.price * (1.0 - b.invalidation_pct))
        engine._entry_levels[setup.symbol] = level.price
        tracker = engine.tracker(setup.symbol)
        tracker.reset_peak()
        tracker.note_peak(live)

        entry = position.entry_price
        results.append(_check("пока активность держится - сидим в сделке",
                              engine._exit_reason(position, entry, live, tracker) is None))

        # Ключевая проверка: цена НЕ изменилась, упала только активность.
        fading = measure(_prints(30, level.price, span_sec=60, near_share=0.3, swings=False),
                         bids, asks, level.price, b)
        reason = engine._exit_reason(position, entry, fading, tracker)
        results.append(_check("выход при падении активности (цена та же)",
                              bool(reason) and "активность" in (reason or ""),
                              reason or "нет"))

        results.append(_check("пик активности отслеживается",
                              tracker.peak_score >= live.score,
                              f"пик {tracker.peak_score:.2f}"))

        # Тейк и уход под уровень остаются предохранителями.
        take = engine._exit_reason(position, position.take_price, live, tracker)
        results.append(_check("тейк после пробоя работает", "тейк" in (take or ""),
                              take or "нет"))
        broken = engine._exit_reason(position, level.price * 0.99, live, tracker)
        results.append(_check("уход под уровень закрывает сделку",
                              "уровень" in (broken or ""), broken or "нет"))
    finally:
        cfg.data_dir = original_dir
    return results


def _btc_candles(dip_pct: float = 0.025, support_touches: int = 3) -> List[Candle]:
    """BTC: боковик с поддержкой, затем свежий пролив к ней."""
    out: List[Candle] = []
    ts = int(time.time() * 1000) - 200 * 300_000
    high = 100_000.0
    support = high * (1 - dip_pct)

    for _ in range(support_touches):
        price = high
        for _ in range(8):                                 # снижение к поддержке
            o = price
            c = max(price * 0.997, support * 1.001)
            out.append(Candle(ts, o, o * 1.0005, min(o, c) * 0.9995, c, 500.0))
            price, ts = c, ts + 300_000
        o = price                                          # касание поддержки
        c = support * 1.003
        out.append(Candle(ts, o, o * 1.0005, support, c, 900.0))
        price, ts = c, ts + 300_000
        for _ in range(8):                                 # возврат к максимуму
            o = price
            c = min(price * 1.004, high)
            out.append(Candle(ts, o, max(o, c) * 1.0005, o * 0.9995, c, 600.0))
            price, ts = c, ts + 300_000

    price = high                                           # свежий пролив
    while price > support * 1.002:
        o = price
        c = price * 0.996
        out.append(Candle(ts, o, o * 1.0002, c * 0.9995, c, 800.0))
        price, ts = c, ts + 300_000
    return out


def run_btc_selftest(cfg: Config) -> List[bool]:
    """ТС биткоина: просадка + опора, цели +5$/-2$."""
    import tempfile

    from bot.btc import BtcEngine, find_dip

    print("\nТС биткоина (синтетические свечи)")
    results: List[bool] = []
    bcfg = cfg.btc

    candles = _btc_candles()
    ticker = Ticker(symbol=bcfg.symbol, last_price=candles[-1].close, change_24h=-0.01,
                    turnover_24h=2_000_000_000, volume_24h=50_000)

    setup = find_dip(ticker, candles, None, cfg)
    results.append(_check("просадка с поддержкой распознана", setup is not None,
                          " | ".join(setup.notes) if setup else "нет"))
    if setup is not None:
        results.append(_check("сделка только в лонг", setup.side == "long", setup.side))
        results.append(_check("глубина просадки в заданных рамках",
                              bcfg.min_dip_pct <= setup.extra["dip_pct"] <= bcfg.max_dip_pct,
                              f"{setup.extra['dip_pct'] * 100:.2f}%"))

    flat = _btc_candles(dip_pct=0.002)
    flat_ticker = Ticker(symbol=bcfg.symbol, last_price=flat[-1].close, change_24h=0.0,
                         turnover_24h=2_000_000_000, volume_24h=50_000)
    results.append(_check("без просадки входа нет",
                          find_dip(flat_ticker, flat, None, cfg) is None))

    # Обвал ниже всех уровней: опоры под ценой нет, ловить нож запрещено.
    crash = list(candles)
    deep = crash[-1].close * 0.90
    crash.append(Candle(crash[-1].ts + 300_000, crash[-1].close, crash[-1].close,
                        deep, deep, 5000.0))
    crash_ticker = Ticker(symbol=bcfg.symbol, last_price=deep, change_24h=-0.09,
                          turnover_24h=2_000_000_000, volume_24h=50_000)
    results.append(_check("в свободном падении без опоры входа нет",
                          find_dip(crash_ticker, crash, None, cfg) is None))

    if setup is None:
        return results

    original_dir = cfg.data_dir
    cfg.data_dir = tempfile.mkdtemp(prefix="selftest-btc-")
    try:
        engine = BtcEngine(cfg, client=None)
        position = engine.broker.open_market(setup, stop_usd=bcfg.stop_loss_usd,
                                             take_usd=bcfg.take_profit_usd)
        profit = engine.broker.net_pnl(position, position.take_price)
        loss = engine.broker.net_pnl(position, position.stop_price)
        results.append(_check(f"прибыль по тейку ≈ +{bcfg.take_profit_usd:.0f}$",
                              abs(profit - bcfg.take_profit_usd) < 0.05, f"{profit:+.2f}$"))
        results.append(_check(f"убыток по стопу ≈ -{bcfg.stop_loss_usd:.0f}$",
                              abs(loss + bcfg.stop_loss_usd) < 0.05, f"{loss:+.2f}$"))
        results.append(_check("пока цена между уровнями - держим",
                              engine._exit_reason(position, position.entry_price) is None))
        take_reason = engine._exit_reason(position, position.take_price)
        results.append(_check("выход по тейку", "тейк" in (take_reason or ""),
                              take_reason or "нет"))
        stop_reason = engine._exit_reason(position, position.stop_price)
        results.append(_check("выход по стопу", "стоп" in (stop_reason or ""),
                              stop_reason or "нет"))
    finally:
        cfg.data_dir = original_dir
    return results


def run_json_selftest(cfg: Config) -> List[bool]:
    """state.json обязан быть валидным JSON при любых значениях статистики.

    Регрессия 08.09.2026: при прибыльных сделках без единого убытка
    profit_factor становился float("inf"), json.dump писал литерал Infinity,
    и панель падала на JSON.parse, показывая живого бота как недоступного.
    Стандартной библиотеке Python такой файл читать не мешает - поэтому
    проверять надо строгим парсером, а не json.load.
    """
    import json
    import tempfile

    from bot.journal import Journal
    from bot.paper import PaperBroker, fmt_profit_factor

    print("\nЖурнал: валидность state.json")
    results: List[bool] = []

    original_dir = cfg.data_dir
    cfg.data_dir = tempfile.mkdtemp(prefix="selftest-json-")
    try:
        broker = PaperBroker(cfg)
        # Сделка в плюс и ни одной в минус - тот самый случай.
        setup = PlainSetup(symbol="TESTUSDT", side="long", price=100.0, timeframe="5",
                           score=0.7, ticker=Ticker(symbol="TESTUSDT", last_price=100.0,
                                                    change_24h=0.05, turnover_24h=1e8,
                                                    volume_24h=1e6))
        position = broker.open_market(setup, stop_usd=2.0, take_usd=5.0)
        broker.close(position, position.take_price, "тейк-профит")

        stats = broker.stats()
        results.append(_check("без убытков профит-фактор не бесконечность",
                              stats["profit_factor"] is None,
                              repr(stats["profit_factor"])))
        results.append(_check("профит-фактор для лога форматируется",
                              fmt_profit_factor(stats["profit_factor"]) == "∞",
                              fmt_profit_factor(stats["profit_factor"])))

        journal = Journal(cfg.trades_csv, cfg.signals_csv, cfg.state_json)
        journal.save_state(list(broker.positions.values()), stats, ["TESTUSDT"],
                           strategy="selftest")
        raw = open(cfg.state_json, encoding="utf-8").read()
        results.append(_check("в файле нет литерала Infinity",
                              "Infinity" not in raw and "NaN" not in raw))
        # parse_constant срабатывает ровно на Infinity/-Infinity/NaN - то есть
        # ведёт себя как строгий парсер браузера, в отличие от json.load.
        try:
            json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(
                ValueError(f"недопустимая константа {c}")))
            strict_ok, detail = True, "разобран строгим парсером"
        except ValueError as exc:
            strict_ok, detail = False, str(exc)
        results.append(_check("state.json проходит строгий разбор", strict_ok, detail))

        # Прямая защита _json_safe: любая бесконечность превращается в null.
        from bot.journal import _json_safe
        cleaned = _json_safe({"a": float("inf"), "b": [float("nan"), 1.5], "c": "ok"})
        results.append(_check("_json_safe чистит inf и nan",
                              cleaned == {"a": None, "b": [None, 1.5], "c": "ok"},
                              repr(cleaned)))
    finally:
        cfg.data_dir = original_dir
    return results


def run_versions_selftest(cfg: Config) -> List[bool]:
    """Реестр версий: у каждой ТС есть версии, overrides применяются, опечатки ловятся."""
    import dataclasses

    from bot import versions
    from bot.config import Config as ConfigClass

    print("\nВерсии торговых систем")
    results: List[bool] = []

    # У каждой ТС, которую умеет запускать run.py, должна быть хотя бы одна версия.
    from run import DEFAULT_ENGINES
    missing = [s for s in DEFAULT_ENGINES if not versions.available(s)]
    results.append(_check("у каждой ТС есть версии", not missing,
                          ", ".join(f"{s}: {versions.available(s)}" for s in DEFAULT_ENGINES)))

    results.append(_check("версия по умолчанию - последняя",
                          versions.latest("breakout") == versions.available("breakout")[-1],
                          versions.latest("breakout")))

    # Сортировка: 0.10 новее 0.9, хотя строкой это не так.
    order = sorted(["0.1", "0.9", "0.10", "0.2"], key=versions._sort_key)
    results.append(_check("версии сортируются по номеру, а не по строке",
                          order == ["0.1", "0.2", "0.9", "0.10"], str(order)))

    # Применение overrides: числа реально доезжают до конфига.
    probe = ConfigClass()
    original = probe.breakout.min_entry_score
    versions.VERSIONS["breakout"]["9.9"] = versions.Version(
        notes="временная версия для самопроверки",
        overrides={"breakout": {"min_entry_score": 0.99},
                   "risk": {"margin_usd": 33.0}},
    )
    try:
        applied = versions.apply(probe, "breakout", "9.9")
        results.append(_check("overrides применились к секции ТС",
                              probe.breakout.min_entry_score == 0.99,
                              f"{original} -> {probe.breakout.min_entry_score}"))
        results.append(_check("overrides применились к общей секции риска",
                              probe.risk.margin_usd == 33.0, f"{probe.risk.margin_usd}"))
        results.append(_check("apply возвращает применённую версию", applied == "9.9", applied))

        # Опечатка в имени параметра должна валить бота на старте, а не тихо
        # проходить: иначе новая версия торговала бы ровно как предыдущая.
        versions.VERSIONS["breakout"]["9.8"] = versions.Version(
            overrides={"breakout": {"min_entry_scoer": 0.1}})
        try:
            versions.apply(ConfigClass(), "breakout", "9.8")
            caught = False
        except SystemExit:
            caught = True
        results.append(_check("опечатка в параметре версии ловится на старте", caught))

        # Несуществующая версия - тоже явная ошибка.
        try:
            versions.apply(ConfigClass(), "breakout", "0.0")
            caught_missing = False
        except SystemExit:
            caught_missing = True
        results.append(_check("запрос несуществующей версии ловится", caught_missing))
    finally:
        versions.VERSIONS["breakout"].pop("9.9", None)
        versions.VERSIONS["breakout"].pop("9.8", None)

    # Базовая версия 0.1 ничего не меняет - она и есть текущее поведение кода.
    base = ConfigClass()
    before = dataclasses.asdict(base)
    versions.apply(base, "breakout", "0.1")
    results.append(_check("v0.1 не меняет базовый конфиг",
                          dataclasses.asdict(base) == before))

    # Версия попадает в state.json - панель показывает, чьи это цифры.
    import json
    import tempfile
    from bot.journal import Journal

    original_dir = cfg.data_dir
    cfg.data_dir = tempfile.mkdtemp(prefix="selftest-versions-")
    try:
        journal = Journal(cfg.trades_csv, cfg.signals_csv, cfg.state_json)
        journal.save_state([], {"trades": 0}, [], strategy="breakout", version="0.1")
        payload = json.load(open(cfg.state_json, encoding="utf-8"))
        results.append(_check("версия попадает в state.json",
                              payload.get("version") == "0.1", repr(payload.get("version"))))
    finally:
        cfg.data_dir = original_dir

    return results


def _passed_level_candles(level: float = 100.0) -> List[Candle]:
    """Уровень ПРОБИТ давно, а цена вернулась к нему СВЕРХУ.

    Ровно тот случай, на который жалоба: пробивать уже нечего — цена выше
    уровня и торгуется там давно, — но 0.1 видит «цену в зоне уровня» и
    считает это сетапом пробоя. Для 0.2 такой уровень позади, а не впереди.
    """
    out: List[Candle] = []
    ts = int(time.time() * 1000) - 140 * 900_000
    price = level * 0.96

    # Фаза 1: уровень формируется — цена подходит снизу и отбивается.
    for _ in range(3):
        for _ in range(6):
            o = price
            c = min(price * 1.005, level * 0.996)
            out.append(Candle(ts, o, max(o, c) * 1.0008, min(o, c) * 0.999, c, 1000.0))
            price, ts = c, ts + 900_000
        o = price
        c = level * 0.994
        out.append(Candle(ts, o, level, min(o, c) * 0.999, c, 1400.0))   # касание
        price, ts = c, ts + 900_000
        for _ in range(6):
            o = price
            c = price * 0.995
            out.append(Candle(ts, o, max(o, c) * 1.0008, min(o, c) * 0.999, c, 900.0))
            price, ts = c, ts + 900_000

    # Фаза 2: уровень пробит, цена ушла выше и долго там торговалась.
    # Держим её примерно на 1% выше, чтобы её собственные экстремумы не
    # склеились с уровнем (tolerance 0.4%).
    for i in range(30):
        base = level * (1.010 if i % 2 == 0 else 1.009)
        out.append(Candle(ts, base, base * 1.0008, base * 0.9992, base, 1000.0))
        ts += 900_000

    # Фаза 3: цена вернулась к уровню СВЕРХУ и стоит в 0.3% над ним —
    # для 0.1 это «зона уровня».
    for i in range(4):
        base = level * (1.003 if i % 2 == 0 else 1.0028)
        out.append(Candle(ts, base, base * 1.0006, base * 0.9995, base, 1100.0))
        ts += 900_000
    return out


def _weak_trend_candles(n: int = 90, start: float = 100.0) -> List[Candle]:
    """Боковик с еле заметным подъёмом: 0.1 такой тренд принимала, 0.2 — нет."""
    out: List[Candle] = []
    ts = int(time.time() * 1000) - n * 900_000
    price = start
    for i in range(n):
        o = price
        # Пила с крошечным сносом вверх: наклон средней околонулевой.
        c = price * (1.0020 if i % 2 == 0 else 0.9982)
        out.append(Candle(ts, o, max(o, c) * 1.001, min(o, c) * 0.999, c, 1000.0))
        price, ts = c, ts + 900_000
    return out


def run_breakout_v2_selftest(cfg: Config) -> List[bool]:
    """Версия 0.2: уровень только на пробой, тренд строже, касаний три."""
    import tempfile

    from bot.activity import measure
    from bot.breakout import pick_level  # для сравнения с 0.1
    from bot.breakout_v2 import (BreakoutV2Engine, TrendScannerV2, build_setup_v2,
                                 in_breakout_zone, level_ahead)

    print("\nТС пробоя, версия 0.2")
    results: List[bool] = []
    b, v2 = cfg.breakout, cfg.breakout_v2
    level_price = 100.0

    # --- уровень позади цены: главная жалоба ---------------------------------
    passed = _passed_level_candles(level_price)
    price_now = passed[-1].close
    old_pick = pick_level(passed, price_now, cfg)
    new_pick = level_ahead(passed, price_now, cfg)
    results.append(_check("0.1 брала уже пройденный уровень (воспроизводим жалобу)",
                          old_pick is not None,
                          old_pick.describe() if old_pick else "нет"))
    results.append(_check("0.2 пройденный уровень НЕ берёт", new_pick is None,
                          new_pick.describe() if new_pick else "отсеян"))

    # --- уровень впереди: его 0.2 берёт --------------------------------------
    # Касаний больше, чем в тесте 0.1: горизонт 0.2 длиннее (400 свечей против
    # 120), и на короткой серии она справедливо отказывается искать уровни.
    ahead = _level_candles(touches=10)
    # Цена ещё под уровнем — классический сетап «на пробой».
    ahead_price = level_price * 0.997
    picked = level_ahead(ahead, ahead_price, cfg)
    results.append(_check("0.2 берёт уровень, к которому цена идёт снизу",
                          picked is not None,
                          picked.describe() if picked else "нет"))
    if picked is None:
        return results

    results.append(_check("касаний не меньше трёх", picked.touches >= v2.min_touches,
                          str(picked.touches)))

    # --- асимметричная зона --------------------------------------------------
    results.append(_check("вход разрешён под уровнем (ждём пробой)",
                          in_breakout_zone(picked.price * 0.997, picked, cfg)))
    results.append(_check("вход разрешён сразу над уровнем (свежий пробой)",
                          in_breakout_zone(picked.price * 1.001, picked, cfg)))
    results.append(_check("вход запрещён, когда цена уже ушла выше",
                          not in_breakout_zone(picked.price * 1.005, picked, cfg)))
    # У 0.1 та же точка входом считалась — показываем разницу версий.
    results.append(_check("у 0.1 та же точка входом считалась (разница версий видна)",
                          in_zone(picked.price * 1.003, picked, cfg)
                          and not in_breakout_zone(picked.price * 1.003, picked, cfg)))

    # --- тренд ---------------------------------------------------------------
    scanner = TrendScannerV2(client=None, cfg=cfg)
    ok_strong, why_strong = scanner._uptrend(_trend_candles(up=True))
    results.append(_check("сильный тренд проходит", ok_strong, why_strong))
    ok_weak, why_weak = scanner._uptrend(_weak_trend_candles())
    results.append(_check("вялый тренд 0.2 отсеивает", not ok_weak, why_weak))

    weak_ticker = Ticker(symbol="TESTUSDT", last_price=100.0, change_24h=0.025,
                         turnover_24h=50_000_000, volume_24h=1_000_000)
    info = {"status": "Trading", "quote": "USDT", "base": "TEST"}
    results.append(_check("рост за сутки ниже порога 0.2 отсеивается",
                          not scanner._tradable(weak_ticker, info),
                          f"{weak_ticker.change_24h * 100:.0f}% < {v2.min_change_24h * 100:.0f}%"))

    # --- активность ----------------------------------------------------------
    bids = [Level(picked.price * (1 - 0.0005 * i), 60.0) for i in range(1, 12)]
    asks = [Level(picked.price * (1 + 0.0005 * i), 60.0) for i in range(1, 12)]
    live = measure(_prints(400, picked.price, span_sec=60), bids, asks, picked.price, b)
    ticker = Ticker(symbol="TESTUSDT", last_price=picked.price * 0.998, change_24h=0.12,
                    turnover_24h=50_000_000, volume_24h=1_000_000)

    setup = build_setup_v2(ticker, picked, live, ratio=2.5, cfg=cfg)
    results.append(_check("сетап 0.2 собран на живой активности", setup is not None,
                          f"скор {setup.score:.2f}" if setup else "нет"))

    # Вялая по себе монета: у уровня всплеск есть, но сама монета мертва.
    sleepy = measure(_prints(60, picked.price, span_sec=60), bids, asks, picked.price, b)
    results.append(_check("вялая монета не проходит порог активности 0.2",
                          build_setup_v2(ticker, picked, sleepy, ratio=3.0, cfg=cfg) is None,
                          f"{sleepy.trades_per_min:.0f} принтов/мин"))
    results.append(_check("но у 0.1 та же монета входом считалась",
                          build_setup(ticker, picked, sleepy, ratio=3.0, cfg=cfg) is not None))

    results.append(_check("без превышения фона 0.2 не входит",
                          build_setup_v2(ticker, picked, live, ratio=1.5, cfg=cfg) is None))

    # --- движок --------------------------------------------------------------
    original_dir = cfg.data_dir
    cfg.data_dir = tempfile.mkdtemp(prefix="selftest-breakout-v2-")
    try:
        engine = BreakoutV2Engine(cfg, client=None)
        results.append(_check("движок 0.2 использует свой сканер",
                              type(engine.scanner).__name__ == "TrendScannerV2",
                              type(engine.scanner).__name__))
        results.append(_check("движок 0.2 использует свой отбор уровня",
                              engine.pick_level_for(passed, price_now) is None))
        results.append(_check("правило выхода унаследовано от 0.1",
                              hasattr(engine, "_exit_reason")))
    finally:
        cfg.data_dir = original_dir
    return results


def _density_view(cfg: Config, wall_side: str = "bid"):
    """Стакан с одной настоящей плотностью — общий вход для обеих версий."""
    import dataclasses

    from bot.orderbook import BookTracker

    d = cfg.density
    ocfg = dataclasses.replace(cfg.orderbook,
                               density_multiplier=d.density_multiplier,
                               min_persist_snapshots=d.min_persist_snapshots,
                               shrink_tolerance=d.max_shrink,
                               poll_interval_sec=d.poll_interval_sec)
    tracker = BookTracker(cfg=ocfg, symbol="TESTUSDT")
    view = None
    for _ in range(d.min_persist_snapshots + 2):
        view = tracker.update(*_book_snapshot(wall_side=wall_side))
    return tracker, view


def run_density_v2_selftest(cfg: Config) -> List[bool]:
    """Версия 0.2 плотностей: всё как в 0.1, но сторона сделки зеркальная."""
    import tempfile

    from bot.density import DensityEngine, find_wall_setup
    from bot.density_v2 import DensityV2Engine, find_wall_setup_inverted
    from bot.models import PendingOrder

    print("\nТС плотностей, версия 0.2 (зеркало 0.1)")
    results: List[bool] = []
    ticker = Ticker(symbol="TESTUSDT", last_price=0.0, change_24h=0.02,
                    turnover_24h=150_000_000, volume_24h=1_500_000)

    # --- bid-стена: 0.1 покупает, 0.2 продаёт --------------------------------
    tracker, view = _density_view(cfg, wall_side="bid")
    ticker.last_price = view.mid
    s1 = find_wall_setup(ticker, view, cfg)
    s2 = find_wall_setup_inverted(ticker, view, cfg)
    results.append(_check("0.1 у bid-стены открывает ЛОНГ",
                          s1 is not None and s1.side == "long",
                          s1.side if s1 else "нет сетапа"))
    results.append(_check("0.2 у той же стены открывает ШОРТ",
                          s2 is not None and s2.side == "short",
                          s2.side if s2 else "нет сетапа"))
    if s1 is None or s2 is None:
        return results

    # --- всё остальное должно совпадать --------------------------------------
    results.append(_check("цена срабатывания совпадает",
                          abs(s1.entry_price - s2.entry_price) < 1e-12,
                          f"{s1.entry_price:.8g} == {s2.entry_price:.8g}"))
    results.append(_check("стена та же самая",
                          s1.wall.price == s2.wall.price and s1.wall.side == s2.wall.side,
                          s2.wall.describe()))
    results.append(_check("дисбаланс тот же", abs(s1.dominance - s2.dominance) < 1e-12,
                          f"x{s2.dominance:.2f}"))

    # --- ask-стена: зеркало в другую сторону ---------------------------------
    tracker_a, view_a = _density_view(cfg, wall_side="ask")
    ticker_a = Ticker(symbol="TESTUSDT", last_price=view_a.mid, change_24h=0.02,
                      turnover_24h=150_000_000, volume_24h=1_500_000)
    a1 = find_wall_setup(ticker_a, view_a, cfg)
    a2 = find_wall_setup_inverted(ticker_a, view_a, cfg)
    results.append(_check("0.1 у ask-стены открывает ШОРТ",
                          a1 is not None and a1.side == "short",
                          a1.side if a1 else "нет сетапа"))
    results.append(_check("0.2 у той же стены открывает ЛОНГ",
                          a2 is not None and a2.side == "long",
                          a2.side if a2 else "нет сетапа"))
    if a1 is not None and a2 is not None:
        results.append(_check("цена срабатывания у ask-стены тоже совпадает",
                              abs(a1.entry_price - a2.entry_price) < 1e-12,
                              f"{a2.entry_price:.8g}"))

    original_dir = cfg.data_dir
    cfg.data_dir = tempfile.mkdtemp(prefix="selftest-density-v2-")
    try:
        e1 = DensityEngine(cfg, client=None)
        e2 = DensityV2Engine(cfg, client=None)

        # --- срабатывание: ждём одного и того же события ---------------------
        qty = cfg.risk.notional_usd / s1.entry_price
        o1 = PendingOrder(symbol="TESTUSDT", side=s1.side, price=s1.entry_price,
                          qty=qty, placed_at=time.time(), setup=s1)
        o2 = PendingOrder(symbol="TESTUSDT", side=s2.side, price=s2.entry_price,
                          qty=qty, placed_at=time.time(), setup=s2)

        import dataclasses
        far = dataclasses.replace(view, best_ask=s1.entry_price * 1.01,
                                  best_bid=s1.entry_price * 1.009)
        touched = dataclasses.replace(view, best_ask=s1.entry_price,
                                      best_bid=s1.entry_price * 0.999)
        results.append(_check("пока цена не дошла — не срабатывает ни у одной версии",
                              not e1._filled(o1, far) and not e2._filled(o2, far)))
        results.append(_check("цена дошла до стены — срабатывают ОБЕ версии",
                              e1._filled(o1, touched) and e2._filled(o2, touched)))

        # --- позиции: зеркальные уровни --------------------------------------
        p1 = e1.broker.open_from_limit(o1, entry_fee_rate=e1.entry_fee_rate())
        e1.broker.positions.clear()
        p2 = e2.broker.open_from_limit(o2, entry_fee_rate=e2.entry_fee_rate())

        results.append(_check("вход по одной и той же цене",
                              abs(p1.entry_price - p2.entry_price) < 1e-12,
                              f"{p2.entry_price:.8g}"))
        results.append(_check("стороны позиций противоположны",
                              p1.side == "long" and p2.side == "short",
                              f"{p1.side} vs {p2.side}"))
        results.append(_check("у 0.1 тейк выше входа, у 0.2 — ниже",
                              p1.take_price > p1.entry_price and p2.take_price < p2.entry_price))
        results.append(_check("у 0.1 стоп ниже входа, у 0.2 — выше",
                              p1.stop_price < p1.entry_price and p2.stop_price > p2.entry_price))

        # Тейк зеркален по направлению. Расстояние в ЦЕНЕ у версий чуть
        # разное, и так и должно быть: у 0.2 комиссия входа тейкерская, значит
        # цене надо пройти немного дальше ради тех же чистых +5$. Поэтому
        # сравниваем деньги (ниже), а здесь - знак и порядок величины.
        d1 = p1.take_price - p1.entry_price
        d2 = p2.take_price - p2.entry_price
        results.append(_check("тейк направлен в противоположные стороны",
                              d1 > 0 > d2, f"{d1:+.6g} / {d2:+.6g}"))
        results.append(_check("расстояние до тейка отличается лишь на комиссию",
                              abs(abs(d1) - abs(d2)) / abs(d1) < 0.05,
                              f"{abs(abs(d1) - abs(d2)) / abs(d1) * 100:.1f}%"))
        profit1 = e1.broker.net_pnl(p1, p1.take_price)
        results.append(_check("обе версии дают одинаковую чистую прибыль по тейку",
                              abs(profit1 - e2.broker.net_pnl(p2, p2.take_price)) < 0.01,
                              f"{profit1:+.2f}$"))
        results.append(_check("расстояние до стопа совпадает",
                              abs((p1.stop_price - p1.entry_price)
                                  + (p2.stop_price - p2.entry_price)) < 1e-9,
                              f"{p1.stop_price - p1.entry_price:+.8g} / "
                              f"{p2.stop_price - p2.entry_price:+.8g}"))

        # --- деньги ----------------------------------------------------------
        d = cfg.density
        profit2 = e2.broker.net_pnl(p2, p2.take_price)
        results.append(_check(f"у 0.2 тейк даёт те же +{d.take_profit_usd:.0f}$",
                              abs(profit2 - d.take_profit_usd) < 0.05, f"{profit2:+.2f}$"))
        results.append(_check("комиссия входа 0.2 тейкерская (ордер стоповый)",
                              abs(p2.entry_fee_rate - cfg.risk.taker_fee) < 1e-12
                              and abs(p1.entry_fee_rate - d.maker_fee) < 1e-12,
                              f"0.1 {p1.entry_fee_rate}, 0.2 {p2.entry_fee_rate}"))

        # --- выходы общие ----------------------------------------------------
        e2.books._trackers["TESTUSDT"] = tracker
        hold = e2._exit_reason(p2, view, p2.entry_price)
        results.append(_check("пока плотность цела — 0.2 тоже держит", hold is None,
                              hold or "держим"))
        eaten_view = None
        for _ in range(2):
            eaten_view = tracker.update(*_book_snapshot(wall_side="bid", wall_shrink=0.10))
        reason = e2._exit_reason(p2, eaten_view, p2.entry_price)
        results.append(_check("выход по съеданию плотности работает и в 0.2",
                              bool(reason) and "съедена" in (reason or ""), reason or "нет"))
    finally:
        cfg.data_dir = original_dir
    return results


# ---------------------------------------------------------------- бэктест
#
# Бэктест (bot/backtest.py) - это тот же разбор ТС, но по прошедшим свечам,
# и ошибиться в нём легче всего двумя способами: подглядеть в будущее и
# посчитать деньги не так, как их считает живой бот. Проверки ниже про это.

def _bt_candles(flat: int = 340, impulse: int = 12, stall: int = 3,
                after: int = 60, start: float = 1.0,
                after_dir: float = -1.0) -> List[Candle]:
    """5-минутки: ровный фон -> резкий рост -> затуп -> движение после входа.

    after_dir < 0 - цена после входа падает (шорт ТС импульса доходит до
    тейка), > 0 - растёт (шорт ловит стоп).
    """
    out: List[Candle] = []
    ts = 1_700_000_000_000
    price = start
    step = 300_000

    for _ in range(flat):
        o = price
        c = price * 1.0002
        out.append(Candle(ts, o, max(o, c) * 1.0005, min(o, c) * 0.9995, c, 1000.0, 104_000.0))
        price, ts = c, ts + step

    for _ in range(impulse):
        o = price
        c = price * 1.013                       # +1.3% за свечу, без откатов
        out.append(Candle(ts, o, c * 1.001, o * 0.9998, c, 4000.0, 420_000.0))
        price, ts = c, ts + step

    peak = price
    for _ in range(stall):
        o = price
        c = price * 0.9996                      # рост встал, сверху тени
        out.append(Candle(ts, o, peak * 1.0002, c * 0.9995, c, 700.0, 70_000.0))
        price, ts = c, ts + step

    for _ in range(after):
        o = price
        c = price * (1.0 + 0.004 * after_dir)
        out.append(Candle(ts, o, max(o, c) * 1.0008, min(o, c) * 0.9992, c, 1500.0, 150_000.0))
        price, ts = c, ts + step

    return out


def run_backtest_selftest(cfg: Config) -> List[bool]:
    """Реплей истории: отсутствие подглядывания, деньги и портфельный лимит."""
    from bot.backtest import (BacktestParams, DensityReplay, ImpulseReplay, PosState,
                              SimTrade, apply_portfolio_limits, btc_by_month,
                              month_series, proxy_activity, summarize, walk_position)
    from bot.history import resample
    from bot.paper import net_pnl_usd

    results: List[bool] = []
    print("\nБэктест: реплей истории")

    params = BacktestParams(months=4)

    # --- 1. Склейка 5m -> 15m не заглядывает вперёд ------------------------
    candles = _bt_candles(flat=20, impulse=0, stall=0, after=0)
    # Обрываем на середине 15-минутки: последняя крупная свеча обязана быть
    # собрана только из прошедших пяти-минуток, а не взята готовой.
    cut = 16                                     # 16 свечей = 5 полных 15m + 1
    partial = resample(candles[:cut], "5", "15", until_ts=candles[cut - 1].ts)
    expected_high = max(c.high for c in candles[15:cut])
    results.append(_check("15m склеивается из 5m без заглядывания вперёд",
                          abs(partial[-1].high - expected_high) < 1e-12
                          and partial[-1].close == candles[cut - 1].close,
                          f"{len(partial)} свечей, последняя из {cut - 15} пятиминуток"))
    results.append(_check("склейка не берёт свечи позже момента решения",
                          all(c.ts <= candles[cut - 1].ts for c in partial)))

    # --- 2. Внутри свечи стоп считается раньше тейка ------------------------
    state = PosState(side="short", qty=4.0, entry_price=100.0,
                     stop_price=101.0, take_price=99.0,
                     entry_fee=0.00055, exit_fee=0.00055,
                     opened_idx=0, opened_ms=0, bar_minutes=5.0)
    both = [Candle(0, 100.0, 100.0, 100.0, 100.0, 1.0),
            Candle(1, 100.0, 101.5, 98.5, 100.0, 1.0)]   # свеча задела оба уровня
    _, price, reason = walk_position(both, state)
    results.append(_check("свеча задела и стоп, и тейк - засчитан стоп",
                          reason == "стоп-лосс" and abs(price - 101.0) < 1e-9,
                          f"{reason} @ {price}"))

    state = PosState(side="short", qty=4.0, entry_price=100.0,
                     stop_price=101.0, take_price=99.0,
                     entry_fee=0.00055, exit_fee=0.00055,
                     opened_idx=0, opened_ms=0, bar_minutes=5.0)
    only_take = [Candle(0, 100.0, 100.0, 100.0, 100.0, 1.0),
                 Candle(1, 100.0, 100.2, 98.5, 98.8, 1.0)]
    _, price, reason = walk_position(only_take, state)
    results.append(_check("тейк засчитывается, когда стоп не задет",
                          reason == "тейк-профит" and abs(price - 99.0) < 1e-9,
                          f"{reason} @ {price}"))

    # --- 3. Деньги реплея = деньги живого бота ------------------------------
    broker = PaperBroker(cfg)
    setup = PlainSetup(symbol="TESTUSDT", side="long", price=100.0, timeframe="5",
                       score=0.7, ticker=Ticker("TESTUSDT", 100.0, 0.2, 5e7, 1e6))
    live = broker.open_market(setup, stop_usd=cfg.risk.stop_loss_usd,
                              take_usd=cfg.risk.take_profit_usd)
    replay = ImpulseReplay(cfg, params)
    sim = replay.open_position("long", 100.0, 0, 0, 5.0,
                               cfg.risk.stop_loss_usd, cfg.risk.take_profit_usd)
    results.append(_check("стоп и тейк реплея совпадают с живым ботом",
                          abs(sim.stop_price - live.stop_price) < 1e-9
                          and abs(sim.take_price - live.take_price) < 1e-9,
                          f"стоп {sim.stop_price:.6f}, тейк {sim.take_price:.6f}"))
    take_pnl = net_pnl_usd("long", sim.qty, 100.0, sim.take_price,
                           sim.entry_fee, sim.exit_fee)
    stop_pnl = net_pnl_usd("long", sim.qty, 100.0, sim.stop_price,
                           sim.entry_fee, sim.exit_fee)
    results.append(_check(f"по тейку ровно +{cfg.risk.take_profit_usd:.0f}$ чистыми",
                          abs(take_pnl - cfg.risk.take_profit_usd) < 0.01, f"{take_pnl:+.2f}$"))
    results.append(_check(f"по стопу ровно -{cfg.risk.stop_loss_usd:.0f}$ чистыми",
                          abs(stop_pnl + cfg.risk.stop_loss_usd) < 0.01, f"{stop_pnl:+.2f}$"))
    broker.close(live, live.take_price, "тест")

    # --- 4. Реплей ТС импульса находит сделку на синтетике -------------------
    data = {"5": _bt_candles(after_dir=-1.0)}
    trades = replay.run_symbol("TESTUSDT", data)
    results.append(_check("реплей импульса открывает шорт на затухшем росте",
                          len(trades) >= 1,
                          f"{len(trades)} сделок" if trades else "ни одной"))
    if trades:
        t = trades[0]
        results.append(_check("сделка шортовая и закрыта в плюс на падении",
                              t.side == "short" and t.net_pnl_usd > 0,
                              f"{t.side} {t.net_pnl_usd:+.2f}$ ({t.exit_reason})"))
        results.append(_check("выход позже входа",
                              t.closed_ms > t.opened_ms, f"{t.duration_min:.0f} мин"))

    up = replay.run_symbol("TESTUSDT", {"5": _bt_candles(after_dir=1.0)})
    results.append(_check("на продолжении роста тот же шорт ловит стоп",
                          bool(up) and up[0].net_pnl_usd < 0,
                          f"{up[0].net_pnl_usd:+.2f}$ ({up[0].exit_reason})" if up else "нет сделок"))

    # --- 5. Портфельный лимит одновременных позиций -------------------------
    def _t(symbol: str, start: int, end: int) -> SimTrade:
        return SimTrade(symbol=symbol, side="short", timeframe="5", opened_ms=start,
                        closed_ms=end, entry_price=1.0, exit_price=1.0, qty=1.0,
                        net_pnl_usd=1.0, fees_usd=0.0, exit_reason="", entry_reason="",
                        score=0.5)
    overlapping = [_t("A", 0, 100), _t("B", 10, 100), _t("C", 20, 100), _t("D", 30, 100)]
    kept = apply_portfolio_limits(list(overlapping), max_open=3)
    results.append(_check("лимит одновременных позиций отбрасывает четвёртую",
                          len(kept) == 3 and {t.symbol for t in kept} == {"A", "B", "C"},
                          ", ".join(t.symbol for t in kept)))
    sequential = [_t("A", 0, 10), _t("B", 20, 30), _t("C", 40, 50), _t("D", 60, 70)]
    results.append(_check("непересекающиеся сделки лимит не трогает",
                          len(apply_portfolio_limits(list(sequential), max_open=3)) == 4))

    # --- 6. Помесячная разбивка и движение BTC ------------------------------
    months = month_series(1_714_521_600_000, 1_725_148_800_000)   # май..сентябрь 2024
    results.append(_check("месяцы периода идут подряд без пропусков",
                          months == ["2024-05", "2024-06", "2024-07", "2024-08", "2024-09"],
                          ", ".join(months)))
    daily = [Candle(1_714_521_600_000, 60000.0, 62000.0, 59000.0, 61000.0, 1.0),
             Candle(1_714_608_000_000, 61000.0, 66000.0, 60500.0, 66000.0, 1.0)]
    btc = btc_by_month(daily)
    results.append(_check("изменение BTC за месяц считается от первого открытия",
                          abs(btc["2024-05"]["change_pct"] - 10.0) < 0.01,
                          f"{btc['2024-05']['change_pct']:+.2f}%"))

    stats = summarize([_t("A", 0, 1), _t("B", 0, 1)])
    results.append(_check("сводка без убытков не отдаёт бесконечность",
                          stats["profit_factor"] is None, str(stats["profit_factor"])))

    # --- 7. Приближение активности и честная пометка density ----------------
    b = cfg.breakout
    hot = Candle(0, 100.0, 100.6, 99.6, 100.5, 1.0, b.min_volume_per_min * 15 * 4)
    cold = Candle(0, 100.0, 100.05, 99.95, 99.96, 1.0, b.min_volume_per_min * 15 * 0.1)
    hot_score, hot_ratio, _ = proxy_activity(hot, 100.0, b.min_volume_per_min, b)
    cold_score, _, _ = proxy_activity(cold, 100.0, b.min_volume_per_min, b)
    results.append(_check("оценка активности отличает кипящую свечу от вялой",
                          hot_score > cold_score and hot_ratio > 1.0,
                          f"{hot_score:.2f} против {cold_score:.2f}, фон x{hot_ratio:.1f}"))

    density = DensityReplay(cfg, params)
    results.append(_check("ТС плотностей помечена как непроверяемая на истории",
                          density.fidelity == "none" and not density.symbols([])
                          and bool(density.caveats),
                          density.fidelity))

    # --- 8. Реплей не трогает стакан ----------------------------------------
    results.append(_check("реплей выключает стакан в своей копии конфига",
                          not replay.cfg.orderbook.enabled and cfg.orderbook.enabled,
                          "копия без стакана, оригинал не тронут"))
    return results
