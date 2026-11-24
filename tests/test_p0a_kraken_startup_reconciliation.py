"""
Phase P0-A — Isolated Tests for Kraken Startup Reconciliation Wiring
====================================================================

Verifies that MultiBrokerEngine invokes KrakenFuturesExecutorWrapper.reconcile_on_startup()
once, idempotently, and fail-closed before allowing any Kraken entry signals:
  1. Nominal startup: global entry lock released ONLY after successful reconciliation.
  2. phantom_db anomaly: global entry lock maintained (UNRESOLVED_ANOMALIES_LOCKED).
  3. Kraken API unavailable: global entry lock maintained (fail-closed).
  4. Double / concurrent calls across shared executor: single execution only.
  5. Signal received DURING reconciliation: blocked, zero premature orders.
  6. CLOSE and REDUCE operations: existing safety behavior preserved & existing
     positions untouched.

All tests run strictly in the RES-3.2 isolated sandbox with mocked Kraken
exchange endpoints — ZERO real orders or network requests are ever sent.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper, KrakenTradeTracker
from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor
from smc_futures.engine.multi_broker_engine import (
    Balance,
    BrokerStatus,
    BrokerType,
    MultiBrokerConfig,
    MultiBrokerEngine,
    Signal,
    SignalType,
    _KRAKEN_RECONCILE_IN_PROGRESS,
    _KRAKEN_RECONCILE_LOCKS,
    _KRAKEN_RECONCILE_REPORTS,
)


def _make_isolated_wrapper() -> KrakenFuturesExecutorWrapper:
    """Create a real KrakenFuturesExecutorWrapper with mocked network/REST layer."""
    wrapper = KrakenFuturesExecutorWrapper(
        api_key="test_key_sandbox",
        api_secret="dGVzdF9zZWNyZXRfc2FuZGJveA==",
        timeout=2.0,
        single_entry_mode=False,
    )
    # Guarantee NO real HTTP / REST calls can ever leave the sandbox
    mock_rest = MagicMock()
    mock_rest.send_order = AsyncMock(
        return_value={"result": "success", "sendStatus": {"order_id": "MOCK_OID_001", "status": "placed"}}
    )
    mock_rest.get_positions = AsyncMock(return_value=[])
    mock_rest.get_open_positions = AsyncMock(return_value=[])
    mock_rest.get_open_orders = AsyncMock(return_value=[])
    mock_rest.get_accounts = AsyncMock(return_value={"flex": {"availableMargin": 40.0, "portfolioValue": 40.0}})
    wrapper._rest_client = mock_rest
    wrapper.get_positions = AsyncMock(return_value=[])
    wrapper.get_open_orders = AsyncMock(return_value=[])
    wrapper._place_order_rest = AsyncMock(
        return_value={"code": "00000", "success": True, "order_id": "MOCK_REST_001", "data": {"orderId": "MOCK_REST_001"}}
    )
    return wrapper


def _make_isolated_kraken_executor(wrapper: KrakenFuturesExecutorWrapper) -> KrakenFuturesExecutor:
    """Create a real KrakenFuturesExecutor backed by the isolated wrapper (no live WS)."""
    executor = KrakenFuturesExecutor(
        api_key="test_key_sandbox",
        api_secret="dGVzdF9zZWNyZXRfc2FuZGJveA==",
        timeout=2.0,
        auto_reconnect=False,
        single_entry_mode=False,
        wrapper=wrapper,
    )
    executor._connected = True
    executor.connect = AsyncMock(return_value=None)
    executor.disconnect = AsyncMock(return_value=None)
    executor.get_account_info = AsyncMock(
        return_value={"available": 40.0, "total": 40.0, "margin_used": 0.0, "unrealized_pnl": 0.0}
    )
    executor.check_balance_sufficient = AsyncMock(return_value=(True, 40.0))
    return executor


def _make_engine(kraken_executor: KrakenFuturesExecutor) -> MultiBrokerEngine:
    config = MultiBrokerConfig(
        default_broker="kraken",
        bitget_enabled=False,
        kraken_enabled=True,
        kraken_api_key="test_key_sandbox",
        kraken_api_secret="dGVzdF9zZWNyZXRfc2FuZGJveA==",
        kraken_symbols=["PF_ETHUSD"],
        min_balance_usd=5.0,
    )
    engine = MultiBrokerEngine(config=config, kraken_executor=kraken_executor)
    engine._kraken_market = MagicMock()  # Avoid live market client init
    engine._balances[BrokerType.KRAKEN] = Balance(
        broker=BrokerType.KRAKEN, available=40.0, total=40.0, currency="USD"
    )
    return engine


def _make_entry_signal(symbol: str = "ETHUSDT") -> Signal:
    return Signal(
        symbol=symbol,
        signal_type=SignalType.LONG,
        entry_price=3000.0,
        stop_loss=2950.0,
        take_profit=3100.0,
        confidence=0.9,
        leverage=5,
    )


@pytest.mark.asyncio
async def test_nominal_startup_releases_lock_only_after_reconciliation(caplog, monkeypatch):
    """Nominal startup: lock starts True, is released ONLY after reconcile_on_startup succeeds."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    wrapper = _make_isolated_wrapper()
    executor = _make_isolated_kraken_executor(wrapper)
    engine = _make_engine(executor)

    # Pre-condition: fail-closed by default
    assert wrapper._global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False

    # Before initialize(), opening order MUST be blocked
    pre_res = await wrapper.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.01, order_type="market", entry_price=3000.0, stop_loss=2950.0, intent="open"
    )
    assert pre_res["success"] is False
    assert pre_res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    assert wrapper._place_order_rest.await_count == 0

    with caplog.at_level(logging.INFO, logger="smarttrade.multi_broker"):
        with (
            patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]),
            patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]),
        ):
            await engine.initialize()

    # Post-condition: lock released by reconcile_on_startup()
    assert wrapper._startup_reconciliation_completed is True
    assert wrapper._global_entry_lock is False
    assert wrapper._global_entry_lock_reason is None
    assert engine._kraken_reconciliation_report["status"] == "SUCCESS"
    assert engine._kraken_reconciliation_report["startup_secure"] is True

    # Structured logs present
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "STARTUP_RECONCILIATION_STARTED" in log_text
    assert "STARTUP_RECONCILIATION_COMPLETED" in log_text
    assert "GLOBAL_ENTRY_LOCK_RELEASED" in log_text

    # No real orders were ever dispatched during startup
    assert wrapper._place_order_rest.await_count == 0
    assert wrapper._rest_client.send_order.await_count == 0


