import pytest
import asyncio
import time
from typing import Dict, Any, List

from smc_futures.engine.execution.base_adapter import BaseExchangeAdapter
from smc_futures.engine.risk.global_risk_engine import GlobalRiskEngine
from smc_futures.engine.execution.strict_oms import StrictMirrorOrderManager
from smc_futures.observability.metrics import REJECTS_TOTAL, ROLLBACK_TOTAL
from prometheus_client import REGISTRY

class MockRejectBitget(BaseExchangeAdapter):
    """Simulates a rejected order on Bitget."""
    async def cancel_all_orders(self, symbol: str = None) -> bool: return True
    async def cancel_order(self, order_id: str, symbol: str) -> bool: return True
    async def close_all_positions(self, symbol: str = None) -> bool: return True
    async def close_position_market(self, symbol: str, side: str, qty: float) -> Dict[str, Any]: return {"success": True}
    async def get_open_positions(self) -> List[Dict[str, Any]]: return []
    async def get_order_status(self, order_id: str, symbol: str) -> Dict[str, Any]: return {}
    async def get_recent_fills(self, symbol: str = None, limit: int = 50) -> List[Dict[str, Any]]: return []
    async def place_post_only_limit(self, symbol: str, side: str, qty: float, price: float) -> Dict[str, Any]: return {"success": True}
    async def execute_order(self, symbol: str, side: str, order_type: str, qty: float, price: float = None) -> Dict[str, Any]:
        return {"success": False, "order_id": None, "error_msg": "REJECTED_MOCK"}
    async def healthcheck(self) -> bool: return True

class MockDoubleFillKraken(BaseExchangeAdapter):
    """
    Simultates Kraken succeeding during an active Rollback, leaving the broker
    unflattened (a Double Fill).
    """
    async def cancel_all_orders(self, symbol: str = None) -> bool: return True
    async def cancel_order(self, order_id: str, symbol: str) -> bool: return True
    async def close_all_positions(self, symbol: str = None) -> bool: 
        # Fake rollback success, but position actually remains
        return True
    async def close_position_market(self, symbol: str, side: str, qty: float) -> Dict[str, Any]: return {"success": True}
    
    async def get_open_positions(self) -> List[Dict[str, Any]]: 
        # Returns a mock ACTIVE position simulating a late double-fill during readback
        return [{"symbol": "BTCUSDT", "side": "long", "size": 1.5, "unrealized_pnl": 0.0}]
        
    async def get_order_status(self, order_id: str, symbol: str) -> Dict[str, Any]: return {}
    async def get_recent_fills(self, symbol: str = None, limit: int = 50) -> List[Dict[str, Any]]: return []
    async def place_post_only_limit(self, symbol: str, side: str, qty: float, price: float) -> Dict[str, Any]: return {"success": True}
    async def execute_order(self, symbol: str, side: str, order_type: str, qty: float, price: float = None) -> Dict[str, Any]:
        return {"success": True, "order_id": "krak_123", "error_msg": ""}
    async def healthcheck(self) -> bool: return True


@pytest.fixture
def test_risk_engine():
    # Instantiate a fresh engine bypassing the singleton lock for tests
    engine = GlobalRiskEngine()
    engine.kill_switch_active = False
    engine.max_spread_bps = 5.0
    engine.max_liquidity_consumption_pct = 0.50
    return engine

def test_microstructure_spread_gate(test_risk_engine):
    start_reject_count = REJECTS_TOTAL.labels(reason='SPREAD_TOO_WIDE')._value.get()
    
    # Payload simulating 6bps spread (above 5.0 max)
    intent = {
        "symbol": "ETHUSDT",
        "side": "buy",
        "quantity": 1.0,
        "spread_bps": 6.5,
        "timestamp_ms": int(time.time() * 1000)
    }
    is_valid, reason_str = test_risk_engine.validate_intent(intent)
    
    assert is_valid is False
    assert "SPREAD_TOO_WIDE" in reason_str
    
    end_reject_count = REJECTS_TOTAL.labels(reason='SPREAD_TOO_WIDE')._value.get()
    assert end_reject_count == start_reject_count + 1.0

def test_microstructure_liquidity_gate(test_risk_engine):
    start_reject_count = REJECTS_TOTAL.labels(reason='LOW_LIQUIDITY')._value.get()
    
    # Payload trying to consume 60% of the L1 liquidity (above 50% max)
    intent = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": 6.0,
        "spread_bps": 2.0,
        "l1_volume": 10.0, # Strategy only sees 10 BTC on top of book
        "timestamp_ms": int(time.time() * 1000)
    }
    is_valid, reason_str = test_risk_engine.validate_intent(intent)
    
    assert is_valid is False
    assert "LOW_LIQUIDITY" in reason_str
    
    end_reject_count = REJECTS_TOTAL.labels(reason='LOW_LIQUIDITY')._value.get()
    assert end_reject_count == start_reject_count + 1.0

@pytest.mark.asyncio
async def test_oms_double_fill_race_condition():
    from unittest.mock import patch
    adapters = {
        "bitget": MockRejectBitget(), # Will fail immediately, forcing Kraken to rollback
        "kraken": MockDoubleFillKraken() # Simulates getting 'Filled' and returning active positions during read_back
    }
    oms = StrictMirrorOrderManager(adapters)
    start_rollback_count = ROLLBACK_TOTAL._value.get()
    
    intent = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": 1.5,
        "timestamp_ms": int(time.time() * 1000)
    }
    
    # Mock V2 risk to allow and capture kill switch event
    with patch("smc_futures.engine.execution.strict_oms.evaluate_risk_v2", return_value=(True, 1.0, [])), \
         patch("smc_futures.engine.execution.strict_oms.publish_event") as mock_pub, \
         patch("smc_futures.engine.execution.strict_oms._wiring_ok", True):
        result = await oms.process_intent(intent)
    
    assert result["success"] is False
    assert result["error"] == "PARTIAL_FAIL_ROLLED_BACK"
    
    end_rollback_count = ROLLBACK_TOTAL._value.get()
    # Validates OMS correctly fired the Rollback increment
    assert end_rollback_count == start_rollback_count + 1.0
    # Kill switch event published via event bus
    mock_pub.assert_called_once()
