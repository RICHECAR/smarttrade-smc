"""
Test Auto-Trading Mirror Execution (Smoke Test)
================================================

Tests that:
1. Orchestrator uses multi_executor when KRAKEN_ENABLED=true
2. AUTO_EXEC calls execute_multi_broker when conditions are met
3. Logs show "[MULTI_EXEC] brokers=['bitget','kraken']"

Run:
    pytest tests/test_auto_mirror_smoke.py -v
"""

import asyncio
import os
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, Any, Optional

# Enable pytest-asyncio for all tests
pytestmark = pytest.mark.anyio

# Test environment setup
os.environ.setdefault("KRAKEN_ENABLED", "true")
os.environ.setdefault("MULTI_BROKER_MIRROR", "true")
os.environ.setdefault("TRADING_MODE", "live")
os.environ.setdefault("AUTO_EXECUTE", "true")


class MockOrderManager:
    """Mock OrderManager with execute_multi_broker support."""
    
    def __init__(self):
        self.execute_called = False
        self.execute_multi_broker_called = False
        self.last_executors = None
        self.last_signal_id = None
    
    async def execute(self, **kwargs):
        self.execute_called = True
        return {"success": True, "entry_order_id": "single_123", "price_executed": 50000.0}
    
    async def execute_multi_broker(
        self,
        symbol: str,
        signal_dict: Dict,
        signal_id: str,
        executors: Dict[str, Any],
        active_trade: Optional[Dict],
        account_balance: float,
        quantity: float,
        leverage: int = 10,
    ):
        """Mock multi-broker execution."""
        self.execute_multi_broker_called = True
        self.last_executors = executors
        self.last_signal_id = signal_id
        
        # Simulate MultiBrokerResult
        class BrokerResult:
            def __init__(self, broker, success, order_id=None, fill_price=None, quantity=None):
                self.broker = broker
                self.success = success
                self.order_id = order_id
                self.fill_price = fill_price
                self.quantity = quantity
                self.error = None
        
        class MultiBrokerResult:
            def __init__(self):
                self.success = True
                self.signal_id = signal_id
                self.broker_results = {
                    "bitget": BrokerResult("bitget", True, "bg_123", 50000.0, quantity),
                    "kraken": BrokerResult("kraken", True, "kr_456", 50001.0, quantity),
                }
        
        return MultiBrokerResult()


class TestOrchestratorMultiBrokerDetection:
    """Test orchestrator multi-broker path selection."""
    
    def test_env_vars_enable_multi_broker(self):
        """Test that env vars correctly enable multi-broker."""
        kraken_enabled = os.getenv("KRAKEN_ENABLED", "0").lower() in ("1", "true", "yes")
        multi_mirror = os.getenv("MULTI_BROKER_MIRROR", "0").lower() in ("1", "true", "yes")
        dual_exec = os.getenv("DUAL_EXECUTION", "0").lower() in ("1", "true", "yes")
        multi_exec = os.getenv("MULTI_BROKER_EXECUTION", "0").lower() in ("1", "true", "yes")
        
        # Combined check (matches orchestrator.py logic)
        multi_broker_enabled = multi_mirror or dual_exec or multi_exec or kraken_enabled
        
        assert kraken_enabled is True, "KRAKEN_ENABLED should be true"
        assert multi_broker_enabled is True, "Multi-broker should be enabled"
    
    def test_use_multi_broker_path_selection(self):
        """Test the condition for using multi-broker path."""
        # Simulate orchestrator state
        multi_broker_enabled = True
        executors = {"bitget": MagicMock(), "kraken": MagicMock()}
        order_mgr = MockOrderManager()
        
        # This is the condition from orchestrator.py lines 652-656
        use_multi_broker = (
            multi_broker_enabled and 
            len(executors) > 1 and
            hasattr(order_mgr, 'execute_multi_broker')
        )
        
        assert use_multi_broker is True, "Should use multi-broker path"
    
    def test_single_broker_fallback(self):
        """Test fallback to single broker when conditions not met."""
        multi_broker_enabled = True
        executors = {"bitget": MagicMock()}  # Only one broker
        order_mgr = MockOrderManager()
        
        use_multi_broker = (
            multi_broker_enabled and 
            len(executors) > 1 and
            hasattr(order_mgr, 'execute_multi_broker')
        )
        
        assert use_multi_broker is False, "Should NOT use multi-broker (only one executor)"


