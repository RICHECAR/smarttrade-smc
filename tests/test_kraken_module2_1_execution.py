"""
Module 2.1 Test Suite — Real Kraken Futures Execution & Safeguards.
═════════════════════════════════════════════════════════════════════

Tests:
1. LONG fully executed (fills, VWAP, SQLite LiveTrade, SL/TP attached, tracker synchronized).
2. SHORT fully executed.
3. Partial execution (size adapted, partial fill flagged, protections sized to real fill).
4. ACK without fill (NO_FILL, no position assumed, no protections placed).
5. Timeout (fail-closed, symbol quarantined, no blind retry).
6. Ambiguous response (unexpected response format, quarantined).
7. Duplicate client_oid (idempotency rejection, 0 network calls).
8. SQLite failure after execution (fail-closed, symbol quarantined, global lock).
9. SL failure (emergency close executed, position closed cleanly).
10. TP failure (position preserved under SL only, TP_FAILED_SL_ONLY flagged).
11. Reboot after open before sync (startup reconciliation catches discrepancy, locks globally).
12. Risk budget gate (refuses order exceeding authorized risk budget, e.g., 2 EUR cap).
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)


@pytest.fixture
def clean_executor():
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock_key", api_secret="c2VjcmV0")
    # Simulate a successful startup reconciliation so entry gates are open
    wrapper._global_entry_lock = False
    wrapper._global_entry_lock_reason = None
    wrapper._startup_reconciliation_completed = True
    wrapper._reconciliation_required_symbols.clear()
    wrapper._active_trackers.clear()
    wrapper._active_client_oids.clear()

    # Pre-flight gate mocks (clean position & carnet)
    wrapper._check_preflight_position_gate = AsyncMock(return_value=(True, None, 0.0))
    wrapper._check_preflight_open_orders_gate = AsyncMock(return_value=(True, None))
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.0)

    # Verification helper for open orders
    wrapper._verify_protection_in_open_orders = AsyncMock(return_value=(True, None))
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# 1. LONG FULLY EXECUTED
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_module2_1_scenario1_long_fully_executed(clean_executor):
    """
    Module 2.1 - Test 1: LONG order fully executed.
    - Market order sent for 1.0 BTC on PF_XBTUSD.
    - Fills confirmed: 1.0 @ 50100.0 (VWAP = 50100.0).
    - Position confirmed: 1.0 long.
    - SL placed @ 48000.0 (size 1.0), TP placed @ 55000.0 (size 1.0).
    - SQLite LiveTrade persisted with entry=50100.0, qty=1.0, status='OPEN'.
    - Tracker active in memory with entry_price=50100.0.
    """
    symbol = "PF_XBTUSD"
    order_id = "ORD-LONG-FULL-101"
    sl_id = "SL-LONG-101"
    tp_id = "TP-LONG-101"

    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(return_value={
        "result": "success", "order_id": order_id, "status": "placed",
    })
    mock_client.place_order = AsyncMock(side_effect=lambda symbol, side, order_type, size, **kw: {
        "result": "success",
        "order_id": sl_id if order_type == "stp" else tp_id,
        "status": "placed",
    })
    mock_client._request = AsyncMock(return_value={
        "result": "success",
        "fills": [{"order_id": order_id, "symbol": symbol, "side": "buy", "size": 1.0, "price": 50100.0, "fillTime": "2026-11-15T19:00:00Z"}]
    })
    clean_executor._rest_client = mock_client
    clean_executor._fetch_position_qty_strict = AsyncMock(side_effect=lambda sym, side: 1.0 if side == "long" else 0.0)

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=MagicMock()) as mock_create, \
         patch("backend.services.live_trades_service.LiveTradesService.record_protection_intent", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):

        res = await clean_executor.place_order(
            symbol=symbol,
            side="buy",
            quantity=1.0,
            order_type="market",
            entry_price=50000.0,
            stop_loss=48000.0,
            take_profit=55000.0,
            leverage=10,
        )

        assert res["success"] is True
        assert res["order_id"] == order_id
        assert abs(res["filled_qty"] - 1.0) < 1e-6
        assert abs(res["price"] - 50100.0) < 1e-6
        assert res["is_partial_fill"] is False
        assert res["sl_attached"] is True
        assert res["tp_attached"] is True

        # SQLite formal trade creation
        mock_create.assert_called_once()
        create_kwargs = mock_create.call_args[1]
        assert create_kwargs["order_id"] == order_id
        assert create_kwargs["symbol"] == symbol
        assert create_kwargs["side"] == "long"
        assert abs(create_kwargs["qty"] - 1.0) < 1e-6
        assert abs(create_kwargs["entry"] - 50100.0) < 1e-6
        assert create_kwargs["status"] == "OPEN"

        # Tracker state
        tracker = clean_executor._active_trackers.get(symbol)
        assert tracker is not None
        assert tracker.status == "OPEN"
        assert abs(tracker.filled_qty - 1.0) < 1e-6
        assert abs(tracker.entry_price - 50100.0) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# 2. SHORT FULLY EXECUTED
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_module2_1_scenario2_short_fully_executed(clean_executor):
    """
    Module 2.1 - Test 2: SHORT order fully executed.
    - Market order sent for 0.5 ETH on PF_ETHUSD.
    - Fills confirmed: 0.5 @ 3000.0.
    - Position confirmed: 0.5 short.
    - Protections placed, SQLite persisted, tracker active.
    """
    symbol = "PF_ETHUSD"
    order_id = "ORD-SHORT-FULL-202"

    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(return_value={
        "result": "success", "order_id": order_id, "status": "placed",
    })
    mock_client.place_order = AsyncMock(return_value={
        "result": "success", "order_id": "PROT-ORD", "status": "placed",
    })
    mock_client._request = AsyncMock(return_value={
        "result": "success",
        "fills": [{"order_id": order_id, "symbol": symbol, "side": "sell", "size": 0.5, "price": 3000.0, "fillTime": "2026-11-15T19:00:00Z"}]
    })
    clean_executor._rest_client = mock_client
    clean_executor._fetch_position_qty_strict = AsyncMock(side_effect=lambda sym, side: 0.5 if side == "short" else 0.0)

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=MagicMock()) as mock_create, \
         patch("backend.services.live_trades_service.LiveTradesService.record_protection_intent", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):

        res = await clean_executor.place_order(
            symbol=symbol,
            side="sell",
            quantity=0.5,
            order_type="market",
            entry_price=3000.0,
            stop_loss=3100.0,
            take_profit=2800.0,
            leverage=10,
        )

        assert res["success"] is True
        assert res["order_id"] == order_id
        assert abs(res["filled_qty"] - 0.5) < 1e-6
        assert abs(res["price"] - 3000.0) < 1e-6
        assert res["is_partial_fill"] is False

        create_kwargs = mock_create.call_args[1]
        assert create_kwargs["side"] == "short"
        assert abs(create_kwargs["qty"] - 0.5) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# 3. PARTIAL EXECUTION
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_module2_1_scenario3_partial_execution(clean_executor):
    """
    Module 2.1 - Test 3: Partial execution on exchange.
    - Requested quantity: 1.0.
    - Exchange fills show only 0.4 executed @ 50000.0.
    - Live position: 0.4.
    - Real executed quantity (0.4) is confirmed (not theoretical 1.0).
    - Protections placed for 0.4, SQLite trade created with qty=0.4, is_partial_fill=True.
    """
    symbol = "PF_XBTUSD"
    order_id = "ORD-PARTIAL-303"

    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(return_value={
        "result": "success", "order_id": order_id, "status": "placed",
    })
    mock_client.place_order = AsyncMock(return_value={
        "result": "success", "order_id": "PROT-ORD", "status": "placed",
    })
    mock_client._request = AsyncMock(return_value={
        "result": "success",
        "fills": [{"order_id": order_id, "symbol": symbol, "side": "buy", "size": 0.4, "price": 50000.0, "fillTime": "2026-11-15T19:00:00Z"}]
    })
    clean_executor._rest_client = mock_client
    clean_executor._fetch_position_qty_strict = AsyncMock(side_effect=lambda sym, side: 0.4 if side == "long" else 0.0)

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=MagicMock()) as mock_create, \
         patch("backend.services.live_trades_service.LiveTradesService.record_protection_intent", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):

        res = await clean_executor.place_order(
            symbol=symbol,
            side="buy",
            quantity=1.0,
            order_type="market",
            entry_price=50000.0,
            stop_loss=48000.0,
            take_profit=55000.0,
        )

        assert res["success"] is True
        assert abs(res["filled_qty"] - 0.4) < 1e-6
        assert abs(res["requested_qty"] - 1.0) < 1e-6
        assert res["is_partial_fill"] is True

        create_kwargs = mock_create.call_args[1]
        assert abs(create_kwargs["qty"] - 0.4) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# 4. ACK SANS FILL
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_module2_1_scenario4_ack_without_fill(clean_executor):
    """
    Module 2.1 - Test 4: ACK received from Kraken but 0 fills executed.
    - Market order returns result='success', status='placed'.
    - But fills query returns 0 fills for this order, position is 0.0.
    - Rule: An ACK does NOT constitute an execution!
    - Outcome: success=False, status='NO_FILL', filled_qty=0.0.
    - No SQLite trade created, no protections placed.
    """
    symbol = "PF_XBTUSD"
    order_id = "ORD-ACK-NO-FILL-404"

    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(return_value={
        "result": "success", "order_id": order_id, "status": "placed",
    })
    mock_client._request = AsyncMock(return_value={
        "result": "success",
        "fills": []  # 0 fills!
    })
    clean_executor._rest_client = mock_client
    clean_executor._fetch_position_qty_strict = AsyncMock(return_value=0.0)

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade") as mock_create:
        res = await clean_executor.place_order(
            symbol=symbol,
            side="buy",
            quantity=1.0,
            order_type="market",
            entry_price=50000.0,
            stop_loss=48000.0,
        )

        assert res["success"] is False
        assert res["status"] == "NO_FILL"
        assert res["code"] == "NO_FILL"
        assert res["filled_qty"] == 0.0
        mock_create.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 5. TIMEOUT
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_module2_1_scenario5_timeout_quarantines_and_fails_closed(clean_executor):
    """
    Module 2.1 - Test 5: Network/REST Timeout during order placement.
    - place_market_order raises asyncio.TimeoutError.
    - Rule: Never blind retry after timeout! Ambiguous state triggers reconciliation.
    - Outcome: success=False, code='TIMEOUT_AMBIGUOUS_RECONCILIATION_REQUIRED', symbol quarantined.
    """
    symbol = "PF_XBTUSD"

    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(side_effect=asyncio.TimeoutError("Kraken gateway timeout"))
    clean_executor._rest_client = mock_client

    res = await clean_executor.place_order(
        symbol=symbol,
        side="buy",
        quantity=1.0,
        order_type="market",
    )

    assert res["success"] is False
    assert "TIMEOUT" in res.get("code", "")
    assert symbol in clean_executor._reconciliation_required_symbols


# ─────────────────────────────────────────────────────────────────────────────
# 6. RÉPONSE AMBIGUË
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_module2_1_scenario6_ambiguous_response_quarantines(clean_executor):
    """
    Module 2.1 - Test 6: Ambiguous/unparseable response from exchange.
    - Exchange returns unexpected structure (e.g. integer or corrupt payload).
    - Rule: Ambiguous responses must trigger reconciliation.
    - Outcome: Symbol quarantined in _reconciliation_required_symbols, fail-closed.
    """
    symbol = "PF_XBTUSD"

    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(return_value="CORRUPT_NON_DICT_STRING")
    clean_executor._rest_client = mock_client

    res = await clean_executor.place_order(
        symbol=symbol,
        side="buy",
        quantity=1.0,
        order_type="market",
    )

    assert res["success"] is False
    assert "AMBIGUOUS" in res.get("code", "")
    assert symbol in clean_executor._reconciliation_required_symbols


# ─────────────────────────────────────────────────────────────────────────────
# 7. DUPLICATE CLIENT_OID
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_module2_1_scenario7_duplicate_client_oid_rejected(clean_executor):
    """
    Module 2.1 - Test 7: Duplicate client_oid guard (Idempotency).
    - First order with client_oid='BOT_PF_XBTUSD_IDEMP_777' is in-flight.
    - Second order with identical client_oid is attempted.
    - Outcome: Duplicate is rejected with DUPLICATE_CLIENT_OID, 0 network calls sent.
    """
    symbol = "PF_XBTUSD"
    cid = "BOT_PF_XBTUSD_IDEMP_777"

    clean_executor._active_client_oids.add(cid)
    mock_client = AsyncMock()
    clean_executor._rest_client = mock_client

    res = await clean_executor.place_order(
        symbol=symbol,
        side="buy",
        quantity=1.0,
        order_type="market",
        client_oid=cid,
    )

    assert res["success"] is False
    assert res["code"] == "DUPLICATE_CLIENT_OID"
    mock_client.place_market_order.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 8. ÉCHEC SQLITE APRÈS EXÉCUTION
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_module2_1_scenario8_sqlite_failure_after_execution_quarantines(clean_executor):
    """
    Module 2.1 - Test 8: SQLite persistence fails after real order fill on Kraken.
    - Order fills on exchange (1.0 BTC @ 50000.0).
    - LiveTradesService.create_live_trade returns None or raises DB error.
    - Rule: Position is open on Kraken but unrecorded in DB -> FAIL-CLOSED.
    - Outcome: Symbol quarantined, _global_entry_lock=True, tracker marked MANUAL_RECONCILIATION_REQUIRED.
    """
    symbol = "PF_XBTUSD"
    order_id = "ORD-SQL-FAIL-808"

    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(return_value={
        "result": "success", "order_id": order_id, "status": "placed",
    })
    mock_client._request = AsyncMock(return_value={
        "result": "success",
        "fills": [{"order_id": order_id, "symbol": symbol, "side": "buy", "size": 1.0, "price": 50000.0, "fillTime": "2026-11-15T19:00:00Z"}]
    })
    clean_executor._rest_client = mock_client
    clean_executor._fetch_position_qty_strict = AsyncMock(side_effect=lambda sym, side: 1.0 if side == "long" else 0.0)

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=None):
        res = await clean_executor.place_order(
            symbol=symbol,
            side="buy",
            quantity=1.0,
            order_type="market",
            entry_price=50000.0,
        )

        assert res["success"] is False
        assert res["code"] == "SQLITE_PERSISTENCE_FAILED"
        assert symbol in clean_executor._reconciliation_required_symbols
        assert clean_executor.global_entry_lock is True

        tracker = clean_executor._active_trackers.get(symbol)
        assert tracker is not None
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"


# ─────────────────────────────────────────────────────────────────────────────
# 9. ÉCHEC SL (EMERGENCY CLOSE)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_module2_1_scenario9_sl_failure_triggers_emergency_close(clean_executor):
    """
    Module 2.1 - Test 9: SL placement fails -> Emergency market close triggered.
    - Order fills 1.0 on exchange.
    - SL placement fails repeatedly (retries exhausted).
    - Emergency market close is triggered on Kraken and succeeds.
    - Outcome: Position closed, tracker marked CLOSED, status='SL_FAILED_POSITION_EMERGENCY_CLOSED'.
    """
    symbol = "PF_XBTUSD"
    order_id = "ORD-SL-FAIL-909"

    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(side_effect=[
        # 1. Opening market order
        {"result": "success", "order_id": order_id, "status": "placed"},
        # 2. Emergency close market order
        {"result": "success", "order_id": "EMERGENCY-CLOSE-ORD", "status": "placed"},
    ])
    # SL order fails
    mock_client.place_order = AsyncMock(return_value={"result": "error", "error": "Kraken SL rejected"})
    mock_client._request = AsyncMock(return_value={
        "result": "success",
        "fills": [{"order_id": order_id, "symbol": symbol, "side": "buy", "size": 1.0, "price": 50000.0, "fillTime": "2026-11-15T19:00:00Z"}]
    })
    clean_executor._rest_client = mock_client
    clean_executor._fetch_position_qty_strict = AsyncMock(side_effect=lambda sym, side: 1.0 if side == "long" else 0.0)
    clean_executor.check_position_integrity = AsyncMock(return_value=(True, None, 1.0))

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=MagicMock()), \
         patch("backend.services.live_trades_service.LiveTradesService.record_protection_intent", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):

        res = await clean_executor.place_order(
            symbol=symbol,
            side="buy",
            quantity=1.0,
            order_type="market",
            entry_price=50000.0,
            stop_loss=48000.0,
        )

        assert res["success"] is False
        assert res["status"] == "SL_FAILED_POSITION_EMERGENCY_CLOSED"
        assert res["sl_attached"] is False
        assert res["_sl_emergency_close"] is True
        assert res["_sl_emergency_close_ok"] is True

        tracker = clean_executor._active_trackers.get(symbol)
        assert tracker is not None
        assert tracker.status == "CLOSED"


# ─────────────────────────────────────────────────────────────────────────────
# 10. ÉCHEC TP (PROTECTION SL ONLY)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_module2_1_scenario10_tp_failure_preserves_sl_protection(clean_executor):
    """
    Module 2.1 - Test 10: TP fails but SL is successfully attached.
    - SL succeeds and protects the position.
    - TP retries are exhausted.
    - Position is NOT closed (it is safely protected by SL).
    - Outcome: success=True, status='TP_FAILED_SL_ONLY', sl_attached=True, tp_attached=False.
    """
    symbol = "PF_XBTUSD"
    order_id = "ORD-TP-FAIL-1010"

    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(return_value={
        "result": "success", "order_id": order_id, "status": "placed",
    })
    # SL succeeds, TP fails
    mock_client.place_order = AsyncMock(side_effect=lambda symbol, side, order_type, size, **kw: (
        {"result": "success", "order_id": "SL-ORD-VALID", "status": "placed"}
        if order_type == "stp" else
        {"result": "error", "error": "Kraken TP rejected"}
    ))
    mock_client._request = AsyncMock(return_value={
        "result": "success",
        "fills": [{"order_id": order_id, "symbol": symbol, "side": "buy", "size": 1.0, "price": 50000.0, "fillTime": "2026-11-15T19:00:00Z"}]
    })
    clean_executor._rest_client = mock_client
    clean_executor._fetch_position_qty_strict = AsyncMock(side_effect=lambda sym, side: 1.0 if side == "long" else 0.0)
    clean_executor.check_position_integrity = AsyncMock(return_value=(True, None, 1.0))

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=MagicMock()), \
         patch("backend.services.live_trades_service.LiveTradesService.record_protection_intent", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):

        res = await clean_executor.place_order(
            symbol=symbol,
            side="buy",
            quantity=1.0,
            order_type="market",
            entry_price=50000.0,
            stop_loss=48000.0,
            take_profit=55000.0,
        )

        assert res["success"] is True
        assert res["status"] == "TP_FAILED_SL_ONLY"
        assert res["sl_attached"] is True
        assert res["tp_attached"] is False
        assert res["_tp_failed"] is True

        tracker = clean_executor._active_trackers.get(symbol)
        assert tracker is not None
        assert tracker.status == "OPEN"
        assert tracker.sl_order_id == "SL-ORD-VALID"


# ─────────────────────────────────────────────────────────────────────────────
# 11. REDÉMARRAGE APRÈS OUVERTURE AVANT SYNCHRONISATION
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_module2_1_scenario11_reboot_after_open_before_sync_locks():
    """
    Module 2.1 - Test 11: Reboot after opening on Kraken before DB synchronization.
    - Kraken exchange has an active open position (0.5 BTC).
    - SQLite database has NO open trade (crash happened before create_live_trade).
    - Fresh instance starts up and runs reconcile_on_startup().
    - Invariant: Startup reconciliation identifies unknown active position, quarantines symbol,
      and activates global entry lock.
    """
    symbol = "PF_XBTUSD"

    fresh_wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[
        {"symbol": symbol, "side": "long", "size": 0.5, "price": 50000.0}
    ])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    fresh_wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", return_value=True):

        report = await fresh_wrapper.reconcile_on_startup()

        assert report["startup_secure"] is False
        assert report["global_entry_lock"] is True
        assert fresh_wrapper.global_entry_lock is True
        assert fresh_wrapper.is_startup_secure is False
        assert symbol in fresh_wrapper._reconciliation_required_symbols


# ─────────────────────────────────────────────────────────────────────────────
# 12. RISK BUDGET PRE-FLIGHT GATE (40 € CAPITAL / 2 € PERTE MAX)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_module2_1_scenario12_risk_budget_gate_refuses_excessive_risk(clean_executor):
    """
    Module 2.1 - Test 12: Risk budget enforcement.
    - Reference capital: 40 €, max risk per trade cap: 2 € (~$2.16 USD).
    - Order requested with risk = 1.0 * (50000 - 45000) = $5000.0 > $2.16.
    - Rule: If an order risk exceeds authorized budget, refuse it!
    - Outcome: Refused with code='RISK_EXCEEDS_BUDGET', 0 exchange calls sent.
    """
    symbol = "PF_XBTUSD"

    mock_client = AsyncMock()
    clean_executor._rest_client = mock_client

    res = await clean_executor.place_order(
        symbol=symbol,
        side="buy",
        quantity=1.0,
        order_type="market",
        entry_price=50000.0,
        stop_loss=45000.0,
        max_risk_usd=2.16,
    )

    assert res["success"] is False
    assert res["code"] == "RISK_EXCEEDS_BUDGET"
    mock_client.place_market_order.assert_not_called()
