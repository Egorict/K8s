"""Исторические свечи для бэктеста: закачка страницами + кэш на томе.

Зачем отдельный модуль. Живому боту нужен хвост истории - один запрос на
монету, и всё. Бэктесту нужны месяцы: биржа отдаёт максимум 1000 свечей за
запрос, поэтому четыре месяца пятиминуток (34 560 свечей) - это 35 страниц,
а на сотнях монет - десятки тысяч запросов. Качать их заново при каждом
пересчёте нельзя, поэтому:

  * страницы идут в прошлое: запрашиваем окно с `end`, следующей границей
    берём время самой старой полученной свечи минус миллисекунда;
  * результат ложится в data/history/<монета>_<тф>.csv.gz - несжатый CSV на
    сотню монет занял бы гигабайты, gzip сжимает такие ряды примерно в 5 раз;
  * при следующем запуске догружается только недостающий кусок (обычно хвост
    за время с прошлого пересчёта).

Файл кэша - обычный CSV с точкой с запятой, как и журнал сделок: его можно
открыть и посмотреть глазами, не запуская Python.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
import os
from typing import Dict, List, Optional, Tuple

from .exchange import BybitPublic
from .models import Candle

log = logging.getLogger("history")

# Длительность свечи в миллисекундах. Ключи - те же строки, что принимает
# Bybit v5 в параметре interval.
INTERVAL_MS: Dict[str, int] = {
    "1": 60_000,
    "3": 180_000,
    "5": 300_000,
    "15": 900_000,
    "30": 1_800_000,
    "60": 3_600_000,
    "240": 14_400_000,
    "D": 86_400_000,
}

# Максимум свечей в одном ответе Bybit.
PAGE_LIMIT = 1000

CSV_COLUMNS = ["ts", "open", "high", "low", "close", "volume", "turnover"]


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError:  # pragma: no cover - опечатка в вызывающем коде
        raise ValueError(f"неизвестный таймфрейм: {interval}") from None


class History:
    """Свечи за произвольный период: сначала из кэша, недостающее - с биржи."""

    def __init__(self, client: BybitPublic, cache_dir: str):
        self.client = client
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        # Сколько запросов ушло на закачку - показываем в прогрессе бэктеста,
        # чтобы было видно, что процесс не завис, а качает.
        self.requests = 0

    # ------------------------------------------------------------------ кэш

    def _path(self, symbol: str, interval: str) -> str:
        return os.path.join(self.cache_dir, f"{symbol}_{interval}.csv.gz")

    def _read_cache(self, symbol: str, interval: str) -> List[Candle]:
        path = self._path(symbol, interval)
        if not os.path.exists(path):
            return []
        try:
            with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
                rows = csv.DictReader(fh, delimiter=";")
                out = [
                    Candle(
                        ts=int(r["ts"]), open=float(r["open"]), high=float(r["high"]),
                        low=float(r["low"]), close=float(r["close"]),
                        volume=float(r["volume"]), turnover=float(r["turnover"]),
                    )
                    for r in rows
                ]
        except (OSError, ValueError, KeyError, EOFError) as exc:
            # Битый кэш (например, процесс убили посреди записи) - не повод
            # валить весь бэктест: просто качаем заново.
            log.warning("Кэш %s повреждён (%s), перекачаю", path, exc)
            return []
        out.sort(key=lambda c: c.ts)
        return out

    def _write_cache(self, symbol: str, interval: str, candles: List[Candle]) -> None:
        path = self._path(symbol, interval)
        tmp = path + ".tmp"
        # Пишем во временный файл и переименовываем: если процесс погасят на
        # середине, старый кэш останется целым.
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";", lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for c in candles:
            writer.writerow([c.ts, f"{c.open:.10g}", f"{c.high:.10g}", f"{c.low:.10g}",
                             f"{c.close:.10g}", f"{c.volume:.10g}", f"{c.turnover:.10g}"])
        with gzip.open(tmp, "wt", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())
        os.replace(tmp, path)

    # ------------------------------------------------------------------ сеть

    async def _fetch_range(self, symbol: str, interval: str,
                           start_ms: int, end_ms: int) -> List[Candle]:
        """Свечи [start_ms, end_ms] страницами, от свежих к старым."""
        step = interval_ms(interval)
        collected: Dict[int, Candle] = {}
        cursor = end_ms
        while cursor >= start_ms:
            try:
                page = await self.client.klines(symbol, interval, limit=PAGE_LIMIT,
                                                start=start_ms, end=cursor)
            except Exception as exc:  # noqa: BLE001 - сеть; страницу пропускаем
                log.debug("[%s %s] страница до %d не пришла: %s", symbol, interval, cursor, exc)
                break
            self.requests += 1
            if not page:
                break
            for candle in page:
                collected[candle.ts] = candle
            oldest = min(c.ts for c in page)
            if oldest <= start_ms or len(page) < PAGE_LIMIT:
                break
            # Защита от зацикливания: если биржа отдала то же окно, что и в
            # прошлый раз, дальше идти некуда.
            if oldest >= cursor:
                break
            cursor = oldest - step
        return [collected[ts] for ts in sorted(collected)]

    # ------------------------------------------------------------------ выдача

    async def load(self, symbol: str, interval: str,
                   start_ms: int, end_ms: int) -> List[Candle]:
        """Свечи монеты за окно. Кэш дополняется недостающими краями."""
        step = interval_ms(interval)
        cached = self._read_cache(symbol, interval)
        changed = False

        if not cached:
            cached = await self._fetch_range(symbol, interval, start_ms, end_ms)
            changed = bool(cached)
        else:
            # Старый край: истории не хватает вглубь.
            if cached[0].ts - start_ms > step:
                older = await self._fetch_range(symbol, interval, start_ms, cached[0].ts - step)
                if older:
                    cached = older + cached
                    changed = True
            # Свежий край: с прошлого пересчёта прошло время.
            if end_ms - cached[-1].ts > step:
                newer = await self._fetch_range(symbol, interval, cached[-1].ts + step, end_ms)
                if newer:
                    known = {c.ts for c in cached}
                    cached = cached + [c for c in newer if c.ts not in known]
                    cached.sort(key=lambda c: c.ts)
                    changed = True

        if changed and cached:
            self._write_cache(symbol, interval, cached)

        return [c for c in cached if start_ms <= c.ts <= end_ms]

    # ------------------------------------------------------------------ уборка

    def size_bytes(self) -> int:
        total = 0
        for name in os.listdir(self.cache_dir):
            try:
                total += os.path.getsize(os.path.join(self.cache_dir, name))
            except OSError:
                continue
        return total

    def keep_only(self, symbols: List[str]) -> int:
        """Удаляет кэш монет, которых больше нет в наборе. Возвращает счётчик.

        Нужно, чтобы том бота не рос бесконечно: набор монет пересчитывается
        по обороту и со временем меняется, а файлы выбывших остались бы лежать.
        """
        keep = set(symbols)
        removed = 0
        for name in os.listdir(self.cache_dir):
            if not name.endswith(".csv.gz"):
                continue
            symbol = name.rsplit("_", 1)[0]
            if symbol in keep:
                continue
            try:
                os.remove(os.path.join(self.cache_dir, name))
                removed += 1
            except OSError:
                continue
        return removed


def resample(candles: List[Candle], src_interval: str, dst_interval: str,
             until_ts: Optional[int] = None) -> List[Candle]:
    """Склейка мелких свечей в крупные (5m -> 15m и т.п.).

    Нужна ровно для одного: последняя крупная свеча на момент решения ещё НЕ
    закрыта, и брать её готовой с биржи - значит подглядывать в будущее. Здесь
    она собирается из тех мелких свечей, которые к этому моменту уже прошли.

    until_ts - время открытия последней учитываемой мелкой свечи включительно.
    """
    if not candles:
        return []
    size = interval_ms(dst_interval)
    buckets: Dict[int, List[Candle]] = {}
    for c in candles:
        if until_ts is not None and c.ts > until_ts:
            break
        buckets.setdefault(c.ts - c.ts % size, []).append(c)

    out: List[Candle] = []
    for ts in sorted(buckets):
        chunk = buckets[ts]
        out.append(Candle(
            ts=ts,
            open=chunk[0].open,
            high=max(c.high for c in chunk),
            low=min(c.low for c in chunk),
            close=chunk[-1].close,
            volume=sum(c.volume for c in chunk),
            turnover=sum(c.turnover for c in chunk),
        ))
    return out


def month_key(ts_ms: int) -> str:
    """'2026-09' по времени свечи. Месяцы считаем в UTC - как и биржа."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def month_bounds(candles: List[Candle]) -> List[Tuple[str, int, int]]:
    """[(месяц, индекс первой свечи, индекс последней)] по возрастанию."""
    out: List[Tuple[str, int, int]] = []
    current = ""
    for i, c in enumerate(candles):
        key = month_key(c.ts)
        if key != current:
            out.append((key, i, i))
            current = key
        else:
            month, first, _ = out[-1]
            out[-1] = (month, first, i)
    return out
