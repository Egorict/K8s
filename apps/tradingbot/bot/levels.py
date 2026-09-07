"""Горизонтальные уровни по свечам: где цену уже разворачивало.

Общий кусок для двух ТС: пробою уровень нужен как сопротивление (его пробивают
вверх), биткоину - как поддержка (от неё откупают просадку). Логика поиска одна
и та же, отличается только сторона.

Уровень - это не одна экстремальная свеча, а место, куда цена приходила
несколько раз и уходила обратно. Поэтому:

  1) находим экстремумы (pivot): свеча, чей хай выше соседей в окне;
  2) склеиваем близкие экстремумы в кластер - рынок никогда не разворачивается
     дважды по одной и той же цене до пятого знака;
  3) касанием считаем каждый экстремум кластера. Чем их больше, тем уровень
     заметнее для остальных участников - а работает он ровно потому, что его
     видят все.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from .models import Candle


@dataclass
class HorizontalLevel:
    price: float
    touches: int
    side: str            # 'resistance' | 'support'
    last_touch_idx: int  # индекс последнего касания в переданном списке свечей
    first_touch_idx: int

    def distance_pct(self, price: float) -> float:
        """Расстояние от цены до уровня в долях: >0 - уровень выше цены."""
        return (self.price - price) / price if price > 0 else 0.0

    def describe(self) -> str:
        return f"{self.price:.8g} ({self.touches} касаний)"


def _pivots(candles: Sequence[Candle], window: int, high_side: bool) -> List[int]:
    """Индексы локальных экстремумов.

    Свеча считается экстремумом, если в окне ±window нет экстремальнее её.
    Края списка пропускаем: там окно неполное, и "экстремум" был бы случайным.
    """
    out: List[int] = []
    for i in range(window, len(candles) - window):
        chunk = candles[i - window:i + window + 1]
        if high_side:
            if candles[i].high >= max(c.high for c in chunk):
                out.append(i)
        else:
            if candles[i].low <= min(c.low for c in chunk):
                out.append(i)
    return out


def find_levels(candles: Sequence[Candle], side: str, tolerance: float = 0.004,
                min_touches: int = 2, window: int = 3) -> List[HorizontalLevel]:
    """Уровни по свечам, от самых "касаемых" к остальным.

    tolerance - на сколько (в долях цены) экстремумы могут расходиться, оставаясь
    одним уровнем. side: 'resistance' (по хаям) или 'support' (по лоям).
    """
    if len(candles) < window * 2 + 2:
        return []

    high_side = side == "resistance"
    idxs = _pivots(candles, window, high_side)
    if not idxs:
        return []

    prices = [(i, candles[i].high if high_side else candles[i].low) for i in idxs]
    # Сортируем по цене - тогда кластеры это просто соседние элементы.
    prices.sort(key=lambda p: p[1])

    levels: List[HorizontalLevel] = []
    cluster: List[tuple] = [prices[0]]
    for item in prices[1:]:
        base = cluster[0][1]
        if base > 0 and abs(item[1] - base) / base <= tolerance:
            cluster.append(item)
            continue
        levels.append(_level_from(cluster, side))
        cluster = [item]
    levels.append(_level_from(cluster, side))

    levels = [l for l in levels if l.touches >= min_touches]
    # Сильный уровень - тот, к которому приходили чаще; при равенстве берём
    # более свежий: старые уровни рынок уже отработал.
    levels.sort(key=lambda l: (l.touches, l.last_touch_idx), reverse=True)
    return levels


def _level_from(cluster: Sequence[tuple], side: str) -> HorizontalLevel:
    idxs = [i for i, _ in cluster]
    prices = [p for _, p in cluster]
    return HorizontalLevel(
        price=sum(prices) / len(prices),
        touches=len(cluster),
        side="resistance" if side == "resistance" else "support",
        last_touch_idx=max(idxs),
        first_touch_idx=min(idxs),
    )


def nearest_level(levels: Sequence[HorizontalLevel], price: float, above: bool,
                  max_distance: float) -> HorizontalLevel | None:
    """Ближайший уровень выше (above=True) или ниже цены в пределах max_distance."""
    best: HorizontalLevel | None = None
    for level in levels:
        distance = level.distance_pct(price)
        if above and not (0 < distance <= max_distance):
            continue
        if not above and not (-max_distance <= distance < 0):
            continue
        if best is None or abs(distance) < abs(best.distance_pct(price)):
            best = level
    return best
