"""Журнал демо-сделок: CSV, который можно открыть в Excel.

trades.csv  - закрытые сделки: номер, дата, монета, время входа/выхода, цены, профит.
signals.csv - все сработавшие сетапы (в том числе те, что не переросли в сделку).
state.json  - текущее состояние: открытые позиции и сводная статистика.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .models import ClosedTrade, Position, Setup

log = logging.getLogger("journal")

TRADE_COLUMNS = [
    "num", "date", "symbol", "side", "timeframe",
    "open_time", "close_time", "duration_min",
    "entry_price", "exit_price", "qty", "notional_usd", "margin_usd", "leverage",
    "stop_price", "take_price",
    "gross_pnl_usd", "fees_usd", "profit_usd", "profit_pct_margin",
    "max_profit_usd", "max_loss_usd",
    "exit_reason", "entry_score", "entry_reason",
]

SIGNAL_COLUMNS = [
    "time", "symbol", "price", "timeframe", "score", "action",
    "change_24h_pct", "turnover_24h_usd", "impulses", "stall", "book", "notes",
]


def _ts(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _date(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")


class Journal:
    def __init__(self, trades_csv: str, signals_csv: str, state_json: str):
        self.trades_csv = trades_csv
        self.signals_csv = signals_csv
        self.state_json = state_json
        for path in (trades_csv, signals_csv, state_json):
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._ensure_header(self.trades_csv, TRADE_COLUMNS)
        self._ensure_header(self.signals_csv, SIGNAL_COLUMNS)
        self._count = self._existing_rows(self.trades_csv)

    # ------------------------------------------------------------------ файлы

    @staticmethod
    def _ensure_header(path: str, columns: List[str]) -> None:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh, delimiter=";").writerow(columns)

    @staticmethod
    def _existing_rows(path: str) -> int:
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                return max(0, sum(1 for _ in fh) - 1)
        except OSError:
            return 0

    @staticmethod
    def _append(path: str, columns: List[str], row: Dict) -> None:
        with open(path, "a", newline="", encoding="utf-8-sig") as fh:
            csv.DictWriter(fh, fieldnames=columns, delimiter=";").writerow(row)

    # ------------------------------------------------------------------ запись

    def log_trade(self, trade: ClosedTrade) -> None:
        self._count += 1
        row = {
            "num": self._count,
            "date": _date(trade.opened_at),
            "symbol": trade.symbol,
            "side": trade.side,
            "timeframe": f"{trade.timeframe}m",
            "open_time": _ts(trade.opened_at),
            "close_time": _ts(trade.closed_at),
            "duration_min": round(trade.duration_sec / 60, 2),
            "entry_price": f"{trade.entry_price:.10g}",
            "exit_price": f"{trade.exit_price:.10g}",
            "qty": f"{trade.qty:.10g}",
            "notional_usd": round(trade.notional_usd, 2),
            "margin_usd": round(trade.margin_usd, 2),
            "leverage": f"x{trade.leverage:g}",
            "stop_price": f"{trade.stop_price:.10g}",
            "take_price": f"{trade.take_price:.10g}",
            "gross_pnl_usd": round(trade.gross_pnl_usd, 4),
            "fees_usd": round(trade.fees_usd, 4),
            "profit_usd": round(trade.net_pnl_usd, 4),
            "profit_pct_margin": round(trade.pnl_pct_on_margin, 2),
            "max_profit_usd": round(trade.max_profit_usd, 4),
            "max_loss_usd": round(trade.max_loss_usd, 4),
            "exit_reason": trade.exit_reason,
            "entry_score": round(trade.entry_score, 3),
            "entry_reason": trade.entry_reason,
        }
        self._append(self.trades_csv, TRADE_COLUMNS, row)

    def log_signal(self, setup: Setup, action: str) -> None:
        impulses = "; ".join(
            f"{tf}m:{'+' if i.found else '-'}{i.gain * 100:.2f}%/{i.candles}св"
            for tf, i in sorted(setup.impulses.items(), key=lambda kv: int(kv[0]))
        )
        row = {
            "time": _ts(time.time()),
            "symbol": setup.symbol,
            "price": f"{setup.price:.10g}",
            "timeframe": f"{setup.primary_tf}m",
            "score": round(setup.score, 3),
            "action": action,
            "change_24h_pct": round(setup.ticker.change_24h * 100, 2),
            "turnover_24h_usd": round(setup.ticker.turnover_24h, 0),
            "impulses": impulses,
            "stall": setup.stall.reason,
            "book": setup.book.summary(),
            "notes": " | ".join(setup.notes),
        }
        self._append(self.signals_csv, SIGNAL_COLUMNS, row)

    def save_state(self, positions: List[Position], stats: Dict, watchlist: List[str]) -> None:
        payload = {
            "updated": _ts(time.time()),
            "stats": stats,
            "watchlist": watchlist,
            "open_positions": [
                {
                    "trade_id": p.trade_id,
                    "symbol": p.symbol,
                    "timeframe": f"{p.timeframe}m",
                    "opened": _ts(p.opened_at),
                    "entry_price": p.entry_price,
                    "last_price": p.last_price,
                    "stop_price": p.stop_price,
                    "take_price": p.take_price,
                    "best_pnl_usd": round(p.best_pnl_usd, 2),
                    "age_min": round(p.age_sec() / 60, 1),
                }
                for p in positions
            ],
        }
        tmp = self.state_json + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_json)
