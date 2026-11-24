"""
Phase 1.8 — P1 Kraken Risk-Based Sizing & Independent Risk Cap Suite (100% Offline)
===================================================================================
Covers all 10 mandatory test requirements across BTC, ETH, SOL, and XRP:
  1. At 1x, SL loss matches requested risk budget.
  2. At 3x, SL loss remains identical.
  3. At 10x, SL loss remains identical.
  4. Changing leverage changes required margin ONLY, never the SL loss budget.
  5. Min contract size exceeding risk budget is refused (never rounded up).
  6. Floored quantity always respects risk budget (actual_risk <= risk_budget).
  7. Insufficient margin blocks opening (fail-closed).
  8. Missing, invalid, or non-finite SL blocks opening.
  9. Independent KRAKEN_MAX_TRADE_RISK_USD cap in place_order() blocks excessive risk OPEN.
 10. CLOSE and REDUCE remain functional and are NOT blocked by the OPEN risk cap.

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
        raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.8: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.8: {args}")


socket.socket.connect = _guarded_connect
socket.create_connection = _blocked_external
socket.getaddrinfo = _blocked_external

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SMARTTRADE_TEST_MODE"] = "1"

from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper, KrakenTradeTracker
from smc_futures.engine.multi_broker_engine import BrokerType, MultiBrokerEngine

INSTRUMENTS = [
    ("BTCUSDT", 90000.0, 89100.0),   # 1% SL = $900/unit
    ("ETHUSDT", 3000.0, 2970.0),     # 1% SL = $30/unit
    ("SOLUSDT", 180.0, 178.20),      # 1% SL = $1.80/unit
    ("XRPUSDT", 1.50, 1.485),        # 1% SL = $0.015/unit
]


@pytest.mark.parametrize("symbol,entry,sl", INSTRUMENTS)
def test_1_2_3_4_leverage_1x_3x_10x_keeps_sl_risk_identical_and_scales_margin_only(symbol, entry, sl):
    """Tests 1, 2, 3 & 4: Risk at SL is invariant across 1x, 3x, 10x; only required_margin scales as 1/leverage."""
    balance = 46.6613
    risk_pct = 0.01  # $0.466613
    sl_dist = abs(entry - sl)
    budget = balance * risk_pct

    ok1, qty1, m1, err1 = MultiBrokerEngine._calculate_risk_based_quantity(
        available_balance=balance, risk_per_trade_pct=risk_pct,
        entry_price=entry, stop_loss=sl, leverage=1, symbol=symbol, broker=BrokerType.KRAKEN,
    )
    ok3, qty3, m3, err3 = MultiBrokerEngine._calculate_risk_based_quantity(
        available_balance=balance, risk_per_trade_pct=risk_pct,
        entry_price=entry, stop_loss=sl, leverage=3, symbol=symbol, broker=BrokerType.KRAKEN,
    )
    ok10, qty10, m10, err10 = MultiBrokerEngine._calculate_risk_based_quantity(
        available_balance=balance, risk_per_trade_pct=risk_pct,
        entry_price=entry, stop_loss=sl, leverage=10, symbol=symbol, broker=BrokerType.KRAKEN,
    )

    assert ok1 and ok3 and ok10, f"Unexpected failures: {err1}, {err3}, {err10}"
    # Exact same quantity & exact same SL risk across 1x, 3x, 10x!
    assert qty1 == qty3 == qty10
    risk_at_sl = qty10 * sl_dist
    assert 0 < risk_at_sl <= budget + 1e-9
    # Required margin scales inversely with leverage
    assert math.isclose(m1, m3 * 3.0, rel_tol=1e-6)
    assert math.isclose(m1, m10 * 10.0, rel_tol=1e-6)
    assert math.isclose(m10, (qty10 * entry) / 10.0, rel_tol=1e-6)


@pytest.mark.parametrize("symbol,entry,sl", INSTRUMENTS)
def test_5_min_qty_exceeding_risk_budget_is_refused_never_forced_up(symbol, entry, sl):
    """Test 5: When even min_qty would exceed risk_budget, order is refused (BELOW_MIN_RISK_BUDGET)."""
    # Tiny balance ($1.00 * 1% = $0.01) -> smaller than min_qty * sl_dist on all 4 instruments
    ok, qty, margin, err = MultiBrokerEngine._calculate_risk_based_quantity(
        available_balance=1.0, risk_per_trade_pct=0.01,
        entry_price=entry, stop_loss=sl, leverage=10, symbol=symbol, broker=BrokerType.KRAKEN,
    )
    assert ok is False
    assert qty == 0.0
    assert err is not None and "BELOW_MIN_RISK_BUDGET" in err


@pytest.mark.parametrize("symbol,entry,sl", INSTRUMENTS)
def test_6_floored_quantity_always_respects_risk_budget(symbol, entry, sl):
    """Test 6: Floor rounding never produces actual SL loss above requested risk budget."""
    for bal in (46.6613, 73.21, 125.77):
        risk_pct = 0.01
        budget = bal * risk_pct
        ok, qty, _, err = MultiBrokerEngine._calculate_risk_based_quantity(
            available_balance=bal, risk_per_trade_pct=risk_pct,
            entry_price=entry, stop_loss=sl, leverage=10, symbol=symbol, broker=BrokerType.KRAKEN,
        )
        assert ok is True, err
        assert qty * abs(entry - sl) <= budget + 1e-9


def test_7_insufficient_margin_blocks_opening():
    """Test 7: Tight SL (0.25%) at 1x requires notional $180 > $46.66 available -> INSUFFICIENT_MARGIN."""
    ok, qty, req_margin, err = MultiBrokerEngine._calculate_risk_based_quantity(
        available_balance=46.6613, risk_per_trade_pct=0.01,
        entry_price=3000.0, stop_loss=2992.5,  # 0.25% SL -> qty ~0.062 -> notional $186 > $46.66 at 1x
        leverage=1, symbol="ETHUSDT", broker=BrokerType.KRAKEN,
    )
    assert ok is False
    assert qty == 0.0
    assert req_margin > 46.6613
    assert err is not None and "INSUFFICIENT_MARGIN" in err

    # Same trade at 10x needs only ~$18.60 margin <= $46.66 -> ALLOWED with identical SL risk!
    ok10, qty10, req_margin10, err10 = MultiBrokerEngine._calculate_risk_based_quantity(
        available_balance=46.6613, risk_per_trade_pct=0.01,
        entry_price=3000.0, stop_loss=2992.5,
        leverage=10, symbol="ETHUSDT", broker=BrokerType.KRAKEN,
    )
    assert ok10 is True, err10
    assert req_margin10 < 46.6613
    assert qty10 * 7.5 <= 46.6613 * 0.01 + 1e-9


@pytest.mark.parametrize("bad_sl", [None, 0.0, -100.0, float("nan"), float("inf"), 3000.0])
def test_8_missing_invalid_or_non_finite_sl_blocks_opening(bad_sl):
    """Test 8: Missing, zero, negative, NaN, inf, or zero-distance SL blocks opening fail-closed."""
    ok, qty, _, err = MultiBrokerEngine._calculate_risk_based_quantity(
        available_balance=46.6613, risk_per_trade_pct=0.01,
        entry_price=3000.0, stop_loss=bad_sl, leverage=10, symbol="ETHUSDT", broker=BrokerType.KRAKEN,
    )
    assert ok is False
    assert qty == 0.0
    assert err in ("INVALID_STOP_LOSS", "INVALID_SL_DISTANCE")


class _SpyRestClient:
    def __init__(self):
        self.exec_calls: List[Dict[str, Any]] = []
        self.positions = [{"symbol": "PF_ETHUSD", "side": "long", "size": 0.05, "price": 3000.0}]

    async def get_positions(self, **kwargs):
        return list(self.positions)

    async def get_open_orders(self, **kwargs):
        return []

    async def place_market_order(self, **kwargs):
        self.exec_calls.append({"method": "place_market_order", **kwargs})
        return {"success": True, "code": "00000", "order_id": "SIM_OK", "price": 3000.0, "filled_size": float(kwargs.get("size", 0.02))}

    async def place_order(self, **kwargs):
        self.exec_calls.append({"method": "place_order", **kwargs})
        return {"success": False, "code": "SIM_FORBIDDEN"}


@pytest.mark.asyncio
async def test_9_independent_max_trade_risk_cap_blocks_excessive_open(monkeypatch):
    """Test 9: KRAKEN_MAX_TRADE_RISK_USD in place_order() refuses OPEN exceeding cap (0 REST calls)."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "1.00")

    from backend.market_data.kraken_futures_book_feed import KrakenFuturesBookFeed
    client = _SpyRestClient()
    w = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0", timeout=2.0, single_entry_mode=False)
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

    # 0.10 ETH * $30 SL distance = $3.00 risk > $1.00 cap -> BLOCKED
    res = await w.place_order(
        symbol="ETHUSDT", side="buy", quantity=0.10, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, leverage=10, intent="open", reduce_only=False,
    )
    assert res["success"] is False
    assert res["code"] == "RISK_EXCEEDS_BUDGET"
    assert len(client.exec_calls) == 0


@pytest.mark.asyncio
async def test_10_close_and_reduce_not_blocked_by_open_risk_cap(monkeypatch):
    """Test 10: CLOSE and REDUCE remain functional even with a very strict KRAKEN_MAX_TRADE_RISK_USD cap."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "0.01")

    client = _SpyRestClient()
    w = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0", timeout=2.0, single_entry_mode=False)
    w._rest_client = client
    w._get_rest_client = lambda: client
    w._global_entry_lock = True
    w._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="TRACKED_1_8", client_oid="BOT_1_8", symbol="PF_ETHUSD",
        side="long", filled_qty=0.05, expected_exposure=0.05, status="OPEN",
    )

    red = await w.place_order(
        symbol="ETHUSDT", side="sell", quantity=0.02, order_type="market",
        entry_price=3000.0, intent="reduce", reduce_only=True,
    )
    assert red.get("success") is True
    assert len(client.exec_calls) == 1

    client.positions[0]["size"] = 0.03
    cls = await w.place_order(
        symbol="ETHUSDT", side="sell", quantity=0.03, order_type="market",
        entry_price=3000.0, intent="close", reduce_only=True,
    )
    assert cls.get("success") is True
    assert len(client.exec_calls) == 2
