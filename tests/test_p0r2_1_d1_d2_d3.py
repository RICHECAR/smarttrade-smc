"""
P0-R2.1 Isolated Test Suite — D1 (Pre-flight Reference Price & SL),
D2 (Strict Exchange-Side STOP Confirmation), D3 (SQLite Consistency after Emergency Close).

Strict isolation guarantees:
- External network hard-blocked (socket guard).
- Zero production DB access (in-memory fake LiveTradesService repository).
- Zero .env read, zero FastAPI / bot startup.
"""

import asyncio
import math
import os
import socket
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ── Hard-block all external network calls ────────────────────────────────────
class _ExternalNetworkBlocked(RuntimeError):
    pass


_ORIG_CONNECT = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) and address else ""
    if host not in ("127.0.0.1", "::1"):
        raise _ExternalNetworkBlocked(f"External network blocked in P0-R2.1 tests: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in P0-R2.1 tests: {args}")


socket.socket.connect = _guarded_connect
socket.create_connection = _blocked_external
socket.getaddrinfo = _blocked_external

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SMARTTRADE_TEST_MODE"] = "1"
os.environ["KRAKEN_RETRY_ON_ORDER_REJECT"] = "1"
os.environ["KRAKEN_POS_RESOLVE_ATTEMPTS"] = "1"
os.environ["KRAKEN_POS_RESOLVE_DELAY"] = "0.0"

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)


@dataclass
class FakeTradeRow:
    id: str
    order_id: str
    symbol: str
    side: str
    qty: float
    status: str = "OPEN"
    close_reason: Optional[str] = None
    close_price: Optional[float] = None
    quarantine_reason: Optional[str] = None


class FakeLiveTradesRepo:
    """In-memory isolated LiveTradesService stand-in (zero production SQLite access)."""

    def __init__(self):
        self.rows: Dict[str, FakeTradeRow] = {}
        self.quarantined_calls: List[Dict[str, Any]] = []
        self.close_calls: List[Dict[str, Any]] = []

        # Fault-injection knobs for D3
        self.omit_created_id = False
        self.close_returns_none = False
        self.close_raises = False
        self.readback_returns_open = False
        self.readback_raises = False

    def create_live_trade(self, order_id: str, symbol: str, side: str, qty: float, **kwargs):
        tid = "" if self.omit_created_id else f"trade-{uuid.uuid4().hex[:8]}"
        row = FakeTradeRow(id=tid, order_id=order_id, symbol=symbol, side=side, qty=qty, status="OPEN")
        if tid:
            self.rows[tid] = row
        return row

    def close_live_trade(
        self,
        trade_id: Optional[str] = None,
        order_id: Optional[str] = None,
        close_price: Optional[float] = None,
        raw_exchange: Optional[Dict[str, Any]] = None,
        close_reason: Optional[str] = None,
        **kwargs,
    ):
        self.close_calls.append({"trade_id": trade_id, "order_id": order_id, "close_reason": close_reason})
        if self.close_raises:
            raise RuntimeError("Simulated SQLite write error on close_live_trade")
        if self.close_returns_none:
            return None
        if not trade_id or trade_id not in self.rows:
            return None
        row = self.rows[trade_id]
        if not self.readback_returns_open:
            row.status = "CLOSED"
        row.close_price = close_price
        row.close_reason = close_reason
        return row

    def get_trade_by_id(self, trade_id: str):
        if self.readback_raises:
            raise RuntimeError("Simulated SQLite read-back error on get_trade_by_id")
        return self.rows.get(trade_id)

    def mark_trade_quarantined(self, symbol: str, order_id: str, reason: str = ""):
        self.quarantined_calls.append({"symbol": symbol, "order_id": order_id, "reason": reason})
        for r in self.rows.values():
            if r.order_id == order_id:
                r.status = "QUARANTINED"
                r.quarantine_reason = reason
        return True

    def record_protection_intent(self, **kwargs):
        return True

    def update_protection_status(self, **kwargs):
        return True

    def update_trade_after_partial_close(self, **kwargs):
        return True

    def close_trade_by_symbol(self, **kwargs):
        return True

    def get_open_trades(self, **kwargs):
        return [r for r in self.rows.values() if r.status == "OPEN"]

    def get_trade_by_order_id(self, order_id: str, **kwargs):
        for r in self.rows.values():
            if r.order_id == order_id:
                return r
        return None

    def update_trade_quantity(self, order_id: str, remaining_qty: float, **kwargs):
        for r in self.rows.values():
            if r.order_id == order_id:
                r.qty = remaining_qty
                return r
        return MagicMock(id="updated-partial", qty=remaining_qty)