@pytest.mark.asyncio
async def test_phantom_db_anomaly_maintains_global_entry_lock(caplog, monkeypatch):
    """phantom_db anomaly (PF_XBTUSD in DB, absent on Kraken): lock stays True (UNRESOLVED_ANOMALIES_LOCKED)."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    wrapper = _make_isolated_wrapper()
    executor = _make_isolated_kraken_executor(wrapper)
    engine = _make_engine(executor)

    phantom_trade = SimpleNamespace(
        id=999,
        order_id="PHANTOM_OID_1",
        client_oid="BOT_PF_XBTUSD_PHANTOM",
        symbol="PF_XBTUSD",
        side="long",
        qty=2.0,
        entry_price=90000.0,
        status="OPEN",
        get_raw_exchange=lambda: {},
    )

    with caplog.at_level(logging.INFO, logger="smarttrade.multi_broker"):
        with (
            patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]),
            patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[phantom_trade]),
            patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True),
            patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True),
        ):
            await engine.initialize()

    # Lock MUST remain active
    assert wrapper._global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False
    assert engine._kraken_reconciliation_report["status"] == "UNRESOLVED_ANOMALIES_LOCKED"
    assert engine._kraken_reconciliation_report["startup_secure"] is False
    assert engine._kraken_reconciliation_report["global_entry_lock"] is True

    # Structured logs present
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "STARTUP_RECONCILIATION_STARTED" in log_text
    assert "UNRESOLVED_ANOMALIES_LOCKED" in log_text
    assert "STARTUP_RECONCILIATION_FAILED" in log_text
    assert "GLOBAL_ENTRY_LOCK_RELEASED" not in log_text

    # Any new opening order MUST still be rejected fail-closed
    open_res = await wrapper.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.01, order_type="market", entry_price=3000.0, stop_loss=2950.0, intent="open"
    )
    assert open_res["success"] is False
    assert open_res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    assert wrapper._place_order_rest.await_count == 0
    assert wrapper._rest_client.send_order.await_count == 0


@pytest.mark.asyncio
async def test_kraken_api_unavailable_maintains_global_entry_lock(caplog, monkeypatch):
    """Kraken API error during startup reconciliation: lock remains True (fail-closed)."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    wrapper = _make_isolated_wrapper()
    wrapper._rest_client.get_positions = AsyncMock(side_effect=RuntimeError("Kraken REST 503 Service Unavailable"))
    wrapper.get_positions = AsyncMock(side_effect=RuntimeError("Kraken REST 503 Service Unavailable"))
    executor = _make_isolated_kraken_executor(wrapper)
    engine = _make_engine(executor)

    with caplog.at_level(logging.INFO, logger="smarttrade.multi_broker"):
        with (
            patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]),
            patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]),
        ):
            await engine.initialize()

    assert wrapper._global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False
    assert engine._kraken_reconciliation_report["startup_secure"] is False
    assert engine._kraken_reconciliation_report["global_entry_lock"] is True

    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "STARTUP_RECONCILIATION_STARTED" in log_text
    assert "STARTUP_RECONCILIATION_FAILED" in log_text
    assert "GLOBAL_ENTRY_LOCK_RELEASED" not in log_text

    # Opening order remains blocked; zero orders sent
    open_res = await wrapper.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.01, order_type="market", entry_price=3000.0, stop_loss=2950.0, intent="open"
    )
    assert open_res["success"] is False
    assert open_res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    assert wrapper._place_order_rest.await_count == 0


