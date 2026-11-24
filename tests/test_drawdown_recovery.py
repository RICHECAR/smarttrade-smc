"""
Tests for Phase 9 — Drawdown Recovery Mode

Covers:
  - Recovery entry when DD > threshold
  - No recovery when DD below threshold
  - Progressive step advancement
  - Full exit after all steps completed
  - State persistence
  - Freeze and block flags at each step
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.reporting.drawdown_recovery import (
    evaluate_recovery_mode,
    RECOVERY_ENTRY_DD_PCT,
    RECOVERY_EXIT_DD_PCT,
    RECOVERY_MIN_WIN_DAYS,
    RECOVERY_MIN_PF,
    RECOVERY_PROGRESSIVE_STEPS,
    RECOVERY_STEP_MIN_DAYS,
)


class TestRecoveryEntry:

    def test_no_recovery_below_threshold(self):
        result = evaluate_recovery_mode(current_dd_pct=3.0)
        assert result["recovery_active"] is False
        assert result["risk_factor"] == 1.0
        assert result["freeze_adaptive"] is False

    def test_enter_recovery_above_threshold(self):
        result = evaluate_recovery_mode(current_dd_pct=6.0)
        assert result["recovery_active"] is True
        assert result["risk_factor"] == RECOVERY_PROGRESSIVE_STEPS[0]
        assert result["freeze_adaptive"] is True
        assert result["block_scale_up"] is True
        assert result["step"] == 0

    def test_exact_threshold_triggers(self):
        result = evaluate_recovery_mode(current_dd_pct=RECOVERY_ENTRY_DD_PCT)
        assert result["recovery_active"] is True

    def test_entry_state_initialized(self):
        result = evaluate_recovery_mode(current_dd_pct=7.0)
        state = result["state"]
        assert state["active"] is True
        assert state["step"] == 0
        assert state["entry_dd_pct"] == 7.0


class TestRecoveryProgression:

    def _active_state(self, step=0, days=0, entry_dd=6.0):
        return {
            "active": True,
            "step": step,
            "days_at_step": days,
            "entry_dd_pct": entry_dd,
        }

    def test_stay_at_step_when_conditions_not_met(self):
        """DD still high → no advancement."""
        result = evaluate_recovery_mode(
            current_dd_pct=4.0,  # above exit threshold
            consecutive_win_days=1,
            recovery_pf=0.9,
            recovery_state=self._active_state(step=0, days=3),
        )
        assert result["recovery_active"] is True
        assert result["step"] == 0

    def test_advance_step_when_all_conditions_met(self):
        """All conditions met → advance to next step."""
        result = evaluate_recovery_mode(
            current_dd_pct=1.0,
            consecutive_win_days=5,
            recovery_pf=1.5,
            recovery_state=self._active_state(step=0, days=3),
        )
        assert result["recovery_active"] is True
        assert result["step"] == 1
        assert result["risk_factor"] == RECOVERY_PROGRESSIVE_STEPS[1]

    def test_advance_requires_min_days(self):
        """Must stay at step for minimum days before advancing."""
        result = evaluate_recovery_mode(
            current_dd_pct=1.0,
            consecutive_win_days=5,
            recovery_pf=1.5,
            recovery_state=self._active_state(step=0, days=1),  # only 1 day
        )
        assert result["step"] == 0  # no change

    def test_advance_requires_win_days(self):
        result = evaluate_recovery_mode(
            current_dd_pct=1.0,
            consecutive_win_days=1,  # too few
            recovery_pf=1.5,
            recovery_state=self._active_state(step=0, days=3),
        )
        assert result["step"] == 0

    def test_advance_requires_recovery_pf(self):
        result = evaluate_recovery_mode(
            current_dd_pct=1.0,
            consecutive_win_days=5,
            recovery_pf=0.8,  # too low
            recovery_state=self._active_state(step=0, days=3),
        )
        assert result["step"] == 0


class TestRecoveryExit:

    def _active_state(self, step=0, days=0, entry_dd=6.0):
        return {
            "active": True,
            "step": step,
            "days_at_step": days,
            "entry_dd_pct": entry_dd,
        }

    def test_full_recovery_after_all_steps(self):
        """Advance from last step → recovery complete."""
        last_step = len(RECOVERY_PROGRESSIVE_STEPS) - 1
        result = evaluate_recovery_mode(
            current_dd_pct=0.5,
            consecutive_win_days=5,
            recovery_pf=1.5,
            recovery_state=self._active_state(step=last_step, days=3),
        )
        assert result["recovery_active"] is False
        assert result["risk_factor"] == 1.0
        assert result["freeze_adaptive"] is False
        assert result["block_scale_up"] is False

    def test_exit_state_reset(self):
        last_step = len(RECOVERY_PROGRESSIVE_STEPS) - 1
        result = evaluate_recovery_mode(
            current_dd_pct=0.5,
            consecutive_win_days=5,
            recovery_pf=1.5,
            recovery_state=self._active_state(step=last_step, days=3),
        )
        state = result["state"]
        assert state["active"] is False


class TestRecoveryFlags:

    def _active_state(self, step=0, days=0):
        return {"active": True, "step": step, "days_at_step": days, "entry_dd_pct": 6.0}

    def test_step0_freezes_adaptive(self):
        result = evaluate_recovery_mode(
            current_dd_pct=3.0,
            recovery_state=self._active_state(step=0, days=0),
        )
        assert result["freeze_adaptive"] is True

    def test_step2_unfreezes_adaptive(self):
        """At step 2, adaptive is unfrozen but scale-up still blocked."""
        result = evaluate_recovery_mode(
            current_dd_pct=1.0,
            consecutive_win_days=5,
            recovery_pf=1.5,
            recovery_state=self._active_state(step=1, days=3),
        )
        # This advances to step 2
        assert result["step"] == 2
        assert result["freeze_adaptive"] is False
        assert result["block_scale_up"] is True

    def test_step3_unblocks_scale_up(self):
        """At step 3 (final), scale-up is unblocked."""
        result = evaluate_recovery_mode(
            current_dd_pct=1.0,
            consecutive_win_days=5,
            recovery_pf=1.5,
            recovery_state=self._active_state(step=2, days=3),
        )
        assert result["step"] == 3
        assert result["block_scale_up"] is False

    def test_days_increment_when_stuck(self):
        result = evaluate_recovery_mode(
            current_dd_pct=3.0,  # still too high
            recovery_state=self._active_state(step=0, days=5),
        )
        state = result["state"]
        assert state["days_at_step"] == 6  # 5 + 1
