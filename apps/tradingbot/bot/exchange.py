"""Публичный клиент Bybit v5 (только чтение рынка).

Ключи API не нужны и не используются: бот в демо-режиме не отправляет ордера,
он берёт настоящие котировки, свечи и стакан, а сделки ведёт у себя в журнале.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

import aiohttp

from .models import Candle, Level, Ticker, TradePrint

log = logging.getLogger("exchange")

BASE_URL = "https://api.bybit.com"


class RateLimiter:
    """Простой ограничитель: не больше `rate` запросов в секунду."""

    def __init__(self, rate: float = 8.0):
        self._min_interval = 1.0 / rate
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._min_interval - (now - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class BybitPublic:
    def __init__(self, category: str = "linear", rate: float = 8.0, timeout: float = 15.0):
        self.category = category
        self._limiter = RateLimiter(rate)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "BybitPublic":
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: Dict, retries: int = 3) -> Dict:
        assert self._session is not None, "клиент не открыт (используй async with)"
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            await self._limiter.wait()
            try:
                async with self._session.get(BASE_URL + path, params=params) as resp:
                    if resp.status == 429:
                        raise RuntimeError("rate limit 429")
                    resp.raise_for_status()
                    data = await resp.json()
                if data.get("retCode") != 0:
                    raise RuntimeError(f"bybit retCode={data.get('retCode')} {data.get('retMsg')}")
                return data.get("result") or {}
            except Exception as exc:  # noqa: BLE001 - сеть, ретраим
                last_err = exc
                await asyncio.sleep(0.6 * (attempt + 1))
        log.warning("GET %s %s не удался: %s", path, params, last_err)
        raise last_err  # type: ignore[misc]

    # ------------------------------------------------------------------ рынок

    async def instruments(self) -> Dict[str, Dict]:
        """Справочник инструментов: symbol -> {launchTime, tickSize, qtyStep, status}."""
        out: Dict[str, Dict] = {}
        cursor = ""
        while True:
            params = {"category": self.category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = await self._get("/v5/market/instruments-info", params)
            for item in result.get("list", []):
                pf = item.get("priceFilter", {}) or {}
                lf = item.get("lotSizeFilter", {}) or {}
                out[item["symbol"]] = {
                    "base": item.get("baseCoin", ""),
                    "quote": item.get("quoteCoin", ""),
                    "status": item.get("status", ""),
                    "launch_ms": int(item.get("launchTime") or 0),
                    "tick_size": float(pf.get("tickSize") or 0) or 0.0,
                    "qty_step": float(lf.get("qtyStep") or 0) or 0.0,
                    "min_qty": float(lf.get("minOrderQty") or 0) or 0.0,
                }
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        return out

    async def tickers(self) -> List[Ticker]:
        result = await self._get("/v5/market/tickers", {"category": self.category})
        out: List[Ticker] = []
        for item in result.get("list", []):
            try:
                out.append(
                    Ticker(
                        symbol=item["symbol"],
                        last_price=float(item["lastPrice"]),
                        change_24h=float(item.get("price24hPcnt") or 0.0),
                        turnover_24h=float(item.get("turnover24h") or 0.0),
                        volume_24h=float(item.get("volume24h") or 0.0),
                        high_24h=float(item.get("highPrice24h") or 0.0),
                        low_24h=float(item.get("lowPrice24h") or 0.0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def ticker(self, symbol: str) -> Optional[Ticker]:
        result = await self._get("/v5/market/tickers", {"category": self.category, "symbol": symbol})
        items = result.get("list") or []
        if not items:
            return None
        item = items[0]
        return Ticker(
            symbol=item["symbol"],
            last_price=float(item["lastPrice"]),
            change_24h=float(item.get("price24hPcnt") or 0.0),
            turnover_24h=float(item.get("turnover24h") or 0.0),
            volume_24h=float(item.get("volume24h") or 0.0),
            high_24h=float(item.get("highPrice24h") or 0.0),
            low_24h=float(item.get("lowPrice24h") or 0.0),
        )

    async def klines(self, symbol: str, interval: str, limit: int = 200,
                     start: Optional[int] = None, end: Optional[int] = None) -> List[Candle]:
        """Свечи от старых к новым. interval: '1', '5', '15', '60', 'D'.

        start/end - границы окна в миллисекундах. Живому боту они не нужны
        (ему всегда нужен хвост истории), а бэктесту (bot/history.py) - да:
        биржа отдаёт максимум 1000 свечей за запрос, и четыре месяца
        пятиминуток выкачиваются страницами, сдвигая `end` в прошлое.
        """
        params = {"category": self.category, "symbol": symbol,
                  "interval": interval, "limit": limit}
        if start is not None:
            params["start"] = int(start)
        if end is not None:
            params["end"] = int(end)
        result = await self._get("/v5/market/kline", params)
        rows = result.get("list") or []
        candles: List[Candle] = []
        for row in rows:
            try:
                candles.append(
                    Candle(
                        ts=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        turnover=float(row[6]) if len(row) > 6 else 0.0,
                    )
                )
            except (TypeError, ValueError, IndexError):
                continue
        candles.sort(key=lambda c: c.ts)  # Bybit отдаёт новые первыми
        return candles

    async def recent_trades(self, symbol: str, limit: int = 500) -> List["TradePrint"]:
        """Лента принтов - реально прошедшие сделки, от свежих к старым.

        Это единственный публичный источник, по которому видно АКТИВНОСТЬ:
        стакан показывает намерения (их снимают), свечи усредняют минуту, а
        лента - то, что действительно исполнилось, с точностью до миллисекунд.
        На ней ТС пробоя и строит решение о входе и выходе.
        """
        result = await self._get(
            "/v5/market/recent-trade",
            {"category": self.category, "symbol": symbol, "limit": min(limit, 1000)},
        )
        out: List[TradePrint] = []
        for row in result.get("list", []):
            try:
                out.append(
                    TradePrint(
                        ts=int(row["time"]),
                        price=float(row["price"]),
                        size=float(row["size"]),
                        side=row.get("side", ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def orderbook(self, symbol: str, depth: int = 200):
        """Снапшот стакана: (bids, asks) - от лучшей цены вглубь."""
        result = await self._get(
            "/v5/market/orderbook", {"category": self.category, "symbol": symbol, "limit": depth}
        )
        bids = [Level(float(p), float(s)) for p, s in result.get("b", [])]
        asks = [Level(float(p), float(s)) for p, s in result.get("a", [])]
        bids.sort(key=lambda l: l.price, reverse=True)
        asks.sort(key=lambda l: l.price)
        return bids, asks
