import pytest
import asyncio
import time
from typing import Dict, Any, List
from unittest.mock import patch, MagicMock

from smc_futures.engine.execution.base_adapter import BaseExchangeAdapter
from smc_futures.engine.execution.strict_oms import StrictMirrorOrderManager

class MockBitgetAdapter(BaseExchangeAdapter):
    async def cancel_all_orders(self, symbol: str = None) -> bool: return True
    async def cancel_order(self, order_id: str, symbol: str) -> bool: return True
    async def close_all_positions(self, symbol: str = None) -> bool: return True
    async def close_position_market(self, symbol: str, side: str, qty: float) -> Dict[str, Any]: return {"success": True}
    async def get_open_positions(self) -> List[Dict[str, Any]]: return []
    async def get_order_status(self, order_id: str, symbol: str) -> Dict[str, Any]: return {}
    async def get_recent_fills(self, symbol: str = None, limit: int = 50) -> List[Dict[str, Any]]: return []
    async def place_post_only_limit(self, symbol: str, side: str, qty: float, price: float) -> Dict[str, Any]: return {"success": True}
    async def execute_order(self, symbol: str, side: str, order_type: str, qty: float, price: float = None) -> Dict[str, Any]:
        return {"success": True, "order_id": "bitget_123", "error_msg": ""}
    async def healthcheck(self) -> bool: return True

class MockKrakenAdapterFailing(BaseExchangeAdapter):
    """Simulates a rejected order on Kraken Futures."""
    async def cancel_all_orders(self, symbol: str = None) -> bool: return True
    async def cancel_order(self, order_id: str, symbol: str) -> bool: return True
    async def close_all_positions(self, symbol: str = None) -> bool: return True
    async def close_position_market(self, symbol: str, side: str, qty: float) -> Dict[str, Any]: return {"success": True}
    async def get_open_positions(self) -> List[Dict[str, Any]]: return []
    async def get_order_status(self, order_id: str, symbol: str) -> Dict[str, Any]: return {}
    async def get_recent_fills(self, symbol: str = None, limit: int = 50) -> List[Dict[str, Any]]: return []
    async def place_post_only_limit(self, symbol: str, side: str, qty: float, price: float) -> Dict[str, Any]: return {"success": True}
    async def execute_order(self, symbol: str, side: str, order_type: str, qty: float, price: float = None) -> Dict[str, Any]:
        return {"success": False, "order_id": None, "error_msg": "INSUFFICIENT_MARGIN"}
    async def healthcheck(self) -> bool: return True
    
class MockKrakenAdapterTimeout(BaseExchangeAdapter):
    """Simulates a network timeout hung order on Kraken Futures."""
    async def cancel_all_orders(self, symbol: str = None) -> bool: return True
    async def cancel_order(self, order_id: str, symbol: str) -> bool: return True
    async def close_all_positions(self, symbol: str = None) -> bool: return True
    async def close_position_market(self, symbol: str, side: str, qty: float) -> Dict[str, Any]: return {"success": True}
    async def get_open_positions(self) -> List[Dict[str, Any]]: return []
    async def get_order_status(self, order_id: str, symbol: str) -> Dict[str, Any]: return {}
    async def get_recent_fills(self, symbol: str = None, limit: int = 50) -> List[Dict[str, Any]]: return []
    async def place_post_only_limit(self, symbol: str, side: str, qty: float, price: float) -> Dict[str, Any]: return {"success": True}
    async def execute_order(self, symbol: str, side: str, order_type: str, qty: float, price: float = None) -> Dict[str, Any]:
        await asyncio.sleep(10) # 10 seconds hangs past the OMS 5s limit
        return {"success": True}
    async def healthcheck(self) -> bool: return True


@pytest.fixture(autouse=True)
def mock_risk_v2():
    """Mock evaluate_risk_v2 to always allow, and mock publish_event to capture kill switch."""
    with patch("smc_futures.engine.execution.strict_oms.evaluate_risk_v2", return_value=(True, 1.0, [])), \
         patch("smc_futures.engine.execution.strict_oms.publish_event") as mock_pub, \
         patch("smc_futures.engine.execution.strict_oms._wiring_ok", True):
        yield mock_pub

@pytest.mark.asyncio
async def test_oms_full_success(mock_risk_v2):
    class MockKrakenAdapterSuccess(MockBitgetAdapter): pass
    
    adapters = {
        "bitget": MockBitgetAdapter(),
        "kraken": MockKrakenAdapterSuccess()
    }
    oms = StrictMirrorOrderManager(adapters)
    
    intent = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": 0.1,
        "timestamp_ms": int(time.time() * 1000)
    }
    
    result = await oms.process_intent(intent)
    assert result["success"] is True
    assert "bitget" in result["results"]
    assert "kraken" in result["results"]
    # No kill switch event should be published on full success
    mock_risk_v2.assert_not_called()

@pytest.mark.asyncio
async def test_oms_partial_failure_rollback(mock_risk_v2):
    adapters = {
        "bitget": MockBitgetAdapter(),
        "kraken": MockKrakenAdapterFailing()
    }
    oms = StrictMirrorOrderManager(adapters)
    
    intent = {
        "symbol": "ETHUSDT",
        "side": "sell",
        "quantity": 2.0,
        "timestamp_ms": int(time.time() * 1000)
    }
    
    result = await oms.process_intent(intent)
    assert result["success"] is False
    assert result["error"] == "PARTIAL_FAIL_ROLLED_BACK"
    # Ensure the kill switch event was published via event bus
    mock_risk_v2.assert_called_once()

@pytest.mark.asyncio
async def test_oms_timeout_rollback(mock_risk_v2):
    adapters = {
        "bitget": MockBitgetAdapter(),
        "kraken": MockKrakenAdapterTimeout()
    }
    oms = StrictMirrorOrderManager(adapters)
    
    intent = {
        "symbol": "XRPUSDT",
        "side": "buy",
        "quantity": 1000,
        "timestamp_ms": int(time.time() * 1000)
    }
    
    # Needs to hit the 5s timeout internally and forcefully return failure
    t0 = time.time()
    result = await oms.process_intent(intent)
    t1 = time.time()
    
    assert result["success"] is False
    # Verify the test ran for ~5 seconds (OMS internal limit) instead of 10s.
    assert t1 - t0 < 6.0
    # Kill switch event published via event bus
    mock_risk_v2.assert_called_once()

@pytest.mark.asyncio
async def test_risk_stale_rejection():
    adapters = {"bitget": MockBitgetAdapter()}
    oms = StrictMirrorOrderManager(adapters)
    
    intent = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": 0.1,
        "timestamp_ms": int(time.time() * 1000)
    }
    
    # V2 risk engine rejects with STALE_DATA reason
    with patch("smc_futures.engine.execution.strict_oms.evaluate_risk_v2",
               return_value=(False, 0.0, ["STALE_DATA"])):
        result = await oms.process_intent(intent)
    assert result["success"] is False
    assert "STALE_DATA" in result["error"]
