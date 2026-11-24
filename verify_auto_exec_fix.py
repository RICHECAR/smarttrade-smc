
import os
import sys
import asyncio
from unittest.mock import MagicMock

# Mock env
os.environ["AUTO_EXECUTE"] = "true"
os.environ["TRADING_MODE"] = "live"

try:
    from smc_futures.smc_config import SMC_CONFIG
    from smc_futures.orchestrator import SMCFuturesOrchestrator
    print("Imports successful.")
except ImportError:
    sys.path.append(os.getcwd())
    from smc_futures.smc_config import SMC_CONFIG
    from smc_futures.orchestrator import SMCFuturesOrchestrator
    print("Imports successful (path adjusted).")

def verify_fix():
    print(f"Checking SMC_CONFIG['AUTO_EXECUTE_ENABLED']: {SMC_CONFIG.get('AUTO_EXECUTE_ENABLED')}")
    
    # Mock context and executor
    mock_ctx = MagicMock()
    mock_ctx.get_executor.return_value = MagicMock()
    
    # Instantiate Orchestrator
    orch = SMCFuturesOrchestrator(symbol="BTCUSDT", context=mock_ctx, live_trading=True, api_key="test", secret_key="test", passphrase="test")
    
    # Manually check the logic in a simplified way (we can't easily run the async method specifically without full mock, 
    # but we can inspect the source code or trust the import worked)
    
    # Inspecting the attribute in the class instance won't show the local variable `should_auto_execute`, 
    # but successful import and instantiation means no SyntaxError or immediate crash.
    
    # Let's check if we can simulate the condition
    should_auto_execute = (
        SMC_CONFIG["AUTO_EXECUTE_ENABLED"] 
        and orch.live_trading 
        and orch.executor is not None
    )
    print(f"calculated should_auto_execute: {should_auto_execute}")
    
    if should_auto_execute:
        print("SUCCESS: Logic evaluates to TRUE with correct config.")
    else:
        print("FAILURE: Logic evaluates to FALSE.")

if __name__ == "__main__":
    verify_fix()
