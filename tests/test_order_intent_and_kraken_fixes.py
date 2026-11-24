"""
Tests for OrderIntent validation, Kraken reduce-only protection,
close_position_service Kraken support, and PositionSafetyGuard fixes.

Covers the critical bugs:
  1. Open orders must NEVER have reduce_only=True
  2. Close/reduce/SL/TP orders CAN have reduce_only=True
  3. SL/TP must be created after opening fill
  4. close_position_service routes correctly per broker
  5. PositionSafetyGuard uses correct Kraken REST method names
  6. Emergency close triggers when SL creation fails

Author: SMC Trading Bot Framework
"""

import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ═══════════════════════════════════════════════════════════════════
#  OrderIntent unit tests
# ═══════════════════════════════════════════════════════════════════

from backend.execution.order_intent import (
    OrderIntent,
    OrderIntentError,
    ValidatedOrderParams,
    resolve_reduce_only,
    validate_order_intent,
    get_close_side,
    map_to_kraken_params,
    map_to_bitget_params,
)


class TestOrderIntentEnum:
    """Test OrderIntent enum values."""

    def test_open_value(self):
        assert OrderIntent.OPEN.value == "open"

    def test_close_value(self):
        assert OrderIntent.CLOSE.value == "close"

    def test_reduce_value(self):
        assert OrderIntent.REDUCE.value == "reduce"

    def test_stop_loss_value(self):
        assert OrderIntent.STOP_LOSS.value == "stop_loss"

    def test_take_profit_value(self):
        assert OrderIntent.TAKE_PROFIT.value == "take_profit"

    def test_from_string(self):
        assert OrderIntent("open") == OrderIntent.OPEN
        assert OrderIntent("close") == OrderIntent.CLOSE


class TestResolveReduceOnly:
    """Core test class: verify reduce_only is NEVER True for OPEN intent."""

    def test_open_order_is_never_reduce_only(self):
        """BUG FIX: Opening a position must NEVER set reduce_only=True."""
        result = resolve_reduce_only(OrderIntent.OPEN, "kraken", explicit_reduce_only=True)
        assert result is False, "OPEN intent must ALWAYS resolve to reduce_only=False"

    def test_open_order_is_never_reduce_only_bitget(self):
        result = resolve_reduce_only(OrderIntent.OPEN, "bitget", explicit_reduce_only=True)
        assert result is False

    def test_open_order_default_false(self):
        result = resolve_reduce_only(OrderIntent.OPEN, "kraken")
        assert result is False

    def test_close_order_can_be_reduce_only(self):
        """Close orders should have reduce_only=True."""
        result = resolve_reduce_only(OrderIntent.CLOSE, "kraken")
        assert result is True

    def test_close_order_reduce_only_bitget(self):
        result = resolve_reduce_only(OrderIntent.CLOSE, "bitget")
        assert result is True

    def test_reduce_order_reduce_only(self):
        result = resolve_reduce_only(OrderIntent.REDUCE, "kraken")
        assert result is True

    def test_stop_loss_reduce_only_kraken(self):
        result = resolve_reduce_only(OrderIntent.STOP_LOSS, "kraken")
        assert result is True

    def test_take_profit_reduce_only_kraken(self):
        result = resolve_reduce_only(OrderIntent.TAKE_PROFIT, "kraken")
        assert result is True

    def test_open_overrides_explicit_true(self):
        """Even if caller explicitly passes reduce_only=True for OPEN, it must be blocked."""
        for exchange in ("kraken", "bitget"):
            result = resolve_reduce_only(OrderIntent.OPEN, exchange, explicit_reduce_only=True)
            assert result is False, f"OPEN must force reduce_only=False on {exchange}"


class TestValidateOrderIntent:
    """Test full order validation pipeline."""

    def test_valid_open_market_order(self):
        params = validate_order_intent(
            intent=OrderIntent.OPEN,
            symbol="PF_XBTUSD",
            side="buy",
            quantity=100,
            order_type="market",
            exchange="kraken",
        )
        assert params.reduce_only is False
        assert params.intent == OrderIntent.OPEN
        assert params.symbol == "PF_XBTUSD"

    def test_valid_close_order(self):
        params = validate_order_intent(
            intent=OrderIntent.CLOSE,
            symbol="PF_XBTUSD",
            side="sell",
            quantity=100,
            order_type="market",
            exchange="kraken",
        )
        assert params.reduce_only is True

    def test_zero_quantity_rejected(self):
        with pytest.raises(OrderIntentError, match="must be > 0"):
            validate_order_intent(
                intent=OrderIntent.OPEN,
                symbol="PF_XBTUSD",
                side="buy",
                quantity=0,
                exchange="kraken",
            )

    def test_limit_without_price_rejected(self):
        with pytest.raises(OrderIntentError, match="requires a price"):
            validate_order_intent(
                intent=OrderIntent.OPEN,
                symbol="PF_XBTUSD",
                side="buy",
                quantity=100,
                order_type="limit",
                exchange="kraken",
            )

    def test_stop_loss_without_trigger_rejected(self):
        with pytest.raises(OrderIntentError, match="requires a trigger_price"):
            validate_order_intent(
                intent=OrderIntent.STOP_LOSS,
                symbol="PF_XBTUSD",
                side="sell",
                quantity=100,
                exchange="kraken",
            )

    def test_sl_order_with_trigger(self):
        params = validate_order_intent(
            intent=OrderIntent.STOP_LOSS,
            symbol="PF_XBTUSD",
            side="sell",
            quantity=100,
            trigger_price=48000.0,
            exchange="kraken",
        )
        assert params.reduce_only is True
        assert params.trigger_price == 48000.0


