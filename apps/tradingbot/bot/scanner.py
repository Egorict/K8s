"""Шаг 1 ТС: поиск монет-кандидатов.

Условия:
  - фьючерсы (USDT-перпетуалы), рост за сутки > 10%;
  - монета - альткоин (BTC/ETH/стейблы/индексы выкидываем);
  - объёмы средние (не мёртвая и не мега-ликвидная);
  - рост именно резкий: большая часть дневного движения - в последние часы.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import ScreenerConfig
from .exchange import BybitPublic
from .indicators import pct, scale
from .models import Ticker

log = logging.getLogger("scanner")


@dataclass
class Candidate:
    ticker: Ticker
    sharpness: float          # доля дневного роста, сделанная в окне резкости
    window_gain: float        # рост за окно резкости
    score: float
    reason: str

    @property
    def symbol(self) -> str:
        return self.ticker.symbol


class Scanner:
    def __init__(self, client: BybitPublic, cfg: ScreenerConfig):
        self.client = client
        self.cfg = cfg
        self._instruments: Dict[str, Dict] = {}
        self._instruments_ts: float = 0.0

    async def _get_instruments(self) -> Dict[str, Dict]:
        # Справочник инструментов меняется редко - обновляем раз в час.
        if not self._instruments or time.time() - self._instruments_ts > 3600:
            self._instruments = await self.client.instruments()
            self._instruments_ts = time.time()
            log.info("Загружено инструментов: %d", len(self._instruments))
        return self._instruments

    def _is_altcoin(self, symbol: str, info: Dict) -> bool:
        base = (info.get("base") or symbol.replace("USDT", "")).upper()
        if base in {b.upper() for b in self.cfg.excluded_bases}:
            return False
        for suffix in self.cfg.excluded_suffixes:
            if base.endswith(suffix.upper()) and len(base) > len(suffix):
                return False
        return True

    def _prefilter(self, tickers: List[Ticker], instruments: Dict[str, Dict]) -> List[Ticker]:
        now_ms = time.time() * 1000
        min_age_ms = self.cfg.min_listing_age_days * 86_400_000
        out: List[Ticker] = []
        for t in tickers:
            info = instruments.get(t.symbol)
            if not info or info.get("status") != "Trading":
                continue
            if info.get("quote") != "USDT":
                continue
            if not self._is_altcoin(t.symbol, info):
                continue
            launch = info.get("launch_ms") or 0
            if launch and now_ms - launch < min_age_ms:
                continue
            if not (self.cfg.min_change_24h < t.change_24h <= self.cfg.max_change_24h):
                continue
            if not (self.cfg.min_turnover_24h <= t.turnover_24h <= self.cfg.max_turnover_24h):
                continue
            out.append(t)
        return out

    async def _sharpness(self, symbol: str, change_24h: float) -> Optional[tuple]:
        """Насколько рост «резкий»: (sharpness, window_gain)."""
        need = max(4, self.cfg.sharpness_window_hours * 4)  # 15m свечей в окне
        candles = await self.client.klines(symbol, "15", limit=need + 4)
        if len(candles) < need:
            return None
        window = candles[-need:]
        start = window[0].open
        last = window[-1].close
        window_gain = pct(start, last)
        if change_24h <= 0:
            return 0.0, window_gain
        # Доля дневного движения, уложившаяся в окно. Считаем по абсолютному
        # приросту цены, чтобы не путаться в процентах от разных баз.
        day_start = last / (1.0 + change_24h)
        day_move = last - day_start
        if day_move <= 0:
            return 0.0, window_gain
        sharpness = (last - start) / day_move
        return max(0.0, min(sharpness, 2.0)), window_gain

    async def scan(self) -> List[Candidate]:
        instruments = await self._get_instruments()
        tickers = await self.client.tickers()
        prefiltered = self._prefilter(tickers, instruments)
        log.info("Прошли базовый фильтр: %d из %d", len(prefiltered), len(tickers))

        candidates: List[Candidate] = []
        # Сортируем по росту и берём разумный запас, чтобы не долбить API.
        prefiltered.sort(key=lambda t: t.change_24h, reverse=True)
        for t in prefiltered[: self.cfg.max_watchlist * 2]:
            try:
                res = await self._sharpness(t.symbol, t.change_24h)
            except Exception as exc:  # noqa: BLE001
                log.debug("Нет свечей по %s: %s", t.symbol, exc)
                continue
            if res is None:
                continue
            sharpness, window_gain = res
            if sharpness < self.cfg.min_sharpness:
                continue

            # Скор кандидата: резкость важнее сырого роста, объём - «золотая середина».
            growth_s = scale(t.change_24h, self.cfg.min_change_24h, 0.60)
            sharp_s = scale(sharpness, self.cfg.min_sharpness, 1.0)
            mid_turnover = (self.cfg.min_turnover_24h + self.cfg.max_turnover_24h) / 2
            vol_s = 1.0 - min(1.0, abs(t.turnover_24h - mid_turnover) / mid_turnover)
            score = 0.45 * sharp_s + 0.35 * growth_s + 0.20 * vol_s

            candidates.append(
                Candidate(
                    ticker=t,
                    sharpness=sharpness,
                    window_gain=window_gain,
                    score=score,
                    reason=(f"24h {t.change_24h * 100:+.1f}%, "
                            f"{self.cfg.sharpness_window_hours}ч {window_gain * 100:+.1f}%, "
                            f"резкость {sharpness * 100:.0f}%, "
                            f"оборот {t.turnover_24h / 1e6:.1f}M$"),
                )
            )

        candidates.sort(key=lambda c: c.score, reverse=True)
        top = candidates[: self.cfg.max_watchlist]
        if top:
            log.info("Кандидаты (%d): %s", len(top), ", ".join(c.symbol for c in top))
        else:
            log.info("Кандидатов под ТС сейчас нет")
        return top
