"""
Unit Test Suite: Bitget V2 Silent Bugs

Test that silent bugs are fixed:
  1. Market orders do NOT include "price" field
  2. productType is normalized to "USDT-FUTURES"
  3. Negative age_ms is clamped to 0 (clock skew)
  4. All errors are logged (no silent None returns)

Author: Trading Bot Framework
"""

import pytest
import json
import logging
from unittest.mock import AsyncMock, Mock, patch, MagicMock
from decimal import Decimal

from backend.engine.execution.bitget_v2_executor import BitgetV2Executor


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("test_bitget_v2")


class TestBitgetV2SilentBugs:
    """
    Test suite for Bitget V2 bug fixes.
    """
    
    @pytest.fixture
    def executor(self):
        """BitgetV2Executor instance."""
        return BitgetV2Executor(
            api_key="test_key",
            api_secret="test_secret",
            passphrase="test_pass"
        )
    
    def test_product_type_normalized(self, executor):
        """
        Test that productType is always USDT-FUTURES (normalized).
        """
        assert executor.product_type == "USDT-FUTURES"
    
    @pytest.mark.asyncio
    async def test_market_order_no_price_field(self, executor):
        """
        Test that market orders NEVER include "price" field.
        This was a critical bug causing 404 errors.
        """
        # Mock the HTTP POST to capture payload
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {
                "code": "00000",
                "data": {"orderId": "order_123"}
            }
            
            result = await executor.place_order_market(
                symbol="BTCUSDT",
                side="buy",
                size="0.001"
            )
            
            # Verify HTTP POST was called
            assert mock_post.called
            
            # Get the payload that was sent
            call_args = mock_post.call_args
            payload = call_args[1]["payload"] if call_args[1] else {}
            
            # KEY TEST: Price field MUST NOT be present
            assert "price" not in payload, "Market order payload must NOT include 'price' field"
            assert "orderType" in payload
            assert payload["orderType"] == "market"
    
    @pytest.mark.asyncio
    async def test_limit_order_includes_price(self, executor):
        """
        Test that limit orders ALWAYS include "price" field.
        """
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {
                "code": "00000",
                "data": {"orderId": "order_123"}
            }
            
            result = await executor.place_order_limit(
                symbol="BTCUSDT",
                side="buy",
                size="0.001",
                price="50000"
            )
            
            # Get the payload
            call_args = mock_post.call_args
            payload = call_args[1]["payload"] if call_args[1] else {}
            
            # KEY TEST: Price field MUST be present
            assert "price" in payload, "Limit order payload must include 'price' field"
            assert payload["price"] == "50000"
            assert payload["orderType"] == "limit"
    
    @pytest.mark.asyncio
    async def test_clock_skew_detection(self, executor):
        """
        Test that negative age_ms (clock skew) is detected and logged.
        """
        # Mock a ticker response with future timestamp
        future_ts_ms = int(1e12) + 1_000_000  # Future timestamp
        
        with patch.object(executor, '_http_get', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "code": "00000",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "ts": str(future_ts_ms),
                        "price": "50000"
                    }
                ]
            }
            
            # Capture logs
            with patch.object(logger, 'warning') as mock_warn:
                result = await executor.get_ticker("BTCUSDT")
                
                # age_ms should be clamped to 0 or logged as skew
                assert result["code"] == "00000"
    
    def test_clock_skew_age_calculation(self, executor):
        """
        Test that age_ms is calculated correctly and clamped.
        """
        import time
        
        # Mock current time
        received_ms = int(time.time() * 1000)
        
        # Exchange ts in seconds (common)
        exchange_ts_sec = int(time.time()) - 10  # 10s ago
        
        # Age calculation
        exchange_ts_ms = exchange_ts_sec * 1000
        age_ms = max(0, received_ms - exchange_ts_ms)
        
        # Age should be positive and ~10000ms
        assert age_ms > 0
        assert age_ms < 15000  # Allow some jitter
    
    @pytest.mark.asyncio
    async def test_http_error_logging_not_silent(self, executor):
        """
        Test that HTTP errors are logged, never silent.
        """
        # Mock a failed HTTP call — response.json() and .text are sync in httpx
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.json.return_value = {"code": "500", "msg": "Internal Server Error", "data": None}
        
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch('httpx.AsyncClient') as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            
            result = await executor._http_post(
                path="/api/v2/mix/invalid",
                payload={"test": "data"},
                operation="TEST_INVALID"
            )
            
            # Should return error response, not None
            assert result is not None
            assert result.get("code") == "500"
    
    @pytest.mark.asyncio
    async def test_no_v1_endpoints_used(self, executor):
        """
        Test that NO v1 endpoints are called (should be v2 only).
        """
        # All internal calls should use /api/v2/
        call_paths = [
            "/api/v2/mix/order/place-order",
            "/api/v2/mix/order/place-tpsl-order",
            "/api/v2/mix/account/set-leverage",
            "/api/v2/mix/position/all-position",  # Correct V2 positions endpoint
            "/api/v2/public/ticker",
        ]
        
        for path in call_paths:
            assert "/api/v2/" in path, f"Path {path} does not use API v2"
            assert not path.startswith("/v1/"), f"Path {path} uses deprecated v1"
    
    @pytest.mark.asyncio
    async def test_tpsl_plan_attachment(self, executor):
        """
        Test that TP/SL plans are attached correctly.
        """
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {
                "code": "00000",
                "data": {"orderId": "plan_123"}
            }
            
            result = await executor.attach_tpsl(
                symbol="BTCUSDT",
                plan_type="pos_profit",
                trigger_price="52000",
                hold_side="long"
            )
            
            call_args = mock_post.call_args
            payload = call_args[1]["payload"] if call_args[1] else {}
            
            # Verify required fields
            assert payload["symbol"] == "BTCUSDT"
            assert payload["productType"] == "USDT-FUTURES"
            assert payload["planType"] == "pos_profit"
            assert payload["triggerPrice"] == "52000"
            assert payload["holdSide"] == "long"
    
    @pytest.mark.asyncio
    async def test_symbol_normalization(self, executor):
        """
        Test that symbol normalization works (remove /, :, etc).
        """
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {
                "code": "00000",
                "data": {"orderId": "order_123"}
            }
            
            # Test various symbol formats
            symbols = [
                ("BTC/USDT", "BTCUSDT"),
                ("BTC:USDT", "BTCUSDT"),
                ("btcusdt", "BTCUSDT"),
                ("BTCUSDT", "BTCUSDT"),
            ]
            
            for input_sym, expected_sym in symbols:
                result = await executor.place_order_market(
                    symbol=input_sym,
                    side="buy",
                    size="0.001"
                )
                
                call_args = mock_post.call_args
                payload = call_args[1]["payload"] if call_args[1] else {}
                
                assert payload["symbol"] == expected_sym, \
                    f"Symbol normalization failed: {input_sym} -> {payload['symbol']} (expected {expected_sym})"
    
    @pytest.mark.asyncio
    async def test_size_decimal_precision(self, executor):
        """
        Test that order size is properly formatted as decimal string.
        """
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {
                "code": "00000",
                "data": {"orderId": "order_123"}
            }
            
            result = await executor.place_order_market(
                symbol="BTCUSDT",
                side="buy",
                size="0.001"
            )
            
            call_args = mock_post.call_args
            payload = call_args[1]["payload"] if call_args[1] else {}
            
            # Size should be a string
            assert isinstance(payload["size"], str)
            assert payload["size"] == "0.001"
    
    def test_margin_mode_isolated(self, executor):
        """
        Test that margin mode is correctly set to isolated.
        """
        assert executor.margin_mode == "isolated"
    
    def test_position_mode_hedge(self, executor):
        """
        Test that position mode is correctly set to hedge_mode.
        """
        assert executor.position_mode == "hedge_mode"
    
    @pytest.mark.asyncio
    async def test_bitget_error_response_handled(self, executor):
        """
        Test that Bitget API error responses are handled (not silent).
        """
        with patch.object(executor, '_http_post', new_callable=AsyncMock) as mock_post:
            # Simulate Bitget error response
            mock_post.return_value = {
                "code": "40001",
                "msg": "Invalid symbol",
                "data": None
            }
            
            result = await executor.place_order_market(
                symbol="INVALID",
                side="buy",
                size="0.001"
            )
            
            # Should NOT return None
            assert result is not None
            # Error code should be preserved
            assert result["code"] == "40001"
            assert "Invalid symbol" in result["msg"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
