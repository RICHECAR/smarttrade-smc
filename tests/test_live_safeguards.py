"""
Unit Test Suite: Live Safeguards

Test that safeguards work correctly in live mode.
"""

import pytest
from unittest.mock import patch
import time

from backend.engine.execution.order_manager_v2 import OrderManager


class TestLiveSafeguards:
    """Test suite for live mode safeguards."""
    
    @pytest.fixture
    def config_live(self):
        return {
            "AUTO_EXECUTE_ENABLED": True,
            "SHADOW_MODE": False,
            "TRADING_MODE": "live",
            "MAX_TRADES_PER_DAY": 3,
            "TRADE_COOLDOWN_SEC": 60,
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
    
    def test_kill_switch_blocks_orders(self, config_live, valid_signal):
        """Test that kill-switch blocks ALL orders."""
        om = OrderManager(config_live)
        om.set_kill_switch(True, "Emergency stop")
        
        result = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id="test_001",
            active_trade=None,
        )
        
        assert result.ok is False
        assert result.reason == "KILL_SWITCH"
    
    def test_cooldown_blocks_rapid_fire(self, config_live, valid_signal):
        """Test that cooldown blocks rapid orders."""
        om = OrderManager(config_live)
        om.last_trade_time = int(time.time() * 1000)
        
        result = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id="test_002",
            active_trade=None,
        )
        
        assert result.ok is False
        assert result.reason == "COOLDOWN_ACTIVE"
    
    def test_max_trades_enforced(self, config_live, valid_signal):
        """Test that max trades/day is enforced."""
        om = OrderManager(config_live)
        om.daily_trades_count = 3
        
        result = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id="test_003",
            active_trade=None,
        )
        
        assert result.ok is False
        assert result.reason == "MAX_TRADES_PER_DAY"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
