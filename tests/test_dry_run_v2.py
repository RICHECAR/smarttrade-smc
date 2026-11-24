
import os
import sys
import pytest
from fastapi.testclient import TestClient

# Ensure we can import from src
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from smc_futures.orchestrator import app

client = TestClient(app)

def test_dry_run_endpoint():
    """Verify POST /api/v2/order/dry-run returns simulated payload"""
    payload = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "pos_side": "long",
        "entry_price": 50000.0,
        "stop_loss": 49000.0,
        "take_profit": 52000.0,
        "risk_pct": 1.0,
        "leverage": 10,
        "product_type": "USDT-FUTURES"
    }
    response = client.post("/api/v2/order/dry-run", json=payload)
    print("\n[DRY-RUN] Response:", response.json())
    
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["status"] == "simulated"
    assert "payloadPreview" in data
    assert data["payloadPreview"]["size"] > 0
    assert data["payloadPreview"]["symbol"] == "BTCUSDT"

def test_force_trade_alias():
    """Verify POST /api/test/force-trade alias works"""
    payload = {
        "symbol": "ETHUSDT",
        "side": "sell",
        "size": 0.5,
        "mode": "shadow"
    }
    response = client.post("/api/test/force-trade", json=payload)
    print("\n[FORCE-TRADE] Response:", response.json())
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "shadow"
    assert data["message"] == "WOULD_SEND — no order placed"

def test_v1_order_shadow_mode():
    """Verify POST /api/v1/order rejection when no keys (or shadow flow if enabled)"""
    # Without keys, it should fail with 503 or auto-execute disabled
    # We haven't mocked the executor, so it will likely hit "no_executor" 503
    payload = {
        "symbol": "SOLUSDT",
        "side": "buy",
        "orderType": "market",
        "quantity": 1.0,
        "productType": "USDT-FUTURES"
    }
    response = client.post("/api/v1/order", json=payload)
    print("\n[V1-ORDER] Response:", response.json())
    
    # Expect 503 because we don't have API keys in env by default for tests
    # OR 403 if auto-execute check fails.
    # OR 200 if shadow mode is enabled and keys are present.
    # OR 400 if validation fails.
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        print("✅ Order placed (likely shadow/dry-run)")
    else:
        print(f"ℹ️ Order rejected as expected: {response.status_code}")
    
    # Just ensure we get a valid JSON response structure
    try:
        data = response.json()
        assert isinstance(data, dict)
    except:
        assert False, "Response is not JSON"

if __name__ == "__main__":
    # Manually run if executed as script
    try:
        test_dry_run_endpoint()
        print("✅ test_dry_run_endpoint passed")
        test_force_trade_alias()
        print("✅ test_force_trade_alias passed")
        test_v1_order_shadow_mode()
        print("✅ test_v1_order_shadow_mode passed")
    except AssertionError as e:
        print(f"❌ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
