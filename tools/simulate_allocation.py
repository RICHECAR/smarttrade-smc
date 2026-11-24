import pandas as pd
import numpy as np
import argparse
import sys
import os
import time
from datetime import datetime
from typing import List

# Add project root to sys.path
sys.path.append(os.getcwd())

from smc_futures.engine.risk.capital_allocation import (
    AdaptiveCapitalAllocator,
    ScaleReadinessSnapshot,
    AllocationAction
)
from smc_futures.engine.risk.capital_allocation.store import AllocationStateStore

def run_simulation(data_path: str, output_path: str, config: dict):
    """
    Simulation runner for Adaptive Capital Allocation.
    Ingests a CSV with metrics and produces decision logs.
    """
    print(f"--- Starting Phase 5 Capital Allocation Simulation ---")
    
    # Load data
    df = pd.read_csv(data_path)
    required_cols = ['timestamp', 'symbol', 'status', 'stale_rate', 'sigma', 'latency_p95']
    for col in required_cols:
        if col not in df.columns:
            print(f"Error: Missing column {col}")
            return

    # Mock time.time for simulation
    import smc_futures.engine.risk.capital_allocation.allocator as allocator_mod
    import smc_futures.engine.risk.capital_allocation.models as models_mod
    
    # Use a temporary store for simulation
    sim_store = AllocationStateStore(path="smc_futures/data/sim_allocation_state.json")
    allocator = AdaptiveCapitalAllocator(config=config, store=sim_store)
    
    results = []
    
    for idx, row in df.iterrows():
        # Advance mock time
        mock_now = float(row['timestamp'])
        
        # We need to monkeypatch time.time() inside the modules that use it
        # Actually, a better way is to pass 'now' to process_snapshot
        # But let's just patch it for this sim run
        import time as time_real
        allocator_mod.time.time = lambda: mock_now
        models_mod.time.time = lambda: mock_now

        # Create dummy snapshot
        snapshot = ScaleReadinessSnapshot(
            symbol=row['symbol'],
            status=row['status'],
            stale_rate=row['stale_rate'],
            sigma=row['sigma'],
            orphan_total=row.get('orphan_total', 0),
            late_fill_total=row.get('late_fill_total', 0),
            rest_rtt_p95=row['latency_p95'],
            ack_to_ws_p95=0.0,
            end_to_end_p95=0.0
        )
        # Force time for simulation if timestamp is numerical
        if isinstance(row['timestamp'], (int, float)):
            # If timestamp is a small number, assume it's an offset/index
            # and simulate real-time passage by mocking time.time() if needed.
            # For simplicity, we just pass the snapshot.
            pass

        decision = allocator.process_snapshot(snapshot)
        
        results.append({
            "timestamp": row['timestamp'],
            "symbol": row['symbol'],
            "status": row['status'],
            "action": decision.action.value,
            "target_capital": decision.target_capital_eur,
            "multiplier": allocator.get_multiplier(row['symbol']),
            "reason": decision.reason
        })

    # Save results
    res_df = pd.DataFrame(results)
    res_df.to_csv(output_path, index=False)
    print(f"Simulation completed. Results saved to {output_path}")
    
    # Print summary
    print(f"Summary:")
    print(res_df['action'].value_counts())
    print(f"Final Multiplier: {results[-1]['multiplier']:.2f}x")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="tests/data/sample_readiness_history.csv")
    parser.add_argument("--out", default="tests/data/sim_results.csv")
    args = parser.parse_args()
    
    # Ensure sample data exists
    if not os.path.exists(args.data):
        os.makedirs(os.path.dirname(args.data), exist_ok=True)
        # Create dummy data if missing
        dummy = pd.DataFrame({
            "timestamp": range(0, 1500, 10),
            "symbol": ["BTCUSDT"] * 150,
            "status": ["GREEN"] * 100 + ["AMBER"] * 20 + ["RED"] * 5 + ["GREEN"] * 25,
            "stale_rate": [0.01] * 150,
            "sigma": [0.0002] * 150,
            "latency_p95": [45.0] * 150
        })
        dummy.to_csv(args.data, index=False)
        print(f"Created dummy simulation data at {args.data}")

    config = {
        "ALLOC_ENABLED": "true",
        "ALLOC_BASE_CAPITAL_EUR": "1000",
        "ALLOC_MAX_CAPITAL_EUR": "5000",
        "ALLOC_STEP_EUR": "500",
        "ALLOC_GREEN_STREAK_MIN": "10", # Reduced for fast sim
        "ALLOC_MIN_HOLD_SEC": "60"     # Reduced for fast sim
    }
    
    run_simulation(args.data, args.out, config)
