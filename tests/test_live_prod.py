
import pytest
import sys
import os
import json
import logging
from unittest.mock import MagicMock, AsyncMock, patch

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from smc_futures.smc_config import SMC_CONFIG
from smc_futures.engine.execution.order_manager import OrderManager
from smc_futures.orchestrator import app, ctx
from fastapi.testclient import TestClient

client = TestClient(app)

# ── Fixtures ──────────────────────────────────────────

@pytest.fixture
def clean_config():
    """Reset configuration to defaults before test."""
    original = SMC_CONFIG.copy()
    original_om = ctx.order_manager
    # Snapshot root logger handlers to restore after test
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    yield SMC_CONFIG
    SMC_CONFIG.clear()
    SMC_CONFIG.update(original)
    ctx.order_manager = original_om
    # Restore logging handlers to prevent format-string pollution
    root.handlers = original_handlers

@pytest.fixture
def mock_httpx():
    """Mock httpx.AsyncClient for zero-network testing."""
    with patch("smc_futures.engine.execution.bitget_executor.httpx.AsyncClient") as mock_cls:
        instance = AsyncMock()
        mock_cls.return_value = instance
        instance.__aenter__.return_value = instance
        instance.__aexit__.return_value = None
        instance.post.return_value = MagicMock(status_code=200, json=lambda: {"code":"00000", "data":{"orderId":"prod-123"}})
        instance.get.return_value = MagicMock(status_code=200, json=lambda: {"code":"00000", "data":[]})
        yield instance

@pytest.fixture
def prod_order_manager(clean_config):
    """Setup OrderManager in LIVE mode with enabled Auto-Execute."""
    clean_config["TRADING_MODE"] = "live"
    # New strict keys
    clean_config["AUTO_EXECUTE_ENABLED"] = True
    clean_config["SHADOW_MODE"] = False
    clean_config["AUTO_EXECUTE_MODE"] = "true"
    
    clean_config["MAX_TRADES_PER_DAY"] = 5
    clean_config["RISK_PER_TRADE_PCT"] = 0.05
    om = OrderManager(clean_config)
    ctx.order_manager = om
    return om

# ── Tests ─────────────────────────────────────────────

def test_kill_switch_blocks(prod_order_manager):
    """Verify kill-switch prevents orders."""
    om = prod_order_manager
    om.engage_kill_switch("TEST_PANIC")
    
    with patch("smc_futures.engine.execution.order_manager._evaluate_risk_v2", return_value=(True, 1.0, [])):
        passed, reason, details = om.pre_flight_checks("BTCUSDT", {"direction": "long"}, "sig_1", account_balance=1000, quantity=1.0)
    assert passed is False
    assert "KILL_SWITCH" in reason

def test_invalid_tpsl_rejected(prod_order_manager):
    """PROD requirement: Invalid TPSL must be rejected."""
    om = prod_order_manager
    # LONG where SL > Entry
    bad_long = {"direction": "long", "entry": 50000, "stop_loss": 55000, "take_profit": 60000}
    
    with patch("smc_futures.engine.execution.order_manager._evaluate_risk_v2", return_value=(True, 1.0, [])):
        passed, reason, details = om.pre_flight_checks("BTCUSDT", bad_long, "sig_bad", account_balance=1000, quantity=1.0)
    assert passed is False
    assert reason == "INVALID_TPSL"

def test_shadow_mode_zero_http(clean_config, mock_httpx):
    """PROD requirement: Shadow mode must make 0 HTTP calls via orchestrator."""
    clean_config["TRADING_MODE"] = "live"
    # New strict keys
    clean_config["AUTO_EXECUTE_ENABLED"] = False
    clean_config["SHADOW_MODE"] = True
    clean_config["AUTO_EXECUTE_MODE"] = "shadow"
    
    # Inject real executor with mock client into context
    from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
    exc = BitgetFuturesExecutor("k", "s", "p")
    exc.client = mock_httpx
    exc.auto_execute_mode = "shadow" # Force it just in case config propogation is weird in test env
    
    print(f"DEBUG: Executor mode set to {exc.auto_execute_mode}", file=sys.stderr)
    
    # Mock _get_executor to return our injected instance
    # Note: orchestrator.py imports _get_executor or defines it locally? 
    # Actually orchestrator uses self.executor. We need to patch where it gets it from or set it on orch.
    # In integration test via client, orchestrator is already initialized.
    # We need to inject into ctx.
    
    ctx.executors["futures"] = exc
    
    # Also need to update the global OrderManager because @app uses ctx.order_manager
    om = OrderManager(clean_config)
    ctx.order_manager = om
    
    # Trigger Order via API
    payload = {
        "symbol": "ETHUSDT",
        "side": "buy",
        "posSide": "long", # V2 needs posSide for hedge mode usually
        "quantity": 1.0, 
        "orderType": "market",
        "price": 0
    }
    
    # Make sure we patch _get_executor in orchestrator module if used, 
    # BUT current orchestrator implementation uses `executor = _get_executor()` inside `place_order_endpoint`.
    # Let's check `orchestrator.py` logic again.
    # It calls `executor = _get_executor()`
    # `_get_executor` is likely a helper function in orchestrator.py or imported.
    # Let's assume patching it works if it's in orchestrator scope.
    
    with patch("smc_futures.orchestrator._get_executor", return_value=exc):
        resp = client.post("/api/v1/order", json=payload)
        
        assert resp.status_code == 200
        data = resp.json()
        assert "orderId" in data
        # Orchestrator returns SHADOW prefix if auto_execute=shadow
        assert "SHADOW" in data["orderId"] or "DRYRUN" in data["orderId"]
        
        # Verify 0 HTTP calls
        assert mock_httpx.post.call_count == 0
        assert mock_httpx.get.call_count == 0

def test_api_status_endpoint(prod_order_manager):
    """Verify GET /api/status returns protection details."""
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    
    assert data["service"] == "smc-engine"
    assert "protection_status" in data
    assert "kill_switch" in data["protection_status"]
    assert "config" in data
    assert data["config"]["max_trades_day"] == 5
    assert "cooldown_sec" in data["config"]
if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
