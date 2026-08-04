import pytest

from bot.core import indicators as ta


def test_sma_aligne_et_padde():
    values = [1, 2, 3, 4, 5]
    result = ta.sma(values, 3)
    assert result[:2] == [None, None]
    assert result[2:] == pytest.approx([2.0, 3.0, 4.0])


def test_sma_historique_insuffisant():
    assert ta.sma([1, 2], 5) == [None, None]


def test_ema_amorce_sur_sma():
    values = [float(v) for v in range(1, 11)]
    result = ta.ema(values, 5)
    assert result[3] is None
    assert result[4] == pytest.approx(3.0)  # SMA des 5 premiers
    # 6 * (2/6) + 3 * (4/6) = 4.0
    assert result[5] == pytest.approx(4.0)


def test_ema_suit_une_serie_constante():
    result = ta.ema([7.0] * 20, 10)
    assert result[-1] == pytest.approx(7.0)


def test_rsi_serie_croissante_est_a_100():
    values = [float(v) for v in range(1, 30)]
    assert ta.rsi(values, 14)[-1] == pytest.approx(100.0)


def test_rsi_serie_decroissante_est_a_zero():
    values = [float(v) for v in range(30, 1, -1)]
    assert ta.rsi(values, 14)[-1] == pytest.approx(0.0)


def test_rsi_reference_wilder():
    """Serie de reference publiee pour le RSI 14 (lissage de Wilder).

    L'ecart tolere absorbe les arrondis de la table publiee (2 decimales).
    """
    closes = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
        45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
        46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45,
        45.78, 45.35, 44.03, 44.18, 44.22, 44.57, 43.42, 42.66, 43.13,
    ]
    expected = [
        70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38,
        54.71, 50.42, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77,
    ]
    result = ta.rsi(closes, 14)
    assert result[:14] == [None] * 14
    assert result[14:] == pytest.approx(expected, abs=0.1)


def test_rsi_borne_entre_0_et_100():
    closes = [100 + (i % 7) * 1.3 - (i % 3) * 0.9 for i in range(60)]
    for value in ta.rsi(closes, 14):
        if value is not None:
            assert 0.0 <= value <= 100.0


def test_bollinger_bandes_centrees_sur_la_sma():
    closes = [10.0, 12.0, 11.0, 13.0, 12.0, 14.0, 13.0, 15.0]
    upper, mid, lower = ta.bollinger(closes, 4, 2.0)
    assert mid[-1] == pytest.approx(sum(closes[-4:]) / 4)
    assert upper[-1] > mid[-1] > lower[-1]
    assert upper[-1] - mid[-1] == pytest.approx(mid[-1] - lower[-1])


def test_bollinger_serie_plate_a_bandes_nulles():
    upper, mid, lower = ta.bollinger([5.0] * 25, 20, 2.0)
    assert upper[-1] == pytest.approx(5.0)
    assert lower[-1] == pytest.approx(5.0)


def test_true_range_prend_le_gap():
    highs = [10.0, 12.0]
    lows = [9.0, 11.5]
    closes = [9.5, 11.8]
    # gap haussier: |high - cloture precedente| = 12 - 9.5 = 2.5 domine high-low = 0.5
    assert ta.true_range(highs, lows, closes)[1] == pytest.approx(2.5)


def test_atr_positif_et_padde():
    highs = [10 + i * 0.5 for i in range(40)]
    lows = [9 + i * 0.5 for i in range(40)]
    closes = [9.5 + i * 0.5 for i in range(40)]
    result = ta.atr(highs, lows, closes, 14)
    assert result[13] is None
    assert result[-1] is not None and result[-1] > 0


def test_atr_refuse_des_longueurs_incoherentes():
    with pytest.raises(ValueError):
        ta.atr([1.0, 2.0], [1.0], [1.0, 2.0], 14)


def test_crossed_above_detecte_le_croisement():
    assert ta.crossed_above([1.0, 3.0], [2.0, 2.0]) is True
    assert ta.crossed_above([3.0, 4.0], [2.0, 2.0]) is False  # deja au-dessus
    assert ta.crossed_below([3.0, 1.0], [2.0, 2.0]) is True


def test_croisement_ignore_les_valeurs_manquantes():
    assert ta.crossed_above([None, 3.0], [2.0, 2.0]) is False
    assert ta.crossed_above([3.0], [2.0]) is False


@pytest.mark.parametrize("func", [ta.sma, ta.ema, ta.rsi, ta.stddev])
def test_periode_invalide(func):
    with pytest.raises(ValueError):
        func([1.0, 2.0, 3.0], 0)
