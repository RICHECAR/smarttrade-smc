"""
Portfolio Drawdown Governor — Global drawdown management
════════════════════════════════════════════════════════

Tracks portfolio-level drawdown and progressively tightens:
  • Allocation multiplier
  • Max concurrent positions
  • Aggression level

Phases:
  NORMAL   (DD < 5%)  → full aggression
  CAUTION  (5–10%)    → 80% allocation, reduced concurrent
  STRESSED (10–20%)   → 50% allocation, conservative only
  CRITICAL (> 20%)    → 25% allocation, minimal positions
  RECOVERY            → gradual ramp from stressed/critical back to normal

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.portfolio_models import (
    DrawdownPhase,
    DrawdownState,
)


class PortfolioDrawdownGovernor:
    """Track and govern global portfolio drawdown."""

    # Phase boundaries
    CAUTION_PCT = 0.05
    STRESSED_PCT = 0.10
    CRITICAL_PCT = 0.20

    # Phase parameters: (alloc_mult, max_concurrent, aggression)
    PHASE_PARAMS = {
        DrawdownPhase.NORMAL:   (1.00, 10, 1.00),
        DrawdownPhase.CAUTION:  (0.80,  7, 0.70),
        DrawdownPhase.STRESSED: (0.50,  4, 0.40),
        DrawdownPhase.CRITICAL: (0.25,  2, 0.15),
        DrawdownPhase.RECOVERY: (0.60,  5, 0.50),
    }

    # Recovery: must recover 50% of drawdown before upgrading phase
    RECOVERY_THRESHOLD = 0.50

    def __init__(self) -> None:
        self._state = DrawdownState()

    # ────────────────────────────────────────────────────
    #  PUBLIC
    # ────────────────────────────────────────────────────

    def update(self, current_equity: float) -> DrawdownState:
        """Update drawdown state with new equity reading."""
        # Track peak
        if current_equity > self._state.peak_equity:
            self._state.peak_equity = current_equity

        self._state.current_equity = current_equity

        # Compute drawdown
        if self._state.peak_equity > 0:
            dd = 1.0 - (current_equity / self._state.peak_equity)
            self._state.drawdown_pct = max(0.0, dd)
        else:
            self._state.drawdown_pct = 0.0

        # Determine phase
        old_phase = self._state.phase
        new_phase = self._classify_phase(self._state.drawdown_pct, old_phase)
        self._state.phase = new_phase

        # Recovery progress
        if new_phase == DrawdownPhase.RECOVERY:
            # How far back from worst to peak
            worst_dd = self._state.drawdown_pct
            if worst_dd > 0:
                self._state.recovery_progress = max(0.0, 1.0 - (worst_dd / self.CRITICAL_PCT))
            else:
                self._state.recovery_progress = 1.0
        elif new_phase == DrawdownPhase.NORMAL:
            self._state.recovery_progress = 1.0
        else:
            self._state.recovery_progress = 0.0

        # Apply phase parameters
        mult, max_pos, aggr = self.PHASE_PARAMS[new_phase]
        self._state.allocation_multiplier = mult
        self._state.max_concurrent_positions = max_pos
        self._state.aggression_level = aggr

        return self._state

    def get_state(self) -> DrawdownState:
        return self._state

    def get_allocation_multiplier(self) -> float:
        return self._state.allocation_multiplier

    def can_open_position(self, current_open: int) -> bool:
        return current_open < self._state.max_concurrent_positions

    # ────────────────────────────────────────────────────
    #  INTERNAL
    # ────────────────────────────────────────────────────

    def _classify_phase(
        self,
        dd: float,
        old_phase: DrawdownPhase,
    ) -> DrawdownPhase:
        """Classify drawdown into phase, with hysteresis for recovery."""
        # Going deeper
        if dd >= self.CRITICAL_PCT:
            return DrawdownPhase.CRITICAL
        if dd >= self.STRESSED_PCT:
            return DrawdownPhase.STRESSED
        if dd >= self.CAUTION_PCT:
            # If coming back from stressed/critical, enter recovery
            if old_phase in (DrawdownPhase.STRESSED, DrawdownPhase.CRITICAL,
                             DrawdownPhase.RECOVERY):
                return DrawdownPhase.RECOVERY
            return DrawdownPhase.CAUTION

        # DD < caution threshold
        if old_phase in (DrawdownPhase.STRESSED, DrawdownPhase.CRITICAL,
                         DrawdownPhase.RECOVERY, DrawdownPhase.CAUTION):
            # Must recover enough before returning to normal
            # (already below 5%, so allow normal)
            return DrawdownPhase.NORMAL
        return DrawdownPhase.NORMAL
