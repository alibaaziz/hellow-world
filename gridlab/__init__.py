"""GridLab — backtest et analyse de risque pour bots grid sur Binance Futures."""

from .engine import BacktestResult, GridConfig, GridEngine, backtest
from .grid import breakeven_step_pct, build_levels, grid_step_pct, suggest_grids
from .optimize import SweepPoint, sweep
from .report import render_html, write_report
from .risk import grid_max_leverage, liquidation_price, max_safe_leverage

__version__ = "1.0.0"

__all__ = [
    "BacktestResult",
    "GridConfig",
    "GridEngine",
    "backtest",
    "build_levels",
    "grid_step_pct",
    "breakeven_step_pct",
    "suggest_grids",
    "sweep",
    "SweepPoint",
    "render_html",
    "write_report",
    "liquidation_price",
    "max_safe_leverage",
    "grid_max_leverage",
]
