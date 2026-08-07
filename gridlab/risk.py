"""USDS-M futures margin and liquidation math (one-way mode, isolated margin).

Formulas follow Binance's maintenance-margin model:

    margin balance      = wallet balance + unrealized PnL
    maintenance margin  = |position notional| * mmr - maintenance amount
    liquidation         = margin balance <= maintenance margin

Solving that equality for price gives the functions below.
"""

from __future__ import annotations

from typing import Optional

# Binance USDS-M tier 1 defaults (BTCUSDT, notional < 50k). Override per symbol.
DEFAULT_MMR = 0.004
DEFAULT_MAINT_AMOUNT = 0.0


def liquidation_price(
    entry: float,
    qty: float,
    wallet: float,
    side: str,
    mmr: float = DEFAULT_MMR,
    maint_amount: float = DEFAULT_MAINT_AMOUNT,
) -> Optional[float]:
    """Liquidation price of a position, or ``None`` when there is no position.

    ``qty`` is the absolute contract size, ``wallet`` the balance backing it
    (initial margin plus realized PnL, minus fees and funding already paid).
    A long can return a negative number, which simply means the balance covers
    a drop to zero: the position cannot be liquidated on price alone.
    """
    if qty <= 0:
        return None
    if side == "long":
        return (entry * qty - wallet - maint_amount) / (qty * (1.0 - mmr))
    if side == "short":
        return (wallet + maint_amount + entry * qty) / (qty * (1.0 + mmr))
    raise ValueError("side must be 'long' or 'short'")


def liquidation_distance_pct(
    entry: float,
    qty: float,
    wallet: float,
    side: str,
    mmr: float = DEFAULT_MMR,
    maint_amount: float = DEFAULT_MAINT_AMOUNT,
) -> Optional[float]:
    """Distance from entry to liquidation, in percent of entry."""
    liq = liquidation_price(entry, qty, wallet, side, mmr, maint_amount)
    if liq is None:
        return None
    return abs(entry - liq) / entry * 100.0


def max_safe_leverage(
    entry: float,
    stop_price: float,
    side: str,
    buffer_pct: float = 20.0,
    mmr: float = DEFAULT_MMR,
) -> float:
    """Highest leverage keeping liquidation beyond ``stop_price``.

    ``buffer_pct`` pushes liquidation a further margin past the stop, so a wick
    through the bottom of your grid does not close the account.

    A position of notional ``N`` on wallet ``W`` has leverage ``L = N / W``.
    Substituting into the liquidation formula, the adverse move it survives is
    ``1/L - mmr`` (in fraction of entry), so the leverage surviving a move of
    ``d`` is ``1 / (d + mmr)``.
    """
    if entry <= 0 or stop_price <= 0:
        raise ValueError("prices must be > 0")
    move = abs(entry - stop_price) / entry
    move *= 1.0 + buffer_pct / 100.0
    if side not in ("long", "short"):
        raise ValueError("side must be 'long' or 'short'")
    denom = move + mmr
    if denom <= 0:
        return float("inf")
    return 1.0 / denom


def grid_max_leverage(
    lower: float,
    upper: float,
    direction: str = "long",
    buffer_pct: float = 20.0,
    mmr: float = DEFAULT_MMR,
) -> float:
    """Leverage a grid can carry without liquidating inside its own range.

    A long grid is fully loaded at ``lower`` with an average entry near the
    middle of the range, so the worst case it must survive is mid -> lower.
    """
    mid = (lower + upper) / 2.0
    if direction == "short":
        return max_safe_leverage(mid, upper, "short", buffer_pct, mmr)
    return max_safe_leverage(mid, lower, "long", buffer_pct, mmr)


def funding_cost(notional: float, rate: float, hours: float) -> float:
    """Funding paid by a long over ``hours`` at a constant 8h ``rate``.

    Negative when the rate is negative (the long is paid). Flip the sign for a
    short position.
    """
    return notional * rate * (hours / 8.0)


def funding_breakeven_rate(step_pct: float, fee_rate: float, cycles_per_day: float) -> float:
    """8h funding rate that cancels the grid's income.

    Above this rate, a long grid bleeds even while filling normally.
    """
    if cycles_per_day <= 0:
        return 0.0
    net_per_cycle = step_pct / 100.0 - 2.0 * fee_rate
    per_day = net_per_cycle * cycles_per_day
    return per_day / 3.0
