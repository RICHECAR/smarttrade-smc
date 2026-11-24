"""
Phase 1.6 / Step 2B.2 — Final Reconciliation Hardening Suite (100% Offline)
===========================================================================
Covers all 21 mandatory scenarios for Corrections A, B, and C:
  - A1..A5: P1 persistence failure & unknown handling (SQLite tmp_path)
  - B1..B7: Hot-recovery global & per-symbol lock coherence (same wrapper instance)
  - C1..C9: Effective REST re-read confirmation after residual SL cancel_order()

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
        raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.6 Step 2B.2: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.6 Step 2B.2: {args}")


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
    db_file = tmp_path / "test_phase1_6_step2b2.db"
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


class ControlledFakeRestClient:
    """Offline fake REST client with scripted open_orders responses for post-cancel re-read tests."""

    def __init__(
        self,
        positions: Optional[List[Dict[str, Any]]] = None,
        open_orders: Optional[List[Dict[str, Any]]] = None,
        fills: Optional[List[Dict[str, Any]]] = None,
        cancel_exc: Optional[BaseException] = None,
        remove_on_cancel: bool = True,
        post_cancel_open_orders_override: Any = None,
        post_cancel_open_orders_exc: Optional[BaseException] = None,
        open_orders_exc: Optional[BaseException] = None,
    ):
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])
        self.fills = list(fills or [])
        self.cancel_exc = cancel_exc
        self.remove_on_cancel = remove_on_cancel
        self.post_cancel_open_orders_override = post_cancel_open_orders_override
        self.post_cancel_open_orders_exc = post_cancel_open_orders_exc
        self.open_orders_exc = open_orders_exc
        self._cancel_happened = False

        self.exec_calls: List[Dict[str, Any]] = []
        self.cancel_calls: List[Dict[str, Any]] = []
        self.open_orders_calls: int = 0

    async def get_positions(self, **kwargs):
        return list(self.positions)

    async def get_open_orders(self, symbol: Optional[str] = None, **kwargs):
        self.open_orders_calls += 1
        if self._cancel_happened:
            if self.post_cancel_open_orders_exc is not None:
                raise self.post_cancel_open_orders_exc
            if self.post_cancel_open_orders_override is not None:
                return self.post_cancel_open_orders_override
        if self.open_orders_exc is not None:
            raise self.open_orders_exc
        if symbol:
            return [o for o in self.open_orders if str(o.get("symbol", "")).upper() == symbol.upper()]
        return list(self.open_orders)

    async def _request(self, method: str, endpoint: str, *args, **kwargs):
        if "fills" in str(endpoint).lower():
            return {"result": "success", "fills": list(self.fills)}
        raise RuntimeError(f"Unexpected _request: {method} {endpoint}")

    async def cancel_order(self, order_id: Optional[str] = None, cli_ord_id: Optional[str] = None, **kwargs):
        self.cancel_calls.append({"order_id": order_id, "cli_ord_id": cli_ord_id})
        self._cancel_happened = True
        if self.cancel_exc is not None:
            raise self.cancel_exc
        if self.remove_on_cancel:
            self.open_orders = [
                o
                for o in self.open_orders
                if not (
                    (order_id and o.get("order_id") == order_id)
                    or (cli_ord_id and o.get("cliOrdId") == cli_ord_id)
                )
            ]
        return {"success": True, "code": "00000", "order_id": order_id}

    async def place_market_order(self, **kwargs):
        self.exec_calls.append({"method": "place_market_order", **kwargs})
        return {"success": True, "code": "00000", "order_id": "SIM_REDUCE_1", "price": 3000.0, "filled_size": float(kwargs.get("size", 0.01))}

    async def place_limit_order(self, **kwargs):
        self.exec_calls.append({"method": "place_limit_order", **kwargs})
        return {"success": False, "code": "SIM_FORBIDDEN"}

    async def place_order(self, **kwargs):
        self.exec_calls.append({"method": "place_order", **kwargs})
        return {"success": False, "code": "SIM_FORBIDDEN"}


def _make_wrapper(client: ControlledFakeRestClient) -> KrakenFuturesExecutorWrapper:
    w = KrakenFuturesExecutorWrapper(
        api_key="test_k_2b2",
        api_secret="dGVzdF9zXzJiMg==",
        timeout=2.0,
        single_entry_mode=False,
    )
    w._rest_client = client
    w._get_rest_client = lambda: client
    return w


def _seed_eth_open(order_id="OPEN_ETH", sl_oid="SL_ETH", sl_clid="BOT_SL_ETH", tp_oid=None, tp_clid=None):
    prot = {
        "version": 1,
        "sl": {"order_id": sl_oid, "client_oid": sl_clid, "order_type": "stp", "trigger_price": 2940.0, "placed_qty": 0.05, "status": "ATTACHED"},
    }
    if tp_oid:
        prot["tp"] = {"order_id": tp_oid, "client_oid": tp_clid, "order_type": "take_profit", "trigger_price": 3120.0, "placed_qty": 0.05, "status": "ATTACHED"}
    return LiveTradesService.create_live_trade(
        order_id=order_id, symbol="PF_ETHUSD", side="long", qty=0.05, entry=3000.0, stop=2940.0, tp=3120.0,
        leverage=10, broker="kraken", status="OPEN", client_oid=f"BOT_{order_id}",
        raw_exchange={"order_id": order_id, "protections": prot},
    )


def _eth_sl(order_id="SL_ETH", cli_ord_id="BOT_SL_ETH"):
    return {
        "order_id": order_id, "cliOrdId": cli_ord_id, "symbol": "PF_ETHUSD",
        "side": "sell", "size": 0.05, "quantity": 0.05, "filled": 0.0,
        "stopPrice": 2940.0, "orderType": "stp", "reduceOnly": True, "status": "untouched",
    }


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTION A — PERSISTANCE P1 (A1..A5)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a1_p1_mark_trade_quarantined_returns_false_triggers_persistence_failed(isolated_sqlite, monkeypatch):
    """A1: mark_trade_quarantined() returns False -> PERSISTENCE_FAILED, PERSISTENCE_FAILED_LOCKED, lock=True."""
    _seed_eth_open()
    client = ControlledFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_eth_sl()],
    )
    wrapper = _make_wrapper(client)
    monkeypatch.setattr(wrapper, "_validate_recovered_protection", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("validator boom")))
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(lambda **kw: False))

    rep = await wrapper.reconcile_on_startup()

    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["validation_errors"][0]["status"] == "VALIDATION_INTERNAL_ERROR"
    assert rep["validation_errors"][0]["persistence_status"] == "PERSISTENCE_FAILED"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_a2_p1_mark_trade_quarantined_raises_sqlite_error_triggers_persistence_unknown(isolated_sqlite, monkeypatch):
    """A2: mark_trade_quarantined() raises SQLite exception -> PERSISTENCE_UNKNOWN, PERSISTENCE_FAILED_LOCKED."""
    _seed_eth_open()
    client = ControlledFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_eth_sl()],
    )
    wrapper = _make_wrapper(client)
    monkeypatch.setattr(wrapper, "_validate_recovered_protection", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("validator boom")))
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(lambda **kw: (_ for _ in ()).throw(RuntimeError("database is locked"))))

    rep = await wrapper.reconcile_on_startup()

    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["validation_errors"][0]["persistence_status"] == "PERSISTENCE_UNKNOWN"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_a3_p1_update_protection_status_fails_triggers_persistence_failed(isolated_sqlite, monkeypatch):
    """A3: update_protection_status() returns False -> PERSISTENCE_FAILED, lock=True, startup_secure=False."""
    _seed_eth_open()
    client = ControlledFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_eth_sl()],
    )
    wrapper = _make_wrapper(client)
    monkeypatch.setattr(wrapper, "_validate_recovered_protection", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("validator boom")))
    monkeypatch.setattr(LiveTradesService, "update_protection_status", staticmethod(lambda **kw: False))

    rep = await wrapper.reconcile_on_startup()

    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["validation_errors"][0]["persistence_status"] == "PERSISTENCE_FAILED"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_a4_p1_multi_trade_persistence_fail_on_first_second_still_processed(isolated_sqlite, monkeypatch):
    """A4: Persistence fails on ETH (trade 1); BTC (trade 2, missing SL) is still quarantined in real SQLite."""
    _seed_eth_open(order_id="OPEN_ETH_A4", sl_oid="SL_ETH_A4", sl_clid="BOT_SL_ETH_A4")
    btc_t = LiveTradesService.create_live_trade(
        order_id="OPEN_BTC_A4", symbol="PF_XBTUSD", side="long", qty=0.002, entry=60000.0, stop=59000.0, tp=62000.0,
        leverage=10, broker="kraken", status="OPEN", client_oid="BOT_BTC_A4",
        raw_exchange={"order_id": "OPEN_BTC_A4", "protections": {"version": 1, "sl": {"order_id": "SL_BTC_MISS", "status": "ATTACHED"}}},
    )
    client = ControlledFakeRestClient(
        positions=[
            {"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0},
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.002, "price": 60000.0},
        ],
        open_orders=[_eth_sl(order_id="SL_ETH_A4", cli_ord_id="BOT_SL_ETH_A4")],
    )
    wrapper = _make_wrapper(client)
    orig_mark_q = LiveTradesService.mark_trade_quarantined

    def _boom_eth(*a, **kw):
        if kw.get("expected_symbol") == "PF_ETHUSD":
            raise RuntimeError("ETH validator boom")
        return True, None

    def _fail_eth_q(symbol, **kw):
        if "ETH" in symbol.upper():
            return False
        return orig_mark_q(symbol=symbol, **kw)

    monkeypatch.setattr(wrapper, "_validate_recovered_protection", _boom_eth)
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(_fail_eth_q))

    rep = await wrapper.reconcile_on_startup()

    assert rep["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    # BTC trade was still inspected and quarantined in temporary SQLite!
    assert LiveTradesService.get_trade_by_id(btc_t.id).status == "QUARANTINED"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_a5_p1_subsequent_reconciliation_stays_locked_until_persistence_and_security_restored(isolated_sqlite, monkeypatch):
    """A5: After P1 persistence failure, next reconcile stays locked while validator fails, then quarantines cleanly when persistence recovers."""
    eth_t = _seed_eth_open()
    client = ControlledFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_eth_sl()],
    )
    wrapper = _make_wrapper(client)
    orig_mark_q = LiveTradesService.mark_trade_quarantined
    monkeypatch.setattr(wrapper, "_validate_recovered_protection", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(lambda **kw: False))

    rep1 = await wrapper.reconcile_on_startup()
    assert rep1["status"] == "PERSISTENCE_FAILED_LOCKED"
    assert wrapper._global_entry_lock is True
    assert LiveTradesService.get_trade_by_id(eth_t.id).status == "OPEN"

    # Persistence recovers, validator still failing -> now QUARANTINE_PERSISTED, still locked
    monkeypatch.setattr(LiveTradesService, "mark_trade_quarantined", staticmethod(orig_mark_q))
    rep2 = await wrapper.reconcile_on_startup()
    assert rep2["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep2["validation_errors"][0]["persistence_status"] == "QUARANTINE_PERSISTED"
    assert LiveTradesService.get_trade_by_id(eth_t.id).status == "QUARANTINED"


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTION B — COHÉRENCE DES VERROUS À CHAUD (B1..B7)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_b1_hot_recovery_same_wrapper_releases_both_global_and_symbol_lock(isolated_sqlite):
    """B1: Residual SL fails cancel in run 1 (locks global + PF_ETHUSD); succeeds in run 2 on SAME wrapper -> both locks released."""
    _seed_eth_open(order_id="OPEN_B1", sl_oid="SL_B1", sl_clid="BOT_SL_B1", tp_oid="TP_B1", tp_clid="BOT_TP_B1")
    client = ControlledFakeRestClient(
        positions=[],
        open_orders=[_eth_sl(order_id="SL_B1", cli_ord_id="BOT_SL_B1")],
        fills=[{"fill_id": "F_B1", "order_id": "TP_B1", "cliOrdId": "BOT_TP_B1", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
        cancel_exc=RuntimeError("503 transient"),
    )
    wrapper = _make_wrapper(client)

    rep1 = await wrapper.reconcile_on_startup()
    assert rep1["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols

    # Hot retry on SAME wrapper instance (no restart)
    client.cancel_exc = None
    rep2 = await wrapper.reconcile_on_startup()
    assert rep2["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert "PF_ETHUSD" not in wrapper._reconciliation_required_symbols
    assert len(client.cancel_calls) == 2
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_b2_two_anomalies_same_symbol_resolving_one_keeps_symbol_and_global_locked(isolated_sqlite):
    """B2: Two anomalies on PF_ETHUSD (1 residual_sl_cleanup resolved + 1 trade quarantine) -> PF_ETHUSD & global stay locked."""
    LiveTradesService.record_exchange_quarantine(
        symbol="PF_ETHUSD", order_id="SL_B2_CLEAN", anomaly_type="residual_sl_cleanup",
        client_oid="BOT_SL_B2", reason="CANCEL_FAILED", broker="kraken",
        raw_exchange={"parent_trade_order_id": "OPEN_B2_1", "sl_order_id": "SL_B2_CLEAN"},
    )
    LiveTradesService.record_exchange_quarantine(
        symbol="PF_ETHUSD", order_id="ORPHAN_B2_POS", anomaly_type="position",
        reason="Untracked ETH position", broker="kraken", raw_exchange={"symbol": "PF_ETHUSD"},
    )
    client = ControlledFakeRestClient(positions=[], open_orders=[])  # SL_B2_CLEAN absent -> resolved, but ORPHAN_B2_POS remains
    wrapper = _make_wrapper(client)

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols
    assert len(client.cancel_calls) == 0
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_b3_eth_resolved_while_btc_anomaly_remains_releases_eth_only_keeps_global_lock(isolated_sqlite):
    """B3: ETH residual_sl_cleanup resolved while BTC quarantine persists -> ETH symbol lock cleared, BTC & global lock kept."""
    LiveTradesService.record_exchange_quarantine(
        symbol="PF_ETHUSD", order_id="SL_B3_ETH", anomaly_type="residual_sl_cleanup",
        client_oid="BOT_SL_B3", reason="CANCEL_FAILED", broker="kraken",
        raw_exchange={"parent_trade_order_id": "OPEN_B3", "sl_order_id": "SL_B3_ETH"},
    )
    LiveTradesService.record_exchange_quarantine(
        symbol="PF_XBTUSD", order_id="ORPHAN_B3_BTC", anomaly_type="position",
        reason="Untracked BTC position", broker="kraken", raw_exchange={"symbol": "PF_XBTUSD"},
    )
    client = ControlledFakeRestClient(positions=[], open_orders=[_eth_sl(order_id="SL_B3_ETH", cli_ord_id="BOT_SL_B3")])
    wrapper = _make_wrapper(client)
    wrapper._reconciliation_required_symbols.add("PF_ETHUSD")

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
    assert "PF_ETHUSD" not in wrapper._reconciliation_required_symbols
    assert len(client.cancel_calls) == 1


@pytest.mark.asyncio
async def test_b4_rest_unavailable_during_hot_retry_keeps_all_locks(isolated_sqlite):
    """B4: REST error during hot retry -> symbol & global locks preserved, 0 cancel calls."""
    LiveTradesService.record_exchange_quarantine(
        symbol="PF_ETHUSD", order_id="SL_B4", anomaly_type="residual_sl_cleanup",
        client_oid="BOT_SL_B4", reason="CANCEL_FAILED", broker="kraken",
        raw_exchange={"parent_trade_order_id": "OPEN_B4", "sl_order_id": "SL_B4"},
    )
    client = ControlledFakeRestClient(positions=[], open_orders_exc=RuntimeError("Kraken 503"))
    wrapper = _make_wrapper(client)
    wrapper._reconciliation_required_symbols.add("PF_ETHUSD")

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols
    assert rep["residual_sl_cleanups"][0]["status"] == "CANCEL_UNKNOWN_REST"
    assert len(client.cancel_calls) == 0


@pytest.mark.asyncio
async def test_b5_b6_open_refused_on_locked_symbol_and_other_symbol_while_global_locked(isolated_sqlite, monkeypatch):
    """B5 & B6: OPEN refused on both locked symbol (ETH) and other symbol (BTC) while global lock is active."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = ControlledFakeRestClient()
    wrapper = _make_wrapper(client)
    wrapper._global_entry_lock = True
    wrapper._reconciliation_required_symbols.add("PF_ETHUSD")

    res_eth = await wrapper.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.05, order_type="market",
        entry_price=3000.0, stop_loss=2940.0, intent="open", reduce_only=False,
    )
    res_btc = await wrapper.place_order(
        symbol="BTCUSDT", side="buy", quantity=0.002, order_type="market",
        entry_price=60000.0, stop_loss=59000.0, intent="open", reduce_only=False,
    )

    assert res_eth["success"] is False and res_eth["code"] in ("GLOBAL_ENTRY_LOCK_ACTIVE", "SYMBOL_QUARANTINED")
    assert res_btc["success"] is False and res_btc["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_b7_reduce_and_close_preserved_while_global_lock_active(isolated_sqlite, monkeypatch):
    """B7: REDUCE and CLOSE remain allowed on non-quarantined tracked position while _global_entry_lock is True."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    client = ControlledFakeRestClient(positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}])
    wrapper = _make_wrapper(client)
    wrapper._global_entry_lock = True
    wrapper._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="EXISTING_B7", client_oid="BOT_B7", symbol="PF_ETHUSD",
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


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTION C — CONFIRMATION REST DE L'ANNULATION (C1..C9)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_c1_cancel_success_then_absent_on_reread_confirms_cancelled(isolated_sqlite):
    """C1: cancel_order succeeds + re-read open_orders shows absent -> CANCELLED_CONFIRMED, lock released."""
    _seed_eth_open(order_id="OPEN_C1", sl_oid="SL_C1", sl_clid="BOT_SL_C1", tp_oid="TP_C1", tp_clid="BOT_TP_C1")
    client = ControlledFakeRestClient(
        positions=[],
        open_orders=[_eth_sl(order_id="SL_C1", cli_ord_id="BOT_SL_C1")],
        fills=[{"fill_id": "F_C1", "order_id": "TP_C1", "cliOrdId": "BOT_TP_C1", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
        remove_on_cancel=True,
    )
    wrapper = _make_wrapper(client)

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert rep["residual_sl_cleanups"][0]["confirmation_status"] == "CANCELLED_CONFIRMED"
    assert len(client.cancel_calls) == 1
    assert client.open_orders_calls >= 2  # initial fetch + post-cancel re-read!
    assert LiveTradesService.get_quarantined_trades(broker="kraken") == []


@pytest.mark.asyncio
async def test_c2_cancel_success_but_order_still_present_marks_not_confirmed_and_locks(isolated_sqlite):
    """C2: cancel_order returns success BUT re-read still sees SL -> CANCEL_NOT_CONFIRMED, anomaly persisted, locked."""
    _seed_eth_open(order_id="OPEN_C2", sl_oid="SL_C2", sl_clid="BOT_SL_C2", tp_oid="TP_C2", tp_clid="BOT_TP_C2")
    client = ControlledFakeRestClient(
        positions=[],
        open_orders=[_eth_sl(order_id="SL_C2", cli_ord_id="BOT_SL_C2")],
        fills=[{"fill_id": "F_C2", "order_id": "TP_C2", "cliOrdId": "BOT_TP_C2", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
        remove_on_cancel=False,  # Order stubbornly remains in open_orders!
    )
    wrapper = _make_wrapper(client)

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["residual_sl_cleanups"][0]["status"] == "CANCEL_NOT_CONFIRMED"
    assert rep["residual_sl_cleanups"][0]["confirmation_status"] == "CANCEL_NOT_CONFIRMED"
    q_rows = LiveTradesService.get_quarantined_trades(broker="kraken")
    assert len(q_rows) == 1 and q_rows[0].order_id == "SL_C2"
    assert len(client.cancel_calls) == 1


@pytest.mark.asyncio
async def test_c3_cancel_success_then_reread_timeout_marks_unknown_rest_and_locks(isolated_sqlite):
    """C3: cancel_order succeeds BUT post-cancel get_open_orders times out -> CANCEL_UNKNOWN_REST, locked."""
    _seed_eth_open(order_id="OPEN_C3", sl_oid="SL_C3", sl_clid="BOT_SL_C3", tp_oid="TP_C3", tp_clid="BOT_TP_C3")
    client = ControlledFakeRestClient(
        positions=[],
        open_orders=[_eth_sl(order_id="SL_C3", cli_ord_id="BOT_SL_C3")],
        fills=[{"fill_id": "F_C3", "order_id": "TP_C3", "cliOrdId": "BOT_TP_C3", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
        post_cancel_open_orders_exc=asyncio.TimeoutError("re-read timeout"),
    )
    wrapper = _make_wrapper(client)

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["residual_sl_cleanups"][0]["status"] == "CANCEL_UNKNOWN_REST"
    assert len(LiveTradesService.get_quarantined_trades(broker="kraken")) == 1


@pytest.mark.asyncio
async def test_c4_cancel_success_then_malformed_reread_marks_unknown_rest_and_locks(isolated_sqlite):
    """C4: cancel_order succeeds BUT re-read returns malformed payload -> CANCEL_UNKNOWN_REST, locked."""
    _seed_eth_open(order_id="OPEN_C4", sl_oid="SL_C4", sl_clid="BOT_SL_C4", tp_oid="TP_C4", tp_clid="BOT_TP_C4")
    client = ControlledFakeRestClient(
        positions=[],
        open_orders=[_eth_sl(order_id="SL_C4", cli_ord_id="BOT_SL_C4")],
        fills=[{"fill_id": "F_C4", "order_id": "TP_C4", "cliOrdId": "BOT_TP_C4", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
        post_cancel_open_orders_override="malformed_not_a_list",
    )
    wrapper = _make_wrapper(client)

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["residual_sl_cleanups"][0]["status"] == "CANCEL_UNKNOWN_REST"
    assert len(LiveTradesService.get_quarantined_trades(broker="kraken")) == 1


@pytest.mark.asyncio
async def test_c5_contradictory_identifiers_refuses_cancel_and_locks(isolated_sqlite):
    """C5: Pending residual_sl_cleanup sees matching order_id with contradictory cliOrdId -> CANCEL_NOT_CONFIRMED, 0 cancel."""
    LiveTradesService.record_exchange_quarantine(
        symbol="PF_ETHUSD", order_id="SL_C5", anomaly_type="residual_sl_cleanup",
        client_oid="BOT_SL_C5_EXPECTED", reason="CANCEL_FAILED", broker="kraken",
        raw_exchange={"parent_trade_order_id": "OPEN_C5", "sl_order_id": "SL_C5"},
    )
    contradictory = _eth_sl(order_id="SL_C5", cli_ord_id="DIFFERENT_CLID_999")
    client = ControlledFakeRestClient(positions=[], open_orders=[contradictory])
    wrapper = _make_wrapper(client)

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert rep["residual_sl_cleanups"][0]["status"] == "CANCEL_NOT_CONFIRMED"
    assert len(client.cancel_calls) == 0


@pytest.mark.asyncio
async def test_c6_order_on_another_symbol_never_cancelled_or_confused(isolated_sqlite):
    """C6: Order with same ID on PF_XBTUSD is ignored when checking PF_ETHUSD residual SL -> 0 cancel on BTC."""
    LiveTradesService.record_exchange_quarantine(
        symbol="PF_ETHUSD", order_id="SL_C6", anomaly_type="residual_sl_cleanup",
        client_oid="BOT_SL_C6", reason="CANCEL_FAILED", broker="kraken",
        raw_exchange={"parent_trade_order_id": "OPEN_C6", "sl_order_id": "SL_C6"},
    )
    btc_ord = {
        "order_id": "SL_C6", "cliOrdId": "BOT_SL_C6", "symbol": "PF_XBTUSD",
        "side": "sell", "size": 0.002, "quantity": 0.002, "stopPrice": 59000.0,
        "orderType": "stp", "reduceOnly": True, "status": "untouched",
    }
    client = ControlledFakeRestClient(positions=[], open_orders=[btc_ord])
    wrapper = _make_wrapper(client)

    await wrapper.reconcile_on_startup()

    assert len(client.cancel_calls) == 0
    assert any(o["symbol"] == "PF_XBTUSD" and o["order_id"] == "SL_C6" for o in client.open_orders)


@pytest.mark.asyncio
async def test_c7_order_already_absent_before_cancel_confirmed_absent_zero_cancels(isolated_sqlite):
    """C7: Residual SL already absent before cancel retry -> CONFIRMED_ABSENT, 0 cancel calls, anomaly closed."""
    LiveTradesService.record_exchange_quarantine(
        symbol="PF_ETHUSD", order_id="SL_C7", anomaly_type="residual_sl_cleanup",
        client_oid="BOT_SL_C7", reason="CANCEL_UNKNOWN_TIMEOUT", broker="kraken",
        raw_exchange={"parent_trade_order_id": "OPEN_C7", "sl_order_id": "SL_C7"},
    )
    client = ControlledFakeRestClient(positions=[], open_orders=[])
    wrapper = _make_wrapper(client)

    rep = await wrapper.reconcile_on_startup()

    assert rep["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert rep["residual_sl_cleanups"][0]["status"] == "CONFIRMED_ABSENT"
    assert len(client.cancel_calls) == 0
    assert LiveTradesService.get_quarantined_trades(broker="kraken") == []


@pytest.mark.asyncio
async def test_c8_c9_two_successive_reconciliations_after_uncertain_cancel_never_close_without_proof(isolated_sqlite):
    """C8 & C9: Run 1 cancel timeout -> anomaly stays QUARANTINED; Run 2 re-read confirms absent -> anomaly closed & unlocked."""
    _seed_eth_open(order_id="OPEN_C8", sl_oid="SL_C8", sl_clid="BOT_SL_C8", tp_oid="TP_C8", tp_clid="BOT_TP_C8")
    client = ControlledFakeRestClient(
        positions=[],
        open_orders=[_eth_sl(order_id="SL_C8", cli_ord_id="BOT_SL_C8")],
        fills=[{"fill_id": "F_C8", "order_id": "TP_C8", "cliOrdId": "BOT_TP_C8", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
        post_cancel_open_orders_exc=asyncio.TimeoutError("re-read timeout"),
        remove_on_cancel=True,  # Cancel actually succeeded on exchange, but re-read timed out!
    )
    wrapper = _make_wrapper(client)

    # Run 1: uncertain re-read -> MUST NOT close anomaly
    rep1 = await wrapper.reconcile_on_startup()
    assert rep1["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert len(LiveTradesService.get_quarantined_trades(broker="kraken")) == 1
    assert len(client.cancel_calls) == 1

    # Run 2: REST healthy, re-read shows order absent -> CONFIRMED_ABSENT, 0 redundant cancel, anomaly closed
    client.post_cancel_open_orders_exc = None
    client._cancel_happened = False
    rep2 = await wrapper.reconcile_on_startup()
    assert rep2["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert rep2["residual_sl_cleanups"][0]["status"] == "CONFIRMED_ABSENT"
    assert len(client.cancel_calls) == 1  # No redundant 2nd cancel!
    assert LiveTradesService.get_quarantined_trades(broker="kraken") == []
    assert len(client.exec_calls) == 0
