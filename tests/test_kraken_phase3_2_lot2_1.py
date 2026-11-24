"""
Tests for Phase 3.2 - Lot 2.1: Native REST cancel_order on KrakenFuturesClient
═══════════════════════════════════════════════════════════════════════════════
Validates:
- cancel_order by order_id
- cancel_order by cli_ord_id
- cancel_order with both identifiers
- cancelStatus.status == 'cancelled' -> success=True
- cancelStatus.status == 'notFound' -> success=False, status='notFound' (never falsely True)
- cancelStatus.status == 'orderAlreadyFilled' -> success=False, status='orderAlreadyFilled'
- API error / rejection in strict and non-strict mode
- Authentication error propagation
- Network / timeout error handling (zero blind retry)
- Validation of missing order identifiers
- Malformed response handling
- Integration with KrakenFuturesExecutorWrapper.cancel_order
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from backend.exchanges.kraken_futures import KrakenFuturesClient
from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper


class TestLot2_1_KrakenClientCancelOrder:
    """Unit and edge-case tests for KrakenFuturesClient.cancel_order."""

    @pytest.fixture
    def client(self):
        return KrakenFuturesClient(
            api_key="mock_key",
            api_secret="bW9ja19zZWNyZXRfbG9uZ19lbm91Z2hfZm9yX2Jhc2U2NA==",
        )

    # ── 1. CONFIRMED CANCELLATIONS ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_1_cancel_order_by_order_id_confirmed(self, client):
        """Order cancelled by order_id, returning status='cancelled'."""
        mock_payload = {
            "result": "success",
            "cancelStatus": {
                "status": "cancelled",
                "order_id": "c18f042e-7da7-4e62-8b6e-a23b9cf4e678",
            },
            "serverTime": "2026-11-14T18:00:00.000Z",
        }
        client._request = AsyncMock(return_value=mock_payload)

        res = await client.cancel_order(order_id="c18f042e-7da7-4e62-8b6e-a23b9cf4e678")

        client._request.assert_awaited_once_with(
            "POST", "/cancelorder", data={"order_id": "c18f042e-7da7-4e62-8b6e-a23b9cf4e678"}
        )
        assert res["success"] is True
        assert res["status"] == "cancelled"
        assert res["result"] == "success"
        assert res["order_id"] == "c18f042e-7da7-4e62-8b6e-a23b9cf4e678"

    @pytest.mark.asyncio
    async def test_2_cancel_order_by_cli_ord_id_confirmed(self, client):
        """Order cancelled by cli_ord_id, returning status='cancelled'."""
        mock_payload = {
            "result": "success",
            "cancelStatus": {
                "status": "cancelled",
                "cliOrdId": "BOT_SL_PF_XBTUSD_12345",
            },
            "serverTime": "2026-11-14T18:00:00.000Z",
        }
        client._request = AsyncMock(return_value=mock_payload)

        res = await client.cancel_order(cli_ord_id="BOT_SL_PF_XBTUSD_12345")

        client._request.assert_awaited_once_with(
            "POST", "/cancelorder", data={"cliOrdId": "BOT_SL_PF_XBTUSD_12345"}
        )
        assert res["success"] is True
        assert res["status"] == "cancelled"
        assert res["result"] == "success"
        assert res["cli_ord_id"] == "BOT_SL_PF_XBTUSD_12345"

    @pytest.mark.asyncio
    async def test_3_cancel_order_both_identifiers_sent(self, client):
        """Both order_id and cli_ord_id provided are included in request data."""
        mock_payload = {
            "result": "success",
            "cancelStatus": {
                "status": "cancelled",
                "order_id": "oid-999",
                "cliOrdId": "BOT_SL_999",
            },
            "serverTime": "2026-11-14T18:00:00.000Z",
        }
        client._request = AsyncMock(return_value=mock_payload)

        res = await client.cancel_order(order_id="oid-999", cli_ord_id="BOT_SL_999")

        client._request.assert_awaited_once_with(
            "POST", "/cancelorder", data={"order_id": "oid-999", "cliOrdId": "BOT_SL_999"}
        )
        assert res["success"] is True
        assert res["order_id"] == "oid-999"
        assert res["cli_ord_id"] == "BOT_SL_999"

    # ── 2. NOT FOUND & IN-FLIGHT FILLS (NEVER FALSELY REPORTED AS SUCCESS) ─

    @pytest.mark.asyncio
    async def test_4_cancel_order_not_found_returns_false_and_not_found_result(self, client):
        """
        Kraken returns result='success' with cancelStatus.status='notFound'.
        Must NOT be reported as success! Must return success=False and status='notFound'.
        """
        mock_payload = {
            "result": "success",
            "cancelStatus": {
                "status": "notFound",
                "order_id": "oid-unknown",
            },
            "serverTime": "2026-11-14T18:00:00.000Z",
        }
        client._request = AsyncMock(return_value=mock_payload)

        res = await client.cancel_order("oid-unknown")

        assert res["success"] is False
        assert res["status"] == "notFound"
        assert res["result"] == "notFound"
        assert "not found" in res["error"].lower()

    @pytest.mark.asyncio
    async def test_5_cancel_order_already_filled_returns_false_and_order_already_filled(self, client):
        """Order was filled in flight before cancel request processed -> success=False."""
        mock_payload = {
            "result": "success",
            "cancelStatus": {
                "status": "orderAlreadyFilled",
                "order_id": "oid-filled",
            },
            "serverTime": "2026-11-14T18:00:00.000Z",
        }
        client._request = AsyncMock(return_value=mock_payload)

        res = await client.cancel_order("oid-filled")

        assert res["success"] is False
        assert res["status"] == "orderAlreadyFilled"
        assert res["result"] == "orderAlreadyFilled"
        assert "already filled" in res["error"].lower()

    # ── 3. API ERRORS & REJECTIONS ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_6_cancel_order_api_rejection_strict_raises_runtime_error(self, client):
        """API error in strict mode (raise_errors=True) raises RuntimeError."""
        mock_payload = {
            "result": "error",
            "error": "apiLimitExceeded",
            "serverTime": "2026-11-14T18:00:00.000Z",
        }
        client._request = AsyncMock(return_value=mock_payload)

        with pytest.raises(RuntimeError, match="Kraken cancel_order API error: apiLimitExceeded"):
            await client.cancel_order("oid-1", raise_errors=True)

    @pytest.mark.asyncio
    async def test_7_cancel_order_api_rejection_non_strict_returns_error_dict(self, client):
        """API error in non-strict mode returns structured error dict without raising."""
        mock_payload = {
            "result": "error",
            "error": "orderNotFound",
            "serverTime": "2026-11-14T18:00:00.000Z",
        }
        client._request = AsyncMock(return_value=mock_payload)

        res = await client.cancel_order("oid-1", raise_errors=False)

        assert res["success"] is False
        assert res["result"] == "error"
        assert "orderNotFound" in res["error"]

    # ── 4. TIMEOUTS, NETWORK & AUTH EXCEPTIONS ────────────────────────

    @pytest.mark.asyncio
    async def test_8_cancel_order_auth_error_strict_raises(self, client):
        """Authentication / credential failure raises exception in strict mode."""
        client._request = AsyncMock(side_effect=ValueError("API credentials not configured"))

        with pytest.raises(ValueError, match="API credentials not configured"):
            await client.cancel_order("oid-1", raise_errors=True)

    @pytest.mark.asyncio
    async def test_9_cancel_order_timeout_strict_raises_timeout_error(self, client):
        """Network timeout in strict mode raises TimeoutError (caller must NOT blind-retry)."""
        client._request = AsyncMock(side_effect=TimeoutError("Request timed out"))

        with pytest.raises(TimeoutError, match="Request timed out"):
            await client.cancel_order("oid-1", raise_errors=True)

    @pytest.mark.asyncio
    async def test_10_cancel_order_timeout_non_strict_returns_error_dict(self, client):
        """Network timeout in non-strict mode returns structured error dict."""
        client._request = AsyncMock(side_effect=TimeoutError("Request timed out"))

        res = await client.cancel_order("oid-1", raise_errors=False)

        assert res["success"] is False
        assert res["result"] == "error"
        assert "Request timed out" in res["error"]

    # ── 5. MISSING IDENTIFIERS & MALFORMED RESPONSES ──────────────────

    @pytest.mark.asyncio
    async def test_11_cancel_order_missing_both_identifiers_strict_raises_value_error(self, client):
        """Calling cancel_order with neither order_id nor cli_ord_id raises ValueError in strict mode."""
        with pytest.raises(ValueError, match="Either order_id or cli_ord_id must be provided"):
            await client.cancel_order(order_id=None, cli_ord_id=None, raise_errors=True)

        with pytest.raises(ValueError, match="Either order_id or cli_ord_id must be provided"):
            await client.cancel_order(order_id="", cli_ord_id="", raise_errors=True)

    @pytest.mark.asyncio
    async def test_12_cancel_order_missing_both_identifiers_non_strict_returns_error(self, client):
        """Calling cancel_order with empty identifiers returns structured error in non-strict mode."""
        res = await client.cancel_order(order_id="", cli_ord_id="", raise_errors=False)

        assert res["success"] is False
        assert res["status"] == "missing_id"
        assert res["result"] == "error"
        assert "Missing order_id or cli_ord_id" in res["error"]

    @pytest.mark.asyncio
    async def test_13_cancel_order_malformed_response_non_dict_strict_raises(self, client):
        """Response is not a dictionary (e.g. string error page) -> raises TypeError in strict mode."""
        client._request = AsyncMock(return_value="<html>502 Bad Gateway</html>")

        with pytest.raises(TypeError, match="Unexpected Kraken cancel_order response: expected dict"):
            await client.cancel_order("oid-1", raise_errors=True)

    @pytest.mark.asyncio
    async def test_14_cancel_order_missing_cancel_status_strict_raises(self, client):
        """Response is dict with result='success' but missing cancelStatus -> raises ValueError in strict mode."""
        client._request = AsyncMock(return_value={"result": "success"})

        with pytest.raises(ValueError, match="Missing cancelStatus in Kraken cancel_order response"):
            await client.cancel_order("oid-1", raise_errors=True)

    # ── 6. INTEGRATION WITH KrakenFuturesExecutorWrapper ──────────────

    @pytest.mark.asyncio
    async def test_15_wrapper_cancel_order_integration_with_real_client(self):
        """
        Integration test: KrakenFuturesExecutorWrapper.cancel_order() calls
        KrakenFuturesClient.cancel_order() and maps outputs properly without errors.
        """
        client = KrakenFuturesClient(
            api_key="mock_key",
            api_secret="bW9ja19zZWNyZXRfbG9uZ19lbm91Z2hfZm9yX2Jhc2U2NA==",
        )
        wrapper = KrakenFuturesExecutorWrapper(api_key="mock_key", api_secret="mock_secret")
        wrapper._rest_client = client

        # Case A: Successful cancellation
        client._request = AsyncMock(return_value={
            "result": "success",
            "cancelStatus": {"status": "cancelled", "order_id": "SL-101"},
        })
        res_a = await wrapper.cancel_order(order_id="SL-101")
        assert res_a["success"] is True
        assert res_a["code"] == "00000"

        # Case B: notFound - wrapper MUST report success=False, NEVER True!
        client._request = AsyncMock(return_value={
            "result": "success",
            "cancelStatus": {"status": "notFound", "order_id": "SL-999"},
        })
        res_b = await wrapper.cancel_order(order_id="SL-999")
        assert res_b["success"] is False
        assert res_b["code"] == "ERROR"

        # Case C: Network timeout handled cleanly by client returning error dict
        client._request = AsyncMock(side_effect=TimeoutError("Network dropped"))
        res_c = await wrapper.cancel_order(order_id="SL-101")
        assert res_c["success"] is False
        assert res_c["code"] == "ERROR"
        assert "Network dropped" in res_c["msg"]

        # Case D: Network timeout in strict mode propagates exception to wrapper
        with pytest.raises(TimeoutError, match="Network dropped"):
            await client.cancel_order("SL-101", raise_errors=True)
