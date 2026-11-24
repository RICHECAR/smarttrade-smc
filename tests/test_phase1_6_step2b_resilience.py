"""
Phase 1.6 / Step 2B — Kraken Resilience & Degraded-Scenario Suite (100% Offline)
================================================================================
Validates SmartTrade's real production safety & startup reconciliation logic
against adversarial / degraded exchange states, strictly offline:
  - External sockets hard-blocked (connect / create_connection / getaddrinfo).
  - Temporary SQLite DB via pytest `tmp_path` ONLY (never production data/trades.db).
  - Fake REST/WS clients with strict call counters for every execution/cancel method.
  - Zero production code modified, zero real Kraken calls, zero real orders.

Mandatory scenarios:
  TEST 1 : Orphan position on Kraken (absent from SQLite)
  TEST 2 : OPEN trade in SQLite with missing SL on Kraken (MISSING_ON_EXCHANGE)
  TEST 3 : OPEN trade with invalid SL (7 individual D2 cases)
  TEST 4 : TP executed with residual active SL (+ non-atomicity failure check)
  TEST 5 : Quantity / exposure mismatch (EXCHANGE_SQLITE_EXPOSURE_MISMATCH) + exact match control
  TEST 6 : REST errors / timeouts / malformed payloads (4 individual cases)
  TEST 7 : Dangerous OPEN entry refusals (7 individual cases)
"""

import asyncio
import json
import math
import os
import socket
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


# ── Hard-block ALL external network sockets ───────────────────────────────────
class _ExternalNetworkBlocked(RuntimeError):
    pass


_ORIG_CONNECT = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) and address else ""
    if host not in ("127.0.0.1", "::1"):
        raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.6 Step 2B: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.6 Step 2B: {args}")


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
from backend.models.live_trade import LiveTrade
from backend.services.live_trades_service import LiveTradesService


@pytest.fixture
def isolated_sqlite(tmp_path, monkeypatch):
    """Real temporary SQLite DB via tmp_path; production data/trades.db is never touched."""
    db_file = tmp_path / "test_phase1_6_step2b.db"
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


_UNSET = object()


class RecordedFakeRestClient:
    """
    100% offline simulated Kraken REST client recording every execution and
    cancellation call without ever touching the network.
    """

    def __init__(
        self,
        positions: Any = _UNSET,
        open_orders: Any = _UNSET,
        fills: Any = _UNSET,
        tickers: Optional[Dict[str, Dict[str, Any]]] = None,
        positions_exc: Optional[BaseException] = None,
        open_orders_exc: Optional[BaseException] = None,
        fills_exc: Optional[BaseException] = None,
        cancel_exc: Optional[BaseException] = None,
    ):
        self.positions = [] if positions is _UNSET else positions
        self.open_orders = [] if open_orders is _UNSET else open_orders
        self.fills = [] if fills is _UNSET else fills
        self.tickers = (
            tickers
            if tickers is not None
            else {
                "PF_ETHUSD": {"symbol": "PF_ETHUSD", "mark_price": 3000.0, "last": 3000.0},
                "PF_XBTUSD": {"symbol": "PF_XBTUSD", "mark_price": 60000.0, "last": 60000.0},
            }
        )
        self.positions_exc = positions_exc
        self.open_orders_exc = open_orders_exc
        self.fills_exc = fills_exc
        self.cancel_exc = cancel_exc

        # Strict call counters
        self.exec_calls: List[Dict[str, Any]] = []
        self.cancel_calls: List[Dict[str, Any]] = []

    async def get_tickers(self) -> Dict[str, Dict[str, Any]]:
        return dict(self.tickers)

    async def get_positions(self, **kwargs):
        if self.positions_exc is not None:
            raise self.positions_exc
        return self.positions

    async def get_open_orders(self, symbol: Optional[str] = None, **kwargs):
        if self.open_orders_exc is not None:
            raise self.open_orders_exc
        if not isinstance(self.open_orders, list):
            return self.open_orders
        if symbol:
            return [o for o in self.open_orders if str(o.get("symbol", "")).upper() == symbol.upper()]
        return list(self.open_orders)

    async def get_fills(self, **kwargs):
        if self.fills_exc is not None:
            raise self.fills_exc
        return list(self.fills)

    async def _request(self, method: str, endpoint: str, *args, **kwargs):
        if "fills" in str(endpoint).lower():
            if self.fills_exc is not None:
                raise self.fills_exc
            return {"result": "success", "fills": list(self.fills)}
        raise RuntimeError(f"Unexpected _request in RecordedFakeRestClient: {method} {endpoint}")

    async def cancel_order(self, order_id: Optional[str] = None, cli_ord_id: Optional[str] = None, **kwargs):
        self.cancel_calls.append({"order_id": order_id, "cli_ord_id": cli_ord_id, **kwargs})
        if self.cancel_exc is not None:
            raise self.cancel_exc
        if isinstance(self.open_orders, list):
            self.open_orders = [
                o
                for o in self.open_orders
                if not (
                    (order_id and o.get("order_id") == order_id)
                    or (cli_ord_id and o.get("cliOrdId") == cli_ord_id)
                )
            ]
        return {"success": True, "code": "00000", "order_id": order_id}

    async def cancel_all_orders(self, **kwargs):
        self.cancel_calls.append({"action": "cancel_all_orders", **kwargs})
        return {"success": True}

    async def _record_exec(self, method: str, **kwargs):
        self.exec_calls.append({"method": method, **kwargs})
        return {"success": False, "code": "SIMULATED_FORBIDDEN_EXEC"}

    async def place_order(self, **kwargs):
        return await self._record_exec("place_order", **kwargs)

    async def place_market_order(self, **kwargs):
        return await self._record_exec("place_market_order", **kwargs)

    async def place_limit_order(self, **kwargs):
        return await self._record_exec("place_limit_order", **kwargs)

    async def place_stop_order(self, **kwargs):
        return await self._record_exec("place_stop_order", **kwargs)

    async def place_take_profit_order(self, **kwargs):
        return await self._record_exec("place_take_profit_order", **kwargs)

    async def edit_order(self, **kwargs):
        return await self._record_exec("edit_order", **kwargs)


