
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
import os
import sys

# Ensure src is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from smc_futures.orchestrator import app
from smc_futures.smc_config import SMC_CONFIG

client = TestClient(app)

@pytest.fixture
def mock_httpx():
    with patch("smc_futures.engine.execution.bitget_executor.httpx.AsyncClient") as mock_client:
        # Setup the mock instance
        instance = AsyncMock()
        mock_client.return_value = instance
        instance.__aenter__.return_value = instance
        instance.__aexit__.return_value = None
        
        # Setup default response for "true" mode tests
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": "00000", 
            "msg": "success", 
            "data": {"orderId": "LIVE-12345"}
        }
        instance.post.return_value = mock_response
        instance.get.return_value = mock_response
        
        yield instance

@pytest.fixture
def override_config():
    """Helper to safely override SMC_CONFIG for a test scope"""
    original = SMC_CONFIG.copy()
    yield SMC_CONFIG
    SMC_CONFIG.clear()
    SMC_CONFIG.update(original)

def test_dry_run_endpoint_zero_calls(mock_httpx):
    """POST /api/v2/order/dry-run MUST NOT call http"""
    payload = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "entry_price": 50000,
        "stop_loss": 49000,
        "take_profit": 52000,
        "risk_pct": 1.0
    }
    response = client.post("/api/v2/order/dry-run", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "simulated"
    
    # VERIFY ZERO CALLS
    assert mock_httpx.post.call_count == 0
    assert mock_httpx.get.call_count == 0

def test_place_order_auto_execute_false(mock_httpx, override_config):
    """Should return 403 when AUTO_EXECUTE is disabled"""
    # Force config
    override_config["AUTO_EXECUTE_ENABLED"] = False
    override_config["SHADOW_MODE"] = False
    
    payload = {
        "symbol": "ETHUSDT",
        "side": "sell",
        "quantity": 1.0, 
        "orderType": "market"
    }
    response = client.post("/api/v1/order", json=payload)
    
    assert response.status_code == 403
    assert response.json()["error"] == "auto_execute_disabled"
    assert mock_httpx.post.call_count == 0

def test_place_order_shadow_mode_zero_calls(mock_httpx, override_config):
    """Should return 200/simulated and MAKE ZERO CALLS when mode='shadow'"""
    override_config["AUTO_EXECUTE_ENABLED"] = False
    override_config["SHADOW_MODE"] = True
    
    payload = {
        "symbol": "SOLUSDT", 
        "side": "buy",
        "quantity": 10.0
    }
    response = client.post("/api/v1/order", json=payload)
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "shadow"
    assert "SHADOW-" in data["orderId"]
    
    # VERIFY ZERO CALLS
    assert mock_httpx.post.call_count == 0

def test_place_order_live_calls_http(mock_httpx, override_config):
    """Should call executor when mode='true'"""
    override_config["AUTO_EXECUTE_ENABLED"] = True
    override_config["SHADOW_MODE"] = False
    
    # Mock the executor retrieval to return an object that uses our mock_httpx
    # We need a mock object that has a .place_order method which calls self.request -> self.client (mock_httpx)
    # Actually, easier: instantiate a real executor with dummy keys, inject mock_httpx into it
    from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
    real_executor = BitgetFuturesExecutor("fake", "fake", "fake")
    real_executor.client = mock_httpx
    
    with patch("smc_futures.orchestrator._get_executor", return_value=real_executor):
        payload = {
            "symbol": "BTCUSDT",
            "side": "buy", 
            "quantity": 0.001
        }
        response = client.post("/api/v1/order", json=payload)
        
        assert response.status_code == 200
        # place_order wraps Bitget response into {"success": True, "order_id": ...}
        assert response.json()["order_id"] == "LIVE-12345"
        
        # VERIFY >=1 CALL (leverage + entry)
        assert mock_httpx.post.call_count >= 1

def test_api_status_exposes_mode(override_config):
    """GET /api/status should return correct auto_execute_mode"""
    override_config["AUTO_EXECUTE_MODE"] = "shadow"
    override_config["AUTO_EXECUTE"] = True
    
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    
    # Check new fields
    assert data["auto_execute"] is True  # shadow implies enabled
    assert data["auto_execute_mode"] == "shadow"

def test_force_trade_shadow_zero_calls(mock_httpx):
    """force-trade with mode=shadow should be zero calls"""
    payload = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "size": 0.1,
        "mode": "shadow"
    }
    response = client.post("/api/test/force-trade", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "shadow"
    assert mock_httpx.post.call_count == 0

def test_force_trade_live_blocked(mock_httpx, override_config):
    """force-trade live should be blocked if FORCE_TRADE_ENABLED=false"""
    override_config["FORCE_TRADE_ENABLED"] = False
    
    payload = {
        "symbol": "BTCUSDT",
        "side": "buy", 
        "size": 0.1,
        "mode": "live"
    }
    response = client.post("/api/test/force-trade", json=payload)
    
    assert response.status_code == 403
    assert response.json()["error"] == "live_disabled"
    assert mock_httpx.post.call_count == 0

if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
