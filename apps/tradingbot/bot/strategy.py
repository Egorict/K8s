"""Шаги 2-3 ТС: импульс на 1m/5m/15m и точка входа - "затуп".

Логика:
  1. На каждом таймфрейме ищем окно из 4-12 свечей с импульсным ростом,
     в котором почти не было просадок и был повышенный объём.
  2. Если импульс подтверждён минимум на двух ТФ - ждём затухания:
     свечи сужаются, объём падает, сверху появляются тени, хай не обновляется,
     скорость роста падает относительно пика. Это и есть "затуп" - вход в шорт.
  3. Стакан корректирует финальный скор и может запретить вход.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

from .config import Config
from .indicators import pct, safe_mean, scale
from .models import Candle, ImpulseInfo, OrderbookView, Setup, StallInfo, Ticker

log = logging.getLogger("strategy")


# --------------------------------------------------------------------- импульс

def find_impulse(candles: Sequence[Candle], timeframe: str, cfg: Config) -> ImpulseInfo:
    """Ищет лучшее окно импульсного роста среди последних 4-12 закрытых свечей."""
    icfg = cfg.impulse
    info = ImpulseInfo(timeframe=timeframe)

    if len(candles) < icfg.max_candles + icfg.volume_baseline_candles // 2:
        info.reason = "мало истории"
        return info

    closed = list(candles[:-1])  # последняя свеча ещё формируется
    if len(closed) < icfg.max_candles + 5:
        info.reason = "мало закрытых свечей"
        return info

    baseline = closed[-(icfg.volume_baseline_candles + icfg.max_candles): -icfg.max_candles]
    baseline_vol = safe_mean([c.volume for c in baseline], default=0.0)
    min_gain = icfg.min_gain.get(timeframe, 0.02)

    best: Optional[ImpulseInfo] = None

    for n in range(icfg.min_candles, icfg.max_candles + 1):
        window = closed[-n:]
        start = window[0].open
        if start <= 0:
            continue
        high = max(c.high for c in window)
        end = window[-1].close
        gain = pct(start, high)
        if gain < min_gain:
            continue

        # Просадка внутри импульса: насколько глубоко откатывались от локального пика.
        peak = start
        worst_retrace = 0.0
        for c in window:
            peak = max(peak, c.high)
            worst_retrace = max(worst_retrace, (peak - c.low))
        move = max(high - start, 1e-12)
        retrace_ratio = worst_retrace / move
        if retrace_ratio > icfg.max_retrace_ratio:
            continue

        red_ratio = sum(1 for c in window if not c.is_green) / n
        if red_ratio > icfg.max_red_ratio:
            continue

        if icfg.require_higher_lows:
            floor = start * 0.998
            if any(c.low < floor for c in window):
                continue

        window_vol = safe_mean([c.volume for c in window], default=0.0)
        vol_ratio = window_vol / baseline_vol if baseline_vol > 0 else 0.0
        if vol_ratio < icfg.min_volume_ratio:
            continue

        # Скорость роста на пике: максимальный прирост за одну свечу, %/свеча.
        peak_speed = max(pct(c.open, c.close) for c in window)
        avg_range = safe_mean([c.range for c in window], default=0.0)

        retrace_s = 1.0 - min(1.0, retrace_ratio / max(icfg.max_retrace_ratio, 1e-9))
        clean_s = 1.0 - min(1.0, red_ratio / max(icfg.max_red_ratio, 1e-9))
        score = (
            0.40 * scale(gain, min_gain, min_gain * 4)
            + 0.25 * retrace_s
            + 0.20 * scale(vol_ratio, icfg.min_volume_ratio, icfg.min_volume_ratio * 3)
            + 0.15 * clean_s
        )

        candidate = ImpulseInfo(
            timeframe=timeframe,
            found=True,
            candles=n,
            gain=gain,
            start_price=start,
            high_price=high,
            max_retrace_ratio=retrace_ratio,
            red_ratio=red_ratio,
            volume_ratio=vol_ratio,
            avg_range=avg_range,
            peak_speed=peak_speed,
            score=max(0.0, min(1.0, score)),
            reason=(f"{n} свечей, +{gain * 100:.2f}%, откат {retrace_ratio * 100:.0f}% "
                    f"от движения, объём x{vol_ratio:.1f}"),
        )
        if best is None or candidate.score > best.score:
            best = candidate

    if best is None:
        info.reason = "импульс не найден"
        return info
    return best


# ---------------------------------------------------------------------- затуп

def find_stall(candles: Sequence[Candle], impulse: ImpulseInfo, cfg: Config) -> StallInfo:
    """Проверяет, что импульс тухнет прямо сейчас - это и есть точка входа."""
    ecfg = cfg.entry
    stall = StallInfo(timeframe=impulse.timeframe)

    if not impulse.found or len(candles) < 4:
        stall.reason = "нет импульса"
        return stall

    forming = candles[-1]
    closed = list(candles[:-1])
    recent = closed[-ecfg.stall_lookback:] + [forming]
    last = forming
    price = last.close

    impulse_window = closed[-impulse.candles:]
    peak_volume = max(c.volume for c in impulse_window)

    drop_from_high = (impulse.high_price - price) / impulse.high_price if impulse.high_price else 0.0
    stall.drop_from_high = drop_from_high

    # 1. Свечи сузились - покупатель выдохся.
    range_shrink = last.range < impulse.avg_range * ecfg.stall_range_ratio

    # 2. Объём затухает относительно пика импульса.
    volume_fade = last.volume < peak_volume * ecfg.stall_volume_ratio

    # 3. Сверху появились тени - продавец начал разгружать.
    upper_wick = max(c.upper_wick_ratio for c in recent) >= ecfg.min_upper_wick_ratio

    # 4. Последние свечи не обновляют хай импульса.
    no_new_high = max(c.high for c in recent[-ecfg.stall_lookback:]) < impulse.high_price * 1.0005

    # 5. Скорость роста упала относительно пика импульса.
    recent_speed = safe_mean([pct(c.open, c.close) for c in recent[-2:]], default=0.0)
    momentum_decay = recent_speed < impulse.peak_speed * ecfg.momentum_decay_ratio

    conditions = {
        "сужение свечей": range_shrink,
        "затухание объёма": volume_fade,
        "верхние тени": upper_wick,
        "хай не обновляется": no_new_high,
        "импульс замедлился": momentum_decay,
    }
    passed = sum(1 for v in conditions.values() if v)

    stall.conditions = conditions
    stall.passed = passed

    if drop_from_high > ecfg.max_drop_from_high:
        stall.reason = f"поезд ушёл: уже -{drop_from_high * 100:.2f}% от вершины"
        return stall
    if passed < ecfg.min_stall_conditions:
        stall.reason = f"затуп не подтверждён ({passed}/{len(conditions)})"
        return stall

    stall.stalled = True
    stall.reason = ", ".join(name for name, ok in conditions.items() if ok)
    return stall


# ---------------------------------------------------------------------- сетап

def _book_adjust(book: OrderbookView, cfg: Config, notes: List[str]) -> Optional[float]:
    """Возвращает поправку к скору или None, если стакан запрещает вход."""
    ocfg = cfg.orderbook
    if not ocfg.enabled:
        return 0.0

    adjust = 0.0

    if book.imbalance > ocfg.max_bid_ask_imbalance:
        notes.append(f"стакан: покупатель давит (imbalance {book.imbalance:.2f}) - вход отменён")
        return None

    bid_wall = book.genuine_bid_wall
    if bid_wall is not None:
        if abs(bid_wall.distance_pct) <= ocfg.bid_wall_block_pct:
            notes.append(f"стакан: настоящая поддержка снизу {bid_wall.describe()} - вход отменён")
            return None
        adjust -= ocfg.bid_wall_penalty
        notes.append(f"стакан: поддержка ниже, минус к скору ({bid_wall.describe()})")

    ask_wall = book.genuine_ask_wall
    if ask_wall is not None and ask_wall.distance_pct > 0:
        adjust += ocfg.ask_wall_bonus
        notes.append(f"стакан: сопротивление сверху, плюс к скору ({ask_wall.describe()})")

    spoofy = [w for w in book.walls if not w.genuine]
    if spoofy:
        notes.append(f"стакан: {len(spoofy)} плотностей не прошли анти-спуфинг - игнорим")

    return adjust


def build_setup(
    symbol: str,
    ticker: Ticker,
    candles_by_tf: Dict[str, List[Candle]],
    book: OrderbookView,
    cfg: Config,
) -> Optional[Setup]:
    """Собирает готовый сетап или None, если условия ТС не выполнены."""
    impulses: Dict[str, ImpulseInfo] = {}
    for tf, candles in candles_by_tf.items():
        impulses[tf] = find_impulse(candles, tf, cfg)

    confirmed = [i for i in impulses.values() if i.found]
    if len(confirmed) < cfg.impulse.min_confirmed_timeframes:
        return None

    # Ведущий ТФ - там, где импульс сильнее всего. Он же задаёт горизонт сделки.
    primary = max(confirmed, key=lambda i: i.score)
    stall = find_stall(candles_by_tf[primary.timeframe], primary, cfg)
    if not stall.stalled:
        return None

    price = candles_by_tf[primary.timeframe][-1].close

    notes: List[str] = [
        f"импульс {primary.timeframe}m: {primary.reason}",
        f"подтверждений ТФ: {len(confirmed)}/{len(impulses)}",
        f"затуп: {stall.reason}",
    ]

    confirm_score = safe_mean([i.score for i in confirmed], default=0.0)
    stall_score = stall.passed / max(len(stall.conditions), 1)

    score = 0.45 * primary.score + 0.25 * confirm_score + 0.30 * stall_score

    adjust = _book_adjust(book, cfg, notes)
    if adjust is None:
        log.info("[%s] сетап отклонён стаканом: %s", symbol, notes[-1])
        return None
    score = max(0.0, min(1.0, score + adjust))

    if score < cfg.entry.min_setup_score:
        return None

    return Setup(
        symbol=symbol,
        price=price,
        primary_tf=primary.timeframe,
        score=score,
        impulses=impulses,
        stall=stall,
        book=book,
        ticker=ticker,
        notes=notes,
    )
