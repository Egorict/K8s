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
    # Насколько сильно цена отбивалась от уровня после касаний (доля цены).
    # 0 у уровней, найденных без проверки отбоя.
    rejection: float = 0.0

    def distance_pct(self, price: float) -> float:
        """Расстояние от цены до уровня в долях: >0 - уровень выше цены."""
        return (self.price - price) / price if price > 0 else 0.0

    def describe(self) -> str:
        base = f"{self.price:.8g} ({self.touches} касаний"
        if self.rejection > 0:
            base += f", отбой {self.rejection * 100:.1f}%"
        return base + ")"


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
                min_touches: int = 2, window: int = 3,
                min_spacing: int = 0, min_rejection: float = 0.0) -> List[HorizontalLevel]:
    """Уровни по свечам, от самых "касаемых" к остальным.

    tolerance - на сколько (в долях цены) экстремумы могут расходиться, оставаясь
    одним уровнем. side: 'resistance' (по хаям) или 'support' (по лоям).

    Два необязательных условия делают уровень "явным":
      min_spacing   - касания разнесены минимум на столько свечей. Три подряд
                      свечи у одной цены - это одно событие, а не три
                      подтверждения;
      min_rejection - после касания цена уходила от уровня хотя бы на эту долю.
                      Без этого в уровни попадает любой локальный экстремум,
                      от которого рынок никак не реагировал.
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
        levels.append(_level_from(cluster, side, candles, min_spacing, min_rejection))
        cluster = [item]
    levels.append(_level_from(cluster, side, candles, min_spacing, min_rejection))

    levels = [l for l in levels if l.touches >= min_touches]
    if min_rejection > 0:
        levels = [l for l in levels if l.rejection >= min_rejection]
    # Сильный уровень - тот, к которому приходили чаще; при равенстве берём
    # более свежий: старые уровни рынок уже отработал.
    levels.sort(key=lambda l: (l.touches, l.last_touch_idx), reverse=True)
    return levels


def _rejection_after(candles: Sequence[Candle], idx: int, price: float,
                     high_side: bool, lookahead: int) -> float:
    """Насколько цена ушла от уровня после касания, в долях."""
    if price <= 0:
        return 0.0
    chunk = candles[idx + 1: idx + 1 + lookahead]
    if not chunk:
        return 0.0
    if high_side:
        # Сопротивление: после касания цена должна была уйти ВНИЗ.
        return max(0.0, (price - min(c.low for c in chunk)) / price)
    return max(0.0, (max(c.high for c in chunk) - price) / price)


def _level_from(cluster: Sequence[tuple], side: str, candles: Sequence[Candle],
                min_spacing: int, min_rejection: float) -> HorizontalLevel:
    high_side = side == "resistance"
    idxs = sorted(i for i, _ in cluster)
    prices = [p for _, p in cluster]
    level_price = sum(prices) / len(prices)

    # Разнесённость: жадно оставляем касания, между которыми есть промежуток.
    if min_spacing > 0:
        kept: List[int] = []
        for i in idxs:
            if not kept or i - kept[-1] >= min_spacing:
                kept.append(i)
        idxs = kept

    rejection = 0.0
    if min_rejection > 0 and idxs:
        lookahead = max(min_spacing, 4) * 2
        depths = [_rejection_after(candles, i, level_price, high_side, lookahead) for i in idxs]
        # Уровень характеризует не рекордный отбой, а типичный: одиночный
        # провал после случайного хая не должен выдавать себя за сопротивление.
        depths.sort()
        rejection = depths[len(depths) // 2]

    return HorizontalLevel(
        price=level_price,
        touches=len(idxs),
        side="resistance" if high_side else "support",
        last_touch_idx=max(idxs) if idxs else 0,
        first_touch_idx=min(idxs) if idxs else 0,
        rejection=rejection,
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
