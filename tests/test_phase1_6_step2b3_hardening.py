"""
Phase 1.6 / Step 2B.3 — Targeted Hardening Suite (100% Offline)
===============================================================
Covers all four targeted corrections from the Step 2B.2 independent audit:
  - C1: Missing / empty SL identifiers -> CANCEL_UNKNOWN_IDENTIFIER (never CONFIRMED_ABSENT)
  - C2: SQLite persistence failure on update_protection_status(..., status="CANCELLED") after TP fill
  - C3: Strict persistence tracking (PERSISTENCE_FAILED / PERSISTENCE_UNKNOWN) on INVALID_ON_EXCHANGE / MISSING_ON_EXCHANGE
  - C4: Symmetric A1..A5 coverage for the TP validation exception branch with direct SQLite verification

Strict isolation:
  - External sockets hard-blocked.
  - Temporary SQLite DB via pytest `tmp_path` ONLY.
  - Fake REST client recording every execution, cancel, and open_orders read call.
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
        raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.6 Step 2B.3: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.6 Step 2B.3: {args}")


socket.socket.connect = _guarded_connect
socket.create_connection = _blocked_external
socket.getaddrinfo = _blocked_external

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SMARTTRADE_TEST_MODE"] = "1"
os.environ["KRAKEN_RETRY_ON_ORDER_REJECT"] = "1"
os.environ["KRAKEN_POS_RESOLVE_ATTEMPTS"] = "1"
os.environ["KRAKEN_POS_RESOLVE_DELAY"] = "0.0"

from backend.db.base import Base
from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
from backend.services.live_trades_service import LiveTradesService


@pytest.fixture
def isolated_sqlite(tmp_path, monkeypatch):
    db_file = tmp_path / "test_phase1_6_step2b3.db"
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


class FakeRestClient2B3:
    def __init__(
        self,
        positions: Optional[List[Dict[str, Any]]] = None,
        open_orders: Optional[List[Dict[str, Any]]] = None,
        fills: Optional[List[Dict[str, Any]]] = None,
    ):
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])
        self.fills = list(fills or [])
        self.exec_calls: List[Dict[str, Any]] = []
        self.cancel_calls: List[Dict[str, Any]] = []
        self.open_orders_calls: int = 0

    async def get_positions(self, **kwargs):
        return list(self.positions)

    async def get_open_orders(self, symbol: Optional[str] = None, **kwargs):
        self.open_orders_calls += 1
        if symbol:
            return [o for o in self.open_orders if str(o.get("symbol", "")).upper() == symbol.upper()]
        return list(self.open_orders)

    async def _request(self, method: str, endpoint: str, *args, **kwargs):
        if "fills" in str(endpoint).lower():
            return {"result": "success", "fills": list(self.fills)}
        raise RuntimeError(f"Unexpected _request: {method} {endpoint}")

    async def cancel_order(self, order_id: Optional[str] = None, cli_ord_id: Optional[str] = None, **kwargs):
        self.cancel_calls.append({"order_id": order_id, "cli_ord_id": cli_ord_id})
        self.open_orders = [
            o
            for o in self.open_orders
            if not (
                (order_id and o.get("order_id") == order_id)
                or (cli_ord_id and o.get("cliOrdId") == cli_ord_id)
            )
        ]
        return {"success": True, "code": "00000", "order_id": order_id or cli_ord_id}

    async def place_market_order(self, **kwargs):
        self.exec_calls.append({"method": "place_market_order", **kwargs})
        return {"success": False, "code": "SIM_FORBIDDEN"}

    async def place_limit_order(self, **kwargs):
        self.exec_calls.append({"method": "place_limit_order", **kwargs})
        return {"success": False, "code": "SIM_FORBIDDEN"}

    async def place_order(self, **kwargs):
        self.exec_calls.append({"method": "place_order", **kwargs})
        return {"success": False, "code": "SIM_FORBIDDEN"}


def _make_wrapper(client: FakeRestClient2B3) -> KrakenFuturesExecutorWrapper:
    w = KrakenFuturesExecutorWrapper(
        api_key="test_k_2b3",
        api_secret="dGVzdF9zXzJiMw==",
        timeout=2.0,
        single_entry_mode=False,
    )
    w._rest_client = client
    w._get_rest_client = lambda: client
    return w


def _seed_eth_open(order_id="OPEN_ETH", sl_oid="SL_ETH", sl_clid="BOT_SL_ETH", tp_oid="TP_ETH", tp_clid="BOT_TP_ETH"):
    prot: Dict[str, Any] = {
        "version": 1,
        "sl": {"order_id": sl_oid, "client_oid": sl_clid, "order_type": "stp", "trigger_price": 2940.0, "placed_qty": 0.05, "status": "ATTACHED"},
    }
    if tp_oid is not None:
        prot["tp"] = {"order_id": tp_oid, "client_oid": tp_clid, "order_type": "take_profit", "trigger_price": 3120.0, "placed_qty": 0.05, "status": "ATTACHED"}
    return LiveTradesService.create_live_trade(
        order_id=order_id, symbol="PF_ETHUSD", side="long", qty=0.05, entry=3000.0, stop=2940.0, tp=3120.0,
        leverage=10, broker="kraken", status="OPEN", client_oid=f"BOT_{order_id}",
        raw_exchange={"order_id": order_id, "protections": prot},
    )


def _eth_sl(order_id="SL_ETH", cli_ord_id="BOT_SL_ETH", **overrides):
    o = {
        "order_id": order_id, "cliOrdId": cli_ord_id, "symbol": "PF_ETHUSD",
        "side": "sell", "size": 0.05, "quantity": 0.05, "filled": 0.0,
        "stopPrice": 2940.0, "orderType": "stp", "reduceOnly": True, "status": "untouched",
    }
    o.update(overrides)
    return o


def _eth_tp(order_id="TP_ETH", cli_ord_id="BOT_TP_ETH", **overrides):
    o = {
        "order_id": order_id, "cliOrdId": cli_ord_id, "symbol": "PF_ETHUSD",
        "side": "sell", "size": 0.05, "quantity": 0.05, "filled": 0.0,
        "stopPrice": 3120.0, "orderType": "take_profit", "reduceOnly": True, "status": "untouched",
    }
    o.update(overrides)
    return o


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTION C1 — IDENTIFIANTS SL ABSENTS / VIDES
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_c1_1_both_identifiers_none_returns_cancel_unknown_identifier_and_preserves_quarantine(isolated_sqlite):
    """C1.1: Both order_id and client_oid None -> CANCEL_UNKNOWN_IDENTIFIER, 0 cancel, quarantine & locks kept."""
    q_trade = LiveTradesService.create_live_trade(
        order_id="", symbol="PF_ETHUSD", side="sell", qty=0.05, entry=3000.0, stop=0.0, tp=0.0,
        leverage=10, broker="kraken", status="QUARANTINED", client_oid=None,
        raw_exchange={"anomaly_type": "residual_sl_cleanup", "parent_trade_order_id": "OPEN_C1_1"},
    )
    client = FakeRestClient2B3(positions=[], open_orders=[])
    wrapper = _make_wrapper(client)

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols
    assert rep["residual_sl_cleanups"][0]["status"] == "CANCEL_UNKNOWN_IDENTIFIER"
    assert rep["residual_sl_cleanups"][0]["confirmation_status"] == "CANCEL_UNKNOWN_IDENTIFIER"
    assert len(client.cancel_calls) == 0
    # SQLite quarantine record is untouched (still QUARANTINED)
    reloaded = LiveTradesService.get_trade_by_id(q_trade.id)
    assert reloaded is not None and reloaded.status == "QUARANTINED"


@pytest.mark.asyncio
async def test_c1_2_both_identifiers_whitespace_empty_returns_cancel_unknown_identifier(isolated_sqlite):
    """C1.2: Both order_id and client_oid empty/whitespace -> CANCEL_UNKNOWN_IDENTIFIER, 0 cancel, locked."""
    q_trade = LiveTradesService.create_live_trade(
        order_id="   ", symbol="PF_ETHUSD", side="sell", qty=0.05, entry=3000.0, stop=0.0, tp=0.0,
        leverage=10, broker="kraken", status="QUARANTINED", client_oid="  ",
        raw_exchange={"anomaly_type": "residual_sl_cleanup", "parent_trade_order_id": "OPEN_C1_2"},
    )
    client = FakeRestClient2B3(positions=[], open_orders=[])
    wrapper = _make_wrapper(client)

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols
    assert rep["residual_sl_cleanups"][0]["status"] == "CANCEL_UNKNOWN_IDENTIFIER"
    assert len(client.cancel_calls) == 0
    assert LiveTradesService.get_trade_by_id(q_trade.id).status == "QUARANTINED"


@pytest.mark.asyncio
async def test_c1_3_valid_order_id_without_client_oid_cancels_and_confirms(isolated_sqlite):
    """C1.3: Valid order_id with client_oid=None -> cancels, REST-confirms, closes quarantine, unlocks."""
    q_trade = LiveTradesService.create_live_trade(
        order_id="SL_ONLY_OID", symbol="PF_ETHUSD", side="sell", qty=0.05, entry=3000.0, stop=0.0, tp=0.0,
        leverage=10, broker="kraken", status="QUARANTINED", client_oid=None,
        raw_exchange={"anomaly_type": "residual_sl_cleanup", "parent_trade_order_id": "OPEN_C1_3"},
    )
    client = FakeRestClient2B3(positions=[], open_orders=[_eth_sl(order_id="SL_ONLY_OID", cli_ord_id="")])
    wrapper = _make_wrapper(client)

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert "PF_ETHUSD" not in wrapper._reconciliation_required_symbols
    assert rep["residual_sl_cleanups"][0]["confirmation_status"] == "CANCELLED_CONFIRMED"
    assert len(client.cancel_calls) == 1
    assert client.cancel_calls[0]["order_id"] == "SL_ONLY_OID"
    assert LiveTradesService.get_trade_by_id(q_trade.id).status == "CLOSED"


@pytest.mark.asyncio
async def test_c1_4_valid_client_oid_without_order_id_cancels_and_confirms(isolated_sqlite):
    """C1.4: Valid client_oid with empty order_id -> cancels by cliOrdId, REST-confirms, closes quarantine, unlocks."""
    q_trade = LiveTradesService.create_live_trade(
        order_id="", symbol="PF_ETHUSD", side="sell", qty=0.05, entry=3000.0, stop=0.0, tp=0.0,
        leverage=10, broker="kraken", status="QUARANTINED", client_oid="BOT_SL_ONLY_CLID",
        raw_exchange={"anomaly_type": "residual_sl_cleanup", "parent_trade_order_id": "OPEN_C1_4"},
    )
    client = FakeRestClient2B3(positions=[], open_orders=[_eth_sl(order_id="", cli_ord_id="BOT_SL_ONLY_CLID")])
    wrapper = _make_wrapper(client)

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert "PF_ETHUSD" not in wrapper._reconciliation_required_symbols
    assert rep["residual_sl_cleanups"][0]["confirmation_status"] == "CANCELLED_CONFIRMED"
    assert len(client.cancel_calls) == 1
    assert client.cancel_calls[0]["cli_ord_id"] == "BOT_SL_ONLY_CLID"
    assert LiveTradesService.get_trade_by_id(q_trade.id).status == "CLOSED"


@pytest.mark.asyncio
async def test_c1_5_confirm_helper_rejects_empty_and_bool_identifiers_without_rest_call():
    """C1.5: Direct call to _confirm_residual_sl_cancelled with missing/empty/bool IDs -> CANCEL_UNKNOWN_IDENTIFIER, 0 REST calls."""
    client = FakeRestClient2B3(positions=[], open_orders=[])
    wrapper = _make_wrapper(client)

    for bad_oid, bad_clid in [(None, None), ("", ""), ("   ", "   "), (True, False)]:
        ok, status, err = await wrapper._confirm_residual_sl_cancelled("PF_ETHUSD", bad_oid, bad_clid)
        assert ok is False
        assert status == "CANCEL_UNKNOWN_IDENTIFIER"
        assert err is not None
    assert client.open_orders_calls == 0


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTION C2 — ÉCHEC DE PERSISTANCE APRÈS TP REMPLI
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_c2_1_tp_fill_sl_cancel_update_protection_returns_false_locks_and_preserves_state(isolated_sqlite, monkeypatch):
    """C2.1: TP filled + SL cancelled & REST-confirmed, but update_protection_status(sl, CANCELLED) returns False -> locked."""
    _seed_eth_open(order_id="OPEN_C2_1", sl_oid="SL_C2_1", sl_clid="BOT_SL_C2_1", tp_oid="TP_C2_1", tp_clid="BOT_TP_C2_1")
    client = FakeRestClient2B3(
        positions=[],
        open_orders=[_eth_sl(order_id="SL_C2_1", cli_ord_id="BOT_SL_C2_1")],
        fills=[{"fill_id": "F_C2_1", "order_id": "TP_C2_1", "cliOrdId": "BOT_TP_C2_1", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
    )
    wrapper = _make_wrapper(client)
    orig_upd = LiveTradesService.update_protection_status

    def _fail_sl_cancel(**kw):
        if kw.get("protection_type") == "sl" and kw.get("status") == "CANCELLED":
            return False
        return orig_upd(**kw)

    monkeypatch.setattr(LiveTradesService, "update_protection_status", staticmethod(_fail_sl_cancel))

    rep = await wrapper.reconcile_on_startup()

    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols
    assert rep["residual_sl_cleanups"][0]["persistence_status"] == "PERSISTENCE_FAILED"
    assert rep["residual_sl_cleanups"][0]["confirmation_status"] == "CANCELLED_CONFIRMED"
    assert len(client.cancel_calls) == 1
    assert len(LiveTradesService.get_quarantined_trades(broker="kraken")) == 1


@pytest.mark.asyncio
async def test_c2_2_tp_fill_sl_cancel_update_protection_raises_sqlite_error_locks(isolated_sqlite, monkeypatch):
    """C2.2: TP filled + SL cancelled & REST-confirmed, but update_protection_status raises SQLite error -> PERSISTENCE_UNKNOWN."""
    _seed_eth_open(order_id="OPEN_C2_2", sl_oid="SL_C2_2", sl_clid="BOT_SL_C2_2", tp_oid="TP_C2_2", tp_clid="BOT_TP_C2_2")
    client = FakeRestClient2B3(
        positions=[],
        open_orders=[_eth_sl(order_id="SL_C2_2", cli_ord_id="BOT_SL_C2_2")],
        fills=[{"fill_id": "F_C2_2", "order_id": "TP_C2_2", "cliOrdId": "BOT_TP_C2_2", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
    )
    wrapper = _make_wrapper(client)
    orig_upd = LiveTradesService.update_protection_status

    def _raise_sl_cancel(**kw):
        if kw.get("protection_type") == "sl" and kw.get("status") == "CANCELLED":
            raise RuntimeError("sqlite3 OperationalError: database is locked")
        return orig_upd(**kw)

    monkeypatch.setattr(LiveTradesService, "update_protection_status", staticmethod(_raise_sl_cancel))

    rep = await wrapper.reconcile_on_startup()

    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols
    assert rep["residual_sl_cleanups"][0]["persistence_status"] == "PERSISTENCE_UNKNOWN"
    assert len(client.cancel_calls) == 1


@pytest.mark.asyncio
async def test_c2_3_hot_recovery_after_sqlite_failure_persists_without_double_cancel(isolated_sqlite, monkeypatch):
    """C2.3: Run 1 REST confirms cancel but ALL SQLite SL persistence fails -> locked; Run 2 SQLite recovers -> persists with 0 extra cancels & unlocks."""
    trade = _seed_eth_open(order_id="OPEN_C2_3", sl_oid="SL_C2_3", sl_clid="BOT_SL_C2_3", tp_oid="TP_C2_3", tp_clid="BOT_TP_C2_3")
    client = FakeRestClient2B3(
        positions=[],
        open_orders=[_eth_sl(order_id="SL_C2_3", cli_ord_id="BOT_SL_C2_3")],
        fills=[{"fill_id": "F_C2_3", "order_id": "TP_C2_3", "cliOrdId": "BOT_TP_C2_3", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
    )
    wrapper = _make_wrapper(client)
    orig_upd = LiveTradesService.update_protection_status
    orig_rec_q = LiveTradesService.record_exchange_quarantine

    def _fail_sl_upd(**kw):
        if kw.get("protection_type") == "sl" and kw.get("status") == "CANCELLED":
            raise RuntimeError("SQLite unavailable")
        return orig_upd(**kw)

    monkeypatch.setattr(LiveTradesService, "update_protection_status", staticmethod(_fail_sl_upd))
    monkeypatch.setattr(LiveTradesService, "record_exchange_quarantine", staticmethod(lambda **kw: (_ for _ in ()).throw(RuntimeError("SQLite unavailable"))))

    # Run 1: REST cancel confirmed, SQLite writes fail -> strictly locked
    rep1 = await wrapper.reconcile_on_startup()
    assert rep1["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep1["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols
    assert len(client.cancel_calls) == 1

    # Run 2: SQLite recovers -> hot retry persists SL CANCELLED without redundant cancel_order
    monkeypatch.setattr(LiveTradesService, "update_protection_status", staticmethod(orig_upd))
    monkeypatch.setattr(LiveTradesService, "record_exchange_quarantine", staticmethod(orig_rec_q))

    rep2 = await wrapper.reconcile_on_startup()
    assert rep2["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert "PF_ETHUSD" not in wrapper._reconciliation_required_symbols
    assert len(client.cancel_calls) == 1  # ZERO double cancel!
    reloaded = LiveTradesService.get_trade_by_id(trade.id)
    assert reloaded.status == "CLOSED"
    assert reloaded.get_raw_exchange()["protections"]["sl"]["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_c2_4_restart_recovery_after_sl_status_persistence_fail_closes_quarantine_no_double_cancel(isolated_sqlite, monkeypatch):
    """C2.4: Run 1 update_protection_status fails while residual_sl_cleanup quarantine persists; Run 2 on fresh wrapper unlocks with 0 extra cancels."""
    _seed_eth_open(order_id="OPEN_C2_4", sl_oid="SL_C2_4", sl_clid="BOT_SL_C2_4", tp_oid="TP_C2_4", tp_clid="BOT_TP_C2_4")
    client = FakeRestClient2B3(
        positions=[],
        open_orders=[_eth_sl(order_id="SL_C2_4", cli_ord_id="BOT_SL_C2_4")],
        fills=[{"fill_id": "F_C2_4", "order_id": "TP_C2_4", "cliOrdId": "BOT_TP_C2_4", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
    )
    wrapper1 = _make_wrapper(client)
    orig_upd = LiveTradesService.update_protection_status

    def _fail_sl_upd(**kw):
        if kw.get("protection_type") == "sl" and kw.get("status") == "CANCELLED":
            return False
        return orig_upd(**kw)

    monkeypatch.setattr(LiveTradesService, "update_protection_status", staticmethod(_fail_sl_upd))
    rep1 = await wrapper1.reconcile_on_startup()
    assert rep1["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert len(LiveTradesService.get_quarantined_trades(broker="kraken")) == 1
    assert len(client.cancel_calls) == 1

    # Simulated restart: fresh wrapper2 with SQLite restored
    monkeypatch.setattr(LiveTradesService, "update_protection_status", staticmethod(orig_upd))
    wrapper2 = _make_wrapper(client)
    rep2 = await wrapper2.reconcile_on_startup()
    assert rep2["startup_secure"] is True
    assert wrapper2._global_entry_lock is False
    assert rep2["residual_sl_cleanups"][0]["status"] == "CONFIRMED_ABSENT"
    assert len(client.cancel_calls) == 1  # ZERO double cancel!
    assert LiveTradesService.get_quarantined_trades(broker="kraken") == []


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTION C3 — INVALID_ON_EXCHANGE / MISSING_ON_EXCHANGE PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_c3_1_sl_invalid_on_exchange_persistence_false_triggers_persistence_failed_locked(isolated_sqlite, monkeypatch):
    """C3.1: SL INVALID_ON_EXCHANGE + mark_trade_quarantined -> False => PERSISTENCE_FAILED_LOCKED & retryable."""
    eth_t = _seed_eth_open(tp_oid=None, tp_clid=None)
    # Invalid SL: reduceOnly=False
    client = FakeRestClient2B3(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_eth_sl(reduceOnly=False)],
    )
    wrapper = _make_wrapper(client)
    orig_mark_q = LiveTradesService.mark_trade_quarantined
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(lambda **kw: False))

    rep1 = await wrapper.reconcile_on_startup()
    assert rep1["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep1["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep1["protection_persistence_errors"][0]["status"] == "INVALID_ON_EXCHANGE"
    assert rep1["protection_persistence_errors"][0]["persistence_status"] == "PERSISTENCE_FAILED"
    assert LiveTradesService.get_trade_by_id(eth_t.id).status == "OPEN"

    # Retry once SQLite recovers -> persists QUARANTINED and stays locked
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(orig_mark_q))
    rep2 = await wrapper.reconcile_on_startup()
    assert rep2["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert LiveTradesService.get_trade_by_id(eth_t.id).status == "QUARANTINED"


@pytest.mark.asyncio
async def test_c3_2_sl_missing_on_exchange_persistence_exception_triggers_persistence_unknown(isolated_sqlite, monkeypatch):
    """C3.2: SL MISSING_ON_EXCHANGE + mark_trade_quarantined raises -> PERSISTENCE_UNKNOWN, PERSISTENCE_FAILED_LOCKED."""
    eth_t = _seed_eth_open(tp_oid=None, tp_clid=None)
    client = FakeRestClient2B3(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[],
    )
    wrapper = _make_wrapper(client)
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(lambda **kw: (_ for _ in ()).throw(RuntimeError("disk full"))))

    rep = await wrapper.reconcile_on_startup()
    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["protection_persistence_errors"][0]["status"] == "MISSING_ON_EXCHANGE"
    assert rep["protection_persistence_errors"][0]["persistence_status"] == "PERSISTENCE_UNKNOWN"
    assert LiveTradesService.get_trade_by_id(eth_t.id).status == "OPEN"


@pytest.mark.asyncio
async def test_c3_3_tp_invalid_on_exchange_persistence_false_triggers_persistence_failed_locked(isolated_sqlite, monkeypatch):
    """C3.3: TP INVALID_ON_EXCHANGE + update_protection_status(tp) returns False -> PERSISTENCE_FAILED_LOCKED."""
    _seed_eth_open()
    client = FakeRestClient2B3(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_eth_sl(), _eth_tp(reduceOnly=False)],
    )
    wrapper = _make_wrapper(client)
    orig_upd = LiveTradesService.update_protection_status

    def _fail_tp_upd(**kw):
        if kw.get("protection_type") == "tp" and kw.get("status") == "INVALID_ON_EXCHANGE":
            return False
        return orig_upd(**kw)

    monkeypatch.setattr(LiveTradesService, "update_protection_status", staticmethod(_fail_tp_upd))

    rep = await wrapper.reconcile_on_startup()
    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["protection_persistence_errors"][0]["protection_type"] == "tp"
    assert rep["protection_persistence_errors"][0]["status"] == "INVALID_ON_EXCHANGE"
    assert rep["protection_persistence_errors"][0]["persistence_status"] == "PERSISTENCE_FAILED"


@pytest.mark.asyncio
async def test_c3_4_tp_missing_on_exchange_persistence_exception_triggers_persistence_unknown(isolated_sqlite, monkeypatch):
    """C3.4: TP MISSING_ON_EXCHANGE + mark_trade_quarantined raises -> PERSISTENCE_UNKNOWN, PERSISTENCE_FAILED_LOCKED."""
    eth_t = _seed_eth_open()
    client = FakeRestClient2B3(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_eth_sl()],  # TP absent with zero fills
        fills=[],
    )
    wrapper = _make_wrapper(client)
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(lambda **kw: (_ for _ in ()).throw(RuntimeError("sqlite locked"))))

    rep = await wrapper.reconcile_on_startup()
    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["protection_persistence_errors"][0]["protection_type"] == "tp"
    assert rep["protection_persistence_errors"][0]["status"] == "MISSING_ON_EXCHANGE"
    assert rep["protection_persistence_errors"][0]["persistence_status"] == "PERSISTENCE_UNKNOWN"
    assert LiveTradesService.get_trade_by_id(eth_t.id).status == "OPEN"


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTION C4 — COUVERTURE SYMÉTRIQUE A1..A5 BRANCHE TP
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_c4_tp_a1_mark_trade_quarantined_returns_false_triggers_persistence_failed(isolated_sqlite, monkeypatch):
    """C4 (TP-A1): TP validation exception + mark_trade_quarantined -> False => PERSISTENCE_FAILED, SQLite still OPEN."""
    eth_t = _seed_eth_open()
    client = FakeRestClient2B3(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_eth_sl(), _eth_tp()],
    )
    wrapper = _make_wrapper(client)
    orig_val = wrapper._validate_recovered_protection

    def _boom_tp(*a, **kw):
        if kw.get("protection_type") == "tp":
            raise RuntimeError("TP validator boom")
        return orig_val(*a, **kw)

    monkeypatch.setattr(wrapper, "_validate_recovered_protection", _boom_tp)
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(lambda **kw: False))

    rep = await wrapper.reconcile_on_startup()

    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["validation_errors"][0]["protection_type"] == "tp"
    assert rep["validation_errors"][0]["persistence_status"] == "PERSISTENCE_FAILED"
    assert LiveTradesService.get_trade_by_id(eth_t.id).status == "OPEN"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_c4_tp_a2_mark_trade_quarantined_raises_sqlite_error_triggers_persistence_unknown(isolated_sqlite, monkeypatch):
    """C4 (TP-A2): TP validation exception + mark_trade_quarantined raises -> PERSISTENCE_UNKNOWN, SQLite still OPEN."""
    eth_t = _seed_eth_open()
    client = FakeRestClient2B3(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_eth_sl(), _eth_tp()],
    )
    wrapper = _make_wrapper(client)
    orig_val = wrapper._validate_recovered_protection

    def _boom_tp(*a, **kw):
        if kw.get("protection_type") == "tp":
            raise RuntimeError("TP validator boom")
        return orig_val(*a, **kw)

    monkeypatch.setattr(wrapper, "_validate_recovered_protection", _boom_tp)
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(lambda **kw: (_ for _ in ()).throw(RuntimeError("database is locked"))))

    rep = await wrapper.reconcile_on_startup()

    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["validation_errors"][0]["protection_type"] == "tp"
    assert rep["validation_errors"][0]["persistence_status"] == "PERSISTENCE_UNKNOWN"
    assert LiveTradesService.get_trade_by_id(eth_t.id).status == "OPEN"


@pytest.mark.asyncio
async def test_c4_tp_a3_update_protection_status_returns_false_triggers_persistence_failed(isolated_sqlite, monkeypatch):
    """C4 (TP-A3): TP validation exception + update_protection_status(tp) -> False => PERSISTENCE_FAILED."""
    _seed_eth_open()
    client = FakeRestClient2B3(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_eth_sl(), _eth_tp()],
    )
    wrapper = _make_wrapper(client)
    orig_val = wrapper._validate_recovered_protection
    orig_upd = LiveTradesService.update_protection_status

    def _boom_tp(*a, **kw):
        if kw.get("protection_type") == "tp":
            raise RuntimeError("TP validator boom")
        return orig_val(*a, **kw)

    def _fail_tp_upd(**kw):
        if kw.get("protection_type") == "tp":
            return False
        return orig_upd(**kw)

    monkeypatch.setattr(wrapper, "_validate_recovered_protection", _boom_tp)
    monkeypatch.setattr(LiveTradesService, "update_protection_status", staticmethod(_fail_tp_upd))

    rep = await wrapper.reconcile_on_startup()

    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["validation_errors"][0]["protection_type"] == "tp"
    assert rep["validation_errors"][0]["persistence_status"] == "PERSISTENCE_FAILED"


@pytest.mark.asyncio
async def test_c4_tp_a4_multi_trade_tp_persistence_fail_on_first_second_still_quarantined(isolated_sqlite, monkeypatch):
    """C4 (TP-A4): ETH hits TP validation exception + persistence fail; BTC (missing SL) is still quarantined in SQLite."""
    _seed_eth_open(order_id="OPEN_ETH_TPA4", sl_oid="SL_ETH_TPA4", sl_clid="BOT_SL_ETH_TPA4", tp_oid="TP_ETH_TPA4", tp_clid="BOT_TP_ETH_TPA4")
    btc_t = LiveTradesService.create_live_trade(
        order_id="OPEN_BTC_TPA4", symbol="PF_XBTUSD", side="long", qty=0.002, entry=60000.0, stop=59000.0, tp=62000.0,
        leverage=10, broker="kraken", status="OPEN", client_oid="BOT_BTC_TPA4",
        raw_exchange={"order_id": "OPEN_BTC_TPA4", "protections": {"version": 1, "sl": {"order_id": "SL_BTC_MISS", "status": "ATTACHED"}}},
    )
    client = FakeRestClient2B3(
        positions=[
            {"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0},
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.002, "price": 60000.0},
        ],
        open_orders=[
            _eth_sl(order_id="SL_ETH_TPA4", cli_ord_id="BOT_SL_ETH_TPA4"),
            _eth_tp(order_id="TP_ETH_TPA4", cli_ord_id="BOT_TP_ETH_TPA4"),
        ],
    )
    wrapper = _make_wrapper(client)
    orig_val = wrapper._validate_recovered_protection
    orig_mark_q = LiveTradesService.mark_trade_quarantined

    def _boom_eth_tp(*a, **kw):
        if kw.get("expected_symbol") == "PF_ETHUSD" and kw.get("protection_type") == "tp":
            raise RuntimeError("ETH TP boom")
        return orig_val(*a, **kw)

    def _fail_eth_q(symbol, **kw):
        if "ETH" in symbol.upper():
            return False
        return orig_mark_q(symbol=symbol, **kw)

    monkeypatch.setattr(wrapper, "_validate_recovered_protection", _boom_eth_tp)
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(_fail_eth_q))

    rep = await wrapper.reconcile_on_startup()

    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert LiveTradesService.get_trade_by_id(btc_t.id).status == "QUARANTINED"


@pytest.mark.asyncio
async def test_c4_tp_a5_same_instance_retry_persists_quarantine_in_sqlite_and_stays_locked(isolated_sqlite, monkeypatch):
    """C4 (TP-A5): Run 1 TP validation exception + persistence fail -> OPEN; Run 2 on same wrapper -> QUARANTINED & locked."""
    eth_t = _seed_eth_open()
    client = FakeRestClient2B3(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_eth_sl(), _eth_tp()],
    )
    wrapper = _make_wrapper(client)
    orig_val = wrapper._validate_recovered_protection
    orig_mark_q = LiveTradesService.mark_trade_quarantined

    def _boom_tp(*a, **kw):
        if kw.get("protection_type") == "tp":
            raise RuntimeError("TP validator boom")
        return orig_val(*a, **kw)

    monkeypatch.setattr(wrapper, "_validate_recovered_protection", _boom_tp)
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(lambda **kw: False))

    rep1 = await wrapper.reconcile_on_startup()
    assert rep1["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert wrapper._global_entry_lock is True
    assert LiveTradesService.get_trade_by_id(eth_t.id).status == "OPEN"

    # Persistence recovers, validator still failing -> now QUARANTINE_PERSISTED in SQLite, still locked
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(orig_mark_q))
    rep2 = await wrapper.reconcile_on_startup()
    assert rep2["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep2["validation_errors"][0]["protection_type"] == "tp"
    assert rep2["validation_errors"][0]["persistence_status"] == "QUARANTINE_PERSISTED"
    assert LiveTradesService.get_trade_by_id(eth_t.id).status == "QUARANTINED"
