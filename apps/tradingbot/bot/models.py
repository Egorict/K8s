"""Структуры данных бота."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Candle:
    ts: int          # время открытия свечи, мс
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float = 0.0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return max(self.high - self.low, 1e-12)

    @property
    def is_green(self) -> bool:
        return self.close >= self.open

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def upper_wick_ratio(self) -> float:
        return self.upper_wick / self.range


@dataclass
class Ticker:
    symbol: str
    last_price: float
    change_24h: float      # доля, 0.15 = +15%
    turnover_24h: float
    volume_24h: float
    high_24h: float = 0.0
    low_24h: float = 0.0


@dataclass
class Level:
    """Уровень стакана."""
    price: float
    size: float

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class Wall:
    """Найденная плотность в стакане."""
    side: str            # 'ask' | 'bid'
    price: float
    size: float
    notional: float
    distance_pct: float  # расстояние до текущей цены, доля
    persisted: int       # в скольких снапшотах подряд видели
    genuine: bool        # прошёл ли анти-спуфинг фильтр

    def describe(self) -> str:
        tag = "real" if self.genuine else "spoof?"
        return (f"{self.side.upper()} {self.notional:,.0f}$ @ {self.price:g} "
                f"({self.distance_pct * 100:+.2f}%, {self.persisted} снап., {tag})")


@dataclass
class OrderbookView:
    """Свёртка стакана для стратегии."""
    mid: float = 0.0
    best_bid: float = 0.0                  # лучшие цены нужны ТС плотностей:
    best_ask: float = 0.0                  # по ним решается, исполнилась ли лимитка
    imbalance: float = 1.0                 # sum(bid notional) / sum(ask notional) в зоне
    walls: List[Wall] = field(default_factory=list)
    genuine_ask_wall: Optional[Wall] = None
    genuine_bid_wall: Optional[Wall] = None
    spoof_count: int = 0

    def summary(self) -> str:
        parts = [f"imb={self.imbalance:.2f}"]
        if self.genuine_ask_wall:
            parts.append("ask_wall=" + self.genuine_ask_wall.describe())
        if self.genuine_bid_wall:
            parts.append("bid_wall=" + self.genuine_bid_wall.describe())
        if self.spoof_count:
            parts.append(f"spoof={self.spoof_count}")
        return "; ".join(parts)


@dataclass
class ImpulseInfo:
    """Результат анализа импульса на одном таймфрейме."""
    timeframe: str
    found: bool = False
    candles: int = 0
    gain: float = 0.0             # доля роста импульса
    start_price: float = 0.0
    high_price: float = 0.0
    max_retrace_ratio: float = 0.0
    red_ratio: float = 0.0
    volume_ratio: float = 0.0
    avg_range: float = 0.0
    peak_speed: float = 0.0       # %/свеча на пике импульса
    score: float = 0.0
    reason: str = ""


@dataclass
class StallInfo:
    """Результат поиска "затупа"."""
    timeframe: str
    stalled: bool = False
    conditions: Dict[str, bool] = field(default_factory=dict)
    passed: int = 0
    drop_from_high: float = 0.0
    reason: str = ""


@dataclass
class Setup:
    """Готовый к исполнению сетап."""
    symbol: str
    price: float
    primary_tf: str
    score: float
    impulses: Dict[str, ImpulseInfo]
    stall: StallInfo
    book: OrderbookView
    ticker: Ticker
    notes: List[str] = field(default_factory=list)


@dataclass
class WallSetup:
    """Сетап ТС плотностей: найденная опора и лимитка перед ней."""
    symbol: str
    side: str              # сторона НАШЕЙ сделки: 'long' у bid-стены, 'short' у ask-стены
    wall: Wall
    entry_price: float     # цена лимитки - перед плотностью, а не в ней
    mid: float
    dominance: float       # во сколько раз плотность крупнее встречной стороны
    ticker: Ticker
    notes: List[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Насколько сетап хорош: живучесть стены + её доминирование + близость."""
        persist = min(1.0, self.wall.persisted / 20.0)
        dominance = min(1.0, self.dominance / 5.0)
        proximity = 1.0 - min(1.0, abs(self.wall.distance_pct) / 0.005)
        return round(0.4 * persist + 0.4 * dominance + 0.2 * proximity, 3)


@dataclass
class PendingOrder:
    """Выставленная, но ещё не исполненная лимитка (ТС плотностей)."""
    symbol: str
    side: str
    price: float
    qty: float
    placed_at: float
    setup: WallSetup

    def age_sec(self) -> float:
        return time.time() - self.placed_at


@dataclass
class Position:
    """Демо-позиция. side: 'short' (ТС импульса) или 'long'/'short' (ТС плотностей)."""
    trade_id: str
    symbol: str
    side: str
    timeframe: str
    qty: float
    entry_price: float
    stop_price: float
    take_price: float
    opened_at: float
    margin_usd: float
    leverage: float
    notional_usd: float
    entry_score: float
    entry_reason: str
    setup_snapshot: Dict = field(default_factory=dict)

    # Комиссии в долях, по сторонам сделки. У ТС импульса вход рыночный
    # (тейкер), у ТС плотностей - лимиткой (мейкер, дешевле), а выход в обеих
    # по рынку. Хранятся в позиции, чтобы расчёт PnL не зависел от того,
    # какая ТС её открыла.
    entry_fee_rate: float = 0.00055
    exit_fee_rate: float = 0.00055

    # Рантайм.
    last_price: float = 0.0
    best_price: float = 0.0            # лучшая (минимальная для шорта) цена
    best_pnl_usd: float = 0.0
    worst_pnl_usd: float = 0.0
    reacted: bool = False              # было ли ожидаемое движение вниз
    breakeven_armed: bool = False
    # Плотность, из-за которой открылась сделка (ТС плотностей).
    wall_side: str = ""
    wall_price: float = 0.0
    wall_notional: float = 0.0

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    def gross_pnl(self, price: float) -> float:
        """PnL без комиссий, USD. Знак зависит от стороны сделки."""
        if self.is_long:
            return (price - self.entry_price) * self.qty
        return (self.entry_price - price) * self.qty

    def fees(self, price: float) -> float:
        return 0.0  # заполняется движком (fee_rate знает он)

    def age_sec(self) -> float:
        return time.time() - self.opened_at


@dataclass
class ClosedTrade:
    trade_id: str
    symbol: str
    side: str
    timeframe: str
    qty: float
    entry_price: float
    exit_price: float
    opened_at: float
    closed_at: float
    stop_price: float
    take_price: float
    margin_usd: float
    leverage: float
    notional_usd: float
    gross_pnl_usd: float
    fees_usd: float
    net_pnl_usd: float
    pnl_pct_on_margin: float
    exit_reason: str
    entry_reason: str
    entry_score: float
    max_profit_usd: float
    max_loss_usd: float
    duration_sec: float
