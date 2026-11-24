"""
Unit and integration tests for SMARTTRADE Phase 3.2 — Lot 2.2
Verifies position closure lifecycle, real fill confirmation, orphan SL/TP purge,
in-flight fill detection, and SQLite synchronization for Kraken Futures.
All tests use strictly isolated mocks (zero real network calls).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)


class MockExchangeState:
    """Simulates dynamic Kraken exchange state for isolated testing."""
    def __init__(self, initial_pos=1.0, side="long"):
        self.positions = {side: float(initial_pos), ("short" if side == "long" else "long"): 0.0}
        self.open_orders = []
        self.order_statuses = {}
        self.fail_pos_query_after_close = False


@pytest.fixture
def exchange_state():
    return MockExchangeState(initial_pos=1.0, side="long")


@pytest.fixture
def mock_executor(exchange_state):
    """Create a KrakenFuturesExecutorWrapper instance with simulated exchange hooks."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock_key", api_secret="mock_secret")
    mock_client = AsyncMock()
    mock_client.cancel_order = AsyncMock(return_value={"success": True, "status": "cancelled"})
    mock_client.get_open_orders = AsyncMock(return_value=[])

    async def default_order_status(order_ids=None):
        orders = []
        for oid in (order_ids or []):
            orders.append({"order_id": oid, "status": "cancelled", "filledSize": 0.0})
        return {"result": "success", "orders": orders}

    mock_client.get_order_status = AsyncMock(side_effect=default_order_status)
    mock_client.get_fills = AsyncMock(return_value=[])
    mock_client._request = AsyncMock(return_value={"result": "success", "fills": []})

    async def mock_fetch_pos(kraken_symbol, position_side):
        if exchange_state.fail_pos_query_after_close:
            raise RuntimeError("Connection timeout querying positions")
        return exchange_state.positions.get(position_side, 0.0)

    wrapper._fetch_position_qty_strict = AsyncMock(side_effect=mock_fetch_pos)

    async def mock_place_order(symbol, side, quantity, order_type, reduce_only, client_order_id):
        # When market close executes, position on exchange drops to 0.0
        exchange_state.positions["long"] = max(0.0, exchange_state.positions.get("long", 0.0) - quantity)
        return {
            "code": "00000",
            "success": True,
            "order_id": "CLOSE-ORD-999",
            "price": 50000.0,
            "price_executed": 50000.0,
        }

    wrapper._place_order_rest = AsyncMock(side_effect=mock_place_order)
    wrapper._rest_client = mock_client
    # Simulate completed startup reconciliation for active bot testing
    wrapper._startup_reconciliation_completed = True
    wrapper._global_entry_lock = False
    wrapper._global_entry_lock_reason = None
    return wrapper


@pytest.fixture
def active_long_tracker(mock_executor):
    """Register an active LONG tracker on PF_XBTUSD with SL and TP."""
    symbol = "PF_XBTUSD"
    tracker = KrakenTradeTracker(
        order_id="ENTRY-ORD-100",
        client_oid="BOT_ENTRY_PF_XBTUSD_100",
        symbol=symbol,
        side="long",
        filled_qty=1.0,
        expected_exposure=1.0,
        status="OPEN",
        sl_order_id="SL-ORD-101",
        sl_client_oid="BOT_SL_PF_XBTUSD_101",
        tp_order_id="TP-ORD-102",
        tp_client_oid="BOT_TP_PF_XBTUSD_102",
        sl_tp_state="PROTECTED",
    )
    mock_executor._active_trackers[symbol] = tracker
    return tracker


# ── TEST 1: NOMINAL FULL CLOSE WITH REAL POSITION ZERO & SL/TP PURGE ───────────

@pytest.mark.asyncio
async def test_close_position_nominal_confirmed_by_exchange_position(mock_executor, active_long_tracker):
    """
    Test 1: Full close nominal scenario.
    - Market close order placed with reduce_only=True.
    - Live position query strictly returns 0.0 after close.
    - Both SL and TP are cancelled and confirmed absent from /openorders.
    - SQLite close_live_trade and update_protection_status called.
    - Tracker marked CLOSED and CLOSED_CONFIRMED.
    """
    symbol = "PF_XBTUSD"

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True) as mock_upd, \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()) as mock_close:

        res = await mock_executor.close_position(symbol, reason="take_profit_hit")

        assert res.get("code") == "00000" or res.get("success") is True
        assert active_long_tracker.status == "CLOSED"
        assert active_long_tracker.sl_tp_state == "CLOSED_CONFIRMED"
        assert active_long_tracker.expected_exposure == 0.0

        # Verify cancel_order called for both SL and TP
        assert mock_executor._rest_client.cancel_order.call_count == 2
        # Verify /openorders verified
        mock_executor._rest_client.get_open_orders.assert_called_with(symbol=symbol)

        # Verify SQLite synchronization
        assert mock_upd.call_count == 2
        mock_close.assert_called_once_with(
            order_id="ENTRY-ORD-100",
            close_price=50000.0,
            close_reason="take_profit_hit",
            raw_exchange=res,
        )


# ── TEST 2: PRE-FLIGHT GATE ALLOWS RE-ENTRY AFTER CLEAN CLOSE ─────────────────

@pytest.mark.asyncio
async def test_close_position_preflight_gate_clean_carnet(mock_executor, active_long_tracker):
    """
    Test 2: After a clean close, the carnet is empty and _check_preflight_open_orders_gate
    passes cleanly (True, None), allowing future bot entries without false alarms.
    """
    symbol = "PF_XBTUSD"

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()):
        await mock_executor.close_position(symbol)

    # Now verify preflight gate on same symbol
    passed, err = await mock_executor._check_preflight_open_orders_gate(symbol)
    assert passed is True
    assert err is None


# ── TEST 3: MARKET ORDER FAILS -> PROTECTIONS PRESERVED (FAIL-CLOSED) ─────────

@pytest.mark.asyncio
async def test_close_position_market_order_fails_preserves_sl_tp(mock_executor, active_long_tracker):
    """
    Test 3: If market close order is rejected by Kraken, protections MUST NOT be cancelled!
    Tracker remains OPEN and position is not closed in SQLite.
    """
    symbol = "PF_XBTUSD"
    mock_executor._place_order_rest = AsyncMock(return_value={
        "code": "ERROR",
        "success": False,
        "msg": "Insufficient margin / market rejected",
    })

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status") as mock_upd, \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade") as mock_close:

        res = await mock_executor.close_position(symbol)

        assert res.get("success") is False
        # SL/TP must NOT be cancelled
        assert mock_executor._rest_client.cancel_order.call_count == 0
        # Tracker remains OPEN
        assert active_long_tracker.status == "OPEN"
        assert active_long_tracker.expected_exposure == 1.0
        # No DB close
        mock_upd.assert_not_called()
        mock_close.assert_not_called()


# ── TEST 4: MARKET ORDER ACCEPTED BUT POSITION UNVERIFIED / NON-ZERO ──────────

@pytest.mark.asyncio
async def test_close_position_market_fills_zero_aborts_protection_cancel(mock_executor, active_long_tracker, exchange_state):
    """
    Test 4: Market order accepted (code=00000) but live position on Kraken is still > 0.
    FAIL-CLOSED: protections must NOT be purged!
    """
    symbol = "PF_XBTUSD"
    # Market order returns success, but position stays at 1.0 (unfilled/resting)
    mock_executor._place_order_rest = AsyncMock(return_value={"code": "00000", "success": True})
    exchange_state.positions["long"] = 1.0

    res = await mock_executor.close_position(symbol)

    assert res.get("code") == "CLOSE_INCOMPLETE"
    assert res.get("closed") is False
    assert mock_executor._rest_client.cancel_order.call_count == 0
    assert active_long_tracker.status == "OPEN"
    assert active_long_tracker.expected_exposure == 1.0


# ── TEST 5: POSITION INSPECTION FAILS -> ABORT PROTECTION PURGE ───────────────

@pytest.mark.asyncio
async def test_close_position_position_query_fails_preserves_protections(mock_executor, active_long_tracker, exchange_state):
    """
    Test 5: Live position query raises network error after market close.
    FAIL-CLOSED: cannot prove position is 0 -> abort protection cancel.
    """
    symbol = "PF_XBTUSD"

    async def mock_place_with_fail(*args, **kwargs):
        exchange_state.fail_pos_query_after_close = True
        return {"code": "00000", "success": True}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place_with_fail)

    res = await mock_executor.close_position(symbol)

    assert res.get("code") in ("CLOSE_POSITION_VERIFICATION_FAILED_QUARANTINED", "CLOSE_CONFIRMATION_FAILED")
    assert res.get("success") is False
    assert mock_executor._rest_client.cancel_order.call_count == 0
    assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
    assert active_long_tracker.sl_tp_state == "POSITION_VERIFICATION_FAILED"
    assert symbol in mock_executor._reconciliation_required_symbols


# ── TEST 6: SL CANCEL TIMEOUT -> QUARANTINE & GATE BLOCKS FUTURE ENTRY ────────

@pytest.mark.asyncio
async def test_close_position_sl_cancel_timeout_triggers_quarantine_and_blocks_entry(mock_executor, active_long_tracker):
    """
    Test 6: Post-close cancel of SL throws TimeoutError or leaves order in /openorders.
    Symbol is placed in quarantine (_reconciliation_required_symbols).
    Subsequent entry orders MUST be unconditionally blocked.
    """
    symbol = "PF_XBTUSD"
    # Cancel order throws TimeoutError
    mock_executor.cancel_order = AsyncMock(side_effect=TimeoutError("Kraken cancel timeout"))
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": "SL-ORD-101", "cliOrdId": "BOT_SL_PF_XBTUSD_101"}
    ])

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status") as mock_upd, \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade"), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined") as mock_q:

        res = await mock_executor.close_position(symbol)

        assert active_long_tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"
        assert symbol in mock_executor._reconciliation_required_symbols

        # SQLite was informed of the orphan and quarantine
        mock_upd.assert_any_call(
            symbol=symbol,
            protection_type="sl",
            status="ORPHAN_UNCONFIRMED",
            order_id="ENTRY-ORD-100",
            client_oid="BOT_SL_PF_XBTUSD_101",
            protection_order_id="SL-ORD-101",
            error_message="Kraken cancel timeout",
            broker="kraken",
        )
        mock_q.assert_called_once()

        # Verify gate: subsequent entry MUST be blocked by quarantine guard
        entry_res = await mock_executor.place_order(symbol=symbol, side="buy", quantity=1.0)
        assert entry_res.get("success") is False
        assert entry_res.get("code") == "ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"


# ── TEST 7: NOT_FOUND ACCEPTED IF CONFIRMED ABSENT FROM /openorders ────────────

@pytest.mark.asyncio
async def test_close_position_not_found_with_confirmed_absence_succeeds(mock_executor, active_long_tracker):
    """
    Test 7: Kraken cancel returns 'notFound' (e.g. SL was already cancelled).
    If /openorders confirms it is absent and order has filled_size=0, nominal close succeeds.
    """
    symbol = "PF_XBTUSD"
    # Client cancel returns notFound
    mock_executor._rest_client.cancel_order = AsyncMock(return_value={"success": False, "status": "notFound"})
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[])
    mock_executor._fetch_order_status_strict = AsyncMock(return_value={"status": "cancelled", "filledSize": 0})

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status"), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade"):

        res = await mock_executor.close_position(symbol)

        assert active_long_tracker.status == "CLOSED"
        assert active_long_tracker.sl_tp_state == "CLOSED_CONFIRMED"
        assert symbol not in mock_executor._reconciliation_required_symbols


# ── TEST 8: IN-FLIGHT FILL DETECTED -> AUDIT & OVERFILL CHECK ─────────────────

@pytest.mark.asyncio
async def test_close_position_in_flight_fill_detected_triggers_audit(mock_executor, active_long_tracker, exchange_state):
    """
    Test 8: SL was filled concurrently (in-flight fill).
    Order is absent from /openorders, but order status shows filled_size > 0.
    Position inspection reveals an opposite short position (overfill conflict).
    Triggers critical alert and quarantine without automatic compensation.
    """
    symbol = "PF_XBTUSD"
    mock_executor._rest_client.cancel_order = AsyncMock(return_value={"success": False, "status": "notFound"})
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[])
    # Order was filled!
    mock_executor._fetch_order_status_strict = AsyncMock(return_value={"status": "filled", "filledSize": 1.0})

    # Simulating that after close order + in-flight fill, short side has 1.0 (overfill conflict)
    async def mock_pos_in_flight(sym, side):
        # Initial check before close order: long=1.0, short=0.0
        # Check after in-flight fill: short=1.0
        if exchange_state.positions.get("long", 0.0) == 0.0 and side == "short":
            return 1.0
        return exchange_state.positions.get(side, 0.0)

    mock_executor._fetch_position_qty_strict = AsyncMock(side_effect=mock_pos_in_flight)

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status"), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade"), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined"):

        res = await mock_executor.close_position(symbol)

        assert active_long_tracker.sl_tp_state in ("FILLED_IN_FLIGHT_OVERFILL", "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED")
        assert symbol in mock_executor._reconciliation_required_symbols


# ── TEST 9: PARTIAL CLOSE SYNCHRONIZATION (MODULE 1) ──────────────────────────

@pytest.mark.asyncio
async def test_close_position_partial_close_preserves_protections(mock_executor, active_long_tracker, exchange_state):
    """
    Test 9: Partial close (quantity=0.5 < expected_exposure=1.0).
    Exposure reduced to 0.5, tracker remains OPEN.
    New SL calibrated to 0.5 and verified in carnet.
    Old SL cancelled. SQLite trade quantity updated to 0.5. SQLite trade NOT closed.
    """
    symbol = "PF_XBTUSD"
    active_long_tracker.sl_price = 48000.0
    active_long_tracker.tp_id = None
    active_long_tracker.tp_order_id = None

    new_sl_oid = "NEW-SL-50"
    mock_executor._rest_client.place_order = AsyncMock(return_value={"result": "success", "order_id": new_sl_oid})
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": new_sl_oid, "status": "open", "side": "sell", "size": 0.5, "stopPrice": 48000.0, "reduceOnly": True}
    ])
    mock_executor.cancel_order = AsyncMock(return_value={"success": True, "data": {"status": "cancelled"}})

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True) as mock_upd, \
         patch("backend.services.live_trades_service.LiveTradesService.update_trade_quantity", return_value=True) as mock_upd_qty, \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade") as mock_close:

        res = await mock_executor.close_position(symbol, quantity=0.5)

        assert res.get("code") == "PARTIAL_CLOSE"
        assert res.get("remaining_qty") == 0.5
        assert active_long_tracker.status == "OPEN"
        assert active_long_tracker.expected_exposure == 0.5
        assert active_long_tracker.sl_order_id == new_sl_oid
        # Old SL was cancelled
        mock_executor.cancel_order.assert_any_call(order_id="SL-ORD-101", cli_ord_id="BOT_SL_PF_XBTUSD_101")
        # Trade quantity was updated in SQLite
        mock_upd_qty.assert_called_once()
        # No DB full close
        mock_close.assert_not_called()


# ── TEST 10: SQLITE ERROR TRIGGERS QUARANTINE & IDEMPOTENCE ───────────────────

@pytest.mark.asyncio
async def test_close_position_sqlite_error_triggers_quarantine(mock_executor, active_long_tracker):
    """
    Test 10A: If SQLite synchronization fails after market close, symbol is placed
    in quarantine for mandatory manual reconciliation (Garde-fou 3).
    """
    symbol = "PF_XBTUSD"

    with patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", side_effect=RuntimeError("SQLite database locked")):
        res = await mock_executor.close_position(symbol)

        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"


@pytest.mark.asyncio
async def test_close_position_idempotent_refusal_when_already_closed(mock_executor, active_long_tracker):
    """
    Test 10B: Calling close_position on a tracker already marked CLOSED is refused fail-closed.
    """
    symbol = "PF_XBTUSD"
    active_long_tracker.status = "CLOSED"

    res = await mock_executor.close_position(symbol)

    assert res.get("code") == "CLOSE_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"
    assert res.get("success") is False
    assert mock_executor._rest_client.cancel_order.call_count == 0


# ── TEST 11: OPPOSITE POSITION QUERY ERROR TRIGGERS FAIL-CLOSED QUARANTINE ───

@pytest.mark.asyncio
async def test_close_position_opposite_pos_query_error_triggers_quarantine(mock_executor, active_long_tracker, exchange_state):
    """
    Test 11: If querying opposite position raises an exception, the code must NEVER
    assume 0.0! FAIL-CLOSED: symbol must be immediately quarantined.
    """
    symbol = "PF_XBTUSD"
    fail_opp = False

    async def mock_pos_opp_error(sym, side):
        if side == "short" and fail_opp:
            raise RuntimeError("Opposite position query timeout")
        return exchange_state.positions.get(side, 0.0)

    mock_executor._fetch_position_qty_strict = AsyncMock(side_effect=mock_pos_opp_error)

    async def mock_place_with_opp_fail(*args, **kwargs):
        nonlocal fail_opp
        fail_opp = True
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place_with_opp_fail)

    res = await mock_executor.close_position(symbol)

    assert res.get("code") == "CLOSE_OPPOSITE_VERIFICATION_FAILED"
    assert res.get("success") is False
    assert symbol in mock_executor._reconciliation_required_symbols
    assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"


# ── TEST 12: IN-FLIGHT FILL DETECTED BY CLIENT_OID ───────────────────────────

@pytest.mark.asyncio
async def test_close_position_in_flight_detected_by_client_oid(mock_executor, active_long_tracker):
    """
    Test 12: Protection has no order_id (sl_order_id=None), but sl_client_oid exists.
    In-flight fill is detected from fills using cliOrdId.
    """
    symbol = "PF_XBTUSD"
    active_long_tracker.sl_order_id = None  # Only client_oid available
    mock_executor._rest_client.cancel_order = AsyncMock(return_value={"success": False, "status": "notFound"})
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[])

    # Fills return fill matching client_oid
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[
        {"order_id": "UNKNOWN_ID", "cliOrdId": "BOT_SL_PF_XBTUSD_101", "size": 1.0, "price": 49000.0}
    ])

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status"), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade"), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined"):

        res = await mock_executor.close_position(symbol)

        assert active_long_tracker.sl_tp_state == "CLOSED_CONFIRMED"


# ── TEST 13: SQLITE FALSE OR NONE TRIGGERS MANDATORY QUARANTINE ──────────────

@pytest.mark.asyncio
async def test_close_position_sqlite_update_returns_false_triggers_quarantine(mock_executor, active_long_tracker):
    """
    Test 13A: If update_protection_status returns False, quarantine is triggered.
    """
    symbol = "PF_XBTUSD"

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=False), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()):

        res = await mock_executor.close_position(symbol)

        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"


@pytest.mark.asyncio
async def test_close_position_sqlite_close_trade_returns_none_triggers_quarantine(mock_executor, active_long_tracker):
    """
    Test 13B: If close_live_trade returns None, quarantine is triggered.
    """
    symbol = "PF_XBTUSD"

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=None):

        res = await mock_executor.close_position(symbol)

        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"


# ── TEST 14: CLOSE PRICE VALIDATED FROM WEIGHTED AVERAGE OF FILLS ─────────────

