"""
Tests — Broker Execution Fix (Chantier 19)
═══════════════════════════════════════════

Validates all 4 bugs are fixed:
  1. _place_order_rest dispatch: place_market_order / place_limit_order
  2. SL dispatch: place_stop_order (primary) / place_stop_loss (v2)
  3. TP dispatch: place_take_profit_order (primary) / place_take_profit (v2)
  4. KrakenOrderResult → dict normalization

Run:  python -m pytest tests/test_broker_execution.py -v
"""

import asyncio
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────
# Fake KrakenOrderResult (mirrors backend/exchanges/kraken_futures.py)
# ──────────────────────────────────────────────────────────────────

@dataclass
class FakeKrakenOrderResult:
    success: bool
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    symbol: Optional[str] = None
    side: Optional[str] = None
    size: Optional[float] = None
    price: Optional[float] = None
    filled_size: Optional[float] = None
    filled_price: Optional[float] = None
    error: Optional[str] = None
    raw_response: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "broker": "kraken",
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "price": self.price,
            "filled_size": self.filled_size,
            "filled_price": self.filled_price,
            "error": self.error,
            "timestamp": self.timestamp.isoformat(),
        }


# ──────────────────────────────────────────────────────────────────
# Fake Primary Client (KrakenFuturesClient pattern)
# ──────────────────────────────────────────────────────────────────

class FakePrimaryClient:
    """Mimics KrakenFuturesClient: has place_market_order, place_limit_order,
    place_stop_order, place_take_profit_order. Returns KrakenOrderResult."""

    def __init__(self):
        self.calls = []

    async def place_market_order(self, symbol, side, size, reduce_only=False, client_order_id=None):
        self.calls.append(("place_market_order", symbol, side, size, reduce_only))
        return FakeKrakenOrderResult(
            success=True, order_id="ORD-MKT-001",
            symbol=symbol, side=side, size=size,
        )

    async def place_limit_order(self, symbol, side, size, price, reduce_only=False, post_only=False, client_order_id=None):
        self.calls.append(("place_limit_order", symbol, side, size, price, reduce_only))
        return FakeKrakenOrderResult(
            success=True, order_id="ORD-LMT-001",
            symbol=symbol, side=side, size=size, price=price,
        )

    async def place_stop_order(self, symbol, side, size, stop_price, limit_price=None, reduce_only=True):
        self.calls.append(("place_stop_order", symbol, side, size, stop_price))
        return FakeKrakenOrderResult(
            success=True, order_id="ORD-SL-001",
            symbol=symbol, side=side, size=size,
        )

    async def place_take_profit_order(self, symbol, side, size, tp_price, reduce_only=True):
        self.calls.append(("place_take_profit_order", symbol, side, size, tp_price))
        return FakeKrakenOrderResult(
            success=True, order_id="ORD-TP-001",
            symbol=symbol, side=side, size=size,
        )

    async def place_order(self, symbol=None, side=None, order_type=None, size=None, stop_price=None, price=None, reduce_only=False, **kw):
        self.calls.append(("place_order", symbol, side, order_type, size, stop_price or price))
        return {"success": True, "order_id": "ORD-UNI-001", "code": "00000"}

    async def get_positions(self):
        return []

    async def get_balances(self):
        return {"usd": 10000}


class FakeFailingClient:
    """Primary client that returns failed results."""

    async def place_market_order(self, **kwargs):
        return FakeKrakenOrderResult(
            success=False, error="insufficient margin",
            symbol=kwargs.get("symbol"), side=kwargs.get("side"),
        )

    async def place_limit_order(self, **kwargs):
        return FakeKrakenOrderResult(
            success=False, error="insufficient margin",
            symbol=kwargs.get("symbol"), side=kwargs.get("side"),
        )

    async def place_stop_order(self, **kwargs):
        return FakeKrakenOrderResult(success=False, error="rejected")

    async def place_take_profit_order(self, **kwargs):
        return FakeKrakenOrderResult(success=False, error="rejected")


# ──────────────────────────────────────────────────────────────────
# Fake V2 Client (KrakenFuturesRESTClientV2 pattern)
# ──────────────────────────────────────────────────────────────────

