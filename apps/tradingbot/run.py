"""Точка входа.

  python run.py              - запустить демо-торговлю (реальные данные, фейковые сделки)
  python run.py --web        - то же + веб-панель на http://localhost:8080
  python run.py report       - краткая сводка по data/trades.csv
  python run.py selftest     - проверить стратегию на синтетических свечах (без сети)

Ключи запуска (переопределяют config.py):
  --margin 20 --leverage 20 --stop 5 --take 15
  --min-change 0.10 --max-positions 3 --no-orderbook --debug
  --web --port 8080
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import signal
import sys

from bot.config import CONFIG, Config
from bot.engine import Engine
from bot.exchange import BybitPublic


def setup_logging(cfg: Config, debug: bool = False) -> None:
    os.makedirs(cfg.data_dir, exist_ok=True)
    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-9s %(message)s", "%H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    file_handler = logging.FileHandler(cfg.log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(stream)
    root.addHandler(file_handler)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    # На Windows консоль по умолчанию не в UTF-8 - иначе кириллица ломается.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def apply_args(cfg: Config, args: argparse.Namespace) -> None:
    if args.margin is not None:
        cfg.risk.margin_usd = args.margin
    if args.leverage is not None:
        cfg.risk.leverage = args.leverage
    if args.stop is not None:
        cfg.risk.stop_loss_usd = args.stop
    if args.take is not None:
        cfg.risk.take_profit_usd = args.take
    if args.min_change is not None:
        cfg.screener.min_change_24h = args.min_change
    if args.max_positions is not None:
        cfg.risk.max_open_positions = args.max_positions
    if args.no_orderbook:
        cfg.orderbook.enabled = False
    if args.web:
        cfg.web_enabled = True
    if args.port is not None:
        cfg.web_port = args.port
        cfg.web_enabled = True
    if args.data_dir:
        cfg.data_dir = args.data_dir
    if args.strategy:
        cfg.strategy = args.strategy


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """В Kubernetes под гасят через SIGTERM - отработаем его как Ctrl+C."""
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: обойдёмся KeyboardInterrupt


def _make_engine(cfg: Config, client: BybitPublic):
    """Одна ТС на процесс: у каждой свой журнал, и статистика не смешивается."""
    if cfg.strategy == "density":
        from bot.density import DensityEngine
        return DensityEngine(cfg, client)
    return Engine(cfg, client)


async def run_bot(cfg: Config) -> None:
    log = logging.getLogger("main")
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    runner = None
    if cfg.web_enabled:
        from bot.web import start_web
        runner = await start_web(cfg)

    async with BybitPublic(category=cfg.category) as client:
        engine = _make_engine(cfg, client)
        task = asyncio.create_task(engine.run(), name="engine")
        stopper = asyncio.create_task(stop.wait(), name="stop")
        try:
            await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            log.info("Останавливаюсь...")
            for t in (task, stopper):
                t.cancel()
            await asyncio.gather(task, stopper, return_exceptions=True)
            engine.shutdown_report()
            if runner is not None:
                await runner.cleanup()


def report(cfg: Config) -> None:
    path = cfg.trades_csv
    if not os.path.exists(path):
        print(f"Журнал пуст: {path} ещё не создан.")
        return
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    if not rows:
        print("Сделок пока нет.")
        return

    total = len(rows)
    pnls = [float(r["profit_usd"]) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    print(f"Файл: {path}")
    print(f"Сделок: {total} | прибыльных: {len(wins)} | убыточных: {len(losses)} "
          f"| winrate: {len(wins) / total * 100:.1f}%")
    print(f"Итог: {sum(pnls):+.2f}$ | лучшая: {max(pnls):+.2f}$ | худшая: {min(pnls):+.2f}$")
    print()
    print(f"{'#':>3} {'монета':<14} {'ТФ':>4} {'вход':<19} {'выход':<19} {'профит':>9}  причина")
    for r in rows[-25:]:
        print(f"{r['num']:>3} {r['symbol']:<14} {r['timeframe']:>4} {r['open_time']:<19} "
              f"{r['close_time']:<19} {float(r['profit_usd']):>+8.2f}$  {r['exit_reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Демо-бот: шорт истощения импульса на альткоин-фьючерсах")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "report", "selftest"])
    parser.add_argument("--margin", type=float, help="маржа на сделку, $ (по умолчанию 20)")
    parser.add_argument("--leverage", type=float, help="плечо (по умолчанию 20)")
    parser.add_argument("--stop", type=float, help="стоп-лосс в $ (по умолчанию 5)")
    parser.add_argument("--take", type=float, help="тейк-профит в $ (по умолчанию 15)")
    parser.add_argument("--min-change", type=float, help="минимальный рост за 24ч, доля (0.10)")
    parser.add_argument("--max-positions", type=int, help="сколько сделок держать одновременно")
    parser.add_argument("--no-orderbook", action="store_true", help="отключить анализ стакана")
    parser.add_argument("--web", action="store_true", help="поднять веб-панель с журналом торговли")
    parser.add_argument("--port", type=int, help="порт веб-панели (по умолчанию 8080)")
    parser.add_argument("--data-dir", help="каталог для журналов (по умолчанию data)")
    parser.add_argument("--strategy", choices=["impulse", "density"],
                        help="какую ТС запускать: impulse (шорт истощения импульса) "
                             "или density (торговля от плотностей в стакане)")
    parser.add_argument("--debug", action="store_true", help="подробные логи")
    args = parser.parse_args()

    cfg = CONFIG
    apply_args(cfg, args)
    setup_logging(cfg, args.debug)

    if args.command == "report":
        report(cfg)
        return
    if args.command == "selftest":
        from tests.selftest import run_selftest
        run_selftest(cfg)
        return

    try:
        asyncio.run(run_bot(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
