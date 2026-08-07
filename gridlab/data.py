"""Market data: Binance USDS-M futures download, CSV cache, synthetic bars.

Only public endpoints are used — no API key, no signature, nothing that can
touch your account. Standard library only.
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

FUTURES_BASE = "https://fapi.binance.com"
KLINES_PATH = "/fapi/v1/klines"
FUNDING_PATH = "/fapi/v1/fundingRate"
MAX_KLINES = 1500
MAX_FUNDING = 1000

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}

Bar = Tuple[int, float, float, float, float, float]   # ts, o, h, l, c, volume


class DataError(RuntimeError):
    pass


def _get(url: str, params: Dict[str, object], retries: int = 4) -> object:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    full = f"{url}?{query}"
    delay = 2.0
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(full, headers={"User-Agent": "gridlab/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
    raise DataError(
        f"echec de la requete Binance ({last}).\n"
        "Verifiez la connexion, ou lancez la commande depuis une machine "
        "ayant acces a fapi.binance.com."
    )


def fetch_klines(symbol: str, interval: str = "1h", days: int = 90,
                 end_ms: Optional[int] = None) -> List[Bar]:
    """Download the last ``days`` of futures candles, paging back as needed."""
    if interval not in INTERVAL_MS:
        raise DataError(f"intervalle inconnu: {interval}")
    step = INTERVAL_MS[interval]
    end = end_ms if end_ms is not None else int(time.time() * 1000)
    start = end - days * 86_400_000

    bars: List[Bar] = []
    cursor = start
    while cursor < end:
        raw = _get(FUTURES_BASE + KLINES_PATH, {
            "symbol": symbol.upper(),
            "interval": interval,
            "startTime": cursor,
            "endTime": end,
            "limit": MAX_KLINES,
        })
        if not isinstance(raw, list) or not raw:
            break
        for row in raw:
            bars.append((int(row[0]), float(row[1]), float(row[2]),
                         float(row[3]), float(row[4]), float(row[5])))
        cursor = int(raw[-1][0]) + step
        if len(raw) < MAX_KLINES:
            break
    if not bars:
        raise DataError(f"aucune bougie recue pour {symbol} {interval}")
    return _dedupe(bars)


def fetch_funding(symbol: str, days: int = 90,
                  end_ms: Optional[int] = None) -> List[Tuple[int, float]]:
    """Download realized funding rates (one entry per 8h settlement)."""
    end = end_ms if end_ms is not None else int(time.time() * 1000)
    start = end - days * 86_400_000
    out: List[Tuple[int, float]] = []
    cursor = start
    while cursor < end:
        raw = _get(FUTURES_BASE + FUNDING_PATH, {
            "symbol": symbol.upper(),
            "startTime": cursor,
            "endTime": end,
            "limit": MAX_FUNDING,
        })
        if not isinstance(raw, list) or not raw:
            break
        for row in raw:
            out.append((int(row["fundingTime"]), float(row["fundingRate"])))
        cursor = int(raw[-1]["fundingTime"]) + 1
        if len(raw) < MAX_FUNDING:
            break
    return out


def _dedupe(bars: Sequence[Bar]) -> List[Bar]:
    seen: Dict[int, Bar] = {}
    for bar in bars:
        seen[bar[0]] = bar
    return [seen[k] for k in sorted(seen)]


# ------------------------------------------------------------------- storage

def save_csv(path: str, bars: Sequence[Bar]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["open_time", "open", "high", "low", "close", "volume"])
        for bar in bars:
            writer.writerow(list(bar))


def load_csv(path: str) -> List[Bar]:
    bars: List[Bar] = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            bars.append((
                int(float(row["open_time"])),
                float(row["open"]), float(row["high"]),
                float(row["low"]), float(row["close"]),
                float(row.get("volume") or 0.0),
            ))
    if not bars:
        raise DataError(f"fichier vide: {path}")
    return bars


def save_funding_csv(path: str, rates: Sequence[Tuple[int, float]]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["funding_time", "funding_rate"])
        writer.writerows(rates)


def load_funding_csv(path: str) -> List[Tuple[int, float]]:
    out: List[Tuple[int, float]] = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            out.append((int(float(row["funding_time"])), float(row["funding_rate"])))
    return out


# ----------------------------------------------------------------- synthetic

def synthetic_bars(n: int = 2000, start_price: float = 60_000.0, interval: str = "1h",
                   drift: float = 0.0, vol: float = 0.004, seed: int = 7,
                   start_ms: int = 1_700_000_000_000) -> List[Bar]:
    """Generate range-bound-ish candles for offline testing and demos.

    A mean-reverting log-price walk: realistic enough to exercise the engine,
    and never to be mistaken for a forecast.
    """
    rng = random.Random(seed)
    step = INTERVAL_MS[interval]
    bars: List[Bar] = []
    log_price = math.log(start_price)
    anchor = log_price

    for i in range(n):
        pull = 0.02 * (anchor - log_price)
        shock = rng.gauss(drift, vol)
        open_price = math.exp(log_price)
        log_price += pull + shock
        close_price = math.exp(log_price)
        span = abs(close_price - open_price) + close_price * abs(rng.gauss(0, vol * 0.6))
        high = max(open_price, close_price) + span * rng.random()
        low = min(open_price, close_price) - span * rng.random()
        low = max(low, 1e-8)
        bars.append((start_ms + i * step, open_price, high, low, close_price,
                     1000.0 * (1.0 + rng.random())))
    return bars


# --------------------------------------------------------------- convenience

def price_stats(bars: Sequence[Bar]) -> Dict[str, float]:
    closes = [b[4] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    ordered = sorted(closes)

    def percentile(p: float) -> float:
        if not ordered:
            return 0.0
        idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
        return ordered[idx]

    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
               if closes[i - 1] > 0]
    mean = sum(returns) / len(returns) if returns else 0.0
    var = sum((r - mean) ** 2 for r in returns) / len(returns) if returns else 0.0

    return {
        "first": closes[0],
        "last": closes[-1],
        "min": min(lows),
        "max": max(highs),
        "p05": percentile(0.05),
        "p25": percentile(0.25),
        "median": percentile(0.50),
        "p75": percentile(0.75),
        "p95": percentile(0.95),
        "vol_per_bar_pct": math.sqrt(var) * 100.0,
        "atr_pct": _atr_pct(bars),
    }


def _atr_pct(bars: Sequence[Bar], period: int = 14) -> float:
    """Average true range over the last ``period`` bars, in percent of price."""
    if len(bars) < 2:
        return 0.0
    ranges: List[float] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1][4]
        high, low = bars[i][2], bars[i][3]
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    window = ranges[-period:] if len(ranges) >= period else ranges
    atr = sum(window) / len(window)
    return atr / bars[-1][4] * 100.0


def auto_range(bars: Sequence[Bar], width: float = 0.90) -> Tuple[float, float]:
    """Grid bounds covering the central ``width`` fraction of recent closes."""
    stats = price_stats(bars)
    tail = (1.0 - width) / 2.0
    closes = sorted(b[4] for b in bars)

    def percentile(p: float) -> float:
        idx = min(len(closes) - 1, max(0, int(round(p * (len(closes) - 1)))))
        return closes[idx]

    lower = percentile(tail)
    upper = percentile(1.0 - tail)
    if upper <= lower:
        lower, upper = stats["min"], stats["max"]
    return lower, upper
