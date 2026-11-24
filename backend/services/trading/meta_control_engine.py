"""
Meta-Control Engine — Global operating mode determination
═════════════════════════════════════════════════════════

Determines the system's strategic posture:

  EXPANSION  — All green: high edge, good execution, low DD, strong regime
  OFFENSIVE  — Strong but not perfect — push hard selectively
  SELECTIVE  — Default: cherry-pick best setups only
  DEFENSIVE  — Stress signals or weakening edge — capital preservation
  SURVIVAL   — Crisis: extreme DD, stack degradation, execution failure

Mode selection depends on:
  • Portfolio drawdown phase
  • Aggregate edge quality
  • Execution conditions
  • Market regime quality
  • Reliability of active setups
  • Stack health score

Asymmetric transitions:
  - Fast degradation (1 cycle to go DEFENSIVE/SURVIVAL)
  - Slow promotion (must hold for N cycles before upgrading)

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.meta_control_models import (
    ModeDecision,
    ModeInputs,
    SystemMode,
)


class MetaControlEngine:
    """Determine the global operating mode of the trading system."""

    # ── Mode parameters: (alloc_mult, max_concurrent_positions, aggression,
    #                       min_edge, min_robustness)
    MODE_PARAMS = {
        SystemMode.EXPANSION:  (1.20, 12, 1.00, 0.00, 0.30),
        SystemMode.OFFENSIVE:  (1.00, 10, 0.85, 0.05, 0.40),
        SystemMode.SELECTIVE:  (0.70,  7, 0.60, 0.10, 0.50),
        SystemMode.DEFENSIVE:  (0.40,  4, 0.30, 0.20, 0.65),
        SystemMode.SURVIVAL:   (0.15,  2, 0.10, 0.40, 0.80),
    }

    # Promotion hold: must stay at score level for N cycles before upgrading
    PROMOTION_HOLD = 3
    # Degradation is instant (1 cycle)

    def __init__(self) -> None:
        self._current_mode = SystemMode.SELECTIVE
        self._candidate_mode: SystemMode | None = None
        self._candidate_cycles = 0

    # ────────────────────────────────────────────────────
    #  PUBLIC
    # ────────────────────────────────────────────────────

    def evaluate(self, inputs: ModeInputs) -> ModeDecision:
        """Evaluate inputs and determine the global operating mode."""
        raw_score = self._compute_score(inputs)
        target_mode = self._score_to_mode(raw_score)
        previous = self._current_mode

        # Fast degradation
        if self._mode_rank(target_mode) > self._mode_rank(self._current_mode):
            self._current_mode = target_mode
            self._candidate_mode = None
            self._candidate_cycles = 0
        # Slow promotion
        elif self._mode_rank(target_mode) < self._mode_rank(self._current_mode):
            if self._candidate_mode == target_mode:
                self._candidate_cycles += 1
                if self._candidate_cycles >= self.PROMOTION_HOLD:
                    self._current_mode = target_mode
                    self._candidate_mode = None
                    self._candidate_cycles = 0
            else:
                self._candidate_mode = target_mode
                self._candidate_cycles = 1
        else:
            # Same mode — reset candidate
            self._candidate_mode = None
            self._candidate_cycles = 0

        params = self.MODE_PARAMS[self._current_mode]
        mode_changed = self._current_mode != previous

        return ModeDecision(
            mode=self._current_mode,
            previous_mode=previous,
            mode_changed=mode_changed,
            confidence=raw_score,
            allocation_multiplier=params[0],
            max_concurrent=params[1],
            aggression=params[2],
            min_edge_threshold=params[3],
            min_robustness_threshold=params[4],
            reason=self._build_reason(inputs, raw_score, target_mode),
        )

    def get_mode(self) -> SystemMode:
        return self._current_mode

    def force_mode(self, mode: SystemMode) -> None:
        """Manual override for emergency situations."""
        self._current_mode = mode
        self._candidate_mode = None
        self._candidate_cycles = 0

    # ────────────────────────────────────────────────────
    #  INTERNAL — scoring
    # ────────────────────────────────────────────────────

    # Weights for composite score
    W_DD = 0.25           # drawdown impact
    W_EDGE = 0.20         # edge quality
    W_EXEC = 0.15         # execution quality
    W_REGIME = 0.15       # regime quality
    W_ROBUST = 0.10       # robustness
    W_HEALTH = 0.15       # stack health

    def _compute_score(self, inp: ModeInputs) -> float:
        """Compute composite score 0–1 (1 = best conditions)."""
        # DD score: 0% DD → 1.0, 25% DD → 0.0
        dd_score = max(0.0, 1.0 - inp.drawdown_pct / 0.25)

        # Map drawdown phases to penalty
        phase_penalties = {
            "normal": 0.0, "caution": 0.15,
            "stressed": 0.35, "critical": 0.60,
            "recovery": 0.25,
        }
        dd_score -= phase_penalties.get(inp.drawdown_phase, 0.0)
        dd_score = max(0.0, dd_score)

        composite = (
            self.W_DD * dd_score
            + self.W_EDGE * inp.avg_edge_quality
            + self.W_EXEC * inp.avg_execution_quality
            + self.W_REGIME * inp.regime_quality
            + self.W_ROBUST * inp.avg_robustness
            + self.W_HEALTH * inp.stack_health
        )
        return max(0.0, min(1.0, composite))

    @staticmethod
    def _score_to_mode(score: float) -> SystemMode:
        if score >= 0.85:
            return SystemMode.EXPANSION
        if score >= 0.70:
            return SystemMode.OFFENSIVE
        if score >= 0.50:
            return SystemMode.SELECTIVE
        if score >= 0.30:
            return SystemMode.DEFENSIVE
        return SystemMode.SURVIVAL

    @staticmethod
    def _mode_rank(mode: SystemMode) -> int:
        """Higher rank = more defensive. Fast transition upward."""
        return {
            SystemMode.EXPANSION: 0,
            SystemMode.OFFENSIVE: 1,
            SystemMode.SELECTIVE: 2,
            SystemMode.DEFENSIVE: 3,
            SystemMode.SURVIVAL: 4,
        }[mode]

    @staticmethod
    def _build_reason(inp: ModeInputs, score: float, target: SystemMode) -> str:
        parts = [f"score={score:.2f}"]
        if inp.drawdown_pct > 0.10:
            parts.append(f"dd={inp.drawdown_pct:.0%}")
        if inp.avg_edge_quality < 0.50:
            parts.append(f"edge_weak={inp.avg_edge_quality:.2f}")
        if inp.avg_execution_quality < 0.50:
            parts.append(f"exec_weak={inp.avg_execution_quality:.2f}")
        if inp.stack_health < 0.50:
            parts.append(f"health_low={inp.stack_health:.2f}")
        return "; ".join(parts)
