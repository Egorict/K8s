"""Шаг 5 ТС: стакан заявок и отсев спуферов.

Идея простая. Спуфер ставит огромную заявку, чтобы её увидели, но снимает её,
как только цена подходит - она живёт секунды и не переживает подход цены.
Настоящая плотность стоит на месте много опросов подряд, не усыхает и не убегает.
Поэтому мы не смотрим на один снапшот, а ведём историю по каждому ценовому уровню.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .config import OrderbookConfig
from .indicators import safe_median
from .models import Level, OrderbookView, Wall

log = logging.getLogger("orderbook")


@dataclass
class _LevelState:
    side: str
    price: float
    first_ts: float
    first_distance: float
    seen: int = 0
    missed: int = 0
    max_size: float = 0.0
    last_size: float = 0.0
    min_distance: float = 1.0
    was_wall: bool = False


@dataclass
class BookTracker:
    """Живёт по одному на монету, накапливает историю снапшотов."""

    cfg: OrderbookConfig
    symbol: str = ""
    _levels: Dict[Tuple[str, float], _LevelState] = field(default_factory=dict)
    _spoof_events: List[float] = field(default_factory=list)
    updates: int = 0

    # ------------------------------------------------------------------ утилиты

    def _zone(self, levels: Sequence[Level], mid: float) -> List[Level]:
        lo = mid * (1 - self.cfg.zone_pct)
        hi = mid * (1 + self.cfg.zone_pct)
        return [l for l in levels if lo <= l.price <= hi and l.size > 0]

    def _record_spoof(self) -> None:
        self._spoof_events.append(time.time())
        cutoff = time.time() - 300
        self._spoof_events = [t for t in self._spoof_events if t >= cutoff]

    # ------------------------------------------------------------------ основное

    def update(self, bids: Sequence[Level], asks: Sequence[Level]) -> OrderbookView:
        if not bids or not asks:
            return OrderbookView()

        mid = (bids[0].price + asks[0].price) / 2
        self.updates += 1

        zone_bids = self._zone(bids, mid)
        zone_asks = self._zone(asks, mid)
        if not zone_bids or not zone_asks:
            return OrderbookView(mid=mid, best_bid=bids[0].price, best_ask=asks[0].price)

        bid_notional = sum(l.notional for l in zone_bids)
        ask_notional = sum(l.notional for l in zone_asks)
        imbalance = bid_notional / ask_notional if ask_notional > 0 else 1.0

        # Порог плотности считаем отдельно для каждой стороны: медиана уровня в зоне.
        thresholds = {
            "bid": safe_median([l.notional for l in zone_bids]) * self.cfg.density_multiplier,
            "ask": safe_median([l.notional for l in zone_asks]) * self.cfg.density_multiplier,
        }

        present: Dict[Tuple[str, float], Level] = {}
        for side, levels in (("bid", zone_bids), ("ask", zone_asks)):
            for l in levels:
                present[(side, l.price)] = l

        now = time.time()

        # 1) Обновляем уже известные уровни и заводим новые.
        for (side, price), level in present.items():
            key = (side, price)
            distance = abs(price - mid) / mid
            state = self._levels.get(key)
            if state is None:
                state = _LevelState(
                    side=side, price=price, first_ts=now,
                    first_distance=max(distance, 1e-9), min_distance=distance,
                )
                self._levels[key] = state
            state.seen += 1
            state.missed = 0
            state.last_size = level.notional
            state.max_size = max(state.max_size, level.notional)
            state.min_distance = min(state.min_distance, distance)
            if level.notional >= thresholds[side]:
                state.was_wall = True
            elif state.was_wall and level.notional < state.max_size * 0.35:
                # Плотность схлопнулась, но уровень остался. Если цена до него
                # даже не дошла - заявку сняли руками, это спуфер. Если цена
                # стоит вплотную - плотность просто съели, это нормально.
                if distance > self.cfg.max_level_drift_pct:
                    self._record_spoof()
                state.was_wall = False

        # 2) Уровни, которых в этом снапшоте нет. Крупная заявка, снятая рано, -
        #    типичный спуфинг, особенно если цена к ней успела подойти.
        for key in list(self._levels.keys()):
            if key in present:
                continue
            state = self._levels[key]
            state.missed += 1
            if state.missed == 1 and state.was_wall:
                approached = state.min_distance < state.first_distance * 0.75
                short_lived = state.seen < self.cfg.min_persist_snapshots
                if short_lived or approached:
                    self._record_spoof()
            if state.missed >= 2:
                del self._levels[key]

        # 3) Собираем плотности текущего снапшота.
        walls: List[Wall] = []
        for (side, price), level in present.items():
            if level.notional < thresholds[side]:
                continue
            state = self._levels[(side, price)]
            distance_pct = (price - mid) / mid
            genuine = (
                state.seen >= self.cfg.min_persist_snapshots
                and state.last_size >= state.max_size * (1.0 - self.cfg.shrink_tolerance)
            )
            walls.append(
                Wall(
                    side=side,
                    price=price,
                    size=level.size,
                    notional=level.notional,
                    distance_pct=distance_pct,
                    persisted=state.seen,
                    genuine=genuine,
                )
            )

        walls.sort(key=lambda w: w.notional, reverse=True)

        genuine_ask = next((w for w in walls if w.side == "ask" and w.genuine), None)
        genuine_bid = next((w for w in walls if w.side == "bid" and w.genuine), None)

        return OrderbookView(
            best_bid=bids[0].price,
            best_ask=asks[0].price,
            mid=mid,
            imbalance=imbalance,
            walls=walls[:6],
            genuine_ask_wall=genuine_ask,
            genuine_bid_wall=genuine_bid,
            spoof_count=len(self._spoof_events),
        )

    @property
    def ready(self) -> bool:
        """История набрана - анти-спуфинг фильтру есть на что опереться."""
        return self.updates >= self.cfg.min_persist_snapshots

    def wall_status(self, side: str, price: float) -> Dict[str, float]:
        """Что стало с конкретной плотностью: съели её, сняли или она стоит.

        Нужно ТС плотностей: сделка живёт ровно столько, сколько живёт уровень,
        под который она открыта. `eaten` - доля, съеденная от максимального
        размера уровня; 1.0 означает "уровня в стакане больше нет".
        """
        state = self._levels.get((side, price))
        if state is None:
            # Уровень выбыл из истории (пропал в двух снапшотах подряд).
            return {"present": 0.0, "eaten": 1.0, "last_notional": 0.0,
                    "max_notional": 0.0, "seen": 0.0}
        present = 1.0 if state.missed == 0 else 0.0
        eaten = 1.0 if not present else 1.0 - state.last_size / max(state.max_size, 1e-9)
        return {
            "present": present,
            "eaten": max(0.0, min(1.0, eaten)),
            "last_notional": state.last_size,
            "max_notional": state.max_size,
            "seen": float(state.seen),
        }


class BookManager:
    """Держит трекеры по монетам и опрашивает стакан."""

    def __init__(self, client, cfg: OrderbookConfig):
        self.client = client
        self.cfg = cfg
        self._trackers: Dict[str, BookTracker] = {}
        self._views: Dict[str, OrderbookView] = {}
        self._last_poll: Dict[str, float] = {}

    def tracker(self, symbol: str) -> BookTracker:
        if symbol not in self._trackers:
            self._trackers[symbol] = BookTracker(cfg=self.cfg, symbol=symbol)
        return self._trackers[symbol]

    def view(self, symbol: str) -> OrderbookView:
        return self._views.get(symbol, OrderbookView())

    def ready(self, symbol: str) -> bool:
        return symbol in self._trackers and self._trackers[symbol].ready

    def forget(self, symbol: str) -> None:
        self._trackers.pop(symbol, None)
        self._views.pop(symbol, None)
        self._last_poll.pop(symbol, None)

    def keep_only(self, symbols) -> None:
        keep = set(symbols)
        for s in list(self._trackers):
            if s not in keep:
                self.forget(s)

    async def poll(self, symbol: str) -> Optional[OrderbookView]:
        if not self.cfg.enabled:
            return None
        now = time.monotonic()
        if now - self._last_poll.get(symbol, 0.0) < self.cfg.poll_interval_sec:
            return self._views.get(symbol)
        self._last_poll[symbol] = now
        try:
            bids, asks = await self.client.orderbook(symbol, self.cfg.depth)
        except Exception as exc:  # noqa: BLE001
            log.debug("Стакан %s недоступен: %s", symbol, exc)
            return self._views.get(symbol)
        view = self.tracker(symbol).update(bids, asks)
        self._views[symbol] = view
        return view
