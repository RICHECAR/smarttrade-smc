"""
CapitalAllocator — Adaptive risk budgeting for Phase 5.
Dynamically adjusts position sizes based on execution quality and infrastructure stability.
Only automatic transitions. Fail-closed philosophy.
"""

import logging
import time
import json
from enum import IntEnum
from typing import Dict, Any, Optional
from smc_futures.observability.metrics import (
    CAPITAL_ALLOCATED_GAUGE, RISK_MULTIPLIER_GAUGE, CAPITAL_REGIME_GAUGE, EXECUTION_QUALITY_SCORE,
    ORPHAN_DETECTED_TOTAL, LATE_FILL_TOTAL, STALE_REJECTION_RATE, REJECTS_TOTAL
)

logger = logging.getLogger("capital_allocator")

class CapitalRegime(IntEnum):
    FROZEN = 0
    MICRO = 1
    NORMAL = 2
    EXPANSION = 3
    RETRACTION = 4

class CapitalAllocator:
    """
    State machine and allocation calculator.
    Purely deterministic. No network calls.
    """
    
    def __init__(self, initial_regime: CapitalRegime = CapitalRegime.MICRO):
        self.regime = initial_regime
        self.last_regime_change_ts = time.time()
        self.last_incident_ts = 0
        
        # Internal performance tracking for transitions
        self.consecutive_safe_days = 0 
        self.last_rolling_stale_rate = 0.0
        
        # Regime multipliers
        self.regime_multipliers = {
            CapitalRegime.FROZEN: 0.0,
            CapitalRegime.MICRO: 0.25,
            CapitalRegime.NORMAL: 1.0,
            CapitalRegime.EXPANSION: 1.2,
            CapitalRegime.RETRACTION: 0.1,
        }
        
    def get_allocation_params(self, symbol: str, equity: float, base_risk_pct: float) -> Dict[str, Any]:
        """
        Main entry point for decision.
        Returns {max_position_size, execution_allowed, regime, multiplier}.
        """
        # 1. Self-audit metrics to trigger transitions
        self._update_regime(symbol)
        
        # 2. Calculate execution quality score (0.0 - 1.0)
        # Higher is better. Derived from rejections and latency.
        q_score = self._calculate_quality_score(symbol)
        EXECUTION_QUALITY_SCORE.labels(symbol=symbol).set(q_score)
        
        # 3. Apply Multiplier Formula (Transparent & Auditable)
        # Base multiplier from current regime
        regime_mult = self.regime_multipliers[self.regime]
        
        # Specific penalties (dynamic scaling within regime)
        latency_penalty = self.last_rolling_stale_rate * 0.7
        penalty_score = clamp(latency_penalty, 0, 0.5)
        
        risk_multiplier = clamp(
            regime_mult * (q_score - penalty_score),
            min_val=0.0,
            max_val=1.2
        )
        
        if self.regime == CapitalRegime.FROZEN:
            risk_multiplier = 0.0

        RISK_MULTIPLIER_GAUGE.labels(symbol=symbol).set(risk_multiplier)
        
        # 4. Final Capital Allocation
        allocated_capital = equity * (base_risk_pct / 100.0) * risk_multiplier
        CAPITAL_ALLOCATED_GAUGE.labels(symbol=symbol).set(risk_multiplier)
        
        return {
            "regime": self.regime.name,
            "risk_multiplier": round(risk_multiplier, 4),
            "allocated_capital_usd": round(allocated_capital, 2),
            "execution_allowed": (risk_multiplier > 0 and self.regime != CapitalRegime.FROZEN),
            "quality_score": round(q_score, 4),
            "stale_rate": round(self.last_rolling_stale_rate, 4)
        }

    def _update_regime(self, symbol: str):
        """State machine logic for regime transitions."""
        old_regime = self.regime
        now = time.time()
        
        # 1. Hard Stops (Fail-Closed)
        # Check for Orphans or Late Fills (critical infrastructure failure)
        try:
            orphans = ORPHAN_DETECTED_TOTAL._value.get()
            late_fills = LATE_FILL_TOTAL._value.get()
            if orphans > 0 or late_fills > 0:
                if self.regime != CapitalRegime.FROZEN:
                    self._transition_to(CapitalRegime.FROZEN, f"CRITICAL_INCIDENT: orphans={orphans}, late_fills={late_fills}", symbol)
                    self.last_incident_ts = now
                return
        except Exception as e:
            logger.error("[CAPITAL] Failed to read orphan/late_fill metrics — FREEZING as precaution: %s", e)
            if self.regime != CapitalRegime.FROZEN:
                self._transition_to(CapitalRegime.FROZEN, f"METRIC_READ_FAILURE: {e}", symbol)
            return

        # 2. Stale Rate Monitoring (Infrastructure Drift)
        stale_rate = 0.0
        try:
            stale_rate = STALE_REJECTION_RATE.labels(symbol=symbol)._value.get()
            self.last_rolling_stale_rate = stale_rate
        except Exception as e:
            logger.warning("[CAPITAL] Failed to read stale_rate metric for %s: %s", symbol, e)

        if stale_rate > 0.20: # 20% stale rate
            if self.regime not in [CapitalRegime.FROZEN, CapitalRegime.RETRACTION]:
                self._transition_to(CapitalRegime.RETRACTION, f"STALE_RATE_HIGH: {stale_rate*100:.1f}%", symbol)
            return

        # 3. Progressive Scale Up (Expansive)
        # Requires 72h of stability (simplified to time since last change/incident)
        if self.regime == CapitalRegime.NORMAL and (now - self.last_regime_change_ts) > (72 * 3600):
            if stale_rate < 0.05: # Infrastucture must be perfect for expansion
                self._transition_to(CapitalRegime.EXPANSION, "72H_STABLE_OBSERVATION", symbol)
                return

        # 4. Recovery (From Retraction to Micro/Normal)
        if self.regime == CapitalRegime.RETRACTION and (now - self.last_regime_change_ts) > 3600:
            if stale_rate < 0.05:
                self._transition_to(CapitalRegime.MICRO, "RECOVERY_COOLDOWN_PASSED", symbol)
                return

        # 5. Initialization / Normalization
        if self.regime == CapitalRegime.MICRO and (now - self.last_regime_change_ts) > 86400: # 24h of micro success
             if stale_rate < 0.10:
                 self._transition_to(CapitalRegime.NORMAL, "MICRO_TEST_PASSED", symbol)

    def _transition_to(self, new_regime: CapitalRegime, reason: str, symbol: str):
        old_regime = self.regime
        self.regime = new_regime
        self.last_regime_change_ts = time.time()
        
        CAPITAL_REGIME_GAUGE.labels(symbol=symbol).set(int(new_regime))
        
        event = {
            "event": "CAPITAL_REGIME_CHANGE",
            "symbol": symbol,
            "from": old_regime.name,
            "to": new_regime.name,
            "reason": reason,
            "timestamp": time.time()
        }
        logger.warning(json.dumps(event))

    def _calculate_quality_score(self, symbol: str) -> float:
        """
        Quality score (1.0 = Perfect, 0.0 = Broken).
        Based on rejection ratios.
        """
        # Simple implementation: 1.0 - rejection_rate
        # In a real system, would include latency p95.
        return clamp(1.0 - self.last_rolling_stale_rate, 0.0, 1.0)

def clamp(val: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(max_val, val))
