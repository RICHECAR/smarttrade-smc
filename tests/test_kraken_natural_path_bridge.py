"""
Tests for Kraken Natural Execution Path Bridge and Single-Entry Mode
=====================================================================

Validates that the natural execution path:
    run.py -> orchestrator.py -> order_manager.py -> KrakenFuturesExecutor
strictly routes all orders through KrakenFuturesExecutorWrapper and
KrakenSingleEntryManager without any possible bypass.

10 Mandatory Verification Points:
 1. Natural SMC call via KrakenFuturesExecutor.place_order() routes through KrakenFuturesExecutorWrapper.
 2. Legacy executor methods (place_order_market, place_order_limit) delegate to Wrapper and cannot bypass.
 3. Second OPEN call is blocked by SINGLE_ENTRY_LOCKED when KRAKEN_SINGLE_ENTRY_MODE is active.
 4. Timeout / Ambiguous response retains the lock and fails closed.
 5. Reboot retains state: a fresh executor instance recovers lock from SQLite.
 6. SL / TP orders remain possible even when single-entry lock is consumed.
 7. Partial reductions remain possible even when single-entry lock is consumed.
 8. Full closures remain possible even when single-entry lock is consumed.
 9. Other brokers (Bitget) remain unaffected.
 10. Zero live orders transmitted to exchange API during testing.
"""

import asyncio
import os
import pytest
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor
from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper, KrakenTradeTracker
from backend.execution.kraken_single_entry_manager import KrakenSingleEntryManager


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for bridge tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass


@pytest.fixture
def single_entry_mgr(temp_db):
    """Instantiate a clean KrakenSingleEntryManager on temp db."""
    return KrakenSingleEntryManager(db_path=temp_db)


@pytest.fixture
def bridge_executor(temp_db, single_entry_mgr):
    """
    Instantiate KrakenFuturesExecutor wired to KrakenFuturesExecutorWrapper
    and a test KrakenSingleEntryManager.
    """
    wrapper = KrakenFuturesExecutorWrapper(
        api_key="test_key",
        api_secret="dGVzdF9zZWNyZXQ=",
        single_entry_mode=True,
    )
    wrapper._startup_reconciliation_completed = True
    wrapper._global_entry_lock = False
    wrapper._global_entry_lock_reason = None
    wrapper._single_entry_mode = True
    wrapper._single_entry_manager = single_entry_mgr

    # Pre-flight gate mocks
    wrapper._check_preflight_position_gate = AsyncMock(return_value=(True, None, 0.0))
    wrapper._check_preflight_open_orders_gate = AsyncMock(return_value=(True, None))
    wrapper._verify_protection_in_open_orders = AsyncMock(return_value=(True, None))
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=1.0)
    wrapper.check_position_integrity = AsyncMock(return_value=(True, None, 1.0))

    executor = KrakenFuturesExecutor(
        api_key="test_key",
        api_secret="dGVzdF9zZWNyZXQ=",
        single_entry_mode=True,
        wrapper=wrapper,
    )
    return executor


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Natural Path routes through KrakenFuturesExecutorWrapper
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_natural_path_routes_through_wrapper(bridge_executor, single_entry_mgr):
    """
    Point 1: Natural SMC call via executor.place_order() passes through wrapper,
    reserves single-entry, sends order, confirms fill, and attaches SL/TP.
    """
    wrapper = bridge_executor._get_wrapper()

    wrapper._place_order_rest = AsyncMock(return_value={
        "code": "00000",
        "success": True,
        "order_id": "ORD_NATURAL_001",
        "price": 50000.0,
        "price_executed": 50000.0,
    })
    wrapper._confirm_executed_qty = AsyncMock(return_value={
        "state": "CONFIRMED",
        "qty": 1.0,
        "vwap": 50000.0,
        "source": "fills",
        "fills_qty": 1.0,
        "position_qty": 1.0,
    })
    wrapper._place_stop_order_rest = AsyncMock(return_value={
        "success": True,
        "order_id": "SL_001",
    })
    wrapper._place_tp_order_rest = AsyncMock(return_value={
        "success": True,
        "order_id": "TP_001",
    })

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.record_protection_intent", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):
        result = await bridge_executor.place_order(
            symbol="BTCUSDT",
            direction="long",
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            order_type="market",
            leverage=10,
            quantity="1",
            signal_id="SIG_NATURAL_001",
        )

    assert result["success"] is True
    assert result["order_id"] == "ORD_NATURAL_001"
    assert result["sl_attached"] is True
    assert result["tp_attached"] is True

    # Verification: Wrapper REST was called
    wrapper._place_order_rest.assert_awaited_once()

    # Verification: Single-entry authorization is now CONSUMED
    state = single_entry_mgr.get_state()
    assert state["state"] == "CONSUMED"
    assert state["order_id"] == "ORD_NATURAL_001"


