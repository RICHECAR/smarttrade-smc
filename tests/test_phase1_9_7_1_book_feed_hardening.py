"""
Phase 1.9.7.1 — Micro-hardening tests for KrakenFuturesBookFeed (P2-1 & P2-2).

100% offline, simulated WebSocket, zero network calls, zero SQLite/.env mutations.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)
from backend.market_data.kraken_futures_book_feed import KrakenFuturesBookFeed


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


def _snap(symbol: str, seq: int = 10, bid: float = 2999.5, ask: float = 3000.5) -> Dict[str, Any]:
    return {
        "feed": "book_snapshot",
        "product_id": symbol,
        "seq": seq,
        "timestamp": 1700000000000,
        "bids": [{"price": bid, "qty": 10.0}],
        "asks": [{"price": ask, "qty": 10.0}],
    }


class _DummyRestClient:
    def __init__(self, live_pos: float = 0.0, live_side: str = "long") -> None:
        self.sent_orders: List[Dict[str, Any]] = []
        self.live_pos = live_pos
        self.live_side = live_side

    async def get_positions(self, **kwargs) -> List[Dict[str, Any]]:
        if self.live_pos > 0:
            return [{"symbol": "PF_ETHUSD", "side": self.live_side, "size": self.live_pos}]
        return []

    async def get_open_orders(self, **kwargs) -> List[Dict[str, Any]]:
        return []


def _make_wrapper(monkeypatch, feed: Optional[KrakenFuturesBookFeed], rest: Optional[_DummyRestClient] = None):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "50.0")
    client = rest or _DummyRestClient()
    w = KrakenFuturesExecutorWrapper(api_key="test", api_secret="test", single_entry_mode=False)
    w._rest_client = client
    w._global_entry_lock = False
    w._global_entry_lock_reason = None
    w._startup_reconciliation_completed = True
    w.set_book_feed(feed)

    async def _fake_place_order_rest(**kwargs):
        client.sent_orders.append(kwargs)
        return {"success": False, "code": "UNIT_TEST_REACHED_REST_SEND", "error": "reached_rest_send"}

    w._place_order_rest = _fake_place_order_rest
    return w, client


# ══════════════════════════════════════════════════════════════════════════════
# P2-1: event == "unsubscribed"
# ══════════════════════════════════════════════════════════════════════════════


def test_1_unsubscribed_single_symbol_invalidates_and_clears_book():
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], heartbeat_timeout_sec=10.0, clock=clock)
    feed.on_connected()
    assert feed.process_message(_snap("PF_ETHUSD", seq=10, bid=2999.5, ask=3000.5)) is True

    # Advance clock: unsubscribed must NOT refresh liveness timestamp
    clock.advance(5.0)
    liveness_before = feed._last_liveness_monotonic
    res = feed.process_message({
        "event": "unsubscribed",
        "feed": "book",
        "product_ids": ["PF_ETHUSD"],
    })
    assert res is False
    assert feed._last_liveness_monotonic == liveness_before  # NOT refreshed

    state = feed.get_book_state("PF_ETHUSD")
    assert state is not None
    assert state.synced is False
    assert state.snapshot_received is False
    assert state.bids == {}
    assert state.asks == {}

    ok, _, code = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is False
    assert code == "MISSING_REFERENCE_PRICE"


def test_2_unsubscribed_multiple_symbols_invalidates_all_listed():
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD", "PF_SOLUSD", "PF_XBTUSD"], clock=clock)
    feed.on_connected()
    assert feed.process_message(_snap("PF_ETHUSD", seq=1, bid=2999.5, ask=3000.5)) is True
    assert feed.process_message(_snap("PF_SOLUSD", seq=1, bid=179.5, ask=180.5)) is True
    assert feed.process_message(_snap("PF_XBTUSD", seq=1, bid=89999.0, ask=90001.0)) is True

    assert feed.process_message({
        "event": "unsubscribed",
        "feed": "book",
        "product_ids": ["PF_ETHUSD", "PF_SOLUSD"],
    }) is False

    for sym in ("PF_ETHUSD", "PF_SOLUSD"):
        ok, _, code = feed.get_best_bid_ask(sym)
        assert ok is False and code == "MISSING_REFERENCE_PRICE"

    # Unlisted symbol remains intact
    ok_btc, q_btc, code_btc = feed.get_best_bid_ask("PF_XBTUSD")
    assert ok_btc is True and code_btc is None and q_btc["ask"] == 90001.0


def test_3_unsubscribed_one_symbol_does_not_invalidate_other_subscribed_symbols():
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD", "PF_XBTUSD"], clock=clock)
    feed.on_connected()
    assert feed.process_message(_snap("PF_ETHUSD", seq=10, bid=2999.5, ask=3000.5)) is True
    assert feed.process_message(_snap("PF_XBTUSD", seq=20, bid=89999.0, ask=90001.0)) is True

    assert feed.process_message({
        "event": "unsubscribed",
        "feed": "book",
        "product_ids": ["PF_ETHUSD"],
    }) is False

    ok_eth, _, code_eth = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok_eth is False and code_eth == "MISSING_REFERENCE_PRICE"

    ok_btc, q_btc, code_btc = feed.get_best_bid_ask("PF_XBTUSD")
    assert ok_btc is True and code_btc is None
    assert q_btc["bid"] == 89999.0 and q_btc["ask"] == 90001.0 and q_btc["seq"] == 20


def test_4_open_market_refused_after_unsubscribed_zero_orders(monkeypatch):
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed.on_connected()
    feed.process_message(_snap("PF_ETHUSD", seq=10, bid=2999.5, ask=3000.5))
    feed.process_message({"event": "unsubscribed", "feed": "book", "product_ids": ["PF_ETHUSD"]})

    w, client = _make_wrapper(monkeypatch, feed)
    res = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    ))
    assert res["success"] is False
    assert res["code"] == "MISSING_REFERENCE_PRICE"
    assert len(client.sent_orders) == 0


def test_5_fresh_snapshot_after_unsubscribed_restores_open_market(monkeypatch):
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed.on_connected()
    feed.process_message(_snap("PF_ETHUSD", seq=10, bid=2999.5, ask=3000.5))
    feed.process_message({"event": "unsubscribed", "feed": "book", "product_ids": ["PF_ETHUSD"]})

    # Fresh snapshot restores sync
    assert feed.process_message(_snap("PF_ETHUSD", seq=50, bid=2999.8, ask=3000.2)) is True
    w, client = _make_wrapper(monkeypatch, feed)
    res = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    ))
    assert res["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(client.sent_orders) == 1


def test_10_unsubscribed_heartbeat_or_wildcard_book_invalidates_all_books():
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD", "PF_XBTUSD"], clock=clock)
    feed.on_connected()
    feed.process_message(_snap("PF_ETHUSD", seq=1, bid=2999.5, ask=3000.5))
    feed.process_message(_snap("PF_XBTUSD", seq=1, bid=89999.0, ask=90001.0))

    # Heartbeat unsubscribed -> must NOT be treated as heartbeat, must invalidate all books
    assert feed.process_message({"event": "unsubscribed", "feed": "heartbeat"}) is False
    assert feed.get_best_bid_ask("PF_ETHUSD")[0] is False
    assert feed.get_best_bid_ask("PF_XBTUSD")[0] is False

    # Re-sync then test wildcard book unsubscribed (no product_ids)
    feed.process_message(_snap("PF_ETHUSD", seq=2, bid=2999.5, ask=3000.5))
    feed.process_message(_snap("PF_XBTUSD", seq=2, bid=89999.0, ask=90001.0))
    assert feed.process_message({"event": "unsubscribed", "feed": "book"}) is False
    assert feed.get_best_bid_ask("PF_ETHUSD")[0] is False
    assert feed.get_best_bid_ask("PF_XBTUSD")[0] is False


# ══════════════════════════════════════════════════════════════════════════════
# P2-2: connect() subscription send() exception handling
# ══════════════════════════════════════════════════════════════════════════════


class _FakeWS:
    def __init__(self, fail_on_call: int) -> None:
        self.fail_on_call = fail_on_call
        self.send_calls = 0
        self.closed = False

    async def send(self, payload: str) -> None:
        self.send_calls += 1
        if self.send_calls == self.fail_on_call:
            raise RuntimeError(f"Simulated send failure on call #{self.send_calls}")

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("fail_on_call", [1, 2])
def test_6_7_8_9_10_connect_send_exception_cleans_state_and_invalidates_books(monkeypatch, fail_on_call):
    fake_ws = _FakeWS(fail_on_call=fail_on_call)
    fake_mod = types.ModuleType("websockets")

    async def _fake_connect(*args, **kwargs):
        return fake_ws

    fake_mod.connect = _fake_connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websockets", fake_mod)

    disconnect_events: List[str] = []
    feed = KrakenFuturesBookFeed(
        symbols=["PF_ETHUSD"],
        on_disconnect_callback=lambda r: disconnect_events.append(r),
    )
    # Pre-seed an old book state to verify it is invalidated on subscribe failure
    feed.on_connected()
    feed.process_message(_snap("PF_ETHUSD", seq=5, bid=2999.5, ask=3000.5))
    feed._connected = False  # simulate pre-connect state

    connected = asyncio.run(feed.connect())
    assert connected is False

    assert fake_ws.send_calls == fail_on_call
    assert fake_ws.closed is True
    assert feed._running is False
    assert feed._connected is False
    assert feed.is_connected is False
    assert feed._ws is None
    assert feed._recv_task is None
    assert len(disconnect_events) == 1  # No double disconnect!
    assert "SUBSCRIBE_FAILED" in disconnect_events[0]

    ok, _, code = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is False
    assert code == "STALE_REFERENCE_PRICE"


# ══════════════════════════════════════════════════════════════════════════════
# Non-regression: heartbeat, reconnect, LIMIT, CLOSE, REDUCE
# ══════════════════════════════════════════════════════════════════════════════


def test_11_heartbeat_and_reconnect_flow_intact():
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], heartbeat_timeout_sec=10.0, clock=clock)
    feed.on_connected()
    assert feed.process_message(_snap("PF_ETHUSD", seq=1, bid=2999.0, ask=3001.0)) is True

    clock.advance(8.0)
    assert feed.process_message({"feed": "heartbeat"}) is True
    clock.advance(8.0)
    ok, q, _ = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is True and q["ask"] == 3001.0

    # Disconnect + reconnect requires fresh snapshot
    feed.on_disconnect("DROP")
    feed.on_connected()
    assert feed.get_best_bid_ask("PF_ETHUSD")[0] is False
    assert feed.process_message(_snap("PF_ETHUSD", seq=10, bid=2999.5, ask=3000.5)) is True
    assert feed.get_best_bid_ask("PF_ETHUSD")[0] is True


def test_12_limit_close_reduce_unchanged(monkeypatch):
    # 1. OPEN LIMIT with no existing position and no WS book feed
    w_lim, c_lim = _make_wrapper(monkeypatch, feed=None, rest=_DummyRestClient(live_pos=0.0))
    r_lim = asyncio.run(w_lim.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="limit",
        price=2990.0, stop_loss=2950.0, reduce_only=False, intent="open",
    ))
    assert r_lim["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(c_lim.sent_orders) == 1

    # 2. REDUCE & CLOSE with active tracked position and no WS book feed
    client = _DummyRestClient(live_pos=0.05, live_side="long")
    w, _ = _make_wrapper(monkeypatch, feed=None, rest=client)
    w._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="ORD_1971",
        client_oid="CID_1971",
        symbol="PF_ETHUSD",
        side="long",
        filled_qty=0.05,
        expected_exposure=0.05,
        entry_price=3000.0,
        status="OPEN",
    )

    r_red = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.02, order_type="market",
        reduce_only=True, intent="reduce",
    ))
    assert r_red["code"] == "UNIT_TEST_REACHED_REST_SEND"

    r_cls = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.05, order_type="market",
        reduce_only=True, intent="close",
    ))
    assert r_cls["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(client.sent_orders) == 2