@pytest.mark.asyncio
async def test_close_position_close_price_from_fills_weighted_average(mock_executor, active_long_tracker, exchange_state):
    """
    Test 14: Close order has 2 fills (0.6 @ 50,000 and 0.4 @ 51,000).
    Weighted average price is 50,400.0, which is passed to close_live_trade.
    """
    symbol = "PF_XBTUSD"
    close_oid = "CLOSE-ORD-FILLS-TEST"

    async def mock_place_fills(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {
            "code": "00000",
            "success": True,
            "order_id": close_oid,
            "price": 0.0,
        }

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place_fills)

    mock_executor._fetch_fills_strict = AsyncMock(return_value=[
        {"order_id": close_oid, "size": 0.6, "price": 50000.0},
        {"order_id": close_oid, "size": 0.4, "price": 51000.0},
    ])

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()) as mock_close:

        res = await mock_executor.close_position(symbol)

        mock_close.assert_called_once()
        call_kwargs = mock_close.call_args[1]
        assert abs(call_kwargs["close_price"] - 50400.0) < 1e-4


# ── TEST 15: PARTIAL CLOSE EXPOSURE UPDATED FROM REAL EXECUTION ──────────────

@pytest.mark.asyncio
async def test_close_position_partial_close_exposure_from_real_fills(mock_executor, active_long_tracker, exchange_state):
    """
    Test 15: Partial close requested for 0.5, but fills/live position show 0.7 remains.
    Tracker expected_exposure must strictly reflect the real live position (0.7).
    """
    symbol = "PF_XBTUSD"
    active_long_tracker.sl_price = 48000.0
    active_long_tracker.tp_id = None
    active_long_tracker.tp_order_id = None

    async def mock_place_partial(*args, **kwargs):
        # After partial order executes, 0.7 remains
        exchange_state.positions["long"] = 0.7
        return {"code": "00000", "success": True}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place_partial)
    new_sl_oid = "NEW-SL-70"
    mock_executor._rest_client.place_order = AsyncMock(return_value={"result": "success", "order_id": new_sl_oid})
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": new_sl_oid, "status": "open", "side": "sell", "size": 0.7, "stopPrice": 48000.0, "reduceOnly": True}
    ])
    mock_executor.cancel_order = AsyncMock(return_value={"success": True, "data": {"status": "cancelled"}})

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_trade_quantity", return_value=True) as mock_upd_qty, \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade") as mock_close:

        res = await mock_executor.close_position(symbol, quantity=0.5)

        assert res.get("code") == "PARTIAL_CLOSE"
        assert active_long_tracker.status == "OPEN"
        assert abs(active_long_tracker.expected_exposure - 0.7) < 1e-6
        assert abs(res.get("remaining_qty") - 0.7) < 1e-6


# ── TESTS SAFETY REVIEW: 4 CIBLES CRITIQUES ───────────────────────────────────

@pytest.mark.asyncio
async def test_close_position_negative_fill_size_weighted_price(mock_executor, active_long_tracker, exchange_state):
    """
    Safety Point 3: Kraken Futures SELL fills have negative sizes (e.g. -0.5).
    Weighted average price calculation must use absolute sizes so that tot_val and tot_sz
    yield a strictly positive close price (e.g. 88,500.0, not -88,500.0 or 0.0 fallback).
    """
    symbol = "PF_XBTUSD"
    close_oid = "CLOSE-ORD-NEG-SIZES"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True, "order_id": close_oid}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[
        {"order_id": close_oid, "size": -0.6, "price": 88000.0},
        {"order_id": close_oid, "size": -0.4, "price": 89000.0},
    ])

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()) as mock_close:

        res = await mock_executor.close_position(symbol)

        mock_close.assert_called_once()
        close_price = mock_close.call_args[1]["close_price"]
        assert close_price > 0.0
        assert abs(close_price - 88400.0) < 1e-4


@pytest.mark.asyncio
async def test_close_position_order_status_query_exception_triggers_quarantine(mock_executor, active_long_tracker, exchange_state):
    """
    Safety Point 1: Silent exceptions eliminated.
    If _fetch_order_status_strict raises an Exception during protection verification,
    it must NOT be silently swallowed; failure to verify triggers quarantine and
    sets tracker.sl_tp_state to CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED.
    """
    symbol = "PF_XBTUSD"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[])
    mock_executor._fetch_order_status_strict = AsyncMock(side_effect=RuntimeError("Kraken status API timeout"))

    with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined") as mock_q:
        res = await mock_executor.close_position(symbol)

        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"
        mock_q.assert_called()


@pytest.mark.asyncio
async def test_close_position_fills_query_exception_triggers_quarantine(mock_executor, active_long_tracker, exchange_state):
    """
    Safety Point 2: Confirmed absent fills vs impossible to retrieve fills.
    If _fetch_fills_strict raises an Exception during protection verification,
    the impossible-to-retrieve fills state must trigger quarantine.
    """
    symbol = "PF_XBTUSD"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[])
    mock_executor._fetch_order_status_strict = AsyncMock(return_value=None)
    mock_executor._fetch_fills_strict = AsyncMock(side_effect=RuntimeError("Fills API unreachable 500"))

    with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined") as mock_q:
        res = await mock_executor.close_position(symbol)

        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"
        mock_q.assert_called()


@pytest.mark.asyncio
async def test_close_position_open_orders_detected_by_alternate_aliases(mock_executor, active_long_tracker, exchange_state):
    """
    Safety Point 4: Validate /openorders structure with exhaustive ID aliases.
    When open order is present with alternate aliases ('id' instead of 'order_id',
    'clientId' or 'client_order_id' instead of 'cliOrdId', or nested in 'order'),
    it must be detected as still open, triggering quarantine.
    """
    symbol = "PF_XBTUSD"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    # Order still present under alternate aliases matching active_long_tracker SL ("SL-ORD-101" / "BOT_SL_PF_XBTUSD_101")
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[
        {"order": {"id": "SL-ORD-101", "clientId": "BOT_SL_PF_XBTUSD_101", "symbol": "PF_XBTUSD"}}
    ])

    with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined") as mock_q:
        res = await mock_executor.close_position(symbol)

        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"
        mock_q.assert_called()


# ── TESTS DERNIÈRE REVUE P0 (4 POINTS DE CONTRÔLE) ───────────────────────────

@pytest.mark.asyncio
async def test_fetch_order_status_strict_5_distinct_cases(mock_executor):
    """
    Point 1: Verify all 5 cases of _fetch_order_status_strict:
    1. Valid response with exploitable status -> dict returned
    2. Valid response without corresponding order -> None returned
    3. Empty or malformed response -> raises RuntimeError
    4. API error response -> raises RuntimeError
    5. Network exception or timeout -> raises
    """
    client = mock_executor._rest_client

    # Case 1: Valid response with exploitable status
    client.get_order_status = AsyncMock(return_value={
        "result": "success",
        "orders": [{"order": {"order_id": "ORD-1", "status": "filled", "filledSize": 1.0}}]
    })
    res1 = await mock_executor._fetch_order_status_strict("ORD-1")
    assert res1 is not None
    assert res1.get("status") == "filled"

    # Case 2: Valid response without corresponding order
    client.get_order_status = AsyncMock(return_value={
        "result": "success",
        "orders": [{"order": {"order_id": "ORD-OTHER", "status": "cancelled"}}]
    })
    res2 = await mock_executor._fetch_order_status_strict("ORD-1")
    assert res2 is None

    # Case 3: Empty or malformed response (never assimilated to confirmed absence)
    client.get_order_status = AsyncMock(return_value={})
    with pytest.raises(RuntimeError, match="Malformed or empty"):
        await mock_executor._fetch_order_status_strict("ORD-1")

    client.get_order_status = AsyncMock(return_value={"result": "success", "orders": "invalid_not_a_list"})
    with pytest.raises(RuntimeError, match="Malformed 'orders' field"):
        await mock_executor._fetch_order_status_strict("ORD-1")

    # Case 4: API error response
    client.get_order_status = AsyncMock(return_value={"result": "error", "error": "OrderNotFoundOnExchange"})
    with pytest.raises(RuntimeError, match="Kraken get_order_status API error"):
        await mock_executor._fetch_order_status_strict("ORD-1")

    # Case 5: Network exception or timeout
    client.get_order_status = AsyncMock(side_effect=TimeoutError("Connection timed out"))
    with pytest.raises(TimeoutError):
        await mock_executor._fetch_order_status_strict("ORD-1")


@pytest.mark.asyncio
async def test_open_orders_normalization_and_rejection_of_ambiguous_dicts(mock_executor, active_long_tracker, exchange_state):
    """
    Point 2: Normalisation de /openorders.
    - Accepts documented envelopes: list, {"openOrders": [...]}, {"orders": [...]}, {"data": [...]}.
    - Rejects ambiguous or unknown dicts (e.g. {"status": "ok"}), triggering quarantine fail-closed.
    - Never iterates over dict keys.
    """
    symbol = "PF_XBTUSD"

    # Helper function tests
    assert mock_executor._normalize_open_orders(None) is None
    assert mock_executor._normalize_open_orders({"openOrders": [{"order_id": "1"}]}) == [{"order_id": "1"}]
    assert mock_executor._normalize_open_orders({"orders": [{"order_id": "2"}]}) == [{"order_id": "2"}]
    assert mock_executor._normalize_open_orders({"data": [{"order_id": "3"}]}) == [{"order_id": "3"}]
    assert mock_executor._normalize_open_orders({"unknown_envelope": [{"order_id": "4"}]}) is None
    assert mock_executor._normalize_open_orders([1, 2, 3]) is None

    # Ambiguous dict returned by get_open_orders triggers quarantine fail-closed
    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value={"ambiguousKey": "not_an_orders_list"})

    with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined") as mock_q:
        res = await mock_executor.close_position(symbol)
        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"
        mock_q.assert_called()


@pytest.mark.asyncio
async def test_close_position_unknown_order_status_triggers_quarantine(mock_executor, active_long_tracker, exchange_state):
    """
    Point 3: Unknown / unrecognized order status must NEVER be assumed cancelled with 0 fills.
    Triggers fail-closed quarantine.
    """
    symbol = "PF_XBTUSD"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[])
    # Status returned with an unknown/unrecognized status string
    mock_executor._fetch_order_status_strict = AsyncMock(return_value={"status": "UNKNOWN_CUSTOM_STATUS", "filledSize": 0})
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[])

    with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined") as mock_q:
        res = await mock_executor.close_position(symbol)
        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"
        mock_q.assert_called()


@pytest.mark.asyncio
async def test_sqlite_mark_trade_quarantined_failure_flagged_and_locked(mock_executor, active_long_tracker, exchange_state):
    """
    Point 4: When mark_trade_quarantined returns False, the failure is explicitly flagged
    in reconciliation notes and logger, and the symbol remains locked in memory.
    """
    symbol = "PF_XBTUSD"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[])
    # Trigger cancellation failure by having fills query fail
    mock_executor._fetch_order_status_strict = AsyncMock(return_value=None)
    mock_executor._fetch_fills_strict = AsyncMock(side_effect=RuntimeError("Fills timeout"))

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True),          patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()),          patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=False):

        res = await mock_executor.close_position(symbol)

        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert "SQLITE_SYNC_FAILED" in (active_long_tracker.reconciliation_notes or "")


# ── TESTS P0.2 : RÉPONSES CONTENANT DES ÉLÉMENTS MAL FORMÉS ──────────────────

@pytest.mark.asyncio
async def test_fetch_order_status_strict_malformed_elements_rejected(mock_executor):
    """
    P0.2: Rejection of malformed elements in _fetch_order_status_strict:
    - Non-dict element in 'orders' list raises RuntimeError (not silently skipped)
    - Non-dict nested 'order' raises RuntimeError
    - Order dict missing 'status' field raises RuntimeError
    """
    client = mock_executor._rest_client

    # 1. Non-dict element in 'orders'
    client.get_order_status = AsyncMock(return_value={
        "result": "success",
        "orders": ["corrupted_string_order"]
    })
    with pytest.raises(RuntimeError, match="Malformed element in get_order_status 'orders' list"):
        await mock_executor._fetch_order_status_strict("ORD-1")

    # 2. Non-dict nested 'order'
    client.get_order_status = AsyncMock(return_value={
        "result": "success",
        "orders": [{"order": 12345}]
    })
    with pytest.raises(RuntimeError, match="Malformed nested 'order'"):
        await mock_executor._fetch_order_status_strict("ORD-1")

    # 3. Order dict missing 'status' field
    client.get_order_status = AsyncMock(return_value={
        "result": "success",
        "orders": [{"orderId": "ORD-1", "filledSize": 0}]  # status missing
    })
    with pytest.raises(RuntimeError, match="missing 'status' field"):
        await mock_executor._fetch_order_status_strict("ORD-1")


def test_normalize_open_orders_contradictory_envelopes_and_malformed_nested(mock_executor):
    """
    P0.2: Rejection in _normalize_open_orders:
    - Multiple contradictory envelopes (e.g. openOrders != orders) rejected (returns None)
    - Malformed nested 'order' (not a dict) rejected (returns None)
    - Ambiguous non-dict or primitive returns None, never converts to empty list []
    """
    # 1. Multiple contradictory envelopes
    contradictory_dict = {
        "openOrders": [{"order_id": "ORD-1"}],
        "orders": [{"order_id": "ORD-2"}],
    }
    assert mock_executor._normalize_open_orders(contradictory_dict) is None

    # Multiple identical envelopes allowed
    consistent_dict = {
        "openOrders": [{"order_id": "ORD-1"}],
        "orders": [{"order_id": "ORD-1"}],
    }
    assert mock_executor._normalize_open_orders(consistent_dict) == [{"order_id": "ORD-1"}]

    # 2. Malformed nested 'order' (not a dict)
    malformed_nested = [{"order": "not_a_dict_order"}]
    assert mock_executor._normalize_open_orders(malformed_nested) is None

    malformed_nested_in_envelope = {"openOrders": [{"order": 42}]}
    assert mock_executor._normalize_open_orders(malformed_nested_in_envelope) is None

    # 3. Inexploitable structures never converted to []
    assert mock_executor._normalize_open_orders({}) is None
    assert mock_executor._normalize_open_orders({"order": {}}) is None
    assert mock_executor._normalize_open_orders({"status": "ok"}) is None
    assert mock_executor._normalize_open_orders("invalid_string") is None
    assert mock_executor._normalize_open_orders(12345) is None

    # 4. Strict identifier validation on each order item (rejection of empty/invalid/missing ID)
    assert mock_executor._normalize_open_orders([{}]) is None
    assert mock_executor._normalize_open_orders([{"order": {}}]) is None
    assert mock_executor._normalize_open_orders({"openOrders": [{}]}) is None
    assert mock_executor._normalize_open_orders({"openOrders": [{"order": {}}]}) is None
    assert mock_executor._normalize_open_orders([{"order_id": ""}]) is None
    assert mock_executor._normalize_open_orders([{"order_id": "   "}]) is None
    assert mock_executor._normalize_open_orders([{"order_id": None}]) is None
    assert mock_executor._normalize_open_orders([{"order_id": True}]) is None
    assert mock_executor._normalize_open_orders([{"order_id": False}]) is None
    assert mock_executor._normalize_open_orders([{"order_id": []}]) is None
    assert mock_executor._normalize_open_orders([{"order_id": {}}]) is None

    # 5. Valid order identifiers accepted across all recognized aliases
    assert mock_executor._normalize_open_orders([{"order_id": 12345}]) == [{"order_id": 12345}]
    assert mock_executor._normalize_open_orders([{"cliOrdId": "BOT_1"}]) == [{"cliOrdId": "BOT_1"}]
    assert mock_executor._normalize_open_orders([{"orderId": "UUID-1"}]) == [{"orderId": "UUID-1"}]
    assert mock_executor._normalize_open_orders([{"id": "UUID-2"}]) == [{"id": "UUID-2"}]
    assert mock_executor._normalize_open_orders([{"client_order_id": "BOT_2"}]) == [{"client_order_id": "BOT_2"}]
    assert mock_executor._normalize_open_orders([{"clientId": "BOT_3"}]) == [{"clientId": "BOT_3"}]
    assert mock_executor._normalize_open_orders([{"clOrdId": "BOT_4"}]) == [{"clOrdId": "BOT_4"}]


# ══════════════════════════════════════════════════════════════════════════════
# NEW P0 TESTS: P0.1, P0.2, P0.3 & P1 (STRICT AUDIT DEFECT VERIFICATION)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_p0_1_order_absent_from_order_status_with_empty_fills_triggers_quarantine(mock_executor, active_long_tracker):
    """
    Test 1 (P0.1): Protection order is absent from /orders/status and fills are empty.
    Absence from /orders/status MUST NEVER be assumed as confirmed cancellation.
    Triggers fail-closed quarantine (CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED).
    """
    symbol = "PF_XBTUSD"

    # get_order_status returns empty orders list (order absent)
    mock_executor._rest_client.get_order_status = AsyncMock(return_value={"result": "success", "orders": []})
    mock_executor._rest_client.get_fills = AsyncMock(return_value=[])

    with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined") as mock_q:
        res = await mock_executor.close_position(symbol)

        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert active_long_tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"
        assert active_long_tracker.sl_tp_state != "CLOSED_CONFIRMED"
        mock_q.assert_called()


@pytest.mark.asyncio
async def test_p0_1_unknown_order_status_with_empty_fills_triggers_quarantine(mock_executor, active_long_tracker):
    """
    Test 2 (P0.1): Protection order has unknown/unrecognized status with empty fills.
    MUST NEVER be assumed cancelled. Triggers fail-closed quarantine.
    """
    symbol = "PF_XBTUSD"

    # get_order_status returns unrecognised status
    mock_executor._rest_client.get_order_status = AsyncMock(return_value={
        "result": "success",
        "orders": [{"order_id": "SL-ORD-101", "status": "CUSTOM_PENDING_STATE", "filledSize": 0.0}]
    })
    mock_executor._rest_client.get_fills = AsyncMock(return_value=[])

    with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined") as mock_q:
        res = await mock_executor.close_position(symbol)

        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert active_long_tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"
        assert active_long_tracker.sl_tp_state != "CLOSED_CONFIRMED"
        mock_q.assert_called()


@pytest.mark.asyncio
async def test_p0_2_network_error_masked_by_client_get_fills_propagates_in_strict(mock_executor):
    """
    Test 3 (P0.2): Network error occurring during fills fetch MUST NOT be converted to []
    by _fetch_fills_strict. It must propagate as an exception.
    """
    # 1. When client._request raises, _fetch_fills_strict propagates the exception
    mock_executor._rest_client._request = AsyncMock(side_effect=ConnectionResetError("Socket reset by peer"))
    # Ensure get_fills is not mocked so it falls back to _request or get_fills(raise_errors=True)
    if hasattr(mock_executor._rest_client, "get_fills"):
        delattr(mock_executor._rest_client, "get_fills")

    with pytest.raises(ConnectionResetError, match="Socket reset by peer"):
        await mock_executor._fetch_fills_strict()

    # 2. executor.get_fills with raise_errors=True also propagates
    with pytest.raises(ConnectionResetError):
        await mock_executor.get_fills(raise_errors=True)

    # 3. executor.get_fills with raise_errors=False returns [] for legacy backward compatibility
    assert await mock_executor.get_fills(raise_errors=False) == []


@pytest.mark.asyncio
async def test_p0_2_fills_response_missing_fills_key_raises(mock_executor):
    """
    Test 4 (P0.2): Fills response missing 'fills' key MUST raise RuntimeError.
    """
    mock_executor._rest_client._request = AsyncMock(return_value={"result": "success", "serverTime": "123"})
    if hasattr(mock_executor._rest_client, "get_fills"):
        delattr(mock_executor._rest_client, "get_fills")

    with pytest.raises(RuntimeError, match="missing 'fills' key"):
        await mock_executor._fetch_fills_strict()


@pytest.mark.asyncio
async def test_p0_2_fills_response_genuinely_empty_and_valid(mock_executor):
    """
    Test 5 (P0.2): Fills response with genuinely empty and valid structure returns [].
    """
    mock_executor._rest_client._request = AsyncMock(return_value={"result": "success", "fills": []})
    if hasattr(mock_executor._rest_client, "get_fills"):
        delattr(mock_executor._rest_client, "get_fills")

    res = await mock_executor._fetch_fills_strict()
    assert res == []


