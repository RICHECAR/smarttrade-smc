"""
Unit tests for Adaptive Weight Engine — Phase 8

Covers:
    - Promotion (FSM step-up)
    - Hold (insufficient KPIs)
    - Rollback (immediate on single failure)
    - Cooldown guard (no promotion during cooldown)
    - next_weight determinism & boundary cases
    - Confidence computation
    - Persistence helpers (load/save state, record decision)
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

from scripts.reporting.adaptive_weight_engine import (
    evaluate_adaptive_weight,
    next_weight,
    compute_confidence,
    compute_cooldown_ok,
    load_state,
    save_state,
    record_decision,
    WEIGHT_STEPS,
    _check_rollback,
    _check_promotion,
)


# ── Helper: default "good" KPIs that pass all promotion gates ────────
def _good_kpis(**overrides) -> dict:
    base = {
        "expectancy_B": 0.25,
        "expectancy_A": 0.10,
        "profit_factor_B": 1.4,
        "winrate_B": 0.52,
        "winrate_A": 0.50,
        "drawdown_B": 0.8,
        "drawdown_A": 1.0,
        "losing_streak_B": 2,
        "decisions": 35,
    }
    base.update(overrides)
    return base


def _bad_kpis(**overrides) -> dict:
    base = {
        "expectancy_B": -0.2,
        "expectancy_A": 0.05,
        "profit_factor_B": 0.8,
        "winrate_B": 0.35,
        "winrate_A": 0.52,
        "drawdown_B": 1.8,
        "drawdown_A": 1.0,
        "losing_streak_B": 6,
        "decisions": 25,
    }
    base.update(overrides)
    return base


class TestAdaptiveWeightEngine(unittest.TestCase):
    """Core FSM tests."""

    # ── Test 1: PROMOTION (0.0 → 0.1) ───────────────────────────────
    def test_promote_from_shadow_to_10pct(self):
        kpis_7d = _good_kpis()
        kpis_30d = _good_kpis()
        state = {
            "last_change_ts": "2026-02-20T00:00:00",
            "last_decision": "HOLD",
            "cooldown_ok": True,
        }
        new_weight, decision = evaluate_adaptive_weight(0.0, kpis_7d, kpis_30d, state)
        self.assertEqual(new_weight, 0.1)
        self.assertEqual(decision, "PROMOTE")

    # ── Test 1b: PROMOTION chain (0.1 → 0.3 → 0.5) ─────────────────
    def test_promote_chain(self):
        kpis = _good_kpis()
        state = {"last_change_ts": "2026-01-01T00:00:00", "last_decision": "HOLD", "cooldown_ok": True}

        w, d = evaluate_adaptive_weight(0.1, kpis, kpis, state)
        self.assertEqual(w, 0.3)
        self.assertEqual(d, "PROMOTE")

        w, d = evaluate_adaptive_weight(0.3, kpis, kpis, state)
        self.assertEqual(w, 0.5)
        self.assertEqual(d, "PROMOTE")

    # ── Test 1c: At MAX weight, promotion becomes HOLD ───────────────
    def test_promote_at_max_stays_hold(self):
        kpis = _good_kpis()
        state = {"last_change_ts": "2026-01-01T00:00:00", "last_decision": "HOLD", "cooldown_ok": True}
        w, d = evaluate_adaptive_weight(0.5, kpis, kpis, state)
        self.assertEqual(w, 0.5)
        self.assertEqual(d, "HOLD")

    # ── Test 2: HOLD when profit factor too low ──────────────────────
    def test_hold_when_profit_factor_too_low(self):
        kpis_7d = _good_kpis(profit_factor_B=1.05, expectancy_B=0.05, expectancy_A=0.10)
        kpis_30d = kpis_7d.copy()
        state = {
            "last_change_ts": "2026-02-20T00:00:00",
            "last_decision": "PROMOTE",
            "cooldown_ok": True,
        }
        new_weight, decision = evaluate_adaptive_weight(0.1, kpis_7d, kpis_30d, state)
        self.assertEqual(new_weight, 0.1)
        self.assertEqual(decision, "HOLD")

    # ── Test 2b: HOLD when not enough decisions ──────────────────────
    def test_hold_insufficient_decisions(self):
        kpis = _good_kpis(decisions=15)
        state = {"last_change_ts": "2026-01-01T00:00:00", "last_decision": "HOLD", "cooldown_ok": True}
        w, d = evaluate_adaptive_weight(0.1, kpis, kpis, state)
        self.assertEqual(w, 0.1)
        self.assertEqual(d, "HOLD")

    # ── Test 2c: HOLD when only 7d window passes but 30d fails ──────
    def test_hold_when_only_7d_passes(self):
        good = _good_kpis()
        bad_30d = _good_kpis(profit_factor_B=1.0)
        state = {"last_change_ts": "2026-01-01T00:00:00", "last_decision": "HOLD", "cooldown_ok": True}
        w, d = evaluate_adaptive_weight(0.0, good, bad_30d, state)
        self.assertEqual(w, 0.0)
        self.assertEqual(d, "HOLD")

    # ── Test 3: ROLLBACK on low winrate ──────────────────────────────
    def test_rollback_on_low_winrate(self):
        kpis_7d = _bad_kpis()
        kpis_30d = _bad_kpis()
        state = {
            "last_change_ts": "2026-03-01T12:00:00",
            "last_decision": "PROMOTE",
            "cooldown_ok": True,
        }
        new_weight, decision = evaluate_adaptive_weight(0.3, kpis_7d, kpis_30d, state)
        self.assertEqual(new_weight, 0.0)
        self.assertEqual(decision, "ROLLBACK")

    # ── Test 3b: ROLLBACK on PF < 1.0 alone ─────────────────────────
    def test_rollback_on_pf_alone(self):
        kpis = _good_kpis(profit_factor_B=0.9, winrate_B=0.55)
        state = {"last_change_ts": "2026-01-01T00:00:00", "last_decision": "HOLD", "cooldown_ok": True}
        w, d = evaluate_adaptive_weight(0.1, kpis, kpis, state)
        self.assertEqual(w, 0.0)
        self.assertEqual(d, "ROLLBACK")

    # ── Test 3c: ROLLBACK on excessive drawdown ──────────────────────
    def test_rollback_on_excessive_drawdown(self):
        kpis = _good_kpis(drawdown_B=1.6, drawdown_A=1.0)
        state = {"last_change_ts": "2026-01-01T00:00:00", "last_decision": "HOLD", "cooldown_ok": True}
        w, d = evaluate_adaptive_weight(0.3, kpis, kpis, state)
        self.assertEqual(w, 0.0)
        self.assertEqual(d, "ROLLBACK")

    # ── Test 3d: ROLLBACK on losing streak ≥ 6 ──────────────────────
    def test_rollback_on_losing_streak(self):
        kpis = _good_kpis(losing_streak_B=6)
        state = {"last_change_ts": "2026-01-01T00:00:00", "last_decision": "HOLD", "cooldown_ok": True}
        w, d = evaluate_adaptive_weight(0.1, kpis, kpis, state)
        self.assertEqual(w, 0.0)
        self.assertEqual(d, "ROLLBACK")

    # ── Test 3e: ROLLBACK overrides cooldown ─────────────────────────
    def test_rollback_ignores_cooldown(self):
        kpis = _bad_kpis()
        state = {
            "last_change_ts": "2026-03-02T23:00:00",
            "last_decision": "PROMOTE",
            "cooldown_ok": False,  # cooldown active BUT rollback should still fire
        }
        w, d = evaluate_adaptive_weight(0.3, kpis, kpis, state)
        self.assertEqual(w, 0.0)
        self.assertEqual(d, "ROLLBACK")

    # ── Test 4: COOLDOWN respected ───────────────────────────────────
    def test_no_promotion_during_cooldown(self):
        kpis = _good_kpis()
        state = {
            "last_change_ts": "2026-03-02T06:00:00",
            "last_decision": "PROMOTE",
            "cooldown_ok": False,
        }
        new_weight, decision = evaluate_adaptive_weight(0.1, kpis, kpis, state)
        self.assertEqual(new_weight, 0.1)
        self.assertEqual(decision, "COOLDOWN")


class TestNextWeight(unittest.TestCase):
    """Verify FSM step logic."""

    def test_all_steps(self):
        self.assertEqual(next_weight(0.0), 0.1)
        self.assertEqual(next_weight(0.1), 0.3)
        self.assertEqual(next_weight(0.3), 0.5)
        self.assertEqual(next_weight(0.5), 0.5)  # capped

    def test_unknown_weight_snaps_down(self):
        # Unexpected value → snap to nearest lower step
        self.assertEqual(next_weight(0.2), 0.3)  # idx=1 (0.1 is last ≤0.2), next=0.3
        self.assertEqual(next_weight(0.05), 0.1) # idx=0 (0.0 is last ≤0.05), next=0.1


class TestConfidence(unittest.TestCase):
    def test_low_confidence(self):
        self.assertEqual(compute_confidence(10, {}), "LOW")

    def test_medium_confidence(self):
        kpis = _good_kpis(decisions=35)
        self.assertEqual(compute_confidence(35, kpis), "MEDIUM")

    def test_high_confidence(self):
        kpis = _good_kpis(decisions=55)
        self.assertEqual(compute_confidence(55, kpis), "HIGH")

    def test_high_decisions_but_bad_kpis_is_medium(self):
        kpis = _good_kpis(profit_factor_B=1.0, expectancy_B=-0.1, expectancy_A=0.1)
        self.assertEqual(compute_confidence(60, kpis), "MEDIUM")


class TestCooldownComputation(unittest.TestCase):
    def test_cooldown_after_promotion_48h(self):
        now = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)
        # Last promotion was 47h ago → still cooling down
        state = {
            "last_change_ts": (now - timedelta(hours=47)).isoformat(),
            "last_decision": "PROMOTE",
        }
        self.assertFalse(compute_cooldown_ok(state, now))

        # Last promotion was 49h ago → cooldown elapsed
        state["last_change_ts"] = (now - timedelta(hours=49)).isoformat()
        self.assertTrue(compute_cooldown_ok(state, now))

    def test_cooldown_after_rollback_24h(self):
        now = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)
        state = {
            "last_change_ts": (now - timedelta(hours=23)).isoformat(),
            "last_decision": "ROLLBACK",
        }
        self.assertFalse(compute_cooldown_ok(state, now))

        state["last_change_ts"] = (now - timedelta(hours=25)).isoformat()
        self.assertTrue(compute_cooldown_ok(state, now))

    def test_cooldown_hold_is_always_ok(self):
        now = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)
        state = {
            "last_change_ts": now.isoformat(),
            "last_decision": "HOLD",
        }
        self.assertTrue(compute_cooldown_ok(state, now))


class TestPersistence(unittest.TestCase):
    """Test load/save state and record_decision I/O."""

    def setUp(self):
        self.tmp_dir = project_root / "reports" / "_test_adaptive_tmp"
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir)
        self.tmp_dir.mkdir(parents=True)

    def tearDown(self):
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir)

    def test_save_and_load_state(self):
        state = {
            "current_weight": 0.1,
            "last_change_ts": "2026-03-01T00:00:00+00:00",
            "last_decision": "PROMOTE",
            "cooldown_ok": True,
        }
        save_state(state, self.tmp_dir)
        loaded = load_state(self.tmp_dir)
        self.assertEqual(loaded["current_weight"], 0.1)
        self.assertEqual(loaded["last_decision"], "PROMOTE")

    def test_load_state_missing_file(self):
        loaded = load_state(self.tmp_dir / "nonexistent")
        self.assertEqual(loaded["current_weight"], 0.0)
        self.assertEqual(loaded["last_decision"], "HOLD")

    def test_record_decision_creates_file(self):
        record_decision(
            decision="PROMOTE",
            previous_weight=0.0,
            new_weight=0.1,
            kpis_7d=_good_kpis(),
            kpis_30d=_good_kpis(),
            confidence="MEDIUM",
            reason="All gates passed",
            base_dir=self.tmp_dir,
        )
        files = list(self.tmp_dir.glob("weight_decisions_*.json"))
        self.assertEqual(len(files), 1)

        with open(files[0], "r") as f:
            data = json.load(f)
        self.assertEqual(data["decision"], "PROMOTE")
        self.assertEqual(data["new_weight"], 0.1)
        self.assertEqual(data["confidence"], "MEDIUM")


class TestInternalHelpers(unittest.TestCase):
    """Granular tests on _check_rollback and _check_promotion."""

    def test_check_rollback_none_when_ok(self):
        kpis = _good_kpis()
        self.assertIsNone(_check_rollback(kpis))

    def test_check_rollback_triggers_on_winrate(self):
        kpis = _good_kpis(winrate_B=0.38)
        reason = _check_rollback(kpis)
        self.assertIsNotNone(reason)
        self.assertIn("WR(B)", reason)

    def test_check_promotion_true(self):
        self.assertTrue(_check_promotion(_good_kpis()))

    def test_check_promotion_false_low_pf(self):
        self.assertFalse(_check_promotion(_good_kpis(profit_factor_B=1.1)))

    def test_check_promotion_false_winrate_delta(self):
        # WR(B) < WR(A) - 3%
        self.assertFalse(_check_promotion(_good_kpis(winrate_B=0.46, winrate_A=0.50)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