def _make_wrapper(client: RecordedFakeRestClient, unlock: bool = False) -> KrakenFuturesExecutorWrapper:
    wrapper = KrakenFuturesExecutorWrapper(
        api_key="test_key_2b",
        api_secret="dGVzdF9zZWNyZXRfMmI=",
        timeout=2.0,
        single_entry_mode=False,
    )
    wrapper._rest_client = client
    wrapper._get_rest_client = lambda: client
    if unlock:
        wrapper._global_entry_lock = False
        wrapper._startup_reconciliation_completed = True
    return wrapper


def _seed_open_eth_trade(
    order_id: str = "OPEN_ETH_OID_1",
    sl_oid: str = "SL_ETH_OID_1",
    sl_clid: str = "BOT_SL_PF_ETHUSD_1",
    tp_oid: Optional[str] = None,
    tp_clid: Optional[str] = None,
    qty: float = 0.05,
    entry: float = 3000.0,
    stop: float = 2940.0,
    tp: float = 3120.0,
) -> LiveTrade:
    protections: Dict[str, Any] = {
        "version": 1,
        "sl": {
            "order_id": sl_oid,
            "client_oid": sl_clid,
            "order_type": "stp",
            "trigger_price": stop,
            "placed_qty": qty,
            "status": "ATTACHED",
        },
    }
    if tp_oid or tp_clid:
        protections["tp"] = {
            "order_id": tp_oid,
            "client_oid": tp_clid,
            "order_type": "take_profit",
            "trigger_price": tp,
            "placed_qty": qty,
            "status": "ATTACHED",
        }
    trade = LiveTradesService.create_live_trade(
        order_id=order_id,
        symbol="PF_ETHUSD",
        side="long",
        qty=qty,
        entry=entry,
        stop=stop,
        tp=tp,
        leverage=10,
        broker="kraken",
        status="OPEN",
        client_oid="BOT_PF_ETHUSD_OPEN_1",
        raw_exchange={"order_id": order_id, "protections": protections},
    )
    assert trade is not None
    return trade


