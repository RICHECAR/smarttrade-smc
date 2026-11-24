"""
Phase 1.9.2 — Mandatory Independent Risk Cap Fail-Closed Suite (100% Offline)
=============================================================================
Proves that KrakenFuturesExecutorWrapper.place_order() strictly refuses OPEN
with code `INVALID_MAX_RISK_CAP` whenever BOTH `KRAKEN_MAX_TRADE_RISK_USD` and
`max_risk_usd` are absent/None/invalid, while keeping CLOSE and REDUCE working
without any cap requirement.

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
        raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.9.2: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.9.2: {args}")


socket.socket.connect = _guarded_connect
socket.create_connection = _blocked_external
socket.getaddrinfo = _blocked_external

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SMARTTRADE_TEST_MODE"] = "1"

from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper, KrakenTradeTracker


class _SpyRestClient192:
    def __init__(self, positions=None):
        self.exec_calls: List[Dict[str, Any]] = []
        self.positions = list(positions or [])

    async def get_positions(self, **kwargs):
        return list(self.positions)

    async def get_open_orders(self, **kwargs):
        return []

    async def place_market_order(self, **kwargs):
        self.exec_calls.append({"method": "place_market_order", **kwargs})
        return {"success": True, "code": "00000", "order_id": "SIM_192", "price": 3000.0, "filled_size": float(kwargs.get("size", 0.015))}

    async def place_order(self, **kwargs):
        self.exec_calls.append({"method": "place_order", **kwargs})
        return {"success": False, "code": "SIM_FORBIDDEN"}


def _make_unlocked_wrapper(client: _SpyRestClient192) -> KrakenFuturesExecutorWrapper:
    """Wrapper with global lock OFF and startup reconciliation COMPLETED so ONLY the risk cap gate is tested."""
    from backend.market_data.kraken_futures_book_feed import KrakenFuturesBookFeed
    w = KrakenFuturesExecutorWrapper(api_key="k192", api_secret="c2VjcmV0", timeout=2.0, single_entry_mode=False)
    w._rest_client = client
    w._get_rest_client = lambda: client
    w._global_entry_lock = False
    w._startup_reconciliation_completed = True
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"])
    feed.on_connected()
    feed.process_message({
        "feed": "book_snapshot", "product_id": "PF_ETHUSD", "seq": 1, "timestamp": 1700000000000,
        "bids": [{"price": 2999.5, "qty": 10.0}], "asks": [{"price": 3000.0, "qty": 10.0}],
    })
    w.set_book_feed(feed)
    return w


@pytest.mark.asyncio
async def test_1_env_absent_and_kwarg_absent_refuses_open_with_invalid_max_risk_cap(monkeypatch):
    """Variable absente + argument absent -> OPEN refusé STRICTEMENT par INVALID_MAX_RISK_CAP (pas GLOBAL_ENTRY_LOCK)."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.delenv("KRAKEN_MAX_TRADE_RISK_USD", raising=False)

    client = _SpyRestClient192()
    w = _make_unlocked_wrapper(client)

    res = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
    )
    assert res["success"] is False
    assert res["code"] == "INVALID_MAX_RISK_CAP"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_2_env_absent_and_kwarg_none_refuses_open_with_invalid_max_risk_cap(monkeypatch):
    """Variable absente + max_risk_usd=None -> OPEN refusé STRICTEMENT par INVALID_MAX_RISK_CAP."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.delenv("KRAKEN_MAX_TRADE_RISK_USD", raising=False)

    client = _SpyRestClient192()
    w = _make_unlocked_wrapper(client)

    res = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
        max_risk_usd=None,
    )
    assert res["success"] is False
    assert res["code"] == "INVALID_MAX_RISK_CAP"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_3_env_absent_and_valid_kwarg_passes_risk_gate_and_enforces_downstream_guards(monkeypatch):
    """Variable absente + max_risk_usd valide -> passe la garde risque, mais ne contourne PAS les gardes suivantes."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.delenv("KRAKEN_MAX_TRADE_RISK_USD", raising=False)

    client = _SpyRestClient192()
    w = _make_unlocked_wrapper(client)
    # Activate global lock to prove valid kwarg passes risk cap gate and still hits downstream global lock
    w._global_entry_lock = True

    # Risk = 0.015 * $30 = $0.45 <= $1.00 -> passes risk cap gate, blocked by GLOBAL_ENTRY_LOCK_ACTIVE
    res_ok = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
        max_risk_usd=1.00,
    )
    assert res_ok["success"] is False
    assert res_ok["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"

    # Risk = 0.10 * $30 = $3.00 > $1.00 -> still blocked by RISK_EXCEEDS_BUDGET even with explicit kwarg
    res_over = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.10, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
        max_risk_usd=1.00,
    )
    assert res_over["success"] is False
    assert res_over["code"] == "RISK_EXCEEDS_BUDGET"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_4_env_present_valid_passes_risk_gate_and_blocks_excess_risk(monkeypatch):
    """Variable présente valide -> passe si risque <= plafond, bloque RISK_EXCEEDS_BUDGET si risque > plafond."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "1.00")

    client = _SpyRestClient192()
    w = _make_unlocked_wrapper(client)
    w._global_entry_lock = True

    res_ok = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
    )
    assert res_ok["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"

    res_over = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.10, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
    )
    assert res_over["success"] is False
    assert res_over["code"] == "RISK_EXCEEDS_BUDGET"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_val",
    ["", "   ", "0", "-1", "-0.01", "nan", "NaN", "inf", "+inf", "-inf", "abc", "1USD"],
)
async def test_5_invalid_env_values_all_refused_with_invalid_max_risk_cap(monkeypatch, bad_val):
    """Vide, espaces, 0, négatif, NaN, +inf, -inf, non numérique -> INVALID_MAX_RISK_CAP."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", bad_val)

    client = _SpyRestClient192()
    w = _make_unlocked_wrapper(client)

    res = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
    )
    assert res["success"] is False
    assert res["code"] == "INVALID_MAX_RISK_CAP"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_6_close_and_reduce_without_cap_remain_operational(monkeypatch):
    """CLOSE et REDUCE sans KRAKEN_MAX_TRADE_RISK_USD et sans max_risk_usd restent 100% fonctionnels."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    monkeypatch.delenv("KRAKEN_MAX_TRADE_RISK_USD", raising=False)

    client = _SpyRestClient192(positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}])
    w = _make_unlocked_wrapper(client)
    w._global_entry_lock = True
    w._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="TRK_192", client_oid="BOT_192", symbol="PF_ETHUSD",
        side="long", filled_qty=0.05, expected_exposure=0.05, status="OPEN",
    )

    red = await w.place_order(
        symbol="ETHUSDT", side="sell", quantity=0.02, order_type="market",
        entry_price=3000.0, intent="reduce", reduce_only=True,
    )
    assert red.get("success") is True

    client.positions[0]["size"] = 0.03
    cls = await w.place_order(
        symbol="ETHUSDT", side="sell", quantity=0.03, order_type="market",
        entry_price=3000.0, intent="close", reduce_only=True,
    )
    assert cls.get("success") is True
    assert len(client.exec_calls) == 2
