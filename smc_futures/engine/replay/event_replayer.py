import asyncio
import time
import logging
from typing import List, Dict, Any

from smc_futures.engine.execution.strict_oms import StrictMirrorOrderManager
from smc_futures.engine.replay.metrics_exporter import CSVMetricsExporter

logger = logging.getLogger("event_replayer")

class AsyncEventReplayer:
    """
    Stress-testing orchestrator. Feeds a high volume of generic intents into the
    exact Production Risk Engine V2 and Strict Mirror OMS to evaluate their mechanical viability.
    """
    def __init__(self, oms: StrictMirrorOrderManager, exporter: CSVMetricsExporter, concurrency: int = 5):
        self.oms = oms
        self.exporter = exporter
        self.concurrency = concurrency
        
    async def process_batch(self, intents: List[Dict[str, Any]]):
        """Run through a list of intents using an asyncio Queue for controlled concurrency."""
        queue = asyncio.Queue()
        for i in intents:
            queue.put_nowait(i)
            
        # Reset the risk engine kill switch if it was accidentally tripped before tests
        # V1 kill_switch reset removed — V2 risk engine is sole authority

        async def worker():
            while True:
                try:
                    intent = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                    
                start_time = time.perf_counter()
                intent_id = intent.get("intent_id", "unknown")
                
                # Update timestamp_ms to pretend this intent JUST arrived (bypass STALE limit for valid intent injections)
                if intent.get("refresh_timestamp", True):
                    intent["timestamp_ms"] = int(time.time() * 1000)
                
                # Feed intent directly into production OMS
                result = await self.oms.process_intent(intent)
                
                end_time = time.perf_counter()
                latency_ms = (end_time - start_time) * 1000
                
                # Check if it was a rollback
                is_rollback = "PARTIAL_FAIL_ROLLED_BACK" in str(result.get("error", ""))
                
                # Record metrics
                self.exporter.add_result(
                    intent_id=result.get("intent_id", intent_id),
                    success=result.get("success", False),
                    error=result.get("error", ""),
                    total_latency_ms=latency_ms,
                    is_rollback=is_rollback,
                    exchange_results=result.get("results", {})
                )
                
                # If a rollback triggers, the Risk Engine permanently locks in production.
                # For stress testing, we must manually unlock it between intents.
                if is_rollback:
                    logger.debug(f"[Replayer] Unlocking Risk Engine after rollback for next mock event.")
                    risk_engine.kill_switch_active = False
                    
                queue.task_done()

        # Spin up concurrent workers
        workers = [asyncio.create_task(worker()) for _ in range(self.concurrency)]
        await asyncio.gather(*workers)
        
        logger.info(f"✅ Event Replay Batch Completed. Processed {len(intents)} intents.")
        self.exporter.export()
