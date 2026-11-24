import asyncio
import os
import uuid
import time
from typing import List, Dict, Any

from smc_futures.engine.execution.asymmetric_oms import AsymmetricOrderManager
from smc_futures.engine.replay.fault_mocks import FaultInjectingMockAdapter
from smc_futures.engine.replay.metrics_exporter import CSVMetricsExporter
from smc_futures.engine.replay.event_replayer import AsyncEventReplayer

import logging
logging.getLogger("oms").setLevel(logging.CRITICAL)
logging.getLogger("risk_engine").setLevel(logging.CRITICAL)
logging.getLogger("pager_duty_mock").setLevel(logging.CRITICAL)

async def run_asymmetric_scenario(scenario_name: str, primary_mock: FaultInjectingMockAdapter, secondary_mock: FaultInjectingMockAdapter):
    """
    Executes a quantitative Asymmetric execution scenario.
    """
    print(f"\n--- Starting Asymmetric Profile: {scenario_name} ---")
    
    adapters = {
        "bitget": primary_mock if primary_mock.name == "bitget" else secondary_mock,
        "kraken": secondary_mock if secondary_mock.name == "kraken" else primary_mock
    }
    
    # Instantiate Phase 4 OMS
    oms = AsymmetricOrderManager(adapters, config={
        "primary_exchange": primary_mock.name,
        "secondary_exchange": secondary_mock.name,
        "max_wait_primary_fill_ms": 2000,
        "partial_fill_policy": "CANCEL_REST",
    })
    
    csv_path = f"asym_results_{scenario_name.replace(' ', '_').lower()}.csv"
    exporter = CSVMetricsExporter(output_path=csv_path)
    replayer = AsyncEventReplayer(oms=oms, exporter=exporter, concurrency=5) # Type error might appear on OMS class type, ignoring for replay
    
    num_intents = 20
    intents = []
    for _ in range(num_intents):
        intents.append({
            "intent_id": uuid.uuid4().hex[:8],
            "symbol": "BTCUSDT",
            "side": "buy",
            "quantity": 1.0,
            "spread_bps": 2.0,
            "l1_volume": 100.0,
            "refresh_timestamp": True,
            "execution_mode": "ASYMMETRIC_MAKER_TAKER"
        })
        
    start_time = time.perf_counter()
    await replayer.process_batch(intents)
    end_time = time.perf_counter()
    
    print(f"[{scenario_name}] Complete in {end_time - start_time:.2f} seconds.")
    print(f"Results saved to: {csv_path}\n")

async def main():
    # Scenario 1: Perfect Maker Fills -> Secondary Hedge
    await run_asymmetric_scenario(
        scenario_name="1_Perfect_Asymmetric_Fill",
        primary_mock=FaultInjectingMockAdapter("bitget", base_latency_ms=20, failure_rate_pct=0.0),
        secondary_mock=FaultInjectingMockAdapter("kraken", base_latency_ms=20, failure_rate_pct=0.0)
    )
    
    # Scenario 2: Primary Maker Timeout (No fills, clean cancel)
    await run_asymmetric_scenario(
        scenario_name="2_Primary_Timeout_Safe_Cancel",
        # Force a timeout rate of exactly 1.0 (100% hanging) to prove it safely cancels without orphan
        primary_mock=FaultInjectingMockAdapter("bitget", timeout_rate_pct=1.0),
        secondary_mock=FaultInjectingMockAdapter("kraken", base_latency_ms=20)
    )
    
    # Scenario 3: Partial Fill -> Hedge Partial -> Cancel Rest
    await run_asymmetric_scenario(
        scenario_name="3_Partial_Fill_Hedging",
        primary_mock=FaultInjectingMockAdapter("bitget", partial_fill_rate_pct=1.0),
        secondary_mock=FaultInjectingMockAdapter("kraken", base_latency_ms=20)
    )
    
    # Scenario 4: Late Fill Race (Orphan Creation -> Alert & Auto-Flatten)
    await run_asymmetric_scenario(
        scenario_name="4_Late_Fill_Race_Condition",
        # Force it to time out by just holding status locally as 'new' for long, but then trigering a LATE FILL
        primary_mock=FaultInjectingMockAdapter("bitget", late_fill_rate_pct=0.9, base_latency_ms=2500),
        secondary_mock=FaultInjectingMockAdapter("kraken", base_latency_ms=20)
    )
    
    # Scenario 5: Secondary Reject Triggering Flatten 
    await run_asymmetric_scenario(
        scenario_name="5_Secondary_Reject_Orphan_Defense",
        primary_mock=FaultInjectingMockAdapter("bitget", failure_rate_pct=0.0),
        # 100% Reject on Secondary
        secondary_mock=FaultInjectingMockAdapter("kraken", failure_rate_pct=1.0)
    )

if __name__ == "__main__":
    asyncio.run(main())