class FakeKrakenClientR21:
    """Deterministic in-memory Kraken client with D1/D2/D3 fault knobs."""

    def __init__(self, ticker_price: Optional[float] = None):
        self.sent_orders: List[Dict[str, Any]] = []
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.open_orders: List[Dict[str, Any]] = []
        self.fills: List[Dict[str, Any]] = []
        self._seq = 0
        self.ticker_price = ticker_price
        self.fill_ratio = 1.0  # < 1.0 simulates partial fill
        self.sl_book_mutator = None  # callable(order_dict) -> list of open_orders dicts
        self.open_orders_error = None  # Exception to raise in get_open_orders
        self.emergency_close_behavior = "confirm_flat"  # confirm_flat | ack_still_open | reject

    async def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        if self.ticker_price is None:
            return None
        return {"symbol": symbol, "markPrice": self.ticker_price, "last": self.ticker_price}

    async def get_positions(self, **kwargs) -> List[Dict[str, Any]]:
        return list(self.positions.values())

    async def get_open_orders(self, symbol: Optional[str] = None, **kwargs) -> List[Dict[str, Any]]:
        if self.open_orders_error is not None and any(o["order_type"] == "stp" for o in self.sent_orders):
            raise self.open_orders_error
        return list(self.open_orders)

    async def get_fills(self, last_fill_time: Optional[str] = None, **kwargs) -> List[Dict[str, Any]]:
        return list(self.fills)

    async def cancel_order(self, order_id: Optional[str] = None, cli_ord_id: Optional[str] = None, **kwargs):
        self.open_orders = [
            o for o in self.open_orders
            if o.get("order_id") != order_id and o.get("cliOrdId") != cli_ord_id
        ]
        return {"success": True, "code": "00000"}

    async def place_market_order(self, symbol: str, side: str, size: float, reduce_only: bool = False, client_order_id: Optional[str] = None, **kwargs):
        return await self.place_order(symbol=symbol, side=side, order_type="mkt", size=size, reduce_only=reduce_only, client_order_id=client_order_id, **kwargs)

    async def place_limit_order(self, symbol: str, side: str, size: float, price: Optional[float] = None, limit_price: Optional[float] = None, reduce_only: bool = False, client_order_id: Optional[str] = None, **kwargs):
        px = limit_price if limit_price is not None else price
        return await self.place_order(symbol=symbol, side=side, order_type="lmt", size=size, limit_price=px, reduce_only=reduce_only, client_order_id=client_order_id, **kwargs)

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: float,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        reduce_only: bool = False,
        trigger_signal: Optional[str] = None,
        client_order_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        self._seq += 1
        oid = f"R21-OID-{self._seq}"
        self.sent_orders.append({
            "order_id": oid,
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "size": float(size),
            "stop_price": stop_price,
            "reduce_only": reduce_only,
            "client_order_id": client_order_id,
        })

        if order_type == "stp":
            base = {
                "order_id": oid,
                "cliOrdId": client_order_id,
                "symbol": symbol,
                "side": side,
                "size": float(size),
                "stopPrice": stop_price,
                "orderType": "stp",
                "reduceOnly": True,
                "status": "untouched",
            }
            if self.sl_book_mutator is not None:
                mutated = self.sl_book_mutator(dict(base))
                if isinstance(mutated, list):
                    self.open_orders.extend(mutated)
                elif isinstance(mutated, dict):
                    self.open_orders.append(mutated)
            else:
                self.open_orders.append(base)
            return {"success": True, "code": "00000", "order_id": oid}

        if client_order_id and str(client_order_id).startswith("BOT_EMERGENCY_"):
            if self.emergency_close_behavior == "reject":
                return {"success": False, "code": "EM_REJECT", "error": "Emergency close rejected"}
            if self.emergency_close_behavior == "ack_still_open":
                return {"success": True, "code": "00000", "order_id": oid, "price": 60000.0}
            self.positions.pop(symbol, None)
            return {"success": True, "code": "00000", "order_id": oid, "price": 59950.0}

        if reduce_only:
            pos = self.positions.get(symbol)
            if pos:
                rem = max(0.0, float(pos["size"]) - float(size))
                if rem <= 1e-8:
                    self.positions.pop(symbol, None)
                else:
                    pos["size"] = rem
            self.fills.append({
                "fill_id": f"FILL-{self._seq}",
                "order_id": oid,
                "cliOrdId": client_order_id,
                "symbol": symbol,
                "side": side,
                "size": float(size),
                "price": 60000.0,
                "fillTime": "2026-11-21T16:00:00Z",
            })
            return {"success": True, "code": "00000", "order_id": oid, "filled_size": float(size), "price": 60000.0}

        filled = round(float(size) * self.fill_ratio, 6)
        fill_price = float(limit_price or self.ticker_price or 60000.0)
        pos_side = "long" if side == "buy" else "short"
        self.positions[symbol] = {"symbol": symbol, "side": pos_side, "size": filled, "price": fill_price}
        self.fills.append({
            "fill_id": f"FILL-{self._seq}",
            "order_id": oid,
            "cliOrdId": client_order_id,
            "symbol": symbol,
            "side": side,
            "size": filled,
            "price": fill_price,
            "fillTime": "2026-11-21T16:00:00Z",
        })
        return {"success": True, "code": "00000", "order_id": oid, "filled_size": filled, "price": fill_price}


