"""Замер активности у уровня: лента принтов + стакан.

Зачем это нужно. Пробой уровня сам по себе ничего не значит: цена может
переползти его на трёх сделках и сползти обратно. Работает только ИМПУЛЬСНЫЙ
пробой - тот, где у уровня кипит торговля: летит лента принтов, ставятся и
снимаются заявки, цена дёргается туда-сюда. Именно эта активность и толкает
цену дальше, и именно она первой заканчивается, когда импульс выдохся.

Поэтому ТС пробоя принимает оба решения по активности, а не по цене:
  * вход - когда у уровня есть активность (цена при этом может быть как под
    уровнем, так и уже над ним);
  * выход - как только активность упала, даже если цена ещё стоит на месте.

Что меряем (всё - из публичных данных, ключи API не нужны):

  trades_per_min  темп ленты: сколько сделок в минуту реально исполняется;
  volume_per_min  оборот в деньгах - отсекает "активность" из мелких принтов;
  near_share      какая доля этого оборота прошла В ЗОНЕ уровня, а не в стороне;
  book_notional   сколько денег стоит заявками в зоне уровня;
  swings          сколько раз цена пересекла уровень туда-обратно - то самое
                  "цена колеблется", а не стоит;
  buy_ratio       кто агрессор: >0.5 - берут по рынку вверх.

Сводный `score` от 0 до 1 собирается из них с весами и сравнивается с фоном
самой монеты: у тухлой монеты 30 принтов в минуту - это много, у ликвидной -
мёртвая тишина, поэтому абсолютного порога мало.
"""
from __future__ import annotations

import statistics
import time
from collections import deque
from typing import Deque, List, Optional, Sequence

from .config import BreakoutConfig
from .indicators import scale
from .models import ActivitySnapshot, Level, TradePrint


def measure(prints: Sequence[TradePrint], bids: Sequence[Level], asks: Sequence[Level],
            level_price: float, cfg: BreakoutConfig) -> ActivitySnapshot:
    """Один замер активности вокруг уровня."""
    if not prints or level_price <= 0:
        return ActivitySnapshot(ts=time.time())

    # Время берём из самой ленты, а не системное: часы контейнера могут
    # разъезжаться с биржей, а нам важен темп, а не абсолютное время.
    now_ms = max(p.ts for p in prints)
    cutoff = now_ms - cfg.activity_window_sec * 1000
    recent = [p for p in prints if p.ts >= cutoff]
    if len(recent) < 2:
        return ActivitySnapshot(ts=time.time())

    span_ms = now_ms - min(p.ts for p in recent)
    if len(recent) == len(prints) and span_ms < cfg.activity_window_sec * 1000:
        # Лента отдала ровно limit сделок и все они уложились в окно - значит
        # окно не покрыто целиком, и делить надо на фактический промежуток,
        # иначе темп занижается тем сильнее, чем активнее рынок.
        elapsed_min = max(span_ms, 1) / 60_000
    else:
        elapsed_min = cfg.activity_window_sec / 60

    total_notional = sum(p.notional for p in recent)
    trades_per_min = len(recent) / elapsed_min
    volume_per_min = total_notional / elapsed_min

    lo = level_price * (1 - cfg.level_zone_pct)
    hi = level_price * (1 + cfg.level_zone_pct)
    near_notional = sum(p.notional for p in recent if lo <= p.price <= hi)
    near_share = near_notional / total_notional if total_notional > 0 else 0.0

    buy_notional = sum(p.notional for p in recent if p.side == "Buy")
    buy_ratio = buy_notional / total_notional if total_notional > 0 else 0.5

    # Колебания: считаем смены стороны относительно уровня по ходу времени.
    # Лента приходит от свежих к старым - разворачиваем в хронологию.
    swings = 0
    previous: Optional[bool] = None
    for p in sorted(recent, key=lambda x: x.ts):
        above = p.price >= level_price
        if previous is not None and above != previous:
            swings += 1
        previous = above

    book_notional = (
        sum(l.notional for l in bids if lo <= l.price <= hi)
        + sum(l.notional for l in asks if lo <= l.price <= hi)
    )

    snapshot = ActivitySnapshot(
        ts=time.time(),
        trades_per_min=trades_per_min,
        volume_per_min=volume_per_min,
        near_share=near_share,
        book_notional=book_notional,
        swings=swings,
        buy_ratio=buy_ratio,
    )
    snapshot.score = _score(snapshot, cfg)
    return snapshot


def _score(s: ActivitySnapshot, cfg: BreakoutConfig) -> float:
    """Сводная активность 0..1.

    Веса подобраны так, чтобы «много мелких принтов в стороне от уровня» не
    выглядело активностью: темп важен, но доля оборота именно у уровня и живой
    стакан весят вместе больше.
    """
    return round(
        0.30 * scale(s.trades_per_min, cfg.min_trades_per_min, cfg.min_trades_per_min * 5)
        + 0.20 * scale(s.volume_per_min, cfg.min_volume_per_min, cfg.min_volume_per_min * 6)
        + 0.25 * scale(s.near_share, cfg.min_near_share, 0.85)
        + 0.15 * scale(s.book_notional, cfg.min_book_notional, cfg.min_book_notional * 5)
        + 0.10 * scale(float(s.swings), 1.0, 8.0),
        4,
    )


class ActivityTracker:
    """История замеров по одной монете: фон, пик и падение активности."""

    def __init__(self, cfg: BreakoutConfig):
        self.cfg = cfg
        self.history: Deque[ActivitySnapshot] = deque(maxlen=cfg.activity_history)
        # Пик за время текущей сделки. Отсчёт падения активности идёт от него,
        # а не от входа: импульс часто разгоняется уже после открытия позиции.
        self.peak_score: float = 0.0

    def add(self, snapshot: ActivitySnapshot) -> ActivitySnapshot:
        self.history.append(snapshot)
        return snapshot

    @property
    def ready(self) -> bool:
        """Набрана ли история, чтобы было с чем сравнивать текущий замер."""
        return len(self.history) >= self.cfg.activity_baseline_min

    def baseline(self) -> float:
        """Фоновая активность монеты - медиана по истории.

        Медиана, а не среднее: одна вспышка не должна поднимать планку так,
        чтобы следующий настоящий импульс на её фоне выглядел вялым.
        """
        if not self.history:
            return 0.0
        return statistics.median(s.score for s in self.history)

    def ratio(self, snapshot: ActivitySnapshot) -> float:
        """Во сколько раз текущая активность выше фоновой."""
        base = self.baseline()
        if base <= 0.01:
            # Фон почти нулевой: любая заметная активность - это всплеск,
            # но бесконечность возвращать нельзя, ограничиваем.
            return 5.0 if snapshot.score > 0.05 else 0.0
        return snapshot.score / base

    def note_peak(self, snapshot: ActivitySnapshot) -> None:
        self.peak_score = max(self.peak_score, snapshot.score)

    def reset_peak(self) -> None:
        self.peak_score = 0.0

    def faded(self, snapshot: ActivitySnapshot) -> Optional[str]:
        """Упала ли активность настолько, что импульс закончился.

        Возвращает причину выхода или None. Это главное правило выхода ТС:
        цена здесь не участвует вовсе.
        """
        if self.peak_score <= 0:
            return None
        if snapshot.score <= self.peak_score * self.cfg.activity_drop_ratio:
            return (f"активность упала: {snapshot.score:.2f} от пика "
                    f"{self.peak_score:.2f} ({snapshot.describe()})")
        if snapshot.score < self.cfg.min_hold_score:
            return f"активность иссякла: {snapshot.score:.2f} ({snapshot.describe()})"
        return None
