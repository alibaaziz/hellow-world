"""Types de base partages par le scanner, les alertes et l'execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def opposite(self) -> "Side":
        return Side.SHORT if self is Side.LONG else Side.LONG


@dataclass(frozen=True)
class Candle:
    """Une bougie fermee de Binance Futures."""

    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float

    @classmethod
    def from_binance(cls, row: list) -> "Candle":
        return cls(
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=int(row[6]),
            quote_volume=float(row[7]),
        )

    @property
    def closed_at(self) -> datetime:
        return datetime.fromtimestamp(self.close_time / 1000, tz=timezone.utc)


@dataclass(frozen=True)
class Series:
    """Serie de bougies fermees pour un symbole / une unite de temps."""

    symbol: str
    interval: str
    candles: list[Candle]

    def __len__(self) -> int:
        return len(self.candles)

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles]

    @property
    def highs(self) -> list[float]:
        return [c.high for c in self.candles]

    @property
    def lows(self) -> list[float]:
        return [c.low for c in self.candles]

    @property
    def volumes(self) -> list[float]:
        return [c.volume for c in self.candles]

    @property
    def last(self) -> Candle:
        return self.candles[-1]


@dataclass
class Signal:
    """Un setup detecte sur un symbole."""

    symbol: str
    side: Side
    interval: str
    price: float
    score: int
    reasons: list[str] = field(default_factory=list)
    stop_loss: float | None = None
    take_profit: float | None = None
    atr: float | None = None
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def risk_pct(self) -> float | None:
        """Distance au stop en % du prix d'entree."""
        if self.stop_loss is None or not self.price:
            return None
        return abs(self.price - self.stop_loss) / self.price * 100

    def key(self) -> tuple[str, str, str]:
        """Cle de deduplication (un meme setup ne doit pas spammer)."""
        return (self.symbol, self.interval, self.side.value)
