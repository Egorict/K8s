"""Все настройки бота в одном месте.

Значения подобраны под ТС: шорт истощения импульса на альткоин-фьючерсах.
Меняй здесь, а не в коде стратегии.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List


@dataclass
class ScreenerConfig:
    """Шаг 1 ТС: отбор монет."""

    # Рост за сутки строго больше этого значения (0.10 = +10%).
    min_change_24h: float = 0.10
    # Верхняя отсечка: +300% за день - это уже не импульс, а памп-и-дамп на нуле
    # ликвидности, туда лезть не стоит.
    max_change_24h: float = 3.00

    # "Средние объёмы": оборот за 24ч в USDT.
    min_turnover_24h: float = 3_000_000
    max_turnover_24h: float = 250_000_000

    # Монета должна быть альткоином - крупняк и стейблы исключаем.
    excluded_bases: List[str] = field(
        default_factory=lambda: [
            "BTC", "ETH", "USDC", "USDT", "DAI", "FDUSD", "TUSD", "BUSD",
            "WBTC", "WETH", "BTCDOM", "STETH", "WSTETH", "EURT", "EURS",
        ]
    )
    # Плечевые/индексные токены на фьючерсах не берём.
    excluded_suffixes: List[str] = field(default_factory=lambda: ["UP", "DOWN", "3L", "3S", "5L", "5S"])

    # "Резкость": какая доля дневного роста уложилась в последние N часов.
    sharpness_window_hours: int = 4
    min_sharpness: float = 0.45  # >=45% дневного движения - за последние 4 часа

    # Минимальный возраст инструмента (дней) - свежие листинги слишком дикие.
    min_listing_age_days: int = 7

    # Сколько лучших кандидатов держим в работе одновременно.
    max_watchlist: int = 25

    # Как часто пересканируем рынок, секунд.
    rescan_interval_sec: int = 180


@dataclass
class ImpulseConfig:
    """Шаг 2 ТС: импульсный рост без просадок на 1m / 5m / 15m."""

    timeframes: List[str] = field(default_factory=lambda: ["1", "5", "15"])

    # Окно поиска импульса: от 4 до 12 свечей (как в ТС).
    min_candles: int = 4
    max_candles: int = 12

    # Минимальный суммарный рост импульса по таймфреймам, в долях.
    min_gain: Dict[str, float] = field(
        default_factory=lambda: {"1": 0.020, "5": 0.035, "15": 0.055}
    )

    # "Без просадок": максимальный откат внутри импульса как доля от всего движения.
    max_retrace_ratio: float = 0.35
    # Максимальная доля красных свечей в окне импульса.
    max_red_ratio: float = 0.34
    # Каждая отдельная свеча не должна проваливаться ниже открытия импульса.
    require_higher_lows: bool = True

    # Объём в импульсе должен быть выше фона (иначе это не импульс, а дрейф).
    min_volume_ratio: float = 1.4
    volume_baseline_candles: int = 40

    # Сколько таймфреймов минимум должны подтвердить импульс.
    min_confirmed_timeframes: int = 2


@dataclass
class EntryConfig:
    """Шаг 3 ТС: "затуп" - импульс тухнет, входим в шорт."""

    # Диапазон последней свечи меньше среднего диапазона импульса * коэффициент.
    stall_range_ratio: float = 0.60
    # Объём затухает относительно пика импульса.
    stall_volume_ratio: float = 0.75
    # Верхняя тень последних свечей (продавец пришёл).
    min_upper_wick_ratio: float = 0.35
    # Сколько последних свечей не обновляют хай.
    stall_lookback: int = 3
    # Скорость роста (%/свеча) должна упасть относительно пика импульса.
    momentum_decay_ratio: float = 0.45

    # Сколько условий "затупа" из 5 обязательно выполнить.
    min_stall_conditions: int = 3

    # Не входить, если цена уже провалилась ниже этого отката от вершины импульса
    # (поезд ушёл, риск/прибыль испорчены).
    max_drop_from_high: float = 0.020

    # Минимальный итоговый скор сетапа, чтобы открыть сделку.
    min_setup_score: float = 0.55


@dataclass
class RiskConfig:
    """Шаг 4 ТС: размер, стоп, тейк."""

    margin_usd: float = 20.0
    leverage: float = 20.0

    stop_loss_usd: float = 5.0       # убыток, при котором вылетаем
    take_profit_usd: float = 15.0    # цель по прибыли

    # Комиссия тейкера в одну сторону (Bybit linear ~0.055%).
    taker_fee: float = 0.00055

    # Максимум одновременно открытых демо-позиций.
    max_open_positions: int = 3
    # Не открывать вторую сделку по той же монете чаще, чем раз в N секунд.
    symbol_cooldown_sec: int = 1800

    @property
    def notional_usd(self) -> float:
        """Размер позиции в USD = маржа * плечо."""
        return self.margin_usd * self.leverage


@dataclass
class ExitConfig:
    """Шаг 4 ТС (вторая часть): выход по "прогноз не сбылся"."""

    # Насколько сильно цена должна откатиться назад от лучшей точки сделки,
    # чтобы признать, что падение не состоялось (доля от достигнутого движения).
    giveback_ratio: float = 0.55
    # Минимальное движение в плюс, после которого включается трекинг отката.
    min_progress_usd: float = 3.0

    # Терпение по таймфреймам, в минутах: чем старше ТФ - тем дольше ждём.
    max_hold_minutes: Dict[str, int] = field(
        default_factory=lambda: {"1": 12, "5": 45, "15": 150}
    )
    # Сколько минут ждать хоть какого-то движения вниз, иначе выходим по "нет реакции".
    no_reaction_minutes: Dict[str, int] = field(
        default_factory=lambda: {"1": 4, "5": 15, "15": 45}
    )
    # Движение, которое считается "реакцией" (доля от цены входа).
    no_reaction_move: float = 0.003

    # Подтягивать стоп в безубыток после достижения этой прибыли.
    breakeven_at_usd: float = 6.0


@dataclass
class OrderbookConfig:
    """Шаг 5 ТС: стакан и фильтр спуфинга."""

    enabled: bool = True
    depth: int = 200                  # глубина снапшота
    poll_interval_sec: float = 1.0    # частота опроса
    history_len: int = 20             # сколько снапшотов помним

    # Плотность = уровень, объём которого в N раз больше медианного уровня.
    density_multiplier: float = 6.0
    # Зона анализа: +-N% от текущей цены.
    zone_pct: float = 0.015

    # Анти-спуфинг: уровень считается настоящим, только если он
    # прожил min_persist_snapshots опросов и не усох сильнее shrink_tolerance,
    # и при этом не убегал от цены (спуферы отодвигают заявку при подходе).
    min_persist_snapshots: int = 6
    shrink_tolerance: float = 0.45
    max_level_drift_pct: float = 0.0015

    # Реакция бота:
    # - настоящая ask-стена сверху = сопротивление, плюс к скору шорта;
    # - настоящая bid-стена снизу = поддержка, вход запрещаем / тейк подрезаем.
    ask_wall_bonus: float = 0.15
    bid_wall_penalty: float = 0.35
    # Если bid-стена ближе этого расстояния до цены - вход запрещён.
    bid_wall_block_pct: float = 0.006
    # Дисбаланс стакана (bid/ask) выше этого - покупатель сильный, шорт опасен.
    max_bid_ask_imbalance: float = 1.8


@dataclass
class Config:
    mode: str = "demo"                 # только demo - реальные ордера не шлются
    exchange: str = "bybit"
    category: str = "linear"           # USDT-перпетуалы

    screener: ScreenerConfig = field(default_factory=ScreenerConfig)
    impulse: ImpulseConfig = field(default_factory=ImpulseConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    orderbook: OrderbookConfig = field(default_factory=OrderbookConfig)

    # Как часто проверяем сетапы по watchlist, секунд.
    setup_interval_sec: float = 5.0
    # Как часто обновляем открытые позиции, секунд.
    position_interval_sec: float = 1.0

    # Каталог с данными. В контейнере сюда монтируется том: BOT_DATA_DIR=/data.
    data_dir: str = os.environ.get("BOT_DATA_DIR", "data")

    # Веб-панель с журналом торговли (bot/web.py).
    web_enabled: bool = os.environ.get("BOT_WEB", "0") == "1"
    web_host: str = os.environ.get("BOT_WEB_HOST", "0.0.0.0")
    web_port: int = int(os.environ.get("BOT_WEB_PORT", "8080"))

    @property
    def trades_csv(self) -> str:
        return os.path.join(self.data_dir, "trades.csv")

    @property
    def signals_csv(self) -> str:
        return os.path.join(self.data_dir, "signals.csv")

    @property
    def state_json(self) -> str:
        return os.path.join(self.data_dir, "state.json")

    @property
    def log_file(self) -> str:
        return os.path.join(self.data_dir, "bot.log")

    def to_dict(self) -> dict:
        return asdict(self)


CONFIG = Config()
