"""Бэктест: «как бы бот торговал последние месяцы», помесячно и рядом с BTC.

Зачем. Живой демо-бот показывает результат только с момента запуска: чтобы
понять, прибыльна ли ТС, надо ждать месяцами. Здесь та же ТС прогоняется по
УЖЕ ПРОШЕДШЕЙ истории цен, и результат виден сразу - по месяцам, а рядом с
каждым месяцем движение биткоина за него же. Так видно не только «сколько
заработал», но и «в каком рынке»: ТС, которая делает деньги только в растущем
рынке, и ТС, которой всё равно, - это разные ТС, хотя итог за период может
совпасть.

ЧТО ЗДЕСЬ ЧЕСТНО, А ЧТО ПРИБЛИЖЕНИЕ - самое важное в этом файле.

Публичная история Bybit - это ТОЛЬКО СВЕЧИ. Ни стакана, ни ленты принтов за
прошлые месяцы не отдаёт никто: эти данные существуют лишь в моменте. Отсюда
три уровня достоверности, и каждый отчёт помечен своим:

  full   - ТС решает по свечам, и реплей вызывает ЕЁ ЖЕ функции разбора
           (impulse, btc). Отличия от живого бота только в частоте проверок;
  approx - часть входных данных ТС восстановить нельзя, и она заменена
           приближением по свечам (breakout: активность у уровня). Цифры
           показывают порядок величины, а не то, что бот сделал бы буквально;
  none   - восстановить нечего (density: вся ТС - это стакан). Цифр нет,
           показываем только движение BTC по месяцам.

Чем ещё реплей отличается от живого бота (относится ко всем ТС):

  * бот проверяет сетапы раз в несколько секунд и видит НЕДОСФОРМИРОВАННУЮ
    свечу; реплей принимает решение один раз - на закрытии свечи. Поэтому
    вход получается на закрытии, а не в середине движения;
  * ведение позиции идёт по свечам: стоп и тейк проверяются по high/low, и
    если свеча задела оба уровня, засчитывается СТОП. Это сознательно
    пессимистично - лучше недосчитать прибыль, чем нарисовать её;
  * набор монет фиксирован на весь период (см. pick_universe), тогда как
    живой сканер каждые несколько минут пересобирает список из всего рынка.

Читать результат стоит с этой поправкой: это проверка идеи ТС на истории,
а не обещание доходности.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .exchange import BybitPublic
from .history import History, interval_ms, month_key, resample
from .indicators import safe_mean, scale
from .models import Candle, OrderbookView, Ticker
from .paper import net_pnl_usd, round_trip_fees, target_levels

log = logging.getLogger("backtest")

# Уровни достоверности отчёта. Панель раскрашивает вкладку по этому полю.
FIDELITY_FULL = "full"
FIDELITY_APPROX = "approx"
FIDELITY_NONE = "none"

# Сколько сделок кладём в отчёт целиком. Помесячные итоги считаются по всем,
# а вот список для раскрытия месяца в панели ограничиваем: иначе backtest.json
# на активной ТС разрастётся до десятков мегабайт и панель будет его качать.
MAX_TRADES_IN_REPORT = 4000


# ---------------------------------------------------------------- параметры

@dataclass
class BacktestParams:
    """Что именно прогоняем. Значения по умолчанию - то, что влезает в под."""

    # Глубина истории. Четыре месяца - компромисс: помесячная картина уже
    # читается, а закачка занимает минуты, а не часы.
    months: int = 4
    # 0 = все монеты, прошедшие фильтр ликвидности. Иначе - топ N по обороту.
    max_symbols: int = 0
    # Нижняя планка оборота за сутки: на монете тоньше этой любой бэктест
    # рисует сделки, которых в реальности не исполнить.
    min_turnover_24h: float = 3_000_000
    # Таймфреймы. fast - на нём ведётся позиция и считаются сетапы ТС импульса,
    # slow - на нём живут уровни ТС пробоя.
    fast_tf: str = "5"
    slow_tf: str = "15"
    # Уровни ТС пробоя пересчитываются не на каждой свече: поиск экстремумов по
    # 400 свечам стоит дорого, а сами уровни за час не меняются.
    level_recalc_every: int = 4

    def window(self) -> Tuple[int, int]:
        """(начало, конец) периода в миллисекундах. Конец - прошлая полночь UTC."""
        now = datetime.now(tz=timezone.utc)
        end = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        start = end - timedelta(days=31 * self.months)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1


# ------------------------------------------------------------------ сделка

@dataclass
class SimTrade:
    """Сделка, которую ТС совершила бы на истории."""
    symbol: str
    side: str
    timeframe: str
    opened_ms: int
    closed_ms: int
    entry_price: float
    exit_price: float
    qty: float
    net_pnl_usd: float
    fees_usd: float
    exit_reason: str
    entry_reason: str
    score: float
    best_pnl_usd: float = 0.0
    worst_pnl_usd: float = 0.0

    @property
    def duration_min(self) -> float:
        return (self.closed_ms - self.opened_ms) / 60_000

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "timeframe": self.timeframe,
            "month": month_key(self.opened_ms),
            "open_time": _fmt(self.opened_ms),
            "close_time": _fmt(self.closed_ms),
            "duration_min": round(self.duration_min, 1),
            "entry_price": float(f"{self.entry_price:.10g}"),
            "exit_price": float(f"{self.exit_price:.10g}"),
            "profit_usd": round(self.net_pnl_usd, 4),
            "fees_usd": round(self.fees_usd, 4),
            "exit_reason": self.exit_reason,
            "entry_reason": self.entry_reason,
            "score": round(self.score, 3),
        }


def _fmt(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------- ведение позиции по свечам

@dataclass
class PosState:
    """Живая позиция внутри реплея."""
    side: str
    qty: float
    entry_price: float
    stop_price: float
    take_price: float
    entry_fee: float
    exit_fee: float
    opened_idx: int
    opened_ms: int
    bar_minutes: float
    best_pnl_usd: float = 0.0
    worst_pnl_usd: float = 0.0
    reacted: bool = False
    breakeven_armed: bool = False
    extra: Dict = field(default_factory=dict)

    def pnl(self, price: float) -> float:
        return net_pnl_usd(self.side, self.qty, self.entry_price, price,
                           self.entry_fee, self.exit_fee)

    def age_min(self, idx: int) -> float:
        return (idx - self.opened_idx) * self.bar_minutes

    def favourable(self, candle: Candle) -> float:
        """Цена свечи, лучшая для нас: лонгу - хай, шорту - лоу."""
        return candle.high if self.side == "long" else candle.low

    def adverse(self, candle: Candle) -> float:
        return candle.low if self.side == "long" else candle.high


def walk_position(candles: Sequence[Candle], state: PosState,
                  on_bar: Optional[Callable[[int, Candle, PosState], Optional[str]]] = None,
                  ) -> Tuple[int, float, str]:
    """Проводит позицию по свечам до выхода. -> (индекс, цена, причина).

    Порядок внутри свечи намеренно пессимистичный: сначала проверяется
    неблагоприятный край (стоп), потом благоприятный (тейк). Внутри одной
    свечи порядок движения цены по OHLC неизвестен, и если засчитывать тейк,
    результат бэктеста получался бы систематически лучше реального.

    on_bar - правила самой ТС (откат, отсутствие реакции, время удержания,
    затухание активности). Вызывается на закрытии свечи, то есть уже после
    стопа и тейка: денежные уровни срабатывают раньше любых рассуждений.
    """
    last_idx = state.opened_idx
    last_price = state.entry_price
    for idx in range(state.opened_idx + 1, len(candles)):
        candle = candles[idx]
        last_idx, last_price = idx, candle.close

        # 1. Стоп: цена дошла до уровня внутри свечи.
        if (state.side == "long" and candle.low <= state.stop_price) or \
           (state.side == "short" and candle.high >= state.stop_price):
            state.worst_pnl_usd = min(state.worst_pnl_usd, state.pnl(state.stop_price))
            return idx, state.stop_price, "стоп-лосс"

        # 2. Тейк.
        if (state.side == "long" and candle.high >= state.take_price) or \
           (state.side == "short" and candle.low <= state.take_price):
            state.best_pnl_usd = max(state.best_pnl_usd, state.pnl(state.take_price))
            return idx, state.take_price, "тейк-профит"

        # 3. Экстремумы свечи - в метрики позиции: правила отката и безубытка
        #    смотрят на лучшую достигнутую точку, а не только на закрытие.
        state.best_pnl_usd = max(state.best_pnl_usd, state.pnl(state.favourable(candle)))
        state.worst_pnl_usd = min(state.worst_pnl_usd, state.pnl(state.adverse(candle)))

        if on_bar is not None:
            reason = on_bar(idx, candle, state)
            if reason:
                return idx, candle.close, reason

    # История кончилась раньше выхода - сделку не засчитываем как закрытую,
    # причина помечена отдельно, чтобы она была видна в отчёте.
    return last_idx, last_price, "период закончился"


# ------------------------------------------------------------ подготовка ряда

class Series:
    """Предрасчёт по ряду свечей: скользящие суммы за сутки и средние.

    Нужен ради скорости. Прогон четырёх месяцев по сотням монет - это
    десятки миллионов точек, и вызывать на каждой полноценный разбор ТС
    невозможно. Зато дешёвые фильтры (рост за сутки, оборот, средние) отсекают
    подавляющее большинство точек, и дорогой разбор остаётся только там, где
    ТС действительно могла бы что-то найти.
    """

    def __init__(self, candles: List[Candle], interval: str):
        self.candles = candles
        self.interval = interval
        self.step_ms = interval_ms(interval)
        self.bar_min = self.step_ms / 60_000
        n = len(candles)
        self.closes = [c.close for c in candles]
        self.bars_24h = max(1, 86_400_000 // self.step_ms)

        # Скользящие суммы за сутки одним проходом.
        self.turnover_24h = [0.0] * n
        self.volume_24h = [0.0] * n
        turn = vol = 0.0
        for i, c in enumerate(candles):
            turn += c.turnover
            vol += c.volume
            if i >= self.bars_24h:
                turn -= candles[i - self.bars_24h].turnover
                vol -= candles[i - self.bars_24h].volume
            self.turnover_24h[i] = turn
            self.volume_24h[i] = vol

    def change_24h(self, i: int) -> Optional[float]:
        j = i - self.bars_24h
        if j < 0 or self.closes[j] <= 0:
            return None
        return self.closes[i] / self.closes[j] - 1.0

    def sma(self, i: int, period: int) -> Optional[float]:
        if i < 0 or i + 1 < period:
            return None
        return safe_mean(self.closes[i + 1 - period: i + 1])

    def ticker(self, symbol: str, i: int) -> Optional[Ticker]:
        """Тикер «как он выглядел бы» на закрытии свечи i."""
        change = self.change_24h(i)
        if change is None:
            return None
        window = self.candles[max(0, i + 1 - self.bars_24h): i + 1]
        return Ticker(
            symbol=symbol,
            last_price=self.closes[i],
            change_24h=change,
            turnover_24h=self.turnover_24h[i],
            volume_24h=self.volume_24h[i],
            high_24h=max(c.high for c in window),
            low_24h=min(c.low for c in window),
        )


# ------------------------------------------------------------- базовый реплей

class Replay:
    """Один прогон одной ТС по истории одной монеты."""

    fidelity = FIDELITY_FULL
    caveats: List[str] = []

    def __init__(self, cfg: Config, params: BacktestParams):
        # Копия конфига: стакана в истории нет, и модули ТС должны об этом
        # знать явно, а не получать пустой стакан и считать его настоящим.
        self.cfg = copy.deepcopy(cfg)
        self.cfg.orderbook.enabled = False
        self.params = params

    def symbols(self, universe: List[Ticker]) -> List[str]:
        return [t.symbol for t in universe]

    async def load(self, hist: History, symbol: str, start: int, end: int) -> Dict[str, List[Candle]]:
        """Свечи, нужные этой ТС. По умолчанию - только быстрый ТФ."""
        return {self.params.fast_tf: await hist.load(symbol, self.params.fast_tf, start, end)}

    def run_symbol(self, symbol: str, data: Dict[str, List[Candle]]) -> List[SimTrade]:
        raise NotImplementedError

    # --- общее: открытие позиции ------------------------------------------

    def open_position(self, side: str, price: float, idx: int, ts: int, bar_min: float,
                      stop_usd: float, take_usd: float,
                      stop_price_limit: Optional[float] = None) -> PosState:
        risk = self.cfg.risk
        notional = risk.notional_usd
        qty = notional / price
        stop_price, take_price = target_levels(
            side, price, qty, notional, stop_usd, take_usd, risk.taker_fee, risk.taker_fee)
        if stop_price_limit is not None:
            stop_price = (max(stop_price, stop_price_limit) if side == "long"
                          else min(stop_price, stop_price_limit))
        return PosState(
            side=side, qty=qty, entry_price=price,
            stop_price=stop_price, take_price=take_price,
            entry_fee=risk.taker_fee, exit_fee=risk.taker_fee,
            opened_idx=idx, opened_ms=ts, bar_minutes=bar_min,
        )

    @staticmethod
    def finish(symbol: str, state: PosState, candles: Sequence[Candle],
               exit_idx: int, exit_price: float, reason: str,
               timeframe: str, entry_reason: str, score: float) -> SimTrade:
        fees = round_trip_fees(state.qty, state.entry_price, exit_price,
                               state.entry_fee, state.exit_fee)
        return SimTrade(
            symbol=symbol, side=state.side, timeframe=timeframe,
            opened_ms=state.opened_ms, closed_ms=candles[exit_idx].ts,
            entry_price=state.entry_price, exit_price=exit_price, qty=state.qty,
            net_pnl_usd=state.pnl(exit_price), fees_usd=fees,
            exit_reason=reason, entry_reason=entry_reason, score=score,
            best_pnl_usd=state.best_pnl_usd, worst_pnl_usd=state.worst_pnl_usd,
        )


# ------------------------------------------------------- ТС импульса (full)

class ImpulseReplay(Replay):
    """Шорт истощения импульса. Разбор свечей - функциями самой ТС.

    Одно отличие от живого бота, о котором надо знать: тот подтверждает импульс
    на 1m/5m/15m и требует двух подтверждений из трёх, здесь таймфреймов два
    (5m и 15m) и подтвердить должны оба. То есть условие входа строже, сделок
    в бэктесте будет меньше, чем у живого бота.
    """

    fidelity = FIDELITY_FULL
    caveats = [
        "Таймфреймы 5m и 15m (у живого бота ещё 1m): подтвердить импульс "
        "должны оба, поэтому вход строже и сделок меньше.",
        "Стакан в истории недоступен, поэтому запрет входа по плотности на "
        "покупку и поправки к скору не применяются.",
    ]

    async def load(self, hist, symbol, start, end):
        # 15-минутки не качаем: они склеиваются из пятиминуток. Это не только
        # вдвое меньше запросов, но и единственный способ получить ЧЕСТНУЮ
        # недоформированную 15-минутку на момент решения.
        return {self.params.fast_tf: await hist.load(symbol, self.params.fast_tf, start, end)}

    def run_symbol(self, symbol: str, data: Dict[str, List[Candle]]) -> List[SimTrade]:
        cfg = self.cfg
        fast = data[self.params.fast_tf]
        icfg, scr = cfg.impulse, cfg.screener
        need5 = icfg.volume_baseline_candles + icfg.max_candles + 10
        # На 15m нужен такой же запас свечей, а одна 15m - это три 5m.
        need5_for_slow = need5 * (interval_ms(self.params.slow_tf) // interval_ms(self.params.fast_tf))
        warmup = max(need5, need5_for_slow)
        if len(fast) < warmup + 10:
            return []

        series = Series(fast, self.params.fast_tf)
        sharp_bars = max(4, int(scr.sharpness_window_hours * 3_600_000 / series.step_ms))
        cooldown_bars = int(cfg.risk.symbol_cooldown_sec * 1000 / series.step_ms)

        trades: List[SimTrade] = []
        i = max(warmup, series.bars_24h + sharp_bars)
        while i < len(fast) - 1:
            # --- дешёвый фильтр: то же, что делает сканер (bot/scanner.py) ---
            change = series.change_24h(i)
            if change is None or not (scr.min_change_24h < change <= scr.max_change_24h):
                i += 1
                continue
            turnover = series.turnover_24h[i]
            if not (scr.min_turnover_24h <= turnover <= scr.max_turnover_24h):
                i += 1
                continue
            if self._sharpness(series, i, sharp_bars, change) < scr.min_sharpness:
                i += 1
                continue

            # --- дорогой разбор: функции самой ТС ---------------------------
            setup = self._setup(symbol, series, i, warmup)
            if setup is None:
                i += 1
                continue

            trade = self._trade(symbol, fast, series, i, setup)
            if trade is None:
                i += 1
                continue
            trades.append(trade)
            # Позиция закрыта - монета уходит в кулдаун, как у живого бота.
            closed_idx = self._index_of(fast, trade.closed_ms, i)
            i = closed_idx + cooldown_bars + 1

        return trades

    @staticmethod
    def _index_of(candles: Sequence[Candle], ts: int, fallback: int) -> int:
        # Свечи идут равномерно, поэтому индекс считается арифметикой, а не
        # поиском: на миллионах точек это заметно.
        if len(candles) < 2:
            return fallback
        step = candles[1].ts - candles[0].ts
        idx = (ts - candles[0].ts) // step
        return int(min(max(idx, fallback), len(candles) - 1))

    @staticmethod
    def _sharpness(series: Series, i: int, bars: int, change: float) -> float:
        """Доля дневного движения, уложившаяся в окно резкости (как в сканере)."""
        j = i - bars
        if j < 0 or change <= 0:
            return 0.0
        start = series.candles[j].open
        last = series.closes[i]
        day_start = last / (1.0 + change)
        day_move = last - day_start
        if day_move <= 0:
            return 0.0
        return max(0.0, min((last - start) / day_move, 2.0))

    def _setup(self, symbol: str, series: Series, i: int, warmup: int):
        from .strategy import build_setup

        fast = series.candles
        ticker = series.ticker(symbol, i)
        if ticker is None:
            return None

        tail = fast[max(0, i + 1 - warmup): i + 1]
        slow = resample(tail, self.params.fast_tf, self.params.slow_tf, until_ts=fast[i].ts)
        if len(slow) < self.cfg.impulse.max_candles + 6:
            return None

        # Последняя свеча обоих ТФ - «формирующаяся» с точки зрения ТС: ровно
        # так она выглядит в момент своего закрытия. Ничего из будущего в
        # срезах нет.
        candles_by_tf = {self.params.fast_tf: tail, self.params.slow_tf: slow}
        return build_setup(symbol, ticker, candles_by_tf, OrderbookView(), self.cfg)

    def _trade(self, symbol: str, fast: List[Candle], series: Series, i: int, setup):
        cfg = self.cfg
        xcfg = cfg.exit
        tf = setup.primary_tf
        state = self.open_position(
            "short", setup.price, i, fast[i].ts, series.bar_min,
            cfg.risk.stop_loss_usd, cfg.risk.take_profit_usd)

        def on_bar(idx: int, candle: Candle, st: PosState) -> Optional[str]:
            # Порядок правил повторяет PaperBroker.evaluate (bot/paper.py).
            if not st.reacted:
                move_down = (st.entry_price - candle.low) / st.entry_price
                if move_down >= xcfg.no_reaction_move:
                    st.reacted = True

            if not st.breakeven_armed and st.best_pnl_usd >= xcfg.breakeven_at_usd:
                st.breakeven_armed = True
                cushion = round_trip_fees(st.qty, st.entry_price, st.entry_price,
                                          st.entry_fee, st.exit_fee) / st.qty
                st.stop_price = min(st.stop_price, st.entry_price - cushion)

            pnl = st.pnl(candle.close)
            if st.best_pnl_usd >= xcfg.min_progress_usd:
                if pnl <= st.best_pnl_usd * (1.0 - xcfg.giveback_ratio):
                    return (f"падение не состоялось: откат с {st.best_pnl_usd:.2f}$ "
                            f"до {pnl:.2f}$")

            age = st.age_min(idx)
            if not st.reacted and age >= xcfg.no_reaction_minutes.get(tf, 15):
                return f"нет реакции за {age:.0f} мин"
            if age >= xcfg.max_hold_minutes.get(tf, 60):
                return f"истекло время удержания ({age:.0f} мин)"
            return None

        exit_idx, exit_price, reason = walk_position(fast, state, on_bar)
        if reason == "период закончился":
            return None
        return self.finish(symbol, state, fast, exit_idx, exit_price, reason,
                           timeframe=tf, entry_reason=" | ".join(setup.notes),
                           score=setup.score)


# ----------------------------------------------------------- ТС биткоина (full)

class BtcReplay(Replay):
    """Откуп просадки BTC. Вход считает find_dip - функция самой ТС.

    Единственная опора, которую нельзя восстановить, - плотность на покупку в
    стакане. Она была необязательной (достаточно любой из двух), поэтому ТС
    работает и без неё, просто входов чуть меньше.
    """

    fidelity = FIDELITY_FULL
    caveats = [
        "Опора «плотность на покупку в стакане» недоступна: вход разрешается "
        "только при горизонтальной поддержке. Живой бот входил бы чуть чаще.",
    ]

    def symbols(self, universe: List[Ticker]) -> List[str]:
        return [self.cfg.btc.symbol]

    def run_symbol(self, symbol: str, data: Dict[str, List[Candle]]) -> List[SimTrade]:
        from .btc import find_dip

        bcfg = self.cfg.btc
        fast = data[self.params.fast_tf]
        warmup = max(bcfg.lookback_candles, bcfg.dip_window_candles) + 5
        if len(fast) < warmup + 10:
            return []

        series = Series(fast, self.params.fast_tf)
        cooldown_bars = int(bcfg.cooldown_sec * 1000 / series.step_ms)
        trades: List[SimTrade] = []

        i = max(warmup, series.bars_24h)
        while i < len(fast) - 1:
            price = series.closes[i]
            window = fast[i + 1 - bcfg.dip_window_candles: i + 1]
            high = max(c.high for c in window)
            dip = (high - price) / high if high > 0 else 0.0
            # Дешёвая отсечка перед дорогим поиском уровней поддержки.
            if not (bcfg.min_dip_pct <= dip <= bcfg.max_dip_pct):
                i += 1
                continue

            ticker = series.ticker(symbol, i)
            if ticker is None:
                i += 1
                continue
            # Хвост, а не весь префикс: find_dip всё равно смотрит только
            # последние lookback_candles, а копировать растущий список на
            # каждой свече - это квадрат по времени.
            setup = find_dip(ticker, fast[max(0, i + 1 - warmup): i + 1], None, self.cfg)
            if setup is None:
                i += 1
                continue

            state = self.open_position("long", setup.price, i, fast[i].ts, series.bar_min,
                                       bcfg.stop_loss_usd, bcfg.take_profit_usd)

            def on_bar(idx: int, candle: Candle, st: PosState) -> Optional[str]:
                age = st.age_min(idx)
                if age >= bcfg.max_hold_minutes:
                    return f"истекло время удержания ({age:.0f} мин)"
                return None

            exit_idx, exit_price, reason = walk_position(fast, state, on_bar)
            if reason == "период закончился":
                break
            trades.append(self.finish(symbol, state, fast, exit_idx, exit_price, reason,
                                      timeframe=bcfg.timeframe,
                                      entry_reason=" | ".join(setup.notes),
                                      score=setup.score))
            i = exit_idx + cooldown_bars + 1

        return trades


# --------------------------------------------------------- ТС пробоя (approx)

def proxy_activity(candle: Candle, level_price: float, baseline_volume: float,
                   cfg) -> Tuple[float, float, Dict[str, float]]:
    """Приближение активности у уровня по ОДНОЙ свече. -> (score, ratio, детали).

    Настоящий замер (bot/activity.py) складывается из шести чисел, четыре из
    которых берутся из ленты принтов и стакана. В истории их нет, поэтому:

      темп ленты (trades_per_min)   - восстановить нечем, вес отдан обороту;
      оборот (volume_per_min)       - есть как есть: turnover свечи / 15 мин;
      доля у уровня (near_share)    - какая часть диапазона свечи попала в
                                      зону уровня. Это не доля ОБОРОТА, а доля
                                      ХОДА ЦЕНЫ, но смысл тот же: торговалось
                                      ли дело у уровня или в стороне;
      заявки в стакане              - восстановить нечем, вес отдан давлению
                                      покупателя: где свеча закрылась внутри
                                      своего диапазона;
      колебания (swings)            - свеча задела уровень: 1, иначе 0. У живой
                                      ленты это число до нескольких десятков,
                                      поэтому вклад здесь заведомо меньше;
      фон монеты (ratio)            - оборот свечи против медианы последних 40.

    Поэтому у ТС пробоя уровень достоверности - approx: уровни, тренд и
    денежная часть настоящие, а «кипит ли торговля» - это оценка по свече.
    """
    minutes = 15.0
    volume_per_min = candle.turnover / minutes
    lo = level_price * (1 - cfg.level_zone_pct)
    hi = level_price * (1 + cfg.level_zone_pct)
    overlap = max(0.0, min(candle.high, hi) - max(candle.low, lo))
    near_share = overlap / candle.range
    swings = 1.0 if candle.low <= level_price <= candle.high else 0.0
    buy_ratio = (candle.close - candle.low) / candle.range

    score = round(
        0.50 * scale(volume_per_min, cfg.min_volume_per_min, cfg.min_volume_per_min * 6)
        + 0.25 * scale(near_share, cfg.min_near_share, 0.85)
        + 0.15 * scale(buy_ratio, cfg.min_buy_ratio, 0.95)
        + 0.10 * swings,
        4,
    )
    ratio = volume_per_min / baseline_volume if baseline_volume > 0 else 0.0
    return score, ratio, {
        "volume_per_min": volume_per_min,
        "near_share": near_share,
        "swings": swings,
        "buy_ratio": buy_ratio,
    }


class BreakoutReplay(Replay):
    """Пробой уровня, версия 0.1. Уровни и тренд - настоящие, активность - оценка."""

    fidelity = FIDELITY_APPROX
    caveats = [
        "Активность у уровня (лента принтов и стакан) в истории недоступна и "
        "заменена оценкой по свече: оборот против фона, доля хода цены в зоне "
        "уровня, положение закрытия внутри свечи. Это главный вход и главный "
        "выход этой ТС, поэтому цифры показывают порядок величины, а не то, "
        "что бот сделал бы буквально.",
        "Пороги по темпу ленты (принтов/мин) и по объёму заявок в стакане не "
        "проверяются - восстановить их нечем.",
        "Уровни пересчитываются раз в час, а не раз в 45 секунд: поиск "
        "экстремумов по сотням свечей на сотнях монет иначе не считается.",
    ]

    def use_v2(self) -> bool:
        return False

    async def load(self, hist, symbol, start, end):
        # Уровням нужен запас истории ДО начала периода, иначе первые недели
        # прогона остались бы без уровней вовсе.
        lookback = (self.cfg.breakout_v2.lookback_candles if self.use_v2()
                    else self.cfg.breakout.lookback_candles)
        pad = lookback * interval_ms(self.params.slow_tf)
        return {self.params.slow_tf: await hist.load(symbol, self.params.slow_tf,
                                                     start - pad, end)}

    def _level_for(self, window: List[Candle], price: float):
        if self.use_v2():
            from .breakout_v2 import level_ahead
            return level_ahead(window, price, self.cfg)
        from .breakout import pick_level
        return pick_level(window, price, self.cfg)

    def _in_zone(self, price: float, level) -> bool:
        if self.use_v2():
            from .breakout_v2 import in_breakout_zone
            return in_breakout_zone(price, level, self.cfg)
        from .breakout import in_zone
        return in_zone(price, level, self.cfg)

    def _trend_ok(self, series: Series, i: int) -> bool:
        """Восходящий тренд - те же три (для 0.2 пять) условий, что у ТС."""
        b = self.cfg.breakout
        change = series.change_24h(i)
        if change is None:
            return False
        min_change = (self.cfg.breakout_v2.min_change_24h if self.use_v2()
                      else b.min_change_24h)
        if not (min_change <= change <= b.max_change_24h):
            return False
        if not (b.min_turnover_24h <= series.turnover_24h[i] <= b.max_turnover_24h):
            return False

        fast_sma = series.sma(i, b.trend_fast_sma)
        slow_sma = series.sma(i, b.trend_slow_sma)
        slow_before = series.sma(i - b.trend_slope_candles, b.trend_slow_sma)
        if fast_sma is None or slow_sma is None or slow_before is None or slow_before <= 0:
            return False
        if series.closes[i] <= fast_sma or fast_sma <= slow_sma:
            return False
        slope = (slow_sma - slow_before) / slow_before
        min_slope = (self.cfg.breakout_v2.min_trend_slope if self.use_v2()
                     else b.min_trend_slope)
        if slope < min_slope:
            return False

        if self.use_v2():
            v2 = self.cfg.breakout_v2
            recent = series.closes[i + 1 - v2.trend_hold_candles: i + 1]
            if len(recent) < v2.trend_hold_candles:
                return False
            above = sum(1 for p in recent if p > slow_sma) / len(recent)
            if above < v2.min_above_slow_share:
                return False
        return True

    def _entry_ok(self, score: float, ratio: float, detail: Dict[str, float]) -> bool:
        b = self.cfg.breakout
        if self.use_v2():
            v2 = self.cfg.breakout_v2
            if detail["volume_per_min"] < v2.min_symbol_volume_per_min:
                return False
            min_score, min_ratio = v2.min_entry_score, v2.min_activity_ratio
        else:
            min_score, min_ratio = b.min_entry_score, b.min_activity_ratio
        if score < min_score or ratio < min_ratio:
            return False
        if detail["volume_per_min"] < b.min_volume_per_min:
            return False
        if detail["near_share"] < b.min_near_share:
            return False
        if detail["swings"] < b.min_swings:
            return False
        return detail["buy_ratio"] >= b.min_buy_ratio

    def run_symbol(self, symbol: str, data: Dict[str, List[Candle]]) -> List[SimTrade]:
        b = self.cfg.breakout
        slow = data[self.params.slow_tf]
        lookback = (self.cfg.breakout_v2.lookback_candles if self.use_v2()
                    else b.lookback_candles)
        warmup = lookback + b.trend_slow_sma + b.trend_slope_candles + 5
        if len(slow) < warmup + 20:
            return []

        series = Series(slow, self.params.slow_tf)
        cooldown_bars = int(self.cfg.risk.symbol_cooldown_sec * 1000 / series.step_ms)
        history: List[float] = []          # фон монеты: обороты последних свечей
        level = None
        level_at = -10 ** 9
        trades: List[SimTrade] = []

        i = max(warmup, series.bars_24h)
        while i < len(slow) - 1:
            history.append(slow[i].turnover / 15.0)
            if len(history) > b.activity_history:
                history.pop(0)

            if not self._trend_ok(series, i):
                i += 1
                continue

            price = series.closes[i]
            if level is None or i - level_at >= self.params.level_recalc_every:
                level = self._level_for(slow[i + 1 - lookback: i + 1], price)
                level_at = i
            if level is None or not self._in_zone(price, level):
                i += 1
                continue
            if len(history) < b.activity_baseline_min:
                i += 1
                continue

            baseline = statistics.median(history)
            score, ratio, detail = proxy_activity(slow[i], level.price, baseline, b)
            if not self._entry_ok(score, ratio, detail):
                i += 1
                continue

            trade = self._trade(symbol, slow, series, i, level, score, ratio, detail, baseline)
            if trade is None:
                break
            trades.append(trade)
            exit_idx = max(i + 1, (trade.closed_ms - slow[0].ts) // series.step_ms)
            i = int(exit_idx) + cooldown_bars + 1
            level = None

        return trades

    def _trade(self, symbol: str, slow: List[Candle], series: Series, i: int,
               level, score: float, ratio: float, detail: Dict[str, float],
               baseline: float) -> Optional[SimTrade]:
        b = self.cfg.breakout
        price = series.closes[i]
        state = self.open_position(
            "long", price, i, slow[i].ts, series.bar_min,
            b.stop_loss_usd, b.take_profit_usd,
            stop_price_limit=level.price * (1.0 - b.invalidation_pct))
        peak = score

        def on_bar(idx: int, candle: Candle, st: PosState) -> Optional[str]:
            nonlocal peak
            cur, _, _ = proxy_activity(candle, level.price, baseline, b)
            peak = max(peak, cur)
            # Главное правило ТС: импульс кончился - выходим, где бы ни была
            # цена. Здесь оно работает на приближённой активности.
            if peak > 0 and cur <= peak * b.activity_drop_ratio:
                return f"активность упала: {cur:.2f} от пика {peak:.2f} (оценка по свече)"
            if cur < b.min_hold_score:
                return f"активность иссякла: {cur:.2f} (оценка по свече)"
            age = st.age_min(idx)
            if st.best_pnl_usd <= 0 and age >= b.no_progress_minutes:
                return f"импульс не начался за {age:.0f} мин"
            if age >= b.max_hold_minutes:
                return f"истекло время удержания ({age:.0f} мин)"
            return None

        exit_idx, exit_price, reason = walk_position(slow, state, on_bar)
        if reason == "период закончился":
            return None
        notes = (f"уровень {level.describe()}, цена {level.distance_pct(price) * -100:+.2f}% | "
                 f"оценка активности {score:.2f} при фоне x{ratio:.1f}")
        return self.finish(symbol, state, slow, exit_idx, exit_price, reason,
                           timeframe=b.timeframe, entry_reason=notes, score=score)


class BreakoutV2Replay(BreakoutReplay):
    """Пробой 0.2: уровень строго на пробой, тренд строже, пороги выше."""

    def use_v2(self) -> bool:
        return True


# ---------------------------------------------------- ТС плотностей (нет данных)

class DensityReplay(Replay):
    """Плотности в стакане. Прогнать на истории нельзя - и не будем делать вид.

    Вся ТС состоит из стакана: найти крупную заявку, проверить её на спуфинг
    десятью снапшотами подряд, встать перед ней лимиткой, следить, съедают её
    или снимают. Ни одного из этих чисел в исторических данных не существует -
    биржа отдаёт только свечи. Любая «замена» уровнем по свечам была бы уже
    другой торговой системой, и её результат ничего не сказал бы об этой.
    """

    fidelity = FIDELITY_NONE
    caveats = [
        "ТС целиком построена на стакане: крупная заявка, её живучесть, "
        "момент, когда её съели. Исторического стакана Bybit не отдаёт, "
        "поэтому прогнать эту ТС на истории нельзя.",
        "Проверить её можно только вперёд - по журналу живого демо-бота.",
    ]

    def symbols(self, universe: List[Ticker]) -> List[str]:
        return []

    async def load(self, hist, symbol, start, end):
        return {}

    def run_symbol(self, symbol: str, data: Dict[str, List[Candle]]) -> List[SimTrade]:
        return []


REPLAYS: Dict[str, Dict[str, type]] = {
    "impulse": {"0.1": ImpulseReplay},
    "density": {"0.1": DensityReplay, "0.2": DensityReplay},
    "breakout": {"0.1": BreakoutReplay, "0.2": BreakoutV2Replay},
    "btc": {"0.1": BtcReplay},
}


def make_replay(cfg: Config, params: BacktestParams) -> Replay:
    """Реплей под ТС и версию из конфига. Незнакомая версия - берём базовую."""
    by_version = REPLAYS.get(cfg.strategy)
    if not by_version:
        raise SystemExit(f"Бэктест не умеет ТС {cfg.strategy}")
    cls = by_version.get(cfg.version) or by_version[sorted(by_version)[0]]
    return cls(cfg, params)


# ------------------------------------------------------------------ портфель

def apply_portfolio_limits(trades: List[SimTrade], max_open: int) -> List[SimTrade]:
    """Отбрасывает сделки, которые бот не открыл бы: лимит одновременных позиций.

    Прогон идёт по монетам независимо, и там уже учтены правила ОДНОЙ монеты
    (кулдаун, одна позиция на монету). Общее для всех монет ограничение одно -
    сколько сделок бот держит одновременно, - и накладывается оно здесь, по
    времени: сделка берётся, если в момент её открытия свободен слот.
    """
    trades.sort(key=lambda t: t.opened_ms)
    kept: List[SimTrade] = []
    open_until: List[int] = []          # времена закрытия занятых слотов
    for trade in trades:
        open_until = [ts for ts in open_until if ts > trade.opened_ms]
        if len(open_until) >= max_open:
            continue
        kept.append(trade)
        open_until.append(trade.closed_ms)
    return kept


# ------------------------------------------------------------------ месяцы

def month_series(start_ms: int, end_ms: int) -> List[str]:
    """Список месяцев периода по возрастанию: ['2026-05', ...]."""
    out: List[str] = []
    cur = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    last = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    while cur <= last:
        out.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def btc_by_month(candles: List[Candle]) -> Dict[str, Dict]:
    """Движение биткоина по месяцам: открытие, закрытие, размах, изменение."""
    buckets: Dict[str, List[Candle]] = {}
    for c in candles:
        buckets.setdefault(month_key(c.ts), []).append(c)
    out: Dict[str, Dict] = {}
    for month, chunk in buckets.items():
        first, last = chunk[0].open, chunk[-1].close
        out[month] = {
            "open": first,
            "close": last,
            "high": max(c.high for c in chunk),
            "low": min(c.low for c in chunk),
            "change_pct": round((last / first - 1.0) * 100, 2) if first > 0 else 0.0,
        }
    return out


def summarize(trades: Sequence[SimTrade]) -> Dict:
    """Сводка по набору сделок - те же метрики, что панель считает по журналу."""
    pnls = [t.net_pnl_usd for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        "net_pnl_usd": round(sum(pnls), 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "best_usd": round(max(pnls), 2) if pnls else 0.0,
        "worst_usd": round(min(pnls), 2) if pnls else 0.0,
        # None вместо бесконечности: в JSON литерала Infinity нет, панель на
        # нём падает (та же причина, что и в PaperBroker.stats).
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "fees_usd": round(sum(t.fees_usd for t in trades), 2),
    }


def max_drawdown_usd(trades: Sequence[SimTrade]) -> float:
    """Максимальная просадка кривой результата в долларах."""
    peak = equity = 0.0
    worst = 0.0
    for t in sorted(trades, key=lambda x: x.closed_ms):
        equity += t.net_pnl_usd
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 2)


def build_report(cfg: Config, params: BacktestParams, replay: Replay,
                 trades: List[SimTrade], btc: Dict[str, Dict],
                 start_ms: int, end_ms: int, symbols: List[str],
                 elapsed_sec: float) -> Dict:
    by_month: Dict[str, List[SimTrade]] = {}
    for t in trades:
        by_month.setdefault(month_key(t.opened_ms), []).append(t)

    months: List[Dict] = []
    equity = 0.0
    for month in month_series(start_ms, end_ms):
        chunk = by_month.get(month, [])
        stats = summarize(chunk)
        equity += stats["net_pnl_usd"]
        months.append({
            "month": month,
            **stats,
            "equity_usd": round(equity, 2),
            "max_drawdown_usd": max_drawdown_usd(chunk),
            "symbols": len({t.symbol for t in chunk}),
            "btc": btc.get(month),
        })

    return {
        "generated": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "strategy": cfg.strategy,
        "version": cfg.version,
        "fidelity": replay.fidelity,
        "caveats": list(replay.caveats),
        "params": {
            "months": params.months,
            "from": _fmt(start_ms),
            "to": _fmt(end_ms),
            "symbols": len(symbols),
            "symbol_list": symbols[:200],
            "timeframes": [params.fast_tf, params.slow_tf],
            "margin_usd": cfg.risk.margin_usd,
            "leverage": cfg.risk.leverage,
            "max_open_positions": cfg.risk.max_open_positions,
            "taker_fee": cfg.risk.taker_fee,
            "elapsed_sec": round(elapsed_sec, 1),
        },
        "total": {**summarize(trades), "max_drawdown_usd": max_drawdown_usd(trades)},
        "months": months,
        # Сделки для раскрытия месяца в панели. Свежие важнее старых.
        "trades": [t.to_dict() for t in sorted(trades, key=lambda x: x.opened_ms,
                                               reverse=True)[:MAX_TRADES_IN_REPORT]],
        "trades_truncated": max(0, len(trades) - MAX_TRADES_IN_REPORT),
    }


# ------------------------------------------------------------------ прогон

async def pick_universe(client: BybitPublic, cfg: Config, params: BacktestParams,
                        start_ms: int) -> List[Ticker]:
    """Монеты, по которым гоняем историю.

    Условия: инструмент торгуется, котируется в USDT, существовал ДО начала
    периода (иначе первые месяцы у него пустые), не входит в исключения своей
    ТС и попадает в ЕЁ диапазон оборота.

    Диапазон берётся из конфига самой ТС, а не общий, и это важно: у ТС
    импульса верхняя граница 250 млн $ - монеты ликвиднее она не берёт
    сознательно. Если отбирать универсум просто «топ по обороту», в него
    попадут ровно те монеты, которые скринер отвергнет на каждой свече, и
    бэктест честно покажет ноль сделок, потратив часы на закачку.

    Верхняя граница расширена вдвое: оборот в тикере сегодняшний, а за
    прошедшие месяцы он у монеты гулял, и жёсткая отсечка по сегодняшнему дню
    выбросила бы монеты, которые тогда были в диапазоне. Нижнюю границу,
    наоборот, не опускаем - на неликвиде бэктест рисует неисполнимые сделки.

    Набор фиксирован на весь период - в этом отличие от живого сканера,
    который пересобирает список каждые несколько минут. Он и не может быть
    динамическим: оборот за прошлые месяцы у нас есть только по тем монетам,
    которые мы скачали.
    """
    instruments = await client.instruments()
    tickers = await client.tickers()

    excluded: set = set()
    min_turnover, max_turnover = params.min_turnover_24h, float("inf")
    if cfg.strategy == "impulse":
        excluded = {b.upper() for b in cfg.screener.excluded_bases}
        min_turnover = max(min_turnover, cfg.screener.min_turnover_24h)
        max_turnover = cfg.screener.max_turnover_24h * 2
    elif cfg.strategy == "breakout":
        excluded = {b.upper() for b in cfg.breakout.excluded_bases}
        min_turnover = max(min_turnover, cfg.breakout.min_turnover_24h)
        max_turnover = cfg.breakout.max_turnover_24h * 2

    out: List[Ticker] = []
    for t in tickers:
        info = instruments.get(t.symbol)
        if not info or info.get("status") != "Trading" or info.get("quote") != "USDT":
            continue
        if (info.get("base") or "").upper() in excluded:
            continue
        launch = info.get("launch_ms") or 0
        if not launch or launch > start_ms:
            continue
        if not (min_turnover <= t.turnover_24h <= max_turnover):
            continue
        out.append(t)

    out.sort(key=lambda t: t.turnover_24h, reverse=True)
    if params.max_symbols > 0:
        out = out[: params.max_symbols]
    log.info("Универсум %s: %d монет (оборот %.0f-%.0f млн $)", cfg.strategy, len(out),
             min_turnover / 1e6, max_turnover / 1e6)
    return out


async def run_backtest(cfg: Config, client: BybitPublic, params: BacktestParams,
                       cache_dir: str,
                       progress: Optional[Callable[[Dict], None]] = None) -> Dict:
    """Полный прогон: набор монет -> история -> сделки -> помесячный отчёт."""
    started = time.monotonic()
    start_ms, end_ms = params.window()
    hist = History(client, cache_dir)
    replay = make_replay(cfg, params)

    def report_progress(**kw) -> None:
        if progress is not None:
            progress(dict(kw, requests=hist.requests))

    # Движение биткоина по месяцам нужно всегда - даже там, где сделок нет.
    report_progress(stage="btc", done=0, total=0, note="качаю дневные свечи BTC")
    btc_daily = await hist.load("BTCUSDT", "D", start_ms, end_ms)
    btc = btc_by_month(btc_daily)

    universe = await pick_universe(client, cfg, params, start_ms) if replay.fidelity != FIDELITY_NONE else []
    symbols = replay.symbols(universe)

    trades: List[SimTrade] = []
    for done, symbol in enumerate(symbols, start=1):
        report_progress(stage="symbols", done=done, total=len(symbols), note=symbol)
        try:
            data = await replay.load(hist, symbol, start_ms, end_ms)
            # Разбор одной монеты - это секунды сплошного счёта. Внутри пода он
            # идёт в том же процессе, что и торговля, поэтому уходит в поток:
            # GIL никуда не девается, но event loop получает управление между
            # байткодами и живой бот не замирает на всё время прогона.
            found = await asyncio.to_thread(replay.run_symbol, symbol, data)
        except Exception as exc:  # noqa: BLE001 - одна монета не валит прогон
            log.warning("[%s] пропускаю: %s", symbol, exc)
            continue
        # Сделки, начавшиеся до начала периода (на разогреве), не наши.
        trades.extend(t for t in found if t.opened_ms >= start_ms)
        if done % 20 == 0:
            log.info("Прогон: %d/%d монет, сделок пока %d, запросов %d",
                     done, len(symbols), len(trades), hist.requests)

    trades = apply_portfolio_limits(trades, cfg.risk.max_open_positions)
    if symbols:
        removed = hist.keep_only(symbols + ["BTCUSDT"])
        if removed:
            log.info("Кэш истории: удалено %d файлов выбывших монет", removed)

    report = build_report(cfg, params, replay, trades, btc, start_ms, end_ms,
                          symbols, time.monotonic() - started)
    report["cache_bytes"] = hist.size_bytes()
    report["requests"] = hist.requests
    report_progress(stage="done", done=len(symbols), total=len(symbols),
                    note=f"{len(trades)} сделок")
    log.info("Бэктест готов: %d сделок, итог %+.2f$, %d запросов, %.0f с",
             report["total"]["trades"], report["total"]["net_pnl_usd"],
             hist.requests, time.monotonic() - started)
    return report
