"""Демо-исполнение: бот "как будто" открывает и закрывает шорты.

Реальные ордера не отправляются никуда - только запись в журнал. Цены, свечи и
стакан при этом настоящие, поэтому сделка получается честной симуляцией:
вход по текущей цене, стоп/тейк по правилам ТС, комиссия тейкера учтена.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from .config import Config
from .models import ClosedTrade, OrderbookView, PendingOrder, PlainSetup, Position, Setup

log = logging.getLogger("paper")


class PaperBroker:
    """Держит демо-позиции и решает, когда их закрывать."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.positions: Dict[str, Position] = {}     # symbol -> позиция
        self.closed: List[ClosedTrade] = []
        self.cooldown: Dict[str, float] = {}         # symbol -> время, до которого не входим
        self.equity_usd: float = 0.0                 # накопленный демо-результат

    # ------------------------------------------------------------------ вход

    def can_open(self, symbol: str) -> tuple:
        if symbol in self.positions:
            return False, "позиция уже открыта"
        if len(self.positions) >= self.cfg.risk.max_open_positions:
            return False, "лимит одновременных позиций"
        until = self.cooldown.get(symbol, 0.0)
        if time.time() < until:
            return False, f"кулдаун ещё {int(until - time.time())}с"
        return True, ""

    def open_short(self, setup: Setup) -> Position:
        risk = self.cfg.risk
        price = setup.price
        notional = risk.notional_usd
        qty = notional / price

        # Комиссию тейкера за вход и выход закладываем в уровни, чтобы
        # чистый убыток по стопу был ровно stop_loss_usd, а прибыль по тейку - take_profit_usd.
        round_trip_fee = notional * risk.taker_fee * 2
        stop_move = (risk.stop_loss_usd - round_trip_fee) / qty
        take_move = (risk.take_profit_usd + round_trip_fee) / qty

        position = Position(
            trade_id=Position.new_id(),
            symbol=setup.symbol,
            side="short",
            timeframe=setup.primary_tf,
            qty=qty,
            entry_price=price,
            stop_price=price + stop_move,
            take_price=price - take_move,
            opened_at=time.time(),
            margin_usd=risk.margin_usd,
            leverage=risk.leverage,
            notional_usd=notional,
            entry_score=setup.score,
            entry_reason=" | ".join(setup.notes),
            setup_snapshot={
                "change_24h": setup.ticker.change_24h,
                "turnover_24h": setup.ticker.turnover_24h,
                "impulses": {
                    tf: {"found": i.found, "gain": i.gain, "candles": i.candles, "score": i.score}
                    for tf, i in setup.impulses.items()
                },
                "stall": setup.stall.conditions,
                "book": setup.book.summary(),
            },
            last_price=price,
            best_price=price,
            entry_fee_rate=risk.taker_fee,
            exit_fee_rate=risk.taker_fee,
        )
        self.positions[setup.symbol] = position
        log.info(
            "[ДЕМО] ШОРТ %s @ %.8g | qty %.6g | стоп %.8g | тейк %.8g | ТФ %sm | скор %.2f",
            position.symbol, price, qty, position.stop_price, position.take_price,
            position.timeframe, setup.score,
        )
        for note in setup.notes:
            log.info("    %s", note)
        return position

    def open_from_limit(self, order: "PendingOrder") -> Position:
        """Исполнение лимитки ТС плотностей: вход строго по её цене.

        Проскальзывания на входе нет по построению - лимитка либо исполняется
        по своей цене, либо не исполняется вовсе. Комиссия входа мейкерская.
        """
        risk = self.cfg.risk
        dcfg = self.cfg.density
        setup = order.setup
        price = order.price
        qty = order.qty
        notional = qty * price

        # Уровни считаем от целей в деньгах, с уже учтённой комиссией круга
        # (мейкер на входе + тейкер на выходе), чтобы +5$ были чистыми.
        round_trip_fee = notional * (dcfg.maker_fee + risk.taker_fee)
        take_move = (dcfg.take_profit_usd + round_trip_fee) / qty
        money_stop_move = (dcfg.stop_loss_usd - round_trip_fee) / qty
        # Второй стоп - за самой плотностью: если её прошли насквозь, идея сделки
        # умерла раньше, чем набежал денежный убыток. Берём тот, что ближе.
        wall_stop_move = abs(price - setup.wall.price) + price * dcfg.stop_beyond_wall_pct
        stop_move = min(money_stop_move, wall_stop_move)

        if setup.side == "long":
            stop_price, take_price = price - stop_move, price + take_move
        else:
            stop_price, take_price = price + stop_move, price - take_move

        position = Position(
            trade_id=Position.new_id(),
            symbol=setup.symbol,
            side=setup.side,
            timeframe="book",          # сделка от стакана, таймфрейма у неё нет
            qty=qty,
            entry_price=price,
            stop_price=stop_price,
            take_price=take_price,
            opened_at=time.time(),
            margin_usd=risk.margin_usd,
            leverage=risk.leverage,
            notional_usd=notional,
            entry_score=setup.score,
            entry_reason=" | ".join(setup.notes),
            setup_snapshot={
                "wall": setup.wall.describe(),
                "dominance": round(setup.dominance, 2),
                "turnover_24h": setup.ticker.turnover_24h,
                "waited_sec": round(order.age_sec(), 1),
            },
            entry_fee_rate=dcfg.maker_fee,
            exit_fee_rate=risk.taker_fee,
            last_price=price,
            best_price=price,
            wall_side=setup.wall.side,
            wall_price=setup.wall.price,
            wall_notional=setup.wall.notional,
        )
        self.positions[setup.symbol] = position
        log.info(
            "[ДЕМО] %s %s @ %.8g по лимитке | qty %.6g | стоп %.8g | тейк %.8g | "
            "плотность %s | ждали %.0fс",
            "ЛОНГ" if setup.side == "long" else "ШОРТ", position.symbol, price, qty,
            stop_price, take_price, setup.wall.describe(), order.age_sec(),
        )
        return position

    def open_market(self, setup: "PlainSetup", stop_usd: float, take_usd: float,
                    stop_price_limit: Optional[float] = None) -> Position:
        """Рыночный вход для ТС пробоя и биткоина.

        Уровни считаются от целей в деньгах с уже заложенной комиссией круга,
        поэтому стоп и тейк дают ровно заявленные суммы чистыми.

        stop_price_limit - технический стоп самой ТС (возврат под уровень).
        Берётся тот из двух, который сработает раньше: смысл денежного стопа
        в том, чтобы ограничить убыток, а не в том, чтобы пересиживать
        сломанную идею сделки.
        """
        risk = self.cfg.risk
        price = setup.price
        notional = risk.notional_usd
        qty = notional / price

        round_trip_fee = notional * risk.taker_fee * 2
        take_move = (take_usd + round_trip_fee) / qty
        stop_move = (stop_usd - round_trip_fee) / qty

        if setup.side == "long":
            stop_price = price - stop_move
            take_price = price + take_move
            if stop_price_limit is not None:
                stop_price = max(stop_price, stop_price_limit)
        else:
            stop_price = price + stop_move
            take_price = price - take_move
            if stop_price_limit is not None:
                stop_price = min(stop_price, stop_price_limit)

        position = Position(
            trade_id=Position.new_id(),
            symbol=setup.symbol,
            side=setup.side,
            timeframe=setup.timeframe,
            qty=qty,
            entry_price=price,
            stop_price=stop_price,
            take_price=take_price,
            opened_at=time.time(),
            margin_usd=risk.margin_usd,
            leverage=risk.leverage,
            notional_usd=notional,
            entry_score=setup.score,
            entry_reason=" | ".join(setup.notes),
            setup_snapshot=dict(setup.extra, book=setup.book),
            entry_fee_rate=risk.taker_fee,
            exit_fee_rate=risk.taker_fee,
            last_price=price,
            best_price=price,
        )
        self.positions[setup.symbol] = position
        log.info(
            "[ДЕМО] %s %s @ %.8g | qty %.6g | стоп %.8g | тейк %.8g | скор %.2f",
            "ЛОНГ" if setup.side == "long" else "ШОРТ", position.symbol, price, qty,
            stop_price, take_price, setup.score,
        )
        for note in setup.notes:
            log.info("    %s", note)
        return position

    # ------------------------------------------------------------------ учёт

    def _fees(self, position: Position, exit_price: float) -> float:
        # Ставки хранятся в самой позиции: у ТС импульса вход по рынку (тейкер),
        # у ТС плотностей - лимиткой (мейкер). Выход в обеих по рынку.
        return position.qty * (position.entry_price * position.entry_fee_rate
                               + exit_price * position.exit_fee_rate)

    def track(self, position: Position, price: float) -> float:
        """Обновляет рантайм-метрики позиции и возвращает текущий PnL."""
        position.last_price = price
        pnl = self.net_pnl(position, price)
        better = price > position.best_price if position.is_long else price < position.best_price
        if better or position.best_price <= 0:
            position.best_price = price
        position.best_pnl_usd = max(position.best_pnl_usd, pnl)
        position.worst_pnl_usd = min(position.worst_pnl_usd, pnl)
        return pnl

    def net_pnl(self, position: Position, price: float) -> float:
        return position.gross_pnl(price) - self._fees(position, price)

    # ------------------------------------------------------------------ выход

    def evaluate(self, position: Position, price: float, book: Optional[OrderbookView]) -> Optional[str]:
        """Возвращает причину закрытия или None, если держим дальше."""
        xcfg = self.cfg.exit
        tf = position.timeframe

        position.last_price = price
        pnl = self.net_pnl(position, price)
        if price < position.best_price:
            position.best_price = price
        position.best_pnl_usd = max(position.best_pnl_usd, pnl)
        position.worst_pnl_usd = min(position.worst_pnl_usd, pnl)

        move_down = (position.entry_price - price) / position.entry_price
        if move_down >= xcfg.no_reaction_move:
            position.reacted = True

        # 1. Стоп-лосс (шорт - страдаем от роста цены).
        if price >= position.stop_price:
            return "стоп-лосс"

        # 2. Тейк-профит.
        if price <= position.take_price:
            return "тейк-профит"

        # 3. Безубыток после хорошего хода вниз.
        if not position.breakeven_armed and position.best_pnl_usd >= xcfg.breakeven_at_usd:
            position.breakeven_armed = True
            fee_cushion = self._fees(position, position.entry_price) / position.qty
            position.stop_price = min(position.stop_price, position.entry_price - fee_cushion)
            log.info("[%s] стоп подтянут в безубыток: %.8g", position.symbol, position.stop_price)

        # 4. "Прогноз не сбылся": цена сходила вниз, но откатывает обратно.
        #    Чем старше ТФ, тем на более долгое падение мы рассчитываем, поэтому
        #    терпение к откатам задано в конфиге отдельно по каждому ТФ.
        if position.best_pnl_usd >= xcfg.min_progress_usd:
            giveback_level = position.best_pnl_usd * (1.0 - xcfg.giveback_ratio)
            if pnl <= giveback_level:
                return (f"падение не состоялось: откат с {position.best_pnl_usd:.2f}$ "
                        f"до {pnl:.2f}$")

        age_min = position.age_sec() / 60.0

        # 5. Нет реакции вообще - цена не пошла вниз в отведённое для ТФ время.
        if not position.reacted and age_min >= xcfg.no_reaction_minutes.get(tf, 15):
            return f"нет реакции за {age_min:.0f} мин"

        # 6. Стакан: настоящая плотность на покупку прямо под ценой - падению конец.
        if book is not None and book.genuine_bid_wall is not None and pnl > 0:
            wall = book.genuine_bid_wall
            if -self.cfg.orderbook.bid_wall_block_pct <= wall.distance_pct < 0:
                return f"плотность на покупку снизу ({wall.notional:,.0f}$), фиксируем"

        # 7. Время вышло.
        if age_min >= xcfg.max_hold_minutes.get(tf, 60):
            return f"истекло время удержания ({age_min:.0f} мин)"

        return None

    def close(self, position: Position, price: float, reason: str) -> ClosedTrade:
        gross = position.gross_pnl(price)
        fees = self._fees(position, price)
        net = gross - fees
        closed_at = time.time()

        trade = ClosedTrade(
            trade_id=position.trade_id,
            symbol=position.symbol,
            side=position.side,
            timeframe=position.timeframe,
            qty=position.qty,
            entry_price=position.entry_price,
            exit_price=price,
            opened_at=position.opened_at,
            closed_at=closed_at,
            stop_price=position.stop_price,
            take_price=position.take_price,
            margin_usd=position.margin_usd,
            leverage=position.leverage,
            notional_usd=position.notional_usd,
            gross_pnl_usd=gross,
            fees_usd=fees,
            net_pnl_usd=net,
            pnl_pct_on_margin=net / position.margin_usd * 100 if position.margin_usd else 0.0,
            exit_reason=reason,
            entry_reason=position.entry_reason,
            entry_score=position.entry_score,
            max_profit_usd=position.best_pnl_usd,
            max_loss_usd=position.worst_pnl_usd,
            duration_sec=closed_at - position.opened_at,
        )

        self.positions.pop(position.symbol, None)
        self.closed.append(trade)
        self.equity_usd += net
        self.cooldown[position.symbol] = closed_at + self.cfg.risk.symbol_cooldown_sec

        log.info(
            "[ДЕМО] ЗАКРЫТ %s @ %.8g | %s | PnL %+.2f$ (%+.1f%% к марже) | итог %+.2f$",
            trade.symbol, price, reason, net, trade.pnl_pct_on_margin, self.equity_usd,
        )
        return trade

    # ------------------------------------------------------------------ отчёт

    def stats(self) -> Dict:
        total = len(self.closed)
        wins = [t for t in self.closed if t.net_pnl_usd > 0]
        losses = [t for t in self.closed if t.net_pnl_usd <= 0]
        gross_win = sum(t.net_pnl_usd for t in wins)
        gross_loss = -sum(t.net_pnl_usd for t in losses)
        return {
            "trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "winrate": (len(wins) / total * 100) if total else 0.0,
            "net_pnl_usd": self.equity_usd,
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win else 0.0,
            "open": len(self.positions),
        }
