#!/usr/bin/env python3
"""Backtest walk-forward de la strategie sur des donnees OHLCV reelles.

Rejoue l'historique bougie par bougie: a chaque bougie fermee on appelle la
meme fonction `evaluate()` que le scanner en direct, puis on simule la suite du
trade sur les bougies suivantes (stop ou objectif touche en premier).

    python tools/backtest.py donnees.csv
    python tools/backtest.py klines.json --min-score 3 --max-bars 50

Formats acceptes:
  - CSV avec des colonnes Open/High/Low/Close/Volume (l'ordre importe peu)
  - JSON brut de /fapi/v1/klines (liste de listes), tel que renvoye par Binance
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config  # noqa: E402
from bot.core import indicators as ta  # noqa: E402
from bot.core import signals as strategy  # noqa: E402
from bot.models import Candle, Series, Side, Signal  # noqa: E402

REQUIRED = ("open", "high", "low", "close")


@dataclass
class Trade:
    signal: Signal
    entry_index: int
    exit_index: int
    exit_price: float
    outcome: str  # "objectif", "stop" ou "expire"
    r_multiple: float

    @property
    def gagnant(self) -> bool:
        return self.r_multiple > 0


@dataclass
class Stats:
    trades: list[Trade]
    bars: int
    evaluated: int
    fee_pct_aller_retour: float = 0.0

    @property
    def total(self) -> int:
        return len(self.trades)

    @property
    def risque_moyen_pct(self) -> float:
        """Distance moyenne au stop, en % du prix d'entree."""
        risques = [t.signal.risk_pct for t in self.trades if t.signal.risk_pct is not None]
        return sum(risques) / len(risques) if risques else 0.0

    def cout_en_r(self, trade: Trade) -> float:
        """Frais d'aller-retour convertis en multiples de R pour ce trade.

        Un stop serre encaisse proportionnellement plus de frais: c'est pour ca
        que le cout se calcule trade par trade et non sur la moyenne.
        """
        risque_pct = trade.signal.risk_pct
        if not risque_pct or not self.fee_pct_aller_retour:
            return 0.0
        return self.fee_pct_aller_retour / risque_pct

    @property
    def total_r_net(self) -> float:
        return sum(t.r_multiple - self.cout_en_r(t) for t in self.trades)

    @property
    def esperance_r_nette(self) -> float:
        return self.total_r_net / self.total if self.total else 0.0

    @property
    def cout_moyen_r(self) -> float:
        if not self.total:
            return 0.0
        return sum(self.cout_en_r(t) for t in self.trades) / self.total

    @property
    def gagnants(self) -> int:
        return sum(1 for t in self.trades if t.gagnant)

    @property
    def taux_reussite(self) -> float:
        return self.gagnants / self.total * 100 if self.total else 0.0

    @property
    def total_r(self) -> float:
        return sum(t.r_multiple for t in self.trades)

    @property
    def esperance_r(self) -> float:
        return self.total_r / self.total if self.total else 0.0

    def par_issue(self) -> dict[str, int]:
        compte: dict[str, int] = {}
        for trade in self.trades:
            compte[trade.outcome] = compte.get(trade.outcome, 0) + 1
        return compte


# ------------------------------------------------------------------ Chargement


def find_header(rows: list[list[str]], limite: int = 5) -> int:
    """Indice de la ligne d'en-tete, en sautant un eventuel preambule.

    Beaucoup d'exports reels (CryptoDataDownload par exemple) commencent par une
    ligne de commentaire avant l'en-tete.
    """
    for i, row in enumerate(rows[:limite]):
        cellules = [c.strip().lower() for c in row]
        if all(name in cellules for name in REQUIRED):
            return i
    entete = [c.strip().lower() for c in rows[0]] if rows else []
    manquantes = [c for c in REQUIRED if c not in entete]
    raise ValueError(f"colonnes manquantes {manquantes} (premiere ligne: {entete})")