@pytest.mark.asyncio
async def test_p0_3_close_live_trade_error_triggers_quarantine_and_not_closed_confirmed(mock_executor, active_long_tracker):
    """
    Test 6 (P0.3): If SQLite close_live_trade returns None, sl_tp_state MUST NOT be
    CLOSED_CONFIRMED. It must enter quarantine state.
    """
    symbol = "PF_XBTUSD"

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=None), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True) as mock_q:

        res = await mock_executor.close_position(symbol)

        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert active_long_tracker.sl_tp_state == "SQLITE_SYNC_FAILED_QUARANTINE"
        assert active_long_tracker.sl_tp_state != "CLOSED_CONFIRMED"
        mock_q.assert_called()


@pytest.mark.asyncio
async def test_p0_3_update_protection_status_error_triggers_quarantine_and_not_closed_confirmed(mock_executor, active_long_tracker):
    """
    Test 7 (P0.3): If SQLite update_protection_status returns False, sl_tp_state MUST NOT be
    CLOSED_CONFIRMED. It must enter quarantine state.
    """
    symbol = "PF_XBTUSD"

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=False), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True) as mock_q:

        res = await mock_executor.close_position(symbol)

        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert active_long_tracker.sl_tp_state == "SQLITE_SYNC_FAILED_QUARANTINE"
        assert active_long_tracker.sl_tp_state != "CLOSED_CONFIRMED"
        mock_q.assert_called()


@pytest.mark.asyncio
async def test_p0_3_quarantine_persistence_failure_logs_warning_and_keeps_memory_quarantine(mock_executor, active_long_tracker):
    """
    Test 8 (P0.3): If mark_trade_quarantined returns False, in-memory quarantine remains active
    and a specific warning note is recorded indicating restart protection is not guaranteed.
    """
    symbol = "PF_XBTUSD"

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=None), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=False):

        res = await mock_executor.close_position(symbol)

        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert "QUARANTINE_PERSISTENCE_FAILED_RESTART_NOT_GUARANTEED" in (active_long_tracker.reconciliation_notes or "")


@pytest.mark.asyncio
async def test_p0_3_inconsistency_tracker_status_and_sl_tp_state_prevented(mock_executor, active_long_tracker):
    """
    Test 9 (P0.3): Inconsistency prevention: tracker.status == MANUAL_RECONCILIATION_REQUIRED
    can NEVER co-exist with sl_tp_state == CLOSED_CONFIRMED.
    """
    symbol = "PF_XBTUSD"

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=False), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=None), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True):

        res = await mock_executor.close_position(symbol)

        if active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED":
            assert active_long_tracker.sl_tp_state != "CLOSED_CONFIRMED"


@pytest.mark.asyncio
async def test_p0_3_no_new_entry_while_quarantine_is_active(mock_executor):
    """
    Test 10 (P0.3): No new entry is permitted while a symbol is in quarantine.
    place_order must return ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED.
    """
    symbol = "PF_XBTUSD"
    mock_executor._reconciliation_required_symbols.add(symbol)

    res = await mock_executor.place_order(
        symbol=symbol,
        side="buy",
        quantity=1.0,
        order_type="market",
    )

    assert res.get("success") is False
    assert res.get("code") == "ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"


@pytest.mark.asyncio
async def test_p1_close_price_missing_handled_cleanly_without_zero_or_fake_pnl(mock_executor, active_long_tracker, exchange_state):
    """
    Test 11 (P1): When no valid close price is available (price is 0.0 or absent),
    close_price is NOT silently passed as 0.0; it is marked missing/unavailable
    and position closure is cleanly separated from financial valuation.
    """
    symbol = "PF_XBTUSD"

    async def mock_place_no_price(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {
            "code": "00000",
            "success": True,
            "order_id": "CLOSE-ORD-NO-PRICE",
            "price": 0.0,
        }

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place_no_price)
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[])

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()) as mock_close:

        res = await mock_executor.close_position(symbol)

        assert res.get("close_price") is None
        assert res.get("close_price_missing") is True
        assert res.get("close_price_status") == "PRICE_UNAVAILABLE"
        assert "CLOSE_PRICE_UNAVAILABLE" in (active_long_tracker.reconciliation_notes or "")
        # Verify close_live_trade was called with close_price=None (which signals no fake PnL calculation)
        mock_close.assert_called_once()
        _, kwargs = mock_close.call_args
        assert kwargs.get("close_price") is None


# ══════════════════════════════════════════════════════════════════════════════
# TESTS STRICTS DE LA SECTION 1 & 2 : VALIDATION EXHAUSTIVE DE _fetch_fills_strict
# ══════════════════════════════════════════════════════════════════════════════

def test_p0_2_exhaustive_fills_response_validation_cases(mock_executor):
    """
    Section 1: Exhaustive verification of _validate_and_normalize_fills_response:
    1. Rejection of raw lists (dict required)
    2. result == success et fills == []
    3. Absence de result
    4. result différent de success
    5. Absence de fills
    6. fills qui n'est pas une liste
    7. Élément de fills qui n'est pas un dictionnaire
    8. Dictionnaire de fill incomplet ou inexploitable:
       - Manque d'identifiant
       - Manque ou taille non numérique / 0 / négatif / NaN / Inf
       - Manque de prix / prix non numérique / bool / 0 / négatif / NaN / Inf
       - Manque de symbole
       - Manque ou invalidité du sens (side)
       - Manque de timestamp / fillTime
    """
    val = mock_executor._validate_and_normalize_fills_response

    # 1. Rejet d'une liste brute (doit être un dict Kraken)
    with pytest.raises(RuntimeError, match="Malformed Kraken fills response: expected dict"):
        val([{"order_id": "ORD-1", "size": 1.0, "price": 50000.0}])

    # 2. result == success et fills == []
    assert val({"result": "success", "fills": []}) == []

    # 3. Absence de result
    with pytest.raises(RuntimeError, match="missing 'result' key"):
        val({"fills": []})

    # 4. result différent de success
    with pytest.raises(RuntimeError, match="Invalid Kraken fills result"):
        val({"result": "error", "error": "NonceOutOfOrder", "fills": []})

    with pytest.raises(RuntimeError, match="Invalid Kraken fills result"):
        val({"result": "failed", "fills": []})

    # 5. Absence de fills
    with pytest.raises(RuntimeError, match="Missing or invalid Kraken fills list"):
        val({"result": "success"})

    # 6. fills qui n'est pas une liste
    with pytest.raises(RuntimeError, match="Missing or invalid Kraken fills list"):
        val({"result": "success", "fills": "not_a_list"})

    with pytest.raises(RuntimeError, match="Missing or invalid Kraken fills list"):
        val({"result": "success", "fills": {"order_id": "123"}})

    # 7. Élément de fills qui n'est pas un dictionnaire
    with pytest.raises(RuntimeError, match="expected dict"):
        val({"result": "success", "fills": ["invalid_string_fill"]})

    with pytest.raises(RuntimeError, match="expected dict"):
        val({"result": "success", "fills": [42]})

    # 8. Validation stricte de chaque champ d'un fill
    base_valid = {
        "order_id": "ORD-1",
        "size": 1.5,
        "price": 50500.0,
        "symbol": "PF_XBTUSD",
        "side": "buy",
        "fillTime": "2026-11-14T20:00:00Z",
    }

    # 8a. Manque d'identifiant
    bad = base_valid.copy(); del bad["order_id"]
    with pytest.raises(RuntimeError, match="missing exploitable order identifier"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["order_id"] = ""
    with pytest.raises(RuntimeError, match="missing exploitable order identifier"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["order_id"] = "   "
    with pytest.raises(RuntimeError, match="missing exploitable order identifier"):
        val({"result": "success", "fills": [bad]})

    # 8b. Taille : manquante, non numérique, zéro, négative, NaN, Infinity
    bad = base_valid.copy(); del bad["size"]
    with pytest.raises(RuntimeError, match="missing numeric size"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["size"] = "not_a_number"
    with pytest.raises(RuntimeError, match="non-numeric size"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["size"] = 0
    with pytest.raises(RuntimeError, match="size must be finite positive"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["size"] = -0.5
    with pytest.raises(RuntimeError, match="size must be finite positive"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["size"] = "nan"
    with pytest.raises(RuntimeError, match="size must be finite positive"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["size"] = "inf"
    with pytest.raises(RuntimeError, match="size must be finite positive"):
        val({"result": "success", "fills": [bad]})

    # 8c. Prix : manquant, non numérique, bool, zéro, négatif, NaN, Infinity
    bad = base_valid.copy(); del bad["price"]
    with pytest.raises(RuntimeError, match="missing price"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["price"] = "invalid_price"
    with pytest.raises(RuntimeError, match="non-numeric price"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["price"] = True
    with pytest.raises(RuntimeError, match="missing or non-numeric price"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["price"] = 0.0
    with pytest.raises(RuntimeError, match="price must be finite positive"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["price"] = -100.0
    with pytest.raises(RuntimeError, match="price must be finite positive"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["price"] = "nan"
    with pytest.raises(RuntimeError, match="price must be finite positive"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["price"] = "inf"
    with pytest.raises(RuntimeError, match="price must be finite positive"):
        val({"result": "success", "fills": [bad]})

    # 8d. Symbole manquant
    bad = base_valid.copy(); del bad["symbol"]
    with pytest.raises(RuntimeError, match="missing symbol"):
        val({"result": "success", "fills": [bad]})

    # 8e. Side manquant ou invalide
    bad = base_valid.copy(); del bad["side"]
    with pytest.raises(RuntimeError, match="missing or invalid side"):
        val({"result": "success", "fills": [bad]})

    bad = base_valid.copy(); bad["side"] = "hold"
    with pytest.raises(RuntimeError, match="missing or invalid side"):
        val({"result": "success", "fills": [bad]})

    # 8f. FillTime manquant
    bad = base_valid.copy(); del bad["fillTime"]
    with pytest.raises(RuntimeError, match="missing fill time"):
        val({"result": "success", "fills": [bad]})

    # Fill valide complet
    res = val({"result": "success", "fills": [base_valid]})
    assert len(res) == 1
    assert res[0]["order_id"] == "ORD-1"


@pytest.mark.asyncio
async def test_p0_2_typeerror_in_client_request_is_propagated_without_fallback(mock_executor):
    """
    Section 2: Demonstrate that an internal TypeError raised by client._request is
    directly propagated and triggers NO second call (no fallback that masks errors).
    """
    mock_executor._rest_client._request = AsyncMock(side_effect=TypeError("Internal type error inside client request"))

    with pytest.raises(TypeError, match="Internal type error inside client request"):
        await mock_executor._fetch_fills_strict()

    # Verify that exactly ONE call was made: no secondary fallback call occurred
    assert mock_executor._rest_client._request.call_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# TESTS DE LA SECTION 3 : INTÉGRATION SQLITE RÉELLE (VRAIE BASE SQLite EN MÉMOIRE)
# ══════════════════════════════════════════════════════════════════════════════

def test_p1_sqlite_real_database_integration_close_live_trade_price_unavailable():
    """
    Section 3: Integration test using a REAL SQLite database in-memory (no mocks on SQLite).
    1. Create an open trade in SQLite.
    2. Simulate confirmed closure with unavailable close price (close_price=None).
    3. Execute the real close_live_trade() method.
    4. Read the SQLite row directly via SQL query / session.
    5. Demonstrate:
       - row.close_price is NULL (None in Python, never 0.0)
       - row.pnl is NULL (None in Python, never artificial 0.0)
       - row.result is 'unknown' (never 'be' or artificial win/loss)
       - row.status is 'CLOSED'
    6. Execute close_live_trade() with valid close_price on a second trade:
       - row.close_price is recorded properly
       - row.pnl is calculated properly
       - row.result is 'win' or 'loss'
       - row.status is 'CLOSED'
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from contextlib import contextmanager
    from backend.models.live_trade import Base, LiveTrade
    from backend.services.live_trades_service import LiveTradesService

    # 1. Create real SQLite in-memory engine and tables
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    @contextmanager
    def mock_db_session():
        session = TestingSessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # Patch get_db_session in live_trades_service to use our real SQLite in-memory DB
    with patch("backend.services.live_trades_service.get_db_session", side_effect=mock_db_session):
        # 1. Insert open trade into real SQLite
        trade_id_1 = "TRADE-REAL-SQLITE-1"
        order_id_1 = "KRAKEN-ORD-TEST-1"
        with mock_db_session() as db:
            trade_1 = LiveTrade(
                id=trade_id_1,
                order_id=order_id_1,
                symbol="PF_XBTUSD",
                side="long",
                qty=1.0,
                entry=50000.0,
                stop=48000.0,
                tp=55000.0,
                broker="kraken",
                status="OPEN",
            )
            db.add(trade_1)

        # 2 & 3 & 4. Execute real close_live_trade with close_price=None (unavailable)
        res_trade = LiveTradesService.close_live_trade(
            order_id=order_id_1,
            close_price=None,
            close_reason="bot_close_real_sqlite_test",
        )
        assert res_trade is not None

        # 5. Read row directly from real SQLite database
        with mock_db_session() as db:
            stmt = select(LiveTrade).where(LiveTrade.order_id == order_id_1)
            persisted = db.execute(stmt).scalar_one()

            # 6 & 7 & 8. Verifications:
            assert persisted.status == "CLOSED", "Status must be CLOSED"
            assert persisted.close_price is None, f"Expected NULL close_price, got: {persisted.close_price}"
            assert persisted.pnl is None, f"Expected NULL pnl, got: {persisted.pnl}"
            assert persisted.result == "unknown", f"Expected 'unknown' result, got: {persisted.result}"
            assert persisted.close_reason == "bot_close_real_sqlite_test"

        # 9. Verify second trade with VALID close price (entry=50000, close=54000 on long)
        trade_id_2 = "TRADE-REAL-SQLITE-2"
        order_id_2 = "KRAKEN-ORD-TEST-2"
        with mock_db_session() as db:
            trade_2 = LiveTrade(
                id=trade_id_2,
                order_id=order_id_2,
                symbol="PF_XBTUSD",
                side="long",
                qty=1.0,
                entry=50000.0,
                stop=48000.0,
                tp=55000.0,
                broker="kraken",
                status="OPEN",
            )
            db.add(trade_2)

        res_trade_2 = LiveTradesService.close_live_trade(
            order_id=order_id_2,
            close_price=54000.0,
            close_reason="tp_hit_test",
        )
        assert res_trade_2 is not None

        with mock_db_session() as db:
            stmt2 = select(LiveTrade).where(LiveTrade.order_id == order_id_2)
            persisted_2 = db.execute(stmt2).scalar_one()

            assert persisted_2.status == "CLOSED"
            assert persisted_2.close_price == 54000.0
            assert persisted_2.pnl == 4000.0  # (54000 - 50000) * 1.0
            assert persisted_2.result == "win"
            assert persisted_2.close_reason == "tp_hit_test"


# ══════════════════════════════════════════════════════════════════════════════
# TESTS DÉDIÉS AUX DERNIÈRES CORRECTIONS P0/P1
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_p0_2_partial_close_requested_but_real_position_zero_triggers_full_close(mock_executor, active_long_tracker, exchange_state):
    """
    Correction 2: Quantity=0.5 requested (partial intent), but real Kraken exposure is 0.0.
    The decision must be governed strictly by live_pos: full closure executed,
    SL/TP protections cancelled, CLOSED_CONFIRMED, SQLite trade closed.
    """
    symbol = "PF_XBTUSD"
    close_oid = "CLOSE-ORD-PARTIAL-REQ-FULL-REAL"

    async def mock_place(*args, **kwargs):
        # Exchange actually closed the entire position (e.g. liquidation or counter-fill)
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True, "order_id": close_oid, "price_executed": 50000.0}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[])

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()) as mock_close:

        # Partial close of 0.5 requested on a 1.0 position
        res = await mock_executor.close_position(symbol, quantity=0.5)

        assert res["closed"] is True
        assert active_long_tracker.expected_exposure == 0.0
        assert active_long_tracker.sl_tp_state == "CLOSED_CONFIRMED"
        mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_p0_2_full_close_requested_but_real_position_remains_open_triggers_close_incomplete(mock_executor, active_long_tracker, exchange_state):
    """
    Correction 2: Full close requested (quantity=None), but real Kraken exposure remains 0.5 > 1e-6.
    The decision must be governed strictly by live_pos: result["closed"]=False,
    code="CLOSE_INCOMPLETE", tracker.status="OPEN", protections PRESERVED, SQLite not closed.
    """
    symbol = "PF_XBTUSD"
    close_oid = "CLOSE-ORD-FULL-REQ-PARTIAL-REAL"

    async def mock_place(*args, **kwargs):
        # Exchange only executed half: remaining live_pos = 0.5
        exchange_state.positions["long"] = 0.5
        return {"code": "00000", "success": True, "order_id": close_oid, "price_executed": 50000.0}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[])

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status") as mock_prot, \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade") as mock_close:

        res = await mock_executor.close_position(symbol, quantity=None)

        assert res["closed"] is False
        assert res["code"] == "CLOSE_INCOMPLETE"
        assert active_long_tracker.status == "OPEN"
        assert active_long_tracker.expected_exposure == 0.5
        mock_prot.assert_not_called()
        mock_close.assert_not_called()


@pytest.mark.asyncio
async def test_p0_3_unhandled_exception_post_order_triggers_outer_quarantine(mock_executor, active_long_tracker, exchange_state):
    """
    Correction 3: Test that an unhandled exception hitting the outer except block
    with order_sent=True triggers quarantine and EXCEPTION_POST_ORDER_UNCONFIRMED.
    """
    symbol = "PF_XBTUSD"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True, "order_id": "ORD-UNHANDLED-CRASH"}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[])

    # Simulate an unhandled exception during post-order processing (e.g. inside _normalize_open_orders)
    mock_executor._normalize_open_orders = MagicMock(side_effect=RuntimeError("Unhandled parser crash post-order"))

    with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True) as mock_q:
        res = await mock_executor.close_position(symbol)

        assert res["code"] == "EXCEPTION"
        assert "Unhandled parser crash post-order" in res["msg"]
        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert active_long_tracker.sl_tp_state == "EXCEPTION_POST_ORDER_UNCONFIRMED"
        mock_q.assert_called_once()


@pytest.mark.asyncio
async def test_p0_4_order_status_missing_filled_size_triggers_quarantine(mock_executor, active_long_tracker, exchange_state):
    """
    Correction 4: /orders/status returns order without filledSize key.
    Must NOT convert to 0.0 default. Must treat as status_unknown=True and trigger quarantine.
    """
    symbol = "PF_XBTUSD"
    close_oid = "CLOSE-ORD-MISSING-FILLEDSIZE"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True, "order_id": close_oid, "price_executed": 50000.0}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[])

    # Status response without filledSize
    async def mock_order_status(oid):
        return {"order_id": oid, "status": "cancelled"}  # missing filledSize!

    mock_executor._fetch_order_status_strict = AsyncMock(side_effect=mock_order_status)

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):
        res = await mock_executor.close_position(symbol)

        assert active_long_tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"
        assert symbol in mock_executor._reconciliation_required_symbols


@pytest.mark.asyncio
async def test_p0_4_order_status_nan_filled_size_triggers_quarantine(mock_executor, active_long_tracker, exchange_state):
    """
    Correction 4: /orders/status returns order with filledSize="nan" or negative.
    Must treat as status_unknown=True and trigger quarantine.
    """
    symbol = "PF_XBTUSD"
    close_oid = "CLOSE-ORD-NAN-FILLEDSIZE"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True, "order_id": close_oid, "price_executed": 50000.0}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[])

    async def mock_order_status(oid):
        return {"order_id": oid, "status": "cancelled", "filledSize": "nan"}

    mock_executor._fetch_order_status_strict = AsyncMock(side_effect=mock_order_status)

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):
        res = await mock_executor.close_position(symbol)

        assert active_long_tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"
        assert symbol in mock_executor._reconciliation_required_symbols


@pytest.mark.asyncio
async def test_p0_5_candidate_close_price_never_uses_unverified_result_price(mock_executor, active_long_tracker, exchange_state):
    """
    Correction 5: close_position() must NEVER use unverified result.get("price").
    If no matching fills and no verified execution price fields (price_executed, avgPrice),
    close_price must be None, close_price_missing=True, close_price_status="PRICE_UNAVAILABLE".
    """
    symbol = "PF_XBTUSD"
    close_oid = "CLOSE-ORD-UNVERIFIED-PRICE"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        # result contains "price": 88000.0 (e.g. market trigger price, not executed price!)
        return {"code": "00000", "success": True, "order_id": close_oid, "price": 88000.0}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[])

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()) as mock_close:

        res = await mock_executor.close_position(symbol)

        assert res["close_price"] is None
        assert res["close_price_missing"] is True
        assert res["close_price_status"] == "PRICE_UNAVAILABLE"
        mock_close.assert_called_once()
        assert mock_close.call_args[1]["close_price"] is None


def test_p1_sqlite_real_database_preserves_zero_pnl_breakeven():
    """
    Correction 6: Real SQLite integration test demonstrating that when a trade has pnl=0.0
    (true break-even), close_live_trade() with close_price=None preserves pnl=0.0
    and does NOT treat 0.0 as missing or replace it with None.
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from backend.models.live_trade import Base, LiveTrade
    from backend.services.live_trades_service import LiveTradesService
    from contextlib import contextmanager

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextmanager
    def mock_db_session():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    with patch("backend.services.live_trades_service.get_db_session", side_effect=mock_db_session):
        trade_id = "TRADE-BE-SQLITE-ZERO"
        order_id = "KRAKEN-ORD-BE-ZERO"

        # 1. Create open trade with pnl=0.0 already recorded (break-even fill)
        with mock_db_session() as db:
            trade = LiveTrade(
                id=trade_id,
                order_id=order_id,
                symbol="PF_XBTUSD",
                side="long",
                qty=1.0,
                entry=50000.0,
                stop=48000.0,
                tp=55000.0,
                broker="kraken",
                status="OPEN",
                pnl=0.0,  # Genuine break-even
            )
            db.add(trade)

        # 2. Close trade with close_price=None
        res = LiveTradesService.close_live_trade(
            order_id=order_id,
            close_price=None,
            close_reason="be_close_test",
        )
        assert res is not None

        # 3. Read directly from SQLite
        with mock_db_session() as db:
            stmt = select(LiveTrade).where(LiveTrade.order_id == order_id)
            persisted = db.execute(stmt).scalar_one()

            assert persisted.status == "CLOSED"
            assert persisted.pnl == 0.0, f"Expected strictly 0.0, got: {persisted.pnl}"
            assert persisted.result == "be", f"Expected 'be', got: {persisted.result}"
            assert persisted.close_price is None


# ═══════════════════════════════════════════════════════════════════════════════
# 10 TESTS UNITAIRES CIBLÉS ET OBLIGATOIRES — AUDIT FINAL P0 DU LOT 2.2
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_mandatory_audit_1_client_without_request_fails_strictly_no_fabrication(mock_executor):
    """
    Mandatory Test 1: Client without _request (or not callable) fails strictly.
    NO synthetic fills fabricated.
    """
    mock_executor._rest_client = MagicMock(spec=[])  # no _request attribute
    with pytest.raises(RuntimeError, match="does not implement callable '_request'"):
        await mock_executor._fetch_fills_strict()


@pytest.mark.asyncio
async def test_mandatory_audit_2_get_fills_returning_list_does_not_bypass_request(mock_executor):
    """
    Mandatory Test 2: Even if get_fills() exists and returns a raw list,
    _fetch_fills_strict does not use it to bypass _request or fabricate data.
    """
    class ClientWithGetFillsOnly:
        def get_fills(self):
            return [{"fill_id": "f1", "order_id": "ord1", "price": 50000.0, "size": 1.0}]

    mock_executor._rest_client = ClientWithGetFillsOnly()
    with pytest.raises(RuntimeError, match="does not implement callable '_request'"):
        await mock_executor._fetch_fills_strict()


@pytest.mark.asyncio
async def test_mandatory_audit_3_timeout_on_close_order_transmission_triggers_quarantine(mock_executor, active_long_tracker):
    """
    Mandatory Test 3: Timeout on order transmission (SEND_ATTEMPTED):
    quarantined in memory and SQLite.
    """
    symbol = "PF_XBTUSD"
    mock_executor._place_order_rest = AsyncMock(side_effect=TimeoutError("Network timeout during transmission"))

    with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined") as mock_q:
        mock_q.return_value = True
        res = await mock_executor.close_position(symbol)

        assert res.get("code") == "CLOSE_TRANSMISSION_EXCEPTION_QUARANTINED"
        assert res.get("success") is False
        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert active_long_tracker.sl_tp_state == "EXCEPTION_POST_ORDER_UNCONFIRMED"
        mock_q.assert_called_once()
        assert mock_q.call_args[1]["symbol"] == symbol
        assert mock_q.call_args[1]["order_id"] == active_long_tracker.order_id


@pytest.mark.asyncio
async def test_mandatory_audit_4_position_query_failure_after_close_triggers_quarantine(mock_executor, active_long_tracker):
    """
    Mandatory Test 4: Position query failure after close order placement:
    quarantined in memory and SQLite, status MANUAL_RECONCILIATION_REQUIRED,
    sl_tp_state POSITION_VERIFICATION_FAILED.
    """
    symbol = "PF_XBTUSD"
    mock_executor._place_order_rest = AsyncMock(return_value={"code": "00000", "success": True, "order_id": "CLOSE-ORD-4"})
    # First 2 calls in integrity check succeed (long=1.0, short=0.0), post-close call fails
    mock_executor._fetch_position_qty_strict = AsyncMock(side_effect=[1.0, 0.0, RuntimeError("Position query timeout")])

    with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined") as mock_q:
        mock_q.return_value = True
        res = await mock_executor.close_position(symbol)

        assert res.get("code") == "CLOSE_POSITION_VERIFICATION_FAILED_QUARANTINED"
        assert res.get("success") is False
        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert active_long_tracker.sl_tp_state == "POSITION_VERIFICATION_FAILED"
        mock_q.assert_called_once()


@pytest.mark.asyncio
async def test_mandatory_audit_5_sqlite_sync_failure_prevents_closed_confirmed(mock_executor, active_long_tracker, exchange_state):
    """
    Mandatory Test 5: SQLite write failure prevents CLOSED_CONFIRMED,
    sets SQLITE_SYNC_FAILED_QUARANTINE, symbol quarantined.
    """
    symbol = "PF_XBTUSD"
    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True, "order_id": "CLOSE-ORD-5", "price_executed": 50000.0}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[
        {"order_id": "CLOSE-ORD-5", "price": 50000.0, "size": 1.0, "fill_id": "f5"}
    ])

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=None), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True) as mock_q:

        res = await mock_executor.close_position(symbol)

        assert active_long_tracker.sl_tp_state == "SQLITE_SYNC_FAILED_QUARANTINE"
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert symbol in mock_executor._reconciliation_required_symbols
        mock_q.assert_called_once()


@pytest.mark.asyncio
async def test_mandatory_audit_6_sqlite_sync_success_transitions_exchange_verified_to_closed_confirmed(mock_executor, active_long_tracker, exchange_state):
    """
    Mandatory Test 6: SQLite write succeeds: transitions from
    EXCHANGE_CLOSED_PROTECTIONS_VERIFIED to CLOSED_CONFIRMED.
    """
    symbol = "PF_XBTUSD"
    state_before_sqlite = []

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True, "order_id": "CLOSE-ORD-6", "price_executed": 50000.0}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[
        {"order_id": "CLOSE-ORD-6", "price": 50000.0, "size": 1.0, "fill_id": "f6"}
    ])

    def spy_close_live_trade(*args, **kwargs):
        # Capture sl_tp_state right before SQLite commits
        state_before_sqlite.append(active_long_tracker.sl_tp_state)
        return MagicMock()

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", side_effect=spy_close_live_trade):

        res = await mock_executor.close_position(symbol)

        assert res.get("success") is True
        assert state_before_sqlite == ["EXCHANGE_CLOSED_PROTECTIONS_VERIFIED"]
        assert active_long_tracker.sl_tp_state == "CLOSED_CONFIRMED"
        assert active_long_tracker.status == "CLOSED"
        assert symbol not in mock_executor._reconciliation_required_symbols


@pytest.mark.asyncio
async def test_mandatory_audit_7_fill_with_different_order_id_ignored_in_close_price(mock_executor, active_long_tracker, exchange_state):
    """
    Mandatory Test 7: Fill with different order_id is ignored for close price calculation.
    """
    symbol = "PF_XBTUSD"
    close_oid = "CLOSE-ORD-7"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True, "order_id": close_oid}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    # Return two fills: one matching close_oid, one for another order
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[
        {"order_id": close_oid, "price": 52000.0, "size": 1.0, "fill_id": "f7-1"},
        {"order_id": "OTHER-ORDER-999", "price": 99999.0, "size": 5.0, "fill_id": "f7-2"},
    ])

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()):

        res = await mock_executor.close_position(symbol)

        assert res.get("success") is True
        assert res["close_price"] == 52000.0  # strictly only matching fill, not 99999.0


@pytest.mark.asyncio
async def test_mandatory_audit_8_duplicate_fills_deduplicated_for_qty_and_vwap(mock_executor, active_long_tracker, exchange_state):
    """
    Mandatory Test 8: Duplicate fills (same fill_id) are deduplicated;
    qty and VWAP are correct.
    """
    symbol = "PF_XBTUSD"
    close_oid = "CLOSE-ORD-8"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True, "order_id": close_oid}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    # Return duplicate fills with identical fill_id
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[
        {"order_id": close_oid, "price": 50000.0, "size": 0.5, "fill_id": "fill-dup-1"},
        {"order_id": close_oid, "price": 50000.0, "size": 0.5, "fill_id": "fill-dup-1"},  # duplicate
        {"order_id": close_oid, "price": 52000.0, "size": 0.5, "fill_id": "fill-dup-2"},
    ])

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()):

        res = await mock_executor.close_position(symbol)

        assert res.get("success") is True
        # VWAP of 0.5 @ 50000 + 0.5 @ 52000 = 51000.0 (not distorted by duplicate)
        assert res["close_price"] == 51000.0


@pytest.mark.asyncio
async def test_mandatory_audit_9_empty_fills_does_not_replace_order_cancellation_proof(mock_executor, active_long_tracker, exchange_state):
    """
    Mandatory Test 9: Empty fills does not replace order cancellation confirmation.
    If order status is unknown/absent, empty fills triggers orphan quarantine.
    """
    symbol = "PF_XBTUSD"
    close_oid = "CLOSE-ORD-9"

    async def mock_place(*args, **kwargs):
        exchange_state.positions["long"] = 0.0
        return {"code": "00000", "success": True, "order_id": close_oid, "price_executed": 50000.0}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_place)
    mock_executor._fetch_fills_strict = AsyncMock(return_value=[])  # empty fills
    # Protection order status query returns None (absent from /orders/status)
    mock_executor._fetch_order_status_strict = AsyncMock(return_value=None)

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True):

        res = await mock_executor.close_position(symbol)

        # Empty fills does NOT allow declaring cancellation confirmed -> quarantine triggered
        assert active_long_tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"
        assert symbol in mock_executor._reconciliation_required_symbols


