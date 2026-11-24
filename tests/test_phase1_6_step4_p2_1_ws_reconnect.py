"""
Phase 1.6 / Step 4 — P2-1 WebSocket Disconnect / Reconnect Mandatory Reconciliation Suite (100% Offline)
======================================================================================================
Covers all 12 mandatory offline scenarios for P2-1:
  1. WS disconnect -> global entry lock immediately activated, OPEN blocked.
  2. WS reconnect success -> REST reconciliation triggered.
  3. WS reconnect failure / exhausted -> global lock preserved.
  4. REST unavailable during reconnect -> global lock preserved.
  5. Ambiguous / malformed REST response -> global lock preserved.
  6. Position anomaly on Kraken -> global lock & symbol lock preserved.
  7. Persistent SQLite quarantine -> global lock preserved.
  8. SQLite failure during reconnect -> global lock preserved.
  9. Clean reconciliation after reconnect -> lock released ONLY after all invariants hold.
 10. Disconnect mid-reconciliation -> stale reconciliation result CANNOT release lock.
 11. Two rapid reconnect events -> zero concurrent reconciliations (serialized).
 12. REDUCE/CLOSE behavior preserved (allowed on tracked symbol under global lock; blind close refused).

Strict isolation:
  - External sockets hard-blocked.
  - Temporary SQLite DB via pytest `tmp_path` ONLY.
"""

import asyncio
import os
import socket
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class _ExternalNetworkBlocked(RuntimeError):
    pass


_ORIG_CONNECT = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) and address else ""
    if host not in ("127.0.0.1", "::1"):
        raise _ExternalNetworkBlocked(f"External network blocked in Step 4 P2-1: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in Step 4 P2-1: {args}")


socket.socket.connect = _guarded_connect
socket.create_connection = _blocked_external
socket.getaddrinfo = _blocked_external

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SMARTTRADE_TEST_MODE"] = "1"
os.environ["KRAKEN_RETRY_ON_ORDER_REJECT"] = "1"
os.environ["KRAKEN_POS_RESOLVE_ATTEMPTS"] = "1"
os.environ["KRAKEN_POS_RESOLVE_DELAY"] = "0.0"

from backend.db.base import Base
from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper, KrakenTradeTracker
from backend.services.live_trades_service import LiveTradesService


@pytest.fixture
def isolated_sqlite(tmp_path, monkeypatch):
    db_file = tmp_path / "test_step4_p2_1.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    @contextmanager
    def _temp_session():
        db = SessionLocal()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr("backend.services.live_trades_service.get_db_session", _temp_session)
    yield db_file, _temp_session
    engine.dispose()


class FakeRestClientP21:
    def __init__(
        self,
        positions: Any = None,
        open_orders: Any = None,
        positions_exc: Optional[BaseException] = None,
        open_orders_exc: Optional[BaseException] = None,
        delay_positions: float = 0.0,
        on_positions_hook: Any = None,
    ):
        self.positions = [] if positions is None else positions
        self.open_orders = [] if open_orders is None else open_orders
        self.positions_exc = positions_exc
        self.open_orders_exc = open_orders_exc
        self.delay_positions = delay_positions
        self.on_positions_hook = on_positions_hook
        self.positions_calls = 0
        self.open_orders_calls = 0
        self.exec_calls: List[Dict[str, Any]] = []
        self.active_reconcile_calls = 0
        self.max_concurrent_reconcile_calls = 0

    async def get_positions(self, **kwargs):
        self.positions_calls += 1
        self.active_reconcile_calls += 1
        self.max_concurrent_reconcile_calls = max(self.max_concurrent_reconcile_calls, self.active_reconcile_calls)
        try:
            if self.delay_positions > 0:
                await asyncio.sleep(self.delay_positions)
            if self.on_positions_hook is not None:
                self.on_positions_hook()
            if self.positions_exc is not None:
                raise self.positions_exc
            if isinstance(self.positions, list):
                return list(self.positions)
            return self.positions
        finally:
            self.active_reconcile_calls -= 1

    async def get_open_orders(self, symbol: Optional[str] = None, **kwargs):
        self.open_orders_calls += 1
        if self.open_orders_exc is not None:
            raise self.open_orders_exc
        if isinstance(self.open_orders, list):
            if symbol:
                return [o for o in self.open_orders if str(o.get("symbol", "")).upper() == symbol.upper()]
            return list(self.open_orders)
        return self.open_orders

    async def place_market_order(self, **kwargs):
        self.exec_calls.append({"method": "place_market_order", **kwargs})
        return {"success": True, "code": "00000", "order_id": "SIM_REDUCE_P21", "price": 3000.0, "filled_size": float(kwargs.get("size", 0.01))}

    async def place_limit_order(self, **kwargs):
        self.exec_calls.append({"method": "place_limit_order", **kwargs})
        return {"success": False, "code": "SIM_FORBIDDEN"}

    async def place_order(self, **kwargs):
        self.exec_calls.append({"method": "place_order", **kwargs})
        return {"success": False, "code": "SIM_FORBIDDEN"}


