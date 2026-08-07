"""Parameter sweeps over grid width, density and leverage.

The point is not to find the best parameters for the past — that number is
always a fiction. It is to see the *shape* of the surface: which settings
survive a bad path, and how quickly returns collapse when the range is wrong.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .engine import BacktestResult, GridConfig, backtest


@dataclass
class SweepPoint:
    width_pct: float
    n_grids: int
    leverage: float
    lower: float
    upper: float
    metrics: Dict[str, Any]
    liquidated: bool

    @property
    def score(self) -> float:
        """Return, penalized for drawdown, and zeroed by a liquidation.

        Liquidation is not a bad outcome to be traded off against a good one —
        it ends the account. It scores below any surviving configuration.
        """
        if self.liquidated:
            return -1e9 + self.metrics["return_pct"]
        return self.metrics["return_pct"] - 0.5 * self.metrics["max_drawdown_pct"]


def sweep(
    bars: Sequence[Sequence[float]],
    base: GridConfig,
    widths: Sequence[float] = (0.05, 0.10, 0.15, 0.25),
    grid_counts: Sequence[int] = (10, 20, 40, 80),
    leverages: Sequence[float] = (1.0, 2.0, 3.0, 5.0),
    center: Optional[float] = None,
    funding: Optional[Sequence[Tuple[int, float]]] = None,
) -> List[SweepPoint]:
    """Run one backtest per combination, centered on ``center`` (default: first close).

    ``widths`` are half-widths as a fraction of the center price: 0.10 means a
    range from -10% to +10% around it.
    """
    anchor = center if center is not None else float(bars[0][4])
    points: List[SweepPoint] = []

    for width, n_grids, leverage in itertools.product(widths, grid_counts, leverages):
        lower = anchor * (1.0 - width)
        upper = anchor * (1.0 + width)
        config = replace(base, lower=lower, upper=upper,
                         n_grids=n_grids, leverage=leverage)
        try:
            result = backtest(bars, config, funding)
        except ValueError:
            continue
        points.append(SweepPoint(
            width_pct=width * 100.0,
            n_grids=n_grids,
            leverage=leverage,
            lower=lower,
            upper=upper,
            metrics=result.metrics,
            liquidated=result.liquidated,
        ))

    points.sort(key=lambda p: p.score, reverse=True)
    return points


def format_table(points: Sequence[SweepPoint], limit: int = 20) -> str:
    header = (f"{'Range':>8} {'Grilles':>8} {'Levier':>7} {'Retour%':>9} "
              f"{'APR%':>9} {'DD%':>7} {'Cycles':>7} {'Funding':>9}  Statut")
    lines = [header, "-" * len(header)]
    for point in points[:limit]:
        m = point.metrics
        status = "LIQUIDE" if point.liquidated else ""
        lines.append(
            f"{point.width_pct:>7.1f}% {point.n_grids:>8d} {point.leverage:>6.1f}x "
            f"{m['return_pct']:>9.2f} {m['apr_pct']:>9.1f} {m['max_drawdown_pct']:>7.2f} "
            f"{m['matched_cycles']:>7d} {-m['funding_paid']:>9.2f}  {status}"
        )
    return "\n".join(lines)


def robustness(points: Sequence[SweepPoint]) -> Dict[str, Any]:
    """Aggregate view of the sweep — the part worth acting on."""
    if not points:
        return {}
    survivors = [p for p in points if not p.liquidated]
    returns = [p.metrics["return_pct"] for p in points]
    return {
        "combinations": len(points),
        "liquidation_rate_pct": 100.0 * (len(points) - len(survivors)) / len(points),
        "profitable_pct": 100.0 * sum(1 for r in returns if r > 0) / len(returns),
        "median_return_pct": sorted(returns)[len(returns) // 2],
        "best": points[0] if points else None,
        "worst_survivor": survivors[-1] if survivors else None,
    }
