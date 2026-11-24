"""
Integration Tests — Phase 8.1: Shadow → Live-Split end-to-end

Tests the full pipeline:
    1. Shadow mode:     A/B allocator picks engine, NO execution, decisions logged
    2. Live-split mode: A/B allocator picks engine, EXECUTION occurs, report valid
    3. Kill-switch:     blocks entry when daily loss exceeded
    4. Capital scaling: adjusts risk_per_trade in the pipeline

All exchange and DB interactions are mocked.
"""

import unittest
import json
import shutil
import sys
from pathlib import Path
from datetime import date, datetime, timezone, timedelta

# Project root
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(project_root))

from smc_futures.engine.ab_allocator import select_engine
from backend.services.ab_testing_service import ABTestingService
from smc_futures.risk.kill_switch import RiskKillSwitch
from scripts.reporting.capital_scaling_engine import (
    evaluate_scaling,
    effective_max_risk,
    load_scaling_state,
    save_scaling_state,
    record_scaling_decision,
)


class TestHarness:
    """
    Lightweight test harness that simulates the orchestrator pipeline
    without touching exchange APIs or real databases.
    """

    def __init__(self, tmp_dir: Path):
        self.tmp_dir = tmp_dir
        self.ab_service = ABTestingService(tmp_dir / "ab_tests")
        self.kill_switch = RiskKillSwitch(
            max_daily_loss_pct=2.0,
            max_session_loss_pct_asia=1.0,
            max_consecutive_losses_asia=6,
            log_dir=tmp_dir / "risk",
        )
        self.executed_orders: list = []
        self.daily_pnl = 0.0
        self.session_pnl = 0.0
        self.equity_start = 10_000.0

    def process_signal(
        self,
        symbol: str,
        session: str,
        mode: str,
        adaptive_weight: float,
        seed: int = 42,
        target_date: date | None = None,
        consecutive_losses: int = 0,
    ) -> dict:
        """
        Simulate the full signal pipeline.

        Returns:
            {
                "selected_engine": "A" | "B",
                "blocked": bool,
                "block_reason": str,
                "executed": bool,
                "mode": str,
            }
        """
        target_date = target_date or date.today()
        key = f"{symbol}|{session}|{target_date.isoformat()}"

        # 1. A/B allocation
        selected = select_engine(key, seed, adaptive_weight)

        # 2. Record decision
        decision_data = {
            "mode": mode,
            "selected": selected,
            "A": {"accept": True, "threshold": 6.0},
            "B": {"accept": True, "threshold": 5.5},
            "key": key,
            "seed": seed,
            "weight": adaptive_weight,
        }
        self.ab_service.record_decision(symbol, decision_data)

        # 3. Kill-switch check
        blocked, block_reason = self.kill_switch.is_blocked(
            session=session,
            target_date=target_date,
            daily_pnl=self.daily_pnl,
            session_pnl=self.session_pnl,
            equity_start=self.equity_start,
            consecutive_losses=consecutive_losses,
        )

        # 4. Execution decision
        executed = False
        if mode == "shadow":
            # Shadow mode: record only, never execute
            executed = False
        elif mode == "live_split" and not blocked:
            # Live split: execute the selected engine's signal
            self.executed_orders.append({
                "symbol": symbol,
                "engine": selected,
                "session": session,
                "date": target_date.isoformat(),
            })
            executed = True

        return {
            "selected_engine": selected,
            "blocked": blocked,
            "block_reason": block_reason,
            "executed": executed,
            "mode": mode,
        }


