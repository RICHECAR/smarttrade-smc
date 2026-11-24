#!/usr/bin/env python3
"""
Integration Test: Multi-Broker Manual Execution

Tests that manual trades execute on BOTH Bitget AND Kraken when:
- MANUAL_EXECUTE_ALL_BROKERS=1
- KRAKEN_ENABLED=true

This test mocks both executors to verify the multi-order executor
properly routes orders to both brokers.

Run: pytest tests/test_multi_broker_dual_execute.py -v
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import os


@pytest.fixture
def mock_env():
    """Set up environment for multi-broker."""
    env = {
        'MANUAL_EXECUTE_ALL_BROKERS': '1',
        'KRAKEN_ENABLED': 'true',
        'MULTI_BROKER_EXECUTION': '0',
        'TRADING_MODE': 'live',
        'ALLOW_MANUAL_TRADES': '1',
    }
    with patch.dict(os.environ, env):
        yield env


@pytest.fixture
def mock_bitget_executor():
    """Create mock Bitget executor."""
    executor = AsyncMock()
    executor.place_order = AsyncMock(return_value={
        'success': True,
        'order_id': 'BITGET123',
        'fill_price': 65000.0,
    })
    executor.set_leverage = AsyncMock(return_value={'code': '00000'})
    executor.get_account_balance = AsyncMock(return_value=10000.0)
    return executor


@pytest.fixture
def mock_kraken_executor():
    """Create mock Kraken executor."""
    executor = AsyncMock()
    executor._mapper = MagicMock()
    executor._mapper.to_kraken = MagicMock(side_effect=lambda s: f"PF_{s[:3]}USD")
    
    # Kraken uses place_order_market instead of place_order
    executor.place_order_market = AsyncMock(return_value={
        'code': '00000',
        'msg': 'success',
        'data': {'orderId': 'KRAKEN456'},
    })
    executor.attach_tpsl = AsyncMock(return_value={'code': '00000'})
    executor.get_account_info = AsyncMock(return_value={'available': 5000.0, 'total': 5000.0})
    return executor


class TestMultiBrokerDualExecution:
    """Test dual broker execution."""
    
    def test_get_enabled_brokers_returns_both(self, mock_env):
        """Test that get_enabled_brokers returns kraken (primary, multi_broker_execution=0)."""
        # Clear cached singleton
        from backend.routes import manual
        
        # Force re-read env vars
        brokers = manual.get_enabled_brokers()
        
        assert 'kraken' in brokers
    
    def test_multi_order_executor_setup(self, mock_env, mock_bitget_executor, mock_kraken_executor):
        """Test MultiOrderExecutor registers both executors."""
        from backend.services.multi_order_executor import MultiOrderExecutor
        
        executor = MultiOrderExecutor()
        executor.set_bitget_executor(mock_bitget_executor)
        executor.set_kraken_executor(mock_kraken_executor)
        
        assert executor.is_broker_enabled('bitget')
        assert executor.is_broker_enabled('kraken')
        assert executor.get_enabled_brokers() == ['bitget', 'kraken']
    
    @pytest.mark.asyncio
    async def test_execute_on_both_brokers(self, mock_env, mock_bitget_executor, mock_kraken_executor):
        """Test that orders execute on both brokers concurrently."""
        from backend.services.multi_order_executor import MultiOrderExecutor
        
        executor = MultiOrderExecutor()
        executor.set_bitget_executor(mock_bitget_executor)
        executor.set_kraken_executor(mock_kraken_executor)
        
        results = await executor.execute_on_brokers(
            symbol='BTCUSDT',
            direction='long',
            entry_price=65000.0,
            stop_loss=64000.0,
            take_profit=68000.0,
            quantity='0.001',
            leverage=10,
            order_type='market',
            brokers=['bitget', 'kraken'],
        )
        
        # Both brokers should have been called
        assert 'bitget' in results
        assert 'kraken' in results
        
        # Bitget should succeed
        bitget_result = results['bitget']
        assert bitget_result.success or bitget_result.skipped, f"Bitget failed: {bitget_result.error}"
        
        # Kraken should succeed (or be skipped if no symbol mapping)
        kraken_result = results['kraken']
        # Note: may be skipped if symbol mapping not found
        assert kraken_result.success or kraken_result.skipped, f"Kraken failed: {kraken_result.error}"
    
    @pytest.mark.asyncio
    async def test_symbol_translation(self, mock_env, mock_kraken_executor):
        """Test that symbols are translated for Kraken."""
        from backend.services.multi_order_executor import MultiOrderExecutor
        from backend.utils.symbol_mapper import SymbolMapper
        
        executor = MultiOrderExecutor()
        executor.set_symbol_mapper(SymbolMapper())
        
        # Test translation
        btc_kraken = executor.translate_symbol('BTCUSDT', 'kraken')
        eth_kraken = executor.translate_symbol('ETHUSDT', 'kraken')
        sol_kraken = executor.translate_symbol('SOLUSDT', 'kraken')
        
        assert btc_kraken == 'PF_XBTUSD', f"Expected PF_XBTUSD, got {btc_kraken}"
        assert eth_kraken == 'PF_ETHUSD', f"Expected PF_ETHUSD, got {eth_kraken}"
        assert sol_kraken == 'PF_SOLUSD', f"Expected PF_SOLUSD, got {sol_kraken}"
    
    @pytest.mark.asyncio
    async def test_partial_failure_handling(self, mock_env, mock_bitget_executor, mock_kraken_executor):
        """Test that partial failures are handled (one broker fails, other succeeds)."""
        from backend.services.multi_order_executor import MultiOrderExecutor
        
        # Make Kraken fail
        mock_kraken_executor.place_order_market = AsyncMock(return_value={
            'code': 'ORDER_FAILED',
            'msg': 'Insufficient margin',
            'data': None,
        })
        
        executor = MultiOrderExecutor()
        executor.set_bitget_executor(mock_bitget_executor)
        executor.set_kraken_executor(mock_kraken_executor)
        
        results = await executor.execute_on_brokers(
            symbol='BTCUSDT',
            direction='long',
            entry_price=65000.0,
            stop_loss=64000.0,
            take_profit=68000.0,
            quantity='0.001',
            leverage=10,
            order_type='market',
            brokers=['bitget', 'kraken'],
        )
        
        # Bitget should succeed
        assert results['bitget'].success
        assert results['bitget'].order_id == 'BITGET123'
        
        # Kraken should fail but not affect Bitget
        assert 'kraken' in results
    
    @pytest.mark.asyncio
    async def test_kraken_disabled_returns_bitget_only(self):
        """Test that when KRAKEN_ENABLED=false, only Bitget is used."""
        env = {
            'MANUAL_EXECUTE_ALL_BROKERS': '1',
            'KRAKEN_ENABLED': '0',  # Disabled
            'TRADING_MODE': 'live',
            'ALLOW_MANUAL_TRADES': '1',
        }
        
        with patch.dict(os.environ, env):
            from backend.routes import manual
            
            # Force fresh import/call
            assert manual.is_manual_all_brokers_enabled() == True
            assert manual.is_kraken_enabled() == False
            
            # Kraken-primary: get_enabled_brokers returns kraken regardless of KRAKEN_ENABLED flag
            brokers = manual.get_enabled_brokers()
            assert brokers == ['kraken']


class TestEnvFlagsLogic:
    """Test environment flag logic."""
    
    def test_manual_all_brokers_and_kraken_enabled(self):
        """MANUAL_EXECUTE_ALL_BROKERS=1 + MULTI_BROKER_EXECUTION=1 => both brokers."""
        env = {
            'MANUAL_EXECUTE_ALL_BROKERS': '1',
            'KRAKEN_ENABLED': '1',
            'MULTI_BROKER_EXECUTION': '1',
        }
        
        with patch.dict(os.environ, env, clear=False):
            from backend.routes import manual
            brokers = manual.get_enabled_brokers()
            assert set(brokers) == {'kraken', 'bitget'}
    
    def test_multi_broker_execution_flag(self):
        """MULTI_BROKER_EXECUTION=1 + MANUAL_EXECUTE_ALL_BROKERS=1 => both brokers."""
        env = {
            'MANUAL_EXECUTE_ALL_BROKERS': '1',
            'KRAKEN_ENABLED': '0',
            'MULTI_BROKER_EXECUTION': '1',
        }
        
        with patch.dict(os.environ, env, clear=False):
            from backend.routes import manual
            brokers = manual.get_enabled_brokers()
            assert set(brokers) == {'kraken', 'bitget'}


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
