"""
False Recovery Guard
════════════════════

Prevents premature return to normal operations after a structural shock.

Recovery from a shock requires evidence along 4 independent axes:
  1. Execution normalized  — fill quality and latency returned to baseline
  2. Opportunity recovered — edge quality / win-rate back to acceptable
  3. Regime stabilized     — no more active transitions, clarity improved
  4. Doctrine resolved     — doctrine validity score above threshold

Only when ALL 4 conditions are met → VALID (full restore).
If 2–3 conditions met → PARTIAL (partial restore: 60% capacity).
Otherwise → INVALID (hold current throttle).

Safety:  minimum hold period of N cycles after a shock before any
         restoration is allowed, even if all conditions look green
         (prevents data-lag false positives).

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.structural_break_models import (
    RecoveryCheckResult,
    RecoveryValidity,
)


class FalseRecoveryGuard:
    """Prevent premature recovery after structural shocks."""

    MIN_HOLD_CYCLES = 4         # minimum cycles after shock before any restore
    EXEC_THRESHOLD = 0.70       # fill quality must exceed this
    OPPORTUNITY_THRESHOLD = 0.50  # edge quality must exceed this
    REGIME_THRESHOLD = 0.60     # regime clarity must exceed this
    DOCTRINE_THRESHOLD = 0.60   # doctrine validity must exceed this

    def __init__(self) -> None:
        self._shock_active = False
        self._cycles_since_shock = 0
        self._last_result: RecoveryCheckResult | None = None

    # ────────────────────────────────────────────────────
    #  Shock lifecycle
    # ────────────────────────────────────────────────────

    def register_shock(self) -> None:
        """Call when a structural break of SEVERE+ is detected."""
        self._shock_active = True
        self._cycles_since_shock = 0

    def tick(self) -> None:
        """Advance one cycle. Call each evaluation loop."""
        if self._shock_active:
            self._cycles_since_shock += 1

    # ────────────────────────────────────────────────────
    #  Evaluation
    # ────────────────────────────────────────────────────

    def evaluate(
        self,
        *,
        avg_fill_quality: float = 1.0,
        avg_edge_quality: float = 1.0,
        regime_clarity: float = 1.0,
        doctrine_validity: float = 1.0,
    ) -> RecoveryCheckResult:
        """
        Check if conditions support recovery from shock.

        Returns RecoveryCheckResult with validity and recommendation.
        """
        # If no shock is active, always valid
        if not self._shock_active:
            return RecoveryCheckResult(
                validity=RecoveryValidity.VALID,
                validity_score=1.0,
                execution_normalized=True,
                opportunity_recovered=True,
                regime_stabilized=True,
                doctrine_resolved=True,
                recommendation="restore",
                detail="no_active_shock",
            )

        # Minimum hold period
        if self._cycles_since_shock < self.MIN_HOLD_CYCLES:
            return RecoveryCheckResult(
                validity=RecoveryValidity.INVALID,
                validity_score=0.0,
                recommendation="hold",
                detail=f"min_hold: {self._cycles_since_shock}/{self.MIN_HOLD_CYCLES} cycles",
            )

        # Check each axis
        exec_ok = avg_fill_quality >= self.EXEC_THRESHOLD
        opp_ok = avg_edge_quality >= self.OPPORTUNITY_THRESHOLD
        regime_ok = regime_clarity >= self.REGIME_THRESHOLD
        doctrine_ok = doctrine_validity >= self.DOCTRINE_THRESHOLD

        axes_passed = sum([exec_ok, opp_ok, regime_ok, doctrine_ok])

        # Validity score: weighted
        validity_score = (
            0.30 * min(1.0, avg_fill_quality / self.EXEC_THRESHOLD)
            + 0.25 * min(1.0, avg_edge_quality / self.OPPORTUNITY_THRESHOLD)
            + 0.20 * min(1.0, regime_clarity / self.REGIME_THRESHOLD)
            + 0.25 * min(1.0, doctrine_validity / self.DOCTRINE_THRESHOLD)
        )
        validity_score = max(0.0, min(1.0, validity_score))

        if axes_passed == 4:
            validity = RecoveryValidity.VALID
            recommendation = "restore"
            # Clear shock
            self._shock_active = False
            self._cycles_since_shock = 0
        elif axes_passed >= 2:
            validity = RecoveryValidity.PARTIAL
            recommendation = "partial_restore"
        else:
            validity = RecoveryValidity.INVALID
            recommendation = "hold"

        details = (
            f"exec={'OK' if exec_ok else 'FAIL'}({avg_fill_quality:.2f}) "
            f"opp={'OK' if opp_ok else 'FAIL'}({avg_edge_quality:.2f}) "
            f"regime={'OK' if regime_ok else 'FAIL'}({regime_clarity:.2f}) "
            f"doctrine={'OK' if doctrine_ok else 'FAIL'}({doctrine_validity:.2f}) "
            f"cycles={self._cycles_since_shock}"
        )

        result = RecoveryCheckResult(
            validity=validity,
            validity_score=round(validity_score, 4),
            execution_normalized=exec_ok,
            opportunity_recovered=opp_ok,
            regime_stabilized=regime_ok,
            doctrine_resolved=doctrine_ok,
            recommendation=recommendation,
            detail=details,
        )
        self._last_result = result
        return result

    def is_shock_active(self) -> bool:
        return self._shock_active

    def get_last_result(self) -> RecoveryCheckResult | None:
        return self._last_result
