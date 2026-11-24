"""
Tests for BitgetFuturesExecutor methods: get_positions, get_account_balance
Verifies methods exist, have correct signatures, and don't crash on basic calls.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock


class TestExecutorMethodsExist:
    """Test that required methods exist on BitgetFuturesExecutor."""

    def test_executor_has_get_positions(self):
        """get_positions() method must exist."""
        from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
        executor = BitgetFuturesExecutor("key", "secret", "pass")
        assert hasattr(executor, "get_positions"), "Missing get_positions method"
        assert callable(getattr(executor, "get_positions"))

    def test_executor_has_get_open_positions_alias(self):
        """get_open_positions() alias must exist for backward compat."""
        from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
        executor = BitgetFuturesExecutor("key", "secret", "pass")
        assert hasattr(executor, "get_open_positions"), "Missing get_open_positions alias"
        assert callable(getattr(executor, "get_open_positions"))

    def test_executor_has_get_account_balance(self):
        """get_account_balance() method must exist."""
        from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
        executor = BitgetFuturesExecutor("key", "secret", "pass")
        assert hasattr(executor, "get_account_balance"), "Missing get_account_balance method"
        assert callable(getattr(executor, "get_account_balance"))


class TestGetPositionsMocked:
    """Test get_positions with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_get_positions_returns_dict(self):
        """get_positions should return dict with data key."""
        from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
        executor = BitgetFuturesExecutor("key", "secret", "pass")
        
        mock_response = {
            "code": "00000",
            "data": [
                {"symbol": "BTCUSDT", "holdSide": "long", "total": "0.01"}
            ]
        }
        
        with patch.object(executor, "_http_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            result = await executor.get_positions("BTCUSDT")
        
        assert isinstance(result, dict)
        assert "data" in result or "code" in result

    @pytest.mark.asyncio
    async def test_get_positions_no_symbol(self):
        """get_positions without symbol should query all."""
        from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
        executor = BitgetFuturesExecutor("key", "secret", "pass")
        
        mock_response = {"code": "00000", "data": []}
        
        with patch.object(executor, "_http_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            result = await executor.get_positions()
        
        # Verify productType is passed
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert call_args[0][1]["productType"] == "USDT-FUTURES"

    @pytest.mark.asyncio
    async def test_get_open_positions_is_alias(self):
        """get_open_positions should call get_positions."""
        from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
        executor = BitgetFuturesExecutor("key", "secret", "pass")
        
        mock_response = {"code": "00000", "data": []}
        
        with patch.object(executor, "_http_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            result1 = await executor.get_positions("BTCUSDT")
            result2 = await executor.get_open_positions("BTCUSDT")
        
        # Both should work identically
        assert result1 == result2


class TestGetAccountBalanceMocked:
    """Test get_account_balance with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_get_account_balance_parses_response(self):
        """get_account_balance should parse and return available/equity."""
        from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
        executor = BitgetFuturesExecutor("key", "secret", "pass")
        
        mock_response = {
            "code": "00000",
            "data": [
                {
                    "marginCoin": "USDT",
                    "available": "1234.56",
                    "equity": "1500.00",
                    "crossedMaxAvailable": "1234.56"
                }
            ]
        }
        
        with patch.object(executor, "_http_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            result = await executor.get_account_balance("USDT")
        
        assert isinstance(result, dict)
        assert "available" in result
        assert result["available"] == 1234.56
        assert result["equity"] == 1500.00
        assert result["marginCoin"] == "USDT"

    @pytest.mark.asyncio
    async def test_get_account_balance_margin_coin_alias(self):
        """get_account_balance should accept both marginCoin and margin_coin."""
        from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
        executor = BitgetFuturesExecutor("key", "secret", "pass")
        
        mock_response = {"code": "00000", "data": [{"available": "100"}]}
        
        with patch.object(executor, "_http_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            # Both should work
            await executor.get_account_balance(marginCoin="USDT")
            await executor.get_account_balance(margin_coin="USDT")
        
        assert mock_get.call_count == 2

    @pytest.mark.asyncio
    async def test_get_account_balance_handles_error(self):
        """get_account_balance should handle API errors gracefully."""
        from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
        executor = BitgetFuturesExecutor("key", "secret", "pass")
        
        mock_response = {"code": "40001", "msg": "Invalid signature"}
        
        with patch.object(executor, "_http_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            result = await executor.get_account_balance("USDT")
        
        # Should return 0 balance on error, not crash
        assert result["available"] == 0.0
        assert "error" in result


class TestPositionsLogging:
    """Test logging output."""

    @pytest.mark.asyncio
    async def test_positions_logs_count(self, caplog):
        """get_positions should log [POSITIONS] count=..."""
        import logging
        caplog.set_level(logging.INFO)
        
        from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
        executor = BitgetFuturesExecutor("key", "secret", "pass")
        
        mock_response = {
            "code": "00000",
            "data": [{"symbol": "BTCUSDT", "total": "1.0"}, {"symbol": "ETHUSDT", "total": "0.5"}]
        }
        
        with patch.object(executor, "_http_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            await executor.get_positions()
        
        assert any("[POSITIONS]" in record.message and "count=2" in record.message 
                   for record in caplog.records)

    @pytest.mark.asyncio
    async def test_balance_logs_value(self, caplog):
        """get_account_balance should log [BALANCE] available=..."""
        import logging
        caplog.set_level(logging.INFO)
        
        from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
        executor = BitgetFuturesExecutor("key", "secret", "pass")
        
        mock_response = {
            "code": "00000",
            "data": [{"available": "500.25", "equity": "600.00"}]
        }
        
        with patch.object(executor, "_http_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            await executor.get_account_balance("USDT")
        
        assert any("[BALANCE]" in record.message and "available=500.25" in record.message 
                   for record in caplog.records)
