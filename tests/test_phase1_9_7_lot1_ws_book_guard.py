"""
Phase 1.9.7 / Lot 1 (Option C) — Synchronized Kraken Futures WS v1 `book` Guard Tests.

100% offline, deterministic (virtual clock), zero network calls, zero SQLite/env mutations.
"""

from __future__ import annotations

import asyncio
import math
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


class _DummyRestClient:
    """Offline fake REST client that records sent orders and never touches network."""

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

    async def get_tickers(self) -> Dict[str, Dict[str, Any]]:
        # Deliberately present to prove OPEN MARKET NEVER falls back to REST /tickers
        return {
            "PF_ETHUSD": {
                "symbol": "PF_ETHUSD",
                "bid": 3000.0,
                "ask": 3001.0,
                "mark_price": 3000.5,
                "last": 3000.5,
            }
        }

    async def place_market_order(self, **kwargs) -> Dict[str, Any]:
        self.sent_orders.append({"type": "market", **kwargs})
        return {"success": True, "order_id": "FAKE_MKT_1", "code": "00000"}

    async def place_limit_order(self, **kwargs) -> Dict[str, Any]:
        self.sent_orders.append({"type": "limit", **kwargs})
        return {"success": True, "order_id": "FAKE_LMT_1", "code": "00000"}


def _make_snapshot(
    symbol: str = "PF_ETHUSD",
    seq: int = 100,
    bid: float = 2999.5,
    ask: float = 3000.5,
    ts_ms: int = 1700000000000,
    bids: Optional[List[Dict[str, Any]]] = None,
    asks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "feed": "book_snapshot",
        "product_id": symbol,
        "seq": seq,
        "timestamp": ts_ms,
        "bids": bids if bids is not None else [{"price": bid, "qty": 10.0}, {"price": bid - 1.0, "qty": 20.0}],
        "asks": asks if asks is not None else [{"price": ask, "qty": 10.0}, {"price": ask + 1.0, "qty": 20.0}],
    }


def _make_wrapper(
    monkeypatch,
    book_feed: Optional[KrakenFuturesBookFeed] = None,
    rest_client: Optional[_DummyRestClient] = None,
    max_risk_usd: str = "50.0",
) -> tuple[KrakenFuturesExecutorWrapper, _DummyRestClient]:
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", max_risk_usd)
    client = rest_client or _DummyRestClient()
    w = KrakenFuturesExecutorWrapper(api_key="test", api_secret="test", single_entry_mode=False)
    w._rest_client = client
    w._global_entry_lock = False
    w._global_entry_lock_reason = None
    w._startup_reconciliation_completed = True
    if book_feed is not None:
        w.set_book_feed(book_feed)

    async def _fake_place_order_rest(**kwargs):
        client.sent_orders.append(kwargs)
        # Return failure with sentinel code so we stop cleanly right after passing all pre-flight gates
        # without requiring SQLite / fill confirmation mocks in unit tests
        return {"success": False, "code": "UNIT_TEST_REACHED_REST_SEND", "error": "reached_rest_send"}

    w._place_order_rest = _fake_place_order_rest
    return w, client


# ══════════════════════════════════════════════════════════════════════════════
# 1. KrakenFuturesBookFeed unit tests (snapshot, deltas, seq, heartbeat, disconnect)
# ══════════════════════════════════════════════════════════════════════════════


def test_1_snapshot_and_incremental_updates_maintain_exact_best_bid_ask():
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], heartbeat_timeout_sec=30.0, clock=clock)
    feed.on_connected()

    assert feed.process_message(_make_snapshot(seq=100, bid=2999.0, ask=3001.0)) is True
    ok, quote, err = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is True and err is None
    assert quote["bid"] == 2999.0
    assert quote["ask"] == 3001.0
    assert quote["seq"] == 100

    # Add better ask (3000.5)
    assert feed.process_message({
        "feed": "book", "product_id": "PF_ETHUSD", "side": "sell",
        "seq": 101, "price": 3000.5, "qty": 5.0, "timestamp": 1700000000100,
    }) is True
    ok, quote, _ = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is True and quote["ask"] == 3000.5

    # Remove 3000.5 (qty=0) -> best ask reverts to 3001.0
    assert feed.process_message({
        "feed": "book", "product_id": "PF_ETHUSD", "side": "sell",
        "seq": 102, "price": 3000.5, "qty": 0, "timestamp": 1700000000200,
    }) is True
    ok, quote, _ = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is True and quote["ask"] == 3001.0

    # Improve bid to 2999.8
    assert feed.process_message({
        "feed": "book", "product_id": "PF_ETHUSD", "side": "buy",
        "seq": 103, "price": 2999.8, "qty": 12.0, "timestamp": 1700000000300,
    }) is True
    ok, quote, _ = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is True and quote["bid"] == 2999.8 and quote["seq"] == 103


