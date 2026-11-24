"""
Unit tests for RiskKillSwitch — Phase 8.2

Covers:
    - Block on daily loss % breach
    - Block on daily loss absolute breach
    - Block on session (ASIA) loss % breach
    - Block on consecutive losses (ASIA)
    - No block in normal conditions
    - No block for non-ASIA sessions (session checks skipped)
    - Never raises exceptions (safety guarantee)
    - Factory from_config
    - JSONL log output
"""

import unittest
import json
import shutil
import sys
from pathlib import Path
from datetime import date

# Project root
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from smc_futures.risk.kill_switch import RiskKillSwitch


class TestRiskKillSwitch(unittest.TestCase):
    """Unit tests for the RiskKillSwitch module."""

    def setUp(self):
        self.log_dir = project_root / "reports" / "risk_test"
        if self.log_dir.exists():
            shutil.rmtree(self.log_dir)

    def tearDown(self):
        if self.log_dir.exists():
            shutil.rmtree(self.log_dir)

    # ── Daily loss % ─────────────────────────────────────────────────

    def test_block_on_max_daily_loss_pct(self):
        """Daily loss >= threshold → blocked."""
        ks = RiskKillSwitch(max_daily_loss_pct=2.0)
        blocked, reason = ks.is_blocked(
            session="london",
            target_date=date(2026, 3, 1),
            daily_pnl=-250.0,
            session_pnl=-100.0,
            equity_start=10_000.0,
        )
        self.assertTrue(blocked)
        self.assertIn("DAILY_LOSS_PCT", reason)

    def test_no_block_daily_loss_below_threshold(self):
        """Daily loss below threshold → not blocked."""
        ks = RiskKillSwitch(max_daily_loss_pct=2.0)
        blocked, reason = ks.is_blocked(
            session="london",
            target_date=date(2026, 3, 1),
            daily_pnl=-100.0,
            session_pnl=-50.0,
            equity_start=10_000.0,
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_exact_daily_loss_pct_boundary(self):
        """Daily loss exactly at threshold → blocked."""
        ks = RiskKillSwitch(max_daily_loss_pct=2.0)
        # 200 / 10000 = 2.0% exactly
        blocked, reason = ks.is_blocked(
            session="ny",
            target_date=date(2026, 3, 1),
            daily_pnl=-200.0,
            session_pnl=-200.0,
            equity_start=10_000.0,
        )
        self.assertTrue(blocked)

    # ── Daily loss absolute ──────────────────────────────────────────

    def test_block_on_max_daily_loss_abs(self):
        """Daily loss absolute >= threshold → blocked."""
        ks = RiskKillSwitch(max_daily_loss_pct=0, max_daily_loss_abs=500.0, l3_loss_pct=0)
        blocked, reason = ks.is_blocked(
            session="london",
            target_date=date(2026, 3, 1),
            daily_pnl=-600.0,
            session_pnl=-200.0,
            equity_start=10_000.0,
        )
        self.assertTrue(blocked)
        self.assertIn("DAILY_LOSS_ABS", reason)

    def test_no_block_daily_loss_abs_disabled(self):
        """max_daily_loss_abs=0 means disabled."""
        ks = RiskKillSwitch(max_daily_loss_pct=0, max_daily_loss_abs=0, l3_loss_pct=0)
        blocked, _ = ks.is_blocked(
            session="london",
            target_date=date(2026, 3, 1),
            daily_pnl=-9999.0,
            session_pnl=0.0,
            equity_start=10_000.0,
        )
        self.assertFalse(blocked)

    # ── ASIA session loss % ──────────────────────────────────────────

    def test_block_on_max_session_loss_pct_asia(self):
        """ASIA session loss >= threshold → blocked."""
        ks = RiskKillSwitch(
            max_daily_loss_pct=10.0,   # won't trigger
            max_session_loss_pct_asia=1.0,
        )
        blocked, reason = ks.is_blocked(
            session="asia",
            target_date=date(2026, 3, 1),
            daily_pnl=-50.0,
            session_pnl=-150.0,
            equity_start=10_000.0,
        )
        self.assertTrue(blocked)
        self.assertIn("SESSION_LOSS_PCT_ASIA", reason)

    def test_no_block_session_loss_non_asia(self):
        """Session loss check only applies to ASIA."""
        ks = RiskKillSwitch(
            max_daily_loss_pct=10.0,
            max_session_loss_pct_asia=0.01,  # very tight
        )
        blocked, _ = ks.is_blocked(
            session="london",
            target_date=date(2026, 3, 1),
            daily_pnl=-50.0,
            session_pnl=-500.0,
            equity_start=10_000.0,
        )
        self.assertFalse(blocked)

    # ── Consecutive losses (ASIA) ────────────────────────────────────

    def test_block_on_consecutive_losses_asia(self):
        """Consecutive losses in ASIA >= max → blocked."""
        ks = RiskKillSwitch(
            max_daily_loss_pct=10.0,
            max_session_loss_pct_asia=10.0,
            max_consecutive_losses_asia=6,
        )
        blocked, reason = ks.is_blocked(
            session="ASIA",  # case-insensitive
            target_date=date(2026, 3, 1),
            daily_pnl=0.0,
            session_pnl=0.0,
            equity_start=10_000.0,
            consecutive_losses=6,
        )
        self.assertTrue(blocked)
        self.assertIn("CONSECUTIVE_LOSSES_ASIA", reason)

    def test_no_block_consecutive_losses_below(self):
        """Consecutive losses below max → not blocked."""
        ks = RiskKillSwitch(
            max_daily_loss_pct=10.0,
            max_consecutive_losses_asia=6,
        )
        blocked, _ = ks.is_blocked(
            session="asia",
            target_date=date(2026, 3, 1),
            daily_pnl=0.0,
            session_pnl=0.0,
            equity_start=10_000.0,
            consecutive_losses=5,
        )
        self.assertFalse(blocked)

    # ── Safety guarantee: never raises ───────────────────────────────

    def test_never_raises_on_bad_input(self):
        """Pass garbage — should return (False, '') not crash."""
        ks = RiskKillSwitch()

        # None equity_start → would cause divide-by-zero inside _evaluate
        blocked, reason = ks.is_blocked(
            session="asia",
            target_date=date(2026, 3, 1),
            daily_pnl=None,  # type: ignore
            session_pnl="bad",  # type: ignore
            equity_start=None,  # type: ignore
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_never_raises_on_zero_equity(self):
        """Zero equity → skip pct check, don't crash."""
        ks = RiskKillSwitch(max_daily_loss_pct=2.0)
        blocked, reason = ks.is_blocked(
            session="london",
            target_date=date(2026, 3, 1),
            daily_pnl=-100.0,
            session_pnl=-100.0,
            equity_start=0.0,
        )
        # Should NOT crash; pct check skipped when equity=0
        self.assertFalse(blocked)

    # ── Positive PnL → never blocked ────────────────────────────────

    def test_no_block_when_profitable(self):
        """Positive daily PnL → no loss trigger."""
        ks = RiskKillSwitch(max_daily_loss_pct=2.0)
        blocked, _ = ks.is_blocked(
            session="asia",
            target_date=date(2026, 3, 1),
            daily_pnl=500.0,
            session_pnl=200.0,
            equity_start=10_000.0,
        )
        self.assertFalse(blocked)

    # ── Factory from_config ──────────────────────────────────────────

    def test_from_config(self):
        """RiskKillSwitch.from_config reads config dict correctly."""
        cfg = {
            "MAX_DAILY_LOSS_PCT": 3.5,
            "MAX_DAILY_LOSS_ABS": 1000.0,
            "MAX_SESSION_LOSS_PCT_ASIA": 2.0,
            "MAX_CONSECUTIVE_LOSSES_ASIA": 4,
            "KILL_SWITCH_MODE": "alert_only",
        }
        ks = RiskKillSwitch.from_config(cfg, log_dir=self.log_dir)
        self.assertEqual(ks.max_daily_loss_pct, 3.5)
        self.assertEqual(ks.max_daily_loss_abs, 1000.0)
        self.assertEqual(ks.max_session_loss_pct_asia, 2.0)
        self.assertEqual(ks.max_consecutive_losses_asia, 4)
        self.assertEqual(ks.kill_switch_mode, "alert_only")
        self.assertEqual(ks.log_dir, self.log_dir)

    def test_from_config_defaults(self):
        """Missing keys → falls back to defaults."""
        ks = RiskKillSwitch.from_config({})
        self.assertEqual(ks.max_daily_loss_pct, 2.0)
        self.assertEqual(ks.max_daily_loss_abs, 0.0)
        self.assertEqual(ks.max_consecutive_losses_asia, 6)

    # ── JSONL log output ─────────────────────────────────────────────

    def test_log_file_written_on_block(self):
        """Log file created with structured JSON entry on block."""
        ks = RiskKillSwitch(max_daily_loss_pct=1.0, log_dir=self.log_dir)
        target = date(2026, 3, 15)
        ks.is_blocked(
            session="london",
            target_date=target,
            daily_pnl=-200.0,
            session_pnl=-200.0,
            equity_start=10_000.0,
        )
        fpath = self.log_dir / "kill_switch_20260315.jsonl"
        self.assertTrue(fpath.exists(), f"Expected log file: {fpath}")
        with open(fpath, "r", encoding="utf-8") as f:
            entry = json.loads(f.readline())
        self.assertEqual(entry["status"], "BLOCKED")
        self.assertEqual(entry["session"], "london")
        self.assertIn("DAILY_LOSS_PCT", entry["reason"])

    def test_log_file_written_on_ok(self):
        """Log file also written for OK status (audit trail)."""
        ks = RiskKillSwitch(max_daily_loss_pct=5.0, log_dir=self.log_dir)
        target = date(2026, 4, 1)
        ks.is_blocked(
            session="ny",
            target_date=target,
            daily_pnl=-10.0,
            session_pnl=-5.0,
            equity_start=10_000.0,
        )
        fpath = self.log_dir / "kill_switch_20260401.jsonl"
        self.assertTrue(fpath.exists())
        with open(fpath, "r", encoding="utf-8") as f:
            entry = json.loads(f.readline())
        self.assertEqual(entry["status"], "OK")

    # ── Priority: daily check before session check ───────────────────

    def test_daily_loss_blocks_before_session(self):
        """Daily loss triggers first, even if session check would also fire."""
        ks = RiskKillSwitch(
            max_daily_loss_pct=1.0,
            max_session_loss_pct_asia=0.5,
        )
        blocked, reason = ks.is_blocked(
            session="asia",
            target_date=date(2026, 3, 1),
            daily_pnl=-200.0,
            session_pnl=-100.0,
            equity_start=10_000.0,
        )
        self.assertTrue(blocked)
        # Daily check fires first
        self.assertIn("DAILY_LOSS_PCT", reason)


if __name__ == "__main__":
    unittest.main()
