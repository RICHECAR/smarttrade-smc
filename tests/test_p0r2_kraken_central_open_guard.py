"""
P0-R2 Isolated Test Suite — Central Kraken Futures OPEN Authorization,
Strict Intent/reduce_only Coherence, Mandatory SL & Exchange Confirmation.

Strict isolation guarantees:
- External network hard-blocked (socket guard).
- Zero SQLite / production DB access (LiveTradesService mocked).
- Zero .env read, zero FastAPI / bot startup.
"""

import asyncio
import math
import os
import socket
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
        raise _ExternalNetworkBlocked(f"External network blocked in P0-R2 tests: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in P0-R2 tests: {args}")


socket.socket.connect = _guarded_connect
socket.create_connection = _blocked_external
socket.getaddrinfo = _blocked_external

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SMARTTRADE_TEST_MODE"] = "1"
os.environ.setdefault("KRAKEN_RETRY_ON_ORDER_REJECT", "1")
os.environ.setdefault("KRAKEN_POS_RESOLVE_ATTEMPTS", "1")
os.environ.setdefault("KRAKEN_POS_RESOLVE_DELAY", "0.0")

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)


class FakeKrakenRestClient:
    """Deterministic in-memory Kraken Futures REST client (zero network)."""

    def __init__(self):
        self.sent_orders: List[Dict[str, Any]] = []
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.open_orders: List[Dict[str, Any]] = []
        self.fills: List[Dict[str, Any]] = []
        self._order_seq = 0

        # Fault-injection knobs
        self.sl_behavior = "confirm"  # confirm | ack_no_book | ack_no_ids | reject | timeout | transport_err | wrong_size | wrong_symbol | wrong_side
        self.emergency_close_behavior = "confirm_flat"  # confirm_flat | ack_still_open | reject

    async def get_positions(self, **kwargs) -> List[Dict[str, Any]]:
        return list(self.positions.values())

    async def get_open_orders(self, symbol: Optional[str] = None, **kwargs) -> List[Dict[str, Any]]:
        if symbol:
            return [o for o in self.open_orders if o.get("symbol", "").upper() == symbol.upper()]
        return list(self.open_orders)

    async def get_fills(self, last_fill_time: Optional[str] = None, **kwargs) -> List[Dict[str, Any]]:
        return list(self.fills)

    async def cancel_order(self, order_id: Optional[str] = None, cli_ord_id: Optional[str] = None, **kwargs):
        self.open_orders = [
            o for o in self.open_orders
            if o.get("order_id") != order_id and o.get("cliOrdId") != cli_ord_id
        ]
        return {"success": True, "code": "00000"}

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        size: float,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        return await self.place_order(
            symbol=symbol,
            side=side,
            order_type="mkt",
            size=size,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            **kwargs,
        )

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        size: float,
        limit_price: float,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        return await self.place_order(
            symbol=symbol,
            side=side,
            order_type="lmt",
            size=size,
            limit_price=limit_price,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            **kwargs,
        )

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
        self._order_seq += 1
        oid = f"FAKE-OID-{self._order_seq}"
        rec = {
            "order_id": oid,
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "size": float(size),
            "stop_price": stop_price,
            "reduce_only": reduce_only,
            "client_order_id": client_order_id,
        }
        self.sent_orders.append(rec)

        # 1. Stop-loss order path
        if order_type == "stp":
            if self.sl_behavior == "reject":
                return {"success": False, "code": "REJECTED", "error": "Exchange rejected SL"}
            if self.sl_behavior == "timeout":
                raise asyncio.TimeoutError("Simulated SL timeout")
            if self.sl_behavior == "transport_err":
                raise ConnectionError("Simulated SL transport failure")
            if self.sl_behavior == "ack_no_ids":
                return {"success": True, "code": "00000"}  # Missing order_id & client_order_id!
            if self.sl_behavior == "ack_no_book":
                # HTTP 200 ack, but never appears in open_orders!
                return {"success": True, "code": "00000", "order_id": oid}

            book_sym = symbol
            book_side = side
            book_size = float(size)
            if self.sl_behavior == "wrong_size":
                book_size = float(size) * 0.5
            elif self.sl_behavior == "wrong_symbol":
                book_sym = "PF_ETHUSD"
            elif self.sl_behavior == "wrong_side":
                book_side = "buy" if side == "sell" else "sell"

            self.open_orders.append({
                "order_id": oid,
                "cliOrdId": client_order_id,
                "symbol": book_sym,
                "side": book_side,
                "size": book_size,
                "stopPrice": stop_price,
                "orderType": "stp",
                "reduceOnly": True,
                "status": "untouched",
            })
            return {"success": True, "code": "00000", "order_id": oid}

        # 2. Take-profit order path
        if order_type == "take_profit":
            self.open_orders.append({
                "order_id": oid,
                "cliOrdId": client_order_id,
                "symbol": symbol,
                "side": side,
                "size": float(size),
                "stopPrice": stop_price,
                "orderType": "take_profit",
                "reduceOnly": True,
                "status": "untouched",
            })
            return {"success": True, "code": "00000", "order_id": oid}

        # 3. Emergency close path
        if client_order_id and str(client_order_id).startswith("BOT_EMERGENCY_"):
            if self.emergency_close_behavior == "reject":
                return {"success": False, "code": "EM_REJECT", "error": "Emergency close rejected"}
            if self.emergency_close_behavior == "ack_still_open":
                return {"success": True, "code": "00000", "order_id": oid}
            # confirm_flat
            self.positions.pop(symbol, None)
            return {"success": True, "code": "00000", "order_id": oid}

        # 4. Reduce / Close path
        if reduce_only:
            pos = self.positions.get(symbol)
            if pos:
                rem = max(0.0, float(pos["size"]) - float(size))
                if rem <= 1e-8:
                    self.positions.pop(symbol, None)
                else:
                    pos["size"] = rem
            self.fills.append({
                "fill_id": f"FILL-{self._order_seq}",
                "order_id": oid,
                "cliOrdId": client_order_id,
                "symbol": symbol,
                "side": side,
                "size": float(size),
                "price": 60000.0,
                "fillTime": "2026-11-21T16:00:00Z",
            })
            return {"success": True, "code": "00000", "order_id": oid, "filled_size": float(size), "price": 60000.0}

        # 5. Market/Limit OPEN fill
        pos_side = "long" if side == "buy" else "short"
        fill_price = float(limit_price or 60000.0)
        self.positions[symbol] = {
            "symbol": symbol,
            "side": pos_side,
            "size": float(size),
            "price": fill_price,
        }
        self.fills.append({
            "fill_id": f"FILL-{self._order_seq}",
            "order_id": oid,
            "cliOrdId": client_order_id,
            "symbol": symbol,
            "side": side,
            "size": float(size),
            "price": fill_price,
            "fillTime": "2026-11-21T16:00:00Z",
        })
        return {
            "success": True,
            "code": "00000",
            "order_id": oid,
            "filled_size": float(size),
            "price": fill_price,
        }


