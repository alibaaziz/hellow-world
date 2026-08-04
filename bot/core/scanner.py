"""Boucle de scan: univers de symboles -> bougies -> signaux -> alertes / execution."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ..config import Config
from ..exchange.binance_futures import BinanceError, BinanceFuturesClient
from ..execution.base import OrderRouter, SymbolFilters
from ..models import Signal
from ..notify.base import Notifier
from .ranking import PairMetrics, RankedPair, compute_metrics, format_table, rank_pairs
from . import signals as strategy

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    signals: list[Signal] = field(default_factory=list)
    ranking: list[RankedPair] = field(default_factory=list)
    scanned: int = 0
    errors: int = 0
    duration: float = 0.0


class Scanner:
    def __init__(
        self,
        cfg: Config,
        client: BinanceFuturesClient,
        notifiers: list[Notifier],
        router: OrderRouter | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.notifiers = notifiers
        self.router = router
        self.filters: dict[str, SymbolFilters] = {}
        self.rank_display = 0  # nombre de paires du classement a afficher (0 = aucun)
        self._universe: list[str] = []
        self._universe_loaded_at: float = 0.0
        self._last_alert: dict[tuple[str, str, str], float] = {}

    # ------------------------------------------------------------- Univers

    async def universe(self, force: bool = False) -> list[str]:
        """Symboles a scanner, rafraichis periodiquement."""
        age_limit = self.cfg.universe_refresh_minutes * 60
        if not force and self._universe and time.monotonic() - self._universe_loaded_at < age_limit:
            return self._universe

        entries = await self.client.perpetual_symbols()
        self.filters = {e["symbol"]: SymbolFilters.from_exchange_info(e) for e in entries}
        available = {e["symbol"] for e in entries}

        if self.cfg.symbols:
            selected = [s for s in self.cfg.symbols if s in available]
            missing = [s for s in self.cfg.symbols if s not in available]
            if missing:
                log.warning("Symboles ignores (absents des perpetuels): %s", ", ".join(missing))
        else:
            tickers = await self.client.ticker_24h()
            liquid = [
                s
                for s in available
                if float(tickers.get(s, {}).get("quoteVolume", 0.0))
                >= self.cfg.min_quote_volume_24h
            ]
            liquid.sort(
                key=lambda s: float(tickers.get(s, {}).get("quoteVolume", 0.0)), reverse=True
            )
            selected = liquid

        if self.cfg.max_symbols > 0:
            selected = selected[: self.cfg.max_symbols]

        self._universe = selected
        self._universe_loaded_at = time.monotonic()
        log.info("Univers: %d symboles suivis (%s)", len(selected), self.cfg.interval)
        if self.cfg.open_interest_enabled and len(selected) > 50:
            log.warning(
                "Open Interest actif sur %d symboles: %d requetes supplementaires par cycle "
                "(une par symbole, toutes les %ds). Limitez avec --top ou espacez les cycles "
                "pour eviter un blocage temporaire de l'IP.",
                len(selected),
                len(selected),
                self.cfg.scan_interval_seconds,
            )
        return selected

    # ---------------------------------------------------------------- Scan

    async def scan_once(self) -> ScanResult:
        started = time.monotonic()
        result = ScanResult()
        symbols = await self.universe()

        funding = await self._funding_rates()

        async def analyse(symbol: str) -> tuple[Signal | None, PairMetrics | None]:
            try:
                series = await self.client.klines(symbol, self.cfg.interval, self.cfg.candles)
            except BinanceError as exc:
                log.warning("%s: bougies indisponibles (%s)", symbol, exc)
                result.errors += 1
                return None, None
            result.scanned += 1
            signal = strategy.evaluate(series, self.cfg)
            metrics = compute_metrics(
                series,
                self.cfg,
                signal,
                funding_rate=funding.get(symbol),
                oi_rows=await self._open_interest(symbol),
            )
            return signal, metrics

        found = await asyncio.gather(*(analyse(s) for s in symbols))

        mesures: list[PairMetrics] = []
        for signal, metrics in found:
            if metrics is not None:
                mesures.append(metrics)
            # Le cooldown ne filtre que les alertes: le classement, lui, doit
            # continuer a refleter l'etat reel du marche a chaque cycle.
            if signal is not None and self._accept(signal):
                result.signals.append(signal)

        result.signals.sort(key=lambda s: s.score, reverse=True)
        result.ranking = rank_pairs(mesures, self.cfg)
        result.duration = time.monotonic() - started
        return result

    async def _funding_rates(self) -> dict[str, float]:
        """Funding de tout l'univers. Une panne ici ne doit pas casser le scan."""
        if not self.cfg.funding_enabled:
            return {}
        try:
            return await self.client.funding_rates()
        except BinanceError as exc:
            log.warning("Funding indisponible ce cycle (%s), classement sans cette mesure.", exc)
            return {}

    async def _open_interest(self, symbol: str) -> list[dict] | None:
        """Historique d'Open Interest d'un symbole, ou None si indisponible."""
        if not self.cfg.open_interest_enabled:
            return None
        period = self.client.oi_period_for(self.cfg.interval)
        try:
            rows = await self.client.open_interest_hist(symbol, period, self.cfg.oi_lookback + 1)
        except BinanceError as exc:
            log.debug("%s: Open Interest indisponible (%s)", symbol, exc)
            return None
        return rows or None

    def _accept(self, signal: Signal) -> bool:
        """Anti-spam: un meme symbole/sens ne realerte pas avant la fin du cooldown."""
        key = signal.key()
        now = time.monotonic()
        last = self._last_alert.get(key)
        if last is not None and now - last < self.cfg.cooldown_minutes * 60:
            return False
        self._last_alert[key] = now
        return True

    async def dispatch(self, signals: list[Signal]) -> None:
        for signal in signals:
            for notifier in self.notifiers:
                try:
                    await notifier.send(signal)
                except Exception:  # une alerte ratee ne doit pas casser le scan
                    log.exception("Canal %s en echec", notifier.name)
            if self.router is not None:
                try:
                    await self.router.submit(signal)
                except Exception:
                    log.exception("Routeur %s en echec sur %s", self.router.name, signal.symbol)

    async def run_forever(self) -> None:
        log.info(
            "Scan toutes les %ds en %s (score min %d)",
            self.cfg.scan_interval_seconds,
            self.cfg.interval,
            self.cfg.min_score,
        )
        while True:
            try:
                result = await self.scan_once()
                log.info(
                    "%d symboles analyses en %.1fs - %d signal(aux)%s",
                    result.scanned,
                    result.duration,
                    len(result.signals),
                    f" - {result.errors} erreur(s)" if result.errors else "",
                )
                if self.rank_display:
                    print(f"\n{format_table(result.ranking, self.rank_display)}\n", flush=True)
                await self.dispatch(result.signals)
            except BinanceError as exc:
                log.error("Cycle interrompu: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erreur inattendue pendant le scan")
            await asyncio.sleep(self.cfg.scan_interval_seconds)