def _make_wrapper(client: FakeKrakenClientR21) -> KrakenFuturesExecutorWrapper:
    w = KrakenFuturesExecutorWrapper(executor=client)
    w._rest_client = client
    w._global_entry_lock = False
    w._startup_reconciliation_completed = True
    return w


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — D1 TESTS (PRE-FLIGHT REFERENCE PRICE & SL DIRECTION)
# ══════════════════════════════════════════════════════════════════════════════

def test_d1_market_long_with_reliable_entry_price_and_valid_sl_allowed(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenClientR21()
    repo = FakeLiveTradesRepo()
    w = _make_wrapper(client)
    with patch("backend.services.live_trades_service.LiveTradesService", repo):
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
            entry_price=60000.0, stop_loss=58000.0, reduce_only=False, intent="open",
        ))
    assert res["success"] is True
    assert res["sl_verified"] is True


def test_d1_market_long_via_ticker_reference_price_allowed(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenClientR21(ticker_price=60000.0)
    repo = FakeLiveTradesRepo()
    w = _make_wrapper(client)
    with patch("backend.services.live_trades_service.LiveTradesService", repo):
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
            entry_price=None, stop_loss=58000.0, reduce_only=False, intent="open",
        ))
    assert res["success"] is True
    assert res["sl_verified"] is True


def test_d1_market_long_with_inverted_sl_via_ticker_refused_before_send(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenClientR21(ticker_price=60000.0)
    w = _make_wrapper(client)
    res = asyncio.run(w.place_order(
        symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=None, stop_loss=62000.0, reduce_only=False, intent="open",
    ))
    assert res["success"] is False
    assert res["code"] == "INVALID_STOP_LOSS_DIRECTION"
    assert len(client.sent_orders) == 0


def test_d1_market_short_with_valid_sl_above_allowed(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenClientR21()
    repo = FakeLiveTradesRepo()
    w = _make_wrapper(client)
    with patch("backend.services.live_trades_service.LiveTradesService", repo):
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD", side="sell", quantity=0.01, order_type="market",
            entry_price=60000.0, stop_loss=62000.0, reduce_only=False, intent="open",
        ))
    assert res["success"] is True
    assert res["sl_verified"] is True


