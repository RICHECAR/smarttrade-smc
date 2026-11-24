
import os
import sys
import asyncio
import logging
from datetime import datetime

# Setup paths
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# 1. MOCK ENV BEFORE IMPORTS
os.environ["AUTO_EXECUTE"] = "shadow"
os.environ["TRADING_MODE"] = "paper"
os.environ["BITGET_PRODUCT_TYPE"] = "USDT-FUTURES"
os.environ["SMC_MAX_TRADES_PER_DAY"] = "5"
os.environ["BITGET_API_KEY"] = "mock_key"
os.environ["BITGET_API_SECRET"] = "mock_secret"
os.environ["BITGET_PASSPHRASE"] = "mock_pass"

# 2. Import Modules
from smc_futures.smc_config import SMC_CONFIG, SHADOW_MODE, AUTO_EXECUTE_ENABLED
from smc_futures.engine.execution.order_manager import OrderManager
from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor

import pytest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TEST")

@pytest.mark.asyncio
async def test_auto_execution_logic():
    print("hhChecking Strict Config...")
    assert SMC_CONFIG["AUTO_EXECUTE_MODE"] == "shadow", "Mode must be shadow"
    assert SHADOW_MODE is True, "SHADOW_MODE flag must be True"
    assert AUTO_EXECUTE_ENABLED is False, "AUTO_EXECUTE_ENABLED must be False in shadow"
    assert SMC_CONFIG["BITGET_PRODUCT_TYPE"] == "USDT-FUTURES", "Product Type normalization failed"
    print("✅ Config Strictness Passed")

    print("\n📦 Testing OrderManager Pre-Flight...")
    om = OrderManager(SMC_CONFIG)
    
    # Mock Executor
    executor = BitgetFuturesExecutor("k", "s", "p")
    
    # Fake Signal
    signal = {
        "direction": "long",
        "entry": 50000.0,
        "stop_loss": 49000.0,
        "take_profit": 52000.0,
        "setup": "CHOCE_TEST",
        "quantity": "0.001"
    }
    
    # 1. Test Shadow Execution (Should pass pre-flight and return dry-run ID)
    ok, reason, details = om.pre_flight_checks("BTCUSDT", signal, "sig_001", None, 1000.0)
    assert ok is True, f"Pre-flight failed: {reason}"
    
    print("✅ Pre-Flight Passed. Executing Shadow Order...")
    
    res = await om.execute_signal(executor, "BTCUSDT", signal, "0.001", 10)
    print(f"Result: {res}")
    
    print(f"DEBUG: om.shadow_mode={om.shadow_mode}")
    print(f"DEBUG: om.config={om.config}")
    assert res.get("success") is True, f"Shadow execution failed. Result: {res}"
    assert "DRYRUN" in str(res.get("entry_order_id", "")), f"Order ID mismatch: {res}"
    assert "price_executed" in res, f"Should have price_executed: {res}"
    print("✅ Shadow Execution Passed")

    # 2. Test Cooldown
    import time
    om._last_trade_time["BTCUSDT"] = time.time() # Force recent
    ok, reason, details = om.pre_flight_checks("BTCUSDT", signal, "sig_002", None, 1000.0)
    assert ok is False, "Cooldown should block"
    assert "COOLDOWN" in reason, f"Reason should be COOLDOWN, got {reason}"
    print("✅ Cooldown Check Passed")
    
    # 3. Test Kill Switch
    om.engage_kill_switch("TEST_KILL")
    ok, reason, details = om.pre_flight_checks("ETHUSDT", signal, "sig_003", None, 1000.0)
    assert ok is False, "Kill switch should block"
    assert "KILL_SWITCH" in reason, "Reason should match"
    print("✅ Kill Switch Passed")

if __name__ == "__main__":
    asyncio.run(test_auto_execution_logic())
