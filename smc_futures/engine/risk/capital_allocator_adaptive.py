import logging
import time
import os
from enum import Enum
from typing import Dict, Any, Optional
from smc_futures.observability.metrics import (
    CAPITAL_REGIME_GAUGE, RISK_MULTIPLIER_GAUGE, EXECUTION_QUALITY_SCORE
)

logger = logging.getLogger(__name__)

class CapitalRegime(Enum):
    FROZEN = 0      # Critical failure, 0 capital
    MICRO = 1       # AMBER state, min capital (0.25x)
    NORMAL = 2      # GREEN state, standard capital (1.0x)
    EXPANSION = 3   # Stable GREEN, ramp up (1.2x+)
    RETRACTION = 4  # Drifting towards AMBER, soft reduce

class CapitalAllocatorAdaptive:
    """
    Adaptive capital allocation based on ScaleReadinessGate and infrastructure health.
    """
    def __init__(self):
        # Configuration from .env via os.getenv (standard for this project)
        self.mode = os.getenv("ALLOCATOR_MODE", "ADAPTIVE").upper()
        self.min_mult = float(os.getenv("ALLOCATOR_MIN_MULT", 0.25))
        self.max_mult = float(os.getenv("ALLOCATOR_MAX_MULT", 1.2))
        self.ramp_step = float(os.getenv("ALLOCATOR_RAMP_STEP", 0.25))
        self.ramp_hold_min = int(os.getenv("ALLOCATOR_RAMP_HOLD_MIN", 1440)) # minutes
        
        self.current_regime = CapitalRegime.NORMAL
        self.multiplier = 1.0
        self.last_ramp_time = time.time()
        self.quality_score = 1.0 # 0 to 1

    def update_regime(self, gate_status: str, metrics: Dict[str, Any]):
        """
        Transition state based on gate and telemetry.
        """
        old_regime = self.current_regime
        
        # 1. State Transitions
        if gate_status == "RED":
            self.current_regime = CapitalRegime.FROZEN
            self.multiplier = 0.0
        elif gate_status == "AMBER":
            if self.current_regime != CapitalRegime.MICRO:
                self.current_regime = CapitalRegime.MICRO
                self.multiplier = self.min_mult
        elif gate_status == "GREEN":
            # Stability Check for expansion
            now = time.time()
            mins_since_ramp = (now - self.last_ramp_time) / 60
            
            if self.current_regime == CapitalRegime.MICRO:
                 self.current_regime = CapitalRegime.NORMAL
                 self.multiplier = 1.0
                 self.last_ramp_time = now
            elif self.current_regime == CapitalRegime.NORMAL and mins_since_ramp >= self.ramp_hold_min:
                 self.current_regime = CapitalRegime.EXPANSION
                 self.multiplier = min(self.multiplier + self.ramp_step, self.max_mult)
                 self.last_ramp_time = now
            elif self.current_regime == CapitalRegime.EXPANSION and mins_since_ramp >= self.ramp_hold_min:
                 self.multiplier = min(self.multiplier + self.ramp_step, self.max_mult)
                 self.last_ramp_time = now

        # 2. Update Gauges
        CAPITAL_REGIME_GAUGE.labels(symbol="global").set(self.current_regime.value)
        RISK_MULTIPLIER_GAUGE.labels(symbol="global").set(self.multiplier)
        
        if old_regime != self.current_regime:
            logger.info(f"[CAPITAL] Regime changed: {old_regime.name} -> {self.current_regime.name} (Mult={self.multiplier:.2f})")
            # Structured log for audit
            import json
            logger.info(json.dumps({
                "event": "CAPITAL_REGIME_CHANGE",
                "old_regime": old_regime.name,
                "new_regime": self.current_regime.name,
                "multiplier": self.multiplier,
                "gate_status": gate_status,
                "ts": time.time()
            }))

    def get_allocation_multiplier(self) -> float:
        """Return the effective multiplier for OrderManager."""
        if self.mode == "FIXED":
            return 1.0
        return self.multiplier
