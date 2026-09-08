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

    # --- 7. ТС пробоя уровня -------------------------------------------------
    results.extend(run_breakout_selftest(cfg))

    # --- 8. ТС биткоина ------------------------------------------------------
    results.extend(run_btc_selftest(cfg))

    # --- 9. Журнал: state.json обязан быть валидным JSON ---------------------
    results.extend(run_json_selftest(cfg))

    # --- 10. Реестр версий ---------------------------------------------------
    results.extend(run_versions_selftest(cfg))

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
