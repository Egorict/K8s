"""ТС №2, версия 0.2: те же плотности, но сделка в противоположную сторону.

Версия 0.1 (bot/density.py) продолжает работать без изменений. Здесь всё то же
самое - отбор монет, поиск стены, все фильтры, цена срабатывания ордера,
расстояния стопа и тейка, правила выхода, - кроме направления сделки:

    0.1: bid-стена -> ЛОНГ,  ask-стена -> ШОРТ
    0.2: bid-стена -> ШОРТ,  ask-стена -> ЛОНГ

Смысл. Версия 0.1 ставит на то, что плотность УДЕРЖИТ цену: встаём перед
стеной и едем от неё. Версия 0.2 - ставка на обратное: что стену продавят или
выкупят, и цена пойдёт сквозь неё. Это самая прямая проверка гипотезы «а что
если делать наоборот»: обе версии видят одни и те же стены в одни и те же
секунды, поэтому разница в результате - это разница именно направления, а не
удачи с отбором монет.

Две вещи, которые пришлось изменить помимо стороны, - иначе версия была бы
не зеркальной, а сломанной:

1. **Тип ордера.** У 0.1 сторона следует из геометрии: bid-стена лежит НИЖЕ
   цены, и перед ней можно поставить только лимитку на покупку; ask-стена
   ВЫШЕ - только на продажу. Если просто инвертировать сторону, получится
   продажа по цене ниже рынка - в реальности такой ордер исполнился бы
   мгновенно по текущей цене, а не дождался бы подхода к стене. Поэтому здесь
   ордер стоповый: он срабатывает по той же цене и по тому же событию
   (цена дошла до стены), но открывает противоположную позицию.

2. **Комиссия входа.** Стоповый ордер исполняется по рынку, то есть тейкером.
   Оставить мейкерскую ставку значило бы приукрасить статистику 0.2 и сделать
   сравнение версий нечестным - а ради сравнения они и живут рядом.

Выходы не трогаем сознательно: «плотность съедена на 80%» и «плотность снята»
закрывают сделку в обеих версиях одинаково.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from .config import Config
from .density import DensityEngine
from .models import OrderbookView, PendingOrder, Ticker, WallSetup

log = logging.getLogger("density")

# Инверсия стороны. Держим одной таблицей, чтобы правило было видно целиком.
FLIP = {"long": "short", "short": "long"}


def find_wall_setup_inverted(ticker: Ticker, view: OrderbookView,
                             cfg: Config) -> Optional[WallSetup]:
    """Тот же сетап, что у 0.1, но сторона сделки зеркальная.

    Логика отбора стены повторена здесь целиком, а не вызвана из 0.1: в 0.1
    сторона участвует в проверке «ордер остаётся отложенным», и эту проверку
    тоже нужно зеркалить. Разница между версиями видна построчно.
    """
    dcfg = cfg.density
    if view.mid <= 0 or view.best_bid <= 0 or view.best_ask <= 0:
        return None

    spread = (view.best_ask - view.best_bid) / view.mid
    if spread > dcfg.max_spread_pct:
        return None

    # Стены и дисбаланс - как в 0.1. Меняется только сторона сделки.
    candidates: List[tuple] = []
    if view.genuine_bid_wall is not None:
        candidates.append((view.genuine_bid_wall, view.imbalance, FLIP["long"]))
    if view.genuine_ask_wall is not None:
        candidates.append((view.genuine_ask_wall,
                           1.0 / view.imbalance if view.imbalance else 0.0, FLIP["short"]))

    best: Optional[WallSetup] = None
    for wall, dominance, side in candidates:
        if wall.notional < dcfg.min_wall_notional:
            continue
        if abs(wall.distance_pct) > dcfg.max_wall_distance_pct:
            continue
        if wall.persisted < dcfg.min_persist_snapshots:
            continue
        if dominance < dcfg.min_dominance:
            continue

        # Цена срабатывания - ровно та же, что у 0.1: перед стеной.
        offset = wall.price * dcfg.entry_offset_pct
        entry = wall.price + offset if wall.side == "bid" else wall.price - offset

        # Ордер обязан остаться отложенным: цена ещё не должна была дойти до
        # уровня. Проверяем по стороне СТЕНЫ, а не сделки - в 0.1 они совпадали,
        # здесь они противоположны.
        if wall.side == "bid" and entry >= view.best_ask:
            continue
        if wall.side == "ask" and entry <= view.best_bid:
            continue

        setup = WallSetup(
            symbol=ticker.symbol,
            side=side,
            wall=wall,
            entry_price=entry,
            mid=view.mid,
            dominance=dominance,
            ticker=ticker,
            notes=[
                f"плотность {wall.describe()}",
                f"дисбаланс зоны x{dominance:.1f}, спред {spread * 100:.3f}%",
                f"версия 0.2: ставим на ПРОБОЙ плотности, "
                f"{'продажа' if side == 'short' else 'покупка'} от {entry:.8g}",
            ],
        )
        if best is None or setup.score > best.score:
            best = setup
    return best


class DensityV2Engine(DensityEngine):
    """Плотности 0.2. Отличается только тремя точками расширения."""

    def find_setup(self, ticker: Ticker, view: OrderbookView) -> Optional[WallSetup]:
        return find_wall_setup_inverted(ticker, view, self.cfg)

    def entry_fee_rate(self) -> float:
        # Стоповый ордер исполняется по рынку - комиссия тейкерская.
        return self.cfg.risk.taker_fee

    def order_kind(self) -> str:
        return "стоп-ордер"

    def _filled(self, order: PendingOrder, view: OrderbookView) -> bool:
        """Ждём ровно того же события, что и 0.1: цена дошла до стены.

        У 0.1 это выражалось через сторону сделки, потому что она совпадала со
        стороной стены. Здесь стороны противоположны, поэтому смотрим на стену:
        к bid-стене цена спускается, к ask-стене поднимается.
        """
        if order.setup.wall.side == "bid":
            return view.best_ask > 0 and view.best_ask <= order.price
        return view.best_bid > 0 and view.best_bid >= order.price

    async def run(self) -> None:
        log.info("Версия 0.2: те же плотности, сделка в ОБРАТНУЮ сторону "
                 "(bid-стена -> шорт, ask-стена -> лонг), вход стоп-ордером по рынку")
        await super().run()