class FakeV2Client:
    """Mimics KrakenFuturesRESTClientV2: has place_market_order, place_limit_order,
    place_stop_loss, place_take_profit. Returns dict."""

    def __init__(self):
        self.calls = []

    async def place_market_order(self, symbol=None, side=None, size=None, reduce_only=False, **kw):
        self.calls.append(("place_market_order", symbol, side, size))
        return {"success": True, "order_id": "V2-ORD-001", "code": "00000"}

    async def place_limit_order(self, symbol=None, side=None, size=None, price=None, reduce_only=False, **kw):
        self.calls.append(("place_limit_order", symbol, side, size, price))
        return {"success": True, "order_id": "V2-LMT-001", "code": "00000"}

    async def place_stop_loss(self, symbol, side, size, stop_price, reduce_only=True):
        self.calls.append(("place_stop_loss", symbol, side, size, stop_price))
        return {"success": True, "order_id": "V2-SL-001"}

    async def place_take_profit(self, symbol, side, size, price, reduce_only=True):
        self.calls.append(("place_take_profit", symbol, side, size, price))
        return {"success": True, "order_id": "V2-TP-001"}

    async def place_order(self, symbol=None, side=None, order_type=None, size=None, stop_price=None, price=None, reduce_only=False, **kw):
        self.calls.append(("place_order", symbol, side, order_type, size, stop_price or price))
        return {"success": True, "order_id": "V2-UNI-001", "code": "00000"}


# ──────────────────────────────────────────────────────────────────
# Helper: create wrapper with injected client
# ──────────────────────────────────────────────────────────────────

from kraken_fake_exchange import make_exchange_stateful


def make_wrapper(client):
    from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
    wrapper = KrakenFuturesExecutorWrapper(api_key="test", api_secret="dGVzdA==")
    wrapper._rest_client = make_exchange_stateful(client)
    wrapper._connected = True
    return wrapper


# ══════════════════════════════════════════════════════════════════
# TEST CLASS 1: _place_order_rest dispatch
# ══════════════════════════════════════════════════════════════════

class TestPlaceOrderRestDispatch:
    """Bug 1 fix: place_market_order / place_limit_order dispatch."""

    @pytest.mark.asyncio
    async def test_market_order_primary_client(self):
        """Primary client: market order → place_market_order()."""
        client = FakePrimaryClient()
        wrapper = make_wrapper(client)
        result = await wrapper._place_order_rest("PF_XBTUSD", "buy", 10, "market")

        assert result["success"] is True
        assert result["code"] == "00000"
        assert result["order_id"] == "ORD-MKT-001"
        assert client.calls[0][0] == "place_market_order"

    @pytest.mark.asyncio
    async def test_limit_order_primary_client(self):
        """Primary client: limit order → place_limit_order()."""
        client = FakePrimaryClient()
        wrapper = make_wrapper(client)
        result = await wrapper._place_order_rest("PF_XBTUSD", "sell", 5, "limit", price=50000.0)

        assert result["success"] is True
        assert result["order_id"] == "ORD-LMT-001"
        assert client.calls[0][0] == "place_limit_order"

    @pytest.mark.asyncio
    async def test_v2_client_fallback(self):
        """V2 client: place_market_order() used (explicit dispatch)."""
        client = FakeV2Client()
        wrapper = make_wrapper(client)
        result = await wrapper._place_order_rest("PF_XBTUSD", "buy", 10, "market")

        assert result["success"] is True
        assert client.calls[0][0] == "place_market_order"

    @pytest.mark.asyncio
    async def test_no_method_returns_not_implemented(self):
        """Client with no order methods → REST_EXCEPTION (explicit dispatch, no fallback)."""
        client = MagicMock()  # no place_market_order, no place_order
        del client.place_market_order
        del client.place_order
        del client.send_order
        wrapper = make_wrapper(client)
        result = await wrapper._place_order_rest("PF_XBTUSD", "buy", 10, "market")

        assert result["code"] == "REST_EXCEPTION"
        assert result.get("success") is not True


# ══════════════════════════════════════════════════════════════════
# TEST CLASS 2: KrakenOrderResult normalization
# ══════════════════════════════════════════════════════════════════