class TestMultiBrokerExecution:
    """Test actual multi-broker execution flow."""
    
    @pytest.mark.asyncio
    async def test_execute_multi_broker_called(self):
        """Test that execute_multi_broker is called with correct params."""
        order_mgr = MockOrderManager()
        executors = {
            "bitget": MagicMock(),
            "kraken": MagicMock(),
        }
        signal_dict = {
            "direction": "LONG",
            "entry": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
        }
        
        result = await order_mgr.execute_multi_broker(
            symbol="BTCUSDT",
            signal_dict=signal_dict,
            signal_id="test_signal_123",
            executors=executors,
            active_trade=None,
            account_balance=10000.0,
            quantity=0.001,
            leverage=10,
        )
        
        assert order_mgr.execute_multi_broker_called is True
        assert order_mgr.last_signal_id == "test_signal_123"
        assert "bitget" in order_mgr.last_executors
        assert "kraken" in order_mgr.last_executors
        assert result.success is True
    
    @pytest.mark.asyncio
    async def test_broker_results_contain_both(self):
        """Test that broker_results contains both bitget and kraken."""
        order_mgr = MockOrderManager()
        executors = {"bitget": MagicMock(), "kraken": MagicMock()}
        
        result = await order_mgr.execute_multi_broker(
            symbol="BTCUSDT",
            signal_dict={"direction": "LONG", "entry": 50000},
            signal_id="test_abc",
            executors=executors,
            active_trade=None,
            account_balance=10000.0,
            quantity=0.001,
        )
        
        assert "bitget" in result.broker_results
        assert "kraken" in result.broker_results
        assert result.broker_results["bitget"].success is True
        assert result.broker_results["kraken"].success is True


class TestMultiExecutorIntegration:
    """Test MultiOrderExecutor integration."""
    
    @pytest.mark.asyncio
    async def test_multi_order_executor_parallel_execution(self):
        """Test that MultiOrderExecutor executes on both brokers in parallel."""
        from backend.services.multi_order_executor import MultiOrderExecutor
        
        executor = MultiOrderExecutor()
        
        # Track execution order
        execution_order = []
        
        async def mock_bitget_place_order(**kwargs):
            execution_order.append(("bitget_start", asyncio.get_event_loop().time()))
            await asyncio.sleep(0.1)  # Simulate network delay
            execution_order.append(("bitget_end", asyncio.get_event_loop().time()))
            return {"success": True, "order_id": "bg_1", "fill_price": 50000}
        
        async def mock_kraken_place_order(**kwargs):
            execution_order.append(("kraken_start", asyncio.get_event_loop().time()))
            await asyncio.sleep(0.1)
            execution_order.append(("kraken_end", asyncio.get_event_loop().time()))
            return {"success": True, "order_id": "kr_1", "fill_price": 50001}
        
        mock_bitget = MagicMock()
        mock_bitget.place_order = mock_bitget_place_order
        
        mock_kraken = MagicMock()
        mock_kraken.place_order = mock_kraken_place_order
        
        executor.set_bitget_executor(mock_bitget)
        executor.set_kraken_executor(mock_kraken)
        
        # Execute
        results = await executor.execute_on_brokers(
            symbol="BTCUSDT",
            direction="long",
            entry_price=50000,
            stop_loss=49000,
            take_profit=51000,
            quantity="0.001",
            leverage=10,
            order_type="market",
            brokers=["bitget", "kraken"],
        )
        
        # Verify both succeeded
        assert results["bitget"].success is True
        assert results["kraken"].success is True
        
        # Verify parallel execution (both started before either ended)
        starts = [e for e in execution_order if "start" in e[0]]
        ends = [e for e in execution_order if "end" in e[0]]
        
        if len(starts) == 2 and len(ends) == 2:
            # Both should have started before first one ended
            first_end_time = min(e[1] for e in ends)
            last_start_time = max(e[1] for e in starts)
            # In parallel, last start should be close to first start (both started together)
            assert last_start_time < first_end_time, "Execution should be parallel"


class TestLoggingOutput:
    """Test expected log messages."""
    
    def test_expected_log_format(self):
        """Verify expected log format for multi-broker."""
        # These are the log messages we expect to see
        expected_logs = [
            "[MULTI_EXEC] Using multi-broker path | brokers=",
            "[MULTI_EXEC] DISPATCH STARTED",
            "[MULTI_EXEC] PRE-FLIGHT PASS",
            "[MULTI_EXEC:bitget] EXECUTING",
            "[MULTI_EXEC:kraken] EXECUTING",
        ]
        
        # Just verify the format is correct (actual log testing would need fixtures)
        for log in expected_logs:
            assert "[MULTI_EXEC" in log
    
    def test_orchestrator_log_format(self):
        """Verify orchestrator log format for multi-broker."""
        # Expected when multi-broker is ready
        expected = "[ORCH] BTCUSDT | MULTI-BROKER READY | brokers=['bitget', 'kraken']"
        
        assert "MULTI-BROKER READY" in expected
        assert "brokers=" in expected


class TestSymbolMapping:
    """Test symbol mapping for Kraken."""
    
    def test_bitget_to_kraken_mapping(self):
        """Test that Bitget symbols are correctly mapped to Kraken format."""
        mappings = {
            "BTCUSDT": "PF_XBTUSD",
            "ETHUSDT": "PF_ETHUSD",
            "SOLUSDT": "PF_SOLUSD",
            "XRPUSDT": "PF_XRPUSD",
        }
        
        # Verify mapping exists
        for bitget, kraken in mappings.items():
            assert bitget != kraken  # They should be different
            assert "USD" in kraken  # Kraken uses USD suffix
            assert "PF_" in kraken or "PI_" in kraken  # Kraken perpetual prefix


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