def _valid_eth_sl_order(
    order_id: str = "SL_ETH_OID_1",
    cli_ord_id: str = "BOT_SL_PF_ETHUSD_1",
    symbol: str = "PF_ETHUSD",
    side: str = "sell",
    size: float = 0.05,
    stop_price: float = 2940.0,
    order_type: str = "stp",
    reduce_only: Any = True,
    status: str = "untouched",
) -> Dict[str, Any]:
    return {
        "order_id": order_id,
        "cliOrdId": cli_ord_id,
        "symbol": symbol,
        "side": side,
        "size": size,
        "quantity": size,
        "filled": 0.0,
        "stopPrice": stop_price,
        "orderType": order_type,
        "reduceOnly": reduce_only,
        "status": status,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TEST 1 — POSITION ORPHELINE SUR KRAKEN
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_orphan_position_on_kraken_quarantined_and_locked(isolated_sqlite, monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = RecordedFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[],
    )
    wrapper = _make_wrapper(client)

    report = await wrapper.reconcile_on_startup()

    assert wrapper._global_entry_lock is True
    assert report["startup_secure"] is False
    assert report["global_entry_lock"] is True
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols

    # Quarantine persisted in temporary SQLite
    q_trades = LiveTradesService.get_quarantined_trades(broker="kraken")
    assert len(q_trades) == 1
    assert q_trades[0].symbol == "PF_ETHUSD"
    assert q_trades[0].status == "QUARANTINED"

    # Subsequent OPEN entry MUST be refused
    open_res = await wrapper.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.05, order_type="market",
        entry_price=3000.0, stop_loss=2940.0, intent="open", reduce_only=False,
    )
    assert open_res["success"] is False
    assert open_res["code"] in ("GLOBAL_ENTRY_LOCK_ACTIVE", "SYMBOL_QUARANTINED")

    # Zero execution or cancel calls
    assert len(client.exec_calls) == 0
    assert len(client.cancel_calls) == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST 2 — TRADE OPEN AVEC SL ABSENT (MISSING_ON_EXCHANGE)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_2_open_trade_with_missing_sl_marked_missing_and_quarantined(isolated_sqlite, monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    trade = _seed_open_eth_trade()

    client = RecordedFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[],  # SL missing on exchange!
    )
    wrapper = _make_wrapper(client)

    report = await wrapper.reconcile_on_startup()

    assert wrapper._global_entry_lock is True
    assert report["startup_secure"] is False
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols

    reloaded = LiveTradesService.get_trade_by_id(trade.id)
    assert reloaded is not None
    assert reloaded.status == "QUARANTINED"
    raw = reloaded.get_raw_exchange()
    assert raw["protections"]["sl"]["status"] == "MISSING_ON_EXCHANGE"

    # New entry blocked
    open_res = await wrapper.place_order(
        symbol="BTCUSDT", side="buy", quantity=0.002, order_type="market",
        entry_price=60000.0, stop_loss=59000.0, intent="open", reduce_only=False,
    )
    assert open_res["success"] is False
    assert open_res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    assert len(client.exec_calls) == 0
    assert len(client.cancel_calls) == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST 3 — TRADE OPEN AVEC SL INVALIDE (7 CAS INDIVIDUELS)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_name,order_override,expected_sl_status",
    [
        ("wrong_symbol", {"symbol": "PF_XBTUSD"}, "MISSING_ON_EXCHANGE"),
        ("wrong_side", {"side": "buy"}, "INVALID_ON_EXCHANGE"),
        ("reduce_only_false", {"reduceOnly": False}, "INVALID_ON_EXCHANGE"),
        ("wrong_order_type_lmt", {"orderType": "lmt"}, "INVALID_ON_EXCHANGE"),
        ("wrong_quantity", {"size": 0.01, "quantity": 0.01}, "INVALID_ON_EXCHANGE"),
        ("invalid_trigger_price_above_entry", {"stopPrice": 3050.0}, "INVALID_ON_EXCHANGE"),
        ("missing_identifiers", {"order_id": "", "cliOrdId": ""}, "MISSING_ON_EXCHANGE"),
    ],
)
async def test_3_open_trade_with_invalid_sl_rejected_and_quarantined(
    isolated_sqlite, monkeypatch, case_name, order_override, expected_sl_status
):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    trade = _seed_open_eth_trade()

    bad_sl = _valid_eth_sl_order()
    bad_sl.update(order_override)

    client = RecordedFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[bad_sl],
    )
    wrapper = _make_wrapper(client)

    report = await wrapper.reconcile_on_startup()

    assert wrapper._global_entry_lock is True, f"Case {case_name}: lock must stay True"
    assert report["startup_secure"] is False, f"Case {case_name}: startup_secure must be False"
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols

    reloaded = LiveTradesService.get_trade_by_id(trade.id)
    assert reloaded is not None
    assert reloaded.status == "QUARANTINED", f"Case {case_name}: trade must be QUARANTINED"
    raw = reloaded.get_raw_exchange()
    assert raw["protections"]["sl"]["status"] == expected_sl_status

    assert len(client.exec_calls) == 0
    assert len(client.cancel_calls) == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST 4 — TP EXÉCUTÉ AVEC SL RÉSIDUEL ACTIF (+ ÉCHEC INTERMÉDIAIRE CANCEL)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_4a_tp_executed_closes_trade_and_cancels_only_residual_sl(isolated_sqlite):
    trade = _seed_open_eth_trade(tp_oid="TP_ETH_OID_1", tp_clid="BOT_TP_PF_ETHUSD_1")

    residual_sl = _valid_eth_sl_order(order_id="SL_ETH_OID_1", cli_ord_id="BOT_SL_PF_ETHUSD_1")
    unrelated_btc_order = {
        "order_id": "UNRELATED_BTC_OID",
        "cliOrdId": "BOT_SL_PF_XBTUSD_99",
        "symbol": "PF_XBTUSD",
        "side": "sell",
        "size": 0.002,
        "quantity": 0.002,
        "stopPrice": 59000.0,
        "orderType": "stp",
        "reduceOnly": True,
        "status": "untouched",
    }

    client = RecordedFakeRestClient(
        positions=[],  # Position now flat after TP filled
        open_orders=[residual_sl, unrelated_btc_order],
        fills=[
            {
                "fill_id": "FILL_TP_1",
                "order_id": "TP_ETH_OID_1",
                "cliOrdId": "BOT_TP_PF_ETHUSD_1",
                "symbol": "PF_ETHUSD",
                "side": "sell",
                "size": 0.05,
                "price": 3120.0,
                "fillTime": "2026-11-21T18:00:00Z",
            }
        ],
    )
    wrapper = _make_wrapper(client)

    await wrapper.reconcile_on_startup()

    # 1. Trade closed coherently in temporary SQLite
    reloaded = LiveTradesService.get_trade_by_id(trade.id)
    assert reloaded is not None
    assert reloaded.status == "CLOSED"
    assert reloaded.close_reason == "take_profit_hit"
    raw = reloaded.get_raw_exchange()
    assert raw["protections"]["tp"]["status"] == "FILLED"

    # 2. cancel_order called EXACTLY once for the residual SL, NEVER for unrelated BTC order
    assert len(client.cancel_calls) == 1
    assert client.cancel_calls[0]["order_id"] == "SL_ETH_OID_1"
    assert client.cancel_calls[0]["cli_ord_id"] == "BOT_SL_PF_ETHUSD_1"
    assert any(o["order_id"] == "UNRELATED_BTC_OID" for o in client.open_orders)

    # 3. Zero execution orders dispatched
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_4b_tp_executed_with_cancel_failure_documents_non_atomicity(isolated_sqlite):
    """
    Intermediate failure test: if cancel_order fails after SQLite close_live_trade(),
    verify exact production behavior (non-atomic: SQLite is CLOSED, cancel_order was
    attempted once and failed, residual SL stays in open_orders).
    """
    trade = _seed_open_eth_trade(
        sl_oid="SL_ETH_OID_2",
        sl_clid="BOT_SL_PF_ETHUSD_2",
        tp_oid="TP_ETH_OID_2",
        tp_clid="BOT_TP_PF_ETHUSD_2",
    )
    residual_sl = _valid_eth_sl_order(order_id="SL_ETH_OID_2", cli_ord_id="BOT_SL_PF_ETHUSD_2")

    client = RecordedFakeRestClient(
        positions=[],
        open_orders=[residual_sl],
        fills=[
            {
                "fill_id": "FILL_TP_2",
                "order_id": "TP_ETH_OID_2",
                "cliOrdId": "BOT_TP_PF_ETHUSD_2",
                "symbol": "PF_ETHUSD",
                "side": "sell",
                "size": 0.05,
                "price": 3120.0,
                "fillTime": "2026-11-21T18:01:00Z",
            }
        ],
        cancel_exc=RuntimeError("Simulated Kraken 503 on cancel_order"),
    )
    wrapper = _make_wrapper(client)

    await wrapper.reconcile_on_startup()

    reloaded = LiveTradesService.get_trade_by_id(trade.id)
    assert reloaded is not None
    assert reloaded.status == "CLOSED"
    assert len(client.cancel_calls) == 1
    assert client.cancel_calls[0]["order_id"] == "SL_ETH_OID_2"
    # Residual SL still present on exchange because cancel failed (documented non-atomicity)
    assert any(o["order_id"] == "SL_ETH_OID_2" for o in client.open_orders)
    assert len(client.exec_calls) == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST 5 — INCOHÉRENCE DE QUANTITÉ + CONTRÔLE QUANTITÉ IDENTIQUE
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_5a_quantity_exposure_mismatch_quarantined_and_locked(isolated_sqlite):
    trade = _seed_open_eth_trade(qty=0.05)
    valid_sl = _valid_eth_sl_order(size=0.05)

    client = RecordedFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.12, "price": 3000.0}],  # 0.12 != 0.05
        open_orders=[valid_sl],
    )
    wrapper = _make_wrapper(client)

    report = await wrapper.reconcile_on_startup()

    assert wrapper._global_entry_lock is True
    assert report["startup_secure"] is False
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols

    reloaded = LiveTradesService.get_trade_by_id(trade.id)
    assert reloaded is not None
    assert reloaded.status == "QUARANTINED"
    assert "EXCHANGE_SQLITE_EXPOSURE_MISMATCH" in (reloaded.error_message or "")

    assert len(client.exec_calls) == 0
    assert len(client.cancel_calls) == 0


