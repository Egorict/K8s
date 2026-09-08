"""ТС №3, версия 0.2: пробой уровня — строго уровня НА ПРОБОЙ.

Версия 0.1 (bot/breakout.py) продолжает работать без изменений; здесь
переопределены только отбор монет, выбор уровня и условия входа. Циклы,
замер активности и правило выхода («упала активность — выходим») общие.

Что не так было в 0.1 и что исправлено:

  1. **Уровень мог оказаться позади цены.** `pick_level` брала ближайший
     уровень ПО МОДУЛЮ расстояния, поэтому годился и уровень ниже цены —
     давно пройденный. Пробивать там нечего.
     Здесь уровень обязан быть впереди: цена подходит к нему снизу, а за
     последние `ahead_lookback` свечей держалась преимущественно ПОД ним.

  2. **Зона входа была симметричной** (±0.4%), то есть вход разрешался и
     когда цена уже ушла выше уровня. Здесь зона асимметрична: снизу шире
     (ждём подхода к уровню), сверху узко (берём только свежее пересечение).

  3. **Тренд проверялся мягко** — рост от 3% за сутки и наклон средней от
     0.5%. Здесь пороги выше, и добавлено требование, чтобы тренд держался:
     цена должна быть выше медленной средней большую часть последних свечей,
     а не заскочить туда одной свечой.

  4. **Активность мерялась только у уровня.** Монета могла быть вялой в
     целом, лишь бы у уровня случился всплеск. Здесь есть отдельный порог на
     активность самой монеты.

  5. **Касаний нужно три**, а не два, и отбои от уровня заметнее.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from .activity import ActivitySnapshot
from .breakout import BreakoutEngine, TrendScanner
from .config import Config
from .exchange import BybitPublic
from .indicators import safe_mean, scale
from .levels import HorizontalLevel, find_levels
from .models import Candle, PlainSetup, Ticker

log = logging.getLogger("breakout")


# --------------------------------------------------------------------- отбор

class TrendScannerV2(TrendScanner):
    """Тренд строже: пороги выше и он обязан держаться, а не мигнуть."""

    def __init__(self, client: BybitPublic, cfg: Config):
        super().__init__(client, cfg)
        self.v2 = cfg.breakout_v2

    def _tradable(self, ticker: Ticker, info: Optional[Dict] = None) -> bool:  # type: ignore[override]
        if not super()._tradable(ticker, info):
            return False
        # Свой порог роста за сутки: 3% в 0.1 пропускали почти любой боковик.
        return ticker.change_24h >= self.v2.min_change_24h

    def _uptrend(self, candles: List[Candle]) -> Tuple[bool, str]:
        ok, why = super()._uptrend(candles)
        if not ok:
            return False, why

        c, v2 = self.cfg, self.v2
        closes = [k.close for k in candles]
        slow = safe_mean(closes[-c.trend_slow_sma:])
        slow_before = safe_mean(
            closes[-(c.trend_slow_sma + c.trend_slope_candles): -c.trend_slope_candles])
        slope = (slow - slow_before) / slow_before if slow_before > 0 else 0.0
        if slope < v2.min_trend_slope:
            return False, f"наклон средней мал для 0.2 ({slope * 100:+.2f}%)"

        # Тренд должен держаться: цена выше медленной средней большую часть
        # последних свечей, а не заскочила туда одной последней.
        recent = closes[-v2.trend_hold_candles:]
        if len(recent) < v2.trend_hold_candles:
            return False, "мало свечей для проверки устойчивости тренда"
        above = sum(1 for price in recent if price > slow) / len(recent)
        if above < v2.min_above_slow_share:
            return False, f"тренд неустойчив: выше средней лишь {above * 100:.0f}% свечей"
        return True, f"тренд вверх, наклон {slope * 100:+.1f}%, держится {above * 100:.0f}%"


# --------------------------------------------------------------------- уровень

def level_ahead(candles: List[Candle], price: float, cfg: Config) -> Optional[HorizontalLevel]:
    """Уровень НА ПРОБОЙ: сопротивление впереди, к которому цена идёт снизу."""
    b, v2 = cfg.breakout, cfg.breakout_v2
    # Свой горизонт: у 0.1 он вчетверо короче, и на растущей монете впереди
    # не оставалось ни одного сопротивления - пробивать было нечего.
    if len(candles) < v2.lookback_candles // 3:
        return None
    window = candles[-v2.lookback_candles:]

    levels = find_levels(window, side="resistance",
                         tolerance=b.level_tolerance,
                         min_touches=v2.min_touches,
                         window=b.pivot_window,
                         min_spacing=b.min_touch_spacing,
                         min_rejection=v2.min_rejection_pct)
    if not levels:
        return None

    checked = window[-v2.ahead_lookback:]
    best: Optional[HorizontalLevel] = None
    for level in levels:
        distance = level.distance_pct(price)
        # Уровень впереди (выше цены) либо только что пройден. Уровень,
        # оставшийся заметно позади, к пробою отношения не имеет.
        if not (-v2.entry_above_pct <= distance <= v2.watch_distance_pct):
            continue
        # Цена должна была держаться ПОД уровнем: если она давно торгуется
        # выше, это уже не сопротивление.
        below = sum(1 for c in checked if c.close < level.price) / max(len(checked), 1)
        if below < v2.min_below_share:
            continue
        # Уровень должен быть свежим: отбивало недавно, а не в начале истории.
        if len(window) - 1 - level.last_touch_idx > v2.max_touch_age:
            continue
        if best is None or abs(distance) < abs(best.distance_pct(price)):
            best = level
    return best


def in_breakout_zone(price: float, level: HorizontalLevel, cfg: Config) -> bool:
    """Асимметричная зона: снизу ждём подхода, сверху — только свежий пробой."""
    v2 = cfg.breakout_v2
    distance = level.distance_pct(price)   # >0: уровень выше цены
    if distance >= 0:
        return distance <= v2.entry_below_pct
    return -distance <= v2.entry_above_pct


# --------------------------------------------------------------------- сетап

def build_setup_v2(ticker: Ticker, level: HorizontalLevel, snapshot: ActivitySnapshot,
                   ratio: float, cfg: Config) -> Optional[PlainSetup]:
    """Вход: уровень на пробой + активность и у уровня, и на самой монете."""
    b, v2 = cfg.breakout, cfg.breakout_v2
    price = ticker.last_price

    # Активность самой монеты, а не только всплеск у уровня.
    if snapshot.volume_per_min < v2.min_symbol_volume_per_min:
        return None
    if snapshot.trades_per_min < v2.min_symbol_trades_per_min:
        return None

    if snapshot.score < v2.min_entry_score:
        return None
    if ratio < v2.min_activity_ratio:
        return None
    if snapshot.near_share < b.min_near_share:
        return None
    if snapshot.book_notional < b.min_book_notional:
        return None
    if snapshot.swings < b.min_swings:
        return None
    if snapshot.buy_ratio < b.min_buy_ratio:
        return None

    distance = level.distance_pct(price)
    where = "под уровнем" if distance >= 0 else "только что над уровнем"
    score = round(min(1.0, 0.6 * snapshot.score + 0.4 * scale(ratio, v2.min_activity_ratio, 4.0)), 3)

    return PlainSetup(
        symbol=ticker.symbol,
        side="long",
        price=price,
        timeframe=b.timeframe,
        score=score,
        ticker=ticker,
        book=snapshot.describe(),
        notes=[
            f"уровень на пробой {level.describe()}, цена {where} ({distance * 100:+.2f}%)",
            f"активность {snapshot.score:.2f} при фоне x{ratio:.1f}",
            snapshot.describe(),
        ],
        extra={"level": level.price, "touches": level.touches,
               "rejection": round(level.rejection, 5),
               "activity": snapshot.score, "activity_ratio": round(ratio, 2)},
    )


# --------------------------------------------------------------------- движок

class BreakoutV2Engine(BreakoutEngine):
    """Пробой 0.2. Отличия — в четырёх точках расширения, остальное от 0.1."""

    def make_scanner(self) -> TrendScanner:
        return TrendScannerV2(self.client, self.cfg)

    def candles_limit(self) -> int:
        return self.cfg.breakout_v2.lookback_candles + 5

    def pick_level_for(self, candles: List[Candle], price: float) -> Optional[HorizontalLevel]:
        return level_ahead(candles, price, self.cfg)

    def in_entry_zone(self, price: float, level: HorizontalLevel) -> bool:
        return in_breakout_zone(price, level, self.cfg)

    def make_setup(self, ticker: Ticker, level: HorizontalLevel,
                   snapshot: ActivitySnapshot, ratio: float) -> Optional[PlainSetup]:
        return build_setup_v2(ticker, level, snapshot, ratio, self.cfg)

    async def run(self) -> None:
        v2 = self.cfg.breakout_v2
        log.info("Версия 0.2: уровень только НА ПРОБОЙ (цена под ним до %.2f%%, "
                 "над ним до %.2f%%), касаний от %d, тренд от %.0f%% за сутки",
                 v2.entry_below_pct * 100, v2.entry_above_pct * 100,
                 v2.min_touches, v2.min_change_24h * 100)
        await super().run()