def test_d1_market_short_with_sl_below_refused_before_send(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenClientR21()
    w = _make_wrapper(client)
    res = asyncio.run(w.place_order(
        symbol="PF_XBTUSD", side="sell", quantity=0.01, order_type="market",
        entry_price=60000.0, stop_loss=58000.0, reduce_only=False, intent="open",
    ))
    assert res["success"] is False
    assert res["code"] == "INVALID_STOP_LOSS_DIRECTION"
    assert len(client.sent_orders) == 0


@pytest.mark.parametrize("bad_ref,expected_code", [
    (None, "MISSING_REFERENCE_PRICE"),
    (0, "INVALID_REFERENCE_PRICE"),
    (0.0, "INVALID_REFERENCE_PRICE"),
    (-50.0, "INVALID_REFERENCE_PRICE"),
    (float("nan"), "INVALID_REFERENCE_PRICE"),
    (float("inf"), "INVALID_REFERENCE_PRICE"),
    (float("-inf"), "INVALID_REFERENCE_PRICE"),
    ("bad_price", "INVALID_REFERENCE_PRICE"),
])
def test_d1_market_without_valid_reference_price_refused_before_send(monkeypatch, bad_ref, expected_code):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenClientR21(ticker_price=None)  # No ticker available
    w = _make_wrapper(client)
    res = asyncio.run(w.place_order(
        symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=bad_ref, stop_loss=58000.0, reduce_only=False, intent="open",
    ))
    assert res["success"] is False
    assert res["code"] == expected_code
    assert len(client.sent_orders) == 0


def test_d1_limit_order_validates_sl_against_limit_price(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenClientR21()
    w = _make_wrapper(client)
    # Inverted SL for LONG limit at 60000
    bad = asyncio.run(w.place_order(
        symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="limit",
        price=60000.0, stop_loss=61000.0, reduce_only=False, intent="open",
    ))
    assert bad["success"] is False
    assert bad["code"] == "INVALID_STOP_LOSS_DIRECTION"
    assert len(client.sent_orders) == 0

    # Valid SL for LONG limit at 60000
    repo = FakeLiveTradesRepo()
    with patch("backend.services.live_trades_service.LiveTradesService", repo):
        good = asyncio.run(w.place_order(
            symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="limit",
            price=60000.0, stop_loss=58000.0, reduce_only=False, intent="open",
        ))
    assert good["success"] is True
    assert good["sl_verified"] is True


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — D2 TESTS (STRICT EXCHANGE-SIDE STOP ORDER CONFIRMATION)
# ══════════════════════════════════════════════════════════════════════════════

def _mutate_del(key):
    def _fn(o):
        o.pop(key, None)
        return o
    return _fn


def _mutate_set(key, val):
    def _fn(o):
        o[key] = val
        return o
    return _fn


def _mutate_ambiguous(o):
    o2 = dict(o)
    return [o, o2]


@pytest.mark.parametrize("case_name,mutator", [
    ("missing_reduceOnly", _mutate_del("reduceOnly")),
    ("reduceOnly_false", _mutate_set("reduceOnly", False)),
    ("wrong_orderType_lmt", _mutate_set("orderType", "lmt")),
    ("missing_orderType", _mutate_del("orderType")),
    ("wrong_symbol", _mutate_set("symbol", "PF_ETHUSD")),
    ("missing_symbol", _mutate_del("symbol")),
    ("wrong_side", _mutate_set("side", "buy")),
    ("missing_side", _mutate_del("side")),
    ("wrong_size", _mutate_set("size", 0.005)),
    ("missing_size", _mutate_del("size")),
    ("wrong_stopPrice_half_tick", _mutate_set("stopPrice", 58000.5)),
    ("missing_stopPrice", _mutate_del("stopPrice")),
    ("missing_identifiers", lambda o: {k: v for k, v in o.items() if k not in ("order_id", "cliOrdId")}),
    ("status_cancelled", _mutate_set("status", "cancelled")),
    ("status_rejected", _mutate_set("status", "rejected")),
    ("status_expired", _mutate_set("status", "expired")),
    ("status_triggered", _mutate_set("status", "triggered")),
    ("status_missing", _mutate_del("status")),
    ("ambiguous_two_candidates", _mutate_ambiguous),
    ("empty_open_orders", lambda o: []),
])
def test_d2_verify_protection_rejects_every_invalid_or_ambiguous_stop_order(monkeypatch, case_name, mutator):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenClientR21()
    client.sl_book_mutator = mutator
    repo = FakeLiveTradesRepo()
    w = _make_wrapper(client)

    with patch("backend.services.live_trades_service.LiveTradesService", repo):
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
            entry_price=60000.0, stop_loss=58000.0, reduce_only=False, intent="open",
        ))

    assert res["success"] is False, f"{case_name} must NOT return success=True"
    assert res.get("sl_verified") is not True, f"{case_name} must NOT set sl_verified=True"
    assert res.get("sl_attached") is False


@pytest.mark.parametrize("err_exc", [asyncio.TimeoutError("timeout"), ConnectionError("transport down")])
def test_d2_open_orders_timeout_or_transport_error_fails_confirmation(monkeypatch, err_exc):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenClientR21()
    client.open_orders_error = err_exc
    repo = FakeLiveTradesRepo()
    w = _make_wrapper(client)

    with patch("backend.services.live_trades_service.LiveTradesService", repo):
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
            entry_price=60000.0, stop_loss=58000.0, reduce_only=False, intent="open",
        ))

    assert res["success"] is False
    assert res.get("sl_attached") is False


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — D3 TESTS (SQLITE CONSISTENCY AFTER EMERGENCY CLOSE)
# ══════════════════════════════════════════════════════════════════════════════

