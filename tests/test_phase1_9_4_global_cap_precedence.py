"""
Phase 1.9.4 — Global Cap Precedence (`min(env_cap, kwarg_cap)`) Suite (100% Offline)
====================================================================================
Covers all 18 mandatory scenarios proving that:
  - No caller can ever raise or bypass KRAKEN_MAX_TRADE_RISK_USD via max_risk_usd.
  - Both sources are validated independently; any invalid source fails closed (INVALID_MAX_RISK_CAP).
  - Effective cap when both are valid is strictly `min(env_cap, kwarg_cap)`.
  - CLOSE and REDUCE are never blocked by this OPEN-only risk cap.

Strict isolation:
  - External sockets hard-blocked.
  - Zero real SQLite writes, zero real Kraken orders.
"""

import os
import socket
from typing import Any, Dict, List

import pytest


class _ExternalNetworkBlocked(RuntimeError):
    pass


_ORIG_CONNECT = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) and address else ""
    if host not in ("127.0.0.1", "::1"):
        raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.9.4: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.9.4: {args}")


socket.socket.connect = _guarded_connect
socket.create_connection = _blocked_external
socket.getaddrinfo = _blocked_external

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SMARTTRADE_TEST_MODE"] = "1"

from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper, KrakenTradeTracker


class _SpyRestClient194:
    def __init__(self, positions=None):
        self.exec_calls: List[Dict[str, Any]] = []
        self.positions = list(positions or [])

    async def get_positions(self, **kwargs):
        return list(self.positions)

    async def get_open_orders(self, **kwargs):
        return []

    async def place_market_order(self, **kwargs):
        self.exec_calls.append({"method": "place_market_order", **kwargs})
        return {"success": True, "code": "00000", "order_id": "SIM_194", "price": 3000.0, "filled_size": float(kwargs.get("size", 0.02))}

    async def place_order(self, **kwargs):
        self.exec_calls.append({"method": "place_order", **kwargs})
        return {"success": False, "code": "SIM_FORBIDDEN"}


def _make_wrapper(client: _SpyRestClient194, *, global_lock: bool = False) -> KrakenFuturesExecutorWrapper:
    from backend.market_data.kraken_futures_book_feed import KrakenFuturesBookFeed
    w = KrakenFuturesExecutorWrapper(api_key="k194", api_secret="c2VjcmV0", timeout=2.0, single_entry_mode=False)
    w._rest_client = client
    w._get_rest_client = lambda: client
    w._global_entry_lock = global_lock
    w._startup_reconciliation_completed = not global_lock
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"])
    feed.on_connected()
    feed.process_message({
        "feed": "book_snapshot", "product_id": "PF_ETHUSD", "seq": 1, "timestamp": 1700000000000,
        "bids": [{"price": 2999.5, "qty": 10.0}], "asks": [{"price": 3000.0, "qty": 10.0}],
    })
    w.set_book_feed(feed)
    return w


# Helper: ETHUSDT @ $3000, SL @ $2970 -> sl_dist = $30/ETH
# qty=0.015 -> risk=$0.45 | qty=0.025 -> risk=$0.75 | qty=0.05 -> risk=$1.50 | qty=0.10 -> risk=$3.00


@pytest.mark.asyncio
async def test_01_global_1_arg_absent_uses_1(monkeypatch):
    """1. Global 1 USD, argument absent -> cap = 1 USD."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "1.00")
    client = _SpyRestClient194()
    w = _make_wrapper(client, global_lock=True)

    # $0.75 <= $1.00 -> passes risk gate (reaches global lock)
    r_ok = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.025, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False)
    assert r_ok["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    # $1.50 > $1.00 -> RISK_EXCEEDS_BUDGET
    r_hi = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.05, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False)
    assert r_hi["code"] == "RISK_EXCEEDS_BUDGET"


@pytest.mark.asyncio
async def test_02_global_1_arg_050_uses_050(monkeypatch):
    """2. Global 1 USD, argument 0.50 USD -> effective cap = 0.50 USD."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "1.00")
    client = _SpyRestClient194()
    w = _make_wrapper(client, global_lock=True)

    # $0.45 <= $0.50 -> passes risk gate
    r_ok = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False, max_risk_usd=0.50)
    assert r_ok["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    # $0.75 > $0.50 -> RISK_EXCEEDS_BUDGET
    r_hi = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.025, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False, max_risk_usd=0.50)
    assert r_hi["code"] == "RISK_EXCEEDS_BUDGET"


@pytest.mark.asyncio
async def test_03_global_1_arg_500_uses_1_no_elevation(monkeypatch):
    """3. Global 1 USD, argument 500 USD -> effective cap = 1 USD (NO elevation allowed!)."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "1.00")
    client = _SpyRestClient194()
    w = _make_wrapper(client, global_lock=False)

    # $1.50 > $1.00 (even though < $500) -> MUST be blocked by RISK_EXCEEDS_BUDGET!
    r = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.05, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False, max_risk_usd=500.0)
    assert r["success"] is False
    assert r["code"] == "RISK_EXCEEDS_BUDGET"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_04_global_absent_arg_2_uses_2(monkeypatch):
    """4. Global absent, argument 2 USD -> effective cap = 2 USD."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.delenv("KRAKEN_MAX_TRADE_RISK_USD", raising=False)
    client = _SpyRestClient194()
    w = _make_wrapper(client, global_lock=True)

    # $1.50 <= $2.00 -> passes risk gate
    r_ok = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.05, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False, max_risk_usd=2.0)
    assert r_ok["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    # $3.00 > $2.00 -> RISK_EXCEEDS_BUDGET
    r_hi = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.10, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False, max_risk_usd=2.0)
    assert r_hi["code"] == "RISK_EXCEEDS_BUDGET"


