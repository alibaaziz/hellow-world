"""Interface commune des canaux d'alerte."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Signal


class Notifier(ABC):
    """Un canal de sortie pour les signaux detectes."""

    name: str = "notifier"

    @abstractmethod
    async def send(self, signal: Signal) -> None:
        """Envoie un signal. Ne doit jamais lever: une alerte ratee n'arrete pas le scan."""

    async def close(self) -> None:
        """Libere les ressources eventuelles."""


def format_signal(signal: Signal) -> str:
    """Rendu texte commun a tous les canaux."""
    arrow = "LONG" if signal.side.value == "LONG" else "SHORT"
    lines = [
        f"{arrow} {signal.symbol} ({signal.interval}) - score {signal.score}",
        f"Prix       : {_fmt(signal.price)}",
    ]
    if signal.stop_loss is not None:
        risk = signal.risk_pct
        suffix = f"  ({risk:.2f}% de risque)" if risk is not None else ""
        lines.append(f"Stop loss  : {_fmt(signal.stop_loss)}{suffix}")
    if signal.take_profit is not None:
        lines.append(f"Take profit: {_fmt(signal.take_profit)}")
    lines.append("Raisons    :")
    lines.extend(f"  - {reason}" for reason in signal.reasons)
    lines.append(f"Detecte a  : {signal.detected_at:%Y-%m-%d %H:%M:%S} UTC")
    return "\n".join(lines)


def _fmt(value: float) -> str:
    """Formatage adapte aux prix crypto (de 0.00001234 a 95000)."""
    if value == 0:
        return "0"
    if abs(value) >= 100:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:.4f}"
    return f"{value:.8f}".rstrip("0")