@pytest.mark.asyncio
async def test_5b_exact_matching_quantity_and_valid_sl_no_false_positive(isolated_sqlite):
    trade = _seed_open_eth_trade(qty=0.05)
    valid_sl = _valid_eth_sl_order(size=0.05)

    client = RecordedFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[valid_sl],
    )
    wrapper = _make_wrapper(client)

    report = await wrapper.reconcile_on_startup()

    assert report["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert "PF_ETHUSD" in report["healthy"]
    assert len(report["quarantined_symbols"]) == 0

    reloaded = LiveTradesService.get_trade_by_id(trade.id)
    assert reloaded is not None
    assert reloaded.status == "OPEN"
    assert len(client.exec_calls) == 0
    assert len(client.cancel_calls) == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST 6 — ERREUR REST / TIMEOUT / RÉPONSE MALFORMÉE (4 CAS INDIVIDUELS)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_6a_timeout_reading_positions_fails_closed(isolated_sqlite, monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = RecordedFakeRestClient(positions_exc=asyncio.TimeoutError("Simulated positions timeout"))
    wrapper = _make_wrapper(client)

    report = await wrapper.reconcile_on_startup()

    assert wrapper._global_entry_lock is True
    assert report["startup_secure"] is False
    assert len(client.exec_calls) == 0
    assert len(client.cancel_calls) == 0

    open_res = await wrapper.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.05, order_type="market",
        entry_price=3000.0, stop_loss=2940.0, intent="open", reduce_only=False,
    )
    assert open_res["success"] is False
    assert open_res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"


@pytest.mark.asyncio
async def test_6b_rest_error_reading_open_orders_fails_closed(isolated_sqlite, monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    _seed_open_eth_trade()
    client = RecordedFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders_exc=RuntimeError("Kraken REST 503 on openorders"),
    )
    wrapper = _make_wrapper(client)

    report = await wrapper.reconcile_on_startup()

    assert wrapper._global_entry_lock is True
    assert report["startup_secure"] is False
    assert len(client.exec_calls) == 0
    assert len(client.cancel_calls) == 0


@pytest.mark.asyncio
async def test_6c_error_during_protection_verification_fails_closed(isolated_sqlite, monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    _seed_open_eth_trade()
    client = RecordedFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_valid_eth_sl_order()],
    )
    wrapper = _make_wrapper(client)

    def _boom(*args, **kwargs):
        raise RuntimeError("Simulated protection validator crash")

    monkeypatch.setattr(wrapper, "_validate_recovered_protection", _boom)

    report = await wrapper.reconcile_on_startup()

    assert wrapper._global_entry_lock is True
    assert report["startup_secure"] is False
    assert len(client.exec_calls) == 0
    assert len(client.cancel_calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_positions", [None, "not_a_list", {"unexpected": True}])
async def test_6d_malformed_positions_payload_fails_closed(isolated_sqlite, malformed_positions):
    client = RecordedFakeRestClient(positions=malformed_positions)
    wrapper = _make_wrapper(client)

    report = await wrapper.reconcile_on_startup()

    assert wrapper._global_entry_lock is True
    assert report["startup_secure"] is False
    assert len(client.exec_calls) == 0
    assert len(client.cancel_calls) == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST 7 — REFUS D'ENTRÉE DANGEREUSE (7 CAS INDIVIDUELS)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_name,exec_enabled,unlock,symbol,side,entry_price,stop_loss,expected_codes",
    [
        ("sl_missing", "true", True, "ETHUSDT", "buy", 3000.0, None, ("INVALID_STOP_LOSS",)),
        ("sl_wrong_side_long", "true", True, "ETHUSDT", "buy", 3000.0, 3050.0, ("INVALID_STOP_LOSS_DIRECTION",)),
        ("sl_nan", "true", True, "ETHUSDT", "buy", 3000.0, float("nan"), ("INVALID_STOP_LOSS",)),
        ("sl_inf", "true", True, "ETHUSDT", "buy", 3000.0, float("inf"), ("INVALID_STOP_LOSS",)),
        ("sl_zero", "true", True, "ETHUSDT", "buy", 3000.0, 0.0, ("INVALID_STOP_LOSS",)),
        ("sl_negative", "true", True, "ETHUSDT", "buy", 3000.0, -10.0, ("INVALID_STOP_LOSS",)),
        ("global_lock_active", "true", False, "ETHUSDT", "buy", 3000.0, 2940.0, ("GLOBAL_ENTRY_LOCK_ACTIVE",)),
        ("kraken_exec_disabled", "false", True, "ETHUSDT", "buy", 3000.0, 2940.0, ("KRAKEN_EXECUTION_DISABLED",)),
        ("unsupported_symbol", "true", True, "UNKNOWNCOINUSDT", "buy", 10.0, 9.0, ("INVALID_STOP_LOSS_TICK", "INVALID_SIZE")),
    ],
)
async def test_7_dangerous_open_entry_refused_before_send(
    isolated_sqlite, monkeypatch, case_name, exec_enabled, unlock, symbol, side, entry_price, stop_loss, expected_codes
):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", exec_enabled)
    client = RecordedFakeRestClient()
    wrapper = _make_wrapper(client, unlock=unlock)

    res = await wrapper.place_order(
        symbol=symbol,
        side=side,
        quantity=0.05,
        order_type="market",
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=3120.0,
        intent="open",
        reduce_only=False,
    )

    assert res["success"] is False, f"Case {case_name} must be refused"
    assert res.get("code") in expected_codes, f"Case {case_name}: got code={res.get('code')}, expected one of {expected_codes}"
    assert len(client.exec_calls) == 0, f"Case {case_name}: zero execution calls permitted"
    assert len(client.cancel_calls) == 0
    assert LiveTradesService.get_open_trades(broker="kraken") == []


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1.6 / STEP 2B.1 — MANDATORY P1 TESTS (A, B, C, D)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_p1_a_sl_validation_exception_quarantines_locks_and_returns_structured_report(isolated_sqlite, monkeypatch):
    """P1-A: Exception during SL validation -> VALIDATION_INTERNAL_ERROR, QUARANTINED, locked, structured report, 0 orders."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    trade = _seed_open_eth_trade()
    client = RecordedFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_valid_eth_sl_order()],
    )
    wrapper = _make_wrapper(client)

    def _boom_sl(*args, **kwargs):
        if kwargs.get("protection_type") == "sl":
            raise ValueError("Simulated SL validator internal crash")
        return True, None

    monkeypatch.setattr(wrapper, "_validate_recovered_protection", _boom_sl)

    report = await wrapper.reconcile_on_startup()

    assert wrapper._global_entry_lock is True
    assert report["startup_secure"] is False
    assert report["global_entry_lock"] is True
    assert "validation_errors" in report and len(report["validation_errors"]) == 1
    assert report["validation_errors"][0]["protection_type"] == "sl"
    assert report["validation_errors"][0]["status"] == "VALIDATION_INTERNAL_ERROR"

    reloaded = LiveTradesService.get_trade_by_id(trade.id)
    assert reloaded is not None
    assert reloaded.status == "QUARANTINED"
    assert reloaded.get_raw_exchange()["protections"]["sl"]["status"] == "VALIDATION_INTERNAL_ERROR"
    assert len(client.exec_calls) == 0
    assert len(client.cancel_calls) == 0


@pytest.mark.asyncio
async def test_p1_b_tp_validation_exception_quarantines_locks_and_returns_structured_report(isolated_sqlite, monkeypatch):
    """P1-B: Exception during TP validation -> VALIDATION_INTERNAL_ERROR, QUARANTINED, locked, structured report, 0 orders."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    trade = _seed_open_eth_trade(tp_oid="TP_ETH_OID_B", tp_clid="BOT_TP_PF_ETHUSD_B")
    valid_tp = {
        "order_id": "TP_ETH_OID_B",
        "cliOrdId": "BOT_TP_PF_ETHUSD_B",
        "symbol": "PF_ETHUSD",
        "side": "sell",
        "size": 0.05,
        "quantity": 0.05,
        "stopPrice": 3120.0,
        "orderType": "take_profit",
        "reduceOnly": True,
        "status": "untouched",
    }
    client = RecordedFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_valid_eth_sl_order(), valid_tp],
    )
    wrapper = _make_wrapper(client)
    orig_validate = wrapper._validate_recovered_protection

    def _boom_tp(*args, **kwargs):
        if kwargs.get("protection_type") == "tp":
            raise KeyError("Simulated TP validator internal crash")
        return orig_validate(*args, **kwargs)

    monkeypatch.setattr(wrapper, "_validate_recovered_protection", _boom_tp)

    report = await wrapper.reconcile_on_startup()

    assert wrapper._global_entry_lock is True
    assert report["startup_secure"] is False
    assert "validation_errors" in report and len(report["validation_errors"]) == 1
    assert report["validation_errors"][0]["protection_type"] == "tp"
    assert report["validation_errors"][0]["status"] == "VALIDATION_INTERNAL_ERROR"

    reloaded = LiveTradesService.get_trade_by_id(trade.id)
    assert reloaded is not None
    assert reloaded.status == "QUARANTINED"
    assert reloaded.get_raw_exchange()["protections"]["tp"]["status"] == "VALIDATION_INTERNAL_ERROR"
    assert len(client.exec_calls) == 0
    assert len(client.cancel_calls) == 0


