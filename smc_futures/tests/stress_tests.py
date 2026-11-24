import asyncio
import os
import uuid
import time
from typing import List, Dict, Any

from smc_futures.engine.execution.strict_oms import StrictMirrorOrderManager
from smc_futures.engine.replay.fault_mocks import FaultInjectingMockAdapter
from smc_futures.engine.replay.metrics_exporter import CSVMetricsExporter
from smc_futures.engine.replay.event_replayer import AsyncEventReplayer

# Silence the root logger slightly to prevent flooding the terminal during load testing
import logging
logging.getLogger("oms").setLevel(logging.CRITICAL)
logging.getLogger("risk_engine").setLevel(logging.CRITICAL)
logging.getLogger("pager_duty_mock").setLevel(logging.CRITICAL)

async def run_scenario(scenario_name: str, num_intents: int, bitget_mock: FaultInjectingMockAdapter, kraken_mock: FaultInjectingMockAdapter):
    """
    Executes a single quantitative stress-test scenario.
    """
    print(f"\n--- Starting Profile: {scenario_name} ({num_intents} Intents) ---")
    
    adapters = {
        "bitget": bitget_mock,
        "kraken": kraken_mock
    }
    
    # Instantiate Production OMS (wrapped around mocks)
    oms = StrictMirrorOrderManager(adapters)
    
    # Start the CSV exporter
    csv_path = f"stress_results_{scenario_name.replace(' ', '_').lower()}.csv"
    exporter = CSVMetricsExporter(output_path=csv_path)
    
    # Initialize Replayer
    replayer = AsyncEventReplayer(oms=oms, exporter=exporter, concurrency=10)
    
    # Generate Intents Payload
    intents = []
    for _ in range(num_intents):
        intents.append({
            "intent_id": uuid.uuid4().hex[:8],
            "symbol": "BTCUSDT",
            "side": "buy",
            "quantity": 1.0,
            "spread_bps": 2.0, # Valid Risk Limits
            "l1_volume": 100.0,
            "refresh_timestamp": True # Ask orchestrator to bump timestamps to avoid Stale Rejects
        })
        
    start_time = time.perf_counter()
    await replayer.process_batch(intents)
    end_time = time.perf_counter()
    
    print(f"[{scenario_name}] Complete in {end_time - start_time:.2f} seconds.")
    print(f"Results saved to: {csv_path}\n")

async def main():
    
    # Profile 1: Perfect Exchange (Baseline Latency)
    await run_scenario(
        scenario_name="Baseline Ideal Market",
        num_intents=50,
        bitget_mock=FaultInjectingMockAdapter("bitget", base_latency_ms=20, latency_jitter_ms=10, failure_rate_pct=0.0),
        kraken_mock=FaultInjectingMockAdapter("kraken", base_latency_ms=30, latency_jitter_ms=5, failure_rate_pct=0.0)
    )
    
    # Profile 2: High Latency & Jitter (No implicit rejections, just slow network up to ~600ms)
    await run_scenario(
        scenario_name="High Latency Network",
        num_intents=100,
        bitget_mock=FaultInjectingMockAdapter("bitget", base_latency_ms=400, latency_jitter_ms=200, failure_rate_pct=0.0),
        kraken_mock=FaultInjectingMockAdapter("kraken", base_latency_ms=500, latency_jitter_ms=250, failure_rate_pct=0.0)
    )
    
    # Profile 3: The "Toxic Pipeline" (Frequent Rejects + Random 30s Timeouts)
    await run_scenario(
        scenario_name="Toxic Fault Injection",
        num_intents=100,
        # Bitget operates normally
        bitget_mock=FaultInjectingMockAdapter("bitget", base_latency_ms=50, latency_jitter_ms=10, failure_rate_pct=0.01),
        # Kraken suffers 15% rate-limit rejections and 2% permanent connection hangs
        kraken_mock=FaultInjectingMockAdapter("kraken", base_latency_ms=80, latency_jitter_ms=20, failure_rate_pct=0.15, timeout_rate_pct=0.02)
    )

if __name__ == "__main__":
    asyncio.run(main())