class FakeWsClient:
    def __init__(self, connected: bool = True):
        self._connected = connected
        self.on_disconnect_callback = None
        self.on_reconnect_callback = None


def _make_wrapper(client: FakeRestClientP21) -> KrakenFuturesExecutorWrapper:
    w = KrakenFuturesExecutorWrapper(
        api_key="test_k_p21",
        api_secret="dGVzdF9zX3AyMQ==",
        timeout=2.0,
        single_entry_mode=False,
    )
    w._rest_client = client
    w._get_rest_client = lambda: client
    return w


@pytest.mark.asyncio
async def test_p2_1_1_ws_disconnect_immediately_locks_global_and_blocks_open(isolated_sqlite, monkeypatch):
    """1. Confirmed WS disconnect immediately activates global entry lock & blocks OPEN."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeRestClientP21()
    wrapper = _make_wrapper(client)
    ws = FakeWsClient(connected=True)
    wrapper.bind_ws_client(ws)

    # First reconcile succeeds -> unlocked
    rep0 = await wrapper.reconcile_on_startup()
    assert rep0["startup_secure"] is True
    assert wrapper._global_entry_lock is False

    # WS disconnects -> immediate fail-closed lock
    ws._connected = False
    ws.on_disconnect_callback("1006 abnormal closure")

    assert wrapper._global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False
    assert wrapper._ws_reconciliation_required is True
    assert wrapper._reconcile_epoch == 1

    res = await wrapper.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.05, order_type="market",
        entry_price=3000.0, stop_loss=2940.0, intent="open", reduce_only=False,
    )
    assert res["success"] is False
    assert res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_p2_1_2_ws_reconnect_triggers_mandatory_rest_reconciliation(isolated_sqlite):
    """2. Successful WS reconnect triggers REST reconciliation (positions + open_orders)."""
    client = FakeRestClientP21()
    wrapper = _make_wrapper(client)
    ws = FakeWsClient(connected=False)
    wrapper.bind_ws_client(ws)

    ws.on_disconnect_callback("drop")
    assert wrapper._global_entry_lock is True

    ws._connected = True
    task = ws.on_reconnect_callback()
    assert task is not None
    rep = await task

    assert rep["startup_secure"] is True
    assert client.positions_calls == 1
    assert client.open_orders_calls == 1
    assert wrapper._global_entry_lock is False
    assert wrapper._ws_reconciliation_required is False


@pytest.mark.asyncio
async def test_p2_1_3_ws_reconnect_unsuccessful_keeps_lock(isolated_sqlite):
    """3. Unsuccessful WS reconnect (ws_connected=False) preserves global lock and triggers 0 REST calls."""
    client = FakeRestClientP21()
    wrapper = _make_wrapper(client)
    wrapper.on_ws_disconnect("WS_RECONNECT_EXHAUSTED")

    rep = await wrapper.on_ws_reconnect(ws_connected=False)

    assert rep["status"] == "WS_NOT_CONNECTED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert wrapper._ws_reconciliation_required is True
    assert client.positions_calls == 0


@pytest.mark.asyncio
async def test_p2_1_4_rest_unavailable_during_reconnect_keeps_lock(isolated_sqlite):
    """4. REST unavailable (timeout/503) during post-reconnect reconciliation preserves lock."""
    client = FakeRestClientP21(positions_exc=asyncio.TimeoutError("Kraken REST timeout"))
    wrapper = _make_wrapper(client)
    wrapper.on_ws_disconnect("drop")

    rep = await wrapper.on_ws_reconnect(ws_connected=True)

    assert rep["startup_secure"] is False
    assert rep["status"] == "EXCHANGE_RECONCILIATION_FAILED"
    assert wrapper._global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False


@pytest.mark.asyncio
async def test_p2_1_5_ambiguous_malformed_rest_response_keeps_lock(isolated_sqlite):
    """5. Ambiguous / malformed REST response during post-reconnect reconciliation preserves lock."""
    client = FakeRestClientP21(positions={"result": "error", "error": "ambiguous"})
    wrapper = _make_wrapper(client)
    wrapper.on_ws_disconnect("drop")

    rep = await wrapper.on_ws_reconnect(ws_connected=True)

    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False


@pytest.mark.asyncio
async def test_p2_1_6_position_anomaly_on_reconnect_keeps_global_and_symbol_lock(isolated_sqlite):
    """6. Untracked orphan position discovered on Kraken after WS reconnect locks global + symbol."""
    client = FakeRestClientP21(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
    )
    wrapper = _make_wrapper(client)
    wrapper.on_ws_disconnect("drop")

    rep = await wrapper.on_ws_reconnect(ws_connected=True)

    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols
    assert len(LiveTradesService.get_quarantined_trades(broker="kraken")) == 1


@pytest.mark.asyncio
async def test_p2_1_7_persistent_sqlite_quarantine_keeps_lock_after_reconnect(isolated_sqlite):
    """7. Existing SQLite quarantine prevents lock release on WS reconnect."""
    LiveTradesService.record_exchange_quarantine(
        symbol="PF_XBTUSD", order_id="ORPHAN_P21_7", anomaly_type="position",
        reason="Pre-existing quarantine", broker="kraken", raw_exchange={"symbol": "PF_XBTUSD"},
    )
    client = FakeRestClientP21()
    wrapper = _make_wrapper(client)
    wrapper.on_ws_disconnect("drop")

    rep = await wrapper.on_ws_reconnect(ws_connected=True)

    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols


@pytest.mark.asyncio
async def test_p2_1_8_sqlite_failure_during_reconnect_keeps_lock(isolated_sqlite, monkeypatch):
    """8. SQLite failure during post-reconnect reconciliation preserves lock fail-closed."""
    client = FakeRestClientP21()
    wrapper = _make_wrapper(client)
    wrapper.on_ws_disconnect("drop")
    monkeypatch.setattr(
        LiveTradesService,
        "get_quarantined_trades",
        staticmethod(lambda **kw: (_ for _ in ()).throw(RuntimeError("database is locked"))),
    )

    rep = await wrapper.on_ws_reconnect(ws_connected=True)

    assert rep["status"] == "DATABASE_UNAVAILABLE_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False


@pytest.mark.asyncio
async def test_p2_1_9_reconnect_alone_never_releases_lock_only_clean_reconciliation_does(isolated_sqlite):
    """9. Reconnect alone does not release lock; lock releases ONLY after full reconciliation invariants pass."""
    client = FakeRestClientP21()
    wrapper = _make_wrapper(client)
    wrapper.on_ws_disconnect("drop")

    # Even if WS marks connected again, lock MUST remain True until reconciliation completes
    wrapper._ws_connected = True
    assert wrapper._global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False

    rep = await wrapper.on_ws_reconnect(ws_connected=True)
    assert rep["startup_secure"] is True
    assert rep["status"] == "SUCCESS"
    assert wrapper._global_entry_lock is False
    assert wrapper._startup_reconciliation_completed is True
    assert wrapper._ws_reconciliation_required is False


@pytest.mark.asyncio
async def test_p2_1_10_disconnect_during_reconciliation_prevents_stale_unlock(isolated_sqlite):
    """10. Newer WS disconnect mid-reconciliation invalidates epoch; stale reconcile CANNOT release lock."""
    wrapper_ref: List[KrakenFuturesExecutorWrapper] = []

    def _drop_mid_flight():
        # Simulate WS dropping while REST get_positions() is in flight
        wrapper_ref[0].on_ws_disconnect("mid-flight drop")

    client = FakeRestClientP21(on_positions_hook=_drop_mid_flight)
    wrapper = _make_wrapper(client)
    wrapper_ref.append(wrapper)
    wrapper.on_ws_disconnect("initial drop")

    rep = await wrapper.on_ws_reconnect(ws_connected=True)

    assert rep["startup_secure"] is False
    assert rep["status"] == "STALE_RECONCILIATION_DISCONNECTED_LOCKED"
    assert wrapper._global_entry_lock is True
    assert wrapper._startup_reconciliation_completed is False
    assert wrapper._ws_reconciliation_required is True


@pytest.mark.asyncio
async def test_p2_1_11_two_rapid_reconnect_events_zero_concurrent_reconciliations(isolated_sqlite):
    """11. Two rapid consecutive reconnect events serialize cleanly with max concurrency == 1."""
    client = FakeRestClientP21(delay_positions=0.05)
    wrapper = _make_wrapper(client)
    ws = FakeWsClient(connected=True)
    wrapper.bind_ws_client(ws)
    wrapper.on_ws_disconnect("drop1")

    t1 = wrapper.schedule_ws_reconnect_reconciliation(ws_client=ws)
    t2 = wrapper.schedule_ws_reconnect_reconciliation(ws_client=ws)
    assert t1 is t2  # Coalesced into single serialized runner task

    # Also test direct concurrent calls to reconcile_on_startup()
    r1, r2 = await asyncio.gather(wrapper.reconcile_on_startup(), wrapper.reconcile_on_startup())
    await t1

    assert client.max_concurrent_reconcile_calls == 1
    assert r1["startup_secure"] is True
    assert r2["startup_secure"] is True
    assert wrapper._global_entry_lock is False


@pytest.mark.asyncio
async def test_p2_1_12_reduce_and_close_preserved_under_ws_disconnect_lock_no_blind_close(isolated_sqlite, monkeypatch):
    """12. Under WS disconnect lock: REDUCE/CLOSE remain allowed on tracked position; blind close without tracker refused."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    client = FakeRestClientP21(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
    )
    wrapper = _make_wrapper(client)
    wrapper.on_ws_disconnect("drop")
    assert wrapper._global_entry_lock is True

    # Blind close on untracked symbol (BTC) -> strictly refused
    blind = await wrapper.close_position("BTCUSDT")
    assert blind["success"] is False
    assert blind["code"] == "CLOSE_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"

    # Tracked non-quarantined symbol (ETH) -> REDUCE & CLOSE preserved
    wrapper._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="TRACKED_P21_12", client_oid="BOT_P21_12", symbol="PF_ETHUSD",
        side="long", filled_qty=0.05, expected_exposure=0.05, status="OPEN",
    )
    red = await wrapper.place_order(
        symbol="ETHUSDT", side="sell", quantity=0.02, order_type="market",
        entry_price=3000.0, intent="reduce", reduce_only=True,
    )
    assert red.get("success") is True
    assert len(client.exec_calls) == 1

    client.positions[0]["size"] = 0.03
    cls = await wrapper.place_order(
        symbol="ETHUSDT", side="sell", quantity=0.03, order_type="market",
        entry_price=3000.0, intent="close", reduce_only=True,
    )
    assert cls.get("success") is True
    assert len(client.exec_calls) == 2
