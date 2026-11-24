"""
Tests for Multi-Broker Order Execution

Tests the MultiOrderExecutor and manual routes with multi-broker support.

Usage:
    pytest tests/test_multi_broker_execute.py -v

Features tested:
  - MultiOrderExecutor with both brokers
  - Symbol mapping for Kraken
  - Partial success scenarios (one broker fails)
  - Skipped broker scenarios
  - Manual execute endpoint with multi-broker
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.services.multi_order_executor import (
    MultiOrderExecutor,
    BrokerOrderResult,
    is_multi_broker_enabled,
    get_default_brokers,
)


# ══════════════════════════════════════════════════════════════════════════════
# MOCK EXECUTORS
# ══════════════════════════════════════════════════════════════════════════════

def create_mock_bitget_executor(success: bool = True, error: str = None):
    """Create a mock Bitget executor."""
    mock = MagicMock()
    
    async def mock_place_order(**kwargs):
        if success:
            return {
                "success": True,
                "order_id": "bitget_order_123",
                "fill_price": kwargs.get("entry_price", 50000),
            }
        else:
            return {
                "success": False,
                "error": error or "Bitget execution failed",
            }
    
    mock.place_order = AsyncMock(side_effect=mock_place_order)
    return mock


def create_mock_kraken_executor(success: bool = True, error: str = None):
    """Create a mock Kraken executor."""
    mock = MagicMock()
    
    async def mock_place_order(**kwargs):
        if success:
            return {
                "success": True,
                "order_id": "kraken_order_456",
                "fill_price": kwargs.get("entry_price", 50000),
            }
        else:
            return {
                "success": False,
                "error": error or "Kraken execution failed",
            }
    
    mock.place_order = AsyncMock(side_effect=mock_place_order)
    
    # Add mapper for symbol translation
    mock._mapper = MagicMock()
    mock._mapper.to_kraken = MagicMock(return_value="PF_XBTUSD")
    
    return mock


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-ORDER EXECUTOR TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestMultiOrderExecutor:
    """Test MultiOrderExecutor class."""
    
    @pytest.fixture
    def executor(self):
        """Create executor with mock brokers."""
        exec = MultiOrderExecutor()
        exec.set_bitget_executor(create_mock_bitget_executor())
        exec.set_kraken_executor(create_mock_kraken_executor())
        return exec
    
    def test_broker_registration(self, executor):
        """Test that brokers are registered correctly."""
        assert executor.is_broker_enabled("bitget")
        assert executor.is_broker_enabled("kraken")
        assert set(executor.get_enabled_brokers()) == {"bitget", "kraken"}
    
    def test_broker_disabled_when_none(self):
        """Test that broker is disabled when executor is None."""
        exec = MultiOrderExecutor()
        assert not exec.is_broker_enabled("bitget")
        assert not exec.is_broker_enabled("kraken")
        assert exec.get_enabled_brokers() == []
    
    @pytest.mark.asyncio
    async def test_execute_both_brokers_success(self, executor):
        """Test successful execution on both brokers."""
        results = await executor.execute_on_brokers(
            symbol="BTCUSDT",
            direction="long",
            entry_price=50000,
            stop_loss=49000,
            take_profit=52000,
            quantity="0.001",
            leverage=10,
            brokers=["bitget", "kraken"],
        )
        
        assert len(results) == 2
        assert results["bitget"].success
        assert results["bitget"].order_id == "bitget_order_123"
        assert results["kraken"].success
        assert results["kraken"].order_id == "kraken_order_456"
    
    @pytest.mark.asyncio
    async def test_execute_bitget_only(self, executor):
        """Test execution on Bitget only."""
        results = await executor.execute_on_brokers(
            symbol="BTCUSDT",
            direction="long",
            entry_price=50000,
            stop_loss=49000,
            take_profit=52000,
            quantity="0.001",
            leverage=10,
            brokers=["bitget"],
        )
        
        assert len(results) == 1
        assert "bitget" in results
        assert results["bitget"].success
    
    @pytest.mark.asyncio
    async def test_execute_partial_failure(self):
        """Test partial failure - one broker succeeds, one fails."""
        exec = MultiOrderExecutor()
        exec.set_bitget_executor(create_mock_bitget_executor(success=True))
        exec.set_kraken_executor(create_mock_kraken_executor(success=False, error="Insufficient funds"))
        
        results = await exec.execute_on_brokers(
            symbol="BTCUSDT",
            direction="long",
            entry_price=50000,
            stop_loss=49000,
            take_profit=52000,
            quantity="0.001",
            leverage=10,
            brokers=["bitget", "kraken"],
        )
        
        assert results["bitget"].success
        assert not results["kraken"].success
        assert "Insufficient funds" in results["kraken"].error
    
    @pytest.mark.asyncio
    async def test_execute_broker_not_enabled(self):
        """Test execution with disabled broker."""
        exec = MultiOrderExecutor()
        exec.set_bitget_executor(create_mock_bitget_executor())
        # Kraken not set
        
        results = await exec.execute_on_brokers(
            symbol="BTCUSDT",
            direction="long",
            entry_price=50000,
            stop_loss=49000,
            take_profit=52000,
            quantity="0.001",
            leverage=10,
            brokers=["bitget", "kraken"],
        )
        
        assert results["bitget"].success
        assert results["kraken"].skipped
        assert "not available" in results["kraken"].skip_reason
    
    @pytest.mark.asyncio
    async def test_execute_symbol_not_supported(self):
        """Test execution when symbol not supported on broker."""
        exec = MultiOrderExecutor()
        exec.set_bitget_executor(create_mock_bitget_executor())
        
        # Mock kraken executor with no mapping for symbol
        mock_kraken = create_mock_kraken_executor()
        mock_kraken._mapper.to_kraken = MagicMock(return_value=None)
        exec.set_kraken_executor(mock_kraken)
        
        # Mock the symbol mapper globally
        with patch.object(exec, 'translate_symbol') as mock_translate:
            mock_translate.side_effect = lambda s, b: s if b == "bitget" else None
            
            results = await exec.execute_on_brokers(
                symbol="SOMEUSDT",
                direction="long",
                entry_price=100,
                stop_loss=90,
                take_profit=110,
                quantity="1",
                leverage=10,
                brokers=["bitget", "kraken"],
            )
        
        assert results["bitget"].success
        assert results["kraken"].skipped
        assert "not supported" in results["kraken"].skip_reason


class TestSymbolMapping:
    """Test symbol translation functionality."""
    
    def test_translate_bitget_symbol(self):
        """Test that Bitget symbols pass through unchanged."""
        exec = MultiOrderExecutor()
        result = exec.translate_symbol("BTCUSDT", "bitget")
        assert result == "BTCUSDT"
    
    def test_translate_kraken_symbol(self):
        """Test translation to Kraken format."""
        exec = MultiOrderExecutor()
        
        # Use real mapper
        result = exec.translate_symbol("BTCUSDT", "kraken")
        assert result == "PF_XBTUSD"
        
        result = exec.translate_symbol("ETHUSDT", "kraken")
        assert result == "PF_ETHUSD"
    
    def test_unknown_broker(self):
        """Test unknown broker returns None."""
        exec = MultiOrderExecutor()
        result = exec.translate_symbol("BTCUSDT", "binance")
        assert result is None


class TestConfigurationFlags:
    """Test configuration flag functions."""
    
    def test_multi_broker_disabled_by_default(self):
        """Test that multi-broker is disabled by default."""
        with patch.dict('os.environ', {}, clear=True):
            assert not is_multi_broker_enabled()
    
    def test_multi_broker_enabled_by_env(self):
        """Test enabling multi-broker via environment."""
        with patch.dict('os.environ', {'MULTI_BROKER_EXECUTION': '1'}):
            assert is_multi_broker_enabled()
    
    def test_default_brokers_single(self):
        """Test default brokers when multi disabled."""
        with patch.dict('os.environ', {}, clear=True):
            brokers = get_default_brokers()
            assert brokers == ["bitget"]
    
    def test_default_brokers_multi(self):
        """Test default brokers when multi enabled AND kraken enabled."""
        with patch.dict('os.environ', {
            'BITGET_ENABLED': 'true',
            'KRAKEN_EXECUTION_ENABLED': 'true'
        }):
            brokers = get_default_brokers()
            assert set(brokers) == {"bitget", "kraken"}
    
    def test_default_brokers_multi_without_kraken(self):
        """Test that without KRAKEN_EXECUTION_ENABLED only returns bitget."""
        with patch.dict('os.environ', {
            'BITGET_ENABLED': 'true',
            'KRAKEN_EXECUTION_ENABLED': 'false'
        }):
            brokers = get_default_brokers()
            assert brokers == ["bitget"]


class TestManualRouteConfigFlags:
    """Test manual route configuration flags (MANUAL_EXECUTE_ALL_BROKERS, KRAKEN_ENABLED)."""
    
    def test_manual_all_brokers_disabled_by_default(self):
        """Test that MANUAL_EXECUTE_ALL_BROKERS is disabled by default."""
        from backend.routes.manual import is_manual_all_brokers_enabled
        with patch.dict('os.environ', {}, clear=True):
            assert not is_manual_all_brokers_enabled()
    
    def test_manual_all_brokers_enabled_by_env(self):
        """Test enabling MANUAL_EXECUTE_ALL_BROKERS via environment."""
        from backend.routes.manual import is_manual_all_brokers_enabled
        with patch.dict('os.environ', {'MANUAL_EXECUTE_ALL_BROKERS': '1'}):
            assert is_manual_all_brokers_enabled()
    
    def test_kraken_enabled_flag(self):
        """Test KRAKEN_ENABLED flag detection."""
        from backend.routes.manual import is_kraken_enabled
        with patch.dict('os.environ', {}, clear=True):
            assert not is_kraken_enabled()
        with patch.dict('os.environ', {'KRAKEN_ENABLED': 'true'}):
            assert is_kraken_enabled()
    
    def test_get_enabled_brokers_manual_flag(self):
        """Test get_enabled_brokers with MANUAL_EXECUTE_ALL_BROKERS."""
        from backend.routes.manual import get_enabled_brokers
        
        # Default: kraken only
        with patch.dict('os.environ', {}, clear=True):
            assert get_enabled_brokers() == ["kraken"]
        
        # MANUAL_EXECUTE_ALL_BROKERS=1 but no MULTI_BROKER_EXECUTION -> kraken only
        with patch.dict('os.environ', {'MANUAL_EXECUTE_ALL_BROKERS': '1', 'KRAKEN_ENABLED': '0'}):
            assert get_enabled_brokers() == ["kraken"]
        
        # MANUAL_EXECUTE_ALL_BROKERS=1 AND MULTI_BROKER_EXECUTION=1 -> both
        with patch.dict('os.environ', {'MANUAL_EXECUTE_ALL_BROKERS': '1', 'MULTI_BROKER_EXECUTION': '1'}):
            brokers = get_enabled_brokers()
            assert set(brokers) == {"kraken", "bitget"}
        
        # MULTI_BROKER_EXECUTION=1 + MANUAL_EXECUTE_ALL_BROKERS=1 -> both
        with patch.dict('os.environ', {'MULTI_BROKER_EXECUTION': '1', 'MANUAL_EXECUTE_ALL_BROKERS': '1'}):
            brokers = get_enabled_brokers()
            assert set(brokers) == {"kraken", "bitget"}


class TestKrakenSymbolMapEnv:
    """Test KRAKEN_SYMBOL_MAP env var loading."""
    
    def test_load_kraken_symbol_map_empty(self):
        """Test loading when env var is not set."""
        from backend.utils.symbol_mapper import load_kraken_symbol_map_from_env
        with patch.dict('os.environ', {}, clear=True):
            result = load_kraken_symbol_map_from_env()
            assert result == {}
    
    def test_load_kraken_symbol_map_valid(self):
        """Test loading valid JSON from env var."""
        from backend.utils.symbol_mapper import load_kraken_symbol_map_from_env
        env_map = '{"BTCUSDT":"PI_XBTUSD","SOLUSDT":"PF_SOLUSD"}'
        with patch.dict('os.environ', {'KRAKEN_SYMBOL_MAP': env_map}):
            result = load_kraken_symbol_map_from_env()
            assert result == {"BTCUSDT": "PI_XBTUSD", "SOLUSDT": "PF_SOLUSD"}
    
    def test_load_kraken_symbol_map_invalid_json(self):
        """Test loading invalid JSON returns empty dict."""
        from backend.utils.symbol_mapper import load_kraken_symbol_map_from_env
        with patch.dict('os.environ', {'KRAKEN_SYMBOL_MAP': 'not valid json'}):
            result = load_kraken_symbol_map_from_env()
            assert result == {}
    
    def test_load_kraken_symbol_map_not_dict(self):
        """Test loading non-dict JSON returns empty dict."""
        from backend.utils.symbol_mapper import load_kraken_symbol_map_from_env
        with patch.dict('os.environ', {'KRAKEN_SYMBOL_MAP': '["BTCUSDT", "ETHUSDT"]'}):
            result = load_kraken_symbol_map_from_env()
            assert result == {}
    
    def test_custom_mapping_overrides_default(self):
        """Test that custom mappings from env override built-in mappings."""
        from backend.utils.symbol_mapper import SymbolMapper
        
        # Custom mapping changes BTCUSDT from PF_XBTUSD to PI_XBTUSD
        custom = {"BTCUSDT": "PI_XBTUSD"}
        mapper = SymbolMapper(custom_mappings=custom)
        
        result = mapper.to_kraken("BTCUSDT")
        assert result == "PI_XBTUSD"  # Overridden
        
        result = mapper.to_kraken("ETHUSDT")
        assert result == "PF_ETHUSD"  # Still default


# ══════════════════════════════════════════════════════════════════════════════
# BROKER ORDER RESULT TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestBrokerOrderResult:
    """Test BrokerOrderResult dataclass."""
    
    def test_successful_result(self):
        """Test successful result creation."""
        result = BrokerOrderResult(
            broker="bitget",
            success=True,
            order_id="12345",
            fill_price=50000,
            quantity=0.001,
        )
        
        assert result.success
        assert result.order_id == "12345"
        assert not result.skipped
    
    def test_failed_result(self):
        """Test failed result creation."""
        result = BrokerOrderResult(
            broker="kraken",
            success=False,
            error="Insufficient margin",
        )
        
        assert not result.success
        assert "Insufficient margin" in result.error
    
    def test_skipped_result(self):
        """Test skipped result creation."""
        result = BrokerOrderResult(
            broker="kraken",
            success=False,
            skipped=True,
            skip_reason="Symbol not supported",
        )
        
        assert not result.success
        assert result.skipped
        assert "not supported" in result.skip_reason
    
    def test_to_dict(self):
        """Test conversion to dictionary."""
        result = BrokerOrderResult(
            broker="bitget",
            success=True,
            order_id="12345",
        )
        
        d = result.to_dict()
        assert d["broker"] == "bitget"
        assert d["success"] is True
        assert d["order_id"] == "12345"
        assert "timestamp" in d


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS (With FastAPI)
# ══════════════════════════════════════════════════════════════════════════════

class TestManualExecuteEndpoint:
    """Test manual execute endpoint with multi-broker support."""
    
    @pytest.fixture
    def mock_dependencies(self):
        """Mock all dependencies for manual routes."""
        with patch('backend.routes.manual._executor') as mock_bitget, \
             patch('backend.routes.manual._kraken_executor') as mock_kraken, \
             patch('backend.routes.manual.get_account_balance') as mock_balance, \
             patch('backend.routes.manual.check_open_position') as mock_position, \
             patch('backend.routes.manual.is_manual_trades_allowed') as mock_allowed:
            
            mock_bitget.return_value = create_mock_bitget_executor()
            mock_kraken.return_value = create_mock_kraken_executor()
            mock_balance.return_value = AsyncMock(return_value=1000.0)
            mock_position.return_value = AsyncMock(return_value=False)
            mock_allowed.return_value = True
            
            yield {
                'bitget': mock_bitget,
                'kraken': mock_kraken,
                'balance': mock_balance,
                'position': mock_position,
                'allowed': mock_allowed,
            }
    
    @pytest.mark.asyncio
    async def test_execute_request_model(self):
        """Test that request model accepts brokers list."""
        from backend.routes.manual import ManualTradeIntent
        
        # With brokers
        intent = ManualTradeIntent(
            symbol="BTCUSDT",
            side="long",
            entry_price=50000,
            stop_loss=49000,
            brokers=["bitget", "kraken"],
        )
        assert intent.brokers == ["bitget", "kraken"]
        
        # Without brokers (default)
        intent2 = ManualTradeIntent(
            symbol="BTCUSDT",
            side="long",
            entry_price=50000,
            stop_loss=49000,
        )
        assert intent2.brokers is None
    
    @pytest.mark.asyncio
    async def test_response_model_multi_broker(self):
        """Test that response model includes multi-broker fields."""
        from backend.routes.manual import ExecuteResponse, BrokerResult
        
        response = ExecuteResponse(
            success=True,
            reason="OK (2 broker(s) succeeded)",
            multi_broker=True,
            broker_results={
                "bitget": BrokerResult(broker="bitget", success=True, order_id="123"),
                "kraken": BrokerResult(broker="kraken", success=True, order_id="456"),
            },
            brokers_success=2,
            brokers_failed=0,
            brokers_skipped=0,
        )
        
        assert response.multi_broker
        assert response.brokers_success == 2
        assert len(response.broker_results) == 2


# ══════════════════════════════════════════════════════════════════════════════
# MIRROR MODE (DUAL EXECUTION) TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestMirrorModeConfig:
    """Test MULTI_BROKER_MIRROR configuration."""
    
    def test_mirror_mode_env_detection(self):
        """Test that MULTI_BROKER_MIRROR is detected from environment."""
        import os
        from backend.services.multi_order_executor import is_multi_broker_mirror_enabled
        
        # Test with MULTI_BROKER_MIRROR=true
        with patch.dict(os.environ, {"MULTI_BROKER_MIRROR": "true"}):
            assert is_multi_broker_mirror_enabled() is True
        
        # Test with MULTI_BROKER_MIRROR=false
        with patch.dict(os.environ, {"MULTI_BROKER_MIRROR": "false"}):
            assert is_multi_broker_mirror_enabled() is False
    
    def test_dual_execution_alias(self):
        """Test that DUAL_EXECUTION is an alias for MULTI_BROKER_MIRROR."""
        import os
        from backend.services.multi_order_executor import is_multi_broker_mirror_enabled
        
        # DUAL_EXECUTION=true should enable mirror mode
        with patch.dict(os.environ, {"DUAL_EXECUTION": "true", "MULTI_BROKER_MIRROR": "false"}):
            assert is_multi_broker_mirror_enabled() is True
    
    def test_get_default_brokers_with_mirror(self):
        """Test get_default_brokers returns both when both enabled."""
        import os
        from backend.services.multi_order_executor import get_default_brokers
        
        with patch.dict(os.environ, {
            "BITGET_ENABLED": "true",
            "KRAKEN_EXECUTION_ENABLED": "true"
        }):
            brokers = get_default_brokers()
            assert "bitget" in brokers
            assert "kraken" in brokers
    
    def test_get_default_brokers_mirror_off(self):
        """Test get_default_brokers returns only bitget when mirror disabled."""
        import os
        from backend.services.multi_order_executor import get_default_brokers
        
        with patch.dict(os.environ, {
            "BITGET_ENABLED": "false",
            "KRAKEN_EXECUTION_ENABLED": "false"
        }, clear=True):
            brokers = get_default_brokers()
            assert brokers == ["bitget"]


# ══════════════════════════════════════════════════════════════════════════════
# RUN TESTS
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
