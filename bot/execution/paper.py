"""Routeur papier: calcule le plan d'ordre et le journalise, sans rien envoyer."""

from __future__ import annotations

import logging

from ..models import Signal
from .base import InsufficientSize, OrderPlan, OrderRouter, SymbolFilters, build_plan

log = logging.getLogger(__name__)


class PaperRouter(OrderRouter):
    """Simule l'execution. C'est le routeur par defaut du bot."""

    name = "paper"

    def __init__(
        self,
        filters: dict[str, SymbolFilters],
        equity_usdt: float,
        risk_pct: float,
        leverage: int,
    ) -> None:
        self.filters = filters
        self.equity_usdt = equity_usdt
        self.risk_pct = risk_pct
        self.leverage = leverage
        self.plans: list[OrderPlan] = []

    async def submit(self, signal: Signal) -> OrderPlan | None:
        symbol_filters = self.filters.get(signal.symbol)
        if symbol_filters is None:
            log.warning("%s: filtres inconnus, ordre papier ignore.", signal.symbol)
            return None
        try:
            plan = build_plan(
                signal, self.equity_usdt, self.risk_pct, self.leverage, symbol_filters
            )
        except (InsufficientSize, ValueError) as exc:
            log.info("Ordre papier ignore - %s", exc)
            return None
        self.plans.append(plan)
        log.info("[PAPIER] %s", plan.describe())
        return plan
