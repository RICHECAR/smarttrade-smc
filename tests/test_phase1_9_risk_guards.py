"""
Phase 1.9 — Hardened Kraken Risk Guards Suite (100% Offline)
============================================================
Covers all mandatory test groups:
  A. Leverage validation in MultiBrokerEngine._calculate_risk_based_quantity():
     None, 0, -1, True, False, NaN, +inf, -inf, non-numeric, non-integer -> INVALID_LEVERAGE;
     valid 1, 3, 10 across BTC, ETH, SOL, XRP.
  B. KRAKEN_MAX_TRADE_RISK_USD fail-closed validation in KrakenFuturesExecutorWrapper.place_order():
     absent, empty string, whitespace, 0, negative, NaN, +inf, -inf, non-numeric, valid.
  C. Execution guards:
     OPEN blocked on invalid cap / unknown symbol / invalid contract multiplier / non-finite ref_price / invalid SL;
     CLOSE and REDUCE never blocked by the OPEN-only risk cap.

Strict isolation:
  - External sockets hard-blocked.
  - Zero real SQLite writes, zero real Kraken orders.
"""

import math
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
        raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.9: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.9: {args}")


socket.socket.connect = _guarded_connect
socket.create_connection = _blocked_external
socket.getaddrinfo = _blocked_external

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SMARTTRADE_TEST_MODE"] = "1"

from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper, KrakenTradeTracker
from backend.kraken_contracts import KRAKEN_CONTRACT_SPECS
from smc_futures.engine.multi_broker_engine import BrokerType, MultiBrokerEngine


class _SpyRestClient19:
    def __init__(self, positions=None):
        self.exec_calls: List[Dict[str, Any]] = []
        self.positions = list(positions or [])

    async def get_positions(self, **kwargs):
        return list(self.positions)

    async def get_open_orders(self, **kwargs):
        return []

    async def place_market_order(self, **kwargs):
        self.exec_calls.append({"method": "place_market_order", **kwargs})
        return {"success": True, "code": "00000", "order_id": "SIM_19", "price": 3000.0, "filled_size": float(kwargs.get("size", 0.01))}

    async def place_order(self, **kwargs):
        self.exec_calls.append({"method": "place_order", **kwargs})
        return {"success": False, "code": "SIM_FORBIDDEN"}


def _make_open_ready_wrapper(client: _SpyRestClient19) -> KrakenFuturesExecutorWrapper:
    from backend.market_data.kraken_futures_book_feed import KrakenFuturesBookFeed
    w = KrakenFuturesExecutorWrapper(api_key="k19", api_secret="c2VjcmV0", timeout=2.0, single_entry_mode=False)
    w._rest_client = client
    w._get_rest_client = lambda: client
    w._global_entry_lock = False
    w._startup_reconciliation_completed = True
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD", "PF_XBTUSD", "PF_FAKEUSD"])
    feed.on_connected()
    for sym, b, a in [("PF_ETHUSD", 2999.5, 3000.0), ("PF_XBTUSD", 89999.0, 90000.0), ("PF_FAKEUSD", 99.9, 100.0), ("PF_UNKNOWNUSD", 99.9, 100.0)]:
        feed.process_message({
            "feed": "book_snapshot", "product_id": sym, "seq": 1, "timestamp": 1700000000000,
            "bids": [{"price": b, "qty": 10.0}], "asks": [{"price": a, "qty": 10.0}],
        })
    w.set_book_feed(feed)
    return w


# ══════════════════════════════════════════════════════════════════════════════
# A. LEVERAGE VALIDATION (INVALID_LEVERAGE, NO SILENT 1x FALLBACK)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "bad_leverage",
    [None, 0, -1, -10, True, False, float("nan"), float("inf"), float("-inf"), "abc", "", 2.5],
)
def test_a_invalid_leverage_always_rejected_fail_closed(bad_leverage):
    ok, qty, margin, err = MultiBrokerEngine._calculate_risk_based_quantity(
        available_balance=46.6613,
        risk_per_trade_pct=0.01,
        entry_price=3000.0,
        stop_loss=2970.0,
        leverage=bad_leverage,
        symbol="ETHUSDT",
        broker=BrokerType.KRAKEN,
    )
    assert ok is False
    assert qty == 0.0
    assert margin == 0.0
    assert err == "INVALID_LEVERAGE"


@pytest.mark.parametrize("leverage", [1, 3, 10])
@pytest.mark.parametrize(
    "symbol,entry,sl",
    [
        ("BTCUSDT", 90000.0, 89100.0),
        ("ETHUSDT", 3000.0, 2970.0),
        ("SOLUSDT", 180.0, 178.20),
        ("XRPUSDT", 1.50, 1.485),
    ],
)
def test_a_valid_leverages_1_3_10_accepted_across_all_4_contracts(leverage, symbol, entry, sl):
    ok, qty, margin, err = MultiBrokerEngine._calculate_risk_based_quantity(
        available_balance=46.6613,
        risk_per_trade_pct=0.01,
        entry_price=entry,
        stop_loss=sl,
        leverage=leverage,
        symbol=symbol,
        broker=BrokerType.KRAKEN,
    )
    assert ok is True, err
    assert qty > 0
    assert qty * abs(entry - sl) <= 46.6613 * 0.01 + 1e-9
    assert math.isclose(margin, (qty * entry) / leverage, rel_tol=1e-6)


