"""
Drawdown Recovery Mode — Phase 9

After significant drawdown, enters a recovery mode that:
  - Reduces risk_pct to a fraction of normal
  - Freezes adaptive weight (no promotions)
  - Only exits recovery after sustained stability

Recovery entry:
  - DD > RECOVERY_ENTRY_DD_PCT (e.g., 5%)

Recovery exit (ALL conditions must hold):
  - DD recovered to < RECOVERY_EXIT_DD_PCT (e.g., 2%)
  - Consecutive winning days >= RECOVERY_MIN_WIN_DAYS (e.g., 3)
  - PF over recovery window >= RECOVERY_MIN_PF (e.g., 1.1)

Design:
  - Pure functions, deterministic, fully testable
  - State is managed externally (dict passed in/out)
  - Called from DailyReporter.evaluate_regime_step()

Author: SMC Trading Bot Framework — Phase 9
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────
RECOVERY_ENTRY_DD_PCT = 5.0       # DD > 5% of equity → enter recovery
RECOVERY_EXIT_DD_PCT = 2.0        # DD must recover to < 2% to exit
RECOVERY_MIN_WIN_DAYS = 3         # need ≥3 consecutive profitable days
RECOVERY_MIN_PF = 1.1             # PF must be >= 1.1 during recovery
RECOVERY_RISK_FACTOR = 0.50       # reduce risk_pct by 50% in recovery
RECOVERY_PROGRESSIVE_STEPS = [    # progressive return to normal
    0.50,   # step 1: 50% of normal risk
    0.70,   # step 2: 70% of normal risk
    0.85,   # step 3: 85% of normal risk
    1.00,   # step 4: back to normal
]
RECOVERY_STEP_MIN_DAYS = 2        # minimum days at each step before next


def evaluate_recovery_mode(
    current_dd_pct: float,
    consecutive_win_days: int = 0,
    recovery_pf: float = 0.0,
    recovery_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Evaluate drawdown recovery mode.

    Args:
        current_dd_pct:       Current drawdown from peak (positive = in drawdown)
        consecutive_win_days: Consecutive days with positive PnL
        recovery_pf:          Profit factor over the recovery period
        recovery_state:       Persistent state dict with keys:
                                - active: bool
                                - step: int (index into RECOVERY_PROGRESSIVE_STEPS)
                                - days_at_step: int
                                - entry_dd_pct: float (DD when recovery started)

    Returns:
        Dict with keys:
            recovery_active: bool
            risk_factor: float (1.0 = normal, <1.0 = reduced)
            freeze_adaptive: bool
            block_scale_up: bool
            step: int (current progressive step)
            total_steps: int
            reason: str
    """
    state = recovery_state or {"active": False, "step": 0, "days_at_step": 0, "entry_dd_pct": 0.0}
    is_active = state.get("active", False)

    # ── Not in recovery: check entry condition ──
    if not is_active:
        if current_dd_pct >= RECOVERY_ENTRY_DD_PCT:
            logger.warning(
                f"[RECOVERY] ENTERING recovery mode: DD={current_dd_pct:.2f}% "
                f">= {RECOVERY_ENTRY_DD_PCT}%"
            )
            return {
                "recovery_active": True,
                "risk_factor": RECOVERY_PROGRESSIVE_STEPS[0],
                "freeze_adaptive": True,
                "block_scale_up": True,
                "step": 0,
                "total_steps": len(RECOVERY_PROGRESSIVE_STEPS),
                "reason": f"DD={current_dd_pct:.2f}% >= {RECOVERY_ENTRY_DD_PCT}% → recovery mode",
                "state": {
                    "active": True,
                    "step": 0,
                    "days_at_step": 0,
                    "entry_dd_pct": current_dd_pct,
                },
            }
        return {
            "recovery_active": False,
            "risk_factor": 1.0,
            "freeze_adaptive": False,
            "block_scale_up": False,
            "step": 0,
            "total_steps": len(RECOVERY_PROGRESSIVE_STEPS),
            "reason": f"DD={current_dd_pct:.2f}% < {RECOVERY_ENTRY_DD_PCT}% — no recovery needed",
            "state": {"active": False, "step": 0, "days_at_step": 0, "entry_dd_pct": 0.0},
        }

    # ── In recovery: check exit condition ──
    current_step = state.get("step", 0)
    days_at_step = state.get("days_at_step", 0)

    # Can we advance to next step?
    can_advance = (
        current_dd_pct < RECOVERY_EXIT_DD_PCT
        and consecutive_win_days >= RECOVERY_MIN_WIN_DAYS
        and recovery_pf >= RECOVERY_MIN_PF
        and days_at_step >= RECOVERY_STEP_MIN_DAYS
    )

    if can_advance:
        next_step = current_step + 1
        if next_step >= len(RECOVERY_PROGRESSIVE_STEPS):
            # Fully recovered
            logger.info(
                f"[RECOVERY] EXIT: DD={current_dd_pct:.2f}%, "
                f"win_days={consecutive_win_days}, PF={recovery_pf:.2f} "
                f"— back to normal"
            )
            return {
                "recovery_active": False,
                "risk_factor": 1.0,
                "freeze_adaptive": False,
                "block_scale_up": False,
                "step": len(RECOVERY_PROGRESSIVE_STEPS) - 1,
                "total_steps": len(RECOVERY_PROGRESSIVE_STEPS),
                "reason": "Recovery complete — all conditions met, progressive return finished",
                "state": {"active": False, "step": 0, "days_at_step": 0, "entry_dd_pct": 0.0},
            }
        else:
            # Advance to next step
            risk_factor = RECOVERY_PROGRESSIVE_STEPS[next_step]
            logger.info(
                f"[RECOVERY] STEP {current_step} → {next_step}: "
                f"risk_factor={risk_factor:.2f}"
            )
            # At later steps, allow adaptive but still block scale-up
            freeze = next_step < 2  # freeze adaptive for steps 0 and 1
            return {
                "recovery_active": True,
                "risk_factor": risk_factor,
                "freeze_adaptive": freeze,
                "block_scale_up": next_step < 3,  # block scale-up until final step
                "step": next_step,
                "total_steps": len(RECOVERY_PROGRESSIVE_STEPS),
                "reason": (
                    f"Recovery step {next_step}/{len(RECOVERY_PROGRESSIVE_STEPS)-1}: "
                    f"risk_factor={risk_factor:.2f}, "
                    f"DD={current_dd_pct:.2f}%, PF={recovery_pf:.2f}"
                ),
                "state": {
                    "active": True,
                    "step": next_step,
                    "days_at_step": 0,
                    "entry_dd_pct": state.get("entry_dd_pct", current_dd_pct),
                },
            }

    # ── Still in recovery, no advancement ──
    risk_factor = RECOVERY_PROGRESSIVE_STEPS[min(current_step, len(RECOVERY_PROGRESSIVE_STEPS) - 1)]
    reasons = []
    if current_dd_pct >= RECOVERY_EXIT_DD_PCT:
        reasons.append(f"DD={current_dd_pct:.2f}% >= {RECOVERY_EXIT_DD_PCT}%")
    if consecutive_win_days < RECOVERY_MIN_WIN_DAYS:
        reasons.append(f"win_days={consecutive_win_days} < {RECOVERY_MIN_WIN_DAYS}")
    if recovery_pf < RECOVERY_MIN_PF:
        reasons.append(f"PF={recovery_pf:.2f} < {RECOVERY_MIN_PF}")
    if days_at_step < RECOVERY_STEP_MIN_DAYS:
        reasons.append(f"days_at_step={days_at_step} < {RECOVERY_STEP_MIN_DAYS}")

    return {
        "recovery_active": True,
        "risk_factor": risk_factor,
        "freeze_adaptive": True,
        "block_scale_up": True,
        "step": current_step,
        "total_steps": len(RECOVERY_PROGRESSIVE_STEPS),
        "reason": f"Recovery active step {current_step}: " + ", ".join(reasons),
        "state": {
            "active": True,
            "step": current_step,
            "days_at_step": days_at_step + 1,
            "entry_dd_pct": state.get("entry_dd_pct", current_dd_pct),
        },
    }
