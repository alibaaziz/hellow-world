"""Fixtures partagees: fabrication de series de bougies synthetiques."""

from __future__ import annotations

import pytest

from bot.config import Config
from bot.models import Candle, Series


@pytest.fixture
def cfg() -> Config:
    return Config(candles=200, min_score=2)


def make_series(
    closes: list[float],
    symbol: str = "TESTUSDT",
    interval: str = "15m",
    spread: float = 0.005,
) -> Series:
    """Construit une serie ou chaque bougie encadre sa cloture d'un ecart fixe."""
    candles = []
    for i, close in enumerate(closes):
        prev = closes[i - 1] if i else close
        high = max(prev, close) * (1 + spread)
        low = min(prev, close) * (1 - spread)
        open_time = i * 900_000
        candles.append(
            Candle(
                open_time=open_time,
                open=prev,
                high=high,
                low=low,
                close=close,
                volume=1000.0,
                close_time=open_time + 899_999,
                quote_volume=1000.0 * close,
            )
        )
    return Series(symbol=symbol, interval=interval, candles=candles)


@pytest.fixture
def series_factory():
    return make_series


def ramp(start: float, end: float, count: int) -> list[float]:
    """Progression lineaire de `start` a `end` sur `count` points."""
    if count < 2:
        return [start]
    step = (end - start) / (count - 1)
    return [start + step * i for i in range(count)]
