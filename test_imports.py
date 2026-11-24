
import sys
import os

# Ensure current directory is in path
sys.path.append(os.getcwd())

try:
    print("Attempting to import orchestrator...")
    from smc_futures.orchestrator import SMCFuturesOrchestrator
    print("✅ SUCCESS: Orchestrator imported successfully.")
except ImportError as e:
    print(f"❌ FAIL: Import error: {e}")
except Exception as e:
    print(f"❌ FAIL: Other error: {e}")
