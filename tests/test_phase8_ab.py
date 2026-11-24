import unittest
import json
import os
import shutil
import sys
from pathlib import Path
from datetime import datetime

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from smc_futures.engine.ab_allocator import select_engine, stable_hash
from backend.services.ab_testing_service import ABTestingService
from smc_futures.engine.strategy.asia_weekend_strategy import AsiaWeekendStrategy

class TestPhase8AB(unittest.TestCase):
    def setUp(self):
        self.test_dir = project_root / "reports" / "ab_tests_verify"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir(parents=True)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

    def test_ab_allocator_deterministic(self):
        key = "BTCUSDT|asia|2026-03-02"
        seed = 42
        weight = 0.5
        
        # Should always be the same for same input
        first = select_engine(key, seed, weight)
        for _ in range(10):
            self.assertEqual(select_engine(key, seed, weight), first)
            
        # Different seed or key should potentially change it
        different = select_engine(key, 999, weight)
        # Note: hash might still land in same bucket by chance, but mostly different
        
    def test_ab_weight_boundaries(self):
        key = "random"
        self.assertEqual(select_engine(key, 1, 0.0), "A")
        self.assertEqual(select_engine(key, 1, 1.0), "B")

    def test_ab_service_structured_logging(self):
        service = ABTestingService(self.test_dir)
        
        data = {
            "mode": "shadow",
            "selected": "A",
            "A": {"accept": True, "threshold": 6.0},
            "B": {"accept": False, "threshold": 5.0},
            "key": "BTC|asia|20260302",
            "seed": 42,
            "weight": 0.0
        }
        
        service.record_decision("BTCUSDT", data)
        
        # Verify file exists and content is structured
        log_files = list(self.test_dir.glob("decisions_*.jsonl"))
        self.assertEqual(len(log_files), 1)
        
        with open(log_files[0], "r") as f:
            entry = json.loads(f.readline())
            self.assertEqual(entry["event"], "AB_DECISION")
            self.assertEqual(entry["symbol"], "BTCUSDT")
            self.assertEqual(entry["A"]["accept"], True)

    def test_strategy_metadata_capture(self):
        try:
            # We need a minimal mock or real strategy init
            strategy = AsiaWeekendStrategy()
            strategy.ab_test_service = ABTestingService(self.test_dir)
            
            candle = {"open": 100, "high": 101, "low": 99, "close": 100, "timestamp": 123456789}
            candles = [candle] * 20
            
            # Test build_signal
            result = strategy.build_signal(
                symbol="ETHUSDT",
                candles_htf=[],
                candles_mtf=candles,
                candles_ltf=candles,
                structure_state={},
                liquidity_state={},
                ob_state={},
                fvg_state={},
                current_price=100.0,
                timestamp=123456789,
                atr=1.0
            )
            
            self.assertIn("ab_test", result.metadata)
            ab = result.metadata["ab_test"]
            self.assertIn("A", ab)
            self.assertIn("selected", ab)
            
            # Check if service recorded it
            log_files = list(self.test_dir.glob("decisions_*.jsonl"))
            self.assertGreaterEqual(len(log_files), 1)
        except Exception as e:
            import traceback
            print(f"\n[ERROR] Strategy Metadata Capture Failed: {e}")
            traceback.print_exc()
            raise

    # ── Additional Phase 8 Tests ──────────────────────────────────────

    def test_mode_shadow_always_selects_A(self):
        """In shadow mode with weight=0.0 (default), engine A is always chosen."""
        key = "BTCUSDT|asia|2026-03-05"
        seed = 42
        # Shadow mode uses weight 0.0 → always A
        for _ in range(50):
            self.assertEqual(select_engine(key, seed, 0.0), "A")

    def test_live_split_respects_weight(self):
        """With weight = 1.0, engine B is always selected (live_split max)."""
        key = "ETHUSDT|london|2026-03-05"
        seed = 42
        self.assertEqual(select_engine(key, seed, 1.0), "B")

    def test_live_split_distribution(self):
        """With weight = 0.5, roughly 50% should go to each engine."""
        seed = 42
        count_b = 0
        n = 1000
        for i in range(n):
            key = f"BTCUSDT|asia|2026-03-{i:04d}"
            if select_engine(key, seed, 0.5) == "B":
                count_b += 1
        ratio = count_b / n
        # Should be roughly 0.5 +/- 0.1
        self.assertGreater(ratio, 0.35, f"B ratio too low: {ratio:.3f}")
        self.assertLess(ratio, 0.65, f"B ratio too high: {ratio:.3f}")

    def test_stable_hash_deterministic(self):
        """stable_hash always returns the same value for the same input."""
        val1 = stable_hash("test_string")
        val2 = stable_hash("test_string")
        self.assertEqual(val1, val2)
        # Different input → different output
        val3 = stable_hash("other_string")
        self.assertNotEqual(val1, val3)

    def test_generate_ab_report_empty(self):
        """generate_ab_report on a date with no data should return zeroed report."""
        service = ABTestingService(self.test_dir)
        report = service.generate_ab_report(target_date="19700101")
        
        self.assertEqual(report["total_decisions"], 0)
        self.assertEqual(report["static_accepts"], 0)
        self.assertEqual(report["adaptive_accepts"], 0)
        self.assertEqual(report["conflicts"], 0)
        self.assertEqual(report["recommendation"], "hold")

    def test_generate_ab_report_with_data(self):
        """generate_ab_report correctly computes stats from recorded decisions."""
        service = ABTestingService(self.test_dir)
        date_str = "20260305"
        
        # Write mock decisions directly
        file_path = self.test_dir / f"decisions_{date_str}.jsonl"
        decisions = [
            {"event": "AB_DECISION", "mode": "shadow", "selected": "A",
             "symbol": "BTCUSDT", "key": "BTCUSDT|asia|2026-03-05",
             "A": {"accept": True, "threshold": 6.0},
             "B": {"accept": True, "threshold": 5.5}},
            {"event": "AB_DECISION", "mode": "shadow", "selected": "A",
             "symbol": "BTCUSDT", "key": "BTCUSDT|london|2026-03-05",
             "A": {"accept": True, "threshold": 6.0},
             "B": {"accept": False, "threshold": 5.0, "reason": "BELOW_RANGE"}},
            {"event": "AB_DECISION", "mode": "shadow", "selected": "A",
             "symbol": "ETHUSDT", "key": "ETHUSDT|asia|2026-03-05",
             "A": {"accept": False, "threshold": 6.0, "reason": "NO_LIQUIDITY"},
             "B": {"accept": True, "threshold": 4.5}},
        ]
        with open(file_path, "w", encoding="utf-8") as f:
            for d in decisions:
                f.write(json.dumps(d) + "\n")
        
        report = service.generate_ab_report(target_date=date_str)
        
        self.assertEqual(report["total_decisions"], 3)
        self.assertEqual(report["static_accepts"], 2)
        self.assertEqual(report["adaptive_accepts"], 2)
        self.assertEqual(report["conflicts"], 2)  # row 2 and row 3 disagree
        self.assertIn("BTCUSDT", report["symbols"])
        self.assertIn("ETHUSDT", report["symbols"])
        self.assertEqual(report["recommendation"], "hold")  # <10 decisions
        
        # Verify files were written
        self.assertTrue((self.test_dir / f"ab_{date_str}.json").exists())
        self.assertTrue((self.test_dir / f"ab_{date_str}.md").exists())

    def test_generate_ab_report_recommendation_increase(self):
        """If B accepts more and conflicts are low, recommend increase_weight."""
        service = ABTestingService(self.test_dir)
        date_str = "20260310"
        file_path = self.test_dir / f"decisions_{date_str}.jsonl"
        
        # 15 decisions: both agree on most (low conflicts), B slightly more permissive
        # Rows 0-7: both accept (no conflict)
        # Rows 8-10: only B accepts (3 conflicts)
        # Rows 11-14: both reject (no conflict)
        # A accepts 8/15 = 53.3%, B accepts 11/15 = 73.3%, conflicts = 3 (20% < 30%)
        decisions = []
        for i in range(15):
            a_accept = i < 8
            b_accept = i < 11
            d = {
                "event": "AB_DECISION", "mode": "live_split", "selected": "A",
                "symbol": "BTCUSDT", "key": f"BTCUSDT|asia|2026-03-{i:02d}",
                "A": {"accept": a_accept, "reason": "BELOW_RANGE" if not a_accept else None},
                "B": {"accept": b_accept, "reason": "LOW_VOL" if not b_accept else None},
            }
            decisions.append(d)
        
        with open(file_path, "w", encoding="utf-8") as f:
            for d in decisions:
                f.write(json.dumps(d) + "\n")
        
        report = service.generate_ab_report(target_date=date_str)
        self.assertEqual(report["recommendation"], "increase_weight")

    def test_generate_ab_report_recommendation_revert(self):
        """If A accepts much more than B, recommend revert."""
        service = ABTestingService(self.test_dir)
        date_str = "20260311"
        file_path = self.test_dir / f"decisions_{date_str}.jsonl"
        
        # 15 decisions: A accepts 14, B accepts 2 → A much better
        decisions = []
        for i in range(15):
            a_accept = i < 14
            b_accept = i < 2
            d = {
                "event": "AB_DECISION", "mode": "live_split", "selected": "A",
                "symbol": "ETHUSDT", "key": f"ETHUSDT|london|2026-03-{i:02d}",
                "A": {"accept": a_accept, "reason": "NO_OB" if not a_accept else None},
                "B": {"accept": b_accept, "reason": "STRICT" if not b_accept else None},
            }
            decisions.append(d)
        
        with open(file_path, "w", encoding="utf-8") as f:
            for d in decisions:
                f.write(json.dumps(d) + "\n")
        
        report = service.generate_ab_report(target_date=date_str)
        self.assertEqual(report["recommendation"], "revert")

    def test_reporting_ab_section_renders_markdown(self):
        """The _render_ab_markdown method produces valid Markdown with all sections."""
        data = {
            "date": "20260305",
            "mode": "shadow",
            "total_decisions": 5,
            "static_accepts": 3,
            "adaptive_accepts": 2,
            "accept_rate_a": 60.0,
            "accept_rate_b": 40.0,
            "conflicts": 1,
            "conflict_rate": 20.0,
            "rejection_reasons_a": {"LOW_VOL": 2},
            "rejection_reasons_b": {"BELOW_RANGE": 3},
            "symbols": {"BTCUSDT": 3, "ETHUSDT": 2},
            "sessions": {"asia": 3, "london": 2},
            "recommendation": "hold",
            "recommendation_reason": "Engines within 5pp.",
            "generated_at": "2026-03-05T12:00:00+00:00",
        }
        
        md = ABTestingService._render_ab_markdown(data)
        
        # Check essential headers and content
        self.assertIn("# A/B Testing Report", md)
        self.assertIn("## Summary", md)
        self.assertIn("## Rejection Reasons", md)
        self.assertIn("### Engine A (Static)", md)
        self.assertIn("### Engine B (Adaptive)", md)
        self.assertIn("## Symbols Breakdown", md)
        self.assertIn("## Recommendation", md)
        self.assertIn("`hold`", md)
        self.assertIn("BTCUSDT", md)
        self.assertIn("LOW_VOL", md)
        self.assertIn("BELOW_RANGE", md)

    def test_load_decisions_nonexistent_file(self):
        """load_decisions returns empty list for nonexistent date."""
        service = ABTestingService(self.test_dir)
        result = service.load_decisions("99991231")
        self.assertEqual(result, [])

    def test_virtual_trade_recording_gated(self):
        """record_virtual_trade should not write when config flag is False."""
        service = ABTestingService(self.test_dir)
        service.record_virtual_trade("BTCUSDT", "B", {"pnl": 10.5})
        
        # Should not have written (default ADAPTIVE_SHADOW_WRITE_TRADES = False)
        trade_files = list(self.test_dir.glob("virtual_trades_*.jsonl"))
        self.assertEqual(len(trade_files), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
