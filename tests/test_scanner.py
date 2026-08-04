"""Tests du scanner avec un faux client Binance: aucun appel reseau."""

from __future__ import annotations

import asyncio

import pytest

from bot.config import Config
from bot.core.scanner import Scanner
from bot.exchange.binance_futures import BinanceError
from bot.models import Series, Side, Signal
from bot.notify.base import Notifier, format_signal

from .conftest import make_series, ramp


class FakeClient:
    """Implemente juste ce que le scanner appelle."""

    def __init__(self, series_par_symbole: dict[str, Series], volumes: dict[str, float]) -> None:
        self.series = series_par_symbole
        self.volumes = volumes
        self.klines_calls: list[str] = []
        self.exchange_info_calls = 0
        self.failing: set[str] = set()
        self.funding: dict[str, float] = {}
        self.funding_calls = 0
        self.funding_fails = False
        self.oi_rows: dict[str, list[dict]] = {}
        self.oi_calls: list[str] = []

    async def funding_rates(self) -> dict[str, float]:
        self.funding_calls += 1
        if self.funding_fails:
            raise BinanceError("funding indisponible")
        return dict(self.funding)

    @staticmethod
    def oi_period_for(interval: str) -> str:
        return interval if interval in ("5m", "15m", "1h") else "15m"

    async def open_interest_hist(self, symbol: str, period: str, limit: int) -> list[dict]:
        self.oi_calls.append(symbol)
        if symbol in self.failing:
            raise BinanceError(f"{symbol}: OI indisponible")
        return self.oi_rows.get(symbol, [])

    async def perpetual_symbols(self) -> list[dict]:
        self.exchange_info_calls += 1
        return [
            {
                "symbol": s,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
            for s in self.series
        ]

    async def ticker_24h(self) -> dict[str, dict]:
        return {s: {"symbol": s, "quoteVolume": str(v)} for s, v in self.volumes.items()}

    async def klines(self, symbol: str, interval: str, limit: int) -> Series:
        self.klines_calls.append(symbol)
        if symbol in self.failing:
            raise BinanceError(f"{symbol}: erreur simulee")
        return self.series[symbol]


class RecordingNotifier(Notifier):
    name = "test"

    def __init__(self) -> None:
        self.sent: list[Signal] = []

    async def send(self, signal: Signal) -> None:
        self.sent.append(signal)


class BrokenNotifier(Notifier):
    name = "casse"

    async def send(self, signal: Signal) -> None:
        raise RuntimeError("canal indisponible")


# Serie calibree pour que le retournement haussier tombe sur la DERNIERE bougie:
# c'est la seule que le scanner evalue en direct.
REVERSAL = (ramp(200, 120, 100) + ramp(120, 175, 40))[:110]


def build_scanner(cfg: Config | None = None, **kwargs):
    cfg = cfg or Config(min_score=2, min_quote_volume_24h=1_000_000)
    series = {
        "BTCUSDT": make_series(REVERSAL, symbol="BTCUSDT"),
        "ETHUSDT": make_series([100.0] * 150, symbol="ETHUSDT"),
    }
    volumes = {"BTCUSDT": 5_000_000_000.0, "ETHUSDT": 2_000_000_000.0}
    client = FakeClient(series, volumes)
    notifier = RecordingNotifier()
    scanner = Scanner(cfg, client, [notifier], **kwargs)
    return scanner, client, notifier


def test_univers_filtre_par_liquidite():
    cfg = Config(min_quote_volume_24h=3_000_000_000)
    scanner, _client, _notifier = build_scanner(cfg)
    univers = asyncio.run(scanner.universe())
    assert univers == ["BTCUSDT"]  # ETHUSDT sous le seuil de volume


def test_univers_trie_par_volume_decroissant():
    scanner, _client, _ = build_scanner()
    assert asyncio.run(scanner.universe()) == ["BTCUSDT", "ETHUSDT"]


def test_univers_respecte_max_symbols():
    cfg = Config(min_quote_volume_24h=1_000_000, max_symbols=1)
    scanner, _client, _ = build_scanner(cfg)
    assert asyncio.run(scanner.universe()) == ["BTCUSDT"]


def test_univers_liste_imposee_ignore_les_inconnus():
    cfg = Config(symbols=["ETHUSDT", "INEXISTANTUSDT"])
    scanner, _client, _ = build_scanner(cfg)
    assert asyncio.run(scanner.universe()) == ["ETHUSDT"]


def test_univers_mis_en_cache():
    scanner, client, _ = build_scanner()
    asyncio.run(scanner.universe())
    asyncio.run(scanner.universe())
    assert client.exchange_info_calls == 1
    asyncio.run(scanner.universe(force=True))
    assert client.exchange_info_calls == 2


def test_univers_remplit_les_filtres():
    scanner, _client, _ = build_scanner()
    asyncio.run(scanner.universe())
    assert scanner.filters["BTCUSDT"].step_size == 0.001


def test_scan_analyse_tous_les_symboles():
    scanner, client, _ = build_scanner()
    result = asyncio.run(scanner.scan_once())
    assert result.scanned == 2
    assert sorted(client.klines_calls) == ["BTCUSDT", "ETHUSDT"]
    assert result.errors == 0


def test_scan_ignore_un_symbole_en_erreur():
    scanner, client, _ = build_scanner()
    client.failing = {"ETHUSDT"}
    result = asyncio.run(scanner.scan_once())
    assert result.errors == 1
    assert result.scanned == 1
    assert all(s.symbol != "ETHUSDT" for s in result.signals)


def test_cooldown_empeche_le_spam():
    cfg = Config(min_score=1, min_quote_volume_24h=1_000_000, cooldown_minutes=60)
    scanner, _client, _ = build_scanner(cfg)
    premier = asyncio.run(scanner.scan_once())
    second = asyncio.run(scanner.scan_once())
    assert premier.signals, "le premier scan doit produire un signal"
    assert second.signals == [], "le meme signal ne doit pas realerter pendant le cooldown"


def test_cooldown_expire_laisse_repasser():
    cfg = Config(min_score=1, min_quote_volume_24h=1_000_000, cooldown_minutes=0)
    scanner, _client, _ = build_scanner(cfg)
    assert asyncio.run(scanner.scan_once()).signals
    assert asyncio.run(scanner.scan_once()).signals


def test_signaux_tries_par_score():
    scanner, _client, _ = build_scanner()
    result = asyncio.run(scanner.scan_once())
    scores = [s.score for s in result.signals]
    assert scores == sorted(scores, reverse=True)


def test_dispatch_transmet_aux_canaux():
    cfg = Config(min_score=1, min_quote_volume_24h=1_000_000)
    scanner, _client, notifier = build_scanner(cfg)
    result = asyncio.run(scanner.scan_once())
    asyncio.run(scanner.dispatch(result.signals))
    assert len(notifier.sent) == len(result.signals) > 0


def test_un_canal_casse_n_empeche_pas_les_autres():
    cfg = Config(min_score=1, min_quote_volume_24h=1_000_000)
    scanner, _client, notifier = build_scanner(cfg)
    scanner.notifiers = [BrokenNotifier(), notifier]
    result = asyncio.run(scanner.scan_once())
    asyncio.run(scanner.dispatch(result.signals))  # ne doit pas lever
    assert len(notifier.sent) == len(result.signals) > 0


def test_dispatch_transmet_au_routeur():
    cfg = Config(min_score=1, min_quote_volume_24h=1_000_000)
    scanner, _client, _ = build_scanner(cfg)
    asyncio.run(scanner.universe())

    recus: list[Signal] = []

    class SpyRouter:
        name = "spy"

        async def submit(self, signal):
            recus.append(signal)
            return None

    scanner.router = SpyRouter()
    result = asyncio.run(scanner.scan_once())
    asyncio.run(scanner.dispatch(result.signals))
    assert recus == result.signals


def test_format_signal_lisible():
    signal = Signal(
        symbol="BTCUSDT",
        side=Side.LONG,
        interval="15m",
        price=50_000.0,
        score=3,
        reasons=["Croisement EMA9 au-dessus de EMA21"],
        stop_loss=49_000.0,
        take_profit=52_000.0,
        atr=500.0,
    )
    texte = format_signal(signal)
    assert "LONG BTCUSDT" in texte
    assert "50,000.00" in texte
    assert "Croisement EMA9" in texte
    assert "2.00% de risque" in texte


@pytest.mark.parametrize(
    "prix, attendu",
    [(95_000.0, "95,000.00"), (12.3456789, "12.3457"), (0.00001234, "0.00001234"), (0.0, "0")],
)
def test_format_prix_adapte_a_l_echelle(prix, attendu):
    from bot.notify.base import _fmt

    assert _fmt(prix) == attendu


# --------------------------------------------------- Funding et Open Interest


def test_funding_recupere_une_seule_fois_par_cycle():
    """Un seul appel pour tout l'univers: c'est ce qui rend la mesure gratuite."""
    scanner, client, _ = build_scanner()
    client.funding = {"BTCUSDT": 0.0005, "ETHUSDT": -0.0002}
    result = asyncio.run(scanner.scan_once())
    assert client.funding_calls == 1
    taux = {p.symbol: p.metrics.funding_rate for p in result.ranking}
    assert taux == {"BTCUSDT": 0.0005, "ETHUSDT": -0.0002}


def test_funding_desactivable():
    cfg = Config(min_quote_volume_24h=1_000_000, funding_enabled=False)
    scanner, client, _ = build_scanner(cfg)
    result = asyncio.run(scanner.scan_once())
    assert client.funding_calls == 0
    assert all(p.metrics.funding_rate is None for p in result.ranking)


def test_panne_de_funding_n_arrete_pas_le_scan():
    scanner, client, _ = build_scanner()
    client.funding_fails = True
    result = asyncio.run(scanner.scan_once())
    assert result.scanned == 2  # le scan continue
    assert all(p.metrics.funding_rate is None for p in result.ranking)


def test_open_interest_non_appele_par_defaut():
    """Une requete par symbole: desactive tant qu'on ne le demande pas."""
    scanner, client, _ = build_scanner()
    result = asyncio.run(scanner.scan_once())
    assert client.oi_calls == []
    assert all(p.metrics.oi_change_pct is None for p in result.ranking)


def test_open_interest_active_interroge_chaque_symbole():
    cfg = Config(min_quote_volume_24h=1_000_000, open_interest_enabled=True, oi_lookback=2)
    scanner, client, _ = build_scanner(cfg)
    client.oi_rows = {
        "BTCUSDT": [{"sumOpenInterest": v} for v in ("100", "110", "150")],
        "ETHUSDT": [{"sumOpenInterest": v} for v in ("200", "200", "200")],
    }
    result = asyncio.run(scanner.scan_once())
    assert sorted(client.oi_calls) == ["BTCUSDT", "ETHUSDT"]
    variations = {p.symbol: p.metrics.oi_change_pct for p in result.ranking}
    assert variations["BTCUSDT"] == pytest.approx(50.0)
    assert variations["ETHUSDT"] == pytest.approx(0.0)


def test_open_interest_manquant_sur_un_symbole():
    """Un symbole sans historique d'OI ne doit pas casser le classement."""
    cfg = Config(min_quote_volume_24h=1_000_000, open_interest_enabled=True, oi_lookback=2)
    scanner, client, _ = build_scanner(cfg)
    client.oi_rows = {"BTCUSDT": [{"sumOpenInterest": v} for v in ("100", "150")]}
    result = asyncio.run(scanner.scan_once())
    variations = {p.symbol: p.metrics.oi_change_pct for p in result.ranking}
    assert variations["BTCUSDT"] == pytest.approx(50.0)
    assert variations["ETHUSDT"] is None
    assert len(result.ranking) == 2


def test_classement_produit_a_chaque_cycle_malgre_le_cooldown():
    """Le cooldown museille les alertes, pas le classement."""
    cfg = Config(min_score=1, min_quote_volume_24h=1_000_000, cooldown_minutes=60)
    scanner, _client, _ = build_scanner(cfg)
    premier = asyncio.run(scanner.scan_once())
    second = asyncio.run(scanner.scan_once())
    assert second.signals == []
    assert len(second.ranking) == len(premier.ranking) == 2


def test_avertissement_si_open_interest_sur_un_gros_univers(caplog):
    """Une requete par symbole sur 300 paires peut faire bannir l'IP: on previent."""
    series = {f"P{i}USDT": make_series([100.0] * 150, symbol=f"P{i}USDT") for i in range(60)}
    volumes = {s: 1_000_000_000.0 for s in series}
    cfg = Config(min_quote_volume_24h=1_000, open_interest_enabled=True)
    scanner = Scanner(cfg, FakeClient(series, volumes), [])
    with caplog.at_level("WARNING"):
        asyncio.run(scanner.universe())
    assert any("Open Interest actif sur 60 symboles" in r.message for r in caplog.records)


def test_pas_d_avertissement_sans_open_interest(caplog):
    series = {f"P{i}USDT": make_series([100.0] * 150, symbol=f"P{i}USDT") for i in range(60)}
    volumes = {s: 1_000_000_000.0 for s in series}
    scanner = Scanner(Config(min_quote_volume_24h=1_000), FakeClient(series, volumes), [])
    with caplog.at_level("WARNING"):
        asyncio.run(scanner.universe())
    assert not any("Open Interest" in r.message for r in caplog.records)