@pytest.mark.asyncio
async def test_mandatory_audit_10_preflight_failure_not_sent_avoids_unjustified_quarantine(mock_executor, active_long_tracker):
    """
    Mandatory Test 10: Failure before transmission (NOT_SENT, e.g. sizing exception before order send):
    clean failure without unjustified quarantine because transmission state remains NOT_SENT.
    """
    symbol = "PF_XBTUSD"
    with patch("backend.execution.kraken_futures_executor._floor_to_precision", side_effect=ValueError("Invalid precision config")):
        res = await mock_executor.close_position(symbol)

        assert res.get("success") is False
        assert res.get("code") == "EXCEPTION"
        assert symbol not in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "OPEN"
        assert mock_executor._place_order_rest.call_count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# LOT 2.2.1 — TESTS OBLIGATOIRES P0 : QUARANTAINE AU DÉMARRAGE ET VERROU FAIL-CLOSED
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_lot2_2_1_case1_sqlite_accessible_empty_quarantines_startup_authorized():
    """
    Test 1: SQLite accessible, liste vide de quarantaines.
    Démarrage autorisé sous réserve des autres contrôles (verrou global désactivé).
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await wrapper.reconcile_on_startup()

        assert wrapper.global_entry_lock is False
        assert wrapper.is_startup_secure is True
        assert report["global_entry_lock"] is False
        assert report["startup_secure"] is True
        assert len(wrapper._reconciliation_required_symbols) == 0


@pytest.mark.asyncio
async def test_lot2_2_1_case2_sqlite_accessible_quarantines_present_restored_properly():
    """
    Test 2: SQLite accessible, quarantaines présentes : restauration correcte des symboles.
    Verrou global levé mais symbole en quarantaine locale bloqué.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    q_trade = MagicMock(
        symbol="PF_XBTUSD",
        order_id="ORD-Q-1",
        client_oid="BOT_PF_XBTUSD_1",
        side="long",
        qty=0.5,
    )
    q_trade.get_raw_exchange = MagicMock(return_value={"protections": {}})

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[q_trade]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await wrapper.reconcile_on_startup()

        # Lot 2.2.3.2: Unresolved quarantines present at startup maintain global lock fail-closed
        assert wrapper.global_entry_lock is True
        assert wrapper.is_startup_secure is False
        assert report["status"] == "UNRESOLVED_ANOMALIES_LOCKED"
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        assert wrapper._active_trackers["PF_XBTUSD"].status == "MANUAL_RECONCILIATION_REQUIRED"


@pytest.mark.asyncio
async def test_lot2_2_1_case3_sqlite_inaccessible_blocks_trading_engine_entries():
    """
    Test 3: SQLite inaccessible (OperationalError / ConnectionError) :
    le moteur est bloqué via le verrou global fail-closed.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", side_effect=ConnectionError("SQLite connection refused")):
        report = await wrapper.reconcile_on_startup()

        assert wrapper.global_entry_lock is True
        assert wrapper.is_startup_secure is False
        assert report["global_entry_lock"] is True
        assert report["startup_secure"] is False
        assert report["status"] == "DATABASE_UNAVAILABLE_LOCKED"

        # Toute tentative d'entrée sur n'importe quel symbole est bloquée
        res = await wrapper.place_order(symbol="PF_XBTUSD", side="buy", quantity=0.1, order_type="market")
        assert res["success"] is False
        assert res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
        assert "global Kraken entry lock active" in res["error"]


@pytest.mark.asyncio
async def test_lot2_2_1_case4_sqlite_corrupted_blocks_trading_engine():
    """
    Test 4: SQLite corrompue (DatabaseError / file is not a database) :
    démarrage et entrées bloqués.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", side_effect=RuntimeError("file is not a database / malformed")):
        report = await wrapper.reconcile_on_startup()

        assert wrapper.global_entry_lock is True
        assert wrapper.is_startup_secure is False
        assert report["status"] == "DATABASE_UNAVAILABLE_LOCKED"

        res = await wrapper.place_order(symbol="PF_ETHUSD", side="buy", quantity=1.0, order_type="market")
        assert res["success"] is False
        assert res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"


@pytest.mark.asyncio
async def test_lot2_2_1_case5_exception_during_loading_never_converted_to_empty_list():
    """
    Test 5: Exception pendant le chargement : aucune conversion en liste vide []
    et le rapport n'annonce jamais une réconciliation saine.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", side_effect=IOError("Disk I/O error")):
        report = await wrapper.reconcile_on_startup()

        assert report.get("startup_secure") is False
        assert report.get("status") == "DATABASE_UNAVAILABLE_LOCKED"
        assert wrapper.global_entry_lock is True


@pytest.mark.asyncio
async def test_lot2_2_1_case6_preexisting_in_memory_quarantines_preserved_on_sqlite_failure():
    """
    Test 6: Quarantaines déjà présentes en mémoire : elles ne sont JAMAIS effacées par l'échec SQLite.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    # Une quarantaine mémoire est déjà active pour PF_SOLUSD
    wrapper._reconciliation_required_symbols.add("PF_SOLUSD")

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", side_effect=TimeoutError("DB lock timeout")):
        report = await wrapper.reconcile_on_startup()

        assert "PF_SOLUSD" in wrapper._reconciliation_required_symbols
        assert wrapper.global_entry_lock is True


@pytest.mark.asyncio
async def test_lot2_2_1_case7_sqlite_reconnection_alone_maintains_lock_until_explicit_reconciliation():
    """
    Test 7: Reconnexion SQLite seule : le verrou est maintenu tant qu'une réconciliation
    explicite n'a pas été rejouée avec succès.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    # 1. Échec initial
    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", side_effect=ConnectionError("Down")):
        await wrapper.reconcile_on_startup()
    assert wrapper.global_entry_lock is True

    # 2. SQLite redevient accessible en arrière-plan, MAIS sans réconciliation exécutée
    # Le verrou doit rester STRICTEMENT ACTIF
    assert wrapper.global_entry_lock is True
    res = await wrapper.place_order(symbol="PF_XBTUSD", side="buy", quantity=0.1)
    assert res["success"] is False
    assert res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"


@pytest.mark.asyncio
async def test_lot2_2_1_case8_successful_reconciliation_unlocks_only_after_security_conditions_validated():
    """
    Test 8: Réconciliation réussie : déverrouillage uniquement après validation complète
    des conditions de sécurité lors du nouvel appel de réconciliation.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    # 1. Échec initial
    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", side_effect=ConnectionError("Down")):
        await wrapper.reconcile_on_startup()
    assert wrapper.global_entry_lock is True

    # 2. Réconciliation rejouée avec succès
    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await wrapper.reconcile_on_startup()

    assert wrapper.global_entry_lock is False
    assert wrapper.is_startup_secure is True
    assert report["global_entry_lock"] is False
    assert report["startup_secure"] is True


@pytest.mark.asyncio
async def test_lot2_2_1_all_entry_points_blocked_when_global_lock_active():
    """
    Vérification supplémentaire : aucune voie d'entrée (place_order, place_order_market, place_order_limit)
    ne peut contourner le verrou global.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    wrapper._global_entry_lock = True
    wrapper._global_entry_lock_reason = "Manual lock for audit test"

    # 1. place_order
    res1 = await wrapper.place_order(symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market")
    assert res1["success"] is False
    assert res1["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"

    # 2. place_order_market
    res2 = await wrapper.place_order_market(symbol="PF_XBTUSD", side="buy", size="0.01")
    assert res2["success"] is False
    assert res2["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"

    # 3. place_order_limit
    res3 = await wrapper.place_order_limit(symbol="PF_XBTUSD", side="buy", size="0.01", price="50000.0")
    assert res3["success"] is False
    assert res3["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"


# ═════════════════════════════════════════════════════════════════════
# LOT 2.2.2 — TESTS OBLIGATOIRES VERROU GLOBAL ET DÉMARRAGE SÉCURISÉ
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_lot2_2_2_test1_new_instance_lock_active_before_reconciliation():
    """
    Test 1: Nouvelle instance : verrou actif avant toute réconciliation.
    _global_entry_lock doit être True dès l'initialisation avec motif explicite.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    assert wrapper.global_entry_lock is True
    assert wrapper._global_entry_lock_reason == "STARTUP_RECONCILIATION_NOT_COMPLETED"
    assert wrapper._startup_reconciliation_completed is False


