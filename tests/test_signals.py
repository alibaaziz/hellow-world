from dataclasses import replace

import pytest

from bot.config import Config
from bot.core import signals as strategy
from bot.models import Series, Side, Signal

from .conftest import make_series, ramp


def snapshot(**overrides) -> strategy.Snapshot:
    """Snapshot neutre: aucune regle ne se declenche tant qu'on ne surcharge rien."""
    base = strategy.Snapshot(
        close=100.0,
        rsi=50.0,
        prev_rsi=50.0,
        ema_fast=[100.0, 100.0],
        ema_slow=[100.0, 100.0],
        ema_trend=100.0,
        bb_upper=110.0,
        bb_mid=100.0,
        bb_lower=90.0,
        prev_bb_width=20.0,
        bb_width=20.0,
        atr=2.0,
    )
    return replace(base, **overrides)


def walk(closes: list[float], cfg: Config, symbol: str = "TESTUSDT") -> list[Signal]:
    """Evalue la serie bougie par bougie, comme le ferait le scanner en direct."""
    series = make_series(closes, symbol=symbol)
    found = []
    for end in range(cfg.min_required_candles, len(series) + 1):
        window = Series(symbol=series.symbol, interval=series.interval, candles=series.candles[:end])
        signal = strategy.evaluate(window, cfg)
        if signal is not None:
            found.append(signal)
    return found


# --------------------------------------------------------------- Snapshot


def test_pas_de_snapshot_si_historique_insuffisant(cfg: Config):
    series = make_series(ramp(100, 110, 20))
    assert strategy.build_snapshot(series, cfg) is None
    assert strategy.evaluate(series, cfg) is None


def test_snapshot_calcule_sur_historique_suffisant(cfg: Config):
    series = make_series(ramp(100, 130, 120))
    snap = strategy.build_snapshot(series, cfg)
    assert snap is not None
    assert snap.close == series.last.close
    assert 0 <= snap.rsi <= 100
    assert snap.bb_upper > snap.bb_mid > snap.bb_lower
    assert snap.atr > 0


# ------------------------------------------------------------- Regles unitaires


def test_regle_ema_cross(cfg: Config):
    haussier = snapshot(ema_fast=[99.0, 101.0], ema_slow=[100.0, 100.0])
    assert strategy._rule_ema_cross(haussier, cfg)[0] is Side.LONG

    baissier = snapshot(ema_fast=[101.0, 99.0], ema_slow=[100.0, 100.0])
    assert strategy._rule_ema_cross(baissier, cfg)[0] is Side.SHORT

    deja_au_dessus = snapshot(ema_fast=[101.0, 102.0], ema_slow=[100.0, 100.0])
    assert strategy._rule_ema_cross(deja_au_dessus, cfg) is None


def test_regle_rsi_reversal(cfg: Config):
    sortie_survente = snapshot(prev_rsi=28.0, rsi=33.0)
    assert strategy._rule_rsi_reversal(sortie_survente, cfg)[0] is Side.LONG

    sortie_surachat = snapshot(prev_rsi=72.0, rsi=68.0)
    assert strategy._rule_rsi_reversal(sortie_surachat, cfg)[0] is Side.SHORT

    # Toujours en survente: pas encore de rebond confirme.
    assert strategy._rule_rsi_reversal(snapshot(prev_rsi=25.0, rsi=27.0), cfg) is None


def test_regle_bollinger_exige_une_expansion(cfg: Config):
    cassure_en_expansion = snapshot(close=115.0, prev_bb_width=18.0, bb_width=20.0)
    assert strategy._rule_bollinger_breakout(cassure_en_expansion, cfg)[0] is Side.LONG

    cassure_en_contraction = snapshot(close=115.0, prev_bb_width=22.0, bb_width=20.0)
    assert strategy._rule_bollinger_breakout(cassure_en_contraction, cfg) is None

    cassure_basse = snapshot(close=85.0, prev_bb_width=18.0, bb_width=20.0)
    assert strategy._rule_bollinger_breakout(cassure_basse, cfg)[0] is Side.SHORT


