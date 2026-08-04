"""Tests du backtest: chargement des donnees et simulation des trades."""

from __future__ import annotations

import json

import pytest

from bot.config import Config
from bot.models import Candle, Series, Side, Signal
from tools.backtest import (
    Stats,
    Trade,
    load,
    load_binance_json,
    load_csv,
    report,
    run,
    simulate_trade,
)

from .conftest import make_series, ramp


def bar(high: float, low: float, close: float, index: int = 0) -> Candle:
    return Candle(
        open_time=index * 60_000,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        close_time=index * 60_000 + 59_999,
        quote_volume=close,
    )


def signal_long(price: float = 100.0, stop: float = 98.0, target: float = 104.0) -> Signal:
    return Signal(
        symbol="TEST",
        side=Side.LONG,
        interval="1h",
        price=price,
        score=2,
        reasons=["test"],
        stop_loss=stop,
        take_profit=target,
        atr=1.0,
    )


def signal_short(price: float = 100.0, stop: float = 102.0, target: float = 96.0) -> Signal:
    return Signal(
        symbol="TEST",
        side=Side.SHORT,
        interval="1h",
        price=price,
        score=2,
        reasons=["test"],
        stop_loss=stop,
        take_profit=target,
        atr=1.0,
    )


def serie_de(barres: list[Candle]) -> Series:
    return Series("TEST", "1h", barres)


# ------------------------------------------------------------------ Simulation


def test_objectif_atteint_donne_le_ratio_complet():
    barres = [bar(100, 100, 100), bar(105, 99, 104)]  # touche 104 sans casser 98
    trade = simulate_trade(serie_de(barres), signal_long(), entry_index=0, max_bars=10)
    assert trade.outcome == "objectif"
    assert trade.r_multiple == pytest.approx(2.0)
    assert trade.gagnant


def test_stop_touche_coute_un_r():
    barres = [bar(100, 100, 100), bar(101, 97, 97.5)]
    trade = simulate_trade(serie_de(barres), signal_long(), entry_index=0, max_bars=10)
    assert trade.outcome == "stop"
    assert trade.r_multiple == pytest.approx(-1.0)
    assert not trade.gagnant


def test_bougie_touchant_les_deux_niveaux_compte_comme_un_stop():
    """Hypothese pessimiste: on ne connait pas l'ordre des touches dans la bougie."""
    barres = [bar(100, 100, 100), bar(105, 97, 101)]
    trade = simulate_trade(serie_de(barres), signal_long(), entry_index=0, max_bars=10)
    assert trade.outcome == "stop"


def test_expiration_valorise_a_la_cloture():
    barres = [bar(100, 100, 100), bar(101, 99.5, 101), bar(101.5, 100, 101)]
    trade = simulate_trade(serie_de(barres), signal_long(), entry_index=0, max_bars=2)
    assert trade.outcome == "expire"
    # sortie a 101 sur un risque de 2 -> +0.5 R
    assert trade.r_multiple == pytest.approx(0.5)


def test_short_symetrique():
    touche_objectif = [bar(100, 100, 100), bar(101, 95, 96)]
    trade = simulate_trade(serie_de(touche_objectif), signal_short(), entry_index=0, max_bars=10)
    assert trade.outcome == "objectif"
    assert trade.r_multiple == pytest.approx(2.0)

    touche_stop = [bar(100, 100, 100), bar(103, 99, 102.5)]
    trade = simulate_trade(serie_de(touche_stop), signal_short(), entry_index=0, max_bars=10)
    assert trade.outcome == "stop"


def test_max_bars_borne_la_detention():
    barres = [bar(100, 100, 100)] + [bar(100.5, 99.5, 100) for _ in range(20)]
    trade = simulate_trade(serie_de(barres), signal_long(), entry_index=0, max_bars=3)
    assert trade.exit_index == 3


def test_simulation_ne_depasse_pas_la_fin_de_serie():
    barres = [bar(100, 100, 100), bar(100.5, 99.5, 100)]
    trade = simulate_trade(serie_de(barres), signal_long(), entry_index=0, max_bars=99)
    assert trade.exit_index == 1


# ----------------------------------------------------------------- Statistiques


def test_stats_agregent_correctement():
    gagnant = Trade(signal_long(), 0, 1, 104.0, "objectif", 2.0)
    perdant = Trade(signal_long(), 2, 3, 98.0, "stop", -1.0)
    stats = Stats(trades=[gagnant, perdant, perdant], bars=100, evaluated=50)
    assert stats.total == 3
    assert stats.gagnants == 1
    assert stats.taux_reussite == pytest.approx(33.33, abs=0.01)
    assert stats.total_r == pytest.approx(0.0)
    assert stats.par_issue() == {"objectif": 1, "stop": 2}


