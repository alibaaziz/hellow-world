"""Classement des paires: quelles paires meritent l'attention maintenant ?

Le score d'un `Signal` vaut 2 dans 93 % des cas, il ne classe donc rien. Ici on
note chaque paire sur un score **continu** de 0 a 100, obtenu en comparant les
paires entre elles plutot qu'a des seuils absolus.

Chaque composante est convertie en rang centile sur l'univers scanne. C'est ce
qui rend le score comparable d'une paire a l'autre : un volume a 8 fois sa
moyenne n'a pas la meme portee sur BTCUSDT que sur un altcoin, mais « premiere
paire de l'univers en anomalie de volume » a le meme sens partout.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..models import Series, Signal
from . import indicators as ta


@dataclass(frozen=True)
class PairMetrics:
    """Mesures brutes d'une paire sur la derniere bougie fermee."""

    symbol: str
    price: float
    volume_ratio: float  # volume de la derniere bougie / moyenne des precedentes
    price_change_pct: float  # variation sur la fenetre de momentum
    atr_expansion: float  # ATR courant / ATR moyen
    range_position: float  # position dans le range recent, 0 = plus bas, 1 = plus haut
    signal: Signal | None = None

    @property
    def abs_price_change_pct(self) -> float:
        return abs(self.price_change_pct)


@dataclass
class RankedPair:
    """Une paire notee, avec le detail de ce qui la fait monter."""

    symbol: str
    score: float  # 0 a 100
    metrics: PairMetrics
    components: dict[str, float] = field(default_factory=dict)

    @property
    def signal(self) -> Signal | None:
        return self.metrics.signal

    def resume(self) -> str:
        m = self.metrics
        sens = m.signal.side.value if m.signal else "-"
        return (
            f"{self.symbol:<14} {self.score:>5.1f}  vol x{m.volume_ratio:>5.2f}  "
            f"var {m.price_change_pct:>+6.2f}%  ATR x{m.atr_expansion:>4.2f}  "
            f"pos {m.range_position:>4.2f}  {sens}"
        )


def compute_metrics(series: Series, cfg: Config, signal: Signal | None = None) -> PairMetrics | None:
    """Mesure une paire. Renvoie None si l'historique est insuffisant."""
    fenetre = cfg.ranking_lookback
    if len(series) < max(fenetre + 1, cfg.min_required_candles):
        return None

    closes = series.closes
    volumes = series.volumes

    # Anomalie de volume: derniere bougie contre la moyenne des precedentes.
    precedents = volumes[-fenetre - 1 : -1]
    moyenne_volume = sum(precedents) / len(precedents) if precedents else 0.0
    volume_ratio = volumes[-1] / moyenne_volume if moyenne_volume > 0 else 0.0

    # Momentum sur la fenetre.
    reference = closes[-fenetre - 1]
    price_change_pct = (closes[-1] - reference) / reference * 100 if reference else 0.0

    # Expansion de volatilite: ATR courant contre sa propre moyenne.
    atr_values = [v for v in ta.atr(series.highs, series.lows, closes, cfg.atr_period) if v]
    recents = atr_values[-fenetre:] if atr_values else []
    moyenne_atr = sum(recents) / len(recents) if recents else 0.0
    atr_expansion = atr_values[-1] / moyenne_atr if atr_values and moyenne_atr > 0 else 0.0

    # Position dans le range recent: proche de 1 = colle aux plus hauts.
    hauts = series.highs[-fenetre:]
    bas = series.lows[-fenetre:]
    plus_haut, plus_bas = max(hauts), min(bas)
    etendue = plus_haut - plus_bas
    range_position = (closes[-1] - plus_bas) / etendue if etendue > 0 else 0.5

    return PairMetrics(
        symbol=series.symbol,
        price=closes[-1],
        volume_ratio=volume_ratio,
        price_change_pct=price_change_pct,
        atr_expansion=atr_expansion,
        range_position=range_position,
        signal=signal,
    )


def percentile_ranks(values: list[float]) -> list[float]:
    """Convertit des valeurs en rangs centiles (0 a 100), ex aequo moyennes.

    Une seule valeur ne peut se comparer a rien: elle vaut 50 par convention.
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [50.0]

    ordre = sorted(range(n), key=lambda i: values[i])
    rangs = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[ordre[j + 1]] == values[ordre[i]]:
            j += 1
        rang_moyen = (i + j) / 2  # ex aequo: meme rang, la moyenne du groupe
        for k in range(i, j + 1):
            rangs[ordre[k]] = rang_moyen / (n - 1) * 100
        i = j + 1
    return rangs


def rank_pairs(metrics: list[PairMetrics], cfg: Config) -> list[RankedPair]:
    """Classe les paires, la plus interessante en premier."""
    if not metrics:
        return []

    composantes = {
        "volume": percentile_ranks([m.volume_ratio for m in metrics]),
        "momentum": percentile_ranks([m.abs_price_change_pct for m in metrics]),
        "volatilite": percentile_ranks([m.atr_expansion for m in metrics]),
        "extreme": percentile_ranks([abs(m.range_position - 0.5) for m in metrics]),
    }
    poids = cfg.ranking_weights

    classees = []
    for i, m in enumerate(metrics):
        detail = {nom: valeurs[i] for nom, valeurs in composantes.items()}
        # Le setup technique est un bonus binaire: il ne se compare pas entre
        # paires, il indique juste qu'une regle vient de se declencher.
        detail["setup"] = 100.0 if m.signal is not None else 0.0
        total = sum(detail[nom] * poids.get(nom, 0.0) for nom in detail)
        somme_poids = sum(poids.get(nom, 0.0) for nom in detail)
        score = total / somme_poids if somme_poids > 0 else 0.0
        classees.append(RankedPair(symbol=m.symbol, score=score, metrics=m, components=detail))

    classees.sort(key=lambda p: p.score, reverse=True)
    return classees


def format_table(pairs: list[RankedPair], limite: int = 10) -> str:
    """Rendu texte du classement."""
    if not pairs:
        return "Aucune paire classee."
    lignes = [
        f"{'PAIRE':<14} {'SCORE':>5}  {'VOLUME':>9}  {'VARIATION':>11}  "
        f"{'ATR':>7}  {'POS':>7}  SETUP",
        "-" * 78,
    ]
    lignes += [p.resume() for p in pairs[:limite]]
    return "\n".join(lignes)