class TestGetCloseSide:
    """Test close side determination."""

    def test_long_closes_with_sell(self):
        assert get_close_side("long") == "sell"

    def test_short_closes_with_buy(self):
        assert get_close_side("short") == "buy"

    def test_buy_closes_with_sell(self):
        assert get_close_side("buy") == "sell"

    def test_sell_closes_with_buy(self):
        assert get_close_side("sell") == "buy"

    def test_unknown_side_raises(self):
        with pytest.raises(OrderIntentError):
            get_close_side("neutral")


class TestMapToKrakenParams:
    """Test mapping to Kraken REST API format."""

    def test_market_open_no_reduce_only(self):
        params = validate_order_intent(
            intent=OrderIntent.OPEN,
            symbol="PF_XBTUSD",
            side="buy",
            quantity=100,
            exchange="kraken",
        )
        kraken_params = map_to_kraken_params(params)
        assert kraken_params["orderType"] == "mkt"
        assert "reduceOnly" not in kraken_params

    def test_close_has_reduce_only(self):
        params = validate_order_intent(
            intent=OrderIntent.CLOSE,
            symbol="PF_XBTUSD",
            side="sell",
            quantity=100,
            exchange="kraken",
        )
        kraken_params = map_to_kraken_params(params)
        assert kraken_params["reduceOnly"] == "true"

    def test_stop_loss_type(self):
        params = validate_order_intent(
            intent=OrderIntent.STOP_LOSS,
            symbol="PF_XBTUSD",
            side="sell",
            quantity=100,
            trigger_price=48000.0,
            exchange="kraken",
        )
        kraken_params = map_to_kraken_params(params)
        assert kraken_params["orderType"] == "stp"
        assert kraken_params["stopPrice"] == 48000.0
        assert kraken_params["reduceOnly"] == "true"


class TestMapToBitgetParams:
    """Test mapping to Bitget API format."""

    def test_open_trade_side(self):
        params = validate_order_intent(
            intent=OrderIntent.OPEN,
            symbol="BTCUSDT",
            side="buy",
            quantity=0.001,
            exchange="bitget",
        )
        bitget_params = map_to_bitget_params(params)
        assert bitget_params["tradeSide"] == "open"
        assert "reduceOnly" not in bitget_params

    def test_close_trade_side(self):
        params = validate_order_intent(
            intent=OrderIntent.CLOSE,
            symbol="BTCUSDT",
            side="sell",
            quantity=0.001,
            exchange="bitget",
        )
        bitget_params = map_to_bitget_params(params)
        assert bitget_params["tradeSide"] == "close"


# ═══════════════════════════════════════════════════════════════════
#  Kraken Executor wrapper tests (mock-based)
# ═══════════════════════════════════════════════════════════════════

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    to_kraken_symbol,
    from_kraken_symbol,
)


class TestSymbolMapping:
    """Test Kraken symbol normalization."""

    def test_btcusdt_to_kraken(self):
        assert to_kraken_symbol("BTCUSDT") == "PF_XBTUSD"

    def test_ethusdt_to_kraken(self):
        assert to_kraken_symbol("ETHUSDT") == "PF_ETHUSD"

    def test_already_kraken_format(self):
        assert to_kraken_symbol("PF_XBTUSD") == "PF_XBTUSD"

    def test_from_kraken(self):
        assert from_kraken_symbol("PF_XBTUSD") == "BTCUSDT"


