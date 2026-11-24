"""
Unit tests for Capital Scaling Engine — Phase 8.3

Covers:
    - SCALE_UP when all requirements met (PF, DD, trades)
    - SCALE_DOWN on bad PF
    - SCALE_DOWN on DD breach
    - Effective max cap applied with adaptive weight
    - Cooldown prevents changes
    - HOLD when conditions neutral
    - Min / max clamping
    - Persistence helpers (load, save, record)
    - effective_max_risk formula
"""

import unittest
import json
import shutil
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Project root
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from scripts.reporting.capital_scaling_engine import (
    evaluate_scaling,
    effective_max_risk,
    effective_risk_pct,
    load_scaling_state,
    save_scaling_state,
    record_scaling_decision,
    DEFAULTS,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _good_kpis(**overrides) -> dict:
    """KPIs that meet all scale-up requirements."""
    base = {
        "profit_factor": 1.5,
        "max_dd_pct": 3.0,
        "trades": 55,
        "winrate": 0.55,
        "losing_streak": 0,
    }
    base.update(overrides)
    return base


def _bad_kpis(**overrides) -> dict:
    """KPIs that trigger scale-down."""
    base = {
        "profit_factor": 0.7,
        "max_dd_pct": 8.0,
        "trades": 55,
        "winrate": 0.35,
        "losing_streak": 0,
    }
    base.update(overrides)
    return base


def _neutral_kpis(**overrides) -> dict:
    """KPIs that trigger neither up nor down (PF ok, not enough trades)."""
    base = {
        "profit_factor": 1.3,
        "max_dd_pct": 4.0,
        "trades": 15,  # < 50 minimum
        "winrate": 0.50,
        "losing_streak": 0,
    }
    base.update(overrides)
    return base


class TestEffectiveMaxRisk(unittest.TestCase):
    """Tests for the effective_max_risk formula."""

    def test_no_adaptive_weight(self):
        """Weight=0 → no reduction."""
        self.assertAlmostEqual(effective_max_risk(0.8, 0.0), 0.8, places=4)

    def test_half_weight(self):
        """Weight=0.5 → 25% reduction: 0.8 * 0.75 = 0.6."""
        self.assertAlmostEqual(effective_max_risk(0.8, 0.5), 0.6, places=4)

    def test_full_weight(self):
        """Weight=1.0 → 50% reduction: 0.8 * 0.5 = 0.4."""
        self.assertAlmostEqual(effective_max_risk(0.8, 1.0), 0.4, places=4)

    def test_weight_clamped_above_1(self):
        """Weight > 1 treated as 1.0."""
        self.assertAlmostEqual(effective_max_risk(0.8, 2.0), 0.4, places=4)

    def test_weight_clamped_below_0(self):
        """Weight < 0 treated as 0.0."""
        self.assertAlmostEqual(effective_max_risk(0.8, -1.0), 0.8, places=4)


class TestEvaluateScaling(unittest.TestCase):
    """Tests for the core evaluate_scaling decision engine."""

    # ── Scale UP ─────────────────────────────────────────────────────

    def test_scale_up_when_requirements_met(self):
        """Good KPIs + enough trades + adaptive >= 0.3 → SCALE_UP by up_step."""
        new_pct, decision, reason = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=_good_kpis(),
            adaptive_weight=0.3,
        )
        self.assertEqual(decision, "SCALE_UP")
        self.assertAlmostEqual(new_pct, 0.55, places=4)
        self.assertIn("scale up", reason)

    def test_scale_up_capped_at_effective_max(self):
        """Can't exceed effective max risk."""
        new_pct, decision, reason = evaluate_scaling(
            current_risk_pct=0.78,
            kpis_30d=_good_kpis(),
            adaptive_weight=0.0,
            config={"RISK_PCT_MAX": 0.8, "SCALE_UP_STEP": 0.05, "SCALE_MIN_ADAPTIVE_WEIGHT": 0.0},
        )
        self.assertEqual(decision, "SCALE_UP")
        self.assertAlmostEqual(new_pct, 0.8, places=4)

    def test_scale_up_blocked_at_max(self):
        """Already at max → HOLD."""
        # With adaptive=0.0, eff_max = 0.8. Already at 0.8 → can't go higher.
        new_pct, decision, _ = evaluate_scaling(
            current_risk_pct=0.8,
            kpis_30d=_good_kpis(),
            adaptive_weight=0.0,
            config={"RISK_PCT_MAX": 0.8, "SCALE_MIN_ADAPTIVE_WEIGHT": 0.0},
        )
        self.assertEqual(decision, "HOLD")
        self.assertAlmostEqual(new_pct, 0.8)

    # ── Scale DOWN ───────────────────────────────────────────────────

    def test_scale_down_on_bad_pf(self):
        """PF < 1.0 with enough trades → SCALE_DOWN."""
        new_pct, decision, reason = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=_bad_kpis(profit_factor=0.8, max_dd_pct=3.0),
        )
        self.assertEqual(decision, "SCALE_DOWN")
        self.assertAlmostEqual(new_pct, 0.4, places=4)
        self.assertIn("PF=", reason)

    def test_scale_down_on_dd_breach(self):
        """Drawdown > max → SCALE_DOWN."""
        new_pct, decision, reason = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=_good_kpis(max_dd_pct=6.0),  # > 5.0 default
        )
        self.assertEqual(decision, "SCALE_DOWN")
        self.assertAlmostEqual(new_pct, 0.4, places=4)
        self.assertIn("DD=", reason)

    def test_scale_down_clamped_at_min(self):
        """Can't go below RISK_PCT_MIN."""
        new_pct, decision, _ = evaluate_scaling(
            current_risk_pct=0.25,
            kpis_30d=_bad_kpis(),
            config={"RISK_PCT_MIN": 0.2, "SCALE_DOWN_STEP": 0.10},
        )
        self.assertEqual(decision, "SCALE_DOWN")
        self.assertAlmostEqual(new_pct, 0.2, places=4)

    def test_scale_down_pf_requires_minimum_trades(self):
        """PF < 1.0 but < 5 trades → not enough data for scale down."""
        new_pct, decision, _ = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=_bad_kpis(profit_factor=0.7, trades=3),
        )
        # DD breach still can trigger though — let's neutralize DD too
        new_pct2, decision2, _ = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d={"profit_factor": 0.7, "max_dd_pct": 3.0, "trades": 3},
        )
        # PF check requires trades >= 5, DD=3.0 < 5.0 → HOLD
        self.assertEqual(decision2, "HOLD")

    # ── Cap enforcement with adaptive weight ─────────────────────────

    def test_cap_applied_with_adaptive_weight(self):
        """Current risk above effective max → capped down."""
        # Effective max = 0.8 * (1 - 0.5*0.5) = 0.6
        new_pct, decision, reason = evaluate_scaling(
            current_risk_pct=0.7,
            kpis_30d=_neutral_kpis(),
            adaptive_weight=0.5,
            config={"RISK_PCT_MAX": 0.8},
        )
        self.assertEqual(decision, "SCALE_DOWN")
        self.assertAlmostEqual(new_pct, 0.6, places=4)
        self.assertIn("capped", reason)

    def test_adaptive_weight_reduces_scale_up_ceiling(self):
        """Scale up can't exceed effective max when weight > 0."""
        # eff_max = 0.8 * (1 - 0.5*0.3) = 0.8 * 0.85 = 0.68
        new_pct, decision, _ = evaluate_scaling(
            current_risk_pct=0.65,
            kpis_30d=_good_kpis(),
            adaptive_weight=0.3,
            config={"RISK_PCT_MAX": 0.8, "SCALE_UP_STEP": 0.05},
        )
        self.assertEqual(decision, "SCALE_UP")
        self.assertAlmostEqual(new_pct, 0.68, places=4)

    # ── Cooldown ─────────────────────────────────────────────────────

    def test_cooldown_prevents_scale_up(self):
        """Recent scaling change → cooldown blocks scale up."""
        recent_ts = datetime.now(timezone.utc).isoformat()
        state = {"last_change_ts": recent_ts, "last_decision": "SCALE_UP"}
        new_pct, decision, reason = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=_good_kpis(),
            adaptive_weight=0.3,
            state=state,
        )
        self.assertEqual(decision, "HOLD")
        self.assertEqual(reason, "Cooldown active")
        self.assertAlmostEqual(new_pct, 0.5)

    def test_cooldown_does_not_prevent_scale_down(self):
        """Scale DOWN bypasses cooldown (safety priority)."""
        recent_ts = datetime.now(timezone.utc).isoformat()
        state = {"last_change_ts": recent_ts, "last_decision": "SCALE_UP"}
        new_pct, decision, _ = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=_bad_kpis(),
            state=state,
        )
        # Scale down has priority over cooldown
        self.assertEqual(decision, "SCALE_DOWN")

    def test_expired_cooldown_allows_scale_up(self):
        """Cooldown expired → scale up allowed."""
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        state = {"last_change_ts": old_ts}
        new_pct, decision, _ = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=_good_kpis(),
            adaptive_weight=0.3,
            state=state,
        )
        self.assertEqual(decision, "SCALE_UP")

    # ── HOLD ─────────────────────────────────────────────────────────

    def test_hold_when_neutral(self):
        """Neutral KPIs (not enough trades for up, PF ok) → HOLD."""
        new_pct, decision, _ = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=_neutral_kpis(),
        )
        self.assertEqual(decision, "HOLD")
        self.assertAlmostEqual(new_pct, 0.5)

    # ── New guards: kill-switch, losing streak, adaptive gate ────────

    def test_scale_down_on_kill_switch(self):
        """Kill-switch active → immediate SCALE_DOWN."""
        new_pct, decision, reason = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=_good_kpis(),
            kill_switch_active=True,
        )
        self.assertEqual(decision, "SCALE_DOWN")
        self.assertIn("Kill-switch", reason)

    def test_scale_down_on_losing_streak(self):
        """Losing streak >= 6 → SCALE_DOWN."""
        new_pct, decision, reason = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=_good_kpis(losing_streak=6),
        )
        self.assertEqual(decision, "SCALE_DOWN")
        self.assertIn("Losing streak", reason)

    def test_scale_up_blocked_by_low_adaptive_weight(self):
        """Adaptive weight < 0.3 → scale-up blocked."""
        new_pct, decision, reason = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=_good_kpis(),
            adaptive_weight=0.1,
        )
        self.assertEqual(decision, "HOLD")
        self.assertIn("Adaptive weight", reason)

    def test_scale_up_blocked_by_taker_quality(self):
        """Taker PF < 1.1 → scale-up blocked."""
        eq = {"taker": {"trades": 10, "profit_factor": 0.9}}
        new_pct, decision, reason = evaluate_scaling(
            current_risk_pct=0.5,
            kpis_30d=_good_kpis(),
            adaptive_weight=0.3,
            exec_quality=eq,
        )
        self.assertEqual(decision, "HOLD")
        self.assertIn("Taker PF", reason)