def test_frais_convertis_en_r_selon_la_largeur_du_stop():
    """Un stop a 2% encaisse 0.08/2 = 0.04 R de frais; un stop a 0.2% en encaisse 0.4."""
    large = Trade(signal_long(100.0, 98.0, 104.0), 0, 1, 104.0, "objectif", 2.0)
    serre = Trade(signal_long(100.0, 99.8, 100.4), 0, 1, 100.4, "objectif", 2.0)

    stats_large = Stats([large], bars=10, evaluated=5, fee_pct_aller_retour=0.08)
    stats_serre = Stats([serre], bars=10, evaluated=5, fee_pct_aller_retour=0.08)

    assert stats_large.cout_en_r(large) == pytest.approx(0.04)
    assert stats_serre.cout_en_r(serre) == pytest.approx(0.40)
    assert stats_large.esperance_r_nette > stats_serre.esperance_r_nette


def test_sans_frais_le_net_egale_le_brut():
    trade = Trade(signal_long(), 0, 1, 104.0, "objectif", 2.0)
    stats = Stats([trade], bars=10, evaluated=5, fee_pct_aller_retour=0.0)
    assert stats.total_r_net == pytest.approx(stats.total_r)


def test_stats_vides_ne_divisent_pas_par_zero():
    stats = Stats([], bars=10, evaluated=5, fee_pct_aller_retour=0.08)
    assert stats.taux_reussite == 0.0
    assert stats.esperance_r == 0.0
    assert stats.esperance_r_nette == 0.0
    assert stats.cout_moyen_r == 0.0


# ------------------------------------------------------------------ Chargement


def test_load_csv_lit_les_colonnes_sans_tenir_compte_de_la_casse(tmp_path):
    fichier = tmp_path / "data.csv"
    fichier.write_text("Date,OPEN,High,low,Close,Volume\n2024-01-01,1,3,0.5,2,100\n")
    series = load_csv(fichier, "X", "1h")
    assert len(series) == 1
    bougie = series.candles[0]
    assert (bougie.open, bougie.high, bougie.low, bougie.close) == (1.0, 3.0, 0.5, 2.0)


def test_load_csv_ignore_les_lignes_vides(tmp_path):
    fichier = tmp_path / "data.csv"
    fichier.write_text("open,high,low,close\n1,2,0.5,1.5\n\n2,3,1.5,2.5\n")
    assert len(load_csv(fichier, "X", "1h")) == 2


def test_load_csv_signale_une_colonne_manquante(tmp_path):
    fichier = tmp_path / "data.csv"
    fichier.write_text("open,high,close\n1,2,1.5\n")
    with pytest.raises(ValueError, match="low"):
        load_csv(fichier, "X", "1h")


def test_load_csv_signale_une_ligne_illisible(tmp_path):
    fichier = tmp_path / "data.csv"
    fichier.write_text("open,high,low,close\n1,2,0.5,abc\n")
    with pytest.raises(ValueError, match="ligne 2"):
        load_csv(fichier, "X", "1h")


def test_load_json_klines_binance(tmp_path):
    fichier = tmp_path / "k.json"
    rows = [[1000, "10", "12", "9", "11", "100", 1999, "1100", 5, "50", "550", "0"]]
    fichier.write_text(json.dumps(rows))
    series = load_binance_json(fichier, "BTCUSDT", "15m")
    assert series.candles[0].close == 11.0
    assert series.candles[0].quote_volume == 1100.0


def test_load_json_rejette_un_format_inattendu(tmp_path):
    fichier = tmp_path / "k.json"
    fichier.write_text(json.dumps({"pas": "une liste"}))
    with pytest.raises(ValueError, match="klines"):
        load_binance_json(fichier, "X", "15m")


def test_load_choisit_le_format_par_extension(tmp_path):
    csv_file = tmp_path / "a.csv"
    csv_file.write_text("open,high,low,close\n1,2,0.5,1.5\n")
    json_file = tmp_path / "b.json"
    json_file.write_text(json.dumps([[1, "1", "2", "0.5", "1.5", "1", 2, "1.5", 1, "1", "1", "0"]]))
    assert len(load(csv_file, "X", "1h")) == 1
    assert len(load(json_file, "X", "1h")) == 1


# --------------------------------------------------------------------- Bout en bout


def test_run_sur_une_serie_sans_signal():
    cfg = Config(min_score=2)
    stats = run(make_series([100.0] * 150, spread=0.0), cfg)
    assert stats.total == 0
    assert stats.evaluated > 0
    assert "Aucun signal" in report("plat", stats, cfg)


def test_run_produit_des_trades_sur_un_retournement():
    cfg = Config(min_score=1)
    stats = run(make_series(ramp(200, 120, 100) + ramp(120, 175, 40)), cfg, fee_pct=0.08)
    assert stats.total > 0
    assert all(t.outcome in {"objectif", "stop", "expire"} for t in stats.trades)
    texte = report("retournement", stats, cfg)
    assert "Signaux:" in texte and "Net    :" in texte


def test_cooldown_bars_espace_les_entrees():
    cfg = Config(min_score=1)
    series = make_series(ramp(200, 120, 100) + ramp(120, 175, 40))
    serre = run(series, cfg, cooldown_bars=0)
    espace = run(series, cfg, cooldown_bars=30)
    assert espace.total <= serre.total