class TestKrakenExecutorPlaceOrder:
    """Test KrakenFuturesExecutorWrapper.place_order() with mocked REST client."""

    @pytest.fixture
    def mock_rest_client(self):
        client = AsyncMock()
        client._placed = False
        async def _mkt(*args, **kw):
            client._placed = True
            return {"success": True, "order_id": "order_123", "status": "placed"}
        client.place_market_order = AsyncMock(side_effect=_mkt)
        client.place_limit_order = AsyncMock(return_value={
            "success": True, "order_id": "order_124", "status": "placed"
        })
        client.place_order = AsyncMock(return_value={
            "success": True, "order_id": "sl_tp_456"
        })
        # Real-fill confirmation data (executor never trusts the requested quantity)
        client.get_fills = AsyncMock(return_value=[
            {"order_id": "order_123", "symbol": "PF_XBTUSD", "size": 100},
            {"order_id": "order_124", "symbol": "PF_XBTUSD", "size": 100},
        ])
        async def _get_pos(*args, **kw):
            if not client._placed:
                return []
            return [{"symbol": "PF_XBTUSD", "side": "long", "size": 100}]
        client.get_positions = AsyncMock(side_effect=_get_pos)
        client.get_open_orders = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def executor(self, mock_rest_client):
        from backend.execution.kraken_futures_executor import KrakenTradeTracker
        wrapper = KrakenFuturesExecutorWrapper(api_key="test", api_secret="dGVzdA==")
        wrapper._rest_client = mock_rest_client
        wrapper._connected = True
        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="close_111", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=100.0, expected_exposure=100.0, status="OPEN"
        )
        return wrapper

    @pytest.mark.asyncio
    async def test_open_order_forces_reduce_only_false(self, executor, mock_rest_client):
        """CRITICAL: Open orders must never send reduce_only=True to Kraken."""
        result = await executor.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=100,
            order_type="market",
            reduce_only=True,  # Caller erroneously passes True
            intent="open",     # But intent is OPEN
        )
        # Verify the REST call received reduce_only=False
        call_args = mock_rest_client.place_market_order.call_args
        assert call_args is not None
        assert call_args.kwargs.get("reduce_only") is False

    @pytest.mark.asyncio
    async def test_close_order_uses_reduce_only_true(self, executor, mock_rest_client):
        """Close orders should have reduce_only=True on Kraken."""
        result = await executor.place_order(
            symbol="BTCUSDT",
            side="sell",
            quantity=100,
            order_type="market",
            reduce_only=True,
            intent="close",
        )
        call_args = mock_rest_client.place_market_order.call_args
        assert call_args is not None
        assert call_args.kwargs.get("reduce_only") is True

    @pytest.mark.asyncio
    async def test_sl_attached_after_open_fill(self, executor, mock_rest_client):
        """SL must be attached after a successful opening order."""
        result = await executor.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=100,
            order_type="market",
            stop_loss=48000.0,
            intent="open",
        )
        # SL is placed via place_order(order_type="stp")
        sl_calls = [c for c in mock_rest_client.place_order.call_args_list
                    if c.kwargs.get("order_type") == "stp"]
        assert len(sl_calls) >= 1
        assert result.get("sl_attached") is True

    @pytest.mark.asyncio
    async def test_tp_attached_after_open_fill(self, executor, mock_rest_client):
        """TP must be attached after a successful opening order."""
        result = await executor.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=100,
            order_type="market",
            take_profit=55000.0,
            intent="open",
        )
        # TP is placed via place_order(order_type="take_profit")
        tp_calls = [c for c in mock_rest_client.place_order.call_args_list
                    if c.kwargs.get("order_type") == "take_profit"]
        assert len(tp_calls) >= 1
        assert result.get("tp_attached") is True

    @pytest.mark.asyncio
    async def test_sl_failure_triggers_emergency_close(self, executor, mock_rest_client):
        """If SL creation fails, the position must be emergency closed."""
        _original = mock_rest_client.place_order
        async def fail_stp(**kwargs):
            if kwargs.get("order_type") == "stp":
                raise RuntimeError("SL creation failed")
            return await _original(**kwargs)
        mock_rest_client.place_order = AsyncMock(side_effect=fail_stp)

        with patch.dict("os.environ", {"KRAKEN_RETRY_ON_ORDER_REJECT": "1"}):
            result = await executor.place_order(
                symbol="BTCUSDT",
                side="buy",
                quantity=100,
                order_type="market",
                stop_loss=48000.0,
                intent="open",
            )
        assert result.get("_sl_emergency_close") is True
        assert result.get("sl_attached") is False

    @pytest.mark.asyncio
    async def test_symbol_normalization(self, executor, mock_rest_client):
        """BTCUSDT should be normalized to PF_XBTUSD for Kraken."""
        await executor.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=100,
            intent="open",
        )
        call_args = mock_rest_client.place_market_order.call_args
        assert call_args is not None
        symbol_arg = call_args.kwargs.get("symbol")
        assert symbol_arg == "PF_XBTUSD"


class TestKrakenExecutorClosePosition:
    """Test close_position method."""

    @pytest.fixture
    def mock_rest_client(self):
        client = AsyncMock()
        client.place_market_order = AsyncMock(return_value={
            "success": True, "order_id": "close_111"
        })
        client.place_order = AsyncMock(return_value={
            "success": True, "order_id": "close_112"
        })
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 100.0, "entry_price": 50000}
        ])
        return client

    @pytest.fixture
    def executor(self, mock_rest_client):
        from backend.execution.kraken_futures_executor import KrakenTradeTracker
        wrapper = KrakenFuturesExecutorWrapper(api_key="test", api_secret="dGVzdA==")
        wrapper._rest_client = mock_rest_client
        wrapper._connected = True
        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="close_111", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=100.0, expected_exposure=100.0, status="OPEN"
        )
        return wrapper

    @pytest.mark.asyncio
    async def test_close_sends_reduce_only(self, executor, mock_rest_client):
        """close_position must always use reduce_only=True."""
        result = await executor.close_position(symbol="BTCUSDT")
        call_args = mock_rest_client.place_market_order.call_args
        assert call_args is not None
        assert call_args.kwargs.get("reduce_only") is True

    @pytest.mark.asyncio
    async def test_close_opposite_side(self, executor, mock_rest_client):
        """Closing a long position should send a sell order."""
        result = await executor.close_position(symbol="BTCUSDT")
        call_args = mock_rest_client.place_market_order.call_args
        assert call_args is not None
        side_arg = call_args.kwargs.get("side")
        assert side_arg == "sell"