@pytest.mark.asyncio
async def test_lot2_2_2_test2_is_startup_secure_returns_false_before_reconciliation():
    """
    Test 2: is_startup_secure renvoie False avant réconciliation.
    La propriété doit vérifier simultanément _startup_reconciliation_completed et not _global_entry_lock.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    assert wrapper.is_startup_secure is False

    # Si completed=False mais lock=False (état incohérent pré-réconciliation), is_startup_secure doit rester False
    wrapper._global_entry_lock = False
    assert wrapper.is_startup_secure is False

    # Si completed=True mais lock=True (verrou activé post-incident), is_startup_secure doit être False
    wrapper._startup_reconciliation_completed = True
    wrapper._global_entry_lock = True
    assert wrapper.is_startup_secure is False

    # Uniquement si completed=True ET lock=False -> True
    wrapper._global_entry_lock = False
    assert wrapper.is_startup_secure is True


@pytest.mark.asyncio
async def test_lot2_2_2_test3_opening_attempt_before_reconciliation_rejected():
    """
    Test 3: Tentative d'ouverture avant réconciliation : rejetée avec GLOBAL_ENTRY_LOCK_ACTIVE.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    res = await wrapper.place_order(symbol="PF_XBTUSD", side="buy", quantity=0.1, order_type="market")
    assert res["success"] is False
    assert res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    assert "STARTUP_RECONCILIATION_NOT_COMPLETED" in res["error"]


@pytest.mark.asyncio
async def test_lot2_2_2_test4_reconciliation_sqlite_successful_and_empty_authorizes_unlock():
    """
    Test 4: Réconciliation SQLite réussie et vide : déverrouillage autorisé.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    assert wrapper.global_entry_lock is True

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "SUCCESS"
    assert report["global_entry_lock"] is False
    assert report["startup_secure"] is True
    assert wrapper.global_entry_lock is False
    assert wrapper._global_entry_lock_reason is None
    assert wrapper._startup_reconciliation_completed is True
    assert wrapper.is_startup_secure is True


@pytest.mark.asyncio
async def test_lot2_2_2_test5_get_open_trades_returns_none_maintains_lock():
    """
    Test 5: get_open_trades() renvoie None : verrou maintenu.
    Suppression de 'if open_trades is None: open_trades = []' vérifiée.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=None):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "DATABASE_UNAVAILABLE_LOCKED"
    assert report["global_entry_lock"] is True
    assert report["startup_secure"] is False
    assert wrapper.global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False
    assert wrapper.is_startup_secure is False
    assert "open_trades" in wrapper._global_entry_lock_reason.lower() or "none" in wrapper._global_entry_lock_reason.lower()


@pytest.mark.asyncio
async def test_lot2_2_2_test6_get_open_trades_returns_malformed_structure_maintains_lock():
    """
    Test 6: get_open_trades() renvoie une structure malformée : verrou maintenu.
    """
    malformed_payloads = [{"status": "error"}, 12345, "invalid_payload", (1, 2, 3)]
    for payload in malformed_payloads:
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[])
        mock_client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = mock_client
        with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
             patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=payload):
            report = await wrapper.reconcile_on_startup()

        assert report["status"] == "DATABASE_UNAVAILABLE_LOCKED"
        assert report["global_entry_lock"] is True
        assert report["startup_secure"] is False
        assert wrapper.global_entry_lock is True
        assert wrapper._startup_reconciliation_completed is False
        assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_2_test7_reconciliation_ambiguous_symbol_quarantines_symbol_lifts_global_lock():
    """
    Test 7: Réconciliation avec état ambigu sur un symbole spécifique :
    - Politique de sécurité justifiée : le symbole ambigu est placé en quarantaine individuelle.
    - Le verrou global se lève pour permettre le trading des symboles sains.
    - Le symbole ambigu reste bloqué individuellement par ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    mock_trade = MagicMock()
    mock_trade.symbol = "PF_XBTUSD"
    mock_trade.quantity = 0.5
    mock_trade.order_id = "ORD-TEST-7"
    mock_trade.client_order_id = "BOT_PF_XBTUSD_7"
    mock_trade.side = "buy"
    mock_trade.entry = 50000.0
    mock_trade.get_raw_exchange.return_value = {
        "protections": {"sl": {"order_id": "SL-ORD-7", "status": "ATTACHED"}}
    }

    # Exchange call succeeds without exception, but SL is missing on Kraken
    mock_client.get_positions = AsyncMock(return_value=[{"symbol": "PF_XBTUSD", "side": "long", "size": 0.5}])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.5)
    wrapper.get_open_orders = AsyncMock(return_value=[])

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[mock_trade]), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status"), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined"):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "UNRESOLVED_ANOMALIES_LOCKED"
    assert "PF_XBTUSD" in report["ambiguous"]
    assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols

    # Lot 2.2.3.2: Le verrou global est maintenu en présence d'anomalie non résolue
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False

    # Le symbole compromis est strictement bloqué
    res = await wrapper.place_order(symbol="PF_XBTUSD", side="buy", quantity=0.1, order_type="market")
    assert res["success"] is False
    assert res["code"] in ("ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED", "GLOBAL_ENTRY_LOCK_ACTIVE")


@pytest.mark.asyncio
async def test_lot2_2_2_test8_exchange_exception_during_reconciliation_maintains_lock():
    """
    Test 8: Exception exchange pendant la réconciliation : verrou maintenu.
    Si Kraken ne peut être contacté pour vérifier l'état, fail-closed impose de garder le verrou global.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    mock_trade = MagicMock()
    mock_trade.symbol = "PF_XBTUSD"
    mock_trade.quantity = 0.5
    mock_trade.order_id = "ORD-TEST-8"
    mock_trade.side = "buy"
    mock_trade.entry = 50000.0
    mock_trade.get_raw_exchange.return_value = {}

    # Exchange throws network/gateway error
    wrapper._fetch_position_qty_strict = AsyncMock(side_effect=RuntimeError("Kraken REST gateway 503"))

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[mock_trade]), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status"), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined"):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "EXCHANGE_RECONCILIATION_FAILED"
    assert report["global_entry_lock"] is True
    assert report["startup_secure"] is False
    assert wrapper.global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_2_test9_sqlite_exception_during_reconciliation_maintains_lock():
    """
    Test 9: Exception SQLite : verrou maintenu.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", side_effect=RuntimeError("SQLite disk I/O error")):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "DATABASE_UNAVAILABLE_LOCKED"
    assert report["global_entry_lock"] is True
    assert report["startup_secure"] is False
    assert wrapper.global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_2_test10_individual_quarantine_in_memory_never_erased_on_sqlite_failure():
    """
    Test 10: Quarantaine individuelle : aucun effacement involontaire de la quarantaine mémoire.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    wrapper._reconciliation_required_symbols.add("PF_SOLUSD")

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", side_effect=RuntimeError("DB disconnected")):
        report = await wrapper.reconcile_on_startup()

    # La quarantaine préexistante en mémoire est STRICTEMENT préservée
    assert "PF_SOLUSD" in wrapper._reconciliation_required_symbols
    assert wrapper.global_entry_lock is True


@pytest.mark.asyncio
async def test_lot2_2_2_test11_incomplete_reconciliation_maintains_lock():
    """
    Test 11: Réconciliation incomplète : verrou maintenu.
    Si la réconciliation est interrompue ou n'a pas validé l'ensemble des conditions, le verrou reste actif.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    assert wrapper._startup_reconciliation_completed is False
    assert wrapper.global_entry_lock is True

    # Vérification que sans complétion effective de reconcile_on_startup(), les entrées restent bloquées
    res = await wrapper.place_order(symbol="PF_ETHUSD", side="buy", quantity=1.0)
    assert res["success"] is False
    assert res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"


@pytest.mark.asyncio
async def test_lot2_2_2_test12_all_entry_points_blocked_when_lock_active():
    """
    Test 12: Aucune voie d'entrée ne contourne le verrou.
    Test systématique sur place_order, place_order_market, et place_order_limit.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    assert wrapper.global_entry_lock is True

    # 1. place_order
    res1 = await wrapper.place_order(symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market")
    assert res1["success"] is False
    assert res1["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"

    # 2. place_order_market
    res2 = await wrapper.place_order_market(symbol="PF_XBTUSD", side="buy", size="0.01")
    assert res2["success"] is False
    assert res2["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"

    # 3. place_order_limit
    res3 = await wrapper.place_order_limit(symbol="PF_XBTUSD", side="buy", size="0.01", price="50000.0")
    assert res3["success"] is False
    assert res3["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"


@pytest.mark.asyncio
async def test_lot2_2_2_test13_close_and_risk_reduction_allowed_when_lock_active():
    """
    Test 13: Fermeture et réduction de risque toujours possibles lorsque cela est légitime.
    Le verrou global de démarrage ne bloque JAMAIS les ordres reduce_only=True ou intent='close'.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client
    assert wrapper.global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False

    # Activer un tracker valide pour tester que la fermeture traverse la barrière de démarrage
    symbol = "PF_XBTUSD"
    wrapper._active_trackers[symbol] = KrakenTradeTracker(
        order_id="ORD-EXISTING-13",
        client_oid="BOT_PF_XBTUSD_13",
        symbol=symbol,
        side="long",
        filled_qty=0.01,
        expected_exposure=0.01,
        status="OPEN",
    )
    wrapper.check_position_integrity = AsyncMock(return_value=(True, "OK", 0.01))
    wrapper._place_order_rest = AsyncMock(return_value={"success": True, "order_id": "ORD-CLOSE-13"})

    # Appel place_order avec reduce_only=True : NE DOIT PAS être bloqué par GLOBAL_ENTRY_LOCK_ACTIVE
    res_reduce = await wrapper.place_order(
        symbol=symbol,
        side="sell",
        quantity=0.01,
        order_type="market",
        reduce_only=True,
    )
    # L'ordre n'est PAS bloqué par GLOBAL_ENTRY_LOCK_ACTIVE
    assert res_reduce.get("code") != "GLOBAL_ENTRY_LOCK_ACTIVE"


# ═════════════════════════════════════════════════════════════════════
# LOT 2.2.3 — TESTS OBLIGATOIRES RÉCONCILIATION GLOBALE ET FAIL-CLOSED
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_lot2_2_3_case1_sqlite_empty_kraken_open_position_quarantines_symbol():
    """
    Test 1: SQLite vide + position Kraken ouverte.
    La position orpheline sur Kraken doit être détectée, le symbole mis en quarantaine et persisté.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[
        {"symbol": "PF_XBTUSD", "side": "long", "size": 0.25}
    ])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", return_value=True) as mock_record_q:
        report = await wrapper.reconcile_on_startup()

    assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
    assert "PF_XBTUSD" in report["quarantined_symbols"]
    assert "PF_XBTUSD" in report["ambiguous"]
    assert wrapper._active_trackers["PF_XBTUSD"].status == "MANUAL_RECONCILIATION_REQUIRED"
    assert wrapper._active_trackers["PF_XBTUSD"].filled_qty == 0.25
    mock_record_q.assert_called_once()

    # Le symbole compromis ne peut pas ouvrir de nouvelle position
    res = await wrapper.place_order(symbol="PF_XBTUSD", side="buy", quantity=0.1)
    assert res["success"] is False
    assert res["code"] in ("ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED", "GLOBAL_ENTRY_LOCK_ACTIVE")


@pytest.mark.asyncio
async def test_lot2_2_3_case2_sqlite_empty_kraken_open_order_quarantines_symbol():
    """
    Test 2: SQLite vide + ordre Kraken ouvert.
    L'ordre orphelin sur Kraken doit être détecté, le symbole mis en quarantaine et persisté.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": "ORPHAN-ORD-99", "symbol": "PF_ETHUSD", "side": "buy", "size": 1.0}
    ])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", return_value=True) as mock_record_q:
        report = await wrapper.reconcile_on_startup()

    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols
    assert "PF_ETHUSD" in report["ambiguous"]
    assert wrapper._active_trackers["PF_ETHUSD"].status == "MANUAL_RECONCILIATION_REQUIRED"
    assert wrapper._active_trackers["ORPHAN-ORD-99"].status == "MANUAL_RECONCILIATION_REQUIRED"
    assert wrapper._orphan_order_trackers["ORPHAN-ORD-99"].order_id == "ORPHAN-ORD-99"
    mock_record_q.assert_called_once()


@pytest.mark.asyncio
async def test_lot2_2_3_case3_sqlite_empty_kraken_empty_valid_authorizes_unlock():
    """
    Test 3: SQLite vide + positions et ordres Kraken vides, réponses valides.
    Preuve positive d'absence d'exposition : déverrouillage autorisé.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "SUCCESS"
    assert report["global_entry_lock"] is False
    assert report["startup_secure"] is True
    assert wrapper.global_entry_lock is False
    assert wrapper.is_startup_secure is True


@pytest.mark.asyncio
async def test_lot2_2_3_case4_global_get_positions_failure_maintains_lock():
    """
    Test 4: Échec de lecture globale des positions.
    Exception réseau ou HTTP : verrou global maintenu.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(side_effect=RuntimeError("Kraken 500 internal server error"))
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "EXCHANGE_RECONCILIATION_FAILED"
    assert report["global_entry_lock"] is True
    assert report["startup_secure"] is False
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_3_case5_global_get_open_orders_failure_maintains_lock():
    """
    Test 5: Échec de lecture globale des ordres.
    Exception sur /openorders : verrou global maintenu.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(side_effect=RuntimeError("Openorders gateway timeout"))
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "EXCHANGE_RECONCILIATION_FAILED"
    assert report["global_entry_lock"] is True
    assert report["startup_secure"] is False
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_3_case6_positions_return_none_maintains_lock():
    """
    Test 6: Retour None des positions.
    Rejet strict sans masquer l'erreur en 0.0 : verrou global maintenu.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=None)
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "EXCHANGE_RECONCILIATION_FAILED"
    assert report["global_entry_lock"] is True
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_3_case7_positions_return_dict_maintains_lock():
    """
    Test 7: Retour dictionnaire des positions.
    Rejet strict d'un dict inattendu au lieu d'une liste : verrou global maintenu.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value={"unexpected": "dict"})
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "EXCHANGE_RECONCILIATION_FAILED"
    assert report["global_entry_lock"] is True
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_3_case8_positions_api_error_dict_maintains_lock():
    """
    Test 8: Dictionnaire d'erreur API.
    Réponse signalant une erreur API Kraken : verrou global maintenu.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value={"result": "error", "error": "EAPI:Invalid key"})
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "EXCHANGE_RECONCILIATION_FAILED"
    assert "EAPI:Invalid key" in report["error"]
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_3_case9_positions_malformed_response_maintains_lock():
    """
    Test 9: Réponse Exchange mal formée (int, string, bool).
    Verrou global maintenu.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=99999)
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "EXCHANGE_RECONCILIATION_FAILED"
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_3_case10_anomaly_persistence_failure_maintains_global_lock():
    """
    Test 10: Échec de persistance d'une anomalie.
    Si la mise en quarantaine dans SQLite échoue (disque plein, verrou DB) :
    le verrou global doit impérativement rester actif.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[
        {"symbol": "PF_SOLUSD", "side": "long", "size": 10.0}
    ])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", side_effect=RuntimeError("Disk write failed")):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert report["global_entry_lock"] is True
    assert report["startup_secure"] is False
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_3_case11_verification_global_lock_maintained():
    """
    Test 11: Vérification du maintien du verrou global.
    Toute tentative d'ordre entrant sous verrou global doit être rejetée avec GLOBAL_ENTRY_LOCK_ACTIVE.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    assert wrapper.global_entry_lock is True

    res = await wrapper.place_order(symbol="PF_XBTUSD", side="buy", quantity=0.01)
    assert res["success"] is False
    assert res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"


@pytest.mark.asyncio
async def test_lot2_2_3_case12_entry_blocked_on_compromised_symbol():
    """
    Test 12: Vérification du blocage des entrées sur symbole compromis.
    Même si le verrou global est levé, le symbole compromis reste bloqué.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    wrapper._startup_reconciliation_completed = True
    wrapper._global_entry_lock = False
    wrapper._reconciliation_required_symbols.add("PF_XBTUSD")

    res = await wrapper.place_order(symbol="PF_XBTUSD", side="buy", quantity=0.01)
    assert res["success"] is False
    assert res["code"] == "ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"


@pytest.mark.asyncio
async def test_lot2_2_3_case13_reduce_only_technical_guarantee_enforced_for_close_and_reduce():
    """
    Test 13: Vérification des fermetures et réductions avec reduce_only=True.
    Pour tout ordre avec intent='close' ou intent='reduce', reduce_only=True doit être
    imposé techniquement et transmis à _place_order_rest().
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    wrapper._startup_reconciliation_completed = True
    wrapper._global_entry_lock = False

    symbol = "PF_XBTUSD"
    wrapper._active_trackers[symbol] = KrakenTradeTracker(
        order_id="ORD-13",
        client_oid="BOT_PF_XBTUSD_13",
        symbol=symbol,
        side="long",
        filled_qty=0.01,
        expected_exposure=0.01,
        status="OPEN",
    )
    wrapper.check_position_integrity = AsyncMock(return_value=(True, "OK", 0.01))

    rest_calls = []
    async def mock_place_rest(**kwargs):
        rest_calls.append(kwargs)
        return {"code": "00000", "success": True, "order_id": "ORD-CLOSE-OK"}
    wrapper._place_order_rest = AsyncMock(side_effect=mock_place_rest)

    # 1. intent="close" sans reduce_only explicite
    res_close = await wrapper.place_order(
        symbol=symbol,
        side="sell",
        quantity=0.01,
        order_type="market",
        intent="close",
        reduce_only=False,
    )
    assert res_close["success"] is True
    assert len(rest_calls) == 1
    assert rest_calls[0]["reduce_only"] is True

    # 2. intent="reduce" sans reduce_only explicite
    wrapper._active_trackers[symbol].status = "OPEN"
    wrapper._active_trackers[symbol].expected_exposure = 0.01
    res_reduce = await wrapper.place_order(
        symbol=symbol,
        side="sell",
        quantity=0.01,
        order_type="market",
        intent="reduce",
        reduce_only=False,
    )
    assert res_reduce["success"] is True
    assert len(rest_calls) == 2
    assert rest_calls[1]["reduce_only"] is True


@pytest.mark.asyncio
async def test_lot2_2_3_case14_non_regression_existing_behaviors_and_strict_fetch():
    """
    Test 14: Validation unitaire directe de _fetch_position_qty_strict().
    - None -> RuntimeError
    - dict d'erreur -> RuntimeError
    - dict inattendu -> TypeError
    - non-list -> TypeError
    - list valide -> calcul de position correct
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    wrapper._rest_client = mock_client

    # 1. None
    mock_client.get_positions = AsyncMock(return_value=None)
    with pytest.raises(RuntimeError, match="returned None"):
        await wrapper._fetch_position_qty_strict("PF_XBTUSD", "long")

    # 2. Dict d'erreur
    mock_client.get_positions = AsyncMock(return_value={"result": "error", "error": "EAPI:Invalid key"})
    with pytest.raises(RuntimeError, match="API error"):
        await wrapper._fetch_position_qty_strict("PF_XBTUSD", "long")

    # 3. Dict sans erreur mais non-list
    mock_client.get_positions = AsyncMock(return_value={"openPositions": []})
    with pytest.raises(TypeError, match="returned dict instead of list"):
        await wrapper._fetch_position_qty_strict("PF_XBTUSD", "long")

    # 4. Int au lieu de list
    mock_client.get_positions = AsyncMock(return_value=12345)
    with pytest.raises(TypeError, match="expected list"):
        await wrapper._fetch_position_qty_strict("PF_XBTUSD", "long")

    # 5. Liste valide
    mock_client.get_positions = AsyncMock(return_value=[
        {"symbol": "PF_XBTUSD", "side": "long", "size": 0.5},
        {"symbol": "PF_XBTUSD", "side": "short", "size": 0.0},
        {"symbol": "PF_ETHUSD", "side": "long", "size": 2.0},
    ])
    qty = await wrapper._fetch_position_qty_strict("PF_XBTUSD", "long")
    assert qty == 0.5

    # 6. Liste vide
    mock_client.get_positions = AsyncMock(return_value=[])
    qty_empty = await wrapper._fetch_position_qty_strict("PF_XBTUSD", "long")
    assert qty_empty == 0.0


# ═════════════════════════════════════════════════════════════════════
# LOT 2.2.3.1 — TESTS OBLIGATOIRES PERSISTANCE ORPHELINS ET FAIL-CLOSED
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_lot2_2_3_1_test_a_internal_type_error_in_get_positions_fails_closed():
    """
    Test A — TypeError interne :
    get_positions(force_refresh=True) lève un TypeError.
    Vérifier que l'erreur n'est pas masquée et que le verrou global reste actif.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(side_effect=TypeError("Internal TypeError inside position parser"))
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "EXCHANGE_RECONCILIATION_FAILED"
    assert "Internal TypeError" in str(report["error"])
    assert report["global_entry_lock"] is True
    assert report["startup_secure"] is False
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_3_1_test_b_persistence_false_fails_closed():
    """
    Test B — Persistance False :
    La méthode de persistance retourne False.
    Vérifier PERSISTENCE_FAILED_LOCKED et que le verrou ne peut pas être levé.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[
        {"symbol": "PF_XBTUSD", "side": "long", "size": 0.5}
    ])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", return_value=False):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert report["global_entry_lock"] is True
    assert report["startup_secure"] is False
    assert wrapper.global_entry_lock is True
    assert wrapper._global_entry_lock_reason == "PERSISTENCE_FAILED_LOCKED"
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_3_1_test_c_persistence_none_fails_closed():
    """
    Test C — Persistance None :
    La méthode de persistance retourne None.
    Vérifier le même comportement Fail-Closed.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": "ORPHAN-ORD-C", "symbol": "PF_ETHUSD", "side": "buy", "size": 1.0}
    ])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", return_value=None):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert report["global_entry_lock"] is True
    assert report["startup_secure"] is False
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_3_1_test_d_sqlite_exception_triggers_rollback_and_fails_closed():
    """
    Test D — Exception SQLite :
    Simuler un échec de transaction ou de commit.
    Vérifier le rollback, la remontée au moteur et le verrou actif.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": "ORPHAN-ORD-D", "symbol": "PF_SOLUSD", "side": "sell", "size": 5.0}
    ])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", side_effect=Exception("Database lock acquisition timeout")):
        report = await wrapper.reconcile_on_startup()

    assert report["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert report["global_entry_lock"] is True
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False


@pytest.mark.asyncio
async def test_lot2_2_3_1_test_e_multiple_orphan_orders_preserved_individually():
    """
    Test E — Ordres multiples :
    Simuler trois ordres orphelins sur un même symbole.
    Fournir trois identifiants Kraken différents.
    Vérifier que les trois identifiants sont conservés sans s'écraser.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": "ORD-KRAKEN-AAA", "cliOrdId": "CID-AAA", "symbol": "PF_XBTUSD", "side": "buy", "size": 0.1, "orderType": "lmt"},
        {"order_id": "ORD-KRAKEN-BBB", "cliOrdId": "CID-BBB", "symbol": "PF_XBTUSD", "side": "sell", "size": 0.2, "orderType": "stp"},
        {"order_id": "ORD-KRAKEN-CCC", "cliOrdId": "CID-CCC", "symbol": "PF_XBTUSD", "side": "sell", "size": 0.3, "orderType": "take_profit"},
    ])
    wrapper._rest_client = mock_client

    persisted_records = []
    def _mock_record_exchange_quarantine(**kwargs):
        persisted_records.append(kwargs)
        return True

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", side_effect=_mock_record_exchange_quarantine):
        report = await wrapper.reconcile_on_startup()

    # Vérification des 3 enregistrements SQLite individuels
    assert len(persisted_records) == 3
    persisted_ids = {r["order_id"] for r in persisted_records}
    assert persisted_ids == {"ORD-KRAKEN-AAA", "ORD-KRAKEN-BBB", "ORD-KRAKEN-CCC"}
    persisted_cids = {r["client_oid"] for r in persisted_records}
    assert persisted_cids == {"CID-AAA", "CID-BBB", "CID-CCC"}

    # Vérification des trackers distincts en mémoire sans écrasement
    assert "ORD-KRAKEN-AAA" in wrapper._orphan_order_trackers
    assert "ORD-KRAKEN-BBB" in wrapper._orphan_order_trackers
    assert "ORD-KRAKEN-CCC" in wrapper._orphan_order_trackers
    assert wrapper._orphan_order_trackers["ORD-KRAKEN-AAA"].expected_exposure == 0.1
    assert wrapper._orphan_order_trackers["ORD-KRAKEN-BBB"].expected_exposure == 0.2
    assert wrapper._orphan_order_trackers["ORD-KRAKEN-CCC"].expected_exposure == 0.3
    assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols


