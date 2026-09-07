"""Мелкие расчётные утилиты без внешних зависимостей."""
from __future__ import annotations

import statistics
from typing import Iterable, List, Sequence

from .models import Candle


def safe_median(values: Iterable[float], default: float = 0.0) -> float:
    data = [v for v in values if v is not None]
    return statistics.median(data) if data else default


def safe_mean(values: Iterable[float], default: float = 0.0) -> float:
    data = list(values)
    return sum(data) / len(data) if data else default


def pct(a: float, b: float) -> float:
    """Изменение от a к b в долях."""
    if a <= 0:
        return 0.0
    return (b - a) / a


def true_ranges(candles: Sequence[Candle]) -> List[float]:
    out: List[float] = []
    prev_close = candles[0].close if candles else 0.0
    for c in candles:
        out.append(max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close)))
        prev_close = c.close
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = true_ranges(candles[-(period + 1):])
    return safe_mean(trs[1:], default=0.0)


def max_drawdown(prices: Sequence[float]) -> float:
    """Максимальная просадка от достигнутого максимума, в долях."""
    peak = float("-inf")
    worst = 0.0
    for p in prices:
        peak = max(peak, p)
        if peak > 0:
            worst = max(worst, (peak - p) / peak)
    return worst


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def scale(value: float, low: float, high: float) -> float:
    """Линейно нормирует value из [low, high] в [0, 1]."""
    if high <= low:
        return 0.0
    return clamp((value - low) / (high - low))