class TestShadowToLiveSplit(unittest.TestCase):
    """Integration test: full pipeline from shadow → live_split."""

    def setUp(self):
        self.tmp_dir = project_root / "reports" / "_integration_test"
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir)
        self.tmp_dir.mkdir(parents=True)

    def tearDown(self):
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir)

    # ── Test 1: Shadow mode selects engine but does NOT execute ──────

    def test_shadow_mode_no_execution(self):
        """In shadow mode, A/B allocator runs and decisions logged,
        but no orders are executed."""
        harness = TestHarness(self.tmp_dir)

        result = harness.process_signal(
            symbol="BTCUSDT",
            session="asia",
            mode="shadow",
            adaptive_weight=0.0,  # weight=0 → always "A"
            seed=42,
        )

        # Engine selected correctly
        self.assertEqual(result["selected_engine"], "A")
        # NOT executed (shadow)
        self.assertFalse(result["executed"])
        self.assertEqual(len(harness.executed_orders), 0)
        # Decision was logged
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        decisions_file = self.tmp_dir / "ab_tests" / f"decisions_{date_str}.jsonl"
        self.assertTrue(decisions_file.exists())
        with open(decisions_file, "r", encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["mode"], "shadow")
        self.assertEqual(entries[0]["selected"], "A")

    # ── Test 2: Live-split is deterministic and executes ─────────────

    def test_live_split_deterministic_execution(self):
        """In live_split mode, A/B allocation is deterministic and
        the selected engine's signal is executed."""
        harness = TestHarness(self.tmp_dir)
        target = date(2026, 6, 15)
        seed = 42
        weight = 0.5  # 50/50 split

        # Run the same signal 5 times — should always select same engine
        results = []
        for _ in range(5):
            harness.executed_orders.clear()
            r = harness.process_signal(
                symbol="ETHUSDT",
                session="london",
                mode="live_split",
                adaptive_weight=weight,
                seed=seed,
                target_date=target,
            )
            results.append(r)

        # All 5 should produce the same engine
        engines = [r["selected_engine"] for r in results]
        self.assertEqual(len(set(engines)), 1, "Allocation must be deterministic")

        # All executed (no loss block)
        for r in results:
            self.assertTrue(r["executed"])

    # ── Test 3: Kill-switch blocks live_split when loss breached ─────

    def test_kill_switch_blocks_live_split(self):
        """When daily loss exceeds threshold, live_split entries are blocked."""
        harness = TestHarness(self.tmp_dir)
        # Simulate loss exceeding 2% of 10 000 equity
        harness.daily_pnl = -250.0   # 2.5% loss

        result = harness.process_signal(
            symbol="BTCUSDT",
            session="asia",
            mode="live_split",
            adaptive_weight=0.3,
        )

        self.assertTrue(result["blocked"])
        self.assertIn("DAILY_LOSS_PCT", result["block_reason"])
        self.assertFalse(result["executed"])
        self.assertEqual(len(harness.executed_orders), 0)

    # ── Test 4: Shadow mode is unaffected by kill-switch ─────────────

    def test_shadow_never_executes_even_without_block(self):
        """Shadow mode never executes, regardless of PnL state."""
        harness = TestHarness(self.tmp_dir)
        harness.daily_pnl = 500.0  # profitable day

        result = harness.process_signal(
            symbol="BTCUSDT",
            session="london",
            mode="shadow",
            adaptive_weight=0.5,
        )

        self.assertFalse(result["blocked"])
        self.assertFalse(result["executed"])

    # ── Test 5: Capital scaling adjusts risk in pipeline ─────────────

    def test_capital_scaling_integration(self):
        """Capital scaling engine runs alongside the pipeline and
        adjusts risk_per_trade based on performance + adaptive weight."""
        # Step 1: initial state
        state = load_scaling_state(self.tmp_dir / "scaling")
        initial_risk = state["current_risk_pct"]  # 0.5 default

        # Step 2: Good KPIs → scale up
        kpis = {
            "profit_factor": 1.6,
            "max_dd_pct": 2.0,
            "trades": 55,
            "winrate": 0.58,
        }
        new_pct, decision, reason = evaluate_scaling(
            current_risk_pct=initial_risk,
            kpis_30d=kpis,
            adaptive_weight=0.3,
            state=state,
        )
        self.assertEqual(decision, "SCALE_UP")
        self.assertGreater(new_pct, initial_risk)

        # Step 3: Save new state
        state["current_risk_pct"] = new_pct
        state["last_change_ts"] = datetime.now(timezone.utc).isoformat()
        state["last_decision"] = decision
        save_scaling_state(state, self.tmp_dir / "scaling")

        # Step 4: Record for audit
        record_scaling_decision(
            decision=decision,
            previous=initial_risk,
            new=new_pct,
            reason=reason,
            kpis_30d=kpis,
            adaptive_weight=0.3,
            eff_max=effective_max_risk(0.8, 0.3),
            base_dir=self.tmp_dir / "scaling",
        )

        # Step 5: Verify persistence
        loaded = load_scaling_state(self.tmp_dir / "scaling")
        self.assertAlmostEqual(loaded["current_risk_pct"], new_pct)
        self.assertEqual(loaded["last_decision"], "SCALE_UP")

        # Step 6: Verify audit file
        files = list((self.tmp_dir / "scaling").glob("scaling_decision_*.json"))
        self.assertTrue(len(files) >= 1)

    # ── Test 6: Full sequence shadow → live_split with all guards ────

    def test_full_sequence_shadow_then_live_split(self):
        """End-to-end: start in shadow, switch to live_split,
        verify kill-switch and capital scaling work together."""
        harness = TestHarness(self.tmp_dir)

        # Phase A: Shadow (weight=0 → engine A)
        r1 = harness.process_signal(
            symbol="BTCUSDT",
            session="asia",
            mode="shadow",
            adaptive_weight=0.0,
        )
        self.assertFalse(r1["executed"])
        self.assertEqual(r1["selected_engine"], "A")

        # Phase B: Promote to live_split (weight=0.3 → some traffic to B)
        r2 = harness.process_signal(
            symbol="BTCUSDT",
            session="asia",
            mode="live_split",
            adaptive_weight=0.3,
        )
        self.assertTrue(r2["executed"])
        self.assertEqual(len(harness.executed_orders), 1)

        # Phase C: Big loss hits → kill-switch triggers
        harness.daily_pnl = -300.0  # 3% of 10k
        r3 = harness.process_signal(
            symbol="ETHUSDT",
            session="london",
            mode="live_split",
            adaptive_weight=0.3,
        )
        self.assertTrue(r3["blocked"])
        self.assertFalse(r3["executed"])
        # No new execution
        self.assertEqual(len(harness.executed_orders), 1)

        # Phase D: Capital scaling reacts
        kpis = {"profit_factor": 0.8, "max_dd_pct": 4.0, "trades": 20, "winrate": 0.40}
        new_pct, decision, _ = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=kpis,
            adaptive_weight=0.3,
        )
        self.assertEqual(decision, "SCALE_DOWN")
        self.assertLess(new_pct, 0.5)

    # ── Test 7: AB report generation after decisions ─────────────────

    def test_ab_report_generation(self):
        """After shadow decisions are recorded, generate_ab_report produces
        a valid report dict."""
        harness = TestHarness(self.tmp_dir)

        # Record several decisions
        for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            harness.process_signal(
                symbol=symbol,
                session="asia",
                mode="shadow",
                adaptive_weight=0.5,
                seed=42,
            )

        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        report = harness.ab_service.generate_ab_report(date_str)

        self.assertIn("total_decisions", report)
        self.assertEqual(report["total_decisions"], 3)
        self.assertIn("mode", report)

    # ── Test 8: ASIA consecutive losses block via harness ────────────

    def test_asia_consecutive_losses_block(self):
        """ASIA session with 6 consecutive losses → blocked."""
        harness = TestHarness(self.tmp_dir)

        result = harness.process_signal(
            symbol="BTCUSDT",
            session="asia",
            mode="live_split",
            adaptive_weight=0.3,
            consecutive_losses=6,
        )

        self.assertTrue(result["blocked"])
        self.assertIn("CONSECUTIVE_LOSSES_ASIA", result["block_reason"])
        self.assertFalse(result["executed"])


if __name__ == "__main__":
    unittest.main()
