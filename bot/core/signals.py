"""Regles de detection: transforment une serie de bougies en `Signal`.

Chaque regle vote LONG ou SHORT. Le score d'un signal est le nombre de regles
qui vont dans le meme sens; seuls les scores >= `Config.min_score` sortent.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..models import Series, Side, Signal
from . import indicators as ta


@dataclass(frozen=True)
class Snapshot:
    """Valeurs d'indicateurs sur la derniere bougie fermee."""

    close: float
    rsi: float
    prev_rsi: float
    ema_fast: list[ta.Number]
    ema_slow: list[ta.Number]
    ema_trend: float
    bb_upper: float
    bb_mid: float
    bb_lower: float
    prev_bb_width: float
    bb_width: float
    atr: float


def build_snapshot(series: Series, cfg: Config) -> Snapshot | None:
    """Calcule les indicateurs; renvoie None si l'historique est insuffisant."""
    if len(series) < cfg.min_required_candles:
        return None

    closes = series.closes
    rsi_values = ta.rsi(closes, cfg.rsi_period)
    ema_fast = ta.ema(closes, cfg.ema_fast)
    ema_slow = ta.ema(closes, cfg.ema_slow)
    ema_trend = ta.ema(closes, cfg.ema_trend)
    upper, mid, lower = ta.bollinger(closes, cfg.bb_period, cfg.bb_std)
    atr_values = ta.atr(series.highs, series.lows, closes, cfg.atr_period)

    required = [
        rsi_values[-1],
        rsi_values[-2],
        ema_trend[-1],
        upper[-1],
        mid[-1],
        lower[-1],
        upper[-2],
        lower[-2],
        atr_values[-1],
    ]
    if any(v is None for v in required):
        return None

    return Snapshot(
        close=closes[-1],
        rsi=rsi_values[-1],  # type: ignore[arg-type]
        prev_rsi=rsi_values[-2],  # type: ignore[arg-type]
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        ema_trend=ema_trend[-1],  # type: ignore[arg-type]
        bb_upper=upper[-1],  # type: ignore[arg-type]
        bb_mid=mid[-1],  # type: ignore[arg-type]
        bb_lower=lower[-1],  # type: ignore[arg-type]
        prev_bb_width=upper[-2] - lower[-2],  # type: ignore[operator]
        bb_width=upper[-1] - lower[-1],  # type: ignore[operator]
        atr=atr_values[-1],  # type: ignore[arg-type]
    )


def _rule_ema_cross(snap: Snapshot, cfg: Config) -> tuple[Side, str] | None:
    """Croisement EMA rapide / EMA lente sur la derniere bougie."""
    if ta.crossed_above(snap.ema_fast, snap.ema_slow):
        return Side.LONG, f"Croisement EMA{cfg.ema_fast} au-dessus de EMA{cfg.ema_slow}"
    if ta.crossed_below(snap.ema_fast, snap.ema_slow):
        return Side.SHORT, f"Croisement EMA{cfg.ema_fast} sous EMA{cfg.ema_slow}"
    return None


def _rule_rsi_reversal(snap: Snapshot, cfg: Config) -> tuple[Side, str] | None:
    """Sortie de zone de survente / surachat (rebond confirme)."""
    if snap.prev_rsi <= cfg.rsi_oversold < snap.rsi:
        return Side.LONG, f"RSI sort de la survente ({snap.prev_rsi:.1f} -> {snap.rsi:.1f})"
    if snap.prev_rsi >= cfg.rsi_overbought > snap.rsi:
        return Side.SHORT, f"RSI sort du surachat ({snap.prev_rsi:.1f} -> {snap.rsi:.1f})"
    return None


def _rule_bollinger_breakout(snap: Snapshot, cfg: Config) -> tuple[Side, str] | None:
    """Cassure d'une bande de Bollinger avec expansion de volatilite."""
    expanding = snap.bb_width > snap.prev_bb_width
    if not expanding:
        return None
    if snap.close > snap.bb_upper:
        return Side.LONG, "Cassure au-dessus de la bande de Bollinger superieure"
    if snap.close < snap.bb_lower:
        return Side.SHORT, "Cassure sous la bande de Bollinger inferieure"
    return None


def _rule_trend_filter(snap: Snapshot, cfg: Config) -> tuple[Side, str] | None:
    """Filtre de tendance: position du prix par rapport a l'EMA longue."""
    if snap.close > snap.ema_trend:
        return Side.LONG, f"Prix au-dessus de EMA{cfg.ema_trend} (tendance haussiere)"
    if snap.close < snap.ema_trend:
        return Side.SHORT, f"Prix sous EMA{cfg.ema_trend} (tendance baissiere)"
    return None


# Le filtre de tendance ne suffit jamais seul: il confirme, il ne declenche pas.
TRIGGER_RULES = (_rule_ema_cross, _rule_rsi_reversal, _rule_bollinger_breakout)
CONFIRM_RULES = (_rule_trend_filter,)


def evaluate(series: Series, cfg: Config) -> Signal | None:
    """Evalue une serie et renvoie un signal si le score atteint le seuil."""
    snap = build_snapshot(series, cfg)
    if snap is None:
        return None
    return decide(snap, series.symbol, series.interval, cfg)


def decide(snap: Snapshot, symbol: str, interval: str, cfg: Config) -> Signal | None:
    """Applique les regles a un snapshot deja calcule.

    Separe de `evaluate` pour que le backtest puisse precalculer les indicateurs
    une seule fois tout en passant par exactement la meme logique de decision.
    """
    triggers: dict[Side, list[str]] = {Side.LONG: [], Side.SHORT: []}
    confirms: dict[Side, list[str]] = {Side.LONG: [], Side.SHORT: []}

    for rule in TRIGGER_RULES:
        result = rule(snap, cfg)
        if result is not None:
            side, reason = result
            triggers[side].append(reason)
    if not (triggers[Side.LONG] or triggers[Side.SHORT]):
        return None

    for rule in CONFIRM_RULES:
        result = rule(snap, cfg)
        if result is not None:
            side, reason = result
            confirms[side].append(reason)

    scores = {s: len(triggers[s]) + len(confirms[s]) for s in Side}
    if scores[Side.LONG] == scores[Side.SHORT]:
        return None  # signaux contradictoires: on s'abstient

    side = max(scores, key=lambda s: scores[s])
    # Une confirmation de tendance seule n'est pas un declencheur valable.
    if not triggers[side]:
        return None
    score = scores[side]
    if score < cfg.min_score:
        return None
    reasons = triggers[side] + confirms[side]

    stop, target = _levels(snap, side, cfg)
    return Signal(
        symbol=symbol,
        side=side,
        interval=interval,
        price=snap.close,
        score=score,
        reasons=reasons,
        stop_loss=stop,
        take_profit=target,
        atr=snap.atr,
    )


def _levels(snap: Snapshot, side: Side, cfg: Config) -> tuple[float, float]:
    """Stop base sur l'ATR et objectif derive du ratio risque/rendement."""
    distance = snap.atr * cfg.atr_stop_multiplier
    if side is Side.LONG:
        return snap.close - distance, snap.close + distance * cfg.risk_reward
    return snap.close + distance, snap.close - distance * cfg.risk_reward
