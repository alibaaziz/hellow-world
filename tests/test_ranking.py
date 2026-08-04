"""Tests du classement des paires."""

from __future__ import annotations

import pytest

from bot.config import Config
from bot.core.ranking import (
    PairMetrics,
    compute_metrics,
    format_table,
    open_interest_change_pct,
    percentile_ranks,
    percentile_ranks_optional,
    rank_pairs,
)
from bot.models import Candle, Series, Side, Signal

from .conftest import make_series, ramp


def serie_avec_volumes(closes: list[float], volumes: list[float], symbol="TESTUSDT") -> Series:
    base = make_series(closes, symbol=symbol)
    candles = [
        Candle(
            open_time=c.open_time,
            open=c.open,
            high=c.high,
            low=c.low,
            close=c.close,
            volume=v,
            close_time=c.close_time,
            quote_volume=v * c.close,
        )
        for c, v in zip(base.candles, volumes)
    ]
    return Series(symbol=symbol, interval="15m", candles=candles)


def metrics(symbol="X", volume_ratio=1.0, change=0.0, atr_exp=1.0, pos=0.5, signal=None,
            funding=None, oi=None):
    return PairMetrics(
        symbol=symbol,
        price=100.0,
        volume_ratio=volume_ratio,
        price_change_pct=change,
        atr_expansion=atr_exp,
        range_position=pos,
        signal=signal,
        funding_rate=funding,
        oi_change_pct=oi,
    )


# ------------------------------------------------------------- Rangs centiles


def test_percentile_ranks_borne_a_0_et_100():
    rangs = percentile_ranks([5.0, 1.0, 3.0])
    assert min(rangs) == 0.0
    assert max(rangs) == 100.0
    assert rangs == [100.0, 0.0, 50.0]


def test_percentile_ranks_moyenne_les_ex_aequo():
    # trois valeurs identiques: meme rang pour toutes
    rangs = percentile_ranks([2.0, 2.0, 2.0])
    assert rangs == [50.0, 50.0, 50.0]


def test_percentile_ranks_cas_degeneres():
    assert percentile_ranks([]) == []
    assert percentile_ranks([42.0]) == [50.0]


def test_percentile_ranks_ordre_preserve():
    valeurs = [0.1, 9.9, 3.3, 7.7]
    rangs = percentile_ranks(valeurs)
    apparies = sorted(zip(valeurs, rangs))
    assert [r for _, r in apparies] == sorted(r for _, r in apparies)


# ----------------------------------------------------------------- Mesures


def test_compute_metrics_detecte_une_anomalie_de_volume():
    cfg = Config(ranking_lookback=20)
    closes = ramp(100, 110, 120)
    volumes = [100.0] * 119 + [800.0]  # derniere bougie a 8x le volume habituel
    m = compute_metrics(serie_avec_volumes(closes, volumes), cfg)
    assert m is not None
    assert m.volume_ratio == pytest.approx(8.0)


def test_compute_metrics_mesure_le_momentum():
    cfg = Config(ranking_lookback=20)
    # +10% exactement sur les 20 dernieres bougies
    closes = [100.0] * 100 + ramp(100, 110, 21)
    m = compute_metrics(serie_avec_volumes(closes, [10.0] * len(closes)), cfg)
    assert m is not None
    assert m.price_change_pct == pytest.approx(10.0)


def test_compute_metrics_position_dans_le_range():
    cfg = Config(ranking_lookback=20)
    m_haut = compute_metrics(make_series(ramp(100, 150, 120)), cfg)
    m_bas = compute_metrics(make_series(ramp(150, 100, 120)), cfg)
    assert m_haut is not None and m_bas is not None
    assert m_haut.range_position > 0.8  # colle aux plus hauts
    assert m_bas.range_position < 0.2  # colle aux plus bas


def test_compute_metrics_refuse_un_historique_court():
    cfg = Config(ranking_lookback=20)
    assert compute_metrics(make_series(ramp(100, 110, 10)), cfg) is None


def test_compute_metrics_survit_a_un_volume_nul():
    """Un symbole sans volume ne doit pas faire exploser le calcul."""
    cfg = Config(ranking_lookback=20)
    closes = ramp(100, 110, 120)
    m = compute_metrics(serie_avec_volumes(closes, [0.0] * 120), cfg)
    assert m is not None
    assert m.volume_ratio == 0.0


def test_abs_price_change_ignore_le_sens():
    assert metrics(change=-7.0).abs_price_change_pct == 7.0
    assert metrics(change=7.0).abs_price_change_pct == 7.0


# ---------------------------------------------------------------- Classement


