"""ТС №2: торговля от плотностей в стакане.

Смысл системы в одном абзаце. Крупная лимитная заявка (плотность) - это стена,
о которую цена тормозит: чтобы пройти уровень, рынку нужно выкупить или продать
весь её объём. Пока стена стоит, от неё отскакивают. Значит, можно встать
лимиткой ПЕРЕД стеной, поехать в её сторону и выйти ровно тогда, когда опора
исчезла - её съели или сняли.

Отличия от ТС импульса (bot/strategy.py):

  * монеты отбираются не по росту, а по активности: живой оборот, реальная
    волатильность, узкий спред. В тонком стакане "мусорной" монеты плотность
    пробивают первым же рыночным ордером, и вся идея не работает;
  * вход лимитный, а не рыночный: цена входа известна заранее, проскальзывания
    нет, комиссия мейкерская;
  * выход привязан не ко времени и не к откату, а к судьбе самой плотности.

Анти-спуфинг не переписывается заново: BookTracker уже ведёт историю по каждому
ценовому уровню и метит настоящие стены (genuine). Здесь пороги только строже -
на эту стену мы ставим деньги, а не подкручиваем ей скор.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Dict, List, Optional

from .config import Config
from .exchange import BybitPublic
from .indicators import atr, scale
from .journal import Journal
from .models import OrderbookView, PendingOrder, Ticker, Wall, WallSetup
from .orderbook import BookManager
from .paper import PaperBroker, fmt_profit_factor

log = logging.getLogger("density")


# --------------------------------------------------------------------- отбор

class DensityScanner:
    """Шаг 1: монеты, на которых плотность вообще что-то значит."""

    def __init__(self, client: BybitPublic, cfg: Config):
        self.client = client
        self.cfg = cfg.density
        self._instruments: Dict[str, Dict] = {}
        self._instruments_ts: float = 0.0

    async def _get_instruments(self) -> Dict[str, Dict]:
        if not self._instruments or time.time() - self._instruments_ts > 3600:
            self._instruments = await self.client.instruments()
            self._instruments_ts = time.time()
            log.info("Загружено инструментов: %d", len(self._instruments))
        return self._instruments

    def _tradable(self, ticker: Ticker, info: Optional[Dict]) -> bool:
        if not info or info.get("status") != "Trading" or info.get("quote") != "USDT":
            return False
        base = (info.get("base") or "").upper()
        if base in {b.upper() for b in self.cfg.excluded_bases}:
            return False
        return self.cfg.min_turnover_24h <= ticker.turnover_24h <= self.cfg.max_turnover_24h

    async def _volatility(self, symbol: str) -> Optional[float]:
        """ATR по 5-минуткам как доля цены: монета должна ходить."""
        candles = await self.client.klines(symbol, "5", limit=40)
        if len(candles) < 20:
            return None
        last = candles[-1].close
        if last <= 0:
            return None
        return atr(candles, period=14) / last

    async def scan(self) -> List[Ticker]:
        instruments = await self._get_instruments()
        tickers = await self.client.tickers()
        prefiltered = [t for t in tickers if self._tradable(t, instruments.get(t.symbol))]
        log.info("Прошли базовый фильтр: %d из %d", len(prefiltered), len(tickers))

        # Оборот - плохой одиночный критерий: у топовых монет он огромен, но и
        # плотности там разбирают мгновенно. Поэтому сначала берём разумный
        # запас по обороту, а ранжируем уже по волатильности.
        prefiltered.sort(key=lambda t: t.turnover_24h, reverse=True)
        scored: List[tuple] = []
        for t in prefiltered[: self.cfg.max_watchlist * 3]:
            try:
                vol = await self._volatility(t.symbol)
            except Exception as exc:  # noqa: BLE001
                log.debug("Нет свечей по %s: %s", t.symbol, exc)
                continue
            if vol is None or vol < self.cfg.min_atr_pct:
                continue
            # Волатильность важнее оборота: она даёт ход до цели, оборот лишь
            # подтверждает, что монета не заброшена.
            score = 0.7 * scale(vol, self.cfg.min_atr_pct, self.cfg.min_atr_pct * 4) + \
                0.3 * scale(t.turnover_24h, self.cfg.min_turnover_24h, self.cfg.max_turnover_24h)
            scored.append((score, vol, t))

        scored.sort(key=lambda row: row[0], reverse=True)
        top = [t for _, _, t in scored[: self.cfg.max_watchlist]]
        if top:
            log.info("Кандидаты (%d): %s", len(top),
                     ", ".join(f"{t.symbol} ATR {v * 100:.2f}%" for _, v, t in scored[:len(top)]))
        else:
            log.info("Активных монат под ТС плотностей сейчас нет")
        return top


# --------------------------------------------------------------------- сетап

def find_wall_setup(ticker: Ticker, view: OrderbookView, cfg: Config) -> Optional[WallSetup]:
    """Шаг 2-3: очевидный дисбаланс плотностей -> лимитка перед стеной.

    Возвращает None, если стены нет, она мелкая, далеко, подозрительная
    (не прошла анти-спуфинг) или обе стороны стакана сопоставимы - то есть
    никакого дисбаланса на самом деле нет.
    """
    dcfg = cfg.density
    if view.mid <= 0 or view.best_bid <= 0 or view.best_ask <= 0:
        return None

    # Широкий спред - монета неликвидна прямо сейчас; лимитка перед стеной
    # окажется в вакууме, а выход по рынку съест всю прибыль.
    spread = (view.best_ask - view.best_bid) / view.mid
    if spread > dcfg.max_spread_pct:
        return None

    # Дисбаланс зоны: во сколько раз одна сторона тяжелее другой.
    # imbalance = bid/ask, поэтому для bid-стены это он сам, для ask - обратный.
    candidates: List[tuple] = []
    if view.genuine_bid_wall is not None:
        candidates.append((view.genuine_bid_wall, view.imbalance, "long"))
    if view.genuine_ask_wall is not None:
        candidates.append((view.genuine_ask_wall, 1.0 / view.imbalance if view.imbalance else 0.0, "short"))

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

        # Встаём перед плотностью, а не в неё: на её цене мы будем последними
        # в очереди и, скорее всего, не исполнимся вовсе.
        offset = wall.price * dcfg.entry_offset_pct
        entry = wall.price + offset if side == "long" else wall.price - offset

        # Лимитка обязана остаться лимиткой: покупка ниже рынка, продажа выше.
        # Если цена уже прошла уровень, сетапа нет.
        if side == "long" and entry >= view.best_ask:
            continue
        if side == "short" and entry <= view.best_bid:
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
                f"лимитка {'на покупку' if side == 'long' else 'на продажу'} "
                f"перед уровнем: {entry:.8g}",
            ],
        )
        if best is None or setup.score > best.score:
            best = setup
    return best


# --------------------------------------------------------------------- движок

class DensityEngine:
    """Два цикла: обновление списка монет и работа со стаканом.

    Стакан здесь - и источник сетапов, и единственный судья по открытым
    сделкам, поэтому и вход, и выход живут в одном цикле: данные, на которых
    принимается решение, должны быть из одного снапшота.
    """

    def __init__(self, cfg: Config, client: BybitPublic):
        self.cfg = cfg
        self.client = client
        self.scanner = DensityScanner(client, cfg)
        # Пороги стакана строже общих: на эту стену мы ставим деньги.
        ocfg = dataclasses.replace(
            cfg.orderbook,
            density_multiplier=cfg.density.density_multiplier,
            min_persist_snapshots=cfg.density.min_persist_snapshots,
            shrink_tolerance=cfg.density.max_shrink,
            poll_interval_sec=cfg.density.poll_interval_sec,
        )
        self.books = BookManager(client, ocfg)
        self.broker = PaperBroker(cfg)
        self.journal = Journal(cfg.trades_csv, cfg.signals_csv, cfg.state_json)

        self.watchlist: Dict[str, Ticker] = {}
        self.pending: Dict[str, PendingOrder] = {}
        self._running = True

    # --- точки расширения для следующих версий ТС -------------------------
    #
    # Версия 0.2 (bot/density_v2.py) торгует те же стены в противоположную
    # сторону. Ей нужно подменить ровно три вещи: как строится сетап, когда
    # ордер считается исполненным и по какой комиссии открывается позиция.
    # Всё остальное - отбор монет, ведение и выходы - общее.

    def find_setup(self, ticker: Ticker, view: OrderbookView) -> Optional[WallSetup]:
        return find_wall_setup(ticker, view, self.cfg)

    def entry_fee_rate(self) -> float:
        """Комиссия входа: у 0.1 вход лимиткой, значит мейкерская."""
        return self.cfg.density.maker_fee

    def order_kind(self) -> str:
        """Как называть ордер в логах и журнале."""
        return "лимитка"

    # ------------------------------------------------------------------ вход

    def _place_order(self, setup: WallSetup) -> None:
        qty = self.cfg.risk.notional_usd / setup.entry_price
        self.pending[setup.symbol] = PendingOrder(
            symbol=setup.symbol, side=setup.side, price=setup.entry_price,
            qty=qty, placed_at=time.time(), setup=setup,
        )
        log.info("[%s] %s %s @ %.8g перед плотностью %s (скор %.2f)",
                 setup.symbol, self.order_kind(),
                 "BUY" if setup.side == "long" else "SELL",
                 setup.entry_price, setup.wall.describe(), setup.score)
        self.journal.log_wall_signal(setup, f"{self.order_kind()} выставлена")

    def _filled(self, order: PendingOrder, view: OrderbookView) -> bool:
        """Исполнилась ли лимитка.

        Покупка исполняется, когда рынок опустился до нашей цены (лучший ask
        дошёл до неё), продажа - когда поднялся. Это консервативнее, чем
        сравнивать с last price: там сделка могла пройти на другой стороне спреда.
        """
        if order.side == "long":
            return view.best_ask > 0 and view.best_ask <= order.price
        return view.best_bid > 0 and view.best_bid >= order.price

    def _wall_alive(self, symbol: str, wall: Wall) -> Dict[str, float]:
        return self.books.tracker(symbol).wall_status(wall.side, wall.price)

    async def _handle_pending(self, symbol: str, view: OrderbookView) -> None:
        order = self.pending.get(symbol)
        if order is None:
            return

        # Пока лимитка висит, плотность может исчезнуть - тогда входить незачем.
        status = self._wall_alive(symbol, order.setup.wall)
        if not status["present"] or status["eaten"] >= self.cfg.density.wall_eaten_ratio:
            log.info("[%s] снимаю %s: плотность %s", symbol, self.order_kind(),
                     "исчезла" if not status["present"] else
                     f"съедена на {status['eaten'] * 100:.0f}%")
            self.journal.log_wall_signal(
                order.setup, f"{self.order_kind()} снята: плотности больше нет")
            self.pending.pop(symbol, None)
            return

        if self._filled(order, view):
            self.pending.pop(symbol, None)
            allowed, why = self.broker.can_open(symbol)
            if not allowed:
                self.journal.log_wall_signal(order.setup, f"пропуск: {why}")
                return
            self.broker.open_from_limit(order, entry_fee_rate=self.entry_fee_rate())
            self.journal.log_wall_signal(order.setup, f"вход: {self.order_kind()} исполнена")
            return

        if order.age_sec() > self.cfg.density.order_ttl_sec:
            log.info("[%s] %s не исполнилась за %.0fс - снимаю",
                     symbol, self.order_kind(), order.age_sec())
            self.journal.log_wall_signal(order.setup, f"{self.order_kind()} снята по таймауту")
            self.pending.pop(symbol, None)

    # ------------------------------------------------------------------ выход

    def _exit_reason(self, position, view: OrderbookView, price: float) -> Optional[str]:
        """Главное правило ТС: сделка живёт ровно столько, сколько живёт стена."""
        dcfg = self.cfg.density

        # 1. Цель по деньгам.
        if position.is_long and price >= position.take_price:
            return f"тейк-профит +{dcfg.take_profit_usd:.0f}$"
        if not position.is_long and price <= position.take_price:
            return f"тейк-профит +{dcfg.take_profit_usd:.0f}$"

        # 2. Уровень пробит насквозь - идея сделки умерла.
        if position.is_long and price <= position.stop_price:
            return "стоп: цена ушла за плотность"
        if not position.is_long and price >= position.stop_price:
            return "стоп: цена ушла за плотность"

        # 3. Плотность съели или сняли. Это и есть штатный выход ТС: опоры
        #    больше нет, а значит и держать позицию не за чем.
        status = self._wall_alive(position.symbol, Wall(
            side=position.wall_side, price=position.wall_price, size=0.0,
            notional=position.wall_notional, distance_pct=0.0, persisted=0, genuine=True,
        ))
        if not status["present"]:
            return "плотность снята из стакана"
        if status["eaten"] >= dcfg.wall_eaten_ratio:
            return (f"плотность съедена на {status['eaten'] * 100:.0f}% "
                    f"({status['last_notional']:,.0f}$ из {status['max_notional']:,.0f}$)")

        # 4. Предохранитель по времени.
        age_min = position.age_sec() / 60.0
        if age_min >= dcfg.max_hold_minutes:
            return f"истекло время удержания ({age_min:.0f} мин)"
        return None

    async def _handle_position(self, symbol: str, view: OrderbookView) -> None:
        position = self.broker.positions.get(symbol)
        if position is None:
            return
        # Выходим по рынку: лонг продаёт в лучший бид, шорт покупает в лучший аск.
        price = view.best_bid if position.is_long else view.best_ask
        if price <= 0:
            return
        self.broker.track(position, price)
        reason = self._exit_reason(position, view, price)
        if reason:
            trade = self.broker.close(position, price, reason)
            self.journal.log_trade(trade)

    # ------------------------------------------------------------------ циклы

    async def scan_loop(self) -> None:
        while self._running:
            try:
                found = await self.scanner.scan()
                new = {t.symbol: t for t in found}
                # Монеты с открытой сделкой или висящей лимиткой не выбрасываем.
                for symbol in list(self.broker.positions) + list(self.pending):
                    if symbol not in new and symbol in self.watchlist:
                        new[symbol] = self.watchlist[symbol]
                self.watchlist = new
                self.books.keep_only(list(new))
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка сканирования: %s", exc)
            await asyncio.sleep(self.cfg.density.rescan_interval_sec)

    async def book_loop(self) -> None:
        while self._running:
            started = time.monotonic()
            for symbol, ticker in list(self.watchlist.items()):
                if not self._running:
                    break
                try:
                    view = await self.books.poll(symbol)
                    if view is None or not self.books.ready(symbol):
                        continue
                    if symbol in self.broker.positions:
                        await self._handle_position(symbol, view)
                        continue
                    if symbol in self.pending:
                        await self._handle_pending(symbol, view)
                        continue
                    allowed, _ = self.broker.can_open(symbol)
                    if not allowed:
                        continue
                    setup = self.find_setup(ticker, view)
                    if setup is not None:
                        self._place_order(setup)
                except Exception as exc:  # noqa: BLE001
                    log.debug("[%s] ошибка обработки стакана: %s", symbol, exc)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.5, self.cfg.density.poll_interval_sec - elapsed))

    async def report_loop(self, interval: float = 60.0) -> None:
        while self._running:
            await asyncio.sleep(interval)
            stats = self.broker.stats()
            self.journal.save_state(
                list(self.broker.positions.values()), stats, list(self.watchlist),
                strategy="density", pending=self._pending_view(),
                version=self.cfg.version,
            )
            log.info(
                "СТАТУС | наблюдаю %d монет | лимиток %d | сделок %d (W%d/L%d, winrate %.0f%%) "
                "| открыто %d | итог %+.2f$",
                len(self.watchlist), len(self.pending), stats["trades"], stats["wins"],
                stats["losses"], stats["winrate"], stats["open"], stats["net_pnl_usd"],
            )

    def _pending_view(self) -> List[Dict]:
        out: List[Dict] = []
        for order in self.pending.values():
            status = self._wall_alive(order.symbol, order.setup.wall)
            out.append({
                "symbol": order.symbol,
                "side": order.side,
                "price": order.price,
                "wall_price": order.setup.wall.price,
                "wall_notional": round(order.setup.wall.notional),
                "wall_eaten_pct": round(status["eaten"] * 100, 1),
                "age_sec": round(order.age_sec()),
            })
        return out

    # ------------------------------------------------------------------ запуск

    async def run(self) -> None:
        d = self.cfg.density
        log.info("Режим: %s | ТС плотностей | размер сделки %.0f$ x%.0f = %.0f$ | "
                 "тейк +%.0f$ | выход при съедании %.0f%% плотности",
                 self.cfg.mode.upper(), self.cfg.risk.margin_usd, self.cfg.risk.leverage,
                 self.cfg.risk.notional_usd, d.take_profit_usd, d.wall_eaten_ratio * 100)
        tasks = [
            asyncio.create_task(self.scan_loop(), name="scan"),
            asyncio.create_task(self.book_loop(), name="book"),
            asyncio.create_task(self.report_loop(), name="report"),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def shutdown_report(self) -> None:
        stats = self.broker.stats()
        self.journal.save_state(
            list(self.broker.positions.values()), stats, list(self.watchlist),
            strategy="density", pending=self._pending_view(),
            version=self.cfg.version,
        )
        log.info("=" * 70)
        log.info("ИТОГ ДЕМО-СЕССИИ (ТС плотностей)")
        log.info("  сделок: %d | прибыльных: %d | убыточных: %d | winrate: %.1f%%",
                 stats["trades"], stats["wins"], stats["losses"], stats["winrate"])
        log.info("  средняя прибыль: %+.2f$ | средний убыток: %+.2f$ | профит-фактор: %s",
                 stats["avg_win"], stats["avg_loss"], fmt_profit_factor(stats["profit_factor"]))
        log.info("  чистый результат: %+.2f$", stats["net_pnl_usd"])
        log.info("  журнал сделок: %s", self.cfg.trades_csv)
        log.info("=" * 70)