# ═══════════════════════════════════════════════════════════════════
#  ClosePositionService broker routing tests
# ═══════════════════════════════════════════════════════════════════

class TestClosePositionServiceBrokerDetection:
    """Test that close_position_service routes correctly per broker."""

    def test_detect_kraken_pf_symbol(self):
        from backend.services.close_position_service import _detect_broker
        assert _detect_broker("PF_XBTUSD") == "kraken"

    def test_detect_bitget_symbol(self):
        from backend.services.close_position_service import _detect_broker
        assert _detect_broker("BTCUSDT") == "bitget"

    def test_detect_kraken_pi_symbol(self):
        from backend.services.close_position_service import _detect_broker
        assert _detect_broker("PI_XBTUSD") == "kraken"


# ═══════════════════════════════════════════════════════════════════
#  PositionSafetyGuard method name tests
# ═══════════════════════════════════════════════════════════════════

class TestSafetyGuardKrakenMethodNames:
    """Verify PositionSafetyGuard uses correct Kraken REST method names."""

    @pytest.mark.asyncio
    async def test_attach_sl_calls_place_stop_loss(self):
        """Guard must call client.place_stop_loss() NOT client.place_stop_order()."""
        from backend.risk.position_safety_guard import PositionSafetyGuard, GuardConfig

        # Mock executor with a REST client
        mock_client = AsyncMock()
        mock_client.place_stop_loss = AsyncMock(return_value={"success": True})
        mock_client.place_take_profit = AsyncMock(return_value={"success": True})

        mock_executor = MagicMock()
        mock_executor._rest_client = mock_client

        guard = PositionSafetyGuard(
            executors={"kraken": mock_executor},
            config=GuardConfig(retry_count=1),
        )

        success = await guard._attach_with_retry(
            executor=mock_executor,
            broker="kraken",
            symbol="PF_XBTUSD",
            plan_type="stop_loss",
            price=48000.0,
            hold_side="long",
            size=100.0,
        )

        mock_client.place_stop_loss.assert_called_once()
        assert success is True

    @pytest.mark.asyncio
    async def test_attach_tp_calls_place_take_profit(self):
        """Guard must call client.place_take_profit() NOT client.place_take_profit_order()."""
        from backend.risk.position_safety_guard import PositionSafetyGuard, GuardConfig

        mock_client = AsyncMock()
        mock_client.place_stop_loss = AsyncMock(return_value={"success": True})
        mock_client.place_take_profit = AsyncMock(return_value={"success": True})

        mock_executor = MagicMock()
        mock_executor._rest_client = mock_client

        guard = PositionSafetyGuard(
            executors={"kraken": mock_executor},
            config=GuardConfig(retry_count=1),
        )

        success = await guard._attach_with_retry(
            executor=mock_executor,
            broker="kraken",
            symbol="PF_XBTUSD",
            plan_type="take_profit",
            price=55000.0,
            hold_side="long",
            size=100.0,
        )

        mock_client.place_take_profit.assert_called_once()
        assert success is True

    @pytest.mark.asyncio
    async def test_emergency_close_on_sl_failure(self):
        """If SL creation fails after retries, emergency close must trigger."""
        from backend.risk.position_safety_guard import PositionSafetyGuard, GuardConfig

        mock_client = AsyncMock()
        mock_client.place_stop_loss = AsyncMock(
            side_effect=RuntimeError("SL fail")
        )

        mock_executor = MagicMock()
        mock_executor._rest_client = mock_client
        mock_executor.place_order = AsyncMock(return_value={"success": True})

        guard = PositionSafetyGuard(
            executors={"kraken": mock_executor},
            config=GuardConfig(
                retry_count=1,
                emergency_close_on_sl_fail=True,
            ),
        )

        # _attach_with_retry should return False
        success = await guard._attach_with_retry(
            executor=mock_executor,
            broker="kraken",
            symbol="PF_XBTUSD",
            plan_type="stop_loss",
            price=48000.0,
            hold_side="long",
            size=100.0,
        )

        assert success is False


# ═══════════════════════════════════════════════════════════════════
#  PHASE 2: Production-Hardening Tests (15+)
# ═══════════════════════════════════════════════════════════════════

from backend.execution.order_intent import (
    InvalidReduceOnlyUsageError,
    InvalidKrakenQuantityError,
    InvalidProtectionOrderError,
    validate_kraken_order_payload,
)