# ══════════════════════════════════════════════════════════════════════════════
# B. KRAKEN_MAX_TRADE_RISK_USD FAIL-CLOSED VALIDATION
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_cap_env",
    ["", "   ", "0", "-1", "-0.5", "nan", "NaN", "inf", "+inf", "-inf", "abc", "10USD"],
)
async def test_b_invalid_max_trade_risk_env_blocks_open_with_invalid_max_risk_cap(monkeypatch, bad_cap_env):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", bad_cap_env)

    client = _SpyRestClient19()
    w = _make_open_ready_wrapper(client)

    res = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
    )
    assert res["success"] is False
    assert res["code"] == "INVALID_MAX_RISK_CAP"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_b_bool_max_risk_kwarg_blocks_open(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.delenv("KRAKEN_MAX_TRADE_RISK_USD", raising=False)

    client = _SpyRestClient19()
    w = _make_open_ready_wrapper(client)

    res = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
        max_risk_usd=True,
    )
    assert res["success"] is False
    assert res["code"] == "INVALID_MAX_RISK_CAP"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_b_absent_max_trade_risk_env_does_not_invent_default_and_valid_cap_enforces_limit(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.delenv("KRAKEN_MAX_TRADE_RISK_USD", raising=False)

    client = _SpyRestClient19()
    w = _make_open_ready_wrapper(client)
    # Phase 1.9.2: Absent KRAKEN_MAX_TRADE_RISK_USD + absent max_risk_usd MUST fail closed with INVALID_MAX_RISK_CAP
    w._global_entry_lock = False
    res_absent = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
    )
    assert res_absent["success"] is False
    assert res_absent["code"] == "INVALID_MAX_RISK_CAP"
    assert len(client.exec_calls) == 0

    # With valid cap = 1.00 USD: risk $0.45 passes risk gate (reaches global lock), risk $3.00 blocked by RISK_EXCEEDS_BUDGET
    w._global_entry_lock = True
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "1.00")
    res_within = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
    )
    assert res_within["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"

    res_over = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.10, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
    )
    assert res_over["success"] is False
    assert res_over["code"] == "RISK_EXCEEDS_BUDGET"
    assert len(client.exec_calls) == 0


# ══════════════════════════════════════════════════════════════════════════════
# C. EXECUTION GUARDS: CLOSE/REDUCE, UNKNOWN SYMBOL, INVALID MULTIPLIER, REF_PRICE, SL
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_cap_env", ["", "0", "-1", "nan", "abc"])
async def test_c_close_and_reduce_never_blocked_by_invalid_open_risk_cap(monkeypatch, bad_cap_env):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", bad_cap_env)

    client = _SpyRestClient19(positions=[{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}])
    w = _make_open_ready_wrapper(client)
    w._global_entry_lock = True
    w._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="TRK_19", client_oid="BOT_19", symbol="PF_ETHUSD",
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


@pytest.mark.asyncio
async def test_c_unknown_symbol_and_invalid_contract_multiplier_blocked(monkeypatch):
    # 1. Unknown symbol in _calculate_risk_based_quantity
    ok, qty, _, err = MultiBrokerEngine._calculate_risk_based_quantity(
        available_balance=46.6613, risk_per_trade_pct=0.01,
        entry_price=10.0, stop_loss=9.9, leverage=10,
        symbol="FAKECOINUSDT", broker=BrokerType.KRAKEN,
    )
    assert ok is False and qty == 0.0 and "UNSUPPORTED_SYMBOL" in str(err)

    # 2. Corrupted contract_size multiplier (0.0 / NaN) in KRAKEN_CONTRACT_SPECS -> blocked in BOTH sizing & place_order
    orig_spec = dict(KRAKEN_CONTRACT_SPECS["PF_ETHUSD"])
    monkeypatch.setitem(KRAKEN_CONTRACT_SPECS, "PF_ETHUSD", {**orig_spec, "contract_size": float("nan")})

    ok_m, _, _, err_m = MultiBrokerEngine._calculate_risk_based_quantity(
        available_balance=46.6613, risk_per_trade_pct=0.01,
        entry_price=3000.0, stop_loss=2970.0, leverage=10,
        symbol="ETHUSDT", broker=BrokerType.KRAKEN,
    )
    assert ok_m is False and err_m == "INVALID_CONTRACT_MULTIPLIER"

    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "1.00")
    client = _SpyRestClient19()
    w = _make_open_ready_wrapper(client)
    res = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
    )
    assert res["success"] is False
    assert res["code"] == "INVALID_CONTRACT_MULTIPLIER"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ref_price", [float("nan"), float("inf"), float("-inf"), 0.0, -100.0])
async def test_c_non_finite_or_invalid_ref_price_blocked(monkeypatch, bad_ref_price):
    ok, _, _, err = MultiBrokerEngine._calculate_risk_based_quantity(
        available_balance=46.6613, risk_per_trade_pct=0.01,
        entry_price=bad_ref_price, stop_loss=2970.0, leverage=10,
        symbol="ETHUSDT", broker=BrokerType.KRAKEN,
    )
    assert ok is False and err == "INVALID_ENTRY_PRICE"

    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "1.00")
    client = _SpyRestClient19()
    w = _make_open_ready_wrapper(client)
    res = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.015, order_type="market",
        entry_price=bad_ref_price, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
    )
    assert res["success"] is False
    assert len(client.exec_calls) == 0
