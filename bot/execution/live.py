"""Routeur reel: passe l'ordre d'entree puis les protections stop / take profit.

Ce module n'est jamais actif par defaut. Il faut explicitement
TRADING_ENABLED=true ET DRY_RUN=false, plus des cles API valides.
"""

from __future__ import annotations

import logging

from ..config import Config
from ..exchange.binance_futures import BinanceError, BinanceFuturesClient
from ..models import Signal
from .base import InsufficientSize, OrderPlan, OrderRouter, SymbolFilters, build_plan

log = logging.getLogger(__name__)


class LiveRouter(OrderRouter):
    """Envoie de vrais ordres sur Binance USD-M Futures."""

    name = "live"

    def __init__(
        self,
        cfg: Config,
        client: BinanceFuturesClient,
        filters: dict[str, SymbolFilters],
    ) -> None:
        if not cfg.trading_enabled or cfg.dry_run:
            raise RuntimeError(
                "LiveRouter demande alors que TRADING_ENABLED != true ou DRY_RUN != false."
            )
        if not (cfg.binance_api_key and cfg.binance_api_secret):
            raise RuntimeError("Cles API Binance manquantes pour le trading reel.")
        self.cfg = cfg
        self.client = client
        self.filters = filters
        self._leverage_set: set[str] = set()

    async def _equity(self) -> float:
        if self.cfg.account_equity_usdt > 0:
            return self.cfg.account_equity_usdt
        return await self.client.account_balance_usdt()

    async def submit(self, signal: Signal) -> OrderPlan | None:
        symbol_filters = self.filters.get(signal.symbol)
        if symbol_filters is None:
            log.warning("%s: filtres inconnus, ordre annule.", signal.symbol)
            return None

        try:
            equity = await self._equity()
            plan = build_plan(
                signal, equity, self.cfg.risk_per_trade_pct, self.cfg.leverage, symbol_filters
            )
        except (InsufficientSize, ValueError) as exc:
            log.info("Ordre annule - %s", exc)
            return None
        except BinanceError as exc:
            log.error("Lecture du solde impossible: %s", exc)
            return None

        try:
            await self._ensure_leverage(plan.symbol)
            entry = await self.client.new_order(
                symbol=plan.symbol,
                side=plan.entry_side,
                type="MARKET",
                quantity=plan.quantity,
            )
            log.info("[REEL] Entree %s -> orderId=%s", plan.describe(), entry.get("orderId"))
        except BinanceError as exc:
            log.error("%s: entree refusee (%s)", plan.symbol, exc)
            return None

        await self._place_protection(plan)
        return plan

    async def _ensure_leverage(self, symbol: str) -> None:
        if symbol in self._leverage_set:
            return
        try:
            await self.client.set_leverage(symbol, self.cfg.leverage)
        except BinanceError as exc:
            log.warning("%s: levier non modifie (%s), levier du compte conserve.", symbol, exc)
        self._leverage_set.add(symbol)

    async def _place_protection(self, plan: OrderPlan) -> None:
        """Stop et objectif en reduceOnly. Un echec ici est journalise, pas fatal."""
        try:
            await self.client.new_order(
                symbol=plan.symbol,
                side=plan.exit_side,
                type="STOP_MARKET",
                stopPrice=plan.stop_loss,
                closePosition="true",
                workingType="MARK_PRICE",
            )
        except BinanceError as exc:
            log.error(
                "%s: STOP LOSS NON PLACE (%s) - position non protegee, verifiez manuellement !",
                plan.symbol,
                exc,
            )

        if plan.take_profit is None:
            return
        try:
            await self.client.new_order(
                symbol=plan.symbol,
                side=plan.exit_side,
                type="TAKE_PROFIT_MARKET",
                stopPrice=plan.take_profit,
                closePosition="true",
                workingType="MARK_PRICE",
            )
        except BinanceError as exc:
            log.error("%s: take profit non place (%s)", plan.symbol, exc)