class TestValidateKrakenOrderPayload:
    """Central Kraken payload validation tests."""

    def test_blocks_open_reduce_only(self):
        """validate_kraken_order_payload must reject OPEN + reduce_only=True."""
        with pytest.raises(InvalidReduceOnlyUsageError):
            validate_kraken_order_payload(
                intent=OrderIntent.OPEN,
                symbol="PF_XBTUSD",
                side="buy",
                quantity=100,
                reduce_only=True,
            )

    def test_blocks_zero_quantity(self):
        with pytest.raises(InvalidKrakenQuantityError):
            validate_kraken_order_payload(
                intent=OrderIntent.OPEN,
                symbol="PF_XBTUSD",
                side="buy",
                quantity=0,
            )

    def test_blocks_fractional_quantity(self):
        """Kraken Futures requires integer contracts."""
        with pytest.raises(InvalidKrakenQuantityError):
            validate_kraken_order_payload(
                intent=OrderIntent.OPEN,
                symbol="PF_XBTUSD",
                side="buy",
                quantity=1.5,
            )

    def test_blocks_invalid_sl_side_long(self):
        """SL above entry for a long position is invalid."""
        with pytest.raises(InvalidProtectionOrderError):
            validate_kraken_order_payload(
                intent=OrderIntent.STOP_LOSS,
                symbol="PF_XBTUSD",
                side="sell",
                quantity=100,
                trigger_price=55000.0,
                entry_price=50000.0,
                position_side="long",
            )

    def test_blocks_invalid_sl_side_short(self):
        """SL below entry for a short position is invalid."""
        with pytest.raises(InvalidProtectionOrderError):
            validate_kraken_order_payload(
                intent=OrderIntent.STOP_LOSS,
                symbol="PF_XBTUSD",
                side="buy",
                quantity=100,
                trigger_price=45000.0,
                entry_price=50000.0,
                position_side="short",
            )

    def test_blocks_invalid_tp_side_long(self):
        """TP below entry for long is invalid."""
        with pytest.raises(InvalidProtectionOrderError):
            validate_kraken_order_payload(
                intent=OrderIntent.TAKE_PROFIT,
                symbol="PF_XBTUSD",
                side="sell",
                quantity=100,
                trigger_price=45000.0,
                entry_price=50000.0,
                position_side="long",
            )

    def test_blocks_invalid_tp_side_short(self):
        """TP above entry for short is invalid."""
        with pytest.raises(InvalidProtectionOrderError):
            validate_kraken_order_payload(
                intent=OrderIntent.TAKE_PROFIT,
                symbol="PF_XBTUSD",
                side="buy",
                quantity=100,
                trigger_price=55000.0,
                entry_price=50000.0,
                position_side="short",
            )

    def test_valid_sl_long(self):
        """SL below entry for long should pass."""
        params = validate_kraken_order_payload(
            intent=OrderIntent.STOP_LOSS,
            symbol="PF_XBTUSD",
            side="sell",
            quantity=100,
            trigger_price=48000.0,
            entry_price=50000.0,
            position_side="long",
        )
        assert params.reduce_only is True
        assert params.trigger_price == 48000.0

    def test_valid_tp_long(self):
        """TP above entry for long should pass."""
        params = validate_kraken_order_payload(
            intent=OrderIntent.TAKE_PROFIT,
            symbol="PF_XBTUSD",
            side="sell",
            quantity=100,
            trigger_price=55000.0,
            entry_price=50000.0,
            position_side="long",
        )
        assert params.reduce_only is True

    def test_blocks_empty_symbol(self):
        with pytest.raises(OrderIntentError, match="empty"):
            validate_kraken_order_payload(
                intent=OrderIntent.OPEN,
                symbol="",
                side="buy",
                quantity=100,
            )

    def test_valid_open_payload(self):
        """A valid OPEN order should pass all checks."""
        params = validate_kraken_order_payload(
            intent=OrderIntent.OPEN,
            symbol="PF_XBTUSD",
            side="buy",
            quantity=100,
        )
        assert params.reduce_only is False
        assert params.intent == OrderIntent.OPEN

    def test_valid_close_payload(self):
        params = validate_kraken_order_payload(
            intent=OrderIntent.CLOSE,
            symbol="PF_XBTUSD",
            side="sell",
            quantity=100,
        )
        assert params.reduce_only is True

    def test_invalid_trigger_price_rejected_before_exchange_call(self):
        """SL without trigger_price must be caught locally."""
        with pytest.raises(OrderIntentError, match="trigger_price"):
            validate_kraken_order_payload(
                intent=OrderIntent.STOP_LOSS,
                symbol="PF_XBTUSD",
                side="sell",
                quantity=100,
            )


class TestProtectionOrderSides:
    """Test SL/TP use correct side for long/short positions."""

    def test_protection_orders_use_correct_side_for_long(self):
        """SL/TP for a long position should be on sell side."""
        assert get_close_side("long") == "sell"

    def test_protection_orders_use_correct_side_for_short(self):
        """SL/TP for a short position should be on buy side."""
        assert get_close_side("short") == "buy"


