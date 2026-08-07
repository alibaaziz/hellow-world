"""Grid bot simulator for USDS-M futures.

The engine walks OHLC bars, fills resting limit orders along an assumed
intrabar path, and tracks the four things that decide whether a grid bot
actually made money:

    net PnL = realized grid profit - fees - funding + open position PnL

Most grid backtests report only the first term. The last one is what turns a
"+8% this month" screenshot into a drawdown when price leaves the range.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .grid import ARITHMETIC, build_levels, grid_step_pct
from .risk import DEFAULT_MAINT_AMOUNT, DEFAULT_MMR, liquidation_price

MS_PER_HOUR = 3_600_000
FUNDING_HOURS = (0, 8, 16)
EPS = 1e-9

LONG = "long"
SHORT = "short"
NEUTRAL = "neutral"


@dataclass
class GridConfig:
    """Everything the bot needs to know. Prices in quote currency (USDT)."""

    lower: float
    upper: float
    n_grids: int
    mode: str = ARITHMETIC
    direction: str = LONG                 # long | short | neutral
    investment: float = 1000.0            # margin allocated, USDT
    leverage: float = 3.0
    maker_fee: float = 0.0002             # limit grid orders
    taker_fee: float = 0.0004             # initial position and forced exits
    qty_mode: str = "equal_quote"         # equal_quote | equal_base
    mmr: float = DEFAULT_MMR
    maint_amount: float = DEFAULT_MAINT_AMOUNT
    funding_rate: float = 0.0001          # per 8h, when no funding series given
    exit_below: Optional[float] = None    # close everything and stop
    exit_above: Optional[float] = None
    close_on_exit: bool = False           # shorthand: exit at the range bounds
    # Leave the standing position open at the end and mark it to market, the
    # way a running bot actually is. Force-closing it would hide the inventory
    # the grid is carrying — the number that decides whether it really made
    # money — inside the realized bucket.
    close_at_end: bool = False

    def __post_init__(self) -> None:
        if self.direction not in (LONG, SHORT, NEUTRAL):
            raise ValueError("direction must be 'long', 'short' or 'neutral'")
        if self.investment <= 0:
            raise ValueError("investment must be > 0")
        if self.leverage <= 0:
            raise ValueError("leverage must be > 0")
        if self.close_on_exit:
            if self.exit_below is None and self.direction in (LONG, NEUTRAL):
                self.exit_below = self.lower
            if self.exit_above is None and self.direction in (SHORT, NEUTRAL):
                self.exit_above = self.upper

    @property
    def notional(self) -> float:
        return self.investment * self.leverage


@dataclass
class Order:
    side: str          # buy | sell
    price: float
    qty: float
    level: int
    paired: Optional[float] = None   # price of the fill that spawned this order


@dataclass
class Trade:
    ts: int
    side: str
    price: float
    qty: float
    fee: float
    kind: str          # init | grid | exit | liquidation
    realized: float


@dataclass
class BacktestResult:
    config: GridConfig
    trades: List[Trade]
    equity: List[Tuple[int, float]]      # (ts, equity)
    prices: List[Tuple[int, float]]      # (ts, close)
    positions: List[Tuple[int, float]]   # (ts, signed qty)
    levels: List[float]
    metrics: Dict[str, Any]
    liquidated: bool
    stop_reason: Optional[str]

    def summary(self) -> str:
        m = self.metrics
        lines = [
            f"Periode          : {m['days']:.1f} jours ({m['bars']} bougies)",
            f"Prix             : {m['first_price']:.2f} -> {m['last_price']:.2f} "
            f"({m['price_change_pct']:+.2f}%)",
            f"Grille           : {self.config.lower:.2f} - {self.config.upper:.2f} "
            f"x{self.config.n_grids} (pas {m['step_pct']:.3f}%)",
            f"Levier           : x{self.config.leverage:g}   "
            f"Notionnel: {self.config.notional:.0f} USDT",
            "",
            f"Cycles fermes    : {m['matched_cycles']}  "
            f"(profit brut des cycles {m['cycle_profit_gross']:+.2f} USDT)",
            f"PnL realise      : {m['realized_pnl']:+.2f} USDT",
            f"Frais            : {-m['fees_paid']:+.2f} USDT",
            f"Funding          : {-m['funding_paid']:+.2f} USDT",
            f"Position ouverte : {m['unrealized_pnl']:+.2f} USDT "
            f"({m['final_position']:+.6f} @ {m['final_entry']:.2f})",
            "-" * 46,
            f"PnL net          : {m['net_pnl']:+.2f} USDT  ({m['return_pct']:+.2f}%)",
            f"APR              : {m['apr_pct']:+.2f}%",
            f"Drawdown max     : {m['max_drawdown_pct']:.2f}%",
            f"Temps dans range : {m['time_in_range_pct']:.1f}%",
            f"Buy & hold       : {m['buy_hold_pct']:+.2f}%",
        ]
        if m["liq_price"] is not None:
            lines.append(f"Prix de liq. fin : {m['liq_price']:.2f}")
        if self.liquidated:
            lines.append("!! LIQUIDE !!")
        elif self.stop_reason:
            lines.append(f"Arret            : {self.stop_reason}")
        return "\n".join(lines)


class GridEngine:
    """Stateful simulator. One instance per backtest."""

    def __init__(self, config: GridConfig):
        self.cfg = config
        self.levels = build_levels(config.lower, config.upper, config.n_grids, config.mode)
        self.orders: Dict[int, Order] = {}

        self.pos = 0.0            # signed contract quantity
        self.entry = 0.0          # average entry price
        self.wallet = config.investment
        self.realized_total = 0.0
        self.fees_total = 0.0
        self.funding_total = 0.0
        self.matched_cycles = 0
        self.cycle_profit_gross = 0.0

        self.trades: List[Trade] = []
        self.equity: List[Tuple[int, float]] = []
        self.prices: List[Tuple[int, float]] = []
        self.positions: List[Tuple[int, float]] = []

        self.liquidated = False
        self.stop_reason: Optional[str] = None
        self._stopped = False

    # ---------------------------------------------------------------- sizing

    def _qty_for_level(self, price: float) -> float:
        per_grid = self.cfg.notional / self.cfg.n_grids
        if self.cfg.qty_mode == "equal_base":
            mid = (self.cfg.lower + self.cfg.upper) / 2.0
            return per_grid / mid
        return per_grid / price

    # ------------------------------------------------------------ book setup

    def _place_initial_orders(self, ts: int, price: float) -> None:
        """Resting orders below and above the start price, plus the seed position.

        A long grid sells into rallies, so it must already hold the inventory
        that every sell above the start price will unload. That seed position is
        bought at market — the single taker fill of the bot's life.
        """
        seed_qty = 0.0
        for i, level in enumerate(self.levels):
            qty = self._qty_for_level(level)
            if level < price - EPS:
                self.orders[i] = Order("buy", level, qty, i)
                if self.cfg.direction == SHORT:
                    seed_qty -= qty
            elif level > price + EPS:
                self.orders[i] = Order("sell", level, qty, i)
                if self.cfg.direction == LONG:
                    seed_qty += qty

        if abs(seed_qty) > EPS:
            side = "buy" if seed_qty > 0 else "sell"
            self._execute(ts, side, price, abs(seed_qty), self.cfg.taker_fee, "init")

    # ------------------------------------------------------------- execution

    def _execute(self, ts: int, side: str, price: float, qty: float,
                 fee_rate: float, kind: str) -> float:
        """Apply a fill. Returns the quantity that reduced the open position."""
        if qty <= EPS:
            return 0.0
        signed = qty if side == "buy" else -qty
        realized = 0.0
        closing = 0.0

        if abs(self.pos) < EPS or (self.pos > 0) == (signed > 0):
            new_pos = self.pos + signed
            self.entry = (self.entry * abs(self.pos) + price * qty) / abs(new_pos)
            self.pos = new_pos
        else:
            closing = min(qty, abs(self.pos))
            direction = 1.0 if self.pos > 0 else -1.0
            realized = (price - self.entry) * closing * direction
            new_pos = self.pos + signed
            if abs(new_pos) < EPS:
                new_pos = 0.0
                self.entry = 0.0
            elif (new_pos > 0) != (self.pos > 0):
                self.entry = price      # flipped through zero
            self.pos = new_pos
            self.realized_total += realized
            self.wallet += realized

        fee = price * qty * fee_rate
        self.wallet -= fee
        self.fees_total += fee
        self.trades.append(Trade(ts, side, price, qty, fee, kind, realized))
        return closing

    def _fill_down(self, ts: int, target: float) -> None:
        """Price falls to ``target``: take every resting buy at or above it."""
        while True:
            candidates = [o for o in self.orders.values()
                          if o.side == "buy" and o.price >= target - EPS]
            if not candidates:
                return
            order = max(candidates, key=lambda o: o.price)
            del self.orders[order.level]
            closed = self._execute(ts, "buy", order.price, order.qty,
                                   self.cfg.maker_fee, "grid")
            self._close_cycle(order, closed)
            # The matching sell goes one level up, carrying the same base qty
            # so the cycle closes exactly what it opened.
            up = order.level + 1
            if up < len(self.levels) and up not in self.orders:
                self.orders[up] = Order("sell", self.levels[up], order.qty, up, order.price)

    def _fill_up(self, ts: int, target: float) -> None:
        """Price rises to ``target``: take every resting sell at or below it."""
        while True:
            candidates = [o for o in self.orders.values()
                          if o.side == "sell" and o.price <= target + EPS]
            if not candidates:
                return
            order = min(candidates, key=lambda o: o.price)
            del self.orders[order.level]
            closed = self._execute(ts, "sell", order.price, order.qty,
                                   self.cfg.maker_fee, "grid")
            self._close_cycle(order, closed)
            down = order.level - 1
            if down >= 0 and down not in self.orders:
                self.orders[down] = Order("buy", self.levels[down], order.qty, down, order.price)

    def _close_cycle(self, order: Order, closed_qty: float) -> None:
        """Book a cycle when a paired order fills *and* reduces the position.

        Both conditions matter. On a long grid, the buy that a sell spawns is
        also "paired", but it re-opens inventory rather than closing anything —
        counting it would book every round trip twice. Only the leg that
        actually reduces the position earns the spread.

        This is the bot's real income, and it is *not* realized PnL: position
        accounting uses average cost, which shuffles profit between the
        realized and unrealized buckets. The totals agree; the split does not.
        """
        if order.paired is None or closed_qty <= EPS:
            return
        self.matched_cycles += 1
        self.cycle_profit_gross += abs(order.price - order.paired) * closed_qty

    # --------------------------------------------------------------- funding

    def _apply_funding(self, ts: int, prev_ts: int, price: float,
                       funding_map: Optional[Dict[int, float]]) -> None:
        """Charge every funding settlement that falls in (prev_ts, ts].

        Longs pay when the rate is positive. On a long grid held through a
        contango market this is a constant, silent drain.
        """
        if abs(self.pos) < EPS:
            return
        for stamp in _funding_stamps(prev_ts, ts):
            rate = self.cfg.funding_rate
            if funding_map is not None:
                rate = funding_map.get(stamp, rate)
            payment = self.pos * price * rate     # >0 means we pay
            self.wallet -= payment
            self.funding_total += payment

    # ------------------------------------------------------------ protection

    def _liq_price(self) -> Optional[float]:
        if abs(self.pos) < EPS:
            return None
        side = LONG if self.pos > 0 else SHORT
        return liquidation_price(self.entry, abs(self.pos), self.wallet, side,
                                 self.cfg.mmr, self.cfg.maint_amount)

    def _check_liquidation(self, ts: int, low: float, high: float) -> bool:
        liq = self._liq_price()
        if liq is None:
            return False
        if self.pos > 0 and low <= liq:
            self._force_close(ts, liq, "liquidation")
            self.wallet = 0.0
            self.liquidated = True
            self.stop_reason = f"liquidation a {liq:.2f}"
            return True
        if self.pos < 0 and high >= liq:
            self._force_close(ts, liq, "liquidation")
            self.wallet = 0.0
            self.liquidated = True
            self.stop_reason = f"liquidation a {liq:.2f}"
            return True
        return False

    def _force_close(self, ts: int, price: float, kind: str) -> None:
        if abs(self.pos) < EPS:
            return
        side = "sell" if self.pos > 0 else "buy"
        self._execute(ts, side, price, abs(self.pos), self.cfg.taker_fee, kind)
        self.orders.clear()

    def _check_exit(self, ts: int, low: float, high: float) -> bool:
        cfg = self.cfg
        if cfg.exit_below is not None and low <= cfg.exit_below:
            self._force_close(ts, cfg.exit_below, "exit")
            self.stop_reason = f"sortie basse a {cfg.exit_below:.2f}"
            return True
        if cfg.exit_above is not None and high >= cfg.exit_above:
            self._force_close(ts, cfg.exit_above, "exit")
            self.stop_reason = f"sortie haute a {cfg.exit_above:.2f}"
            return True
        return False

    # -------------------------------------------------------------- main run

    def run(self, bars: Sequence[Sequence[float]],
            funding: Optional[Sequence[Tuple[int, float]]] = None) -> BacktestResult:
        """``bars`` is a sequence of ``(ts_ms, open, high, low, close)``."""
        if not bars:
            raise ValueError("no bars to backtest")

        funding_map = {int(t): r for t, r in funding} if funding else None
        first_ts = int(bars[0][0])
        first_price = float(bars[0][1])
        self._place_initial_orders(first_ts, first_price)

        prev_ts = first_ts
        last_close = first_price

        for bar in bars:
            ts, o, h, l, c = int(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])
            last_close = c

            if not self._stopped:
                self._apply_funding(ts, prev_ts, o, funding_map)

                # Assume the bar walked open -> low -> high -> close when it
                # closed up, and open -> high -> low -> close when it closed
                # down. Conservative for a grid: it under-counts fills on the
                # bars that whipsaw both ways.
                if c >= o:
                    self._fill_down(ts, l)
                    self._fill_up(ts, h)
                    self._fill_down(ts, c)
                else:
                    self._fill_up(ts, h)
                    self._fill_down(ts, l)
                    self._fill_up(ts, c)

                if self._check_liquidation(ts, l, h) or self._check_exit(ts, l, h):
                    self._stopped = True

            self.prices.append((ts, c))
            self.positions.append((ts, self.pos))
            self.equity.append((ts, self._equity(c)))
            prev_ts = ts

        if not self._stopped and self.cfg.close_at_end:
            self._force_close(int(bars[-1][0]), last_close, "exit")
            if self.equity:
                self.equity[-1] = (self.equity[-1][0], self._equity(last_close))

        return self._build_result(bars, last_close)

    def _equity(self, price: float) -> float:
        if self.liquidated:
            return 0.0
        return self.wallet + self._unrealized(price)

    def _unrealized(self, price: float) -> float:
        if abs(self.pos) < EPS:
            return 0.0
        return (price - self.entry) * self.pos

    # --------------------------------------------------------------- metrics

    def _build_result(self, bars: Sequence[Sequence[float]], last_price: float) -> BacktestResult:
        cfg = self.cfg
        first_price = float(bars[0][1])
        ms = int(bars[-1][0]) - int(bars[0][0])
        days = max(ms / 86_400_000.0, 1e-9)

        equity_values = [e for _, e in self.equity] or [cfg.investment]
        peak = equity_values[0]
        max_dd = 0.0
        for value in equity_values:
            peak = max(peak, value)
            if peak > 0:
                max_dd = max(max_dd, (peak - value) / peak)

        in_range = sum(1 for _, p in self.prices if cfg.lower <= p <= cfg.upper)
        final_equity = equity_values[-1]
        net = final_equity - cfg.investment
        ret_pct = net / cfg.investment * 100.0

        metrics: Dict[str, Any] = {
            "bars": len(bars),
            "days": days,
            "first_price": first_price,
            "last_price": last_price,
            "price_change_pct": (last_price / first_price - 1.0) * 100.0,
            "step_pct": grid_step_pct(cfg.lower, cfg.upper, cfg.n_grids, cfg.mode),
            "matched_cycles": self.matched_cycles,
            "cycle_profit_gross": self.cycle_profit_gross,
            "trades": len(self.trades),
            "realized_pnl": self.realized_total,
            "fees_paid": self.fees_total,
            "funding_paid": self.funding_total,
            "unrealized_pnl": self._unrealized(last_price),
            "final_position": self.pos,
            "final_entry": self.entry,
            "final_equity": final_equity,
            "net_pnl": net,
            "return_pct": ret_pct,
            "apr_pct": ret_pct * 365.0 / days,
            "max_drawdown_pct": max_dd * 100.0,
            "time_in_range_pct": 100.0 * in_range / max(len(self.prices), 1),
            "buy_hold_pct": (last_price / first_price - 1.0) * 100.0 * cfg.leverage,
            "cycles_per_day": self.matched_cycles / days,
            "liq_price": self._liq_price(),
            "liquidated": self.liquidated,
        }

        return BacktestResult(
            config=cfg,
            trades=self.trades,
            equity=self.equity,
            prices=self.prices,
            positions=self.positions,
            levels=self.levels,
            metrics=metrics,
            liquidated=self.liquidated,
            stop_reason=self.stop_reason,
        )


def _funding_stamps(prev_ts: int, ts: int) -> List[int]:
    """Funding settlement timestamps in ``(prev_ts, ts]`` (00/08/16 UTC)."""
    if ts <= prev_ts:
        return []
    stamps: List[int] = []
    day = (prev_ts // 86_400_000) * 86_400_000
    while day <= ts:
        for hour in FUNDING_HOURS:
            stamp = day + hour * MS_PER_HOUR
            if prev_ts < stamp <= ts:
                stamps.append(stamp)
        day += 86_400_000
    return sorted(stamps)


def backtest(bars: Sequence[Sequence[float]], config: GridConfig,
             funding: Optional[Sequence[Tuple[int, float]]] = None) -> BacktestResult:
    """Convenience wrapper: one config, one run."""
    return GridEngine(config).run(bars, funding)
