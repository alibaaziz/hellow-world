"""Grid geometry: level construction and per-cycle economics."""

from __future__ import annotations

import math
from typing import List

ARITHMETIC = "arithmetic"
GEOMETRIC = "geometric"


def build_levels(lower: float, upper: float, n_grids: int, mode: str = ARITHMETIC) -> List[float]:
    """Return the ``n_grids + 1`` price levels of a grid, ascending.

    ``n_grids`` counts the sub-intervals, matching Binance's "number of grids":
    a 10-grid bot has 11 price levels and 10 buy/sell cycles.
    """
    if lower <= 0:
        raise ValueError("lower must be > 0")
    if upper <= lower:
        raise ValueError("upper must be > lower")
    if n_grids < 1:
        raise ValueError("n_grids must be >= 1")

    if mode == ARITHMETIC:
        step = (upper - lower) / n_grids
        levels = [lower + i * step for i in range(n_grids + 1)]
    elif mode == GEOMETRIC:
        ratio = (upper / lower) ** (1.0 / n_grids)
        levels = [lower * ratio ** i for i in range(n_grids + 1)]
    else:
        raise ValueError("mode must be 'arithmetic' or 'geometric'")

    levels[-1] = upper  # kill float drift on the last level
    return levels


def grid_step_pct(lower: float, upper: float, n_grids: int, mode: str = ARITHMETIC) -> float:
    """Average distance between two levels, in percent of the level below.

    This is the number that matters: it must clear two maker fees before the
    bot makes anything at all.
    """
    levels = build_levels(lower, upper, n_grids, mode)
    pcts = [(levels[i + 1] - levels[i]) / levels[i] for i in range(len(levels) - 1)]
    return 100.0 * sum(pcts) / len(pcts)


def cycle_profit(buy_price: float, sell_price: float, qty: float, fee_rate: float) -> float:
    """Net profit of one completed buy-low / sell-high cycle, fees included."""
    gross = (sell_price - buy_price) * qty
    fees = (buy_price + sell_price) * qty * fee_rate
    return gross - fees


def breakeven_step_pct(fee_rate: float) -> float:
    """Minimum grid step (%) for a cycle to break even at the given fee rate.

    A cycle pays the fee twice, so the step has to cover both legs.
    """
    return 100.0 * 2.0 * fee_rate / (1.0 - fee_rate)


def suggest_grids(lower: float, upper: float, fee_rate: float, safety: float = 3.0,
                  mode: str = ARITHMETIC) -> int:
    """Largest grid count whose step still earns ``safety`` times the fee cost.

    Arithmetic steps are a fixed number of dollars, so their percentage value
    shrinks as price rises: the thinnest cycle sits at the top of the range.
    Sizing against that worst case keeps every cycle profitable, not just the
    ones near the bottom.
    """
    min_step = breakeven_step_pct(fee_rate) * safety / 100.0
    if mode == GEOMETRIC:
        n = math.log(upper / lower) / math.log(1.0 + min_step)
    else:
        n = (upper - lower) / (upper * min_step)
    return max(1, int(n))
