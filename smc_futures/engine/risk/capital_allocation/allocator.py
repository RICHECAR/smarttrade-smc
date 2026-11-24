import logging
import time
import os
from typing import Dict, Any, Optional
from .models import (
    ScaleReadinessSnapshot, 
    AllocationDecision, 
    AllocationAction, 
    AllocationRegime,
    RiskBudgetModel
)
from .policy import AllocationPolicy
from .store import AllocationStateStore

logger = logging.getLogger(__name__)

class AdaptiveCapitalAllocator:
    """
    Main implementation of Phase 5 Adaptive Capital Allocation.
    Orchestrates policy decisions, persistence, and regime detection.
    """
    def __init__(
        self, 
        config: Dict[str, Any], 
        store: Optional[AllocationStateStore] = None,
        policy: Optional[AllocationPolicy] = None
    ):
        self.config = config
        self.store = store or AllocationStateStore()
        
        # Configuration
        self.enabled = config.get("ALLOC_ENABLED", os.getenv("ALLOC_ENABLED", "false")).lower() == "true"
        self.base_capital = float(config.get("ALLOC_BASE_CAPITAL_EUR", os.getenv("ALLOC_BASE_CAPITAL_EUR", "1000")))
        self.max_capital = float(config.get("ALLOC_MAX_CAPITAL_EUR", os.getenv("ALLOC_MAX_CAPITAL_EUR", "10000")))
        self.step_size = float(config.get("ALLOC_STEP_EUR", os.getenv("ALLOC_STEP_EUR", "500")))
        self.green_streak_min = int(config.get("ALLOC_GREEN_STREAK_MIN", os.getenv("ALLOC_GREEN_STREAK_MIN", "60")))
        self.delever_factor_red = float(config.get("ALLOC_DELEVER_RED", os.getenv("ALLOC_DELEVER_RED", "0.5")))
        self.min_hold_time_sec = int(config.get("ALLOC_MIN_HOLD_SEC", os.getenv("ALLOC_MIN_HOLD_SEC", "3600")))
        
        self.risk_model = RiskBudgetModel(
            base_capital=self.base_capital,
            max_capital=self.max_capital
        )
        
        # In-memory transient state (streaks)
        self._streaks: Dict[str, float] = {}  # symbol -> first_green_ts
        self._last_step_ts: Dict[str, float] = {} # symbol -> last_step_ts

    def get_allocation(self, symbol: str) -> float:
        """Get current allocated capital for a symbol."""
        state = self.store.get_state(symbol)
        if state:
            return float(state.get("current_capital", self.base_capital))
        return self.base_capital

    def process_snapshot(self, snapshot: ScaleReadinessSnapshot) -> AllocationDecision:
        """
        Process a new readiness snapshot and return an allocation decision.
        """
        symbol = snapshot.symbol
        state = self.store.get_state(symbol) or {
            "current_capital": self.base_capital,
            "last_action": "INIT",
            "timestamp": time.time()
        }
        
        current_cap = float(state["current_capital"])
        now = time.time()
        
        decision = AllocationDecision(
            target_capital_eur=current_cap,
            current_capital_eur=current_cap,
            action=AllocationAction.HOLD,
            reason="DEFAULT_HOLD",
            regime=AllocationRegime.NORMAL
        )

        # 1. HARD SAFETY: Orphan or Late Fills -> Immediate Rollback
        if snapshot.orphan_total > 0 or snapshot.late_fill_total > 0:
            decision.action = AllocationAction.ROLLBACK
            decision.target_capital_eur = self.base_capital * self.delever_factor_red
            decision.reason = f"CRITICAL_FAILURE: orphans={snapshot.orphan_total}, late={snapshot.late_fill_total}"
            decision.regime = AllocationRegime.FROZEN
            self._reset_streak(symbol)
            return self._finalize_decision(decision, snapshot)

        # 2. RED Status -> Immediate Reduction
        if snapshot.status == "RED":
            decision.action = AllocationAction.DOWN
            decision.target_capital_eur = max(self.base_capital * self.delever_factor_red, self.base_capital * 0.1)
            decision.reason = "STATUS_RED_GUARD"
            decision.regime = AllocationRegime.STRESS
            self._reset_streak(symbol)
            return self._finalize_decision(decision, snapshot)

        # 3. AMBER Status -> Freeze
        if snapshot.status == "AMBER":
            decision.action = AllocationAction.HOLD
            decision.reason = "STATUS_AMBER_FREEZE"
            self._reset_streak(symbol)
            return self._finalize_decision(decision, snapshot)

        # 4. GREEN Status -> Potential Upstep
        if snapshot.status == "GREEN":
            # Track streak
            if symbol not in self._streaks:
                self._streaks[symbol] = now
            
            streak_duration = now - self._streaks[symbol]
            
            # Check conditions for up-step
            if streak_duration >= (self.green_streak_min * 60):
                last_step = self._last_step_ts.get(symbol, 0)
                if (now - last_step) >= self.min_hold_time_sec:
                    if current_cap < self.max_capital:
                        decision.action = AllocationAction.UP
                        decision.target_capital_eur = min(current_cap + self.step_size, self.max_capital)
                        decision.reason = f"STABLE_GREEN_STREAK: {int(streak_duration/60)}min"
                        self._last_step_ts[symbol] = now
                    else:
                        decision.reason = "MAX_CAPITAL_REACHED"
                else:
                    decision.reason = f"WAITING_HOLD_TIME: {int(self.min_hold_time_sec - (now - last_step))}s left"
            else:
                decision.reason = f"BUILDING_STREAK: {int(streak_duration/60)}/{self.green_streak_min}min"

        return self._finalize_decision(decision, snapshot)

    def _reset_streak(self, symbol: str):
        if symbol in self._streaks:
            del self._streaks[symbol]

    def _finalize_decision(self, decision: AllocationDecision, snapshot: ScaleReadinessSnapshot) -> AllocationDecision:
        """Persist findings and emit logs."""
        symbol = snapshot.symbol
        
        # Record to store
        self.store.update_state(symbol, {
            "current_capital": decision.target_capital_eur,
            "last_action": decision.action.value,
            "reason": decision.reason,
            "regime": decision.regime.value,
            "timestamp": decision.timestamp,
            "status": snapshot.status
        })
        
        if decision.action != AllocationAction.HOLD:
            logger.info(
                f"[ALLOCATION] Decision for {symbol}: {decision.action.value.upper()} "
                f"| {decision.current_capital_eur:.0f} -> {decision.target_capital_eur:.0f} "
                f"| Reason: {decision.reason} | Regime: {decision.regime.value}"
            )
        
        return decision

    def get_multiplier(self, symbol: str) -> float:
        """
        Calculate a risk multiplier based on current versus base capital.
        Can be used by Risk Engine to adjust position sizes.
        """
        current = self.get_allocation(symbol)
        return current / self.base_capital