def test_rank_pairs_met_l_anomalie_en_tete():
    cfg = Config()
    lot = [
        metrics("CALME", volume_ratio=1.0, change=0.1, atr_exp=1.0),
        metrics("MOYEN", volume_ratio=2.0, change=1.0, atr_exp=1.2),
        metrics("CHAUD", volume_ratio=9.0, change=8.0, atr_exp=3.0),
    ]
    classement = rank_pairs(lot, cfg)
    assert [p.symbol for p in classement] == ["CHAUD", "MOYEN", "CALME"]
    assert classement[0].score > classement[-1].score


def test_rank_pairs_donne_un_score_continu():
    """C'est tout l'interet face au score entier des signaux: peu d'ex aequo."""
    cfg = Config()
    lot = [metrics(f"P{i}", volume_ratio=i * 1.3, change=i * 0.7, atr_exp=1 + i * 0.1)
           for i in range(1, 21)]
    scores = [p.score for p in rank_pairs(lot, cfg)]
    assert len(set(scores)) > 15  # les paires se departagent vraiment
    assert all(0.0 <= s <= 100.0 for s in scores)


def test_rank_pairs_expose_le_detail():
    cfg = Config()
    classement = rank_pairs([metrics("A", volume_ratio=5.0), metrics("B")], cfg)
    detail = classement[0].components
    assert set(detail) == {"volume", "momentum", "volatilite", "extreme", "setup"}


def test_un_setup_technique_fait_monter_la_paire():
    cfg = Config()
    signal = Signal(
        symbol="AVEC", side=Side.LONG, interval="15m", price=100.0, score=2, reasons=["x"]
    )
    avec = metrics("AVEC", volume_ratio=2.0, signal=signal)
    sans = metrics("SANS", volume_ratio=2.0)
    classement = {p.symbol: p.score for p in rank_pairs([avec, sans], cfg)}
    assert classement["AVEC"] > classement["SANS"]


def test_rank_pairs_gere_une_liste_vide_ou_unique():
    cfg = Config()
    assert rank_pairs([], cfg) == []
    seul = rank_pairs([metrics("SEUL")], cfg)
    assert len(seul) == 1 and 0.0 <= seul[0].score <= 100.0


def test_poids_personnalises_changent_le_classement():
    volumineux = metrics("VOLUME", volume_ratio=9.0, change=0.1)
    mobile = metrics("MOMENTUM", volume_ratio=1.0, change=9.0)
    par_volume = Config(ranking_weights={"volume": 1.0})
    par_momentum = Config(ranking_weights={"momentum": 1.0})
    assert rank_pairs([volumineux, mobile], par_volume)[0].symbol == "VOLUME"
    assert rank_pairs([volumineux, mobile], par_momentum)[0].symbol == "MOMENTUM"


def test_poids_nuls_ne_divisent_pas_par_zero():
    classement = rank_pairs([metrics("A"), metrics("B")], Config(ranking_weights={}))
    assert all(p.score == 0.0 for p in classement)


# ------------------------------------------------------------------ Affichage


def test_format_table_liste_les_paires():
    cfg = Config()
    classement = rank_pairs([metrics("BTCUSDT", volume_ratio=5.0), metrics("ETHUSDT")], cfg)
    texte = format_table(classement, limite=10)
    assert "BTCUSDT" in texte and "ETHUSDT" in texte
    assert "PAIRE" in texte


def test_format_table_respecte_la_limite():
    cfg = Config()
    classement = rank_pairs([metrics(f"P{i}", volume_ratio=float(i)) for i in range(10)], cfg)
    texte = format_table(classement, limite=3)
    assert len(texte.splitlines()) == 5  # entete + separateur + 3 lignes


def test_format_table_sans_paire():
    assert "Aucune paire" in format_table([])


# ------------------------------------------------- Open Interest et funding


def oi_rows(valeurs: list[float]) -> list[dict]:
    return [
        {"symbol": "X", "sumOpenInterest": str(v), "sumOpenInterestValue": str(v * 100),
         "timestamp": 1_583_127_900_000 + i * 900_000}
        for i, v in enumerate(valeurs)
    ]


def test_open_interest_change_calcule_la_variation():
    # 100 -> 130 sur la fenetre demandee
    assert open_interest_change_pct(oi_rows([100.0, 110.0, 130.0]), lookback=2) == pytest.approx(30.0)


def test_open_interest_change_detecte_un_debouclage():
    assert open_interest_change_pct(oi_rows([200.0, 150.0]), lookback=1) == pytest.approx(-25.0)


def test_open_interest_change_limite_la_fenetre_a_l_historique():
    """Un lookback plus grand que l'historique remonte au plus ancien disponible."""
    assert open_interest_change_pct(oi_rows([100.0, 200.0]), lookback=99) == pytest.approx(100.0)


