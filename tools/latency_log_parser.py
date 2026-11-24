import json
import argparse
import os
from datetime import datetime
from collections import defaultdict
import numpy as np

def analyze_traces(file_path: str):
    if not os.path.exists(file_path):
        print(f"Error: File {file_path} not found.")
        return

    traces = []
    with open(file_path, 'r') as f:
        for line in f:
            try:
                traces.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not traces:
        print("No traces found in log file.")
        return

    print(f"=== Latency Diagnostic Report ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ===")
    print(f"Total Traces: {len(traces)}")
    print("-" * 50)

    segments = ["decision_to_send", "rest_rtt", "ack_to_ws", "end_to_end", "fill_latency"]
    stats = defaultdict(list)

    for t in traces:
        for s in segments:
            if s in t:
                stats[s].append(t[s])

    for s in segments:
        data = stats[s]
        if not data:
            continue
        
        print(f"{s.upper():<20}")
        print(f"  P50: {np.percentile(data, 50):>8.2f}ms")
        print(f"  P90: {np.percentile(data, 90):>8.2f}ms")
        print(f"  P95: {np.percentile(data, 95):>8.2f}ms")
        print(f"  Max: {np.max(data):>8.2f}ms")
        print()

    # Correlation Hints
    print("=== System Health Hints ===")
    p95_e2e = np.percentile(stats["end_to_end"], 95) if stats["end_to_end"] else 0
    if p95_e2e > 1000:
        print("CRITICAL: P95 End-to-End > 1000ms. Infrastructure drift or heavy exchange lag detected.")
    elif p95_e2e > 500:
        print("WARNING: P95 End-to-End > 500ms. Consider pausing scaling (AMBER state).")
    else:
        print("HEALTHY: Latency within acceptable bounds.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze SMC Latency Traces")
    parser.add_argument("--file", type=str, default="smc_futures/data/logs/latency_traces.jsonl", help="Path to jsonl log file")
    args = parser.parse_args()
    
    analyze_traces(args.file)
