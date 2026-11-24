"""
Recovery Protocol Engine
════════════════════════

After a significant drawdown, prevents the system from instantly
returning to full aggression.  Enforces a phased recovery:

  NONE          → no recovery active (normal operations)
  STABILISATION → just entered DD threshold, halt new positions, let
                  existing ones play out.  Duration: 6 cycles.
  REBUILDING    → allow 1 new position per cycle at half size.
                  Must confirm 3 winning trades before advancing.
  VERIFICATION  → gradually increase capacity (60%).
                  Must sustain positive equity delta for 5 cycles.
  RESTORED      → full capacity restored.  If DD re-occurs → instant
                  STABILISATION again.

Trigger: drawdown_pct > trigger_threshold (default 12%).
Reset:   equity recovers above high-water mark.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.meta_control_models import RecoveryPhase, RecoveryState


class RecoveryProtocolEngine:
    """Phased recovery after significant drawdown events."""

    TRIGGER_DD_PCT = 0.12          # DD% that triggers recovery
    STABILISATION_CYCLES = 6       # cycles to stay in stabilisation
    REBUILDING_WINS_NEEDED = 3     # winning trades to exit REBUILDING
    VERIFICATION_CYCLES = 5        # positive-delta cycles needed

    def __init__(self) -> None:
        self._phase = RecoveryPhase.NONE
        self._cycles_in_phase = 0
        self._wins_since_phase = 0
        self._positive_deltas = 0
        self._trigger_equity: float = 0.0
        self._hwm: float = 0.0
        self._last_equity: float = 0.0

    # ────────────────────────────────────────────────────
    #  PUBLIC
    # ────────────────────────────────────────────────────

    def update(
        self,
        equity: float,
        drawdown_pct: float,
        *,
        trade_won: bool | None = None,
    ) -> RecoveryState:
        """
        Update recovery state each cycle.

        Parameters
        ----------
        equity : float
            Current portfolio equity.
        drawdown_pct : float
            Current drawdown percentage (0–1).
        trade_won : bool | None
            If a trade closed this cycle, whether it won.
        """
        # Track HWM
        if equity > self._hwm:
            self._hwm = equity

        # Equity delta
        delta = equity - self._last_equity if self._last_equity > 0 else 0.0
        self._last_equity = equity

        # ── Entry condition
        if self._phase == RecoveryPhase.NONE:
            if drawdown_pct >= self.TRIGGER_DD_PCT:
                self._enter_phase(RecoveryPhase.STABILISATION, equity)
            return self._build_state(drawdown_pct)

        # ── Re-trigger: if DD deepens during recovery, reset to STABILISATION
        if (
            self._phase not in (RecoveryPhase.NONE, RecoveryPhase.STABILISATION)
            and drawdown_pct >= self.TRIGGER_DD_PCT * 1.5
        ):
            self._enter_phase(RecoveryPhase.STABILISATION, equity)
            return self._build_state(drawdown_pct)

        # ── Exit: equity above HWM → restored
        if drawdown_pct <= 0.01 and equity >= self._hwm * 0.99:
            self._phase = RecoveryPhase.RESTORED
            self._cycles_in_phase = 0
            # After 1 cycle of RESTORED, go back to NONE
            return self._build_state(drawdown_pct)

        # ── Phase transitions
        self._cycles_in_phase += 1

        if trade_won is not None and trade_won:
            self._wins_since_phase += 1

        if delta > 0:
            self._positive_deltas += 1

        if self._phase == RecoveryPhase.STABILISATION:
            if self._cycles_in_phase >= self.STABILISATION_CYCLES:
                self._enter_phase(RecoveryPhase.REBUILDING, equity)

        elif self._phase == RecoveryPhase.REBUILDING:
            if self._wins_since_phase >= self.REBUILDING_WINS_NEEDED:
                self._enter_phase(RecoveryPhase.VERIFICATION, equity)

        elif self._phase == RecoveryPhase.VERIFICATION:
            if self._positive_deltas >= self.VERIFICATION_CYCLES:
                self._enter_phase(RecoveryPhase.RESTORED, equity)

        elif self._phase == RecoveryPhase.RESTORED:
            # 1 cycle grace, then NONE
            if self._cycles_in_phase >= 1:
                self._phase = RecoveryPhase.NONE
                self._cycles_in_phase = 0

        return self._build_state(drawdown_pct)

    def get_phase(self) -> RecoveryPhase:
        return self._phase

    def force_reset(self) -> None:
        """Manual reset — for operator intervention."""
        self._phase = RecoveryPhase.NONE
        self._cycles_in_phase = 0
        self._wins_since_phase = 0
        self._positive_deltas = 0

    # ────────────────────────────────────────────────────
    #  Capacity multipliers per phase
    # ────────────────────────────────────────────────────

    def get_capacity_multiplier(self) -> float:
        """How much of normal capacity is allowed."""
        return {
            RecoveryPhase.NONE: 1.0,
            RecoveryPhase.STABILISATION: 0.0,   # no new positions
            RecoveryPhase.REBUILDING: 0.30,     # 30% capacity
            RecoveryPhase.VERIFICATION: 0.60,   # 60% capacity
            RecoveryPhase.RESTORED: 1.0,
        }[self._phase]

    def get_max_new_positions(self) -> int:
        """Max new positions allowed per cycle in current phase."""
        return {
            RecoveryPhase.NONE: 999,
            RecoveryPhase.STABILISATION: 0,
            RecoveryPhase.REBUILDING: 1,
            RecoveryPhase.VERIFICATION: 2,
            RecoveryPhase.RESTORED: 999,
        }[self._phase]

    # ────────────────────────────────────────────────────
    #  Internal
    # ────────────────────────────────────────────────────

    def _enter_phase(self, phase: RecoveryPhase, equity: float) -> None:
        self._phase = phase
        self._cycles_in_phase = 0
        self._wins_since_phase = 0
        self._positive_deltas = 0
        self._trigger_equity = equity

    def _build_state(self, drawdown_pct: float) -> RecoveryState:
        return RecoveryState(
            phase=self._phase,
            trigger_drawdown=self._trigger_equity,
            current_drawdown=drawdown_pct,
            cycles_in_phase=self._cycles_in_phase,
            allocation_cap=self.get_capacity_multiplier(),
            min_evidence_trades=max(0, self.REBUILDING_WINS_NEEDED - self._wins_since_phase),
            expansion_allowed=self._phase in (RecoveryPhase.NONE, RecoveryPhase.RESTORED),
            progress=self._compute_progress(),
        )

    def _compute_progress(self) -> float:
        """Compute 0-1 progress toward full restoration."""
        if self._phase == RecoveryPhase.NONE:
            return 1.0
        if self._phase == RecoveryPhase.STABILISATION:
            return min(1.0, self._cycles_in_phase / max(1, self.STABILISATION_CYCLES)) * 0.25
        if self._phase == RecoveryPhase.REBUILDING:
            return 0.25 + min(1.0, self._wins_since_phase / max(1, self.REBUILDING_WINS_NEEDED)) * 0.25
        if self._phase == RecoveryPhase.VERIFICATION:
            return 0.50 + min(1.0, self._positive_deltas / max(1, self.VERIFICATION_CYCLES)) * 0.40
        return 0.90  # RESTORED
