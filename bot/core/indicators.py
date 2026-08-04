"""Indicateurs techniques en Python pur (aucune dependance).

Toutes les fonctions renvoient une liste de la meme longueur que l'entree,
remplie de `None` tant que l'indicateur n'a pas assez d'historique. Cela permet
de lire directement `values[-1]` et de comparer avec `values[-2]`.
"""

from __future__ import annotations

Number = float | None


def sma(values: list[float], period: int) -> list[Number]:
    """Moyenne mobile simple."""
    if period <= 0:
        raise ValueError("period doit etre > 0")
    out: list[Number] = [None] * len(values)
    if len(values) < period:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: list[float], period: int) -> list[Number]:
    """Moyenne mobile exponentielle, amorcee par une SMA."""
    if period <= 0:
        raise ValueError("period doit etre > 0")
    out: list[Number] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[Number]:
    """RSI avec lissage de Wilder."""
    if period <= 0:
        raise ValueError("period doit etre > 0")
    out: list[Number] = [None] * len(values)
    if len(values) <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[i] = _rsi_from_averages(avg_gain, avg_loss)
    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def stddev(values: list[float], period: int) -> list[Number]:
    """Ecart-type glissant (population), aligne sur la fin de fenetre."""
    out: list[Number] = [None] * len(values)
    if period <= 0:
        raise ValueError("period doit etre > 0")
    if len(values) < period:
        return out
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((v - mean) ** 2 for v in window) / period
        out[i] = var**0.5
    return out


def bollinger(
    values: list[float], period: int = 20, num_std: float = 2.0
) -> tuple[list[Number], list[Number], list[Number]]:
    """Bandes de Bollinger -> (superieure, moyenne, inferieure)."""
    mid = sma(values, period)
    sd = stddev(values, period)
    upper: list[Number] = [None] * len(values)
    lower: list[Number] = [None] * len(values)
    for i, (m, s) in enumerate(zip(mid, sd)):
        if m is None or s is None:
            continue
        upper[i] = m + num_std * s
        lower[i] = m - num_std * s
    return upper, mid, lower


def true_range(highs: list[float], lows: list[float], closes: list[float]) -> list[Number]:
    """True Range. Le premier element est None (pas de cloture precedente)."""
    _check_ohlc(highs, lows, closes)
    out: list[Number] = [None] * len(closes)
    for i in range(1, len(closes)):
        prev_close = closes[i - 1]
        out[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
    return out


def atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> list[Number]:
    """Average True Range (lissage de Wilder)."""
    if period <= 0:
        raise ValueError("period doit etre > 0")
    tr = true_range(highs, lows, closes)
    out: list[Number] = [None] * len(closes)
    if len(closes) <= period:
        return out
    seed_window = [v for v in tr[1 : period + 1] if v is not None]
    if len(seed_window) < period:
        return out
    prev = sum(seed_window) / period
    out[period] = prev
    for i in range(period + 1, len(closes)):
        current = tr[i]
        if current is None:
            continue
        prev = (prev * (period - 1) + current) / period
        out[i] = prev
    return out


def crossed_above(fast: list[Number], slow: list[Number]) -> bool:
    """La serie rapide vient-elle de passer au-dessus de la lente ?"""
    a, b = _last_two_pairs(fast, slow)
    if a is None or b is None:
        return False
    (prev_f, prev_s), (cur_f, cur_s) = a, b
    return prev_f <= prev_s and cur_f > cur_s


def crossed_below(fast: list[Number], slow: list[Number]) -> bool:
    """La serie rapide vient-elle de passer sous la lente ?"""
    a, b = _last_two_pairs(fast, slow)
    if a is None or b is None:
        return False
    (prev_f, prev_s), (cur_f, cur_s) = a, b
    return prev_f >= prev_s and cur_f < cur_s


def _last_two_pairs(
    fast: list[Number], slow: list[Number]
) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    if len(fast) < 2 or len(slow) < 2:
        return None, None
    prev_f, cur_f = fast[-2], fast[-1]
    prev_s, cur_s = slow[-2], slow[-1]
    if None in (prev_f, cur_f, prev_s, cur_s):
        return None, None
    return (prev_f, prev_s), (cur_f, cur_s)  # type: ignore[return-value]


def _check_ohlc(highs: list[float], lows: list[float], closes: list[float]) -> None:
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows et closes doivent avoir la meme longueur")