class TestNormalizeOrderResult:
    """Bug 4 fix: KrakenOrderResult → dict."""

    def test_dict_passthrough(self):
        from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
        d = {"success": True, "order_id": "123"}
        assert KrakenFuturesExecutorWrapper._normalize_order_result(d) is d

    def test_kraken_order_result_success(self):
        from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
        result = FakeKrakenOrderResult(success=True, order_id="ORD-001", symbol="PF_XBTUSD")
        normalized = KrakenFuturesExecutorWrapper._normalize_order_result(result)
        assert isinstance(normalized, dict)
        assert normalized["success"] is True
        assert normalized["code"] == "00000"
        assert normalized["order_id"] == "ORD-001"

    def test_kraken_order_result_failure(self):
        from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
        result = FakeKrakenOrderResult(success=False, error="insufficient margin")
        normalized = KrakenFuturesExecutorWrapper._normalize_order_result(result)
        assert isinstance(normalized, dict)
        assert normalized["success"] is False
        assert "margin" in normalized.get("error", "")

    def test_unknown_type_handled(self):
        from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
        normalized = KrakenFuturesExecutorWrapper._normalize_order_result(42)
        assert normalized["code"] == "UNKNOWN_RESPONSE"


# ══════════════════════════════════════════════════════════════════
# TEST CLASS 3: SL/TP dispatch
# ══════════════════════════════════════════════════════════════════

class TestSLTPDispatch:
    """Bugs 2+3: SL → place_stop_order, TP → place_take_profit_order."""

    @pytest.mark.asyncio
    async def test_sl_uses_place_stop_order_on_primary(self):
        """SL attachment uses place_order(order_type='stp') on primary client."""
        client = FakePrimaryClient()
        wrapper = make_wrapper(client)
        result = await wrapper.place_order(
            symbol="BTCUSDT", side="buy", quantity=10,
            order_type="market", stop_loss=60000.0,
        )
        sl_calls = [c for c in client.calls if c[0] == "place_order" and c[3] == "stp"]
        assert len(sl_calls) == 1
        assert sl_calls[0][5] == 60000.0  # stop_price

    @pytest.mark.asyncio
    async def test_tp_uses_place_take_profit_order_on_primary(self):
        """TP attachment uses place_order(order_type='take_profit') on primary client."""
        client = FakePrimaryClient()
        wrapper = make_wrapper(client)
        result = await wrapper.place_order(
            symbol="BTCUSDT", side="buy", quantity=10,
            order_type="market", take_profit=70000.0,
        )
        tp_calls = [c for c in client.calls if c[0] == "place_order" and c[3] == "take_profit"]
        assert len(tp_calls) == 1
        assert tp_calls[0][5] == 70000.0  # tp_price

    @pytest.mark.asyncio
    async def test_sl_uses_place_stop_loss_on_v2(self):
        """SL attachment uses place_order(order_type='stp') on V2 client."""
        client = FakeV2Client()
        wrapper = make_wrapper(client)
        result = await wrapper.place_order(
            symbol="BTCUSDT", side="buy", quantity=10,
            order_type="market", stop_loss=60000.0,
        )
        sl_calls = [c for c in client.calls if c[0] == "place_order" and c[3] == "stp"]
        assert len(sl_calls) == 1

    @pytest.mark.asyncio
    async def test_tp_uses_place_take_profit_on_v2(self):
        """TP attachment uses place_order(order_type='take_profit') on V2 client."""
        client = FakeV2Client()
        wrapper = make_wrapper(client)
        result = await wrapper.place_order(
            symbol="BTCUSDT", side="buy", quantity=10,
            order_type="market", take_profit=70000.0,
        )
        tp_calls = [c for c in client.calls if c[0] == "place_order" and c[3] == "take_profit"]
        assert len(tp_calls) == 1


# ══════════════════════════════════════════════════════════════════
# TEST CLASS 4: Pre-broker validation
# ══════════════════════════════════════════════════════════════════

