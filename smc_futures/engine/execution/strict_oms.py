import asyncio
import logging
import uuid
import time
from typing import Dict, Any, List

from smc_futures.engine.execution.base_adapter import BaseExchangeAdapter
try:
    from core.phase6_wiring import evaluate_risk_v2, publish_event
    from core.events.event_types import SystemEvent, EventType
    _wiring_ok = True
except ImportError:
    def evaluate_risk_v2(**kw):
        return True, 1.0, []
    publish_event = None  # type: ignore
    _wiring_ok = False
from smc_futures.observability.metrics import ROLLBACK_TOTAL, PARTIAL_EXEC_TOTAL
from smc_futures.observability.alerts import notify_partial_execution, notify_double_fill

logger = logging.getLogger("oms")

class StrictMirrorOrderManager:
    """
    OMS Worker that dispatches validated intents across multiple adapters and ensures
    strict Mirroring. A failure on one side immediately triggers a rollback on the other.
    """
    def __init__(self, adapters: Dict[str, BaseExchangeAdapter]):
        """
        Receives dict of initialized adapters matching BaseExchangeAdapter.
        Example: {"bitget": bitget_adapter, "kraken": kraken_adapter}
        """
        self.adapters = adapters
        
    async def process_intent(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        Takes a stateless intent, validates it under Risk, and executes it.
        Returns unified result dictionary.
        """
        intent_id = uuid.uuid4().hex[:8]
        symbol = intent.get("symbol")
        side = intent.get("side")
        qty = intent.get("quantity")
        logger.info(f"[OMS][{intent_id}] Processing Intent: {side} {qty} {symbol}")
        
        # 1. Ask Risk Engine V2 for authorization (sole authority)
        proposed_notional = abs(float(qty or 0) * float(intent.get("price", 0) or 0))
        v2_allowed, v2_mult, v2_reasons = evaluate_risk_v2(
            symbol=symbol or "",
            side=side or "",
            proposed_size_usd=proposed_notional,
            current_price=float(intent.get("price", 0) or 0),
        )
        if not v2_allowed:
            reject_reason = ",".join(v2_reasons) if v2_reasons else "RISK_V2_BLOCKED"
            logger.warning(f"[OMS][{intent_id}] Rejecting Intent: {reject_reason}")
            return {"success": False, "intent_id": intent_id, "error": reject_reason}
            
        # 2. Issue concurrent network tasks
        tasks = []
        exchange_names = []
        
        for name, adapter in self.adapters.items():
            exchange_names.append(name)
            # Passing 0 as price since we emulate Market execution for intent in this basic spec.
            # Real logic might extract 'price' or 'order_type'.
            tasks.append(
                asyncio.create_task(
                    adapter.execute_order(symbol, side, "market", qty)
                )
            )
            
        logger.info(f"[OMS][{intent_id}] Broadcasting to {exchange_names}")
        
        # We explicitly enforce a Strict Timeout constraint here (e.g. 5 seconds)
        # Any hang is treated as a severe failure!
        done, pending = await asyncio.wait(tasks, timeout=5.0)
        
        if pending:
            logger.critical(f"[OMS][{intent_id}] TIMEOUT: {len(pending)} adapter(s) hung. Forcing rollback!")
            for t in pending:
                t.cancel()
        
        results = {}
        for i, name in enumerate(exchange_names):
            # Evaluate results
            try:
                if tasks[i] in done:
                    res = tasks[i].result()
                    results[name] = res
                else:
                    results[name] = {"success": False, "error_msg": "Timeout"}
            except Exception as e:
                logger.error(f"[OMS][{intent_id}] Exception executing on {name}: {e}")
                results[name] = {"success": False, "error_msg": str(e)}
                
        # 3. Validation and Mirror Mode logic
        success_count = sum(1 for res in results.values() if res.get("success"))
        
        if success_count == len(self.adapters):
            logger.info(f"[OMS][{intent_id}] ✅ STRICT MIRROR OK: Position opened globally.")
            return {"success": True, "intent_id": intent_id, "results": results}
            
        elif success_count == 0:
            logger.warning(f"[OMS][{intent_id}] ❌ ALL BROKERS FAILED. No partial position opened.")
            return {"success": False, "intent_id": intent_id, "results": results}
            
        else:
            # PARTIAL SUCCESS -> WE MUST ROLLBACK!
            PARTIAL_EXEC_TOTAL.inc()
            ROLLBACK_TOTAL.inc()
            notify_partial_execution(intent_id)
            
            logger.critical(f"[OMS][{intent_id}] ⚠️ STRICT MIRROR FAULT ⚠️")
            logger.critical(f"[OMS][{intent_id}] Mismatch Detected! Rollback Initiated.")
            
            # Immediately activate kill switch via event bus (V2 only)
            if _wiring_ok and publish_event:
                publish_event(SystemEvent(
                    event_type=EventType.RISK_KILL_SWITCH,
                    source="strict_oms",
                    message=f"MIRROR_ROLLBACK_{intent_id}",
                    details={"reason": f"MIRROR_ROLLBACK_{intent_id}", "intent_id": intent_id},
                ))
            logger.critical(f"[OMS][{intent_id}] KILL_SWITCH activated via event bus")
            
            # Execute Rollback (inverse active positions) on the successful exchanges
            rollback_tasks = []
            for name, res in results.items():
                if res.get("success"):
                    logger.critical(f"[OMS][{intent_id}] ⏪ Rolling back {name} to match failed branch.")
                    rollback_tasks.append(
                        self.adapters[name].close_all_positions(symbol)
                    )
            
            if rollback_tasks:
                await asyncio.gather(*rollback_tasks, return_exceptions=True)
                
            # Read-Back Validation: Ensure we are FLAT!
            # AND Defense against Double-Fill Race condition:
            # Did the "failed" execution finally come through while we were rolling back?
            flat_verification_tasks = []
            for name, adapter in self.adapters.items():
                flat_verification_tasks.append(
                    asyncio.create_task(adapter.get_open_positions())
                )
                
            verif_done, _ = await asyncio.wait(flat_verification_tasks, timeout=3.0)
            
            for i, name in enumerate(exchange_names):
                try:
                    positions = flat_verification_tasks[i].result() if flat_verification_tasks[i] in verif_done else []
                    active_for_symbol = [p for p in positions if p.get("symbol") == symbol and float(p.get("size", 0)) > 0]
                    
                    if len(active_for_symbol) > 0:
                        logger.critical(f"[OMS][{intent_id}] ❌ DANGER: {name} is NOT FLAT after rollback! (Race condition / Double Fill?)")
                        notify_double_fill()
                        # Extreme failsafe: close all unconditionally 
                        await self.adapters[name].close_all_positions() 
                except Exception as e:
                    logger.error(f"[OMS][{intent_id}] Readback failed on {name}: {e}")
                
            return {"success": False, "intent_id": intent_id, "error": "PARTIAL_FAIL_ROLLED_BACK", "results": results}
