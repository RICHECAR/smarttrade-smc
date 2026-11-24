"""
Unit tests for Execution Quality Analyzer — Maker vs Taker

Covers:
    - compute_group_kpis: basic, empty, edge cases
    - split_trades_by_execution_mode: correct split
    - analyze_execution_quality: full analysis with flags
    - TAKER_DEGRADING_EDGE detection
    - is_taker_blocking_promotion gate
    - is_taker_blocking_scaleup gate
    - fallback rate warnings
"""

import unittest
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from scripts.reporting.execution_quality_analyzer import (
    compute_group_kpis,
    split_trades_by_execution_mode,
    analyze_execution_quality,
    is_taker_blocking_promotion,
    is_taker_blocking_scaleup,
    TAKER_DEGRADING_MIN_TRADES,
    TAKER_DEGRADING_PF_THRESHOLD,
    FALLBACK_RATE_WARNING,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _winning_trade(pnl=10.0, fees=0.5, fallback=False, **extra):
    t = {
        "pnl": pnl, "pnl_gross": pnl + fees, "fees": fees,
        "r_multiple": pnl / 5.0, "mae": 0.3, "slippage": 0.1,
        "maker_fallback_used": fallback, "is_win": True,
    }
    t.update(extra)
    return t


def _losing_trade(pnl=-8.0, fees=0.5, fallback=False, **extra):
    t = {
        "pnl": pnl, "pnl_gross": pnl + fees, "fees": fees,
        "r_multiple": pnl / 5.0, "mae": 1.2, "slippage": 0.3,
        "maker_fallback_used": fallback, "is_win": False,
    }
    t.update(extra)
    return t


# ── Tests ────────────────────────────────────────────────────────────

class TestComputeGroupKpis(unittest.TestCase):
    """Test compute_group_kpis."""

    def test_empty_trades(self):
        kpis = compute_group_kpis([])
        self.assertEqual(kpis["trades"], 0)
        self.assertEqual(kpis["win_rate"], 0.0)
        self.assertEqual(kpis["profit_factor"], 0.0)

    def test_single_winner(self):
        kpis = compute_group_kpis([_winning_trade(pnl=10, fees=0.5)])
        self.assertEqual(kpis["trades"], 1)
        self.assertEqual(kpis["wins"], 1)
        self.assertEqual(kpis["win_rate"], 1.0)
        self.assertGreater(kpis["profit_factor"], 0)
        self.assertAlmostEqual(kpis["net_pnl"], 10.0, places=2)

    def test_single_loser(self):
        kpis = compute_group_kpis([_losing_trade(pnl=-8, fees=0.5)])
        self.assertEqual(kpis["trades"], 1)
        self.assertEqual(kpis["wins"], 0)
        self.assertEqual(kpis["win_rate"], 0.0)

    def test_mixed_trades(self):
        trades = [_winning_trade(pnl=15), _losing_trade(pnl=-5), _winning_trade(pnl=10)]
        kpis = compute_group_kpis(trades)
        self.assertEqual(kpis["trades"], 3)
        self.assertEqual(kpis["wins"], 2)
        self.assertAlmostEqual(kpis["win_rate"], 2.0 / 3.0, places=3)
        self.assertGreater(kpis["profit_factor"], 1.0)

    def test_kpis_include_slippage_and_mae(self):
        trades = [_winning_trade(mae=0.5, slippage=0.2), _winning_trade(mae=1.0, slippage=0.4)]
        kpis = compute_group_kpis(trades)
        self.assertAlmostEqual(kpis["avg_mae"], 0.75, places=2)
        self.assertAlmostEqual(kpis["avg_slippage"], 0.3, places=2)


class TestSplitTrades(unittest.TestCase):
    """Test split_trades_by_execution_mode."""

    def test_all_maker(self):
        trades = [_winning_trade(fallback=False), _losing_trade(fallback=False)]
        maker, taker = split_trades_by_execution_mode(trades)
        self.assertEqual(len(maker), 2)
        self.assertEqual(len(taker), 0)

    def test_all_taker(self):
        trades = [_winning_trade(fallback=True), _losing_trade(fallback=True)]
        maker, taker = split_trades_by_execution_mode(trades)
        self.assertEqual(len(maker), 0)
        self.assertEqual(len(taker), 2)

    def test_mixed_split(self):
        trades = [
            _winning_trade(fallback=False),
            _winning_trade(fallback=True),
            _losing_trade(fallback=False),
            _losing_trade(fallback=True),
        ]
        maker, taker = split_trades_by_execution_mode(trades)
        self.assertEqual(len(maker), 2)
        self.assertEqual(len(taker), 2)

    def test_missing_key_defaults_to_maker(self):
        trades = [{"pnl": 5.0}]  # no maker_fallback_used key
        maker, taker = split_trades_by_execution_mode(trades)
        self.assertEqual(len(maker), 1)
        self.assertEqual(len(taker), 0)


class TestAnalyzeExecutionQuality(unittest.TestCase):
    """Full analysis tests."""

    def test_no_trades(self):
        result = analyze_execution_quality([])
        self.assertEqual(result["maker"]["trades"], 0)
        self.assertEqual(result["taker"]["trades"], 0)
        self.assertEqual(result["fallback_rate"], 0.0)
        self.assertFalse(result["taker_degrading_edge"])

    def test_all_maker_no_flag(self):
        trades = [_winning_trade(fallback=False) for _ in range(10)]
        result = analyze_execution_quality(trades)
        self.assertEqual(result["maker"]["trades"], 10)
        self.assertEqual(result["taker"]["trades"], 0)
        self.assertFalse(result["taker_degrading_edge"])
        self.assertAlmostEqual(result["fallback_rate"], 0.0)

    def test_taker_degrading_edge_flag(self):
        """5+ taker trades with PF < 1.0 → flag active."""
        trades = [_losing_trade(fallback=True) for _ in range(6)]
        result = analyze_execution_quality(trades)
        self.assertTrue(result["taker_degrading_edge"])
        self.assertIsNotNone(result["taker_degrading_reason"])
        self.assertTrue(any("TAKER_DEGRADING_EDGE" in w for w in result["warnings"]))

    def test_taker_good_no_flag(self):
        """5+ taker trades with PF > 1.0 → no flag."""
        trades = [_winning_trade(fallback=True, pnl=20) for _ in range(6)]
        result = analyze_execution_quality(trades)
        self.assertFalse(result["taker_degrading_edge"])

    def test_low_taker_count_no_flag(self):
        """< 5 taker trades → no flag even if bad PF."""
        trades = [_losing_trade(fallback=True) for _ in range(3)]
        result = analyze_execution_quality(trades)
        self.assertFalse(result["taker_degrading_edge"])

    def test_high_fallback_rate_warning(self):
        """Fallback rate > 40% → warning."""
        maker = [_winning_trade(fallback=False) for _ in range(3)]
        taker = [_winning_trade(fallback=True) for _ in range(7)]
        result = analyze_execution_quality(maker + taker)
        self.assertGreater(result["fallback_rate"], FALLBACK_RATE_WARNING)
        self.assertTrue(any("FALLBACK_RATE_HIGH" in w for w in result["warnings"]))

    def test_fallback_rate_calculation(self):
        maker = [_winning_trade(fallback=False) for _ in range(8)]
        taker = [_winning_trade(fallback=True) for _ in range(2)]
        result = analyze_execution_quality(maker + taker)
        self.assertAlmostEqual(result["fallback_rate"], 0.2, places=2)


class TestTakerBlockingPromotion(unittest.TestCase):
    """Test is_taker_blocking_promotion gate."""

    def test_no_degradation_no_block(self):
        eq = {"taker_degrading_edge": False, "fallback_rate": 0.1,
              "taker": {"trades": 10, "profit_factor": 1.5}}
        blocked, _ = is_taker_blocking_promotion(eq)
        self.assertFalse(blocked)

    def test_degrading_edge_blocks(self):
        eq = {"taker_degrading_edge": True, "taker_degrading_reason": "PF bad",
              "taker": {"trades": 10, "profit_factor": 0.7}}
        blocked, reason = is_taker_blocking_promotion(eq)
        self.assertTrue(blocked)
        self.assertIn("TAKER_DEGRADING_EDGE", reason)

    def test_high_fallback_and_low_pf_blocks(self):
        eq = {"taker_degrading_edge": False, "fallback_rate": 0.5,
              "taker": {"trades": 10, "profit_factor": 1.0}}
        blocked, reason = is_taker_blocking_promotion(eq)
        self.assertTrue(blocked)
        self.assertIn("HIGH_FALLBACK_RATE", reason)

    def test_high_fallback_but_good_pf_no_block(self):
        eq = {"taker_degrading_edge": False, "fallback_rate": 0.5,
              "taker": {"trades": 10, "profit_factor": 1.2}}
        blocked, _ = is_taker_blocking_promotion(eq)
        self.assertFalse(blocked)


class TestTakerBlockingScaleup(unittest.TestCase):
    """Test is_taker_blocking_scaleup gate."""

    def test_good_taker_no_block(self):
        eq = {"taker": {"trades": 10, "profit_factor": 1.3}}
        blocked, _ = is_taker_blocking_scaleup(eq)
        self.assertFalse(blocked)

    def test_bad_taker_blocks(self):
        eq = {"taker": {"trades": 10, "profit_factor": 0.9}}
        blocked, reason = is_taker_blocking_scaleup(eq)
        self.assertTrue(blocked)
        self.assertIn("Taker PF", reason)

    def test_few_taker_trades_no_block(self):
        eq = {"taker": {"trades": 3, "profit_factor": 0.5}}
        blocked, _ = is_taker_blocking_scaleup(eq)
        self.assertFalse(blocked)


if __name__ == "__main__":
    unittest.main()