@pytest.mark.asyncio
async def test_double_and_concurrent_startup_reconciliation_runs_only_once():
    """Multiple components / concurrent calls sharing the same executor reconcile exactly once."""
    wrapper = _make_isolated_wrapper()
    executor = _make_isolated_kraken_executor(wrapper)
    engine_a = _make_engine(executor)
    engine_b = _make_engine(executor)

    original_reconcile = wrapper.reconcile_on_startup
    call_count = 0

    async def counted_reconcile():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return await original_reconcile()

    wrapper.reconcile_on_startup = counted_reconcile

    with (
        patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]),
        patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]),
    ):
        # Concurrent calls from two engines sharing the exact same executor/wrapper
        rep_a, rep_b = await asyncio.gather(
            engine_a.reconcile_kraken_on_startup(),
            engine_b.reconcile_kraken_on_startup(),
        )
        # Sequential repeat call
        rep_c = await engine_a.reconcile_kraken_on_startup()

    assert call_count == 1
    assert rep_a["status"] == "SUCCESS"
    assert rep_b["status"] == "SUCCESS"
    assert rep_c["status"] == "SUCCESS"
    assert wrapper._global_entry_lock is False
    assert wrapper._place_order_rest.await_count == 0


@pytest.mark.asyncio
async def test_signal_received_during_reconciliation_is_blocked_without_premature_open():
    """Signal arriving WHILE reconcile_on_startup() is in progress cannot open prematurely."""
    wrapper = _make_isolated_wrapper()
    executor = _make_isolated_kraken_executor(wrapper)
    engine = _make_engine(executor)

    reconcile_started = asyncio.Event()
    allow_reconcile_finish = asyncio.Event()
    original_reconcile = wrapper.reconcile_on_startup

    async def slow_reconcile():
        reconcile_started.set()
        await allow_reconcile_finish.wait()
        return await original_reconcile()

    wrapper.reconcile_on_startup = slow_reconcile

    with (
        patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]),
        patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]),
    ):
        init_task = asyncio.create_task(engine.initialize())
        await reconcile_started.wait()

        # While reconciliation is still running, attempt to process a Kraken signal AND direct order
        signal = _make_entry_signal("ETHUSDT")
        await engine._execute_signal_single(BrokerType.KRAKEN, signal)
        order_res = await engine.execute_order_kraken(symbol="ETHUSDT", side="buy", quantity=0.01)

        assert order_res.success is False
        assert "STARTUP_RECONCILIATION_IN_PROGRESS" in (order_res.error or "")
        assert engine._stats["orders_sent"] == 0
        assert wrapper._place_order_rest.await_count == 0
        assert wrapper._rest_client.send_order.await_count == 0

        # Let reconciliation complete
        allow_reconcile_finish.set()
        await init_task

    assert wrapper._global_entry_lock is False
    assert wrapper._startup_reconciliation_completed is True
    assert wrapper._place_order_rest.await_count == 0


