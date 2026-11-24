"""
Unit Test Suite: Shadow Mode Zero Network Calls

Test that shadow mode makes ZERO network calls (no HTTP at all).
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from backend.engine.execution.order_manager_v2 import OrderManager


class TestShadowZeroNetwork:
    """Test suite for shadow mode (DRY_RUN): ZERO network calls guaranteed."""
    
    @pytest.fixture
    def config_shadow(self):
        return {
            "SHADOW_MODE": True,
            "MAX_TRADES_PER_DAY": 5,
            "MIN_RR": 1.5,
        }
    
    @pytest.fixture
    def valid_signal(self):
        return {
            "direction": "long",
            "entry": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "rr_ratio": 2.0,
        }
    
    @pytest.mark.asyncio
    async def test_shadow_execute_zero_network(self, config_shadow, valid_signal):
        """Test that shadow mode makes ZERO network calls."""
        om = OrderManager(config_shadow)
        
        with patch('httpx.AsyncClient.post') as mock_post:
            result = await om.execute_signal(
                symbol="BTCUSDT",
                signal_dict=valid_signal,
                signal_id="test_001",
                executor=None,
            )
            
            assert mock_post.call_count == 0
            assert result["success"] is True
            assert result["mode"] == "DRY_RUN"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
