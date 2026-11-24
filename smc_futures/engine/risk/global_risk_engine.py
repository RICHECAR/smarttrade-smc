import logging
import time
from typing import Dict, Any, Optional
from smc_futures.observability.metrics import (
    REJECTS_TOTAL, ORPHAN_DETECTED_TOTAL, LATE_FILL_TOTAL,
    EXECUTION_MODE_ACTIVE, COOLDOWN_ACTIVE
)
from smc_futures.engine.risk.capital_allocation import (
    AdaptiveCapitalAllocator, 
    ScaleReadinessSnapshot,
    AllocationAction
)

logger = logging.getLogger("risk_engine")

class GlobalRiskEngine:
    """
    Singleton service that intercepts stateless intents before they reach the OMS.
    Performs hard validations across the system to protect capital.
    """
    
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(GlobalRiskEngine, cls).__new__(cls, *args, **kwargs)
        return cls._instance
        
    def __init__(self):
        # We only initialize once
        if getattr(self, '_initialized', False):
            return
            
        self.stale_threshold_ms = 250  # Hardcoded max latency from Phase 0 constraints
        self.max_daily_loss_pct = 3.0
        
        # Phase 2 Microstructure Caps
        self.max_spread_bps = 5.0  # e.g., Maximum allowed BBA spread distance 
        self.max_liquidity_consumption_pct = 0.50 # e.g., Order Size must not exceed 50% of the BBA volume
        
        # Phase 4 Asymmetric Maker Caps & Hardening
        self.min_maker_spread_bps = 0.5 # Minimum spread required to justify a Post-Only Maker limit
        self.cooldown_duration_ms = 60000 # 60 seconds post-incident cooldown
        self.last_failsafe_timestamp_ms = 0
        
        self.kill_switch_active = False
        self.last_failure_reason = ""
        
        # Phase 5: New persistent allocator
        self.capital_allocator = AdaptiveCapitalAllocator(config={})
        
        # ── Orphan/Late-Fill baseline tracking ─────────────────────
        # Prometheus Counters are monotonic: they never reset to 0.
        # Without a baseline, a SINGLE orphan permanently locks the bot.
        # We track the acknowledged baseline and only lock on NEW incidents.
        self._orphan_baseline = 0
        self._late_fill_baseline = 0
        
        # Analytics recorder (lazy import to avoid circular deps)
        self._ar = None
        try:
            from backend.services.analytics_service import AnalyticsRecorder
            self._ar = AnalyticsRecorder
        except ImportError:
            pass
        
        self._initialized = True
        
    def trigger_cooldown(self, reason: str):
        """Activer un temps de repos post-incident (FLATTEN / FAILED_SAFE)."""
        self.last_failsafe_timestamp_ms = int(time.time() * 1000)
        self.last_failure_reason = reason
        logger.warning(f"[RISK] COOLDOWN ACTIVATED ({self.cooldown_duration_ms}ms). Reason: {reason}")
        # ── DB: record risk event ──
        if self._ar:
            try:
                self._ar.record_risk_event(
                    event_type="COOLDOWN",
                    severity="WARN",
                    detail={"reason": reason, "duration_ms": self.cooldown_duration_ms},
                )
            except Exception:
                pass
        
    def activate_kill_switch(self, reason: str):
        """Immediately lock the risk engine."""
        self.kill_switch_active = True
        self.last_failure_reason = reason
        logger.critical(f"🚨 GLOBAL RISK ENGINE LOCKED 🚨 Reason: {reason}")
        # ── DB: record risk event ──
        if self._ar:
            try:
                self._ar.record_risk_event(
                    event_type="KILL_SWITCH",
                    severity="CRITICAL",
                    detail={"reason": reason},
                )
            except Exception:
                pass
    
    def reset_kill_switch(self):
        """Reset kill switch & acknowledge current orphan/late-fill baseline."""
        self.kill_switch_active = False
        self.last_failure_reason = ""
        # Acknowledge current counter values so they don't re-trigger
        try:
            from smc_futures.observability.metrics import ORPHAN_DETECTED_TOTAL, LATE_FILL_TOTAL
            self._orphan_baseline = int(ORPHAN_DETECTED_TOTAL._value.get())
            self._late_fill_baseline = int(LATE_FILL_TOTAL._value.get())
        except Exception:
            pass
        logger.info("[RISK] Kill switch RESET. Orphan/LateFill baselines updated.")
        if self._ar:
            try:
                self._ar.record_risk_event(
                    event_type="KILL_SWITCH_RELEASE",
                    severity="INFO",
                    detail={"action": "manual_reset"},
                )
            except Exception:
                pass
        
    def _get_snapshot(self, symbol: str) -> ScaleReadinessSnapshot:
        """Collect metrics for a Phase 5 Snapshot."""
        from smc_futures.observability.metrics import (
            STALE_REJECTION_RATE, VOLATILITY_SIGMA, 
            LATENCY_P95_GAUGE, LATENCY_P99_GAUGE,
            ORPHAN_DETECTED_TOTAL, LATE_FILL_TOTAL,
            SCALE_GATE_STATUS
        )
        
        # Pull from Prometheus (fallback to 0)
        def _get_val(m, **lbl):
            try: return m.labels(**lbl)._value.get()
            except: return 0.0

        status_code = _get_val(SCALE_GATE_STATUS, symbol=symbol)
        # Matching ScaleReadinessGate GateStatus enum: GREEN=0, AMBER=1, RED=2
        status = "GREEN" if status_code == 0 else ("AMBER" if status_code == 1 else "RED")

        return ScaleReadinessSnapshot(
            symbol=symbol,
            status=status,
            stale_rate=_get_val(STALE_REJECTION_RATE, symbol=symbol),
            sigma=_get_val(VOLATILITY_SIGMA, symbol=symbol),
            orphan_total=int(ORPHAN_DETECTED_TOTAL._value.get()),
            late_fill_total=int(LATE_FILL_TOTAL._value.get()),
            rest_rtt_p95=_get_val(LATENCY_P95_GAUGE, symbol=symbol),
            ack_to_ws_p95=0.0, # Placeholder
            end_to_end_p95=0.0, # Placeholder
            rest_rtt_p99=_get_val(LATENCY_P99_GAUGE, symbol=symbol)
        )

    def validate_intent(self, intent: Dict[str, Any]) -> tuple[bool, str]:
        """
        Validates an incoming strategy intent.
        Returns:
            (is_valid, reject_reason)
        """
        # 0. Capital Allocation Check (Phase 5)
        symbol = intent.get("symbol", "BTCUSDT")
        
        # Update Allocator with latest metrics before deciding
        snapshot = self._get_snapshot(symbol)
        decision = self.capital_allocator.process_snapshot(snapshot)
        
        # Adaptive Multiplier
        multiplier = self.capital_allocator.get_multiplier(symbol)
        intent["risk_multiplier"] = multiplier
        intent["regime"] = decision.regime.value
        
        if decision.action == AllocationAction.ROLLBACK:
            if not self.kill_switch_active:
                self.activate_kill_switch(f"CAPITAL_ROLLBACK: {decision.reason}")
            return False, f"CAPITAL_BLOCKED: {decision.reason}"

        # 0.b Global Guardrail (Orphan / Late Fill Hard Stop)
        # Only trigger on NEW incidents (delta vs acknowledged baseline)
        new_orphans = snapshot.orphan_total - self._orphan_baseline
        new_late_fills = snapshot.late_fill_total - self._late_fill_baseline
        if new_orphans > 0 or new_late_fills > 0:
            if not self.kill_switch_active:
                self.activate_kill_switch(
                    f"CRITICAL INCIDENT: +{new_orphans} orphans, +{new_late_fills} late fills"
                )
            return False, f"SYSTEM LOCKED: {new_orphans} new orphans, {new_late_fills} new late fills."

        # 1. Kill Switch Check
        if self.kill_switch_active:
            return False, "SYSTEM LOCKED: Global Kill Switch is Active"
            
        current_time_ms = int(time.time() * 1000)
        
        # 1.b Cooldown Check (Prevents churn after a rollback/failsafe)
        if current_time_ms - self.last_failsafe_timestamp_ms < self.cooldown_duration_ms:
            COOLDOWN_ACTIVE.set(1)
            time_left = self.cooldown_duration_ms - (current_time_ms - self.last_failsafe_timestamp_ms)
            return False, f"COOLDOWN_ACTIVE: {time_left}ms remaining"
        COOLDOWN_ACTIVE.set(0)
        
        # 1.c Enforce Execution Mode (Locking auto-switch)
        # Forces the intent to ASYMMETRIC_MAKER_TAKER by default for scaling Phase 4.
        exec_mode = "ASYMMETRIC_MAKER_TAKER"
        intent["execution_mode"] = exec_mode
        EXECUTION_MODE_ACTIVE.labels(mode=exec_mode).set(1)
            
        # 2. Stale Data Check (Latency)
        intent_time_ms = intent.get("timestamp_ms", 0)
        current_time_ms = int(time.time() * 1000)
        latency: int = current_time_ms - intent_time_ms
        
        if latency > self.stale_threshold_ms:
            REJECTS_TOTAL.labels(reason='STALE_DATA').inc()
            logger.warning(
                f"[RISK] Intent REJECTED: STALE_DATA. Latency={latency}ms > {self.stale_threshold_ms}ms cutoff. "
                f"Infrastructure drift or exchange lag detected. (Phase 4.5 Latency Guard)"
            )
            if self._ar:
                try:
                    self._ar.record_risk_event(
                        event_type="HEALTH_GATE_FAIL", severity="WARNING",
                        detail={"gate": "STALE_DATA", "latency_ms": latency, "threshold_ms": self.stale_threshold_ms},
                        symbol=symbol,
                    )
                except Exception:
                    pass
            return False, f"STALE_DATA: Latency {latency}ms"
            
        # 3. Microstructure Pre-Flight: Spread Gate
        spread_bps = intent.get("spread_bps", 0)
        if spread_bps > self.max_spread_bps:
            REJECTS_TOTAL.labels(reason='SPREAD_TOO_WIDE').inc()
            logger.warning(f"[RISK] Intent REJECTED: SPREAD TO WIDE. {spread_bps}bps > {self.max_spread_bps}bps.")
            return False, f"SPREAD_TOO_WIDE: {spread_bps}bps"
            
        # 3.b Microstructure Maker Viability Gate (Phase 4)
        #     When spread is too tight for a Post-Only Maker limit,
        #     fallback to TAKER_MARKET instead of rejecting the trade.
        exec_mode = intent.get("execution_mode", "STRICT_MIRROR")
        if exec_mode == "ASYMMETRIC_MAKER_TAKER" and spread_bps < self.min_maker_spread_bps:
             # ── TAKER FALLBACK (Phase 8 fix) ─────────────────────────
             intent["execution_mode"] = "TAKER_MARKET"
             intent["maker_fallback_used"] = True
             logger.info(
                 f"[RISK] MAKER→TAKER FALLBACK | spread={spread_bps}bps < "
                 f"{self.min_maker_spread_bps}bps | switching to TAKER_MARKET"
             )
            
        # 4. Microstructure Pre-Flight: Liquidity Gate (Size vs BBA Depth)
        qty = intent.get("quantity", 0)
        l1_volume = intent.get("l1_volume", float('inf'))  # Infinity assumes no restriction provided by strategy
        if l1_volume > 0:
            consumption_pct = qty / l1_volume
            if consumption_pct > self.max_liquidity_consumption_pct:
                REJECTS_TOTAL.labels(reason='LOW_LIQUIDITY').inc()
                logger.warning(f"[RISK] Intent REJECTED: LIQUIDITY. Consuming {consumption_pct*100}% of L1 > {self.max_liquidity_consumption_pct*100}%")
                return False, f"LOW_LIQUIDITY: Consumption {consumption_pct*100:.1f}%"
            
        # 5. Minimum Quantity / Notional Fallback Risk
        if qty <= 0:
            REJECTS_TOTAL.labels(reason='INVALID_QTY').inc()
            return False, "INVALID_QTY: Size must be > 0"
            
        logger.info(f"[RISK] Intent VALIDATED for {intent.get('symbol')} ({intent.get('side')})")
        return True, ""
        
        
risk_engine = GlobalRiskEngine()
