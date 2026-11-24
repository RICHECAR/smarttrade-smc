#!/usr/bin/env python3
"""
Institutional Pre-Live Health Check.
Verifies the production safety invariants before allowing the bot to execute live.
Returns Exit Code 0 if GO, Exit Code >0 if NO-GO.
"""

import urllib.request
import json
import requests
import sys
import time

PROMETHEUS_METRICS_URL = "http://localhost:8000/metrics"
HEALTH_EXECUTION_URL = "http://localhost:8000/health/execution"

def check_prometheus():
    try:
        response = requests.get(PROMETHEUS_METRICS_URL, timeout=2)
        if response.status_code != 200:
            print(f"[FAIL] Prometheus endpoint unavailable (Status {response.status_code})")
            return False
            
        metrics_text = response.text
        
        # Guardrail 1: Orphans
        if "orphan_detected_total 0.0" not in metrics_text and "orphan_detected_total" in metrics_text:
            print("[FAIL] ORPHANS DETECTED IN PROMETHEUS GAUGE! Trading absolutely locked.")
            return False
            
        # Guardrail 2: Late Fills
        if "late_fill_total 0.0" not in metrics_text and "late_fill_total" in metrics_text:
            print("[FAIL] LATE FILLS DETECTED IN PROMETHEUS GAUGE! Trading absolutely locked.")
            return False
            
        return True
    except Exception as e:
        print(f"[FAIL] Could not connect to Prometheus metrics: {e}")
        return False

def check_health_api():
    try:
        response = requests.get(HEALTH_EXECUTION_URL, timeout=2)
        if response.status_code != 200:
            print(f"[FAIL] Execution Health endpoint unavailable (Status {response.status_code})")
            return False
            
        data = response.json()
        
        if data.get("kill_switch_active"):
            print(f"[FAIL] Kill Switch is ACTIVE! Reason: {data.get('last_failure_reason')}")
            return False
            
        exec_mode = data.get("execution_mode")
        if exec_mode != "ASYMMETRIC_MAKER_TAKER":
            print(f"[FAIL] Execution mode must be ASYMMETRIC_MAKER_TAKER. Found: {exec_mode}")
            return False
            
        if data.get("cooldown_active"):
            print(f"[WARNING] Bot is currently in cooldown. Reason: {data.get('last_failure_reason')}")
            # Do we block live on cooldown? Usually yes.
            print("[FAIL] Wait for cooldown to expire before scale limits applied.")
            return False
            
        print("[OK] Execution JSON API is pristine and configured correctly.")
        return True
    except Exception as e:
        print(f"[FAIL] Could not connect to /health/execution: {e}")
        return False

def main():
    print("=== SMC Institutional Pre-Live Checks ===")
    
    prom_ok = check_prometheus()
    api_ok = check_health_api()
    
    if prom_ok and api_ok:
        print("\n\u2705 ALL PRE-FLIGHT CHECKS PASS. GO FOR LIVE/SCALE.")
        sys.exit(0)
    else:
        print("\n\u274c CHECKS FAILED. NO-GO FOR LIVE TRADING.")
        sys.exit(1)

if __name__ == "__main__":
    main()