def _make_wrapper(fake_client: FakeKrakenRestClient) -> KrakenFuturesExecutorWrapper:
    w = KrakenFuturesExecutorWrapper(executor=fake_client)
    w._rest_client = fake_client
    w._global_entry_lock = False
    w._startup_reconciliation_completed = True
    return w


def _mock_live_trades_service():
    """Patch LiveTradesService so zero SQLite DB calls ever occur."""
    mock_svc = MagicMock()
    mock_svc.create_live_trade.return_value = MagicMock(id="mock-trade-uuid")
    mock_svc.close_live_trade.return_value = MagicMock(id="mock-trade-uuid", status="CLOSED")
    mock_svc.get_trade_by_id.return_value = MagicMock(id="mock-trade-uuid", status="CLOSED")
    mock_svc.record_protection_intent.return_value = True
    mock_svc.update_protection_status.return_value = True
    mock_svc.mark_trade_quarantined.return_value = True
    mock_svc.update_trade_after_partial_close.return_value = True
    mock_svc.close_trade_by_symbol.return_value = True
    mock_svc.get_open_trades.return_value = []
    return patch("backend.services.live_trades_service.LiveTradesService", mock_svc)


# ══════════════════════════════════════════════════════════════════════════════
# 1. AUTHORIZATION TESTS (Réserve A)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("env_val", [None, "", "   ", "false", "FALSE", "0", "no", "invalid", "maybe", "2", "on"])
def test_open_blocked_when_kraken_execution_not_explicitly_true(monkeypatch, env_val):
    if env_val is None:
        monkeypatch.delenv("KRAKEN_EXECUTION_ENABLED", raising=False)
    else:
        monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", env_val)

    fake = FakeKrakenRestClient()
    w = _make_wrapper(fake)

    with _mock_live_trades_service():
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=0.01,
            order_type="market",
            entry_price=60000.0,
            stop_loss=58000.0,
            reduce_only=False,
            intent="open",
        ))

    assert res["success"] is False
    assert res["code"] == "KRAKEN_EXECUTION_DISABLED"
    assert len(fake.sent_orders) == 0