@pytest.mark.asyncio
async def test_close_and_reduce_preserve_existing_safety_behavior_and_positions(monkeypatch):
    """
    When _global_entry_lock is True:
    - Existing open positions are preserved (never closed automatically by startup reconciliation).
    - Explicit CLOSE and REDUCE orders on non-quarantined symbols remain permitted.
    - New OPEN orders remain strictly blocked by GLOBAL_ENTRY_LOCK_ACTIVE.
    """
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    wrapper = _make_isolated_wrapper()
    existing_position = {
        "symbol": "PF_ETHUSD",
        "side": "long",
        "size": 0.05,
        "price": 3000.0,
    }
    wrapper._rest_client.get_positions = AsyncMock(return_value=[existing_position])
    wrapper.get_positions = AsyncMock(return_value=[existing_position])
    # Register active bot tracker for the existing open position
    wrapper._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="EXISTING_OID_1",
        client_oid="BOT_PF_ETHUSD_EXISTING",
        symbol="PF_ETHUSD",
        side="long",
        filled_qty=0.05,
        expected_exposure=0.05,
        status="OPEN",
    )

    # Confirm lock is active before any reconciliation
    assert wrapper._global_entry_lock is True

    # 1. OPEN order is blocked
    open_res = await wrapper.place_order(
        symbol="ETHUSDT",
        side="buy",
        quantity=0.01,
        order_type="market",
        entry_price=3000.0,
        stop_loss=2950.0,
        intent="open",
        reduce_only=False,
    )
    assert open_res["success"] is False
    assert open_res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    assert wrapper._place_order_rest.await_count == 0

    # 2. REDUCE order passes global entry lock gate (mocked REST, no real order)
    reduce_res = await wrapper.place_order(
        symbol="ETHUSDT",
        side="sell",
        quantity=0.01,
        order_type="market",
        entry_price=3000.0,
        intent="reduce",
        reduce_only=True,
    )
    assert reduce_res.get("code") != "GLOBAL_ENTRY_LOCK_ACTIVE"
    assert reduce_res.get("success") is True or reduce_res.get("code") == "00000"

    # Simulate exchange reflecting the 0.01 reduction (0.05 -> 0.04)
    existing_position["size"] = 0.04

    # 3. CLOSE order passes global entry lock gate (mocked REST, no real order)
    close_res = await wrapper.place_order(
        symbol="ETHUSDT",
        side="sell",
        quantity=0.04,
        order_type="market",
        entry_price=3000.0,
        intent="close",
        reduce_only=True,
    )
    assert close_res.get("code") != "GLOBAL_ENTRY_LOCK_ACTIVE"
    assert close_res.get("success") is True or close_res.get("code") == "00000"

    # Real Kraken REST client was NEVER called (only our local mock _place_order_rest)
    assert wrapper._rest_client.send_order.await_count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE P0-A.1 — STRICT READY, CANCELLATION, RETRY AFTER FAILURE, DISCONNECTED
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_p0a1_ready_strict_refuses_failed_report_and_blocks_orders_sent():
    """
    P0-A.1: Even after a failed reconciliation (phantom_db or 503) or if
    _kraken_reconciliation_done is True, _is_kraken_reconciliation_ready() MUST
    return False and block signals/orders BEFORE incrementing orders_sent.
    """
    wrapper = _make_isolated_wrapper()
    wrapper._rest_client.get_positions = AsyncMock(side_effect=RuntimeError("Kraken 503"))
    executor = _make_isolated_kraken_executor(wrapper)
    engine = _make_engine(executor)

    with (
        patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]),
        patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]),
    ):
        await engine.initialize()

    # Failed report must NOT be cached in _KRAKEN_RECONCILE_REPORTS
    assert wrapper not in _KRAKEN_RECONCILE_REPORTS
    assert engine._kraken_reconciliation_done is False

    # Even if someone artificially sets _kraken_reconciliation_done = True, READY must stay False
    engine._kraken_reconciliation_done = True
    ready, reason = engine._is_kraken_reconciliation_ready()
    assert ready is False
    assert reason != "READY"

    # Signal and direct order are blocked BEFORE orders_sent is incremented
    await engine._execute_signal_single(BrokerType.KRAKEN, _make_entry_signal("ETHUSDT"))
    res = await engine.execute_order_kraken(symbol="ETHUSDT", side="buy", quantity=0.01)
    assert res.success is False
    assert engine._stats["orders_sent"] == 0
    assert wrapper._place_order_rest.await_count == 0
    assert wrapper._rest_client.send_order.await_count == 0

    # Also verify: if startup_secure is True but global_entry_lock is still True
    # (e.g. SINGLE_ENTRY_MODE_CONSUMED), READY must STILL be False
    wrapper._startup_reconciliation_completed = True
    wrapper._global_entry_lock = True
    wrapper._global_entry_lock_reason = "SINGLE_ENTRY_MODE_CONSUMED"
    engine._kraken_reconciliation_report = {
        "status": "SUCCESS",
        "startup_secure": True,
        "global_entry_lock": True,
    }
    ready2, reason2 = engine._is_kraken_reconciliation_ready()
    assert ready2 is False
    assert "SINGLE_ENTRY_MODE_CONSUMED" in reason2


