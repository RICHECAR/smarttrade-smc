"""
Parameter Adaptation Engine
════════════════════════════

Dynamically adjusts system parameters (thresholds, confidence levels,
scoring weights, risk multipliers) based on measured effectiveness.

Pipeline:
  1. Register parameters with bounds and defaults
  2. Record outcome after each parameter is used
  3. Compute effectiveness (before/after outcome comparison)
  4. Propose bounded adjustments toward better-performing regions
  5. Emit AdaptationPlan with rollback support

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from backend.services.trading.self_evolution_models import (
    AdaptationPlan,
    ParameterState,
)


class ParameterAdaptationEngine:
    """Adapts system parameters based on measured outcome effectiveness."""

    # ── Configuration ─────────────────────────────────────────────
    MIN_OBSERVATIONS = 8        # min outcomes per param before adapting
    LEARNING_RATE = 0.05        # max relative step per adaptation
    EXPLORATION_RATE = 0.10     # fraction of time to try a wider step
    EFFECTIVENESS_DECAY = 0.95  # exponential decay on old observations
    MAX_RELATIVE_CHANGE = 0.20  # max 20 % deviation from default
    COOLDOWN_CYCLES = 3         # min cycles between adjustments

    def __init__(self) -> None:
        # param_key → ParameterState
        self._params: Dict[str, ParameterState] = {}
        # param_key → list of (value_at_time, outcome_pnl)
        self._observations: Dict[str, List[tuple]] = defaultdict(list)
        # param_key → cycles since last change
        self._cooldown: Dict[str, int] = defaultdict(int)
        # param_key → previous values for rollback
        self._rollback_stack: Dict[str, List[float]] = defaultdict(list)
        self._last_plan: AdaptationPlan | None = None
        self._cycle_count: int = 0

    # ── Register ──────────────────────────────────────────────────
    def register_parameter(
        self,
        param_name: str,
        module_name: str,
        default_value: float,
        min_bound: float,
        max_bound: float,
    ) -> None:
        """Register a parameter for dynamic adaptation."""
        key = f"{module_name}.{param_name}"
        self._params[key] = ParameterState(
            param_name=param_name,
            module_name=module_name,
            current_value=default_value,
            default_value=default_value,
            min_bound=min_bound,
            max_bound=max_bound,
        )

    # ── Record outcome ────────────────────────────────────────────
    def record_outcome(
        self,
        param_name: str,
        module_name: str,
        outcome_pnl: float,
    ) -> None:
        """Record the outcome achieved when a parameter had its current value."""
        key = f"{module_name}.{param_name}"
        if key not in self._params:
            return
        current = self._params[key].current_value
        self._observations[key].append((current, outcome_pnl))

    # ── Adapt ─────────────────────────────────────────────────────
    def adapt(self) -> AdaptationPlan:
        """Propose parameter adjustments based on accumulated observations."""
        self._cycle_count += 1
        changes: List[ParameterState] = []

        for key, state in self._params.items():
            self._cooldown[key] += 1
            obs = self._observations.get(key, [])

            if len(obs) < self.MIN_OBSERVATIONS:
                continue
            if self._cooldown[key] < self.COOLDOWN_CYCLES:
                continue

            new_value = self._compute_optimal(key, state, obs)
            if new_value is None:
                continue

            # Check relative change cap
            max_dev = self.MAX_RELATIVE_CHANGE * abs(state.default_value) if state.default_value != 0 else (state.max_bound - state.min_bound) * self.MAX_RELATIVE_CHANGE
            clamped = max(state.default_value - max_dev,
                          min(state.default_value + max_dev, new_value))
            clamped = max(state.min_bound, min(state.max_bound, clamped))

            if abs(clamped - state.current_value) < 1e-8:
                continue

            # Effectiveness: compare recent vs old outcomes
            eff = self._compute_effectiveness(obs)

            # Store rollback
            self._rollback_stack[key].append(state.current_value)
            if len(self._rollback_stack[key]) > 10:
                self._rollback_stack[key] = self._rollback_stack[key][-10:]

            # Apply
            old_val = state.current_value
            state.current_value = clamped
            state.last_change = clamped - old_val
            state.change_count += 1
            state.effectiveness_score = eff
            self._cooldown[key] = 0

            changes.append(ParameterState(
                param_name=state.param_name,
                module_name=state.module_name,
                current_value=clamped,
                default_value=state.default_value,
                min_bound=state.min_bound,
                max_bound=state.max_bound,
                last_change=clamped - old_val,
                change_count=state.change_count,
                effectiveness_score=eff,
            ))

        avg_eff = (sum(c.effectiveness_score for c in changes) / len(changes)) if changes else 0.0

        plan = AdaptationPlan(
            adaptations=changes,
            total_changes=len(changes),
            avg_effectiveness=avg_eff,
            rollback_available=True,
        )
        self._last_plan = plan
        return plan

    # ── Rollback ──────────────────────────────────────────────────
    def rollback(self, param_name: str, module_name: str) -> bool:
        """Revert a parameter to its previous value."""
        key = f"{module_name}.{param_name}"
        if key not in self._params or not self._rollback_stack.get(key):
            return False
        prev = self._rollback_stack[key].pop()
        self._params[key].current_value = prev
        return True

    def rollback_all(self) -> int:
        """Revert all parameters to defaults. Returns count reverted."""
        count = 0
        for key, state in self._params.items():
            if state.current_value != state.default_value:
                state.current_value = state.default_value
                state.last_change = 0.0
                count += 1
        return count

    # ── Getters ───────────────────────────────────────────────────
    def get_last_plan(self) -> AdaptationPlan | None:
        return self._last_plan

    def get_parameter(self, param_name: str, module_name: str) -> Optional[ParameterState]:
        key = f"{module_name}.{param_name}"
        return self._params.get(key)

    def get_all_parameters(self) -> List[ParameterState]:
        return list(self._params.values())

    def get_value(self, param_name: str, module_name: str) -> float:
        key = f"{module_name}.{param_name}"
        if key in self._params:
            return self._params[key].current_value
        return 0.0

    # ── Internal ──────────────────────────────────────────────────
    def _compute_optimal(self, key: str, state: ParameterState, obs: List[tuple]) -> Optional[float]:
        """Weighted regression pointing toward the best-performing parameter value."""
        if len(obs) < self.MIN_OBSERVATIONS:
            return None

        # Apply decay weights (recent observations matter more)
        n = len(obs)
        weights = [self.EFFECTIVENESS_DECAY ** (n - 1 - i) for i in range(n)]
        total_w = sum(weights)

        # Weighted mean of values weighted by outcome
        # Positive outcomes → pull toward that value
        val_sum = 0.0
        out_sum = 0.0
        for i, (val, outcome) in enumerate(obs):
            w = weights[i]
            val_sum += w * val * (1.0 + outcome)  # outcome-boosted value
            out_sum += w * (1.0 + outcome)

        if out_sum <= 0:
            return None

        target = val_sum / out_sum
        # Step toward target with learning rate
        step = (target - state.current_value) * self.LEARNING_RATE
        return state.current_value + step

    def _compute_effectiveness(self, obs: List[tuple]) -> float:
        """Measure whether recent outcomes are better than older ones."""
        if len(obs) < 4:
            return 0.5

        half = len(obs) // 2
        old_pnls = [o[1] for o in obs[:half]]
        new_pnls = [o[1] for o in obs[half:]]

        old_mean = sum(old_pnls) / len(old_pnls)
        new_mean = sum(new_pnls) / len(new_pnls)

        # Normalise to 0–1 where 0.5 = no change
        if old_mean == 0:
            return 0.5 + min(0.5, max(-0.5, new_mean))
        improvement = (new_mean - old_mean) / max(0.001, abs(old_mean))
        return max(0.0, min(1.0, 0.5 + improvement * 0.5))
