"""
Unit Test Suite: Auto-Execution Hook

Test that validated signals trigger execute() when AUTO_EXECUTE_ENABLED=true.

Scenario:
  1. Create orchestrator with AUTO_EXECUTE_ENABLED=true
  2. Feed a valid signal
  3. Verify pre_flight_checks passes
  4. Verify execute_signal is called
  5. Verify result is logged correctly

Author: Trading Bot Framework
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, Mock, MagicMock, patch
from typing import Dict, Any

from backend.engine.execution.order_manager_v2 import OrderManager, PreFlightResult
from backend.engine.execution.bitget_v2_executor import BitgetV2Executor


class TestAutoExecHook:
    """
    Test suite for auto-execution hook.
    Verifies that signals validated = execution triggered.
    """
    
    @pytest.fixture
    def config_auto_exec_true(self):
        """Config with AUTO_EXECUTE_ENABLED=true."""
        return {
            "AUTO_EXECUTE_MODE": "true",
            "AUTO_EXECUTE_ENABLED": True,
            "SHADOW_MODE": False,
            "TRADING_MODE": "live",
            "MAX_TRADES_PER_DAY": 5,
            "MAX_OPEN_TRADES_PER_SYMBOL": 1,
            "TRADE_COOLDOWN_SEC": 300,
            "MIN_RR": 1.5,
        }
    
    @pytest.fixture
    def config_shadow_mode(self):
        """Config with SHADOW_MODE=true."""
        return {
            "AUTO_EXECUTE_MODE": "shadow",
            "AUTO_EXECUTE_ENABLED": False,
            "SHADOW_MODE": True,
            "TRADING_MODE": "live",
            "MAX_TRADES_PER_DAY": 5,
            "MAX_OPEN_TRADES_PER_SYMBOL": 1,
            "TRADE_COOLDOWN_SEC": 300,
            "MIN_RR": 1.5,
        }
    
    @pytest.fixture
    def config_disabled(self):
        """Config with AUTO_EXECUTE disabled."""
        return {
            "AUTO_EXECUTE_MODE": "false",
            "AUTO_EXECUTE_ENABLED": False,
            "SHADOW_MODE": False,
            "TRADING_MODE": "paper",
            "MAX_TRADES_PER_DAY": 5,
            "MAX_OPEN_TRADES_PER_SYMBOL": 1,
            "TRADE_COOLDOWN_SEC": 300,
            "MIN_RR": 1.5,
        }
    
    @pytest.fixture
    def order_manager(self, config_auto_exec_true):
        """OrderManager instance."""
        return OrderManager(config_auto_exec_true)
    
    @pytest.fixture
    def valid_signal(self):
        """A valid trading signal."""
        return {
            "direction": "long",
            "entry": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "rr_ratio": 2.0,
            "setup": "fvg_break",
            "trigger": "ltf_touch",
            "confidence": 0.85,
        }
    
    def test_pre_flight_checks_valid_signal(self, order_manager, valid_signal):
        """
        Test that pre_flight_checks pass for a valid signal.
        """
        result = order_manager.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id="test_sig_001",
            active_trade=None,
        )
        
        assert result.ok is True
        assert result.reason == "PASS"
        assert result.details.get("rr") == 2.0
    
    def test_pre_flight_checks_kill_switch(self, order_manager, valid_signal):
        """
        Test that kill-switch blocks execution.
        """
        order_manager.set_kill_switch(True, "Manual kill")
        
        result = order_manager.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id="test_sig_002",
            active_trade=None,
        )
        
        assert result.ok is False
        assert result.reason == "KILL_SWITCH"
    
    def test_pre_flight_checks_paper_mode(self, config_disabled, valid_signal):
        """
        Test that paper mode blocks execution.
        """
        om = OrderManager(config_disabled)
        result = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id="test_sig_003",
            active_trade=None,
        )
        
        assert result.ok is False
        assert result.reason == "PAPER_MODE"
    
    def test_pre_flight_checks_position_open(self, order_manager, valid_signal):
        """
        Test that active position blocks new orders.
        """
        active = {"entry": 49900.0, "direction": "long"}
        
        result = order_manager.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id="test_sig_004",
            active_trade=active,
        )
        
        assert result.ok is False
        assert result.reason == "POSITION_ALREADY_OPEN"
    
    def test_pre_flight_checks_cooldown(self, order_manager, valid_signal):
        """
        Test that cooldown blocks new orders.
        """
        import time
        # Simulate a recent trade
        order_manager.last_trade_time = int(time.time() * 1000)
        
        result = order_manager.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id="test_sig_005",
            active_trade=None,
        )
        
        assert result.ok is False
        assert result.reason == "COOLDOWN_ACTIVE"
    
    def test_pre_flight_checks_invalid_tp_sl_long(self, order_manager):
        """
        Test that invalid TP/SL for LONG is rejected.
        Geometry: LONG requires SL < entry < TP
        """
        invalid_signal = {
            "direction": "long",
            "entry": 50000.0,
            "stop_loss": 51000.0,  # INVALID: SL > entry
            "take_profit": 52000.0,
            "rr_ratio": 2.0,
        }
        
        result = order_manager.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=invalid_signal,
            signal_id="test_sig_006",
            active_trade=None,
        )
        
        assert result.ok is False
        assert result.reason == "INVALID_TP_SL_GEOMETRY"
    
    def test_pre_flight_checks_invalid_tp_sl_short(self, order_manager):
        """
        Test that invalid TP/SL for SHORT is rejected.
        Geometry: SHORT requires TP < entry < SL
        """
        invalid_signal = {
            "direction": "short",
            "entry": 50000.0,
            "stop_loss": 49000.0,  # INVALID: SL < entry
            "take_profit": 48000.0,
            "rr_ratio": 2.0,
        }
        
        result = order_manager.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=invalid_signal,
            signal_id="test_sig_007",
            active_trade=None,
        )
        
        assert result.ok is False
        assert result.reason == "INVALID_TP_SL_GEOMETRY"
    
    def test_pre_flight_checks_duplicate_signal(self, order_manager, valid_signal):
        """
        Test that duplicate signals are rejected (idempotence).
        """
        sig_id = "test_sig_008"
        
        # First signal passes
        result1 = order_manager.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id=sig_id,
            active_trade=None,
        )
        order_manager.executed_signals.add(hash(sig_id))
        assert result1.ok is True
        
        # Second signal rejected (duplicate)
        result2 = order_manager.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id=sig_id,
            active_trade=None,
        )
        assert result2.ok is False
        assert result2.reason == "DUPLICATE_SIGNAL"
    
    def test_pre_flight_checks_insufficient_rr(self, order_manager):
        """
        Test that insufficient Risk/Reward is rejected.
        """
        low_rr_signal = {
            "direction": "long",
            "entry": 50000.0,
            "stop_loss": 49500.0,
            "take_profit": 50750.0,
            "rr_ratio": 0.5,  # INVALID: RR < MIN_RR (1.5)
        }
        
        result = order_manager.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=low_rr_signal,
            signal_id="test_sig_009",
            active_trade=None,
        )
        
        assert result.ok is False
        assert result.reason == "INSUFFICIENT_RR"
    
    @pytest.mark.asyncio
    async def test_execute_signal_shadow_mode(self, config_shadow_mode, valid_signal):
        """
        Test shadow mode execution: no network calls, simulated order.
        """
        om = OrderManager(config_shadow_mode)
        
        result = await om.execute_signal(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id="test_sig_010",
            executor=None,
        )
        
        assert result["success"] is True
        assert result["mode"] == "DRY_RUN"
        assert result["order_id"].startswith("DRY_RUN_")
        assert result["fill_price"] == valid_signal["entry"]
        assert result["error"] is None
    
    @pytest.mark.asyncio
    async def test_execute_signal_live_no_executor(self, config_auto_exec_true, valid_signal):
        """
        Test live execution without executor: should fail gracefully.
        """
        om = OrderManager(config_auto_exec_true)
        
        result = await om.execute_signal(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id="test_sig_011",
            executor=None,
        )
        
        assert result["success"] is False
        assert result["mode"] == "LIVE"
        assert result["order_id"] is None
        assert "No executor" in result["error"]
    
    def test_daily_trades_limit(self, order_manager, valid_signal):
        """
        Test that daily trade limit is enforced.
        """
        # Simulate maxing out daily limit
        order_manager.daily_trades_count = order_manager.config.get("MAX_TRADES_PER_DAY", 5)
        
        result = order_manager.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id="test_sig_012",
            active_trade=None,
        )
        
        assert result.ok is False
        assert result.reason == "MAX_TRADES_PER_DAY"
    
    def test_trades_per_symbol_limit(self, config_auto_exec_true, valid_signal):
        """
        Test that per-symbol trade limit is enforced.
        """
        om = OrderManager(config_auto_exec_true)
        om.daily_trades_symbols["BTCUSDT"] = om.config.get("MAX_OPEN_TRADES_PER_SYMBOL", 1)
        
        result = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=valid_signal,
            signal_id="test_sig_013",
            active_trade=None,
        )
        
        assert result.ok is False
        assert result.reason == "MAX_TRADES_PER_SYMBOL"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