@pytest.mark.asyncio
async def test_lot2_2_3_1_test_f_orphan_order_without_position_identified_persisted_blocks_entries():
    """
    Test F — Ordre orphelin sans position :
    Simuler un carnet contenant un ordre ouvert sans position.
    Vérifier son identification, sa persistance et le blocage des nouvelles entrées sur le symbole.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": "ORD-ORPHAN-STANDALONE", "cliOrdId": "CID-STANDALONE", "symbol": "PF_DOTUSD", "side": "buy", "size": 100.0, "orderType": "lmt"}
    ])
    wrapper._rest_client = mock_client

    persisted_kwargs = {}
    def _mock_record_exchange_quarantine(**kwargs):
        persisted_kwargs.update(kwargs)
        return True

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", side_effect=_mock_record_exchange_quarantine):
        report = await wrapper.reconcile_on_startup()

    assert persisted_kwargs["order_id"] == "ORD-ORPHAN-STANDALONE"
    assert persisted_kwargs["client_oid"] == "CID-STANDALONE"
    assert persisted_kwargs["anomaly_type"] == "order"
    assert persisted_kwargs["qty"] == 100.0
    assert "PF_DOTUSD" in wrapper._reconciliation_required_symbols

    # Blocage strict de toute nouvelle entrée sur PF_DOTUSD
    res = await wrapper.place_order(symbol="PF_DOTUSD", side="buy", quantity=10.0)
    assert res["success"] is False
    assert res["code"] in ("ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED", "GLOBAL_ENTRY_LOCK_ACTIVE")


@pytest.mark.asyncio
async def test_lot2_2_3_1_test_g_orphan_position_real_sqlite_persistence_and_retrieval():
    """
    Test G — Position orpheline :
    Simuler une position Kraken sans trade SQLite.
    Vérifier sa persistance durable réelle en base et sa relecture.
    """
    from backend.services.live_trades_service import LiveTradesService, get_db_session
    from backend.models.live_trade import LiveTrade
    import uuid

    symbol = "PF_AVAXUSD"
    order_id = "POS_PF_AVAXUSD"

    # Persistance réelle sans mock
    success = LiveTradesService.record_exchange_quarantine(
        symbol=symbol,
        order_id=order_id,
        anomaly_type="position",
        side="long",
        qty=15.5,
        entry_price=35.0,
        client_oid="BOT_AVAX_TEST",
        reason="Test G position orpheline",
        broker="kraken",
    )
    assert success is True

    # Vérification de la persistance durable en base SQLite
    with get_db_session() as db:
        trade = db.query(LiveTrade).filter(LiveTrade.order_id == order_id, LiveTrade.broker == "kraken").first()
        assert trade is not None
        assert trade.symbol == symbol
        assert trade.status == "QUARANTINED"
        assert trade.qty == 15.5
        assert trade.entry == 35.0
        assert "POSITION_ORPHAN" in trade.error_message


@pytest.mark.asyncio
async def test_lot2_2_3_1_test_h_reboot_recovery_from_sqlite_maintains_quarantine():
    """
    Test H — Redémarrage :
    Persister une anomalie, recréer l'exécuteur, vérifier que la réconciliation
    retrouve l'anomalie dans SQLite et maintient la protection nécessaire.
    """
    from backend.services.live_trades_service import LiveTradesService

    test_sym = "PF_LINKUSD"
    test_oid = "ORD-REBOOT-LINK-99"

    # 1. Persistance préalable en base SQLite
    saved = LiveTradesService.record_exchange_quarantine(
        symbol=test_sym,
        order_id=test_oid,
        anomaly_type="order",
        side="buy",
        qty=50.0,
        client_oid="CID-LINK-99",
        reason="Restored at reboot test",
        broker="kraken",
    )
    assert saved is True

    # 2. Recréation d'une instance fraîche de KrakenFuturesExecutorWrapper (simulation redémarrage bot)
    fresh_wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    fresh_wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await fresh_wrapper.reconcile_on_startup()

    # 3. Vérification que l'anomalie persistée est rechargée et que le symbole est en quarantaine
    assert test_sym in fresh_wrapper._reconciliation_required_symbols
    assert test_sym in report["quarantined_symbols"]
    assert test_sym in fresh_wrapper._active_trackers
    assert fresh_wrapper._active_trackers[test_sym].status == "MANUAL_RECONCILIATION_REQUIRED"
    assert test_oid in fresh_wrapper._orphan_order_trackers
    assert fresh_wrapper._orphan_order_trackers[test_oid].order_id == test_oid

    # 4. Vérification que toute nouvelle entrée sur ce symbole reste strictement bloquée
    res = await fresh_wrapper.place_order(symbol=test_sym, side="buy", quantity=10.0)
    assert res["success"] is False
    assert res["code"] in ("ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED", "GLOBAL_ENTRY_LOCK_ACTIVE")


# ═══════════════════════════════════════════════════════════════════════════════
# LOT 2.2.3.2 — TESTS DE VALIDATION P0 (SYMBOLES CONNUS & VERROU FAIL-CLOSED)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lot2_2_3_2_known_btc_with_extra_order_persists_order_and_locks_globally():
    """
    Test Lot 2.2.3.2 - Cas 1:
    Position BTC connue dans SQLite + ordre BTC supplémentaire inconnu dans le carnet Kraken.
    L'ordre orphelin doit être enregistré individuellement en SQLite avec son order_id réel,
    le symbole mis en anomalie, et le verrou global maintenu fermé (fail-closed).
    """
    from backend.services.live_trades_service import LiveTradesService, get_db_session
    from backend.models.live_trade import LiveTrade
    import uuid

    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    
    mock_trade = MagicMock()
    mock_trade.symbol = "PF_XBTUSD"
    mock_trade.quantity = 0.5
    mock_trade.order_id = "ORD-LEGIT-BTC-1"
    mock_trade.client_order_id = "BOT_PF_XBTUSD_LEGIT"
    mock_trade.side = "buy"
    mock_trade.entry = 50000.0
    mock_trade.get_raw_exchange.return_value = {
        "protections": {
            "sl": {"order_id": "SL-ORD-1", "client_oid": "CID-SL-1", "status": "ATTACHED"},
            "tp": {"order_id": "TP-ORD-1", "client_oid": "CID-TP-1", "status": "ATTACHED"},
        }
    }

    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[
        {"symbol": "PF_XBTUSD", "side": "long", "size": 0.5, "price": 50000.0}
    ])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": "SL-ORD-1", "cliOrdId": "CID-SL-1", "symbol": "PF_XBTUSD", "orderType": "stop-loss", "side": "sell", "size": 0.5, "price": 45000.0, "status": "open"},
        {"order_id": "TP-ORD-1", "cliOrdId": "CID-TP-1", "symbol": "PF_XBTUSD", "orderType": "take-profit", "side": "sell", "size": 0.5, "price": 55000.0, "status": "open"},
        {"order_id": "ORD-EXTRA-PARASITE-999", "cliOrdId": "CID-EXTRA-PARASITE", "symbol": "PF_XBTUSD", "orderType": "limit", "side": "sell", "size": 0.1, "price": 60000.0, "status": "open"},
    ])
    wrapper._rest_client = mock_client
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.5)
    wrapper.get_open_orders = AsyncMock(return_value=mock_client.get_open_orders.return_value)

    persisted_records = []
    real_record_fn = LiveTradesService.record_exchange_quarantine

    def _spy_record(**kwargs):
        persisted_records.append(kwargs)
        return real_record_fn(**kwargs)

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[mock_trade]), \
         patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", side_effect=_spy_record):
        report = await wrapper.reconcile_on_startup()

    # 1. L'ordre parasite a été persisté individuellement en base SQLite
    assert len(persisted_records) == 1
    assert persisted_records[0]["order_id"] == "ORD-EXTRA-PARASITE-999"
    assert persisted_records[0]["client_oid"] == "CID-EXTRA-PARASITE"
    assert persisted_records[0]["anomaly_type"] == "order"
    assert persisted_records[0]["reason"] == "UNTRACKED_OPEN_ORDER_ON_KNOWN_SYMBOL"

    # Vérification réelle en base SQLite
    with get_db_session() as db:
        saved_order = db.query(LiveTrade).filter(
            LiveTrade.order_id == "ORD-EXTRA-PARASITE-999",
            LiveTrade.broker == "kraken"
        ).first()
        assert saved_order is not None
        assert saved_order.status == "QUARANTINED"
        assert "UNTRACKED_OPEN_ORDER_ON_KNOWN_SYMBOL" in (saved_order.error_message or "")

    # 2. Le tracker d'ordre est distinct et indexé sans écrasement
    assert "ORD-EXTRA-PARASITE-999" in wrapper._orphan_order_trackers
    assert wrapper._orphan_order_trackers["ORD-EXTRA-PARASITE-999"].order_id == "ORD-EXTRA-PARASITE-999"

    # 3. Le verrou global reste fermé fail-closed
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False
    assert report["global_entry_lock"] is True
    assert report["startup_secure"] is False
    assert report["status"] == "UNRESOLVED_ANOMALIES_LOCKED"
    assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols


@pytest.mark.asyncio
async def test_lot2_2_3_2_known_btc_with_quantity_discrepancy_quarantines_trade_and_locks_globally():
    """
    Test Lot 2.2.3.2 - Cas 2:
    Position BTC connue dans SQLite (qty=1.0) mais Kraken indique une exposition de 0.5.
    Le trade SQLite doit être basculé en QUARANTINED avec motif d'exposition mismatch,
    et le verrou global doit rester fermé.
    """
    from backend.services.live_trades_service import LiveTradesService, get_db_session
    from backend.models.live_trade import LiveTrade
    import uuid

    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")

    trade_id_test = f"ORD-DISC-{uuid.uuid4().hex[:8]}"
    with get_db_session() as db:
        t = LiveTrade(
            order_id=trade_id_test,
            broker="kraken",
            symbol="PF_XBTUSD",
            side="buy",
            qty=1.0,
            entry=50000.0,
            stop=45000.0,
            tp=55000.0,
            status="OPEN",
        )
        t.set_raw_exchange({"protections": {"sl": {"order_id": "SL-ORD-2"}, "tp": {"order_id": "TP-ORD-2"}}})
        db.add(t)
        db.commit()

    db_trade = LiveTradesService.get_trade_by_order_id(trade_id_test, broker="kraken")
    assert db_trade is not None
    assert db_trade.status == "OPEN"

    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[
        {"symbol": "PF_XBTUSD", "side": "long", "size": 0.5, "price": 50000.0}
    ])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": "SL-ORD-2", "symbol": "PF_XBTUSD", "orderType": "stop-loss", "side": "sell", "size": 1.0, "price": 45000.0, "status": "open", "reduceOnly": True},
        {"order_id": "TP-ORD-2", "symbol": "PF_XBTUSD", "orderType": "take-profit", "side": "sell", "size": 1.0, "price": 55000.0, "status": "open", "reduceOnly": True},
    ])
    wrapper._rest_client = mock_client
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.5)
    wrapper.get_open_orders = AsyncMock(return_value=mock_client.get_open_orders.return_value)

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[db_trade]):
        report = await wrapper.reconcile_on_startup()

    # 1. Vérification en base SQLite réelle : le trade est basculé en QUARANTINED
    updated_trade = LiveTradesService.get_trade_by_order_id(trade_id_test, broker="kraken")
    assert updated_trade is not None
    assert updated_trade.status == "QUARANTINED"
    assert "EXCHANGE_SQLITE_EXPOSURE_MISMATCH" in (updated_trade.error_message or "")

    # 2. Le verrou global reste fermé fail-closed
    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False
    assert report["global_entry_lock"] is True
    assert report["startup_secure"] is False
    assert report["status"] == "UNRESOLVED_ANOMALIES_LOCKED"
    assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols


@pytest.mark.asyncio
async def test_lot2_2_3_2_known_btc_with_legitimate_sltp_allows_secure_startup():
    """
    Test Lot 2.2.3.2 - Cas 3:
    Position BTC connue dans SQLite + position Kraken strictement identique + SL/TP légitimes.
    Aucune anomalie : le démarrage sécurisé est possible, le verrou global est levé.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")

    mock_trade = MagicMock()
    mock_trade.symbol = "PF_XBTUSD"
    mock_trade.quantity = 0.5
    mock_trade.order_id = "ORD-HEALTHY-BTC-3"
    mock_trade.client_order_id = "BOT_PF_XBTUSD_3"
    mock_trade.side = "buy"
    mock_trade.entry = 50000.0
    mock_trade.get_raw_exchange.return_value = {
        "protections": {
            "sl": {"order_id": "SL-ORD-3", "client_oid": "CID-SL-3", "status": "ATTACHED"},
            "tp": {"order_id": "TP-ORD-3", "client_oid": "CID-TP-3", "status": "ATTACHED"},
        }
    }

    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[
        {"symbol": "PF_XBTUSD", "side": "long", "size": 0.5, "price": 50000.0}
    ])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": "SL-ORD-3", "cliOrdId": "CID-SL-3", "symbol": "PF_XBTUSD", "orderType": "stop-loss", "side": "sell", "size": 0.5, "price": 45000.0, "status": "open", "reduceOnly": True},
        {"order_id": "TP-ORD-3", "cliOrdId": "CID-TP-3", "symbol": "PF_XBTUSD", "orderType": "take-profit", "side": "sell", "size": 0.5, "price": 55000.0, "status": "open", "reduceOnly": True},
    ])
    wrapper._rest_client = mock_client
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.5)
    wrapper.get_open_orders = AsyncMock(return_value=mock_client.get_open_orders.return_value)

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[mock_trade]):
        report = await wrapper.reconcile_on_startup()

    # Démarrage sécurisé autorisé
    assert wrapper.global_entry_lock is False
    assert wrapper._startup_reconciliation_completed is True
    assert wrapper.is_startup_secure is True
    assert report["status"] == "SUCCESS"
    assert report["global_entry_lock"] is False
    assert report["startup_secure"] is True
    assert len(report["ambiguous"]) == 0
    assert "PF_XBTUSD" in report["healthy"]