def load_csv(path: Path, symbol: str, interval: str, reverse: bool = False) -> Series:
    """Lit un CSV OHLCV. Les noms de colonnes sont reconnus sans tenir compte de la casse.

    `reverse=True` pour les fichiers ordonnes du plus recent au plus ancien: la
    strategie a besoin d'un historique chronologique croissant.
    """
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise ValueError(f"{path}: fichier vide")

    try:
        header_at = find_header(rows)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc

    header = [c.strip().lower() for c in rows[header_at]]
    idx = {name: header.index(name) for name in REQUIRED}
    vol_idx = header.index("volume") if "volume" in header else None

    data_rows = rows[header_at + 1 :]
    if reverse:
        data_rows = list(reversed(data_rows))

    candles = []
    for line_no, row in enumerate(data_rows, start=2):
        if not any(cell.strip() for cell in row):
            continue
        try:
            values = {name: float(row[i]) for name, i in idx.items()}
            volume = float(row[vol_idx]) if vol_idx is not None else 0.0
        except (ValueError, IndexError) as exc:
            raise ValueError(f"{path}: ligne {line_no} illisible ({exc})") from exc
        open_time = (line_no - 2) * 60_000
        candles.append(
            Candle(
                open_time=open_time,
                open=values["open"],
                high=values["high"],
                low=values["low"],
                close=values["close"],
                volume=volume,
                close_time=open_time + 59_999,
                quote_volume=volume * values["close"],
            )
        )
    return Series(symbol=symbol, interval=interval, candles=candles)


def load_binance_json(path: Path, symbol: str, interval: str) -> Series:
    """Lit un export brut de /fapi/v1/klines (liste de listes)."""
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
        raise ValueError(f"{path}: attendu une liste de listes au format klines Binance")
    return Series(symbol=symbol, interval=interval, candles=[Candle.from_binance(r) for r in rows])


def load(path: Path, symbol: str, interval: str, reverse: bool = False) -> Series:
    if path.suffix.lower() == ".json":
        return load_binance_json(path, symbol, interval)
    return load_csv(path, symbol, interval, reverse=reverse)


# ------------------------------------------------------------------ Simulation


def simulate_trade(
    series: Series, signal: Signal, entry_index: int, max_bars: int
) -> Trade:
    """Rejoue les bougies suivantes jusqu'au stop, a l'objectif ou a l'expiration.

    Si une meme bougie touche le stop ET l'objectif, on retient le stop: on ne
    peut pas savoir dans quel ordre les niveaux ont ete atteints, et surestimer
    ses gains est la pire erreur d'un backtest.
    """
    assert signal.stop_loss is not None and signal.take_profit is not None
    risque = abs(signal.price - signal.stop_loss)
    dernier = min(entry_index + max_bars, len(series) - 1)

    for i in range(entry_index + 1, dernier + 1):
        bougie = series.candles[i]
        if signal.side is Side.LONG:
            stop_touche = bougie.low <= signal.stop_loss
            cible_touchee = bougie.high >= signal.take_profit
        else:
            stop_touche = bougie.high >= signal.stop_loss
            cible_touchee = bougie.low <= signal.take_profit

        if stop_touche:
            return Trade(signal, entry_index, i, signal.stop_loss, "stop", -1.0)
        if cible_touchee:
            gain = abs(signal.take_profit - signal.price)
            return Trade(signal, entry_index, i, signal.take_profit, "objectif", gain / risque)

    sortie = series.candles[dernier].close
    gain = (sortie - signal.price) if signal.side is Side.LONG else (signal.price - sortie)
    return Trade(signal, entry_index, dernier, sortie, "expire", gain / risque)


def iter_snapshots(series: Series, cfg: Config):
    """Produit (index, Snapshot) pour chaque bougie exploitable, en un seul passage.

    Tous les indicateurs sont causaux — la valeur a l'indice i ne depend que des
    bougies jusqu'a i — donc les calculer une fois sur la serie entiere donne
    exactement les memes nombres que de les recalculer sur chaque fenetre, mais
    en O(n) au lieu de O(n^2).
    """
    closes = series.closes
    rsi = ta.rsi(closes, cfg.rsi_period)
    ema_fast = ta.ema(closes, cfg.ema_fast)
    ema_slow = ta.ema(closes, cfg.ema_slow)
    ema_trend = ta.ema(closes, cfg.ema_trend)
    upper, mid, lower = ta.bollinger(closes, cfg.bb_period, cfg.bb_std)
    atr = ta.atr(series.highs, series.lows, closes, cfg.atr_period)

    for i in range(cfg.min_required_candles - 1, len(series)):
        requis = (rsi[i], rsi[i - 1], ema_trend[i], upper[i], mid[i], lower[i],
                  upper[i - 1], lower[i - 1], atr[i])
        if any(v is None for v in requis):
            continue
        yield i, strategy.Snapshot(
            close=closes[i],
            rsi=rsi[i],
            prev_rsi=rsi[i - 1],
            ema_fast=[ema_fast[i - 1], ema_fast[i]],
            ema_slow=[ema_slow[i - 1], ema_slow[i]],
            ema_trend=ema_trend[i],
            bb_upper=upper[i],
            bb_mid=mid[i],
            bb_lower=lower[i],
            prev_bb_width=upper[i - 1] - lower[i - 1],
            bb_width=upper[i] - lower[i],
            atr=atr[i],
        )