def test_direct_rest_open_blocked_when_kraken_execution_disabled(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    fake = FakeKrakenRestClient()
    w = _make_wrapper(fake)

    res = asyncio.run(w._place_order_rest(
        symbol="PF_XBTUSD",
        side="buy",
        quantity=0.01,
        order_type="market",
        reduce_only=False,
    ))
    assert res["success"] is False
    assert res["code"] == "KRAKEN_EXECUTION_DISABLED"
    assert len(fake.sent_orders) == 0


def test_open_allowed_when_flag_true_and_sl_confirmed(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    fake = FakeKrakenRestClient()
    w = _make_wrapper(fake)

    with _mock_live_trades_service():
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=0.01,
            order_type="market",
            entry_price=60000.0,
            stop_loss=58000.0,
            reduce_only=False,
            intent="open",
        ))

    assert res["success"] is True
    assert res["sl_attached"] is True
    assert res["sl_verified"] is True
    assert any(o["order_type"] == "stp" for o in fake.sent_orders)


# ══════════════════════════════════════════════════════════════════════════════
# 2. STRICT INTENT / REDUCE_ONLY COHERENCE TESTS (Réserve C)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("intent,reduce_only,expected_code", [
    (None, False, "INVALID_ORDER_INTENT"),
    ("", False, "INVALID_ORDER_INTENT"),
    ("   ", True, "INVALID_ORDER_INTENT"),
    ("unknown", False, "INVALID_ORDER_INTENT"),
    ("flip", True, "INVALID_ORDER_INTENT"),
    ("open", True, "CONTRADICTORY_INTENT_REDUCE_ONLY"),
    ("OPEN", True, "CONTRADICTORY_INTENT_REDUCE_ONLY"),
    ("reduce", False, "CONTRADICTORY_INTENT_REDUCE_ONLY"),
    ("REDUCE", False, "CONTRADICTORY_INTENT_REDUCE_ONLY"),
    ("close", False, "CONTRADICTORY_INTENT_REDUCE_ONLY"),
    ("CLOSE", False, "CONTRADICTORY_INTENT_REDUCE_ONLY"),
    ("open", "false", "CONTRADICTORY_INTENT_REDUCE_ONLY"),
    ("close", 1, "CONTRADICTORY_INTENT_REDUCE_ONLY"),
])
def test_intent_reduce_only_matrix_rejects_invalid_and_contradictory(monkeypatch, intent, reduce_only, expected_code):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    fake = FakeKrakenRestClient()
    w = _make_wrapper(fake)

    with _mock_live_trades_service():
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=0.01,
            order_type="market",
            entry_price=60000.0,
            stop_loss=58000.0,
            reduce_only=reduce_only,
            intent=intent,
        ))

    assert res["success"] is False
    assert res["code"] == expected_code
    assert len(fake.sent_orders) == 0