@pytest.mark.asyncio
async def test_lot2_2_3_2_known_btc_extra_order_persistence_failure_locks_globally():
    """
    Test Lot 2.2.3.2 - Cas 4:
    Échec de persistance SQLite pour l'ordre orphelin sur symbole connu.
    Fail-closed immédiat avec statut PERSISTENCE_FAILED_LOCKED.
    """
    from backend.services.live_trades_service import LiveTradesService

    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")

    mock_trade = MagicMock()
    mock_trade.symbol = "PF_XBTUSD"
    mock_trade.quantity = 0.5
    mock_trade.order_id = "ORD-LEGIT-4"
    mock_trade.client_order_id = "BOT-4"
    mock_trade.side = "buy"
    mock_trade.entry = 50000.0
    mock_trade.get_raw_exchange.return_value = {
        "protections": {"sl": {"order_id": "SL-4"}, "tp": {"order_id": "TP-4"}}
    }

    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[{"symbol": "PF_XBTUSD", "side": "long", "size": 0.5}])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": "SL-4", "symbol": "PF_XBTUSD", "orderType": "stop-loss", "side": "sell", "size": 0.5, "price": 45000.0},
        {"order_id": "TP-4", "symbol": "PF_XBTUSD", "orderType": "take-profit", "side": "sell", "size": 0.5, "price": 55000.0},
        {"order_id": "ORD-ORPHAN-FAIL", "symbol": "PF_XBTUSD", "orderType": "limit", "side": "buy", "size": 0.1, "price": 40000.0},
    ])
    wrapper._rest_client = mock_client
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.5)
    wrapper.get_open_orders = AsyncMock(return_value=mock_client.get_open_orders.return_value)

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[mock_trade]), \
         patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", return_value=False):
        report = await wrapper.reconcile_on_startup()

    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False
    assert report["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert report["global_entry_lock"] is True


@pytest.mark.asyncio
async def test_lot2_2_3_2_known_btc_exposure_mismatch_quarantine_failure_locks_globally():
    """
    Test Lot 2.2.3.2 - Cas 5:
    Divergence d'exposition sur BTC mais l'appel mark_trade_quarantined échoue (retourne False).
    Fail-closed immédiat avec statut PERSISTENCE_FAILED_LOCKED.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")

    mock_trade = MagicMock()
    mock_trade.symbol = "PF_XBTUSD"
    mock_trade.quantity = 1.0
    mock_trade.order_id = "ORD-LEGIT-5"
    mock_trade.client_order_id = "BOT-5"
    mock_trade.side = "buy"
    mock_trade.entry = 50000.0
    mock_trade.get_raw_exchange.return_value = {
        "protections": {"sl": {"order_id": "SL-5"}, "tp": {"order_id": "TP-5"}}
    }

    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[{"symbol": "PF_XBTUSD", "side": "long", "size": 0.5}])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": "SL-5", "symbol": "PF_XBTUSD", "orderType": "stop-loss", "side": "sell", "size": 0.5, "price": 45000.0},
        {"order_id": "TP-5", "symbol": "PF_XBTUSD", "orderType": "take-profit", "side": "sell", "size": 0.5, "price": 55000.0},
    ])
    wrapper._rest_client = mock_client
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.5)
    wrapper.get_open_orders = AsyncMock(return_value=mock_client.get_open_orders.return_value)

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[mock_trade]), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=False):
        report = await wrapper.reconcile_on_startup()

    assert wrapper.global_entry_lock is True
    assert wrapper.is_startup_secure is False
    assert report["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert report["global_entry_lock"] is True


@pytest.mark.asyncio
async def test_lot2_2_3_2_reboot_recovery_with_quarantined_trade_maintains_global_lock():
    """
    Test Lot 2.2.3.2 - Cas 6:
    Redémarrage du bot avec trade persisté en QUARANTINED dans SQLite.
    Une instance fraîche doit charger la quarantaine et maintenir le verrou global fermé.
    """
    from backend.services.live_trades_service import LiveTradesService

    test_sym = "PF_ETHUSD"
    test_oid = "ORD-REBOOT-ETH-66"

    saved = LiveTradesService.record_exchange_quarantine(
        symbol=test_sym,
        order_id=test_oid,
        anomaly_type="order",
        side="sell",
        qty=5.0,
        client_oid="CID-ETH-66",
        reason="Reboot recovery Lot 2.2.3.2 test",
        broker="kraken",
    )
    assert saved is True

    fresh_wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    fresh_wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await fresh_wrapper.reconcile_on_startup()

    assert test_sym in fresh_wrapper._reconciliation_required_symbols
    assert test_sym in report["quarantined_symbols"]
    assert test_oid in fresh_wrapper._orphan_order_trackers

    assert fresh_wrapper.global_entry_lock is True
    assert fresh_wrapper.is_startup_secure is False
    assert report["status"] == "UNRESOLVED_ANOMALIES_LOCKED"
    assert report["global_entry_lock"] is True

    res = await fresh_wrapper.place_order(symbol="PF_SOLUSD", side="buy", quantity=1.0)
    assert res["success"] is False
    assert res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 1: SAFE PARTIAL REDUCTION & SL/TP SYNCHRONIZATION (7 SCENARIOS)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_module1_scenario1_nominal_50_percent_reduction(mock_executor, active_long_tracker, exchange_state):
    """
    Scenario 1: Nominal 50% partial reduction.
    - Initial exposure 1.0 LONG.
    - Partial close requested for 0.5 -> exchange leaves 0.5.
    - New SL placed with size 0.5, reduce_only=True, verified in open orders.
    - New TP placed with size 0.5, verified in open orders.
    - Old SL and TP cancelled only AFTER new SL is confirmed active.
    - SQLite LiveTrade.qty updated to 0.5 (trade remains OPEN, not closed).
    - Tracker updated to expected_exposure=0.5, status=OPEN.
    """
    symbol = "PF_XBTUSD"
    active_long_tracker.sl_price = 48000.0
    active_long_tracker.tp_price = 55000.0

    new_sl_oid = "NEW-SL-NOMINAL-50"
    new_tp_oid = "NEW-TP-NOMINAL-50"

    # Track placed orders to simulate realistic carnet verification
    open_orders_carnet = []

    async def mock_place_order(symbol, side, order_type, size, **kwargs):
        oid = new_sl_oid if order_type == "stp" else new_tp_oid
        price = kwargs.get("stop_price") or kwargs.get("price") or kwargs.get("tp_price")
        open_orders_carnet.append({
            "order_id": oid,
            "status": "open",
            "side": side,
            "size": size,
            "stopPrice": price,
            "reduceOnly": True,
        })
        return {"result": "success", "success": True, "order_id": oid, "code": "00000"}

    mock_executor._rest_client.place_order = AsyncMock(side_effect=mock_place_order)
    mock_executor._rest_client.get_open_orders = AsyncMock(side_effect=lambda symbol=None: list(open_orders_carnet))
    mock_executor.cancel_order = AsyncMock(return_value={"success": True, "data": {"status": "cancelled"}})

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True) as mock_upd_prot, \
         patch("backend.services.live_trades_service.LiveTradesService.update_trade_quantity", return_value=True) as mock_upd_qty, \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade") as mock_close:

        res = await mock_executor.close_position(symbol, quantity=0.5)

        assert res.get("success") is True
        assert res.get("code") == "PARTIAL_CLOSE"
        assert res.get("closed") is False
        assert abs(res.get("remaining_qty") - 0.5) < 1e-6
        assert res.get("new_sl_order_id") == new_sl_oid
        assert res.get("new_tp_order_id") == new_tp_oid

        # Tracker state
        assert active_long_tracker.status == "OPEN"
        assert abs(active_long_tracker.expected_exposure - 0.5) < 1e-6
        assert active_long_tracker.sl_order_id == new_sl_oid
        assert active_long_tracker.tp_order_id == new_tp_oid
        assert active_long_tracker.sl_tp_state == "PROTECTED"

        # Old protections cancelled
        mock_executor.cancel_order.assert_any_call(order_id="SL-ORD-101", cli_ord_id="BOT_SL_PF_XBTUSD_101")
        mock_executor.cancel_order.assert_any_call(order_id="TP-ORD-102", cli_ord_id="BOT_TP_PF_XBTUSD_102")

        # SQLite synchronization
        mock_upd_qty.assert_called_once_with(
            order_id="ENTRY-ORD-100",
            remaining_qty=0.5,
            close_order_id="CLOSE-ORD-999",
            close_price=50000.0,
            broker="kraken",
        )
        # SQLite trade must NOT be closed
        mock_close.assert_not_called()


@pytest.mark.asyncio
async def test_module1_scenario2_new_sl_rejected_preserves_old_sl_and_quarantines(mock_executor, active_long_tracker, exchange_state):
    """
    Scenario 2: Kraken rejects placement of the new SL order.
    - ABSOLUTE RULE: Old SL is NEVER cancelled (never leave position naked).
    - Symbol is immediately locked in quarantine (_reconciliation_required_symbols).
    - SQLite marks the trade in quarantine.
    - Operation returns failure code PARTIAL_CLOSE_NEW_SL_FAILED_QUARANTINED.
    """
    symbol = "PF_XBTUSD"
    active_long_tracker.sl_price = 48000.0
    active_long_tracker.tp_price = None
    active_long_tracker.tp_order_id = None

    # Kraken rejects the new SL order
    mock_executor._rest_client.place_order = AsyncMock(return_value={
        "result": "error",
        "success": False,
        "error": "InsufficientMarginForReduceOnly",
    })
    mock_executor.cancel_order = AsyncMock()

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status") as mock_upd_prot, \
         patch("backend.services.live_trades_service.LiveTradesService.update_trade_quantity") as mock_upd_qty, \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True) as mock_quar:

        res = await mock_executor.close_position(symbol, quantity=0.5)

        assert res.get("success") is False
        assert res.get("code") == "PARTIAL_CLOSE_NEW_SL_FAILED_QUARANTINED"
        assert res.get("closed") is False

        # Old SL was NEVER cancelled!
        mock_executor.cancel_order.assert_not_called()
        assert active_long_tracker.sl_order_id == "SL-ORD-101"

        # Symbol quarantined and tracker flagged
        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"

        # SQLite quarantine called
        mock_quar.assert_called_once()
        mock_upd_qty.assert_not_called()


@pytest.mark.asyncio
async def test_module1_scenario3_new_sl_ack_accepted_but_missing_from_orderbook_quarantines(mock_executor, active_long_tracker):
    """
    Scenario 3: New SL receives REST ACK, but reading /openorders returns empty or unconfirmed.
    - "Never trust ACK alone" rule enforced by require_confirmation=True.
    - Old SL is preserved.
    - Symbol is quarantined.
    """
    symbol = "PF_XBTUSD"
    active_long_tracker.sl_price = 48000.0
    active_long_tracker.tp_price = None
    active_long_tracker.tp_order_id = None

    new_sl_oid = "NEW-SL-UNCONFIRMED-GHOST"
    # REST API says "success", but carnet is empty!
    mock_executor._rest_client.place_order = AsyncMock(return_value={
        "result": "success",
        "success": True,
        "order_id": new_sl_oid,
        "code": "00000",
    })
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[])
    mock_executor.cancel_order = AsyncMock()

    with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True) as mock_quar:
        res = await mock_executor.close_position(symbol, quantity=0.5)

        assert res.get("success") is False
        assert res.get("code") == "PARTIAL_CLOSE_NEW_SL_FAILED_QUARANTINED"

        # Old SL preserved!
        mock_executor.cancel_order.assert_not_called()
        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"


@pytest.mark.asyncio
async def test_module1_scenario4_old_sl_cancel_failed_quarantines_duplicate_sl_risk(mock_executor, active_long_tracker):
    """
    Scenario 4: New SL confirmed in carnet, but cancelling the OLD SL fails.
    - Two SL orders are now potentially active in the market (duplicate SL danger).
    - Fail-closed: symbol is immediately quarantined with critical alert.
    - Code returns PARTIAL_CLOSE_OLD_SL_CANCEL_FAILED_QUARANTINED.
    """
    symbol = "PF_XBTUSD"
    active_long_tracker.sl_price = 48000.0
    active_long_tracker.tp_price = None
    active_long_tracker.tp_order_id = None

    new_sl_oid = "NEW-SL-CONFIRMED-DUPLICATE-RISK"
    mock_executor._rest_client.place_order = AsyncMock(return_value={"result": "success", "success": True, "order_id": new_sl_oid})
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": new_sl_oid, "status": "open", "side": "sell", "size": 0.5, "stopPrice": 48000.0, "reduceOnly": True}
    ])
    # Cancel old SL fails!
    mock_executor.cancel_order = AsyncMock(return_value={"success": False, "msg": "Internal exchange cancel error"})

    with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True) as mock_quar:
        res = await mock_executor.close_position(symbol, quantity=0.5)

        assert res.get("success") is False
        assert res.get("code") == "PARTIAL_CLOSE_OLD_SL_CANCEL_FAILED_QUARANTINED"
        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert "duplicate" in active_long_tracker.reconciliation_notes.lower()
        mock_quar.assert_called_once()


@pytest.mark.asyncio
async def test_module1_scenario5_sqlite_update_quantity_failure_quarantines(mock_executor, active_long_tracker):
    """
    Scenario 5: Exchange reduction and SL recalibration succeed, but SQLite update_trade_quantity fails.
    - State is out of sync between exchange and database.
    - Symbol is quarantined, tracker marked MANUAL_RECONCILIATION_REQUIRED.
    - Code returns PARTIAL_CLOSE_SQLITE_UPDATE_FAILED_QUARANTINED.
    """
    symbol = "PF_XBTUSD"
    active_long_tracker.sl_price = 48000.0
    active_long_tracker.tp_price = None
    active_long_tracker.tp_order_id = None

    new_sl_oid = "NEW-SL-SQL-FAIL"
    mock_executor._rest_client.place_order = AsyncMock(return_value={"result": "success", "success": True, "order_id": new_sl_oid})
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": new_sl_oid, "status": "open", "side": "sell", "size": 0.5, "stopPrice": 48000.0, "reduceOnly": True}
    ])
    mock_executor.cancel_order = AsyncMock(return_value={"success": True, "data": {"status": "cancelled"}})

    with patch("backend.services.live_trades_service.LiveTradesService.update_trade_quantity", return_value=False), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True) as mock_quar:

        res = await mock_executor.close_position(symbol, quantity=0.5)

        assert res.get("success") is False
        assert res.get("code") == "PARTIAL_CLOSE_SQLITE_UPDATE_FAILED_QUARANTINED"
        assert symbol in mock_executor._reconciliation_required_symbols
        assert active_long_tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        mock_quar.assert_called_once()


@pytest.mark.asyncio
async def test_module1_scenario6_live_position_discrepancy_aligns_to_real_position(mock_executor, active_long_tracker, exchange_state):
    """
    Scenario 6: Discrepancy between requested quantity and actual executed quantity.
    - Requested close: 0.5.
    - But exchange execution leaves remaining live position at 0.4 (e.g. overfill / rounding).
    - SL recalibration and SQLite update MUST strictly align to real live_pos (0.4), not the theoretical 0.5.
    """
    symbol = "PF_XBTUSD"
    active_long_tracker.sl_price = 48000.0
    active_long_tracker.tp_price = None
    active_long_tracker.tp_order_id = None

    # Simulate that exchange executed 0.6, leaving 0.4
    async def mock_partial_fill_discrepancy(*args, **kwargs):
        exchange_state.positions["long"] = 0.4
        return {"code": "00000", "success": True, "order_id": "CLOSE-DISCREPANCY"}

    mock_executor._place_order_rest = AsyncMock(side_effect=mock_partial_fill_discrepancy)

    new_sl_oid = "NEW-SL-REAL-04"
    placed_sl_size = None

    async def mock_place_sl(symbol, side, order_type, size, **kwargs):
        nonlocal placed_sl_size
        placed_sl_size = size
        return {"result": "success", "success": True, "order_id": new_sl_oid}

    mock_executor._rest_client.place_order = AsyncMock(side_effect=mock_place_sl)
    mock_executor._rest_client.get_open_orders = AsyncMock(return_value=[
        {"order_id": new_sl_oid, "status": "open", "side": "sell", "size": 0.4, "stopPrice": 48000.0, "reduceOnly": True}
    ])
    mock_executor.cancel_order = AsyncMock(return_value={"success": True, "data": {"status": "cancelled"}})

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_trade_quantity", return_value=True) as mock_upd_qty:

        res = await mock_executor.close_position(symbol, quantity=0.5)

        assert res.get("success") is True
        assert res.get("code") == "PARTIAL_CLOSE"
        assert abs(res.get("remaining_qty") - 0.4) < 1e-6

        # SL was placed with real 0.4, NOT theoretical 0.5!
        assert abs(placed_sl_size - 0.4) < 1e-6
        assert abs(active_long_tracker.expected_exposure - 0.4) < 1e-6

        # SQLite updated to real 0.4
        mock_upd_qty.assert_called_once()
        assert abs(mock_upd_qty.call_args[1]["remaining_qty"] - 0.4) < 1e-6


@pytest.mark.asyncio
async def test_module1_scenario7_two_consecutive_partial_reductions(mock_executor, active_long_tracker, exchange_state):
    """
    Scenario 7: Two consecutive partial reductions on the same position.
    - Initial position: 1.0 LONG, SL-1.
    - Reduction 1 (50%): live_pos becomes 0.5. SL-2 (0.5) placed, SL-1 cancelled. SQLite=0.5.
    - Reduction 2 (50% of remainder): live_pos becomes 0.25. SL-3 (0.25) placed, SL-2 cancelled. SQLite=0.25.
    - Verifies complete chain of custody, no ghost orders, and clean tracker continuity.
    """
    symbol = "PF_XBTUSD"
    active_long_tracker.sl_order_id = "SL-STEP-1"
    active_long_tracker.sl_client_oid = "BOT_SL_STEP1"
    active_long_tracker.sl_price = 48000.0
    active_long_tracker.tp_price = None
    active_long_tracker.tp_order_id = None

    current_sl_in_carnet = "SL-STEP-1"
    current_carnet_size = 1.0

    async def mock_place_sl(symbol, side, order_type, size, **kwargs):
        nonlocal current_sl_in_carnet, current_carnet_size
        new_oid = "SL-STEP-2" if size == 0.5 else "SL-STEP-3"
        current_sl_in_carnet = new_oid
        current_carnet_size = size
        return {"result": "success", "success": True, "order_id": new_oid}

    mock_executor._rest_client.place_order = AsyncMock(side_effect=mock_place_sl)

    async def mock_open_orders(symbol=None):
        return [{
            "order_id": current_sl_in_carnet,
            "status": "open",
            "side": "sell",
            "size": current_carnet_size,
            "stopPrice": 48000.0,
            "reduceOnly": True,
        }]

    mock_executor._rest_client.get_open_orders = AsyncMock(side_effect=mock_open_orders)
    cancelled_orders = []

    async def mock_cancel(order_id=None, cli_ord_id=None, **kwargs):
        cancelled_orders.append(order_id)
        return {"success": True, "data": {"status": "cancelled"}}

    mock_executor.cancel_order = AsyncMock(side_effect=mock_cancel)

    with patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_trade_quantity", return_value=True) as mock_upd_qty:

        # ── REDUCTION 1: 1.0 -> 0.5 ──
        res1 = await mock_executor.close_position(symbol, quantity=0.5)

        assert res1["code"] == "PARTIAL_CLOSE"
        assert abs(res1["remaining_qty"] - 0.5) < 1e-6
        assert active_long_tracker.status == "OPEN"
        assert abs(active_long_tracker.expected_exposure - 0.5) < 1e-6
        assert active_long_tracker.sl_order_id == "SL-STEP-2"
        assert "SL-STEP-1" in cancelled_orders

        # ── REDUCTION 2: 0.5 -> 0.25 ──
        res2 = await mock_executor.close_position(symbol, quantity=0.25)

        assert res2["code"] == "PARTIAL_CLOSE"
        assert abs(res2["remaining_qty"] - 0.25) < 1e-6
        assert active_long_tracker.status == "OPEN"
        assert abs(active_long_tracker.expected_exposure - 0.25) < 1e-6
        assert active_long_tracker.sl_order_id == "SL-STEP-3"
        assert "SL-STEP-2" in cancelled_orders

        # Verify SQLite received both updates
        assert mock_upd_qty.call_count == 2
        assert abs(mock_upd_qty.call_args_list[0][1]["remaining_qty"] - 0.5) < 1e-6
        assert abs(mock_upd_qty.call_args_list[1][1]["remaining_qty"] - 0.25) < 1e-6


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 1.1: RECONCILIATION & REBOOT RECOVERY (5 SCENARIOS)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_module1_1_scenario1_reboot_nominal_partially_reduced_trade():
    """
    Module 1.1 - Scenario 1: Nominal reboot with a partially reduced trade.
    - SQLite has LiveTrade with qty=0.5, SL=48000, TP=55000, status=OPEN.
    - Kraken live position is exactly 0.5.
    - Carnet has valid active SL (0.5) and TP (0.5), both reduceOnly=True.
    - No unknown orders.
    - Startup must succeed cleanly (startup_secure=True, global_entry_lock=False).
    - Tracker initialized to 0.5 expected exposure, status OPEN.
    """
    symbol = "PF_XBTUSD"
    trade_oid = "ORD-REBOOT-NOM-50"
    sl_oid = "SL-REBOOT-50"
    tp_oid = "TP-REBOOT-50"

    fake_db_trade = MagicMock()
    fake_db_trade.order_id = trade_oid
    fake_db_trade.client_oid = "BOT_REBOOT_50"
    fake_db_trade.symbol = symbol
    fake_db_trade.side = "long"
    fake_db_trade.qty = 0.5
    fake_db_trade.entry = 50000.0
    fake_db_trade.stop = 48000.0
    fake_db_trade.tp = 55000.0
    fake_db_trade.status = "OPEN"
    fake_db_trade.sl_order_id = sl_oid
    fake_db_trade.tp_order_id = tp_oid
    fake_db_trade.sl_client_oid = "BOT_SL_50"
    fake_db_trade.tp_client_oid = "BOT_TP_50"
    fake_db_trade.sl_status = "ATTACHED"
    fake_db_trade.tp_status = "ATTACHED"
    fake_db_trade.get_raw_exchange = MagicMock(return_value={
        "protections": {
            "sl": {"order_id": sl_oid, "client_oid": "BOT_SL_50", "status": "ATTACHED"},
            "tp": {"order_id": tp_oid, "client_oid": "BOT_TP_50", "status": "ATTACHED"},
        }
    })

    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[
        {"symbol": symbol, "side": "long", "size": 0.5}
    ])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {
            "order_id": sl_oid,
            "cliOrdId": "BOT_SL_50",
            "symbol": symbol,
            "side": "sell",
            "size": 0.5,
            "stopPrice": 48000.0,
            "orderType": "stp",
            "reduceOnly": True,
            "status": "open",
            "filledSize": 0.0,
        },
        {
            "order_id": tp_oid,
            "cliOrdId": "BOT_TP_50",
            "symbol": symbol,
            "side": "sell",
            "size": 0.5,
            "limitPrice": 55000.0,
            "price": 55000.0,
            "orderType": "take_profit",
            "reduceOnly": True,
            "status": "open",
            "filledSize": 0.0,
        },
    ])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[fake_db_trade]), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):

        report = await wrapper.reconcile_on_startup()

        assert report["startup_secure"] is True
        assert report["global_entry_lock"] is False
        assert report["status"] == "SUCCESS"
        assert wrapper.is_startup_secure is True
        assert wrapper.global_entry_lock is False
        assert symbol not in wrapper._reconciliation_required_symbols

        tracker = wrapper._active_trackers.get(symbol)
        assert tracker is not None
        assert tracker.status == "OPEN"
        assert abs(tracker.expected_exposure - 0.5) < 1e-6
        assert tracker.sl_order_id == sl_oid
        assert tracker.tp_order_id == tp_oid
        assert tracker.sl_tp_state == "PROTECTED"


@pytest.mark.asyncio
async def test_module1_1_scenario2_reboot_crash_exposure_mismatch_quarantines_and_locks():
    """
    Module 1.1 - Scenario 2: Post-crash reboot with exposure mismatch.
    - Exchange shows live_pos = 0.4 (fill happened on Kraken).
    - SQLite has trade with qty = 1.0 (crash happened before update_trade_quantity).
    - Invariant: Discrepancy detected, trade quarantined, global entry lock maintained.
    - Règle 1: Aucune correction automatique de quantité sans preuve.
    """
    symbol = "PF_XBTUSD"
    trade_oid = "ORD-REBOOT-MISMATCH-10"
    sl_oid = "SL-MISMATCH"
    sl_client_oid = "BOT_SL_MISMATCH"

    fake_db_trade = MagicMock()
    fake_db_trade.order_id = trade_oid
    fake_db_trade.client_oid = "BOT_MISMATCH_10"
    fake_db_trade.symbol = symbol
    fake_db_trade.side = "long"
    fake_db_trade.qty = 1.0  # SQLite says 1.0!
    fake_db_trade.entry = 50000.0
    fake_db_trade.stop = 48000.0
    fake_db_trade.tp = None
    fake_db_trade.status = "OPEN"
    fake_db_trade.sl_order_id = sl_oid
    fake_db_trade.tp_order_id = None
    fake_db_trade.sl_client_oid = sl_client_oid
    fake_db_trade.tp_client_oid = None
    fake_db_trade.sl_status = "ATTACHED"
    fake_db_trade.tp_status = None
    fake_db_trade.get_raw_exchange = MagicMock(return_value={
        "protections": {
            "sl": {"order_id": sl_oid, "client_oid": sl_client_oid, "status": "ATTACHED"},
        }
    })

    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[
        {"symbol": symbol, "side": "long", "size": 0.4}  # Kraken says 0.4!
    ])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {
            "order_id": "SL-MISMATCH",
            "cliOrdId": "BOT_SL_MISMATCH",
            "symbol": symbol,
            "side": "sell",
            "size": 0.4,
            "stopPrice": 48000.0,
            "orderType": "stp",
            "reduceOnly": True,
            "status": "open",
            "filledSize": 0.0,
        }
    ])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[fake_db_trade]), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True) as mock_quar, \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):

        report = await wrapper.reconcile_on_startup()

        assert report["startup_secure"] is False
        assert report["global_entry_lock"] is True
        assert wrapper.is_startup_secure is False
        assert wrapper.global_entry_lock is True
        assert symbol in wrapper._reconciliation_required_symbols

        # In-memory tracker must be marked MANUAL_RECONCILIATION_REQUIRED
        tracker = wrapper._active_trackers.get(symbol)
        assert tracker is not None
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"

        # SQLite quarantine called with EXCHANGE_SQLITE_EXPOSURE_MISMATCH
        assert mock_quar.call_count >= 1
        reasons = [c[1].get("reason") for c in mock_quar.call_args_list]
        assert "EXCHANGE_SQLITE_EXPOSURE_MISMATCH" in reasons


@pytest.mark.asyncio
async def test_module1_1_scenario3_reboot_duplicate_sl_on_kraken_persists_orphan_and_locks():
    """
    Module 1.1 - Scenario 3: Post-crash reboot with duplicate SL on Kraken.
    - Kraken carnet contains two SL orders:
      1. SL-NEW-50 (size 0.5) tracked by SQLite.
      2. SL-OLD-100 (size 1.0) untracked residual order.
    - Règle 2: Ne pas annuler automatiquement l'ordre non suivi. Détection et persistance en base.
    - Global entry lock maintained.
    """
    symbol = "PF_XBTUSD"
    trade_oid = "ORD-REBOOT-DUP"
    new_sl_oid = "SL-NEW-50"
    old_sl_oid = "SL-OLD-100"

    fake_db_trade = MagicMock()
    fake_db_trade.order_id = trade_oid
    fake_db_trade.client_oid = "BOT_DUP"
    fake_db_trade.symbol = symbol
    fake_db_trade.side = "long"
    fake_db_trade.qty = 0.5
    fake_db_trade.entry = 50000.0
    fake_db_trade.stop = 48000.0
    fake_db_trade.tp = None
    fake_db_trade.status = "OPEN"
    fake_db_trade.sl_order_id = new_sl_oid
    fake_db_trade.tp_order_id = None
    fake_db_trade.sl_client_oid = "BOT_SL_NEW_50"
    fake_db_trade.tp_client_oid = None
    fake_db_trade.sl_status = "ATTACHED"
    fake_db_trade.tp_status = None
    fake_db_trade.get_raw_exchange = MagicMock(return_value={
        "protections": {
            "sl": {"order_id": new_sl_oid, "client_oid": "BOT_SL_NEW_50", "status": "ATTACHED"},
        }
    })

    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[
        {"symbol": symbol, "side": "long", "size": 0.5}
    ])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {
            "order_id": new_sl_oid,
            "cliOrdId": "BOT_SL_NEW_50",
            "symbol": symbol,
            "side": "sell",
            "size": 0.5,
            "stopPrice": 48000.0,
            "orderType": "stp",
            "reduceOnly": True,
            "status": "open",
            "filledSize": 0.0,
        },
        {
            "order_id": old_sl_oid,
            "cliOrdId": "BOT_SL_OLD_100",
            "symbol": symbol,
            "side": "sell",
            "size": 1.0,
            "stopPrice": 48000.0,
            "orderType": "stp",
            "reduceOnly": True,
            "status": "open",
            "filledSize": 0.0,
        },
    ])
    mock_client.cancel_order = AsyncMock()
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[fake_db_trade]), \
         patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", return_value=True) as mock_record_quar, \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):

        report = await wrapper.reconcile_on_startup()

        # Invariant: Old untracked SL was NOT automatically cancelled!
        mock_client.cancel_order.assert_not_called()

        # Invariant: Old SL was persisted to SQLite quarantine
        mock_record_quar.assert_called_once()
        call_kwargs = mock_record_quar.call_args[1]
        assert call_kwargs["order_id"] == old_sl_oid
        assert call_kwargs["symbol"] == symbol

        # Invariant: Global entry lock maintained
        assert report["startup_secure"] is False
        assert report["global_entry_lock"] is True
        assert wrapper.is_startup_secure is False
        assert wrapper.global_entry_lock is True
        assert old_sl_oid in wrapper._orphan_order_trackers


@pytest.mark.asyncio
async def test_module1_1_scenario4_reboot_oversized_sl_fails_validation_and_quarantines():
    """
    Module 1.1 - Scenario 4: Reboot with oversized SL covering a reduced position.
    - Position is 0.5 on exchange and in SQLite.
    - But SL in carnet has size 1.0 (recalibration didn't happen before crash).
    - Invariant: _validate_recovered_protection rejects oversized SL (size 1.0 > trade_qty 0.5).
    - Trade quarantined as INVALID_ON_EXCHANGE, global entry lock maintained.
    """
    symbol = "PF_XBTUSD"
    trade_oid = "ORD-REBOOT-OVERSIZED"
    sl_oid = "SL-OVERSIZED-100"

    fake_db_trade = MagicMock()
    fake_db_trade.order_id = trade_oid
    fake_db_trade.client_oid = "BOT_OVERSIZED"
    fake_db_trade.symbol = symbol
    fake_db_trade.side = "long"
    fake_db_trade.qty = 0.5  # Position is 0.5
    fake_db_trade.entry = 50000.0
    fake_db_trade.stop = 48000.0
    fake_db_trade.tp = None
    fake_db_trade.status = "OPEN"
    fake_db_trade.sl_order_id = sl_oid
    fake_db_trade.tp_order_id = None
    fake_db_trade.sl_client_oid = "BOT_SL_OVERSIZED"
    fake_db_trade.tp_client_oid = None
    fake_db_trade.sl_status = "ATTACHED"
    fake_db_trade.tp_status = None
    fake_db_trade.get_raw_exchange = MagicMock(return_value={
        "protections": {
            "sl": {"order_id": sl_oid, "client_oid": "BOT_SL_OVERSIZED", "status": "ATTACHED"},
        }
    })

    wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[
        {"symbol": symbol, "side": "long", "size": 0.5}
    ])
    mock_client.get_open_orders = AsyncMock(return_value=[
        {
            "order_id": sl_oid,
            "cliOrdId": "BOT_SL_OVERSIZED",
            "symbol": symbol,
            "side": "sell",
            "size": 1.0,  # SL is 1.0 > 0.5!
            "stopPrice": 48000.0,
            "orderType": "stp",
            "reduceOnly": True,
            "status": "open",
            "filledSize": 0.0,
        }
    ])
    wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[fake_db_trade]), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True) as mock_quar, \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True) as mock_upd_prot:

        report = await wrapper.reconcile_on_startup()

        assert report["startup_secure"] is False
        assert report["global_entry_lock"] is True
        assert wrapper.is_startup_secure is False
        assert wrapper.global_entry_lock is True
        assert symbol in wrapper._reconciliation_required_symbols

        # Protection status marked INVALID_ON_EXCHANGE due to oversized SL
        mock_upd_prot.assert_called_with(
            symbol=symbol,
            protection_type="sl",
            status="INVALID_ON_EXCHANGE",
            order_id=trade_oid,
            client_oid="BOT_SL_OVERSIZED",
            error_message="Oversized SL protection order: 1.0 > required 0.5 (size=1.0, filled=0.0, status=open)",
            broker="kraken",
        )


@pytest.mark.asyncio
async def test_module1_1_scenario5_reboot_tp_absent_distinguishes_total_vs_partial_vs_unexecuted():
    """
    Module 1.1 - Scenario 5: Absent TP at startup with fill analysis (Règle 3).
    - 5A: Total TP fill confirmed in recent fills -> trade closed cleanly in SQLite, no false quarantine.
    - 5B: Partial TP fill confirmed -> PARTIALLY_FILLED recorded, quarantined fail-closed.
    - 5C: 0 fills found -> genuine CRITICAL_TP_MISSING, quarantined fail-closed.
    """
    symbol = "PF_XBTUSD"

    def make_trade(tp_id):
        t = MagicMock()
        t.order_id = f"ORD-{tp_id}"
        t.client_oid = f"BOT-{tp_id}"
        t.symbol = symbol
        t.side = "long"
        t.qty = 1.0
        t.entry = 50000.0
        t.stop = 48000.0
        t.tp = 55000.0
        t.status = "OPEN"
        t.sl_order_id = "SL-5"
        t.tp_order_id = tp_id
        t.sl_client_oid = "BOT_SL_5"
        t.tp_client_oid = f"BOT_{tp_id}"
        t.sl_status = "ATTACHED"
        t.tp_status = "ATTACHED"
        t.get_raw_exchange = MagicMock(return_value={
            "protections": {
                "sl": {"order_id": "SL-5", "client_oid": "BOT_SL_5", "status": "ATTACHED"},
                "tp": {"order_id": tp_id, "client_oid": f"BOT_{tp_id}", "status": "ATTACHED"},
            }
        })
        return t

    # ── CASE 5A: TOTAL TP FILL ──
    trade_5a = make_trade("TP-TOTAL")
    wrapper_5a = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    client_5a = AsyncMock()
    client_5a.get_positions = AsyncMock(return_value=[])  # Position closed on exchange
    client_5a.get_open_orders = AsyncMock(return_value=[])
    client_5a._request = AsyncMock(return_value={
        "result": "success",
        "fills": [{"order_id": "TP-TOTAL", "symbol": symbol, "side": "sell", "size": 1.0, "price": 55000.0, "fillTime": "2026-11-15T18:00:00.000Z"}]
    })
    wrapper_5a._rest_client = client_5a

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[trade_5a]), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade") as mock_close_5a, \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status") as mock_upd_5a:

        report_5a = await wrapper_5a.reconcile_on_startup()

        # Trade was closed in SQLite with reason take_profit_hit
        mock_close_5a.assert_called_once_with(
            order_id="ORD-TP-TOTAL",
            close_reason="take_profit_hit",
            raw_exchange={"tp_fills": [{"order_id": "TP-TOTAL", "symbol": symbol, "side": "sell", "size": 1.0, "price": 55000.0, "fillTime": "2026-11-15T18:00:00.000Z"}], "tp_filled_size": 1.0},
        )
        assert symbol not in wrapper_5a._reconciliation_required_symbols

    # ── CASE 5B: PARTIAL TP FILL ──
    trade_5b = make_trade("TP-PARTIAL")
    wrapper_5b = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    client_5b = AsyncMock()
    client_5b.get_positions = AsyncMock(return_value=[
        {"symbol": symbol, "side": "long", "size": 0.6}  # 0.4 executed, 0.6 remains
    ])
    client_5b.get_open_orders = AsyncMock(return_value=[
        {
            "order_id": "SL-5",
            "cliOrdId": "BOT_SL_5",
            "symbol": symbol,
            "side": "sell",
            "size": 0.6,
            "stopPrice": 48000.0,
            "orderType": "stp",
            "reduceOnly": True,
            "status": "open",
            "filledSize": 0.0,
        }
    ])
    client_5b._request = AsyncMock(return_value={
        "result": "success",
        "fills": [{"order_id": "TP-PARTIAL", "symbol": symbol, "side": "sell", "size": 0.4, "price": 55000.0, "fillTime": "2026-11-15T18:00:00.000Z"}]
    })
    wrapper_5b._rest_client = client_5b

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[trade_5b]), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True) as mock_quar_5b, \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status") as mock_upd_5b:

        report_5b = await wrapper_5b.reconcile_on_startup()

        assert report_5b["startup_secure"] is False
        assert report_5b["global_entry_lock"] is True
        assert symbol in wrapper_5b._reconciliation_required_symbols
        # Protection updated with PARTIALLY_FILLED
        mock_upd_5b.assert_any_call(
            symbol=symbol,
            protection_type="tp",
            status="PARTIALLY_FILLED",
            order_id="ORD-TP-PARTIAL",
            protection_order_id="TP-PARTIAL",
            client_oid="BOT_TP-PARTIAL",
            error_message="TP partially filled: 0.4/1.0",
            broker="kraken",
        )

    # ── CASE 5C: ZERO FILLS (MISSING) ──
    trade_5c = make_trade("TP-ZERO-FILLS")
    wrapper_5c = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    client_5c = AsyncMock()
    client_5c.get_positions = AsyncMock(return_value=[
        {"symbol": symbol, "side": "long", "size": 1.0}
    ])
    client_5c.get_open_orders = AsyncMock(return_value=[
        {
            "order_id": "SL-5",
            "cliOrdId": "BOT_SL_5",
            "symbol": symbol,
            "side": "sell",
            "size": 1.0,
            "stopPrice": 48000.0,
            "orderType": "stp",
            "reduceOnly": True,
            "status": "open",
            "filledSize": 0.0,
        }
    ])
    client_5c._request = AsyncMock(return_value={
        "result": "success",
        "fills": []  # Zero fills!
    })
    wrapper_5c._rest_client = client_5c

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[trade_5c]), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True) as mock_quar_5c, \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status") as mock_upd_5c:

        report_5c = await wrapper_5c.reconcile_on_startup()

        assert report_5c["startup_secure"] is False
        assert report_5c["global_entry_lock"] is True
        assert symbol in wrapper_5c._reconciliation_required_symbols
        mock_upd_5c.assert_any_call(
            symbol=symbol,
            protection_type="tp",
            status="MISSING_ON_EXCHANGE",
            order_id="ORD-TP-ZERO-FILLS",
            client_oid="BOT_TP-ZERO-FILLS",
            error_message="TP missing on Kraken exchange without fills",
            broker="kraken",
        )