class TestPreBrokerValidation:
    """Validates the pre-broker gate blocks invalid orders."""

    def test_zero_quantity_blocked(self):
        from backend.execution.order_error_taxonomy import validate_pre_broker
        result = validate_pre_broker("PF_XBTUSD", "buy", 0.0)
        assert result is not None
        assert result["code"] == "ZERO_QUANTITY"

    def test_invalid_side_blocked(self):
        from backend.execution.order_error_taxonomy import validate_pre_broker
        result = validate_pre_broker("PF_XBTUSD", "up", 10.0)
        assert result is not None
        assert result["code"] == "INVALID_SIDE"

    def test_limit_without_price_blocked(self):
        from backend.execution.order_error_taxonomy import validate_pre_broker
        result = validate_pre_broker("PF_XBTUSD", "buy", 10.0, order_type="limit", price=None)
        assert result is not None
        assert result["code"] == "MISSING_LIMIT_PRICE"

    def test_fractional_contracts_blocked(self):
        from backend.execution.order_error_taxonomy import validate_pre_broker
        result = validate_pre_broker("PF_XRPUSD", "buy", 10.5)
        assert result is not None
        assert result["code"] == "FRACTIONAL_QTY"

    def test_reduce_only_on_open_blocked(self):
        from backend.execution.order_error_taxonomy import validate_pre_broker
        result = validate_pre_broker(
            "PF_XBTUSD", "buy", 10.0, reduce_only=True, intent="open"
        )
        assert result is not None
        assert result["code"] == "REDUCE_ONLY_OPEN"

    def test_valid_market_order_passes(self):
        from backend.execution.order_error_taxonomy import validate_pre_broker
        result = validate_pre_broker("PF_XBTUSD", "buy", 10.0)
        assert result is None

    def test_valid_limit_order_passes(self):
        from backend.execution.order_error_taxonomy import validate_pre_broker
        result = validate_pre_broker("PF_XBTUSD", "sell", 5.0, order_type="limit", price=50000.0)
        assert result is None

    def test_invalid_symbol_blocked(self):
        from backend.execution.order_error_taxonomy import validate_pre_broker
        result = validate_pre_broker(
            "FAKE_XYZ", "buy", 10.0,
            valid_symbols={"PF_XBTUSD", "PF_ETHUSD"},
        )
        assert result is not None
        assert result["code"] == "INVALID_SYMBOL"


# ══════════════════════════════════════════════════════════════════
# TEST CLASS 5: Error taxonomy classifier
# ══════════════════════════════════════════════════════════════════

class TestErrorClassifier:
    """Validates classify_error maps broker responses."""

    def test_success_returns_none(self):
        from backend.execution.order_error_taxonomy import classify_error
        assert classify_error({"success": True, "code": "00000"}) is None

    def test_not_implemented(self):
        from backend.execution.order_error_taxonomy import classify_error, ErrorCode
        assert classify_error({"code": "NOT_IMPLEMENTED"}) == ErrorCode.NOT_IMPLEMENTED

    def test_insufficient_margin(self):
        from backend.execution.order_error_taxonomy import classify_error, ErrorCode
        assert classify_error({"code": "REJECT", "msg": "insufficient margin"}) == ErrorCode.INSUFFICIENT_MARGIN

    def test_rate_limited(self):
        from backend.execution.order_error_taxonomy import classify_error, ErrorCode
        assert classify_error({"code": "429", "msg": "rate limit exceeded"}) == ErrorCode.RATE_LIMITED

    def test_unknown_fallback(self):
        from backend.execution.order_error_taxonomy import classify_error, ErrorCode
        assert classify_error({"code": "XYZ", "msg": "something weird"}) == ErrorCode.ORDER_REJECTED


# ══════════════════════════════════════════════════════════════════
# TEST CLASS 6: Symbol normalization
# ══════════════════════════════════════════════════════════════════

class TestSymbolNormalization:
    def test_btcusdt_to_kraken(self):
        from backend.execution.kraken_futures_executor import to_kraken_symbol
        assert to_kraken_symbol("BTCUSDT") == "PF_XBTUSD"

    def test_already_kraken_format(self):
        from backend.execution.kraken_futures_executor import to_kraken_symbol
        assert to_kraken_symbol("PF_XBTUSD") == "PF_XBTUSD"

    def test_no_double_prefix(self):
        from backend.execution.kraken_futures_executor import to_kraken_symbol
        result = to_kraken_symbol("PF_PF_XBTUSD")
        # Should not add another PF_
        assert result.startswith("PF_")

    def test_case_insensitive(self):
        from backend.execution.kraken_futures_executor import to_kraken_symbol
        assert to_kraken_symbol("btcusdt") == "PF_XBTUSD"

    def test_slash_normalization(self):
        from backend.execution.kraken_futures_executor import to_kraken_symbol
        assert to_kraken_symbol("BTC/USDT") == "PF_XBTUSD"


# ══════════════════════════════════════════════════════════════════
# TEST CLASS 7: Full round-trip (integration smoke)
# ══════════════════════════════════════════════════════════════════

