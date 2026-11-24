"""
Allocation Stability Guard — Smooth & Safe Capital Reallocation
══════════════════════════════════════════════════════════════

Prevents capital allocation from oscillating. Wraps EdgeAllocator
output with:

  1. Step-size limiter   — max ±step_size per evaluation cycle
  2. Hysteresis band     — no change unless delta > threshold
  3. Global cap          — sum of all allocations ≤ global_max
  4. Cooldown timer      — after sharp drop, freeze for N cycles

Config via .env:
  STAB_MAX_STEP=0.15          (max allocation change per cycle)
  STAB_HYSTERESIS=0.05        (ignore changes smaller than this)
  STAB_GLOBAL_CAP=6.0         (sum of all allocation_pct)
  STAB_COOLDOWN_CYCLES=3      (freeze cycles after sharp down-step)
  STAB_SHARP_DROP_THRESHOLD=0.30  (delta > this triggers cooldown)
"""

import logging
import os
import time
from typing import Any, Dict, Optional

from backend.services.trading.robustness_models import StabilizedAllocation

logger = logging.getLogger("smarttrade.robustness.alloc_stability")


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class AllocationStabilityGuard:
    """
    Smooths allocation changes and enforces stability constraints.

    Usage:
        guard = AllocationStabilityGuard()
        result = guard.stabilize("setup_key", raw_allocation=1.4)
        # result.stabilized_allocation — the safe, smoothed value
    """

    def __init__(self) -> None:
        self._max_step = _envf("STAB_MAX_STEP", 0.15)
        self._hysteresis = _envf("STAB_HYSTERESIS", 0.05)
        self._global_cap = _envf("STAB_GLOBAL_CAP", 6.0)
        self._cooldown_cycles = _envi("STAB_COOLDOWN_CYCLES", 3)
        self._sharp_drop = _envf("STAB_SHARP_DROP_THRESHOLD", 0.30)

        # State per setup
        self._prev: Dict[str, float] = {}    # previous stabilized allocation
        self._cooldowns: Dict[str, int] = {}  # remaining cooldown cycles

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def stabilize(self, key: str, raw_allocation: float) -> StabilizedAllocation:
        """
        Apply step, hysteresis, and cooldown constraints to a raw allocation.
        """
        prev = self._prev.get(key, 1.0)
        cooldown_left = self._cooldowns.get(key, 0)
        delta = raw_allocation - prev

        # ── Cooldown check ──
        if cooldown_left > 0:
            self._cooldowns[key] = cooldown_left - 1
            return StabilizedAllocation(
                setup_key=key,
                raw_allocation=raw_allocation,
                stabilized_allocation=prev,
                previous_allocation=prev,
                step_applied=0.0,
                in_cooldown=True,
                reason=f"cooldown {cooldown_left} cycles remaining",
            )

        # ── Hysteresis ──
        if abs(delta) < self._hysteresis:
            return StabilizedAllocation(
                setup_key=key,
                raw_allocation=raw_allocation,
                stabilized_allocation=prev,
                previous_allocation=prev,
                step_applied=0.0,
                reason="within hysteresis band",
            )

        # ── Step limiter ──
        capped = False
        if abs(delta) > self._max_step:
            clamped_delta = self._max_step if delta > 0 else -self._max_step
            capped = True
        else:
            clamped_delta = delta

        new_alloc = prev + clamped_delta
        new_alloc = max(0.0, min(2.0, new_alloc))  # absolute floor/ceiling

        # ── Sharp drop → trigger cooldown ──
        if clamped_delta < -self._sharp_drop:
            self._cooldowns[key] = self._cooldown_cycles
            logger.info(
                "[STAB] %s sharp drop: %.3f → %.3f, cooldown=%d",
                key, prev, new_alloc, self._cooldown_cycles,
            )

        self._prev[key] = new_alloc

        return StabilizedAllocation(
            setup_key=key,
            raw_allocation=raw_allocation,
            stabilized_allocation=round(new_alloc, 4),
            previous_allocation=prev,
            step_applied=round(clamped_delta, 4),
            capped=capped,
            reason="step-limited" if capped else "applied",
        )

    def stabilize_batch(
        self, raw_allocations: Dict[str, float]
    ) -> Dict[str, StabilizedAllocation]:
        """
        Stabilize a batch, then enforce global cap.
        """
        results: Dict[str, StabilizedAllocation] = {}
        for key, raw in raw_allocations.items():
            results[key] = self.stabilize(key, raw)

        # ── Global cap enforcement ──
        total = sum(r.stabilized_allocation for r in results.values())
        if total > self._global_cap and total > 0:
            scale = self._global_cap / total
            for key, r in results.items():
                scaled = round(r.stabilized_allocation * scale, 4)
                if scaled != r.stabilized_allocation:
                    results[key] = StabilizedAllocation(
                        setup_key=key,
                        raw_allocation=r.raw_allocation,
                        stabilized_allocation=scaled,
                        previous_allocation=r.previous_allocation,
                        step_applied=round(scaled - r.previous_allocation, 4),
                        capped=True,
                        in_cooldown=r.in_cooldown,
                        reason=f"global cap ({total:.2f} → {self._global_cap})",
                    )
                    self._prev[key] = scaled
            logger.info(
                "[STAB] Global cap applied: %.2f → %.2f",
                total, self._global_cap,
            )

        return results

    def get_state(self) -> Dict[str, Any]:
        return {
            "allocations": dict(self._prev),
            "cooldowns": dict(self._cooldowns),
        }
