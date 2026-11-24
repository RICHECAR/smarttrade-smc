import logging
from enum import Enum
from typing import Dict, Any, Optional
from smc_futures.observability.metrics import (
    SCALE_GATE_STATUS, ORPHAN_DETECTED_TOTAL, LATE_FILL_TOTAL,
    STALE_REJECTION_RATE, VOLATILITY_SIGMA, LATENCY_P95_GAUGE
)

logger = logging.getLogger(__name__)

class GateStatus(Enum):
    GREEN = 0   # All systems nominal, scaling allowed
    AMBER = 1   # High latency or stale rate, pause scaling
    RED = 2     # Critical failure (orphan/late fill), scaling locked

class ScaleReadinessGate:
    """
    Evaluates system readiness for scaling capital.
    Enforces hard stops on critical failures.
    """
    def __init__(self, stale_threshold: float = 0.05, latency_p95_threshold: float = 500.0):
        self.stale_threshold = stale_threshold
        self.latency_p95_threshold = latency_p95_threshold
        self.status = GateStatus.GREEN
        self.reason = "INITIALIZED"
        self.last_evaluation_ts = 0
        
        # Manual Lock
        self.manual_lock = False
        self.lock_reason = ""

    def lock_scaling(self, reason: str):
        """Manually lock scaling (e.g. for maintenance or known incident)."""
        self.manual_lock = True
        self.lock_reason = reason
        self.status = GateStatus.AMBER
        self.reason = f"SCALING_MANUALLY_LOCKED: {reason}"
        logger.warning(f"[GATE] Scaling manually LOCKED: {reason}")
        try:
            SCALE_GATE_STATUS.labels(symbol="TOTAL").set(GateStatus.AMBER.value)
        except Exception:
            pass

    def unlock_scaling(self):
        """Remove manual lock."""
        self.manual_lock = False
        self.lock_reason = ""
        self.reason = "MANUAL_UNLOCK"
        logger.info("[GATE] Scaling manually UNLOCKED")

    def evaluate(self, stale_rate: float, latency_p95: float, sigma: float = 0.0, symbol: str = "TOTAL") -> GateStatus:
        """
        Evaluate readiness based on metrics.
        """
        def _set_gate(val: int) -> None:
            """Safe helper — always pass the required 'symbol' label."""
            try:
                SCALE_GATE_STATUS.labels(symbol=symbol).set(val)
            except Exception:
                pass

        # 1. Manual Lock (AMBER)
        if self.manual_lock:
            self.status = GateStatus.AMBER
            self.reason = f"SCALING_MANUALLY_LOCKED: {self.lock_reason}"
            _set_gate(GateStatus.AMBER.value)
            return self.status

        # 2. Critical Failures (RED)
        # Any orphan or late fill is a hard RED.
        try:
            orphans = ORPHAN_DETECTED_TOTAL._value.get()
            late_fills = LATE_FILL_TOTAL._value.get()
            if orphans > 0 or late_fills > 0:
                self.status = GateStatus.RED
                self.reason = f"CRITICAL_FAILURE: Orphan({orphans}) or LateFill({late_fills}) detected"
                _set_gate(GateStatus.RED.value)
                return self.status
        except Exception:
            pass  # Counter not yet initialized

        # 3. Risk Indicators (RED)
        # 15% stale rate is a hard RED (unstable infra)
        if stale_rate > 0.15:
            self.status = GateStatus.RED
            self.reason = f"EXCESSIVE_STALE: {stale_rate*100:.1f}% > 15%"
            _set_gate(GateStatus.RED.value)
            return self.status

        # 4. Risk Indicators (AMBER)
        if stale_rate > self.stale_threshold or latency_p95 > self.latency_p95_threshold:
            self.status = GateStatus.AMBER
            self.reason = f"PAUSE_SCALING: (Stale={stale_rate*100:.1f}% > {self.stale_threshold*100:.1f}%) OR (p95={latency_p95:.1f}ms > {self.latency_p95_threshold}ms)"
            _set_gate(GateStatus.AMBER.value)
            return self.status

        # 5. Volatility Spike (AMBER/RED)
        if sigma > 0.005:
            self.status = GateStatus.AMBER
            self.reason = f"HIGH_VOLATILITY: Sigma={sigma:.4f} > 0.005"
            _set_gate(GateStatus.AMBER.value)
            return self.status

        # 6. Normal (GREEN)
        self.status = GateStatus.GREEN
        self.reason = "SYSTEMS_NOMINAL"
        _set_gate(GateStatus.GREEN.value)
        return self.status

    def check_readiness(self, symbol: str = "TOTAL") -> Dict[str, Any]:
        """
        Pull latest metrics from Gauges and evaluate status.
        Interface expected by orchestrator and API.
        """
        # Extract values from gauges (symbol-specific)
        stale_rate = 0.0
        latency_p95 = 0.0
        sigma = 0.0
        
        try:
            # Prometheus client Gauge labels return a Child object which has _value
            stale_rate = STALE_REJECTION_RATE.labels(symbol=symbol)._value.get()
            latency_p95 = LATENCY_P95_GAUGE.labels(symbol=symbol)._value.get()
            sigma = VOLATILITY_SIGMA.labels(symbol=symbol)._value.get()
        except Exception:
            pass # Metrics might not be initialized for this symbol yet
            
        status = self.evaluate(stale_rate, latency_p95, sigma, symbol=symbol)
        
        failures = []
        if status != GateStatus.GREEN:
            failures.append(self.reason)
            
        return {
            "symbol": symbol,
            "status": status.name,
            "is_ready": status == GateStatus.GREEN,
            "reason": self.reason,
            "failures": failures,
            "metrics": {
                "stale_rate": f"{stale_rate*100:.2f}%",
                "latency_p95": f"{latency_p95:.1f}ms",
                "sigma": f"{sigma:.5f}"
            }
        }

    def get_summary(self) -> Dict[str, Any]:
        return {
            "status": self.status.name,
            "reason": self.reason,
            "stale_threshold": self.stale_threshold,
            "latency_threshold": self.latency_p95_threshold,
            "manual_lock": self.manual_lock
        }