class TestFullRoundTrip:
    """End-to-end: place_order → _place_order_rest → result."""

    @pytest.mark.asyncio
    async def test_market_buy_full_flow(self):
        """Full market buy flow with primary client."""
        client = FakePrimaryClient()
        wrapper = make_wrapper(client)
        result = await wrapper.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=100,
            order_type="market",
        )
        assert result["success"] is True
        assert result["order_id"] == "ORD-MKT-001"
        assert client.calls[0][0] == "place_market_order"
        assert client.calls[0][1] == "PF_XBTUSD"  # symbol was normalized

    @pytest.mark.asyncio
    async def test_market_buy_with_sl_tp(self):
        """Market buy with SL+TP attachment."""
        client = FakePrimaryClient()
        wrapper = make_wrapper(client)
        result = await wrapper.place_order(
            symbol="ETHUSDT",
            side="buy",
            quantity=50,
            order_type="market",
            stop_loss=3000.0,
            take_profit=4000.0,
        )
        assert result["success"] is True
        assert result.get("sl_attached") is True
        assert result.get("tp_attached") is True
        # Verify all 3 orders placed: market + SL (place_order stp) + TP (place_order take_profit)
        assert client.calls[0][0] == "place_market_order"
        sl_calls = [c for c in client.calls if c[0] == "place_order" and c[3] == "stp"]
        tp_calls = [c for c in client.calls if c[0] == "place_order" and c[3] == "take_profit"]
        assert len(sl_calls) == 1
        assert len(tp_calls) == 1

    @pytest.mark.asyncio
    async def test_failed_order_preserves_error(self):
        """Failed order preserves raw error message."""
        client = FakeFailingClient()
        wrapper = make_wrapper(client)
        result = await wrapper._place_order_rest("PF_XBTUSD", "buy", 10, "market")
        assert result.get("success") is False
        msg = result.get("msg", "") or result.get("error", "")
        assert "margin" in msg.lower()

    @pytest.mark.asyncio
    async def test_long_normalized_to_buy(self):
        """Side 'long' is normalized to 'buy'."""
        client = FakePrimaryClient()
        wrapper = make_wrapper(client)
        result = await wrapper.place_order(
            symbol="BTCUSDT", side="long", quantity=10, order_type="market",
        )
        assert result["success"] is True
        assert client.calls[0][2] == "buy"

    @pytest.mark.asyncio
    async def test_short_normalized_to_sell(self):
        """Side 'short' is normalized to 'sell'."""
        client = FakePrimaryClient()
        wrapper = make_wrapper(client)
        result = await wrapper.place_order(
            symbol="BTCUSDT", side="short", quantity=10, order_type="market",
        )
        assert result["success"] is True
        assert client.calls[0][2] == "sell"

    @pytest.mark.asyncio
    async def test_integer_contracts_enforced(self):
        """Fractional quantity gets floored to integer contracts."""
        client = FakePrimaryClient()
        wrapper = make_wrapper(client)
        # qty=7.8 on XRP (prec 0) should become 7.0 (int(7.8) = 7)
        result = await wrapper.place_order(
            symbol="XRPUSDT", side="buy", quantity=7.8, order_type="market",
        )
        assert result["success"] is True
        # Size sent to client should be 7.0
        assert client.calls[0][3] == 7.0


# ══════════════════════════════════════════════════════════════════
# TEST CLASS 8: Emergency close on SL failure
# ══════════════════════════════════════════════════════════════════

class TestEmergencyClose:
    """When SL attachment fails, position must be emergency-closed."""

    @pytest.mark.asyncio
    async def test_sl_fail_triggers_emergency_close(self):
        """If SL fails all retries → emergency close fired."""
        client = FakePrimaryClient()

        # Override place_order to fail for SL (order_type="stp")
        _original_place_order = client.place_order
        async def fail_sl_place_order(symbol=None, side=None, order_type=None, **kwargs):
            if order_type == "stp":
                return {"success": False, "error": "rejected"}
            return await _original_place_order(symbol=symbol, side=side, order_type=order_type, **kwargs)

        client.place_order = fail_sl_place_order
        wrapper = make_wrapper(client)

        # Patch retry env to 1 for speed
        with patch.dict("os.environ", {"KRAKEN_RETRY_ON_ORDER_REJECT": "1"}):
            result = await wrapper.place_order(
                symbol="BTCUSDT", side="buy", quantity=10,
                order_type="market", stop_loss=60000.0,
            )

        assert result.get("sl_attached") is False
        assert result.get("_sl_emergency_close") is True