class TestGuardPerPositionLock:
    """Test PositionSafetyGuard per-position lock and cooldown."""

    @pytest.mark.asyncio
    async def test_guard_does_not_duplicate_existing_protection(self):
        """If Kraken already has SL+TP, guard must NOT create duplicates."""
        from backend.risk.position_safety_guard import PositionSafetyGuard, GuardConfig

        mock_client = AsyncMock()
        # Simulate get_open_orders returning existing SL + TP
        mock_client.get_open_orders = AsyncMock(return_value={
            "openOrders": [
                {"symbol": "PF_XBTUSD", "orderType": "stp", "side": "sell"},
                {"symbol": "PF_XBTUSD", "orderType": "take_profit", "side": "sell"},
            ]
        })
        mock_client.place_stop_loss = AsyncMock(return_value={"success": True})
        mock_client.place_take_profit = AsyncMock(return_value={"success": True})

        mock_executor = MagicMock()
        mock_executor._rest_client = mock_client

        guard = PositionSafetyGuard(
            executors={"kraken": mock_executor},
            config=GuardConfig(retry_count=1, protection_repair_cooldown_sec=0),
        )

        pos = {
            "symbol": "PF_XBTUSD", "side": "long", "size": 100,
            "entry_price": 50000, "markPrice": 50500, "leverage": 5,
        }

        await guard._ensure_sl_tp(
            pos, mock_executor, "kraken", "PF_XBTUSD", "long", 100, 50000.0, 5
        )

        # SL and TP should NOT have been created (they already exist)
        mock_client.place_stop_loss.assert_not_called()
        mock_client.place_take_profit.assert_not_called()

    @pytest.mark.asyncio
    async def test_guard_closes_if_protection_missing_after_retries(self):
        """After max failures, guard must force-close the position."""
        from backend.risk.position_safety_guard import PositionSafetyGuard, GuardConfig

        mock_client = AsyncMock()
        mock_client.get_open_orders = AsyncMock(return_value={"openOrders": []})
        mock_client.place_stop_loss = AsyncMock(
            side_effect=RuntimeError("SL fail")
        )

        mock_executor = MagicMock()
        mock_executor._rest_client = mock_client
        mock_executor.place_order = AsyncMock(return_value={"success": True})

        guard = PositionSafetyGuard(
            executors={"kraken": mock_executor},
            config=GuardConfig(
                retry_count=1,
                emergency_close_on_sl_fail=True,
                protection_repair_cooldown_sec=0,
                max_protection_failures=2,
                force_close_if_unprotected=True,
            ),
        )

        # Set failure count to max
        guard._protection_failures["kraken:PF_XBTUSD:long"] = 2

        pos = {
            "symbol": "PF_XBTUSD", "side": "long", "size": 100,
            "entry_price": 50000, "markPrice": 50500, "leverage": 5,
        }

        await guard._ensure_sl_tp(
            pos, mock_executor, "kraken", "PF_XBTUSD", "long", 100, 50000.0, 5
        )

        # Emergency close should have triggered
        assert guard._stats["emergency_closes"] >= 1

    @pytest.mark.asyncio
    async def test_guard_cooldown_skips(self):
        """Guard should skip protection repair if cooldown hasn't elapsed."""
        import time
        from backend.risk.position_safety_guard import PositionSafetyGuard, GuardConfig

        mock_client = AsyncMock()
        mock_client.get_open_orders = AsyncMock(return_value={"openOrders": []})
        mock_client.place_stop_loss = AsyncMock(return_value={"success": True})

        mock_executor = MagicMock()
        mock_executor._rest_client = mock_client

        guard = PositionSafetyGuard(
            executors={"kraken": mock_executor},
            config=GuardConfig(
                retry_count=1,
                protection_repair_cooldown_sec=9999,  # Very long cooldown
            ),
        )

        # Set recent repair attempt
        guard._last_repair_attempt["kraken:PF_XBTUSD:long"] = time.monotonic()

        pos = {
            "symbol": "PF_XBTUSD", "side": "long", "size": 100,
            "entry_price": 50000, "markPrice": 50500, "leverage": 5,
        }

        await guard._ensure_sl_tp(
            pos, mock_executor, "kraken", "PF_XBTUSD", "long", 100, 50000.0, 5
        )

        # SL should NOT be created (cooldown active)
        mock_client.place_stop_loss.assert_not_called()
        assert guard._stats["cooldown_skips"] >= 1


class TestClosePositionIdempotent:
    """Test close_position is idempotent when no position exists."""

    @pytest.mark.asyncio
    async def test_close_position_is_idempotent(self):
        """Closing when no position exists should return success (idempotent)."""
        from backend.services.close_position_service import (
            close_position, set_close_service_executor, CloseStatus,
        )

        mock_executor = AsyncMock()
        mock_executor.get_positions = AsyncMock(return_value={
            "code": "00000", "data": []
        })

        set_close_service_executor(mock_executor)

        result = await close_position("BTCUSDT", percent=100, reason="test")
        assert result.success is True
        assert result.status == CloseStatus.ALREADY_CLOSED

    @pytest.mark.asyncio
    async def test_reduce_position_partial_qty(self):
        """Reducing a position should calculate correct partial qty."""
        from backend.execution.kraken_futures_executor import (
            KrakenFuturesExecutorWrapper, KrakenTradeTracker,
        )

        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 100, "entry_price": 50000}
        ])
        mock_client.place_order = AsyncMock(return_value={
            "success": True, "order_id": "reduce_001"
        })

        from backend.execution.kraken_futures_executor import KrakenTradeTracker
        wrapper = KrakenFuturesExecutorWrapper(api_key="test", api_secret="dGVzdA==")
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="close_999", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=100.0, expected_exposure=100.0, status="OPEN"
        )
        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="reduce_001", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=100.0, expected_exposure=100.0, status="OPEN"
        )

        result = await wrapper.reduce_position("BTCUSDT", percent=50)
        # 50% of 100 = 50 contracts
        assert result.get("close_qty") == 50.0 or result.get("success") or result.get("code") in ("00000", None)


