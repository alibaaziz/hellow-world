"""Couche d'execution: dimensionnement, arrondis et interface de routeur.

Le scanner produit des `Signal`; un `OrderRouter` les transforme en `OrderPlan`
puis, eventuellement, en ordres reels. Par defaut le routeur est en papier.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..models import Side, Signal


@dataclass(frozen=True)
class SymbolFilters:
    """Contraintes de precision imposees par Binance pour un symbole."""

    symbol: str
    tick_size: float
    step_size: float
    min_qty: float
    min_notional: float

    @classmethod
    def from_exchange_info(cls, entry: dict[str, Any]) -> "SymbolFilters":
        filters = {f["filterType"]: f for f in entry.get("filters", [])}
        price_filter = filters.get("PRICE_FILTER", {})
        lot = filters.get("LOT_SIZE", {})
        notional = filters.get("MIN_NOTIONAL", {})
        return cls(
            symbol=entry["symbol"],
            tick_size=float(price_filter.get("tickSize", 0.01)),
            step_size=float(lot.get("stepSize", 0.001)),
            min_qty=float(lot.get("minQty", 0.0)),
            min_notional=float(notional.get("notional", 0.0)),
        )

    def round_price(self, price: float) -> float:
        return _round_to_step(price, self.tick_size)

    def round_qty(self, qty: float) -> float:
        return _round_to_step(qty, self.step_size)


def _round_to_step(value: float, step: float) -> float:
    """Arrondi vers le bas sur un multiple de `step`, sans bruit binaire."""
    if step <= 0:
        return value
    dec_step = Decimal(str(step))
    units = (Decimal(str(value)) / dec_step).to_integral_value(rounding="ROUND_FLOOR")
    return float(units * dec_step)


@dataclass
class OrderPlan:
    """Tout ce qu'il faut pour passer l'ordre, avant tout appel reseau."""

    signal: Signal
    quantity: float
    notional: float
    leverage: int
    stop_loss: float
    take_profit: float | None
    risk_usdt: float

    @property
    def symbol(self) -> str:
        return self.signal.symbol

    @property
    def side(self) -> Side:
        return self.signal.side

    @property
    def entry_side(self) -> str:
        return "BUY" if self.side is Side.LONG else "SELL"

    @property
    def exit_side(self) -> str:
        return "SELL" if self.side is Side.LONG else "BUY"

    def describe(self) -> str:
        return (
            f"{self.entry_side} {self.quantity} {self.symbol} @~{self.signal.price} "
            f"(notionnel {self.notional:.2f} USDT, levier x{self.leverage}, "
            f"stop {self.stop_loss}, risque {self.risk_usdt:.2f} USDT)"
        )


class InsufficientSize(ValueError):
    """La taille calculee ne respecte pas les minimums du symbole."""


def build_plan(
    signal: Signal,
    equity_usdt: float,
    risk_pct: float,
    leverage: int,
    filters: SymbolFilters,
) -> OrderPlan:
    """Dimensionne la position pour risquer `risk_pct`% du capital jusqu'au stop.

    Leve `InsufficientSize` si le capital ne permet pas d'atteindre les minimums
    du symbole, ou `ValueError` si le signal n'a pas de stop exploitable.
    """
    if signal.stop_loss is None:
        raise ValueError(f"{signal.symbol}: signal sans stop loss, dimensionnement impossible.")
    if equity_usdt <= 0:
        raise InsufficientSize("Capital nul ou negatif.")

    stop = filters.round_price(signal.stop_loss)
    stop_distance = abs(signal.price - stop)
    if stop_distance <= 0:
        raise ValueError(f"{signal.symbol}: distance au stop nulle apres arrondi.")

    risk_usdt = equity_usdt * risk_pct / 100
    raw_qty = risk_usdt / stop_distance
    qty = filters.round_qty(raw_qty)

    if qty <= 0 or qty < filters.min_qty:
        raise InsufficientSize(
            f"{signal.symbol}: quantite {raw_qty:.8f} sous le minimum {filters.min_qty}."
        )

    notional = qty * signal.price
    if filters.min_notional and notional < filters.min_notional:
        raise InsufficientSize(
            f"{signal.symbol}: notionnel {notional:.2f} sous le minimum "
            f"{filters.min_notional:.2f} USDT."
        )

    margin_required = notional / max(leverage, 1)
    if margin_required > equity_usdt:
        raise InsufficientSize(
            f"{signal.symbol}: marge requise {margin_required:.2f} > capital {equity_usdt:.2f}."
        )

    take_profit = filters.round_price(signal.take_profit) if signal.take_profit else None
    return OrderPlan(
        signal=signal,
        quantity=qty,
        notional=notional,
        leverage=leverage,
        stop_loss=stop,
        take_profit=take_profit,
        risk_usdt=qty * stop_distance,
    )


class OrderRouter(ABC):
    """Destination des signaux une fois valides."""

    name: str = "router"

    @abstractmethod
    async def submit(self, signal: Signal) -> OrderPlan | None:
        """Traite un signal. Renvoie le plan execute, ou None s'il a ete ignore."""

    async def close(self) -> None:
        """Libere les ressources eventuelles."""


def quantity_precision(step_size: float) -> int:
    """Nombre de decimales autorisees pour une quantite."""
    if step_size <= 0:
        return 8
    return max(0, -math.floor(math.log10(step_size)))