def test_regle_tendance(cfg: Config):
    assert strategy._rule_trend_filter(snapshot(close=110.0, ema_trend=100.0), cfg)[0] is Side.LONG
    assert strategy._rule_trend_filter(snapshot(close=90.0, ema_trend=100.0), cfg)[0] is Side.SHORT


# --------------------------------------------------------------- Agregation


def test_marche_plat_ne_declenche_rien(cfg: Config):
    assert walk([100.0] * 150, cfg) == []


def test_retournement_haussier_donne_des_longs(cfg: Config):
    """Longue baisse puis reprise: le scanner doit finir par sortir un LONG."""
    signaux = walk(ramp(200, 120, 100) + ramp(120, 175, 40), cfg)
    assert signaux, "aucun signal detecte sur un retournement haussier net"
    longs = [s for s in signaux if s.side is Side.LONG]
    assert longs
    assert all(s.score >= cfg.min_score for s in signaux)
    assert all(s.reasons for s in signaux)


def test_retournement_baissier_donne_des_shorts(cfg: Config):
    signaux = walk(ramp(120, 200, 100) + ramp(200, 145, 40), cfg)
    assert signaux
    assert any(s.side is Side.SHORT for s in signaux)


def test_niveaux_coherents_en_long(cfg: Config):
    longs = [s for s in walk(ramp(200, 120, 100) + ramp(120, 175, 40), cfg) if s.side is Side.LONG]
    assert longs
    for signal in longs:
        assert signal.stop_loss < signal.price < signal.take_profit
        risque = signal.price - signal.stop_loss
        gain = signal.take_profit - signal.price
        assert gain == pytest.approx(cfg.risk_reward * risque)
        assert signal.risk_pct is not None and signal.risk_pct > 0


def test_niveaux_coherents_en_short(cfg: Config):
    shorts = [
        s for s in walk(ramp(120, 200, 100) + ramp(200, 145, 40), cfg) if s.side is Side.SHORT
    ]
    assert shorts
    for signal in shorts:
        assert signal.take_profit < signal.price < signal.stop_loss


def test_score_minimal_filtre_les_signaux_faibles():
    closes = ramp(200, 120, 100) + ramp(120, 175, 40)
    permissifs = walk(closes, Config(min_score=1))
    exigeants = walk(closes, Config(min_score=99))
    assert permissifs
    assert exigeants == []
    assert len(permissifs) >= len(walk(closes, Config(min_score=3)))


def test_declencheurs_et_confirmations_ne_se_confondent_pas():
    """Le filtre de tendance confirme mais ne declenche jamais seul."""
    cfg = Config(min_score=1)
    # Tendance haussiere etablie, aucun declencheur actif sur la derniere bougie.
    snap = snapshot(close=110.0, ema_trend=100.0)
    assert all(rule(snap, cfg) is None for rule in strategy.active_rules(cfg))
    assert strategy._rule_trend_filter(snap, cfg)[0] is Side.LONG
    # Meme avec le seuil le plus permissif, une hausse reguliere n'alerte jamais:
    # le filtre de tendance voterait LONG en continu, mais il ne declenche pas.
    assert walk(ramp(100, 400, 200), cfg) == []


def test_signaux_contradictoires_sont_ignores(cfg: Config):
    """Autant de votes LONG que SHORT: on s'abstient."""
    snap = snapshot(
        close=100.0,
        ema_trend=100.0,  # aucune confirmation (close == ema_trend)
        ema_fast=[99.0, 101.0],  # declencheur LONG
        ema_slow=[100.0, 100.0],
        prev_rsi=72.0,  # declencheur SHORT
        rsi=68.0,
    )
    assert strategy._rule_ema_cross(snap, cfg)[0] is Side.LONG
    assert strategy._rule_rsi_reversal(snap, cfg)[0] is Side.SHORT
    assert strategy._rule_trend_filter(snap, cfg) is None


def test_signal_key_pour_deduplication(cfg: Config):
    signaux = walk(ramp(200, 120, 100) + ramp(120, 175, 40), cfg, symbol="BTCUSDT")
    assert signaux
    symbole, interval, sens = signaux[0].key()
    assert symbole == "BTCUSDT"
    assert interval == "15m"
    assert sens in {"LONG", "SHORT"}
