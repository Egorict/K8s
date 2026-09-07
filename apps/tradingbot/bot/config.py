"""Все настройки бота в одном месте.

В боте живут две независимые торговые системы, выбор - ключом `--strategy`
(или переменной `BOT_STRATEGY`):

  * `impulse` - шорт истощения импульса на альткоин-фьючерсах. Блоки
    ScreenerConfig / ImpulseConfig / EntryConfig / ExitConfig - про неё;
  * `density` - торговля от плотностей в стакане (DensityConfig).

Общее для обеих: RiskConfig (размер сделки) и OrderbookConfig (сбор стакана и
анти-спуфинг). Меняй здесь, а не в коде стратегии.
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
class DensityConfig:
    """ТС №2: торговля от плотностей в стакане.

    Идея: крупная лимитная заявка (плотность) держит цену. Пока она стоит -
    цена от неё отскакивает, поэтому встаём лимиткой ПЕРЕД ней и едем в её
    сторону. Как только плотность съели или сняли, опора исчезла - выходим,
    не дожидаясь, пока цена пройдёт уровень насквозь.
    """

    # --- Шаг 1: какие монеты вообще берём ---------------------------------
    # Монета должна быть живой: тонкий стакан "мусорной" монеты пробивают
    # первым же рыночным ордером, и плотность там ничего не держит.
    min_turnover_24h: float = 20_000_000
    max_turnover_24h: float = 3_000_000_000
    # ...и подвижной: на монете без движения лимитка простоит вечно.
    # ATR по 5-минуткам как доля цены.
    min_atr_pct: float = 0.0030
    # Широкий спред = мало желающих торговать; плотность в таком стакане
    # это чаще всего один шутник, а не реальный интерес.
    max_spread_pct: float = 0.0010
    # Стейблы и завёрнутые токены не двигаются - смысла в них нет.
    excluded_bases: List[str] = field(
        default_factory=lambda: ["USDC", "USDT", "DAI", "FDUSD", "TUSD", "BUSD",
                                 "WBTC", "WETH", "STETH", "WSTETH", "BTCDOM"]
    )
    max_watchlist: int = 12
    rescan_interval_sec: int = 300

    # --- Шаг 2: что считаем плотностью ------------------------------------
    # Уровень крупнее медианного в зоне в N раз. Порог выше, чем у ТС импульса
    # (там стена - лишь один из факторов скора, здесь - вся суть сделки).
    density_multiplier: float = 8.0
    # ...и не меньше этой суммы в деньгах, иначе на ликвидной монете
    # "плотностью" станет любой чуть больший уровень.
    min_wall_notional: float = 40_000
    # Плотность должна доминировать над встречной стороной зоны: если по обе
    # стороны стоят сопоставимые объёмы, никакого дисбаланса нет.
    min_dominance: float = 2.0
    # Работаем только с близкими стенами: до дальней цена может и не дойти,
    # а пока идёт - стену успеют снять.
    max_wall_distance_pct: float = 0.005
    # Анти-спуфинг: сколько опросов подряд плотность обязана простоять.
    # Строже, чем у ТС импульса: здесь мы на неё ставим деньги.
    min_persist_snapshots: int = 10
    # Насколько плотность может усохнуть, оставаясь "настоящей".
    max_shrink: float = 0.30

    # --- Шаг 3: вход -------------------------------------------------------
    # Лимитка встаёт перед плотностью, на этом расстоянии от неё (доля цены).
    # Не в саму плотность: в очереди на её цене мы будем последними.
    entry_offset_pct: float = 0.0004
    # Сколько ждём исполнения лимитки, прежде чем снять её.
    order_ttl_sec: int = 180
    # Комиссия мейкера (лимитка) - вход дешевле, чем по рынку.
    maker_fee: float = 0.0002

    # --- Шаг 4: выход ------------------------------------------------------
    # Плотность съедена на эту долю от максимального размера - выходим.
    wall_eaten_ratio: float = 0.80
    # Цель по прибыли.
    take_profit_usd: float = 5.0
    # Предохранитель: если цена прошла плотность насквозь, ждать нечего.
    # Реальный стоп = ближайший из двух - за уровнем или по деньгам.
    stop_beyond_wall_pct: float = 0.0015
    stop_loss_usd: float = 5.0
    # Дольше держать смысла нет: сетап живёт, пока стоит плотность.
    max_hold_minutes: int = 40

    # Как часто опрашиваем стакан по монетам из наблюдения, секунд.
    poll_interval_sec: float = 1.0


@dataclass
class BreakoutConfig:
    """ТС №3: пробой уровня по тренду. Только лонги.

    Логика: берём монету, которая и так растёт (торгуем по тренду, а не против),
    находим на ней горизонтальное сопротивление с несколькими касаниями и входим
    в момент, когда цена его ТОЛЬКО ЧТО перешла. Держим, пока идёт импульс от
    пробоя; выходим, когда импульс выдохся или когда цена вернулась под уровень -
    значит пробой был ложным.
    """

    # --- Шаг 1: монеты в тренде -------------------------------------------
    min_change_24h: float = 0.03      # растёт за сутки, но
    max_change_24h: float = 1.00      # без вертикальных пампов - там уровней нет
    min_turnover_24h: float = 5_000_000
    max_turnover_24h: float = 500_000_000
    excluded_bases: List[str] = field(
        default_factory=lambda: ["USDC", "USDT", "DAI", "FDUSD", "TUSD", "BUSD",
                                 "WBTC", "WETH", "STETH", "WSTETH", "BTCDOM"]
    )
    # Цена выше своей же средней - подтверждение, что тренд вверх, а не отскок
    # в падении. Считается по тому же ТФ, на котором ищем уровни.
    trend_sma_candles: int = 50
    max_watchlist: int = 15
    rescan_interval_sec: int = 240

    # --- Шаг 2: уровень ----------------------------------------------------
    timeframe: str = "15"             # ТФ для поиска уровней
    lookback_candles: int = 120       # глубина истории под уровни
    pivot_window: int = 3             # окно локального экстремума
    level_tolerance: float = 0.004    # экстремумы в пределах 0.4% - один уровень
    min_touches: int = 2              # уровень без повторных касаний не уровень
    # Уровень должен быть рядом: пробой уровня, до которого 10%, нас не касается.
    max_level_distance: float = 0.02

    # --- Шаг 3: вход -------------------------------------------------------
    # "Немного пересекли": уже выше уровня, но ещё не улетели. Верхняя граница
    # важнее нижней - вход вдогонку после 3% от уровня даёт негодный риск.
    min_break_pct: float = 0.0005
    max_break_pct: float = 0.010
    # Пробой должен быть на объёме: тихий выход за уровень чаще всего ложный.
    min_volume_ratio: float = 1.3
    volume_baseline_candles: int = 30
    # Перед пробоем цена обязана быть ПОД уровнем - иначе это не пробой,
    # а продолжение движения, которое началось раньше.
    require_below_before: int = 2
    # Плотность прямо над точкой входа съест весь импульс - такой пробой пропускаем.
    ask_wall_block_pct: float = 0.004

    # --- Шаг 4: выход ------------------------------------------------------
    # Фиксированного тейка у этой ТС нет: выходим, когда импульс от пробоя утих.
    # Признак затухания - откат от лучшей точки сделки на эту долю.
    fade_giveback: float = 0.45
    # ...но только после того, как импульс вообще состоялся: без этого порога
    # любое дрожание цены в первую минуту закрывало бы сделку.
    min_progress_usd: float = 2.5
    # Импульса не случилось вовсе - сидеть в сделке незачем.
    no_progress_minutes: int = 20
    # Ложный пробой: цена вернулась под уровень на эту долю.
    invalidation_pct: float = 0.002
    # Предохранитель по деньгам (реальный стоп - ближайший из двух).
    stop_loss_usd: float = 5.0
    max_hold_minutes: int = 180

    setup_interval_sec: float = 5.0


@dataclass
class BtcConfig:
    """ТС №4: откуп просадки биткоина. Только лонги, только BTCUSDT.

    Логика: BTC регулярно даёт короткие проливы на пару процентов. Покупаем
    просадку, но не в свободном падении - нужна опора под ценой: горизонтальная
    поддержка с касаниями или настоящая плотность в стакане.
    """

    symbol: str = "BTCUSDT"

    # --- Вход --------------------------------------------------------------
    # Просадка считается от максимума окна, а не от цены сутки назад: нас
    # интересует именно свежий пролив.
    timeframe: str = "5"
    dip_window_candles: int = 48      # 48 x 5м = 4 часа
    min_dip_pct: float = 0.02         # "просел на пару процентов"
    # Глубже - это уже не откат, а слом: ловить такое лонгом не надо.
    max_dip_pct: float = 0.08

    # Опора под ценой. Без неё вход запрещён: покупать пролив без поддержки -
    # это ловля ножа. Достаточно любого из двух подтверждений.
    require_support: bool = True
    support_distance: float = 0.006   # уровень поддержки не дальше 0.6% вниз
    level_tolerance: float = 0.0015   # BTC ликвиден, уровни у него узкие
    min_touches: int = 2
    pivot_window: int = 3
    lookback_candles: int = 180
    # Плотность на покупку в стакане - вторая допустимая опора.
    wall_distance: float = 0.004

    # --- Выход -------------------------------------------------------------
    take_profit_usd: float = 5.0
    stop_loss_usd: float = 2.0
    max_hold_minutes: int = 120

    # Свой кулдаун: монета одна, и общий получасовой простой съел бы всю
    # активность бота.
    cooldown_sec: int = 600
    poll_interval_sec: float = 2.0


@dataclass
class Config:
    mode: str = "demo"                 # только demo - реальные ордера не шлются
    exchange: str = "bybit"
    category: str = "linear"           # USDT-перпетуалы

    # Какую ТС запускать:
    #   impulse  - шорт истощения импульса
    #   density  - торговля от плотностей в стакане
    #   breakout - пробой уровня по тренду (только лонги)
    #   btc      - откуп просадки биткоина (только лонги)
    # Один процесс ведёт одну ТС - так статистика по каждой остаётся чистой,
    # а в кластере это просто разные поды со своими томами.
    strategy: str = os.environ.get("BOT_STRATEGY", "impulse")

    screener: ScreenerConfig = field(default_factory=ScreenerConfig)
    impulse: ImpulseConfig = field(default_factory=ImpulseConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    orderbook: OrderbookConfig = field(default_factory=OrderbookConfig)
    density: DensityConfig = field(default_factory=DensityConfig)
    breakout: BreakoutConfig = field(default_factory=BreakoutConfig)
    btc: BtcConfig = field(default_factory=BtcConfig)

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