def test_2_quiet_symbol_without_book_updates_remains_valid_while_heartbeat_alive():
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], heartbeat_timeout_sec=10.0, clock=clock)
    feed.on_connected()
    assert feed.process_message(_make_snapshot(seq=10, bid=2999.0, ask=3001.0)) is True

    # Advance 25s with periodic heartbeats but ZERO book updates
    for _ in range(5):
        clock.advance(5.0)
        assert feed.process_message({"feed": "heartbeat", "time": 1700000005000}) is True

    ok, quote, code = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is True and code is None
    assert quote["bid"] == 2999.0 and quote["ask"] == 3001.0


def test_3_heartbeat_timeout_invalidates_stream_fail_closed():
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], heartbeat_timeout_sec=10.0, clock=clock)
    feed.on_connected()
    assert feed.process_message(_make_snapshot(seq=10, bid=2999.0, ask=3001.0)) is True

    # Exactly at 10.0s -> still alive
    clock.advance(10.0)
    ok, _, _ = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is True

    # Exceeds 10.0s -> stale fail-closed
    clock.advance(0.1)
    ok, err, code = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is False
    assert code == "STALE_REFERENCE_PRICE"
    assert "timeout" in err.lower()


def test_4_disconnect_immediately_invalidates_all_books():
    clock = _FakeClock(1000.0)
    reasons: List[str] = []
    feed = KrakenFuturesBookFeed(
        symbols=["PF_ETHUSD"],
        heartbeat_timeout_sec=30.0,
        clock=clock,
        on_disconnect_callback=lambda r: reasons.append(r),
    )
    feed.on_connected()
    assert feed.process_message(_make_snapshot(seq=50, bid=2999.0, ask=3001.0)) is True
    feed.on_disconnect("SOCKET_DROP")

    assert reasons == ["SOCKET_DROP"]
    ok, _, code = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is False
    assert code == "STALE_REFERENCE_PRICE"

    # Even if reconnected without a fresh snapshot, book must remain refused
    feed.on_connected()
    ok2, _, code2 = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok2 is False
    assert code2 == "MISSING_REFERENCE_PRICE"


def test_5_seq_gap_desyncs_fail_closed_until_fresh_snapshot():
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed.on_connected()
    assert feed.process_message(_make_snapshot(seq=100, bid=2999.0, ask=3001.0)) is True

    # Gap: seq=102 instead of 101
    assert feed.process_message({
        "feed": "book", "product_id": "PF_ETHUSD", "side": "buy",
        "seq": 102, "price": 2999.5, "qty": 5.0, "timestamp": 1700000000200,
    }) is False

    ok, _, code = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is False
    assert code == "STALE_REFERENCE_PRICE"

    # Subsequent delta must NOT silently recover
    assert feed.process_message({
        "feed": "book", "product_id": "PF_ETHUSD", "side": "buy",
        "seq": 103, "price": 2999.6, "qty": 5.0, "timestamp": 1700000000300,
    }) is False
    ok2, _, code2 = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok2 is False and code2 == "STALE_REFERENCE_PRICE"

    # Fresh snapshot restores sync
    assert feed.process_message(_make_snapshot(seq=200, bid=2999.5, ask=3000.5)) is True
    ok3, quote3, code3 = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok3 is True and code3 is None and quote3["seq"] == 200


