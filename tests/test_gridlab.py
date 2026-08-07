"""Unit tests for GridLab. Run with: python -m unittest discover tests"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gridlab import data
from gridlab.engine import EPS, GridConfig, GridEngine, _funding_stamps, backtest
from gridlab.grid import (
    breakeven_step_pct,
    build_levels,
    cycle_profit,
    grid_step_pct,
    suggest_grids,
)
from gridlab.optimize import sweep
from gridlab.report import render_html
from gridlab.risk import (
    grid_max_leverage,
    liquidation_price,
    max_safe_leverage,
)

HOUR = 3_600_000
T0 = 1_700_000_000_000


def bar(i, o, h, l, c):
    return (T0 + i * HOUR, o, h, l, c)


class TestGridGeometry(unittest.TestCase):
    def test_arithmetic_levels(self):
        levels = build_levels(100, 110, 10)
        self.assertEqual(len(levels), 11)
        self.assertAlmostEqual(levels[0], 100)
        self.assertAlmostEqual(levels[-1], 110)
        self.assertAlmostEqual(levels[5], 105)

    def test_geometric_levels_have_constant_ratio(self):
        levels = build_levels(100, 200, 8, "geometric")
        ratios = [levels[i + 1] / levels[i] for i in range(len(levels) - 1)]
        for ratio in ratios[1:]:
            self.assertAlmostEqual(ratio, ratios[0], places=10)
        self.assertAlmostEqual(levels[-1], 200)

    def test_invalid_bounds_rejected(self):
        for args in [(0, 10, 5), (100, 100, 5), (100, 110, 0), (-1, 10, 5)]:
            with self.assertRaises(ValueError):
                build_levels(*args)

    def test_step_pct_matches_manual(self):
        # 100 -> 110 in 10 steps: steps of 1 on bases 100..109
        expected = sum(1 / p for p in range(100, 110)) / 10 * 100
        self.assertAlmostEqual(grid_step_pct(100, 110, 10), expected, places=9)

    def test_breakeven_step_covers_two_fees(self):
        fee = 0.0002
        step = breakeven_step_pct(fee) / 100
        buy, qty = 100.0, 1.0
        sell = buy * (1 + step)
        self.assertAlmostEqual(cycle_profit(buy, sell, qty, fee), 0.0, places=9)

    def test_suggest_grids_keeps_step_above_fees(self):
        n = suggest_grids(100, 120, 0.0002, safety=3.0)
        self.assertGreater(grid_step_pct(100, 120, n), 3 * breakeven_step_pct(0.0002) * 0.99)


class TestRisk(unittest.TestCase):
    def test_long_liquidation_matches_definition(self):
        entry, qty, wallet, mmr = 100.0, 10.0, 200.0, 0.004
        liq = liquidation_price(entry, qty, wallet, "long", mmr)
        # At the liquidation price, margin balance equals maintenance margin.
        margin_balance = wallet + (liq - entry) * qty
        maintenance = liq * qty * mmr
        self.assertAlmostEqual(margin_balance, maintenance, places=8)

    def test_short_liquidation_matches_definition(self):
        entry, qty, wallet, mmr = 100.0, 10.0, 200.0, 0.004
        liq = liquidation_price(entry, qty, wallet, "short", mmr)
        margin_balance = wallet + (entry - liq) * qty
        maintenance = liq * qty * mmr
        self.assertAlmostEqual(margin_balance, maintenance, places=8)
        self.assertGreater(liq, entry)

    def test_more_margin_pushes_liquidation_away(self):
        near = liquidation_price(100, 10, 100, "long")
        far = liquidation_price(100, 10, 400, "long")
        self.assertLess(far, near)

    def test_no_position_has_no_liquidation(self):
        self.assertIsNone(liquidation_price(100, 0, 500, "long"))

    def test_max_safe_leverage_survives_the_stop(self):
        entry, stop, wallet = 100.0, 90.0, 1000.0
        lev = max_safe_leverage(entry, stop, "long", buffer_pct=0.0)
        qty = wallet * lev / entry
        liq = liquidation_price(entry, qty, wallet, "long")
        self.assertLessEqual(liq, stop + 1e-6)

    def test_buffer_lowers_leverage(self):
        tight = max_safe_leverage(100, 90, "long", buffer_pct=0.0)
        safe = max_safe_leverage(100, 90, "long", buffer_pct=50.0)
        self.assertLess(safe, tight)

    def test_grid_max_leverage_is_finite_and_positive(self):
        lev = grid_max_leverage(90, 110, "long")
        self.assertGreater(lev, 1.0)
        self.assertLess(lev, 100.0)


class TestEngineMechanics(unittest.TestCase):
    def base_config(self, **kw):
        params = dict(lower=100.0, upper=110.0, n_grids=10, investment=1000.0,
                      leverage=1.0, maker_fee=0.0, taker_fee=0.0,
                      funding_rate=0.0, close_at_end=False)
        params.update(kw)
        return GridConfig(**params)

    def test_initial_book_splits_around_start_price(self):
        engine = GridEngine(self.base_config())
        engine._place_initial_orders(T0, 105.0)
        buys = [o for o in engine.orders.values() if o.side == "buy"]
        sells = [o for o in engine.orders.values() if o.side == "sell"]
        self.assertEqual(len(buys), 5)      # 100..104
        self.assertEqual(len(sells), 5)     # 106..110
        self.assertTrue(all(o.price < 105 for o in buys))
        self.assertTrue(all(o.price > 105 for o in sells))

    def test_long_grid_seeds_inventory_for_every_sell(self):
        engine = GridEngine(self.base_config())
        engine._place_initial_orders(T0, 105.0)
        expected = sum(o.qty for o in engine.orders.values() if o.side == "sell")
        self.assertAlmostEqual(engine.pos, expected, places=9)
        self.assertAlmostEqual(engine.entry, 105.0, places=9)

    def test_short_grid_seeds_a_short_position(self):
        engine = GridEngine(self.base_config(direction="short"))
        engine._place_initial_orders(T0, 105.0)
        expected = sum(o.qty for o in engine.orders.values() if o.side == "buy")
        self.assertAlmostEqual(engine.pos, -expected, places=9)

    def test_neutral_grid_starts_flat(self):
        engine = GridEngine(self.base_config(direction="neutral"))
        engine._place_initial_orders(T0, 105.0)
        self.assertAlmostEqual(engine.pos, 0.0, places=12)

    def test_one_down_up_move_closes_exactly_one_cycle(self):
        config = self.base_config()
        bars = [bar(0, 105, 105, 104, 105)]
        result = backtest(bars, config)
        self.assertEqual(result.metrics["matched_cycles"], 1)
        qty = (config.notional / config.n_grids) / 104.0
        self.assertAlmostEqual(result.metrics["cycle_profit_gross"], 1.0 * qty, places=9)

    def test_cycle_profit_scales_with_the_number_of_crossings(self):
        config = self.base_config()
        bars = [bar(i, 105, 105, 104, 105) for i in range(4)]
        result = backtest(bars, config)
        self.assertEqual(result.metrics["matched_cycles"], 4)

    def test_zero_fee_flat_price_is_a_wash(self):
        config = self.base_config()
        bars = [bar(i, 105, 105, 105, 105) for i in range(10)]
        result = backtest(bars, config)
        self.assertEqual(result.metrics["matched_cycles"], 0)
        self.assertAlmostEqual(result.metrics["net_pnl"], 0.0, places=6)

    def test_pnl_decomposition_adds_up_to_net(self):
        config = self.base_config(maker_fee=0.0002, taker_fee=0.0004,
                                  funding_rate=0.0001, leverage=2.0)
        bars = data.synthetic_bars(n=400, start_price=105.0, seed=3)
        bars = [(t, o, h, l, c) for t, o, h, l, c, _ in bars]
        result = backtest(bars, config)
        m = result.metrics
        rebuilt = (m["realized_pnl"] - m["fees_paid"] - m["funding_paid"]
                   + m["unrealized_pnl"])
        self.assertAlmostEqual(rebuilt, m["net_pnl"], places=6)

    def test_fees_are_charged_on_every_fill(self):
        config = self.base_config(maker_fee=0.001, taker_fee=0.002)
        bars = [bar(0, 105, 105, 104, 105)]
        result = backtest(bars, config)
        expected = sum(t.price * t.qty * (0.002 if t.kind == "init" else 0.001)
                       for t in result.trades)
        self.assertAlmostEqual(result.metrics["fees_paid"], expected, places=9)

    def test_long_grid_never_goes_short(self):
        config = self.base_config()
        bars = data.synthetic_bars(n=600, start_price=105.0, seed=11)
        bars = [(t, o, h, l, c) for t, o, h, l, c, _ in bars]
        result = backtest(bars, config)
        self.assertTrue(all(p >= -EPS for _, p in result.positions))

    def test_short_grid_never_goes_long(self):
        config = self.base_config(direction="short")
        bars = data.synthetic_bars(n=600, start_price=105.0, seed=11)
        bars = [(t, o, h, l, c) for t, o, h, l, c, _ in bars]
        result = backtest(bars, config)
        self.assertTrue(all(p <= EPS for _, p in result.positions))

    def test_rally_through_the_top_flattens_a_long_grid(self):
        config = self.base_config()
        bars = [bar(0, 105, 105, 105, 105), bar(1, 105, 130, 105, 130)]
        result = backtest(bars, config)
        self.assertAlmostEqual(result.metrics["final_position"], 0.0, places=9)
        self.assertEqual(result.metrics["matched_cycles"], 0)  # sells were the seed

    def test_crash_below_the_range_leaves_a_full_bag(self):
        config = self.base_config(leverage=1.0)
        bars = [bar(0, 105, 105, 105, 105), bar(1, 105, 105, 95, 96)]
        result = backtest(bars, config)
        self.assertGreater(result.metrics["final_position"], 0)
        self.assertLess(result.metrics["unrealized_pnl"], 0)

    def test_exit_below_stops_the_bot(self):
        config = self.base_config(exit_below=100.0)
        bars = [bar(0, 105, 105, 105, 105), bar(1, 105, 105, 99, 99),
                bar(2, 99, 120, 99, 120)]
        result = backtest(bars, config)
        self.assertAlmostEqual(result.metrics["final_position"], 0.0, places=9)
        self.assertIsNotNone(result.stop_reason)

    def test_high_leverage_liquidates_on_a_crash(self):
        config = self.base_config(leverage=20.0, lower=90.0, upper=110.0, n_grids=20)
        bars = [bar(0, 105, 105, 105, 105), bar(1, 105, 105, 70, 70)]
        result = backtest(bars, config)
        self.assertTrue(result.liquidated)
        self.assertAlmostEqual(result.metrics["final_equity"], 0.0, places=9)

    def test_low_leverage_survives_the_same_crash(self):
        config = self.base_config(leverage=1.0, lower=90.0, upper=110.0, n_grids=20)
        bars = [bar(0, 105, 105, 105, 105), bar(1, 105, 105, 70, 70)]
        result = backtest(bars, config)
        self.assertFalse(result.liquidated)


class TestFunding(unittest.TestCase):
    def test_stamps_land_on_the_eight_hour_marks(self):
        day = (T0 // 86_400_000) * 86_400_000
        # Half-open interval (prev, ts]: one full day holds 00:00, 08:00, 16:00,
        # and stops just short of the next midnight.
        stamps = _funding_stamps(day - 1, day + 86_400_000 - 1)
        self.assertEqual(stamps, [day, day + 8 * HOUR, day + 16 * HOUR])

    def test_boundary_stamp_is_counted_once(self):
        day = (T0 // 86_400_000) * 86_400_000
        self.assertEqual(_funding_stamps(day - 1, day), [day])
        self.assertEqual(_funding_stamps(day, day + HOUR), [])

    def test_no_stamps_inside_a_single_hour(self):
        day = (T0 // 86_400_000) * 86_400_000
        self.assertEqual(_funding_stamps(day + HOUR, day + 2 * HOUR), [])

    def test_long_pays_positive_funding(self):
        config = GridConfig(lower=100, upper=110, n_grids=10, investment=1000,
                            leverage=1.0, maker_fee=0.0, taker_fee=0.0,
                            funding_rate=0.001, close_at_end=False)
        bars = [bar(i, 105, 105, 105, 105) for i in range(48)]
        result = backtest(bars, config)
        self.assertGreater(result.metrics["funding_paid"], 0)
        self.assertLess(result.metrics["net_pnl"], 0)

    def test_short_receives_positive_funding(self):
        config = GridConfig(lower=100, upper=110, n_grids=10, investment=1000,
                            leverage=1.0, direction="short", maker_fee=0.0,
                            taker_fee=0.0, funding_rate=0.001, close_at_end=False)
        bars = [bar(i, 105, 105, 105, 105) for i in range(48)]
        result = backtest(bars, config)
        self.assertLess(result.metrics["funding_paid"], 0)

    def test_explicit_funding_series_overrides_the_constant(self):
        config = GridConfig(lower=100, upper=110, n_grids=10, investment=1000,
                            leverage=1.0, maker_fee=0.0, taker_fee=0.0,
                            funding_rate=0.5, close_at_end=False)
        bars = [bar(i, 105, 105, 105, 105) for i in range(48)]
        day = (T0 // 86_400_000) * 86_400_000
        series = [(day + h * HOUR, 0.0) for h in range(0, 72, 8)]
        result = backtest(bars, config, funding=series)
        self.assertAlmostEqual(result.metrics["funding_paid"], 0.0, places=9)


class TestDataAndReport(unittest.TestCase):
    def test_synthetic_bars_are_well_formed(self):
        bars = data.synthetic_bars(n=300, seed=1)
        self.assertEqual(len(bars), 300)
        for ts, o, h, l, c, v in bars:
            self.assertLessEqual(l, min(o, c) + 1e-9)
            self.assertGreaterEqual(h, max(o, c) - 1e-9)
            self.assertGreater(l, 0)
        stamps = [b[0] for b in bars]
        self.assertEqual(stamps, sorted(stamps))

    def test_synthetic_bars_are_deterministic(self):
        self.assertEqual(data.synthetic_bars(n=50, seed=4),
                         data.synthetic_bars(n=50, seed=4))

    def test_csv_round_trip(self):
        import tempfile
        bars = data.synthetic_bars(n=40, seed=2)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bars.csv")
            data.save_csv(path, bars)
            loaded = data.load_csv(path)
        self.assertEqual(len(loaded), 40)
        for original, restored in zip(bars, loaded):
            self.assertEqual(original[0], restored[0])
            self.assertAlmostEqual(original[4], restored[4], places=9)

    def test_auto_range_brackets_most_closes(self):
        bars = data.synthetic_bars(n=800, seed=5)
        lower, upper = data.auto_range(bars, 0.9)
        inside = sum(1 for b in bars if lower <= b[4] <= upper)
        self.assertGreater(inside / len(bars), 0.85)
        self.assertLess(lower, upper)

    def test_report_is_self_contained_html(self):
        bars = [(t, o, h, l, c) for t, o, h, l, c, _ in
                data.synthetic_bars(n=300, start_price=105, seed=6)]
        config = GridConfig(lower=100, upper=110, n_grids=20, investment=1000)
        html_doc = render_html(backtest(bars, config))
        self.assertIn("<svg", html_doc)
        self.assertNotIn("http://", html_doc)
        self.assertNotIn("<script", html_doc)
        self.assertIn("prefers-color-scheme", html_doc)

    def test_sweep_ranks_and_flags_liquidations(self):
        bars = [(t, o, h, l, c) for t, o, h, l, c, _ in
                data.synthetic_bars(n=500, start_price=105, seed=8)]
        base = GridConfig(lower=100, upper=110, n_grids=10, investment=1000)
        points = sweep(bars, base, widths=(0.05, 0.15), grid_counts=(10, 30),
                       leverages=(1.0, 25.0))
        self.assertEqual(len(points), 8)
        scores = [p.score for p in points]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for point in points:
            if point.liquidated:
                self.assertLess(point.score, -1e6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