@pytest.mark.asyncio
async def test_p1_c_multi_trade_first_crashes_second_anomaly_still_processed(isolated_sqlite, monkeypatch):
    """P1-C: First trade hits validation exception; second trade (BTC missing SL) is still inspected & quarantined."""
    eth_trade = _seed_open_eth_trade(order_id="OPEN_ETH_C", sl_oid="SL_ETH_C", sl_clid="BOT_SL_ETH_C")
    btc_trade = LiveTradesService.create_live_trade(
        order_id="OPEN_BTC_C",
        symbol="PF_XBTUSD",
        side="long",
        qty=0.002,
        entry=60000.0,
        stop=59000.0,
        tp=62000.0,
        leverage=10,
        broker="kraken",
        status="OPEN",
        client_oid="BOT_PF_XBTUSD_C",
        raw_exchange={
            "order_id": "OPEN_BTC_C",
            "protections": {
                "version": 1,
                "sl": {"order_id": "SL_BTC_MISSING", "client_oid": "BOT_SL_BTC_C", "status": "ATTACHED"},
            },
        },
    )
    assert btc_trade is not None

    client = RecordedFakeRestClient(
        positions=[
            {"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0},
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.002, "price": 60000.0},
        ],
        open_orders=[_valid_eth_sl_order(order_id="SL_ETH_C", cli_ord_id="BOT_SL_ETH_C")],  # BTC SL missing!
    )
    wrapper = _make_wrapper(client)

    def _boom_eth(*args, **kwargs):
        if kwargs.get("expected_symbol") == "PF_ETHUSD":
            raise RuntimeError("ETH validator crash")
        return True, None

    monkeypatch.setattr(wrapper, "_validate_recovered_protection", _boom_eth)

    report = await wrapper.reconcile_on_startup()

    assert wrapper._global_entry_lock is True
    assert report["startup_secure"] is False
    # Both ETH (validation error) AND BTC (missing SL) were processed and quarantined!
    reloaded_eth = LiveTradesService.get_trade_by_id(eth_trade.id)
    reloaded_btc = LiveTradesService.get_trade_by_id(btc_trade.id)
    assert reloaded_eth.status == "QUARANTINED"
    assert reloaded_eth.get_raw_exchange()["protections"]["sl"]["status"] == "VALIDATION_INTERNAL_ERROR"
    assert reloaded_btc.status == "QUARANTINED"
    assert reloaded_btc.get_raw_exchange()["protections"]["sl"]["status"] == "MISSING_ON_EXCHANGE"
    assert len(client.exec_calls) == 0
    assert len(client.cancel_calls) == 0