def test_6_delta_before_snapshot_buffered_and_replayed_or_desynced():
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed.on_connected()

    # Delta arrives before snapshot -> not synced yet
    assert feed.process_message({
        "feed": "book", "product_id": "PF_ETHUSD", "side": "sell",
        "seq": 101, "price": 3000.2, "qty": 4.0, "timestamp": 1700000000100,
    }) is False
    ok, _, code = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is False and code == "MISSING_REFERENCE_PRICE"

    # Snapshot seq=100 arrives -> replays contiguous seq=101 cleanly
    assert feed.process_message(_make_snapshot(seq=100, bid=2999.0, ask=3001.0)) is True
    ok2, quote, _ = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok2 is True and quote["ask"] == 3000.2 and quote["seq"] == 101


@pytest.mark.parametrize("bad_msg", [
    "not_a_dict",
    {"feed": "book_snapshot", "product_id": "PF_ETHUSD", "seq": True, "timestamp": 1700000000000, "bids": [], "asks": []},
    {"feed": "book_snapshot", "product_id": "PF_ETHUSD", "seq": 1, "timestamp": 0, "bids": [], "asks": []},
    {"feed": "book_snapshot", "product_id": "PF_ETHUSD", "seq": 1, "timestamp": 1700000000000, "bids": "bad", "asks": []},
    {"feed": "book_snapshot", "product_id": "PF_ETHUSD", "seq": 1, "timestamp": 1700000000000,
     "bids": [{"price": float("nan"), "qty": 1.0}], "asks": [{"price": 3001.0, "qty": 1.0}]},
    {"feed": "book_snapshot", "product_id": "PF_ETHUSD", "seq": 1, "timestamp": 1700000000000,
     "bids": [{"price": 3002.0, "qty": 1.0}], "asks": [{"price": 3001.0, "qty": 1.0}]},  # crossed book
    {"event": "error", "product_id": "PF_ETHUSD", "message": "subscription failed"},
])
def test_7_malformed_or_crossed_book_frames_fail_closed(bad_msg):
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed.on_connected()
    assert feed.process_message(bad_msg) is False
    ok, _, _ = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is False


# ══════════════════════════════════════════════════════════════════════════════
# 2. KrakenFuturesExecutorWrapper.place_order() OPEN MARKET pre-flight gates
# ══════════════════════════════════════════════════════════════════════════════


def test_8_open_market_long_uses_ask_and_short_uses_bid(monkeypatch):
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed.on_connected()
    feed.process_message(_make_snapshot(seq=10, bid=2999.0, ask=3001.0))

    w, client = _make_wrapper(monkeypatch, book_feed=feed)

    # LONG with valid ASK & signal within 0.30% -> passes all pre-flight gates
    res_long = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    ))
    assert res_long["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(client.sent_orders) == 1

    # SHORT with valid BID & signal within 0.30% -> passes all pre-flight gates
    res_short = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=3050.0, reduce_only=False, intent="open",
    ))
    assert res_short["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(client.sent_orders) == 2


def test_9_entry_price_present_cannot_bypass_missing_ws_book_or_fallback_to_rest(monkeypatch):
    # No book feed attached, even though entry_price=3000.0 AND REST get_tickers() returns 3000.5!
    w, client = _make_wrapper(monkeypatch, book_feed=None)
    res = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    ))
    assert res["success"] is False
    assert res["code"] == "MISSING_REFERENCE_PRICE"
    assert len(client.sent_orders) == 0