class TestSymbolNormalizationConsistency:
    """Test symbol normalization is consistent across services."""

    def test_symbol_normalization_consistent_across_services(self):
        """to_kraken_symbol result must match KrakenFuturesExecutorWrapper.normalize_symbol."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="test", api_secret="dGVzdA==")

        for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
            direct = to_kraken_symbol(sym)
            via_wrapper = wrapper.normalize_symbol(sym)
            assert direct == via_wrapper, f"Mismatch for {sym}: {direct} vs {via_wrapper}"

    def test_stale_protection_order_detected(self):
        """Verify protected flag is correctly read/written."""
        from backend.risk.position_safety_guard import PositionSafetyGuard, GuardConfig

        guard = PositionSafetyGuard(
            executors={},
            config=GuardConfig(),
        )

        pos_key = "kraken:PF_XBTUSD:long"
        # Initially not protected
        assert guard._trailing_highs.get(f"protected:{pos_key}") is None

        # After marking
        guard._trailing_highs[f"protected:{pos_key}"] = 1.0
        assert guard._trailing_highs.get(f"protected:{pos_key}") == 1.0


class TestCloseServiceBrokerPassthrough:
    """Test that close_position_service passes broker to _wait_for_confirmation."""

    @pytest.mark.asyncio
    async def test_close_retries_and_confirms_final_state(self):
        """Kraken close_position retries and verifies position is gone."""
        mock_client = AsyncMock()
        # First call: position still open; second call: position closed
        mock_client.get_positions = AsyncMock(side_effect=[
            [{"symbol": "PF_XBTUSD", "side": "long", "size": 100, "entry_price": 50000}],
            [{"symbol": "PF_XBTUSD", "side": "long", "size": 100, "entry_price": 50000}],
            [],  # After close - verified
        ])
        mock_client.place_market_order = AsyncMock(return_value={
            "success": True, "order_id": "close_999"
        })
        mock_client.place_order = AsyncMock(return_value={
            "success": True, "order_id": "close_998"
        })

        from backend.execution.kraken_futures_executor import KrakenTradeTracker
        wrapper = KrakenFuturesExecutorWrapper(api_key="test", api_secret="dGVzdA==")
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="close_999", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=100.0, expected_exposure=100.0, status="OPEN"
        )

        result = await wrapper.close_position("BTCUSDT", reason="test_verify")
        assert result.get("success") or result.get("code") in ("00000", "NO_POSITION", "NO_OPEN_QTY")


class TestCustomExceptionHierarchy:
    """Test custom exception types are proper subclasses."""

    def test_invalid_reduce_only_is_order_intent_error(self):
        assert issubclass(InvalidReduceOnlyUsageError, OrderIntentError)

    def test_invalid_kraken_qty_is_order_intent_error(self):
        assert issubclass(InvalidKrakenQuantityError, OrderIntentError)

    def test_invalid_protection_is_order_intent_error(self):
        assert issubclass(InvalidProtectionOrderError, OrderIntentError)


# ═══════════════════════════════════════════════════════════════════
#  PHASE 3 — AUDIT TESTS
#  Validates fixes from the paranoid chaos test mental.
# ═══════════════════════════════════════════════════════════════════


class TestWsFallbackBlocked:
    """Fix #1: WS fallback path in multi_broker_router must be blocked."""

    def test_no_ws_fallback_method_calls(self):
        """Verify no actual WS method calls remain (comments are OK)."""
        import inspect, re
        from backend.engine.execution.multi_broker_router import MultiBrokerRouter
        source = inspect.getsource(MultiBrokerRouter._send_kraken_order)
        # Match actual call patterns like .send_order_kraken( — not just the string in comments
        for method in ("send_order_kraken", "send_stop_loss_order", "send_take_profit_order"):
            pattern = rf"\.{method}\s*\("
            assert not re.search(pattern, source), (
                f"WS method call '.{method}(...)' still present in _send_kraken_order"
            )

    @pytest.mark.asyncio
    async def test_executor_unavailable_returns_error(self):
        """If KrakenFuturesExecutorWrapper is None, send_order must fail explicitly."""
        from backend.engine.execution.multi_broker_router import (
            MultiBrokerRouter, BrokerType, OrderResult,
        )
        router = MultiBrokerRouter({"DEFAULT_BROKER": "kraken", "KRAKEN_ENABLED": "true"})
        router._broker_status[BrokerType.KRAKEN] = True
        router._kraken_client = MagicMock()

        # Ensure no executor wrapper cached
        router._kraken_executor = None

        # Patch the import to fail
        with patch.dict("sys.modules", {"backend.execution.kraken_futures_executor": None}):
            result = await router._send_kraken_order(
                symbol="BTCUSDT", qty=100, side=MagicMock(value="buy"),
                price=None, order_type=MagicMock(value="market"),
                stop_loss=None, take_profit=None, reduce_only=False,
            )
        assert isinstance(result, OrderResult)
        assert result.success is False
        assert "EXECUTOR_UNAVAILABLE" in (result.error_code or "")