def test_open_interest_change_donnees_inexploitables():
    assert open_interest_change_pct([], lookback=8) is None
    assert open_interest_change_pct(oi_rows([100.0]), lookback=8) is None  # un seul point
    assert open_interest_change_pct(oi_rows([0.0, 50.0]), lookback=1) is None  # reference nulle
    assert open_interest_change_pct([{"pas": "le bon champ"}], lookback=8) is None


def test_compute_metrics_integre_funding_et_oi():
    cfg = Config(ranking_lookback=20, oi_lookback=2)
    series = make_series(ramp(100, 110, 120))
    m = compute_metrics(series, cfg, funding_rate=0.0005, oi_rows=oi_rows([100.0, 110.0, 125.0]))
    assert m is not None
    assert m.funding_rate == 0.0005
    assert m.abs_funding_bps == pytest.approx(5.0)  # 0.05% = 5 points de base
    assert m.oi_change_pct == pytest.approx(25.0)


def test_metriques_optionnelles_absentes_par_defaut():
    cfg = Config(ranking_lookback=20)
    m = compute_metrics(make_series(ramp(100, 110, 120)), cfg)
    assert m is not None
    assert m.funding_rate is None and m.oi_change_pct is None
    assert m.abs_funding_bps is None and m.abs_oi_change_pct is None


def test_funding_negatif_compte_comme_extreme():
    """Un funding tres negatif signale autant qu'un tres positif."""
    assert metrics(funding=-0.0009).abs_funding_bps == pytest.approx(9.0)
    assert metrics(funding=0.0009).abs_funding_bps == pytest.approx(9.0)


def test_percentile_ranks_optional_neutralise_les_trous():
    rangs = percentile_ranks_optional([5.0, None, 1.0])
    assert rangs is not None
    assert rangs[1] == 50.0  # la paire sans donnee n'est ni avantagee ni penalisee
    assert rangs[0] == 100.0 and rangs[2] == 0.0


def test_percentile_ranks_optional_sans_aucune_donnee():
    assert percentile_ranks_optional([None, None]) is None


def test_composante_absente_est_retiree_de_la_ponderation():
    """Sans funding ni OI, le score reste sur 100 au lieu d'etre dilue."""
    cfg = Config()
    signal = Signal(
        symbol="FORTE", side=Side.LONG, interval="15m", price=100.0, score=2, reasons=["x"]
    )
    lot = [metrics("FORTE", volume_ratio=9.0, change=9.0, atr_exp=3.0, pos=1.0, signal=signal),
           metrics("FAIBLE", volume_ratio=1.0, change=0.0, atr_exp=1.0, pos=0.5)]
    classement = rank_pairs(lot, cfg)
    assert "funding" not in classement[0].components
    assert "open_interest" not in classement[0].components
    # Premiere sur toutes les composantes disponibles: le score sature a 100
    # parce que les poids se renormalisent sur celles qui restent.
    assert classement[0].score == pytest.approx(100.0)


def test_open_interest_fait_monter_une_paire():
    cfg = Config()
    forte = metrics("OIFORT", oi=40.0)
    faible = metrics("OIFAIBLE", oi=0.5)
    classement = {p.symbol: p.score for p in rank_pairs([forte, faible], cfg)}
    assert classement["OIFORT"] > classement["OIFAIBLE"]


def test_funding_extreme_fait_monter_une_paire():
    cfg = Config()
    extreme = metrics("CHAUD", funding=0.0025)
    neutre = metrics("CALME", funding=0.00001)
    classement = {p.symbol: p.score for p in rank_pairs([extreme, neutre], cfg)}
    assert classement["CHAUD"] > classement["CALME"]


def test_paire_sans_oi_n_est_pas_penalisee_face_a_une_paire_moyenne():
    """Une paire sans donnee d'OI doit se retrouver au milieu, pas derniere."""
    cfg = Config(ranking_weights={"open_interest": 1.0})
    lot = [metrics("HAUT", oi=50.0), metrics("INCONNU"), metrics("BAS", oi=0.1)]
    scores = {p.symbol: p.score for p in rank_pairs(lot, cfg)}
    assert scores["HAUT"] > scores["INCONNU"] > scores["BAS"]


def test_format_table_affiche_oi_et_funding():
    cfg = Config()
    classement = rank_pairs([metrics("BTCUSDT", oi=12.5, funding=0.0003)], cfg)
    texte = format_table(classement)
    assert "OI" in texte and "FUNDING" in texte
    assert "+12.5%" in texte
    assert "+0.030%" in texte  # 0.0003 rendu en pourcentage


def test_format_table_affiche_un_tiret_sans_donnee():
    cfg = Config()
    texte = format_table(rank_pairs([metrics("BTCUSDT")], cfg))
    assert "BTCUSDT" in texte
