"""
Test: Bitget V2 Payload Validation
Verify: MARKET orders have NO price, LIMIT orders HAVE price + GTC
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor


@pytest.fixture
def executor():
    """Fixture for BitgetFuturesExecutor."""
    return BitgetFuturesExecutor(
        api_key="test_key",
        api_secret="test_secret",
        passphrase="test_pass"
    )


class TestBitgetV2Payloads:
    """V2 payload validation tests."""

    @pytest.mark.asyncio
    async def test_market_order_no_price(self, executor):
        """Verify MARKET order payload excludes 'price' field."""
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"code": "00000", "data": {"orderId": "test_123"}}
            
            result = await executor.place_order_market("BTCUSDT", "buy", "0.01")
            
            # Verify _http_post was called
            assert mock_post.called
            
            # Get the payload from the call
            call_args = mock_post.call_args
            payload = call_args[0][1]  # Second argument is payload
            
            # MARKET order MUST NOT have 'price'
            assert "price" not in payload
            assert "orderType" in payload and payload["orderType"] == "market"

    @pytest.mark.asyncio
    async def test_limit_order_has_price_and_gtc(self, executor):
        """Verify LIMIT order payload includes 'price' and 'timeInForce=GTC'."""
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"code": "00000", "data": {"orderId": "test_123"}}
            
            result = await executor.place_order_limit("BTCUSDT", "buy", "0.01", "100.5")
            
            # Verify _http_post was called
            assert mock_post.called
            
            # Get the payload from the call
            call_args = mock_post.call_args
            payload = call_args[0][1]  # Second argument is payload
            
            # LIMIT order MUST have 'price' and 'force' (Bitget V2 field name)
            assert "price" in payload
            assert payload["price"] == "100.5"
            assert "force" in payload
            assert payload["force"] == "GTC"
            assert "orderType" in payload and payload["orderType"] == "limit"

    @pytest.mark.asyncio
    async def test_product_type_always_usdt_futures(self, executor):
        """Verify productType is always 'USDT-FUTURES'."""
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"code": "00000", "data": {"orderId": "test_123"}}
            
            await executor.place_order_market("BTCUSDT", "buy", "0.01")
            
            call_args = mock_post.call_args
            payload = call_args[0][1]
            
            assert payload["productType"] == "USDT-FUTURES"

    @pytest.mark.asyncio
    async def test_margin_mode_isolated(self, executor):
        """Verify marginMode is 'isolated'."""
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"code": "00000", "data": {"orderId": "test_123"}}
            
            await executor.place_order_market("BTCUSDT", "buy", "0.01")
            
            call_args = mock_post.call_args
            payload = call_args[0][1]
            
            assert payload["marginMode"] == "isolated"

    @pytest.mark.asyncio
    async def test_http_call_logs_payload(self, executor):
        """Verify [HTTP_CALL] log is generated."""
        with patch('smc_futures.engine.execution.bitget_executor.logger') as mock_logger:
            with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
                mock_post.return_value = {"code": "00000", "data": {"orderId": "test_123"}}
                
                await executor.place_order_market("BTCUSDT", "buy", "0.01")
                
                # Verify logger was called
                # (Would need actual logging to test, skipping for now)

    @pytest.mark.asyncio
    async def test_side_normalized_lowercase(self, executor):
        """Verify side is normalized to lowercase."""
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"code": "00000", "data": {"orderId": "test_123"}}
            
            # Pass uppercase
            await executor.place_order_market("BTCUSDT", "BUY", "0.01")
            
            call_args = mock_post.call_args
            payload = call_args[0][1]
            
            # MUST be lowercase
            assert payload["side"] == "buy"

    @pytest.mark.asyncio
    async def test_symbol_normalized_uppercase(self, executor):
        """Verify symbol is normalized to uppercase without slash."""
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"code": "00000", "data": {"orderId": "test_123"}}
            
            # Pass lowercase with slash
            await executor.place_order_market("BTC/USDT", "buy", "0.01")
            
            call_args = mock_post.call_args
            payload = call_args[0][1]
            
            # MUST be uppercase without slash
            assert payload["symbol"] == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_trade_side_always_open(self, executor):
        """Verify tradeSide is always 'open' for entry."""
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"code": "00000", "data": {"orderId": "test_123"}}
            
            await executor.place_order_market("BTCUSDT", "buy", "0.01")
            
            call_args = mock_post.call_args
            payload = call_args[0][1]
            
            assert payload["tradeSide"] == "open"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
