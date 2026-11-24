"""
Tests for Kraken Single-Entry Execution Mode (KRAKEN_SINGLE_ENTRY_MODE)
========================================================================

Validates the 5 mandatory conditions and sizing rules:
1. Persistent Lock: Survives backend reboots via SQLite.
2. Atomic Reservation: Pre-allocates single authorization before dispatch,
   preventing concurrent signals from opening multiple positions.
3. First Fill Consumes: Any fill (full or partial) permanently consumes authorization.
4. Ambiguous Protection: Timeout or unknown response locks authorization indefinitely.
5. Asymmetric Permission: Blocks all new entries while allowing SL/TP, reductions,
   closures, and startup reconciliation.
6. Sizing Gate: Refuses orders below min_size without artificial inflation.

Zero live orders transmitted to exchange API during testing.
"""

import asyncio
import os
import pytest
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper, KrakenTradeTracker
from backend.execution.kraken_single_entry_manager import KrakenSingleEntryManager


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for single-entry tests."""
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
    mgr = KrakenSingleEntryManager(db_path=temp_db)
    return mgr


@pytest.fixture
def executor(temp_db, single_entry_mgr):
    """Instantiate KrakenFuturesExecutorWrapper wired to the test single_entry_mgr."""
    exec_wrap = KrakenFuturesExecutorWrapper(
        api_key="test_key",
        api_secret="dGVzdF9zZWNyZXQ=",
    )
    exec_wrap._startup_reconciliation_completed = True
    exec_wrap._global_entry_lock = False
    exec_wrap._global_entry_lock_reason = None
    exec_wrap._single_entry_mode = True
    exec_wrap._single_entry_manager = single_entry_mgr

    # Pre-flight gate mocks
    exec_wrap._check_preflight_position_gate = AsyncMock(return_value=(True, None, 0.0))
    exec_wrap._check_preflight_open_orders_gate = AsyncMock(return_value=(True, None))
    exec_wrap._verify_protection_in_open_orders = AsyncMock(return_value=(True, None))
    return exec_wrap


@pytest.mark.asyncio
async def test_single_entry_atomic_reservation_success(single_entry_mgr):
    """Condition 2: Atomic reservation succeeds when IDLE."""
    ok, reason = single_entry_mgr.reserve_entry("PF_XBTUSD", "CLIENT_OID_1")
    assert ok is True
    assert reason == "RESERVED"
    
    state = single_entry_mgr.get_state()
    assert state["state"] == "RESERVED"
    assert state["symbol"] == "PF_XBTUSD"
    assert state["client_oid"] == "CLIENT_OID_1"


@pytest.mark.asyncio
async def test_single_entry_atomic_reservation_rejects_second(single_entry_mgr):
    """Condition 2: Atomic reservation blocks a second reservation attempt."""
    ok1, reason1 = single_entry_mgr.reserve_entry("PF_XBTUSD", "CLIENT_OID_1")
    assert ok1 is True
    
    ok2, reason2 = single_entry_mgr.reserve_entry("PF_XBTUSD", "CLIENT_OID_2")
    assert ok2 is False
    assert "RESERVED" in reason2


@pytest.mark.asyncio
async def test_single_entry_concurrency_race_condition(executor):
    """Condition 2: Two simultaneous entry orders result in exactly one execution and one rejection."""
    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(return_value={"result": "success", "order_id": "ord1", "status": "placed"})
    mock_client.place_order = AsyncMock(return_value={"result": "success", "order_id": "ord1", "status": "placed"})
    mock_client._request = AsyncMock(return_value={
        "result": "success",
        "fills": [{"order_id": "ord1", "symbol": "PF_XBTUSD", "side": "buy", "size": 0.001, "price": 90000.0, "fillTime": "2026-11-15T19:00:00Z"}]
    })
    executor._rest_client = mock_client
    executor._fetch_position_qty_strict = AsyncMock(side_effect=lambda sym, side: 0.001 if side == "long" else 0.0)

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=MagicMock()), \
         patch("backend.services.live_trades_service.LiveTradesService.record_protection_intent", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):

        # Launch two concurrent entry orders
        t1 = executor.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=0.001,
            order_type="market",
            price=90000.0,
            stop_loss=88500.0,
            intent="open",
            client_order_id="RACE_OID_1",
        )
        t2 = executor.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=0.001,
            order_type="market",
            price=90000.0,
            stop_loss=88500.0,
            intent="open",
            client_order_id="RACE_OID_2",
        )

        r1, r2 = await asyncio.gather(t1, t2)
        results = [r1, r2]
        
        # Exactly one succeeded and one was blocked with SINGLE_ENTRY_LOCKED
        successes = [r for r in results if r.get("success") is True]
        blocked = [r for r in results if r.get("code") in ("SINGLE_ENTRY_LOCKED", "GLOBAL_ENTRY_LOCK_ACTIVE")]

        assert len(successes) == 1, f"Expected 1 success, got {len(successes)}: {results}"
        assert len(blocked) == 1, f"Expected 1 blocked by single-entry gate, got {len(blocked)}: {results}"


@pytest.mark.asyncio
async def test_single_entry_first_fill_consumes_permission(executor):
    """Condition 3: First fill permanently consumes authorization and engages global entry lock."""
    order_id = "ORD-SINGLE-FILL-1"
    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(return_value={"result": "success", "order_id": order_id, "status": "placed"})
    mock_client.place_order = AsyncMock(return_value={"result": "success", "order_id": order_id, "status": "placed"})
    mock_client._request = AsyncMock(return_value={
        "result": "success",
        "fills": [{"order_id": order_id, "symbol": "PF_XBTUSD", "side": "buy", "size": 0.001, "price": 90000.0, "fillTime": "2026-11-15T19:00:00Z"}]
    })
    executor._rest_client = mock_client
    executor._fetch_position_qty_strict = AsyncMock(side_effect=lambda sym, side: 0.001 if side == "long" else 0.0)

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=MagicMock()), \
         patch("backend.services.live_trades_service.LiveTradesService.record_protection_intent", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):

        res1 = await executor.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=0.001,
            order_type="market",
            price=90000.0,
            stop_loss=88500.0,
            intent="open",
            client_order_id="TEST_OID_CONSUMED",
        )
        assert res1.get("success") is True
        
        # Verify manager state is CONSUMED
        state = executor._single_entry_manager.get_state()
        assert state["state"] == "CONSUMED"
        assert state["fill_qty"] == 0.001
        assert executor.global_entry_lock is True
        assert executor._global_entry_lock_reason == "SINGLE_ENTRY_MODE_CONSUMED"

        # Any 2nd OPEN order must be strictly blocked
        res2 = await executor.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=0.001,
            order_type="market",
            price=90000.0,
            stop_loss=88500.0,
            intent="open",
            client_order_id="TEST_OID_SECOND",
        )
        assert res2.get("success") is False
        assert res2.get("code") in ("SINGLE_ENTRY_LOCKED", "GLOBAL_ENTRY_LOCK_ACTIVE")


@pytest.mark.asyncio
async def test_single_entry_partial_fill_consumes_permission(executor):
    """Condition 3: Even a partial fill consumes the single-entry authorization permanently."""
    order_id = "ORD-PARTIAL-1"
    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(return_value={"result": "success", "order_id": order_id, "status": "placed"})
    mock_client.place_order = AsyncMock(return_value={"result": "success", "order_id": order_id, "status": "placed"})
    # Requested 0.001, but only 0.0005 filled
    mock_client._request = AsyncMock(return_value={
        "result": "success",
        "fills": [{"order_id": order_id, "symbol": "PF_XBTUSD", "side": "buy", "size": 0.0005, "price": 90000.0, "fillTime": "2026-11-15T19:00:00Z"}]
    })
    executor._rest_client = mock_client
    executor._fetch_position_qty_strict = AsyncMock(side_effect=lambda sym, side: 0.0005 if side == "long" else 0.0)

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=MagicMock()), \
         patch("backend.services.live_trades_service.LiveTradesService.record_protection_intent", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):

        res = await executor.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=0.001,
            order_type="market",
            price=90000.0,
            stop_loss=88500.0,
            intent="open",
            client_order_id="TEST_OID_PARTIAL",
        )
        assert res.get("success") is True
        assert res.get("is_partial_fill") is True
        assert res.get("filled_qty") == 0.0005

        # Authorization must be CONSUMED
        state = executor._single_entry_manager.get_state()
        assert state["state"] == "CONSUMED"
        assert state["fill_qty"] == 0.0005

        # Subsequent entry blocked
        res2 = await executor.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=0.001,
            intent="open",
        )
        assert res2.get("success") is False
        assert res2.get("code") in ("SINGLE_ENTRY_LOCKED", "GLOBAL_ENTRY_LOCK_ACTIVE")


@pytest.mark.asyncio
async def test_single_entry_timeout_locks_ambiguous(executor):
    """Condition 4: Timeout during REST dispatch locks single entry ambiguous and fails closed."""
    mock_client = AsyncMock()
    mock_client.place_market_order = AsyncMock(side_effect=asyncio.TimeoutError("Network timeout"))
    mock_client.place_order = AsyncMock(side_effect=asyncio.TimeoutError("Network timeout"))
    executor._rest_client = mock_client

    res = await executor.place_order(
        symbol="PF_XBTUSD",
        side="buy",
        quantity=0.001,
        order_type="market",
        price=90000.0,
        intent="open",
        client_order_id="TIMEOUT_OID",
    )
    assert res.get("success") is False
    assert "TIMEOUT" in res.get("code")

    # State in SQLite must be LOCKED_AMBIGUOUS
    state = executor._single_entry_manager.get_state()
    assert state["state"] == "LOCKED_AMBIGUOUS"

    # Second entry order blocked
    res2 = await executor.place_order(
        symbol="PF_XBTUSD",
        side="buy",
        quantity=0.001,
        intent="open",
    )
    assert res2.get("success") is False
    assert res2.get("code") in ("SINGLE_ENTRY_LOCKED", "GLOBAL_ENTRY_LOCK_ACTIVE")


@pytest.mark.asyncio
async def test_single_entry_persistence_survives_reboot(temp_db, single_entry_mgr):
    """Condition 1: Consumed single-entry state persists across backend reboot and locks entry."""
    # Step 1: Pre-set state to CONSUMED in SQLite
    single_entry_mgr.consume_entry(order_id="REBOOT_ORDER_123", fill_qty=0.001, symbol="PF_XBTUSD")
    assert single_entry_mgr.get_state()["state"] == "CONSUMED"

    # Step 2: Create a fresh executor instance (reboot simulation)
    new_exec = KrakenFuturesExecutorWrapper(api_key="k", api_secret="cw==")
    new_exec._single_entry_mode = True
    new_exec._single_entry_manager = KrakenSingleEntryManager(db_path=temp_db)

    # Step 3: Run startup reconciliation
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    new_exec._rest_client = mock_client

    with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]), \
         patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
        report = await new_exec.reconcile_on_startup()

    # Verify global lock is engaged due to CONSUMED single-entry mode
    assert new_exec.global_entry_lock is True
    assert new_exec._global_entry_lock_reason == "SINGLE_ENTRY_MODE_CONSUMED"
    assert report.get("single_entry_mode") == "CONSUMED"

    # Step 4: Verify that a new entry order is rejected
    res = await new_exec.place_order(
        symbol="PF_XBTUSD",
        side="buy",
        quantity=0.001,
        intent="open",
    )
    assert res.get("success") is False
    assert res.get("code") in ("SINGLE_ENTRY_LOCKED", "GLOBAL_ENTRY_LOCK_ACTIVE")


@pytest.mark.asyncio
async def test_single_entry_allows_protection_and_close_while_locked(executor):
    """Condition 5: While entry is locked (consumed), SL/TP and close operations remain fully authorized."""
    # Lock the entry gate
    executor._single_entry_manager.consume_entry("ORD_EXISTING", 0.001, "PF_XBTUSD")
    executor._global_entry_lock = True
    executor._global_entry_lock_reason = "SINGLE_ENTRY_MODE_CONSUMED"

    # Setup tracker for existing position
    tracker = KrakenTradeTracker(
        order_id="ORD_EXISTING",
        client_oid="BOT_EXISTING",
        symbol="PF_XBTUSD",
        side="long",
        filled_qty=0.001,
        expected_exposure=0.001,
        entry_price=90000.0,
        status="OPEN",
    )
    executor._active_trackers["PF_XBTUSD"] = tracker

    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[{"symbol": "PF_XBTUSD", "size": 0.001, "side": "long"}])
    mock_client.get_open_orders = AsyncMock(return_value=[])
    mock_client.place_market_order = AsyncMock(return_value={"result": "success", "order_id": "close_ord", "status": "placed"})
    mock_client.place_order = AsyncMock(return_value={"result": "success", "order_id": "close_ord", "status": "placed"})
    executor._rest_client = mock_client
    executor._fetch_position_qty_strict = AsyncMock(side_effect=lambda sym, side: 0.001 if side == "long" else 0.0)

    # Placing a REDUCE or CLOSE order must NOT be blocked by the single-entry lock
    with patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=True):
        close_res = await executor.close_position("PF_XBTUSD", reason="TEST_CLOSE")
    
    assert close_res.get("success") is True or close_res.get("code") == "00000"


@pytest.mark.asyncio
async def test_single_entry_below_min_size_rejected_without_inflation(executor):
    """Sizing Rule: Orders below min_size are rejected (BELOW_MIN_SIZE) and NEVER artificially inflated."""
    # Min size for PF_XBTUSD is 0.0001 BTC. Pass 0.00005 BTC.
    res = await executor.place_order(
        symbol="PF_XBTUSD",
        side="buy",
        quantity=0.00005,
        order_type="market",
        price=90000.0,
        stop_loss=88500.0,
        intent="open",
        client_order_id="BELOW_MIN_OID",
    )
    assert res.get("success") is False
    assert res.get("code") in ("BELOW_MIN_SIZE", "BELOW_MIN_RISK_BUDGET")
    
    # Verify reservation remains IDLE (not wasted by a pre-flight rejected order)
    state = executor._single_entry_manager.get_state()
    assert state["state"] == "IDLE"