def test_place_order_market_without_intent_fails_closed(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    fake = FakeKrakenRestClient()
    w = _make_wrapper(fake)

    res = asyncio.run(w.place_order_market("PF_XBTUSD", "sell", "0.01", reduce_only=False))
    assert res["success"] is False
    assert res["code"] == "INVALID_ORDER_INTENT"
    assert len(fake.sent_orders) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 3. MANDATORY STOP LOSS PRE-FLIGHT VALIDATION (Réserve B)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad_sl,side,entry,expected_code", [
    (None, "buy", 60000.0, "INVALID_STOP_LOSS"),
    (0, "buy", 60000.0, "INVALID_STOP_LOSS"),
    (0.0, "buy", 60000.0, "INVALID_STOP_LOSS"),
    (-100.0, "buy", 60000.0, "INVALID_STOP_LOSS"),
    ("not_a_number", "buy", 60000.0, "INVALID_STOP_LOSS"),
    (True, "buy", 60000.0, "INVALID_STOP_LOSS"),
    (float("nan"), "buy", 60000.0, "INVALID_STOP_LOSS"),
    (float("inf"), "buy", 60000.0, "INVALID_STOP_LOSS"),
    (float("-inf"), "buy", 60000.0, "INVALID_STOP_LOSS"),
    (60000.0, "buy", 60000.0, "INVALID_STOP_LOSS_DIRECTION"),
    (62000.0, "buy", 60000.0, "INVALID_STOP_LOSS_DIRECTION"),
    (60000.0, "sell", 60000.0, "INVALID_STOP_LOSS_DIRECTION"),
    (58000.0, "sell", 60000.0, "INVALID_STOP_LOSS_DIRECTION"),
])
def test_open_rejects_missing_or_invalid_stop_loss_before_order(monkeypatch, bad_sl, side, entry, expected_code):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    fake = FakeKrakenRestClient()
    w = _make_wrapper(fake)

    with _mock_live_trades_service():
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD",
            side=side,
            quantity=0.01,
            order_type="market",
            entry_price=entry,
            stop_loss=bad_sl,
            reduce_only=False,
            intent="open",
        ))

    assert res["success"] is False
    assert res["code"] == expected_code
    assert len(fake.sent_orders) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 4. EXCHANGE-SIDE SL CONFIRMATION & POST-FILL FAILURE HANDLING
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sl_behavior", [
    "ack_no_book",
    "ack_no_ids",
    "reject",
    "timeout",
    "transport_err",
    "wrong_size",
    "wrong_symbol",
    "wrong_side",
])
def test_unconfirmed_or_failed_sl_triggers_confirmed_emergency_close_and_never_returns_success(monkeypatch, sl_behavior):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_RETRY_ON_ORDER_REJECT", "1")
    fake = FakeKrakenRestClient()
    fake.sl_behavior = sl_behavior
    fake.emergency_close_behavior = "confirm_flat"
    w = _make_wrapper(fake)

    with _mock_live_trades_service():
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=0.01,
            order_type="market",
            entry_price=60000.0,
            stop_loss=58000.0,
            reduce_only=False,
            intent="open",
        ))

    assert res["success"] is False
    assert res.get("sl_attached") is False
    assert res.get("status") == "SL_FAILED_POSITION_EMERGENCY_CLOSED"
    assert res.get("emergency_close_confirmed") is True
    # Emergency close order must have opposite side, reduce_only=True, exact filled qty
    em_orders = [o for o in fake.sent_orders if str(o.get("client_order_id", "")).startswith("BOT_EMERGENCY_")]
    assert len(em_orders) == 1
    assert em_orders[0]["side"] == "sell"
    assert em_orders[0]["reduce_only"] is True
    assert abs(em_orders[0]["size"] - 0.01) < 1e-8
    assert "PF_XBTUSD" not in fake.positions


