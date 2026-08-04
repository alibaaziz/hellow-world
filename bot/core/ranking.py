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
    funding_rate: float | None = None  # dernier taux de funding (0.0001 = 0.01%)
    oi_change_pct: float | None = None  # variation d'Open Interest sur la fenetre

    @property
    def abs_price_change_pct(self) -> float:
        return abs(self.price_change_pct)

    @property
    def abs_funding_bps(self) -> float | None:
        """Funding en points de base absolus. Un funding extreme, positif ou
        negatif, signale un positionnement desequilibre: le sens importe moins
        que l'amplitude pour reperer une paire a surveiller."""
        return abs(self.funding_rate) * 10_000 if self.funding_rate is not None else None

    @property
    def abs_oi_change_pct(self) -> float | None:
        """Une chute d'Open Interest (debouclage) est aussi remarquable qu'une
        hausse (argent frais): on classe sur l'amplitude."""
        return abs(self.oi_change_pct) if self.oi_change_pct is not None else None


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
        oi = f"{m.oi_change_pct:>+6.1f}%" if m.oi_change_pct is not None else "     -"
        funding = f"{m.funding_rate * 100:>+6.3f}%" if m.funding_rate is not None else "      -"
        return (
            f"{self.symbol:<12} {self.score:>5.1f}  x{m.volume_ratio:>5.2f}  "
            f"{m.price_change_pct:>+6.2f}%  x{m.atr_expansion:>4.2f}  "
            f"{oi}  {funding}  {sens}"
        )


def open_interest_change_pct(rows: list[dict], lookback: int) -> float | None:
    """Variation d'Open Interest en % entre la fenetre et maintenant.

    `rows` est la reponse de /futures/data/openInterestHist, du plus ancien au
    plus recent. Renvoie None si les donnees sont absentes ou inexploitables.
    """
    valeurs: list[float] = []
    for row in rows:
        try:
            valeurs.append(float(row["sumOpenInterest"]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(valeurs) < 2:
        return None
    reference = valeurs[max(0, len(valeurs) - 1 - lookback)]
    if reference <= 0:
        return None
    return (valeurs[-1] - reference) / reference * 100


def compute_metrics(
    series: Series,
    cfg: Config,
    signal: Signal | None = None,
    funding_rate: float | None = None,
    oi_rows: list[dict] | None = None,
) -> PairMetrics | None:
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
        funding_rate=funding_rate,
        oi_change_pct=(
            open_interest_change_pct(oi_rows, cfg.oi_lookback) if oi_rows is not None else None
        ),
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


def percentile_ranks_optional(values: list[float | None]) -> list[float] | None:
    """Rangs centiles tolerants aux trous.

    Les paires sans donnee recoivent 50 : ne pas savoir ne doit ni avantager ni
    penaliser une paire. Renvoie None si personne n'a la donnee, pour que la
    composante soit retiree de la ponderation au lieu de tout aplatir a 50.
    """
    presents = [(i, v) for i, v in enumerate(values) if v is not None]
    if not presents:
        return None
    rangs_presents = percentile_ranks([v for _, v in presents])
    resultat = [50.0] * len(values)
    for (i, _), rang in zip(presents, rangs_presents):
        resultat[i] = rang
    return resultat


def rank_pairs(metrics: list[PairMetrics], cfg: Config) -> list[RankedPair]:
    """Classe les paires, la plus interessante en premier."""
    if not metrics:
        return []

    composantes: dict[str, list[float]] = {
        "volume": percentile_ranks([m.volume_ratio for m in metrics]),
        "momentum": percentile_ranks([m.abs_price_change_pct for m in metrics]),
        "volatilite": percentile_ranks([m.atr_expansion for m in metrics]),
        "extreme": percentile_ranks([abs(m.range_position - 0.5) for m in metrics]),
    }
    # Funding et Open Interest peuvent etre desactives ou indisponibles: la
    # composante n'existe alors tout simplement pas, et les poids se
    # renormalisent sur celles qui restent.
    for nom, valeurs in (
        ("funding", [m.abs_funding_bps for m in metrics]),
        ("open_interest", [m.abs_oi_change_pct for m in metrics]),
    ):
        rangs = percentile_ranks_optional(valeurs)
        if rangs is not None:
            composantes[nom] = rangs
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
        f"{'PAIRE':<12} {'SCORE':>5}  {'VOL':>6}  {'VARIAT':>7}  {'ATR':>5}  "
        f"{'OI':>7}  {'FUNDING':>7}  SETUP",
        "-" * 74,
    ]
    lignes += [p.resume() for p in pairs[:limite]]
    return "\n".join(lignes)