@pytest.mark.asyncio
async def test_05_global_absent_arg_absent_refused(monkeypatch):
    """5. Global absent, argument absent -> INVALID_MAX_RISK_CAP."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.delenv("KRAKEN_MAX_TRADE_RISK_USD", raising=False)
    client = _SpyRestClient194()
    w = _make_wrapper(client, global_lock=False)

    r = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False)
    assert r["success"] is False and r["code"] == "INVALID_MAX_RISK_CAP"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_env", ["nan", "", "   ", "-1.0", "0", "inf", "abc"])
async def test_06_07_08_invalid_global_with_valid_arg_refused(monkeypatch, bad_env):
    """6, 7, 8. Global NaN / empty / negative / 0 / inf / non-numeric + valid arg 2 USD -> REFUSED (INVALID_MAX_RISK_CAP)."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", bad_env)
    client = _SpyRestClient194()
    w = _make_wrapper(client, global_lock=False)

    r = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False, max_risk_usd=2.0)
    assert r["success"] is False and r["code"] == "INVALID_MAX_RISK_CAP"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_arg", [float("nan"), float("inf"), float("-inf"), 0, 0.0, -1.0, True, False, "", "   ", "abc"])
async def test_09_10_11_12_valid_global_with_invalid_arg_refused(monkeypatch, bad_arg):
    """9, 10, 11, 12. Global valid + arg NaN / inf / 0 / negative / bool / empty / non-numeric -> REFUSED."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "1.00")
    client = _SpyRestClient194()
    w = _make_wrapper(client, global_lock=False)

    r = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False, max_risk_usd=bad_arg)
    assert r["success"] is False and r["code"] == "INVALID_MAX_RISK_CAP"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_13_identical_valid_caps_use_common_cap(monkeypatch):
    """13. Both caps equal (1.00 USD) -> effective cap = 1.00 USD."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "1.00")
    client = _SpyRestClient194()
    w = _make_wrapper(client, global_lock=True)

    r_ok = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.025, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False, max_risk_usd=1.00)
    assert r_ok["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    r_hi = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.05, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False, max_risk_usd=1.00)
    assert r_hi["code"] == "RISK_EXCEEDS_BUDGET"


@pytest.mark.asyncio
async def test_14_15_risk_above_global_only_and_above_both_refused(monkeypatch):
    """14 & 15. Risk > global (but < arg) AND Risk > both -> both refused with RISK_EXCEEDS_BUDGET."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "1.00")
    client = _SpyRestClient194()
    w = _make_wrapper(client, global_lock=False)

    # Risk = $1.50: above global ($1.00), below arg ($2.00) -> REFUSED
    r14 = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.05, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False, max_risk_usd=2.00)
    assert r14["success"] is False and r14["code"] == "RISK_EXCEEDS_BUDGET"

    # Risk = $3.00: above both ($1.00 and $2.00) -> REFUSED
    r15 = await w.place_order(symbol="ETHUSDT", side="buy", quantity=0.10, order_type="market", entry_price=3000.0, stop_loss=2970.0, intent="open", reduce_only=False, max_risk_usd=2.00)
    assert r15["success"] is False and r15["code"] == "RISK_EXCEEDS_BUDGET"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_16_17_close_and_reduce_without_cap_not_blocked(monkeypatch):
    """16 & 17. CLOSE and REDUCE without any cap remain operational."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    monkeypatch.delenv("KRAKEN_MAX_TRADE_RISK_USD", raising=False)
    client = _SpyRestClient194(positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}])
    w = _make_wrapper(client, global_lock=True)
    w._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="TRK_194", client_oid="BOT_194", symbol="PF_ETHUSD",
        side="long", filled_qty=0.05, expected_exposure=0.05, status="OPEN",
    )

    red = await w.place_order(symbol="ETHUSDT", side="sell", quantity=0.02, order_type="market", entry_price=3000.0, intent="reduce", reduce_only=True)
    assert red.get("success") is True

    client.positions[0]["size"] = 0.03
    cls = await w.place_order(symbol="ETHUSDT", side="sell", quantity=0.03, order_type="market", entry_price=3000.0, intent="close", reduce_only=True)
    assert cls.get("success") is True
    assert len(client.exec_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_env", ["nan", "", "0", "-5", "abc"])
async def test_18_close_and_reduce_with_invalid_global_env_not_blocked(monkeypatch, bad_env):
    """18. CLOSE and REDUCE with invalid KRAKEN_MAX_TRADE_RISK_USD env var remain operational."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", bad_env)
    client = _SpyRestClient194(positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}])
    w = _make_wrapper(client, global_lock=True)
    w._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="TRK_194_B", client_oid="BOT_194_B", symbol="PF_ETHUSD",
        side="long", filled_qty=0.05, expected_exposure=0.05, status="OPEN",
    )

    red = await w.place_order(symbol="ETHUSDT", side="sell", quantity=0.02, order_type="market", entry_price=3000.0, intent="reduce", reduce_only=True)
    assert red.get("success") is True

    client.positions[0]["size"] = 0.03
    cls = await w.place_order(symbol="ETHUSDT", side="sell", quantity=0.03, order_type="market", entry_price=3000.0, intent="close", reduce_only=True)
    assert cls.get("success") is True
    assert len(client.exec_calls) == 2