@pytest.mark.parametrize("em_behavior", ["reject", "ack_still_open"])
def test_emergency_close_failed_or_unconfirmed_quarantines_and_locks_entries(monkeypatch, em_behavior):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_RETRY_ON_ORDER_REJECT", "1")
    fake = FakeKrakenRestClient()
    fake.sl_behavior = "ack_no_book"
    fake.emergency_close_behavior = em_behavior
    w = _make_wrapper(fake)

    with _mock_live_trades_service():
        res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=0.01,
            order_type="market",
            entry_price=60000.0,
            stop_loss=58000.0,
            reduce_only=False,
            intent="open",
        ))

    assert res["success"] is False
    assert res["status"] == "MANUAL_RECONCILIATION_REQUIRED"
    assert res.get("emergency_close_confirmed") is False
    assert "PF_XBTUSD" in w._reconciliation_required_symbols
    assert w._global_entry_lock is True


# ══════════════════════════════════════════════════════════════════════════════
# 5. NON-REGRESSION: REDUCE & CLOSE PRESERVED WHEN KRAKEN_EXECUTION_ENABLED=false
# ══════════════════════════════════════════════════════════════════════════════

def test_reduce_and_close_remain_allowed_when_kraken_execution_disabled(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    fake = FakeKrakenRestClient()
    fake.positions["PF_XBTUSD"] = {"symbol": "PF_XBTUSD", "side": "long", "size": 0.02, "price": 60000.0}
    w = _make_wrapper(fake)
    w._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
        order_id="EXIST-OID-1",
        client_oid="BOT_PF_XBTUSD_EXIST",
        symbol="PF_XBTUSD",
        side="long",
        filled_qty=0.02,
        expected_exposure=0.02,
        entry_price=60000.0,
        status="OPEN",
    )

    with _mock_live_trades_service():
        # 1. REDUCE via place_order(intent="reduce", reduce_only=True)
        red_res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD",
            side="sell",
            quantity=0.01,
            order_type="market",
            reduce_only=True,
            intent="reduce",
        ))
        assert red_res["success"] is True
        assert fake.sent_orders[-1]["reduce_only"] is True
        assert abs(w._active_trackers["PF_XBTUSD"].expected_exposure - 0.01) < 1e-8

        # 2. CLOSE via place_order(intent="close", reduce_only=True)
        cls_res = asyncio.run(w.place_order(
            symbol="PF_XBTUSD",
            side="sell",
            quantity=0.01,
            order_type="market",
            reduce_only=True,
            intent="close",
        ))
        assert cls_res["success"] is True
        assert fake.sent_orders[-1]["reduce_only"] is True
        assert w._active_trackers["PF_XBTUSD"].status == "CLOSED"
        assert "PF_XBTUSD" not in fake.positions

    # Verify ZERO non-reduce-only orders were ever sent
    assert all(o["reduce_only"] is True for o in fake.sent_orders)


def test_close_position_helper_works_when_kraken_execution_disabled(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    fake = FakeKrakenRestClient()
    fake.positions["PF_XBTUSD"] = {"symbol": "PF_XBTUSD", "side": "short", "size": 0.01, "price": 60000.0}
    w = _make_wrapper(fake)
    w._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
        order_id="EXIST-OID-2",
        client_oid="BOT_PF_XBTUSD_EXIST2",
        symbol="PF_XBTUSD",
        side="short",
        filled_qty=0.01,
        expected_exposure=0.01,
        entry_price=60000.0,
        status="OPEN",
    )

    with _mock_live_trades_service():
        res = asyncio.run(w.close_position("PF_XBTUSD", quantity=0.01, reduce_only=True, reason="safety_close"))

    assert res.get("success") is True or res.get("code") == "00000"
    assert len(fake.sent_orders) == 1
    assert fake.sent_orders[0]["side"] == "buy"
    assert fake.sent_orders[0]["reduce_only"] is True
    assert "PF_XBTUSD" not in fake.positions