class TestEffectiveRiskPct(unittest.TestCase):
    """Tests for the effective_risk_pct coupling formula."""

    def test_no_adaptive(self):
        self.assertAlmostEqual(effective_risk_pct(0.6, 0.0), 0.6, places=4)

    def test_30pct_adaptive(self):
        # 0.6 * (1 - 0.5*0.3) = 0.6 * 0.85 = 0.51
        self.assertAlmostEqual(effective_risk_pct(0.6, 0.3), 0.51, places=4)

    def test_50pct_adaptive(self):
        # 0.6 * (1 - 0.5*0.5) = 0.6 * 0.75 = 0.45
        self.assertAlmostEqual(effective_risk_pct(0.6, 0.5), 0.45, places=4)

    def test_full_adaptive(self):
        # 0.6 * (1 - 0.5*1.0) = 0.6 * 0.5 = 0.3
        self.assertAlmostEqual(effective_risk_pct(0.6, 1.0), 0.3, places=4)

class TestScalingPersistence(unittest.TestCase):
    """Tests for load/save/record functions."""

    def setUp(self):
        self.state_dir = project_root / "reports" / "scaling_test"
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)

    def tearDown(self):
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)

    def test_load_default_state(self):
        """No file → default state returned."""
        state = load_scaling_state(self.state_dir)
        self.assertEqual(state["current_risk_pct"], DEFAULTS["RISK_PCT_BASE"])
        self.assertIsNone(state["last_change_ts"])
        self.assertEqual(state["last_decision"], "HOLD")

    def test_save_and_load(self):
        """Round-trip save/load."""
        data = {
            "current_risk_pct": 0.65,
            "last_change_ts": "2026-03-01T12:00:00+00:00",
            "last_decision": "SCALE_UP",
        }
        save_scaling_state(data, self.state_dir)
        loaded = load_scaling_state(self.state_dir)
        self.assertAlmostEqual(loaded["current_risk_pct"], 0.65)
        self.assertEqual(loaded["last_decision"], "SCALE_UP")

    def test_record_scaling_decision(self):
        """Decision JSON written to reports dir."""
        record_scaling_decision(
            decision="SCALE_UP",
            previous=0.5,
            new=0.55,
            reason="PF=1.5 → scale up",
            kpis_30d=_good_kpis(),
            adaptive_weight=0.1,
            eff_max=0.76,
            base_dir=self.state_dir,
        )
        files = list(self.state_dir.glob("scaling_decision_*.json"))
        self.assertEqual(len(files), 1, "Expected one decision file")
        with open(files[0], "r", encoding="utf-8") as f:
            entry = json.load(f)
        self.assertEqual(entry["decision"], "SCALE_UP")
        self.assertAlmostEqual(entry["previous_risk_pct"], 0.5)
        self.assertAlmostEqual(entry["new_risk_pct"], 0.55)

    def test_load_corrupted_state_returns_default(self):
        """Corrupted state file → fallback to default."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        bad_path = self.state_dir / "state.json"
        bad_path.write_text("NOT JSON", encoding="utf-8")
        state = load_scaling_state(self.state_dir)
        self.assertEqual(state["current_risk_pct"], DEFAULTS["RISK_PCT_BASE"])


if __name__ == "__main__":
    unittest.main()