@pytest.mark.asyncio
async def test_p1_d_validation_error_stays_locked_on_subsequent_reconciliation(isolated_sqlite, monkeypatch):
    """P1-D: After validation error quarantines trade, subsequent reconciliation does NOT artificially become healthy."""
    _seed_open_eth_trade()
    client = RecordedFakeRestClient(
        positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}],
        open_orders=[_valid_eth_sl_order()],
    )
    wrapper = _make_wrapper(client)
    orig_validate = wrapper._validate_recovered_protection
    monkeypatch.setattr(wrapper, "_validate_recovered_protection", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash")))

    rep1 = await wrapper.reconcile_on_startup()
    assert rep1["startup_secure"] is False
    assert wrapper._global_entry_lock is True

    # Even if validator recovers, the persisted QUARANTINED trade keeps lock active until explicitly resolved
    monkeypatch.setattr(wrapper, "_validate_recovered_protection", orig_validate)
    rep2 = await wrapper.reconcile_on_startup()
    assert rep2["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_ETHUSD" in rep2["quarantined_symbols"]
    assert len(client.exec_calls) == 0


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1.6 / STEP 2B.1 — MANDATORY P2 TESTS (A, B, C, D, E, F, G)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_p2_a_residual_sl_cancel_succeeds_cleanly_no_spurious_lock(isolated_sqlite):
    """P2-A: TP filled, SL cancel succeeds -> 1 cancel call, CLOSED, CANCELLED in report, lock released."""
    trade = _seed_open_eth_trade(
        order_id="OPEN_P2A", sl_oid="SL_P2A", sl_clid="BOT_SL_P2A", tp_oid="TP_P2A", tp_clid="BOT_TP_P2A"
    )
    client = RecordedFakeRestClient(
        positions=[],
        open_orders=[_valid_eth_sl_order(order_id="SL_P2A", cli_ord_id="BOT_SL_P2A")],
        fills=[{"fill_id": "F_P2A", "order_id": "TP_P2A", "cliOrdId": "BOT_TP_P2A", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
    )
    wrapper = _make_wrapper(client)

    report = await wrapper.reconcile_on_startup()

    assert report["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert len(client.cancel_calls) == 1
    assert client.cancel_calls[0]["order_id"] == "SL_P2A"
    assert report["residual_sl_cleanups"][0]["status"] == "CANCELLED"
    assert LiveTradesService.get_trade_by_id(trade.id).status == "CLOSED"
    assert LiveTradesService.get_quarantined_trades(broker="kraken") == []
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_p2_b_residual_sl_cancel_http_503_persists_anomaly_and_locks(isolated_sqlite):
    """P2-B: TP filled + cancel_order 503 -> trade CLOSED, residual_sl_cleanup persisted, CANCEL_FAILED, locked."""
    trade = _seed_open_eth_trade(
        order_id="OPEN_P2B", sl_oid="SL_P2B", sl_clid="BOT_SL_P2B", tp_oid="TP_P2B", tp_clid="BOT_TP_P2B"
    )
    client = RecordedFakeRestClient(
        positions=[],
        open_orders=[_valid_eth_sl_order(order_id="SL_P2B", cli_ord_id="BOT_SL_P2B")],
        fills=[{"fill_id": "F_P2B", "order_id": "TP_P2B", "cliOrdId": "BOT_TP_P2B", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
        cancel_exc=RuntimeError("HTTP 503 Service Unavailable"),
    )
    wrapper = _make_wrapper(client)

    report = await wrapper.reconcile_on_startup()

    assert LiveTradesService.get_trade_by_id(trade.id).status == "CLOSED"
    assert report["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols
    assert report["residual_sl_cleanups"][0]["status"] == "CANCEL_FAILED"
    assert report["residual_sl_cleanups"][0]["sl_order_id"] == "SL_P2B"

    q_list = LiveTradesService.get_quarantined_trades(broker="kraken")
    assert len(q_list) == 1
    assert q_list[0].order_id == "SL_P2B"
    assert q_list[0].symbol == "PF_ETHUSD"
    assert q_list[0].get_raw_exchange()["anomaly_type"] == "residual_sl_cleanup"
    assert q_list[0].get_raw_exchange()["cleanup_status"] == "CANCEL_FAILED"
    assert len(client.cancel_calls) == 1
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_p2_c_residual_sl_cancel_timeout_marks_unknown_and_locks(isolated_sqlite):
    """P2-C: Timeout on cancel_order -> CANCEL_UNKNOWN_TIMEOUT (never fake cancelled), anomaly persisted, locked."""
    trade = _seed_open_eth_trade(
        order_id="OPEN_P2C", sl_oid="SL_P2C", sl_clid="BOT_SL_P2C", tp_oid="TP_P2C", tp_clid="BOT_TP_P2C"
    )
    client = RecordedFakeRestClient(
        positions=[],
        open_orders=[_valid_eth_sl_order(order_id="SL_P2C", cli_ord_id="BOT_SL_P2C")],
        fills=[{"fill_id": "F_P2C", "order_id": "TP_P2C", "cliOrdId": "BOT_TP_P2C", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
        cancel_exc=asyncio.TimeoutError("Cancel request timed out"),
    )
    wrapper = _make_wrapper(client)

    report = await wrapper.reconcile_on_startup()

    assert LiveTradesService.get_trade_by_id(trade.id).status == "CLOSED"
    assert report["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert report["residual_sl_cleanups"][0]["status"] == "CANCEL_UNKNOWN_TIMEOUT"

    q_list = LiveTradesService.get_quarantined_trades(broker="kraken")
    assert len(q_list) == 1
    assert q_list[0].get_raw_exchange()["cleanup_status"] == "CANCEL_UNKNOWN_TIMEOUT"
    assert len(client.cancel_calls) == 1
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_p2_d_residual_sl_already_absent_no_redundant_cancel(isolated_sqlite):
    """P2-D: Pending residual_sl_cleanup anomaly when order is already absent on Kraken -> 0 cancel calls, resolved."""
    LiveTradesService.record_exchange_quarantine(
        symbol="PF_ETHUSD",
        order_id="SL_P2D",
        anomaly_type="residual_sl_cleanup",
        client_oid="BOT_SL_P2D",
        reason="CANCEL_UNKNOWN_TIMEOUT: previous timeout",
        broker="kraken",
        raw_exchange={"parent_trade_order_id": "OPEN_P2D", "sl_order_id": "SL_P2D", "cleanup_status": "CANCEL_UNKNOWN_TIMEOUT"},
    )
    client = RecordedFakeRestClient(positions=[], open_orders=[])  # Already absent!
    wrapper = _make_wrapper(client)

    report = await wrapper.reconcile_on_startup()

    assert len(client.cancel_calls) == 0
    assert report["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert report["residual_sl_cleanups"][0]["status"] == "CONFIRMED_ABSENT"
    assert LiveTradesService.get_quarantined_trades(broker="kraken") == []
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_p2_e_subsequent_reconciliation_retries_and_clears_after_confirmed_cancel(isolated_sqlite):
    """P2-E: 1st reconcile fails cancel (503) -> locked; 2nd reconcile retries cancel & succeeds -> anomaly cleared."""
    _seed_open_eth_trade(
        order_id="OPEN_P2E", sl_oid="SL_P2E", sl_clid="BOT_SL_P2E", tp_oid="TP_P2E", tp_clid="BOT_TP_P2E"
    )
    client = RecordedFakeRestClient(
        positions=[],
        open_orders=[_valid_eth_sl_order(order_id="SL_P2E", cli_ord_id="BOT_SL_P2E")],
        fills=[{"fill_id": "F_P2E", "order_id": "TP_P2E", "cliOrdId": "BOT_TP_P2E", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
        cancel_exc=RuntimeError("HTTP 503 transient"),
    )
    wrapper = _make_wrapper(client)

    # Run 1: cancel fails
    rep1 = await wrapper.reconcile_on_startup()
    assert rep1["startup_secure"] is False
    assert wrapper._global_entry_lock is True
    assert len(client.cancel_calls) == 1
    assert len(LiveTradesService.get_quarantined_trades(broker="kraken")) == 1

    # Run 2: Kraken recovers -> retry succeeds
    client.cancel_exc = None
    rep2 = await wrapper.reconcile_on_startup()
    assert rep2["startup_secure"] is True
    assert wrapper._global_entry_lock is False
    assert len(client.cancel_calls) == 2
    assert client.cancel_calls[1]["order_id"] == "SL_P2E"
    assert rep2["residual_sl_cleanups"][0]["status"] == "CANCELLED_ON_RETRY"
    assert LiveTradesService.get_quarantined_trades(broker="kraken") == []
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_p2_f_isolation_eth_residual_sl_never_cancels_btc_order(isolated_sqlite):
    """P2-F: ETH residual_sl_cleanup retry NEVER cancels an unrelated BTC order."""
    LiveTradesService.record_exchange_quarantine(
        symbol="PF_ETHUSD",
        order_id="SL_ETH_ISO",
        anomaly_type="residual_sl_cleanup",
        client_oid="BOT_SL_ETH_ISO",
        reason="CANCEL_FAILED: previous error",
        broker="kraken",
        raw_exchange={"parent_trade_order_id": "OPEN_ETH_ISO", "sl_order_id": "SL_ETH_ISO", "cleanup_status": "CANCEL_FAILED"},
    )
    btc_order = {
        "order_id": "SL_BTC_UNRELATED",
        "cliOrdId": "BOT_SL_BTC_UNRELATED",
        "symbol": "PF_XBTUSD",
        "side": "sell",
        "size": 0.002,
        "quantity": 0.002,
        "stopPrice": 59000.0,
        "orderType": "stp",
        "reduceOnly": True,
        "status": "untouched",
    }
    client = RecordedFakeRestClient(
        positions=[],
        open_orders=[
            _valid_eth_sl_order(order_id="SL_ETH_ISO", cli_ord_id="BOT_SL_ETH_ISO"),
            btc_order,
        ],
    )
    wrapper = _make_wrapper(client)

    await wrapper.reconcile_on_startup()

    assert len(client.cancel_calls) == 1
    assert client.cancel_calls[0]["order_id"] == "SL_ETH_ISO"
    assert any(o["order_id"] == "SL_BTC_UNRELATED" for o in client.open_orders)
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_p2_g_idempotence_three_successive_reconciliations_no_duplicates(isolated_sqlite):
    """P2-G: 3 successive reconciliations while cancel keeps failing -> single anomaly row (0 duplicates), 1 attempt/run."""
    _seed_open_eth_trade(
        order_id="OPEN_P2G", sl_oid="SL_P2G", sl_clid="BOT_SL_P2G", tp_oid="TP_P2G", tp_clid="BOT_TP_P2G"
    )
    client = RecordedFakeRestClient(
        positions=[],
        open_orders=[_valid_eth_sl_order(order_id="SL_P2G", cli_ord_id="BOT_SL_P2G")],
        fills=[{"fill_id": "F_P2G", "order_id": "TP_P2G", "cliOrdId": "BOT_TP_P2G", "symbol": "PF_ETHUSD", "side": "sell", "size": 0.05, "price": 3120.0, "fillTime": "2026-11-21T18:00:00Z"}],
        cancel_exc=RuntimeError("Persistent HTTP 503"),
    )
    wrapper = _make_wrapper(client)

    for run_idx in range(1, 4):
        rep = await wrapper.reconcile_on_startup()
        assert rep["startup_secure"] is False
        assert wrapper._global_entry_lock is True
        q_rows = LiveTradesService.get_quarantined_trades(broker="kraken")
        assert len(q_rows) == 1, f"Run {run_idx}: expected exactly 1 anomaly row, got {len(q_rows)}"
        assert q_rows[0].order_id == "SL_P2G"
        assert len(client.cancel_calls) == run_idx

    assert len(client.exec_calls) == 0