def test_10_open_market_without_entry_price_uses_ws_book(monkeypatch):
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed.on_connected()
    feed.process_message(_make_snapshot(seq=10, bid=2999.0, ask=3001.0))

    w, client = _make_wrapper(monkeypatch, book_feed=feed)
    res = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=None, stop_loss=2950.0, reduce_only=False, intent="open",
    ))
    assert res["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(client.sent_orders) == 1


def test_11_missing_ask_for_long_and_missing_bid_for_short_refused(monkeypatch):
    clock = _FakeClock(1000.0)
    # Book with bids only (ASK missing)
    feed_no_ask = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed_no_ask.on_connected()
    feed_no_ask.process_message(_make_snapshot(seq=10, bids=[{"price": 2999.0, "qty": 5.0}], asks=[]))

    w1, c1 = _make_wrapper(monkeypatch, book_feed=feed_no_ask)
    res_long = asyncio.run(w1.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    ))
    assert res_long["success"] is False
    assert res_long["code"] == "MISSING_REFERENCE_PRICE"
    assert len(c1.sent_orders) == 0

    # Book with asks only (BID missing)
    feed_no_bid = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed_no_bid.on_connected()
    feed_no_bid.process_message(_make_snapshot(seq=10, bids=[], asks=[{"price": 3001.0, "qty": 5.0}]))

    w2, c2 = _make_wrapper(monkeypatch, book_feed=feed_no_bid)
    res_short = asyncio.run(w2.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=3050.0, reduce_only=False, intent="open",
    ))
    assert res_short["success"] is False
    assert res_short["code"] == "MISSING_REFERENCE_PRICE"
    assert len(c2.sent_orders) == 0


@pytest.mark.parametrize("bad_entry", [0, -100.0, float("nan"), float("inf"), float("-inf"), True, "abc"])
def test_12_invalid_signal_entry_price_refused(monkeypatch, bad_entry):
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed.on_connected()
    feed.process_message(_make_snapshot(seq=10, bid=2999.0, ask=3001.0))

    w, client = _make_wrapper(monkeypatch, book_feed=feed)
    res = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=bad_entry, stop_loss=2950.0, reduce_only=False, intent="open",
    ))
    assert res["success"] is False
    assert res["code"] == "INVALID_REFERENCE_PRICE"
    assert len(client.sent_orders) == 0


@pytest.mark.parametrize("side,signal_p,bid,ask,sl,expected_ok", [
    # Below 0.30% (0.20%)
    ("buy",  1000.0, 1001.0, 1002.0, 950.0,  True),
    ("sell", 1000.0, 998.0,  999.0,  1050.0, True),
    # Exactly 0.30% -> deterministic ALLOW
    ("buy",  1000.0, 1002.0, 1003.0, 950.0,  True),
    ("sell", 1000.0, 997.0,  998.0,  1050.0, True),
    # Strictly above 0.30% (0.301%) -> deterministic REFUSE
    ("buy",  1000.0, 1002.0, 1003.01, 950.0,  False),
    ("sell", 1000.0, 996.99, 998.0,   1050.0, False),
])
def test_13_deviation_boundary_0_30_pct(monkeypatch, side, signal_p, bid, ask, sl, expected_ok):
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed.on_connected()
    feed.process_message(_make_snapshot(seq=10, bid=bid, ask=ask))

    w, client = _make_wrapper(monkeypatch, book_feed=feed)
    res = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side=side, quantity=0.01, order_type="market",
        entry_price=signal_p, stop_loss=sl, reduce_only=False, intent="open",
    ))
    if expected_ok:
        assert res["code"] == "UNIT_TEST_REACHED_REST_SEND"
        assert len(client.sent_orders) == 1
    else:
        assert res["success"] is False
        assert res["code"] == "ENTRY_PRICE_DEVIATION_TOO_HIGH"
        assert len(client.sent_orders) == 0


def test_14_stop_already_crossed_long_and_short(monkeypatch):
    clock = _FakeClock(1000.0)
    # LONG: signal=1000, SL=998, live ASK=998.0 (crossed, within 0.30% deviation)
    feed_long = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed_long.on_connected()
    feed_long.process_message(_make_snapshot(seq=10, bid=997.5, ask=998.0))
    w1, c1 = _make_wrapper(monkeypatch, book_feed=feed_long)
    res_l = asyncio.run(w1.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=1000.0, stop_loss=998.0, reduce_only=False, intent="open",
    ))
    assert res_l["success"] is False
    assert res_l["code"] == "STOP_ALREADY_CROSSED"
    assert len(c1.sent_orders) == 0

    # SHORT: signal=1000, SL=1002, live BID=1002.0 (crossed, within 0.30% deviation)
    feed_short = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed_short.on_connected()
    feed_short.process_message(_make_snapshot(seq=10, bid=1002.0, ask=1002.5))
    w2, c2 = _make_wrapper(monkeypatch, book_feed=feed_short)
    res_s = asyncio.run(w2.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.01, order_type="market",
        entry_price=1000.0, stop_loss=1002.0, reduce_only=False, intent="open",
    ))
    assert res_s["success"] is False
    assert res_s["code"] == "STOP_ALREADY_CROSSED"
    assert len(c2.sent_orders) == 0


