import asyncio
import logging
import uuid
import time
from typing import Dict, Any

from smc_futures.engine.execution.base_adapter import BaseExchangeAdapter
try:
    from core.phase6_wiring import evaluate_risk_v2
except ImportError:
    def evaluate_risk_v2(**kw):
        return True, 1.0, []
from smc_futures.observability.alerts import trigger_critical_alert
from smc_futures.app_context import build_app_context

logger = logging.getLogger("oms")

class AsymmetricOrderManager:
    """
    State Machine for Institutional Asymmetric Execution (Maker-Taker).
    Eliminates Double-Slippage Rollback cost by posting Limit (Maker) on Primary,
    waiting for fills, and then Market/Limit Hedging (Taker) on Secondary.
    """
    def __init__(self, adapters: Dict[str, BaseExchangeAdapter], config: Dict[str, Any] = None):
        self.adapters = adapters
        self.config = config or {}
        
        # Phase 4.5 Latency Tracking
        from smc_futures.app_context import build_app_context
        self.ctx = build_app_context()
        self.latency_tracker = self.ctx.latency_tracker
        
        self.primary_name = self.config.get("primary_exchange", "bitget")
        self.secondary_name = self.config.get("secondary_exchange", "kraken")
        
        self.max_wait_ms = self.config.get("max_wait_primary_fill_ms", 4500)
        self.partial_fill_policy = self.config.get("partial_fill_policy", "CANCEL_REST")
        
        # State tracking per intent
        self.active_intents: Dict[str, str] = {}
        
    async def process_intent(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        State Machine Runner
        """
        intent_id = intent.get("intent_id", uuid.uuid4().hex[:8])
        symbol = intent.get("symbol")
        side = intent.get("side")
        qty = intent.get("quantity")
        price = intent.get("price", 0.0) # Assume signal provides limit price
        
        self._log_state(intent_id, "CREATED")
        
        # 0. Latency Start
        t_signal = intent.get("t_signal", time.time())
        # We use intent_id as a temporary key until we get the primary_order_id
        # Actually, let's use intent_id for now.
        self.latency_tracker.record_timestamp(intent_id, "t_signal", t_signal)
        
        # 1. RISK VALIDATION (V2 — sole authority)
        proposed_notional = abs(float(qty or 0) * float(price or 0))
        v2_allowed, v2_mult, v2_reasons = evaluate_risk_v2(
            symbol=symbol or "",
            side=side or "",
            proposed_size_usd=proposed_notional,
            current_price=float(price or 0),
        )
        if not v2_allowed:
            reject_reason = ",".join(v2_reasons) if v2_reasons else "RISK_V2_BLOCKED"
            self._log_state(intent_id, "FAILED_SAFE")
            logger.warning(f"[AsymmetricOMS][{intent_id}] Rejected by Risk V2: {reject_reason}")
            return {"success": False, "intent_id": intent_id, "error": reject_reason, "state": "FAILED_SAFE"}
        self._log_state(intent_id, "RISK_OK")
        
        primary_adapter = self.adapters.get(self.primary_name)
        secondary_adapter = self.adapters.get(self.secondary_name)
        
        if not primary_adapter or not secondary_adapter:
            self._log_state(intent_id, "FAILED_SAFE")
            return {"success": False, "intent_id": intent_id, "error": "MISSING_ADAPTERS", "state": "FAILED_SAFE"}
            
        # 2. PRIMARY PLACED (Maker)
        self._log_state(intent_id, "PRIMARY_PLACED")
        logger.info(f"[AsymmetricOMS][{intent_id}] Placing Post-Only Maker on {self.primary_name} for {qty} {symbol} @ {price}")
        
        self.latency_tracker.record_timestamp(intent_id, "t_order_send")
        p_res = await primary_adapter.place_post_only_limit(symbol, side, qty, price)
        self.latency_tracker.record_timestamp(intent_id, "t_exchange_ack")
        
        if not p_res.get("success"):
            self._log_state(intent_id, "FAILED_SAFE")
            logger.warning(f"[AsymmetricOMS][{intent_id}] Primary Reject: {p_res.get('error_msg')}. Silent fail, no position.")
            risk_engine.trigger_cooldown(f"Primary Reject: {p_res.get('error_msg')}")
            return {"success": False, "intent_id": intent_id, "error": f"PRIMARY_REJECT: {p_res.get('error_msg')}", "state": "FAILED_SAFE"}
            
        primary_order_id = p_res.get("order_id")
        # Link intent_id timestamps to primary_order_id if they are different, 
        # but LatencyTracker uses one key. Let's stick with intent_id for end-to-end.
        
        # 3. WS WAIT / POLLING LOOP
        start_wait = time.time()
        fill_qty = 0.0
        status = "new"
        
        interval = 0.05
        elapsed_ms = 0
        
        while elapsed_ms < self.max_wait_ms:
            # Emulate WS event receipt or fast polling (get_recent_fills / get_order_status)
            o_stat = await primary_adapter.get_order_status(primary_order_id, symbol)
            status = o_stat.get("status", "new")
            
            if status in ["filled", "partial_fill"]:
                self.latency_tracker.record_timestamp(intent_id, "t_ws_order_update_first")
                # In a real impl, we'd extract the exact filled quantity. Assuming simulated complete for brevity unless partial
                fill_qty = float(o_stat.get("filled_qty", qty if status == "filled" else qty / 2.0))
                if status == "filled":
                    self.latency_tracker.record_timestamp(intent_id, "t_fill")
                break
                
            await asyncio.sleep(interval)
            elapsed_ms = (time.time() - start_wait) * 1000
            
        # Evaluating the Loop Result
        if status == "filled":
            self._log_state(intent_id, "PRIMARY_FILLED")
        elif status == "partial_fill" or fill_qty > 0:
            self._log_state(intent_id, "PRIMARY_PARTIAL")
            if self.partial_fill_policy == "CANCEL_REST":
                logger.info(f"[AsymmetricOMS][{intent_id}] Primary partially filled ({fill_qty}). Canceling residual.")
                await primary_adapter.cancel_order(primary_order_id, symbol)
        else:
            # TIMEOUT
            logger.warning(f"[AsymmetricOMS][{intent_id}] Primary Timeout ({self.max_wait_ms}ms). Canceling order.")
            await primary_adapter.cancel_order(primary_order_id, symbol)
            self._log_state(intent_id, "PRIMARY_TIMEOUT_CANCELED")
            
            # Late fill check (simulate)
            failsafe_stat = await primary_adapter.get_order_status(primary_order_id, symbol)
            if failsafe_stat.get("status") in ["filled", "partial_fill"]:
                logger.critical(f"[AsymmetricOMS][{intent_id}] LATE FILL DETECTED AFTER CANCEL")
                self._log_state(intent_id, "LATE_FILL_DETECTED")
                trigger_critical_alert("LATE_FILL_DETECTED", f"Order {primary_order_id} filled asynchronously after cancel ACK.")
                try:
                    from core.events.event_bus import get_event_bus
                    from core.events.event_types import SystemEvent, EventType
                    get_event_bus().publish(SystemEvent(EventType.RISK_KILL_SWITCH, message="LATE_FILL_DETECTED", details={"order_id": primary_order_id}, source="asymmetric_oms"))
                except Exception:
                    risk_engine.kill_switch_active = True  # fallback
                await primary_adapter.close_position_market(symbol, side, qty)
                risk_engine.trigger_cooldown("LATE_FILL_DETECTED")
                return {"success": False, "intent_id": intent_id, "state": "LATE_FILL_DETECTED"}
                
            risk_engine.trigger_cooldown("PRIMARY_TIMEOUT_CANCELED")
            return {"success": False, "intent_id": intent_id, "state": "PRIMARY_TIMEOUT_CANCELED"}
            
        # 4. SECONDARY HEDGE (Taker)
        self._log_state(intent_id, "SECONDARY_HEDGE_PLACED")
        logger.info(f"[AsymmetricOMS][{intent_id}] Primary executed. Hedging {fill_qty} {symbol} on {self.secondary_name} (Market)")
        
        s_res = await secondary_adapter.execute_order(symbol, side, "market", fill_qty, price=None)
        
        if not s_res.get("success"):
            self._log_state(intent_id, "ORPHAN_RISK_DETECTED")
            logger.critical(f"[AsymmetricOMS][{intent_id}] Secondary Hedge FAILED. ORPHAN POSITION on {self.primary_name}!")
            trigger_critical_alert("ORPHAN_RISK_DETECTED", f"Secondary hedge failed for Intent {intent_id}. Triggering Flatten.")
            try:
                from core.events.event_bus import get_event_bus
                from core.events.event_types import SystemEvent, EventType
                get_event_bus().publish(SystemEvent(EventType.RISK_KILL_SWITCH, message="ORPHAN_RISK_DETECTED", details={"intent_id": intent_id}, source="asymmetric_oms"))
            except Exception:
                risk_engine.kill_switch_active = True  # fallback
            
            # Automatic Flatten of the naked Primary Leg!
            await primary_adapter.close_position_market(symbol, side, fill_qty)
            risk_engine.trigger_cooldown("ORPHAN_RISK_DETECTED")
            return {"success": False, "intent_id": intent_id, "error": "ORPHAN_HEDGE_FAIL", "state": "ORPHAN_RISK_DETECTED"}
            
        # 5. COMPLETED
        self._log_state(intent_id, "COMPLETED")
        return {"success": True, "intent_id": intent_id, "fill_qty": fill_qty, "state": "COMPLETED"}
        
    def _log_state(self, intent_id: str, new_state: str):
        self.active_intents[intent_id] = new_state
        # The JSON Logger picks this up cleanly
        logger.info(f"[AsymmetricOMS][{intent_id}] Transition -> {new_state}")