def run(
    series: Series,
    cfg: Config,
    max_bars: int = 40,
    cooldown_bars: int = 0,
    fee_pct: float = 0.0,
) -> Stats:
    """Parcourt l'historique et accumule les trades simules."""
    trades: list[Trade] = []
    evaluated = 0
    prochaine_entree_possible = 0

    for index, snap in iter_snapshots(series, cfg):
        signal = strategy.decide(snap, series.symbol, series.interval, cfg)
        evaluated += 1
        if signal is None or index < prochaine_entree_possible:
            continue
        if signal.stop_loss is None or signal.take_profit is None:
            continue
        trade = simulate_trade(series, signal, index, max_bars)
        trades.append(trade)
        prochaine_entree_possible = index + max(cooldown_bars, 1)

    return Stats(
        trades=trades,
        bars=len(series),
        evaluated=evaluated,
        fee_pct_aller_retour=fee_pct,
    )


# ------------------------------------------------------------------ Affichage


def report(nom: str, stats: Stats, cfg: Config) -> str:
    lignes = [
        f"=== {nom} ===",
        f"Bougies: {stats.bars}  |  fenetres evaluees: {stats.evaluated}  "
        f"|  score min: {cfg.min_score}",
    ]
    if not stats.total:
        lignes.append("Aucun signal declenche sur cet historique.")
        return "\n".join(lignes)

    issues = stats.par_issue()
    detail = "  ".join(f"{k}: {v}" for k, v in sorted(issues.items()))
    longs = sum(1 for t in stats.trades if t.signal.side is Side.LONG)
    seuil = 100 / (1 + cfg.risk_reward)  # taux de reussite d'equilibre au ratio configure
    lignes += [
        f"Signaux: {stats.total}  ({longs} longs / {stats.total - longs} shorts)",
        f"Issues : {detail}",
        f"Reussite: {stats.taux_reussite:.1f}%  (equilibre a {seuil:.1f}% pour un ratio "
        f"1:{cfg.risk_reward:g})",
        f"Brut   : {stats.total_r:+.2f} R cumule  |  esperance {stats.esperance_r:+.3f} R/trade",
    ]
    if stats.fee_pct_aller_retour:
        lignes += [
            f"Frais  : {stats.fee_pct_aller_retour:.3f}% aller-retour, stop moyen a "
            f"{stats.risque_moyen_pct:.2f}% -> {stats.cout_moyen_r:.3f} R/trade",
            f"Net    : {stats.total_r_net:+.2f} R cumule  |  esperance "
            f"{stats.esperance_r_nette:+.3f} R/trade",
        ]
    return "\n".join(lignes)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest walk-forward de la strategie")
    parser.add_argument("fichier", type=Path, help="CSV OHLCV ou JSON klines Binance")
    parser.add_argument("--symbol", default="BACKTEST")
    parser.add_argument("--interval", default="?")
    parser.add_argument("--min-score", type=int, default=2)
    parser.add_argument(
        "--max-bars", type=int, default=40, help="bougies avant expiration d'un trade"
    )
    parser.add_argument(
        "--cooldown-bars", type=int, default=0, help="bougies d'attente entre deux entrees"
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="fichier ordonne du plus recent au plus ancien",
    )
    parser.add_argument(
        "--fee-pct",
        type=float,
        default=0.08,
        help="frais aller-retour en %% du notionnel (0.08 = taker Binance des deux cotes)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = Config(min_score=args.min_score)
    try:
        series = load(args.fichier, args.symbol, args.interval, reverse=args.reverse)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Chargement impossible: {exc}", file=sys.stderr)
        return 1

    if len(series) < cfg.min_required_candles:
        print(
            f"Historique trop court: {len(series)} bougies, "
            f"il en faut au moins {cfg.min_required_candles}.",
            file=sys.stderr,
        )
        return 1

    stats = run(
        series,
        cfg,
        max_bars=args.max_bars,
        cooldown_bars=args.cooldown_bars,
        fee_pct=args.fee_pct,
    )
    print(report(args.fichier.name, stats, cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