def test_d3_emergency_close_confirmed_updates_exact_trade_id_and_verifies_readback(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenClientR21()
    client.sl_book_mutator = lambda o: []  # SL never appears in orderbook
    repo = FakeLiveTradesRepo()
    # Pre-seed another trade on the SAME symbol to prove we never close the wrong row!
    other_row = FakeTradeRow(id="trade-other-same-sym", order_id="OTHER-OID", symbol="PF_XBTUSD", side="long", qty=0.05, status="OPEN")
    repo.rows[other_row.id] = other_row
    w = _make_wrapper(client)

    with patch("backend.services.live_trades_service.LiveTradesService", repo):
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
            entry_price=60000.0, stop_loss=58000.0, reduce_only=False, intent="open",
        ))

    assert res["success"] is False
    assert res["status"] == "SL_FAILED_POSITION_EMERGENCY_CLOSED"
    assert res["emergency_close_confirmed"] is True
    assert res["db_close_verified"] is True
    tid = res["trade_id"]
    assert tid and tid != other_row.id
    assert repo.rows[tid].status == "CLOSED"
    assert repo.rows[tid].close_reason == "SL_FAILED_EMERGENCY_CLOSE"
    # Other row on the same symbol MUST remain untouched
    assert repo.rows[other_row.id].status == "OPEN"
    assert w._active_trackers["PF_XBTUSD"].status == "CLOSED"


@pytest.mark.parametrize("fault_attr", [
    "close_returns_none",
    "close_raises",
    "readback_returns_open",
    "readback_raises",
    "omit_created_id",
])
def test_d3_sqlite_close_failure_quarantines_and_never_claims_closed(monkeypatch, fault_attr):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenClientR21()
    client.sl_book_mutator = lambda o: []
    repo = FakeLiveTradesRepo()
    setattr(repo, fault_attr, True)
    w = _make_wrapper(client)

    with patch("backend.services.live_trades_service.LiveTradesService", repo):
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
            entry_price=60000.0, stop_loss=58000.0, reduce_only=False, intent="open",
        ))

    assert res["success"] is False
    assert res["status"] == "EMERGENCY_CLOSE_DB_DESYNC_QUARANTINED"
    assert res["code"] == "EMERGENCY_CLOSE_DB_SYNC_FAILED"
    assert res["db_close_verified"] is False
    # In-memory tracker must NOT mask DB inconsistency as CLOSED
    assert w._active_trackers["PF_XBTUSD"].status == "MANUAL_RECONCILIATION_REQUIRED"
    assert "PF_XBTUSD" in w._reconciliation_required_symbols
    assert w._global_entry_lock is True
    assert len(repo.quarantined_calls) == 1
    if fault_attr == "omit_created_id":
        assert len(repo.close_calls) == 0  # Must NOT attempt blind symbol close


def test_d3_partial_fill_emergency_close_uses_exact_filled_qty_and_closes_db(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenClientR21()
    client.fill_ratio = 0.5  # 0.02 requested -> 0.01 filled
    client.sl_book_mutator = lambda o: []
    repo = FakeLiveTradesRepo()
    w = _make_wrapper(client)

    with patch("backend.services.live_trades_service.LiveTradesService", repo):
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD", side="buy", quantity=0.02, order_type="market",
            entry_price=60000.0, stop_loss=58000.0, reduce_only=False, intent="open",
        ))

    assert res["success"] is False
    assert res["status"] == "SL_FAILED_POSITION_EMERGENCY_CLOSED"
    assert res["db_close_verified"] is True
    em = [o for o in client.sent_orders if str(o.get("client_order_id", "")).startswith("BOT_EMERGENCY_")]
    assert len(em) == 1
    assert abs(em[0]["size"] - 0.01) < 1e-8  # Exact partial fill qty, NOT 0.02!
    assert em[0]["reduce_only"] is True
    assert repo.rows[res["trade_id"]].status == "CLOSED"


def test_d3_non_regression_reduce_position_works_when_kraken_open_disabled(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    client = FakeKrakenClientR21()
    client.positions["PF_XBTUSD"] = {"symbol": "PF_XBTUSD", "side": "long", "size": 0.02, "price": 60000.0}
    repo = FakeLiveTradesRepo()
    w = _make_wrapper(client)
    w._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
        order_id="EXIST-OID-R21",
        client_oid="BOT_PF_XBTUSD_R21",
        symbol="PF_XBTUSD",
        side="long",
        filled_qty=0.02,
        expected_exposure=0.02,
        entry_price=60000.0,
        status="OPEN",
    )

    with patch("backend.services.live_trades_service.LiveTradesService", repo):
        res = asyncio.run(w.reduce_position("PF_XBTUSD", percent=50.0, reason="safety_reduce"))

    assert res.get("success") is True or res.get("code") == "00000"
    assert len(client.sent_orders) == 1
    assert client.sent_orders[0]["reduce_only"] is True
    assert abs(client.sent_orders[0]["size"] - 0.01) < 1e-8
