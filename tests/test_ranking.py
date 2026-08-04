"""Tests du classement des paires."""

from __future__ import annotations

import pytest

from bot.config import Config
from bot.core.ranking import (
    PairMetrics,
    compute_metrics,
    format_table,
    percentile_ranks,
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


def metrics(symbol="X", volume_ratio=1.0, change=0.0, atr_exp=1.0, pos=0.5, signal=None):
    return PairMetrics(
        symbol=symbol,
        price=100.0,
        volume_ratio=volume_ratio,
        price_change_pct=change,
        atr_expansion=atr_exp,
        range_position=pos,
        signal=signal,
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