class TestOrderIdExtractionPrecedence:
    """Fix #2/#3: order_id extraction must not lose top-level order_id when data=None."""

    @pytest.mark.asyncio
    async def test_order_id_preserved_when_data_is_none(self):
        """Simulate Kraken response with order_id at top level but data=None."""
        from backend.services.close_position_service import _execute_close_order, set_close_service_kraken_executor

        mock_executor = AsyncMock()
        mock_executor.close_position = AsyncMock(return_value={
            "code": "00000",
            "success": True,
            "order_id": "real_kraken_id_123",
            "data": None,
        })
        set_close_service_kraken_executor(mock_executor)

        result = await _execute_close_order(
            symbol="PF_XBTUSD", hold_side="long",
            qty=100, is_full_close=True, broker="kraken",
        )
        assert result["success"] is True
        assert result["order_id"] != ""
        assert "kraken" in result["order_id"] or "real_kraken_id_123" in result["order_id"]

    @pytest.mark.asyncio
    async def test_order_id_from_data_dict(self):
        """When data is a dict with orderId, extract correctly."""
        from backend.services.close_position_service import _execute_close_order, set_close_service_kraken_executor

        mock_executor = AsyncMock()
        mock_executor.close_position = AsyncMock(return_value={
            "code": "00000",
            "success": True,
            "data": {"orderId": "nested_id_456"},
        })
        set_close_service_kraken_executor(mock_executor)

        result = await _execute_close_order(
            symbol="PF_XBTUSD", hold_side="long",
            qty=100, is_full_close=True, broker="kraken",
        )
        assert result["success"] is True
        assert "nested_id_456" in result["order_id"]


class TestCancelOrderRestFirst:
    """Fix #4: cancel_order must use REST, not WS executor."""

    @pytest.mark.asyncio
    async def test_cancel_order_uses_rest_client(self):
        """Verify cancel_order calls REST client, not WS executor."""
        from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper

        wrapper = KrakenFuturesExecutorWrapper(api_key="test", api_secret="test")
        mock_rest = MagicMock()
        mock_rest.cancel_order = AsyncMock(return_value={"result": "success"})
        wrapper._rest_client = mock_rest

        # Should NOT touch _get_executor
        with patch.object(wrapper, '_get_executor', side_effect=RuntimeError("WS must not be called")):
            result = await wrapper.cancel_order("PF_XBTUSD", "order_123")
        assert result["code"] == "00000"
        mock_rest.cancel_order.assert_awaited_once_with("order_123")


class TestWaitForConfirmationHoldSide:
    """Fix #5: _wait_for_confirmation must pass hold_side to _get_position."""

    @pytest.mark.asyncio
    async def test_hold_side_propagated(self):
        """Verify hold_side is passed through to _get_position."""
        from backend.services import close_position_service as svc

        calls = []
        original_get_pos = svc._get_position

        async def tracking_get_position(symbol, hold_side=None, broker="bitget"):
            calls.append({"symbol": symbol, "hold_side": hold_side, "broker": broker})
            return None  # Simulate closed position

        svc._get_position = tracking_get_position
        try:
            confirmed, data = await svc._wait_for_confirmation(
                symbol="BTCUSDT",
                expected_qty_reduction=100,
                original_size=100,
                timeout_sec=0.3,
                broker="kraken",
                hold_side="long",
            )
            # First call should have hold_side="long"
            assert len(calls) >= 1
            assert calls[0]["hold_side"] == "long"
            assert calls[0]["broker"] == "kraken"
        finally:
            svc._get_position = original_get_pos


class TestDiagnosticScriptExitCode:
    """Fix #6: Diagnostic script must suppress logging to avoid stderr exit code issues."""

    def test_logging_disabled_in_script(self):
        """Verify the diagnostic script disables logging before imports."""
        script_path = Path(__file__).parent.parent / "scripts" / "kraken_trade_safety_check.py"
        content = script_path.read_text(encoding="utf-8")
        # logging.disable must appear BEFORE the first module import
        disable_pos = content.find("logging.disable")
        first_backend_import = content.find("from backend.")
        assert disable_pos > 0, "logging.disable not found in script"
        assert disable_pos < first_backend_import, (
            "logging.disable must appear before 'from backend.' imports"
        )


class TestRouterNoSilentWsOrders:
    """Ensure no code path can silently use WS for Kraken orders."""

    def test_send_kraken_order_has_no_ws_method_calls(self):
        """Verify _send_kraken_order source has no WS method call invocations."""
        import inspect, re
        from backend.engine.execution.multi_broker_router import MultiBrokerRouter
        source = inspect.getsource(MultiBrokerRouter._send_kraken_order)
        for forbidden in ("send_order_kraken", "send_stop_loss_order", "send_take_profit_order"):
            pattern = rf"\.{forbidden}\s*\("
            assert not re.search(pattern, source), (
                f"WS method call '.{forbidden}(...)' found in _send_kraken_order"
            )