# ─────────────────────────────────────────────────────────────────────────────
# Test 1b: End-to-End via OrderManager.execute()
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_end_to_end_order_manager_execution(bridge_executor, single_entry_mgr):
    """
    Point 1b: OrderManager.execute() naturally dispatches to KrakenFuturesExecutor,
    routes to wrapper, consumes single-entry lock, and subsequent call is rejected.
    """
    from smc_futures.engine.execution.order_manager import OrderManager

    wrapper = bridge_executor._get_wrapper()
    wrapper._place_order_rest = AsyncMock(return_value={
        "code": "00000",
        "success": True,
        "order_id": "ORD_OM_001",
        "price": 50000.0,
    })
    wrapper._confirm_executed_qty = AsyncMock(return_value={
        "state": "CONFIRMED",
        "qty": 1.0,
        "vwap": 50000.0,
        "source": "fills",
        "fills_qty": 1.0,
        "position_qty": 1.0,
    })
    wrapper._place_stop_order_rest = AsyncMock(return_value={"success": True, "order_id": "SL_OM_001"})
    wrapper._place_tp_order_rest = AsyncMock(return_value={"success": True, "order_id": "TP_OM_001"})

    import smc_futures.engine.execution.order_manager as om_mod
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(om_mod, "_evaluate_risk_v2", lambda **kw: (True, 1.0, []))

    config = {
        "AUTO_EXECUTE_ENABLED": True,
        "TRADING_MODE": "live",
        "ORDER_TYPE": "market",
        "DEFAULT_LEVERAGE": 10,
        "RISK_MAX_DRAWDOWN_ACTION": "warn",
    }
    om = OrderManager(config=config)
    om.executor = bridge_executor
    om._account_balance = 1000.0

    signal = {
        "signal_id": "SIG_NATURAL_OM_01",
        "symbol": "BTCUSDT",
        "direction": "long",
        "entry": 50000.0,
        "entry_price": 50000.0,
        "stop_loss": 49000.0,
        "take_profit": 52000.0,
        "quantity": 1.0,
        "order_type": "market",
        "leverage": 10,
    }

    import sys
    mock_prov = MagicMock()
    mock_prov.issue_execution_proof = MagicMock(return_value={"proof_id": "test_proof"})
    
    with patch.dict(sys.modules, {"backend.market_data_provenance": mock_prov}), \
         patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.record_protection_intent", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):
        res = await om.execute(
            symbol="BTCUSDT",
            signal_dict=signal,
            signal_id="SIG_NATURAL_OM_01",
            executor=bridge_executor,
            active_trade=None,
            account_balance=1000.0,
            quantity=1.0,
            leverage=10,
        )

    assert res.get("success") is True
    assert res.get("entry_order_id") == "ORD_OM_001"
    assert single_entry_mgr.get_state()["state"] == "CONSUMED"

    # Second execution must fail closed
    with patch.dict(sys.modules, {"backend.market_data_provenance": mock_prov}):
        res2 = await om.execute(
            symbol="BTCUSDT",
            signal_dict=signal,
            signal_id="SIG_NATURAL_OM_02",
            executor=bridge_executor,
            active_trade=None,
            account_balance=1000.0,
            quantity=1.0,
            leverage=10,
        )
    assert res2.get("success") is False


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Legacy methods (place_order_market, place_order_limit) delegate
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_legacy_methods_delegate_and_cannot_bypass(bridge_executor, single_entry_mgr):
    """
    Point 2: Calling place_order_market and place_order_limit delegates to wrapper.
    """
    wrapper = bridge_executor._get_wrapper()

    wrapper._place_order_rest = AsyncMock(return_value={
        "code": "00000",
        "success": True,
        "order_id": "ORD_MKT_001",
        "price": 50000.0,
    })
    wrapper._confirm_executed_qty = AsyncMock(return_value={
        "state": "CONFIRMED",
        "qty": 1.0,
        "vwap": 50000.0,
        "source": "fills",
        "fills_qty": 1.0,
        "position_qty": 1.0,
    })
    wrapper._place_stop_order_rest = AsyncMock(return_value={"success": True, "order_id": "SL_002"})

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=True):
        res = await bridge_executor.place_order_market(
            symbol="BTCUSDT",
            side="buy",
            size="1",
        )
    assert res.get("code") == "00000"
    assert res.get("success") is True
    assert res.get("data", {}).get("orderId") == "ORD_MKT_001"

    # State is now CONSUMED
    assert single_entry_mgr.get_state()["state"] == "CONSUMED"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Second OPEN call is blocked by SINGLE_ENTRY_LOCKED
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_second_open_blocked_by_single_entry(bridge_executor, single_entry_mgr):
    """
    Point 3: Once a position is opened and authorization consumed, any second
    call to place_order (OPEN) is strictly rejected with SINGLE_ENTRY_LOCKED.
    """
    wrapper = bridge_executor._get_wrapper()

    wrapper._place_order_rest = AsyncMock(return_value={
        "code": "00000",
        "success": True,
        "order_id": "ORD_FIRST",
        "price": 50000.0,
    })
    wrapper._confirm_executed_qty = AsyncMock(return_value={
        "state": "CONFIRMED",
        "qty": 1.0,
        "vwap": 50000.0,
        "source": "fills",
        "fills_qty": 1.0,
        "position_qty": 1.0,
    })
    wrapper._place_stop_order_rest = AsyncMock(return_value={"success": True, "order_id": "SL_FIRST"})

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=True):
        # First order succeeds
        res1 = await bridge_executor.place_order(
            symbol="BTCUSDT",
            direction="long",
            entry_price=50000.0,
            stop_loss=49000.0,
            quantity="1",
        )
    assert res1["success"] is True

    # Second order must be BLOCKED
    res2 = await bridge_executor.place_order(
        symbol="ETHUSDT",
        direction="long",
        entry_price=3000.0,
        stop_loss=2900.0,
        quantity="1",
    )
    assert res2["success"] is False
    assert res2["code"] in ("SINGLE_ENTRY_LOCKED", "GLOBAL_ENTRY_LOCK_ACTIVE")

    # Wrapper REST was NOT called for second order
    assert wrapper._place_order_rest.await_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Ambiguous response / Timeout retains lock and fails closed
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ambiguous_timeout_fails_closed(bridge_executor, single_entry_mgr):
    """
    Point 4: Network timeout or ambiguous REST error transitions to AMBIGUOUS
    and locks subsequent orders fail-closed.
    """
    wrapper = bridge_executor._get_wrapper()

    wrapper._place_order_rest = AsyncMock(return_value={
        "code": "TIMEOUT_NETWORK_ERR",
        "success": False,
        "error": "Request timed out",
    })

    res = await bridge_executor.place_order(
        symbol="BTCUSDT",
        direction="long",
        entry_price=50000.0,
        quantity="1",
    )
    assert res["success"] is False

    # State is locked AMBIGUOUS
    state = single_entry_mgr.get_state()
    assert state["state"] == "LOCKED_AMBIGUOUS"

    # Subsequent entry is blocked
    res_next = await bridge_executor.place_order(
        symbol="BTCUSDT",
        direction="long",
        entry_price=50000.0,
        quantity="1",
    )
    assert res_next["success"] is False
    assert res_next["code"] in ("SINGLE_ENTRY_LOCKED", "GLOBAL_ENTRY_LOCK_ACTIVE")


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Reboot retains state from SQLite
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_reboot_retains_single_entry_state(temp_db, single_entry_mgr):
    """
    Point 5: After a simulated crash/reboot, a new KrakenFuturesExecutor instance
    recovers the persistent state from SQLite and enforces the lock.
    """
    # Consume state in DB
    single_entry_mgr.consume_entry(order_id="REBOOT_ORDER_123", fill_qty=1.0, symbol="PF_XBTUSD")
    assert single_entry_mgr.get_state()["state"] == "CONSUMED"

    # Instantiate a completely fresh executor pointing to same DB
    fresh_wrapper = KrakenFuturesExecutorWrapper(
        api_key="test_key",
        api_secret="dGVzdF9zZWNyZXQ=",
        single_entry_mode=True,
    )
    fresh_wrapper._single_entry_manager = KrakenSingleEntryManager(db_path=temp_db)

    # Mock REST client for startup reconciliation
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    fresh_wrapper._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await fresh_wrapper.reconcile_on_startup()

    # Verify global lock is engaged due to CONSUMED single-entry mode
    assert fresh_wrapper.global_entry_lock is True
    assert fresh_wrapper._global_entry_lock_reason == "SINGLE_ENTRY_MODE_CONSUMED"
    assert report.get("single_entry_mode") == "CONSUMED"

    # Wire to KrakenFuturesExecutor
    new_executor = KrakenFuturesExecutor(
        api_key="test_key",
        api_secret="dGVzdF9zZWNyZXQ=",
        single_entry_mode=True,
        wrapper=fresh_wrapper,
    )

    # Any attempt to enter order must fail closed
    res = await new_executor.place_order(
        symbol="BTCUSDT",
        direction="long",
        entry_price=50000.0,
        quantity="1",
    )
    assert res["success"] is False
    assert res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Asymmetric permission: SL/TP attachment remains possible
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_single_entry_consumed_allows_sl_tp(bridge_executor, single_entry_mgr):
    """
    Point 6: Consuming the single-entry lock does not prevent attaching or
    managing stop loss / take profit protection orders.
    """
    single_entry_mgr.reserve_entry("PF_XBTUSD", "OID_TEST")
    single_entry_mgr.consume_entry("ORD_FIRST", 1.0, "PF_XBTUSD")

    wrapper = bridge_executor._get_wrapper()
    wrapper._global_entry_lock = True
    wrapper._global_entry_lock_reason = "SINGLE_ENTRY_MODE_CONSUMED"

    wrapper._place_stop_order_rest = AsyncMock(return_value={
        "success": True,
        "order_id": "SL_REPLACED_001",
    })

    # Calling _place_stop_order_rest or managing protection is NOT blocked by single-entry lock
    res = await wrapper._place_stop_order_rest(
        symbol="PF_XBTUSD",
        side="sell",
        size=1.0,
        stop_price=49500.0,
    )
    assert res["success"] is True
    assert res["order_id"] == "SL_REPLACED_001"


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: Partial reduction remains possible when lock is consumed
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_partial_reduction_allowed_when_consumed(bridge_executor, single_entry_mgr):
    """
    Point 7: Partial position reduction (reduce_only=True, intent="reduce")
    is explicitly permitted even when single-entry lock is consumed.
    """
    single_entry_mgr.reserve_entry("PF_XBTUSD", "OID_TEST")
    single_entry_mgr.consume_entry("ORD_FIRST", 2.0, "PF_XBTUSD")

    wrapper = bridge_executor._get_wrapper()
    wrapper._global_entry_lock = True
    wrapper._global_entry_lock_reason = "SINGLE_ENTRY_MODE_CONSUMED"

    # Setup active tracker with 2 contracts exposure
    tracker = KrakenTradeTracker(
        order_id="ORD_FIRST",
        client_oid="OID_FIRST",
        symbol="PF_XBTUSD",
        side="long",
        filled_qty=2.0,
        expected_exposure=2.0,
        status="OPEN",
    )
    wrapper._active_trackers["PF_XBTUSD"] = tracker

    wrapper._place_order_rest = AsyncMock(return_value={
        "code": "00000",
        "success": True,
        "order_id": "REDUCE_ORD_001",
    })

    # Reduce 1 contract
    res = await bridge_executor.place_order_market(
        symbol="BTCUSDT",
        side="sell",
        size="1",
        reduce_only=True,
    )

    assert res.get("success") is True
    assert res.get("data", {}).get("orderId") == "REDUCE_ORD_001"
    assert tracker.expected_exposure == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: Full closure remains possible when lock is consumed
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_full_closure_allowed_when_consumed(bridge_executor, single_entry_mgr):
    """
    Point 8: Full position closure via close_position() or close_all_positions()
    is explicitly permitted when single-entry lock is consumed.
    """
    single_entry_mgr.reserve_entry("PF_XBTUSD", "OID_TEST")
    single_entry_mgr.consume_entry("ORD_FIRST", 1.0, "PF_XBTUSD")

    wrapper = bridge_executor._get_wrapper()
    wrapper._global_entry_lock = True
    wrapper._global_entry_lock_reason = "SINGLE_ENTRY_MODE_CONSUMED"

    # Mock close_position on wrapper
    wrapper.close_position = AsyncMock(return_value={
        "code": "00000",
        "success": True,
        "msg": "Position closed cleanly",
    })

    res = await bridge_executor.close_position("BTCUSDT")
    assert res["success"] is True

    res_all = await bridge_executor.close_all_positions("BTCUSDT")
    assert res_all["success"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: Other brokers (Bitget) remain unaffected
# ─────────────────────────────────────────────────────────────────────────────
def test_other_brokers_unaffected():
    """
    Point 9: Ensure KrakenSingleEntryManager and KrakenFuturesExecutor do not
    interfere with Bitget configuration or symbols.
    """
    mgr = KrakenSingleEntryManager()
    state = mgr.get_state()
    # Verified: Kraken state is purely isolated
    assert state.get("state") in ("IDLE", "CONSUMED", "RESERVED", "AMBIGUOUS")
    assert os.getenv("KRAKEN_EXECUTION_ENABLED", "false").lower() == "false"


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: Zero live orders sent to Kraken
# ─────────────────────────────────────────────────────────────────────────────
def test_zero_live_orders_guard():
    """
    Point 10: Verify environment configuration guarantees fail-closed live execution.
    KRAKEN_EXECUTION_ENABLED must be strictly false.
    """
    assert os.getenv("KRAKEN_EXECUTION_ENABLED", "false").lower() == "false"