@pytest.mark.asyncio
async def test_p0a2_cancelled_error_preserves_lock_cleans_state_and_releases_asyncio_lock():
    """
    P0-A.2: asyncio.CancelledError during reconcile_on_startup() must:
    - keep _global_entry_lock = True and _startup_reconciliation_completed = False
    - clean _KRAKEN_RECONCILE_IN_PROGRESS and _kraken_reconciliation_in_progress
    - NOT poison _KRAKEN_RECONCILE_REPORTS
    - release the per-wrapper asyncio.Lock
    - propagate CancelledError
    - allow a clean subsequent reconciliation to succeed.
    """
    wrapper = _make_isolated_wrapper()
    executor = _make_isolated_kraken_executor(wrapper)
    engine = _make_engine(executor)

    started = asyncio.Event()
    original_reconcile = wrapper.reconcile_on_startup

    async def hanging_reconcile():
        started.set()
        await asyncio.Future()  # wait forever until cancelled

    wrapper.reconcile_on_startup = hanging_reconcile

    task = asyncio.create_task(engine.reconcile_kraken_on_startup())
    await started.wait()
    assert wrapper in _KRAKEN_RECONCILE_IN_PROGRESS
    assert engine._kraken_reconciliation_in_progress is True

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # State must be completely clean and fail-closed
    assert wrapper._global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False
    assert wrapper not in _KRAKEN_RECONCILE_IN_PROGRESS
    assert engine._kraken_reconciliation_in_progress is False
    assert engine._kraken_reconciliation_done is False
    assert wrapper not in _KRAKEN_RECONCILE_REPORTS
    assert not _KRAKEN_RECONCILE_LOCKS[wrapper].locked()

    ready, _ = engine._is_kraken_reconciliation_ready()
    assert ready is False

    # Subsequent clean reconciliation MUST NOT be blocked by a poisoned cache
    wrapper.reconcile_on_startup = original_reconcile
    with (
        patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]),
        patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]),
    ):
        rep = await engine.reconcile_kraken_on_startup()

    assert rep["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert engine._is_kraken_reconciliation_ready() == (True, "READY")
    assert wrapper._rest_client.send_order.await_count == 0


@pytest.mark.asyncio
async def test_p0a3_transient_failure_allows_retry_and_caches_only_on_success():
    """
    P0-A.3: Transient 503 error is NOT cached; controlled retry succeeds and
    is then cached idempotently so a 3rd call does not re-run reconcile_on_startup.
    """
    wrapper = _make_isolated_wrapper()
    executor = _make_isolated_kraken_executor(wrapper)
    engine = _make_engine(executor)

    original_reconcile = wrapper.reconcile_on_startup
    call_count = 0

    async def counted_reconcile():
        nonlocal call_count
        call_count += 1
        return await original_reconcile()

    wrapper.reconcile_on_startup = counted_reconcile

    # 1st attempt: transient Kraken API 503
    wrapper._rest_client.get_positions = AsyncMock(side_effect=RuntimeError("Kraken 503"))
    with (
        patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]),
        patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]),
    ):
        rep1 = await engine.reconcile_kraken_on_startup()

    assert call_count == 1
    assert rep1["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert wrapper not in _KRAKEN_RECONCILE_REPORTS
    assert engine._is_kraken_reconciliation_ready()[0] is False

    # 2nd attempt: Kraken API recovered -> retry runs and succeeds
    wrapper._rest_client.get_positions = AsyncMock(return_value=[])
    with (
        patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]),
        patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]),
    ):
        rep2 = await engine.reconcile_kraken_on_startup()

    assert call_count == 2
    assert rep2["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert wrapper in _KRAKEN_RECONCILE_REPORTS
    assert engine._is_kraken_reconciliation_ready() == (True, "READY")

    # 3rd attempt: idempotent after success -> does NOT re-invoke reconcile_on_startup
    rep3 = await engine.reconcile_kraken_on_startup()
    assert call_count == 2
    assert rep3["startup_secure"] is True
    assert wrapper._rest_client.send_order.await_count == 0


@pytest.mark.asyncio
async def test_p0a1_disconnected_kraken_refuses_reconciliation_without_caching_failure(caplog):
    """
    Requirement 5: If Kraken broker is not connected, reconcile_kraken_on_startup()
    keeps _global_entry_lock=True, does NOT mark done, does NOT cache failure,
    does NOT invoke wrapper.reconcile_on_startup(), and logs clearly.
    """
    wrapper = _make_isolated_wrapper()
    wrapper.reconcile_on_startup = AsyncMock()
    executor = _make_isolated_kraken_executor(wrapper)
    engine = _make_engine(executor)
    engine._broker_status[BrokerType.KRAKEN] = BrokerStatus(
        broker=BrokerType.KRAKEN,
        connected=False,
        error="WebSocket handshake failed",
    )

    with caplog.at_level(logging.INFO, logger="smarttrade.multi_broker"):
        rep = await engine.reconcile_kraken_on_startup()

    assert rep["status"] == "KRAKEN_NOT_CONNECTED"
    assert rep["startup_secure"] is False
    assert rep["global_entry_lock"] is True
    assert wrapper.reconcile_on_startup.await_count == 0
    assert wrapper._global_entry_lock is True
    assert engine._kraken_reconciliation_done is False
    assert wrapper not in _KRAKEN_RECONCILE_REPORTS
    assert engine._is_kraken_reconciliation_ready()[0] is False

    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "STARTUP_RECONCILIATION_FAILED" in log_text
    assert "KRAKEN_NOT_CONNECTED" in log_text
    assert wrapper._rest_client.send_order.await_count == 0