def test_15_conservative_reference_price_enforced_in_risk_gate(monkeypatch):
    clock = _FakeClock(1000.0)
    # LONG: signal=1000.0, ASK=1002.5 (dev=0.25% <= 0.30%), SL=995.0
    # Signal distance = 5.0 (qty=1.0 -> $5.0), Conservative ref = max(1000, 1002.5) = 1002.5 -> distance = 7.5 ($7.5 > $6.0 cap!)
    feed_l = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed_l.on_connected()
    feed_l.process_message(_make_snapshot(seq=10, bid=1002.0, ask=1002.5))
    w_l, c_l = _make_wrapper(monkeypatch, book_feed=feed_l, max_risk_usd="6.0")
    res_l = asyncio.run(w_l.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=1.0, order_type="market",
        entry_price=1000.0, stop_loss=995.0, reduce_only=False, intent="open",
    ))
    assert res_l["success"] is False
    assert res_l["code"] == "RISK_EXCEEDS_BUDGET"
    assert len(c_l.sent_orders) == 0

    # SHORT: signal=1000.0, BID=997.5 (dev=0.25% <= 0.30%), SL=1005.0
    # Signal distance = 5.0 ($5.0), Conservative ref = min(1000, 997.5) = 997.5 -> distance = 7.5 ($7.5 > $6.0 cap!)
    feed_s = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed_s.on_connected()
    feed_s.process_message(_make_snapshot(seq=10, bid=997.5, ask=998.0))
    w_s, c_s = _make_wrapper(monkeypatch, book_feed=feed_s, max_risk_usd="6.0")
    res_s = asyncio.run(w_s.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=1.0, order_type="market",
        entry_price=1000.0, stop_loss=1005.0, reduce_only=False, intent="open",
    ))
    assert res_s["success"] is False
    assert res_s["code"] == "RISK_EXCEEDS_BUDGET"
    assert len(c_s.sent_orders) == 0


def test_16_disconnected_or_desynced_book_blocks_open_market_with_zero_orders(monkeypatch):
    clock = _FakeClock(1000.0)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], clock=clock)
    feed.on_connected()
    feed.process_message(_make_snapshot(seq=10, bid=2999.0, ask=3001.0))
    feed.on_disconnect("NET_DROP")

    w, client = _make_wrapper(monkeypatch, book_feed=feed)
    res = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    ))
    assert res["success"] is False
    assert res["code"] == "STALE_REFERENCE_PRICE"
    assert len(client.sent_orders) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 3. Non-regression: LIMIT, CLOSE, REDUCE remain strictly unaffected
# ══════════════════════════════════════════════════════════════════════════════


def test_17_limit_order_unaffected_without_ws_book(monkeypatch):
    w, client = _make_wrapper(monkeypatch, book_feed=None)
    res = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="limit",
        price=2990.0, stop_loss=2950.0, reduce_only=False, intent="open",
    ))
    assert res["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(client.sent_orders) == 1


def test_18_close_and_reduce_remain_operational_without_ws_book(monkeypatch):
    client = _DummyRestClient(live_pos=0.05, live_side="long")
    w, _ = _make_wrapper(monkeypatch, book_feed=None, rest_client=client)
    w._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="ORD_1",
        client_oid="CID_1",
        symbol="PF_ETHUSD",
        side="long",
        filled_qty=0.05,
        expected_exposure=0.05,
        entry_price=3000.0,
        status="OPEN",
    )

    res_reduce = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.02, order_type="market",
        reduce_only=True, intent="reduce",
    ))
    assert res_reduce["code"] == "UNIT_TEST_REACHED_REST_SEND"

    res_close = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.05, order_type="market",
        reduce_only=True, intent="close",
    ))
    assert res_close["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(client.sent_orders) == 2
