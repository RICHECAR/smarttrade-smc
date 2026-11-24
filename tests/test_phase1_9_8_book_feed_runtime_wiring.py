"""
Phase 1.9.8 — Runtime Wiring Tests for KrakenFuturesBookFeed in MultiBrokerEngine & FastAPI.

100% offline, simulated WebSocket, zero real Kraken network calls, zero .env/SQLite mutations.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)
from smc_futures.engine.multi_broker_engine import (
    BrokerStatus,
    BrokerType,
    MultiBrokerConfig,
    MultiBrokerEngine,
)


class _SimWS:
    def __init__(self, incoming: Optional[List[Dict[str, Any]]] = None) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        for m in (incoming or []):
            self.queue.put_nowait(json.dumps(m))
        self.sent: List[Dict[str, Any]] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        return await self.queue.get()

    async def close(self) -> None:
        self.closed = True


def _install_fake_ws(monkeypatch, factory):
    fake_mod = types.ModuleType("websockets")

    class _FakeConnClosed(Exception):
        pass

    fake_mod.ConnectionClosed = _FakeConnClosed  # type: ignore[attr-defined]
    fake_mod.connect = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websockets", fake_mod)


class _FakeRest:
    def __init__(self, pos: float = 0.0) -> None:
        self.pos = pos
        self.sent_orders: List[Dict[str, Any]] = []

    async def get_positions(self, **kwargs):
        return [{"symbol": "PF_ETHUSD", "side": "long", "size": self.pos}] if self.pos > 0 else []

    async def get_open_orders(self, **kwargs):
        return []


class _FakeKrakenAdapter:
    """Minimal adapter mimicking KrakenFuturesExecutor._get_wrapper()."""

    def __init__(self, wrapper: KrakenFuturesExecutorWrapper) -> None:
        self._wrapper = wrapper
        self._client = None
        self.connect_calls = 0
        self.disconnect_calls = 0

    def _get_wrapper(self) -> KrakenFuturesExecutorWrapper:
        return self._wrapper

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


def _make_engine(
    monkeypatch,
    kraken_symbols: Optional[List[str]] = None,
    rest_pos: float = 0.0,
) -> tuple[MultiBrokerEngine, KrakenFuturesExecutorWrapper, _FakeRest, List[Any]]:
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "50.0")

    rest = _FakeRest(pos=rest_pos)
    wrapper = KrakenFuturesExecutorWrapper(api_key="t", api_secret="t", single_entry_mode=False)
    wrapper._rest_client = rest
    wrapper._global_entry_lock = False
    wrapper._global_entry_lock_reason = None
    wrapper._startup_reconciliation_completed = True

    set_book_feed_calls: List[Any] = []
    orig_set = wrapper.set_book_feed

    def _spy_set_book_feed(feed: Any) -> None:
        # Record whether feed was already connected when set_book_feed was invoked
        set_book_feed_calls.append((feed, bool(feed.is_connected) if feed is not None else None))
        orig_set(feed)

    wrapper.set_book_feed = _spy_set_book_feed  # type: ignore[method-assign]

    async def _fake_place_order_rest(**kwargs):
        rest.sent_orders.append(kwargs)
        return {"success": False, "code": "UNIT_TEST_REACHED_REST_SEND", "error": "ok"}

    wrapper._place_order_rest = _fake_place_order_rest  # type: ignore[method-assign]

    adapter = _FakeKrakenAdapter(wrapper)
    cfg = MultiBrokerConfig(
        default_broker="kraken",
        bitget_enabled=False,
        kraken_enabled=True,
        kraken_symbols=kraken_symbols if kraken_symbols is not None else ["PF_ETHUSD", "PF_XBTUSD", "PF_UNKNOWN_BAD"],
        reconnect_delay_sec=0.01,
        max_reconnect_attempts=1,
    )
    engine = MultiBrokerEngine(config=cfg, kraken_executor=adapter)
    return engine, wrapper, rest, set_book_feed_calls


@pytest.mark.asyncio
async def test_1_startup_wires_authorized_symbols_and_attaches_only_after_confirmed_connect(monkeypatch):
    sockets: List[_SimWS] = []

    async def _factory(*args, **kwargs):
        ws = _SimWS([
            {"event": "subscribed", "feed": "heartbeat"},
            {"event": "subscribed", "feed": "book", "product_ids": ["PF_ETHUSD", "PF_XBTUSD"]},
        ])
        sockets.append(ws)
        return ws

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, rest, spy_calls = _make_engine(monkeypatch)

    ok = await engine.start_kraken_book_feed()
    assert ok is True
    assert len(sockets) == 1
    # Unauthorized symbol PF_UNKNOWN_BAD must be filtered out!
    book_sub = [m for m in sockets[0].sent if m.get("feed") == "book"][0]
    assert book_sub["product_ids"] == ["PF_ETHUSD", "PF_XBTUSD"]
    # Proof that set_book_feed() was called ONLY AFTER connect() confirmed (is_connected=True)
    assert len(spy_calls) == 1
    assert spy_calls[0][0] is engine._kraken_book_feed
    assert spy_calls[0][1] is True
    assert wrapper._book_feed is engine._kraken_book_feed

    await engine.shutdown()
    assert sockets[0].closed is True
    assert wrapper._book_feed is None
    assert engine._kraken_book_feed is None


@pytest.mark.asyncio
async def test_2_connect_failure_blocks_open_market_and_never_attaches_feed(monkeypatch):
    async def _factory(*args, **kwargs):
        raise OSError("ws down")

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, rest, spy_calls = _make_engine(monkeypatch)

    ok = await engine.start_kraken_book_feed()
    assert ok is False
    assert wrapper._book_feed is None
    assert all(feed is None for feed, _ in spy_calls)

    res = await wrapper.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert res["success"] is False
    assert res["code"] == "MISSING_REFERENCE_PRICE"
    assert len(rest.sent_orders) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("frames", [
    # Missing heartbeat ACK
    [{"event": "subscribed", "feed": "book", "product_ids": ["PF_ETHUSD"]}],
    # Missing book ACK
    [{"event": "subscribed", "feed": "heartbeat"}],
])
async def test_3_4_missing_heartbeat_or_book_ack_blocks_open_market(monkeypatch, frames):
    ws = _SimWS(frames)

    async def _factory(*args, **kwargs):
        return ws

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, rest, _ = _make_engine(monkeypatch, kraken_symbols=["PF_ETHUSD"])

    # Use short ack timeout for fast test
    from backend.market_data.kraken_futures_book_feed import KrakenFuturesBookFeed
    engine._kraken_book_feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], subscribe_ack_timeout_sec=0.05)

    ok = await engine.start_kraken_book_feed()
    assert ok is False
    assert ws.closed is True
    assert wrapper._book_feed is None

    res = await wrapper.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert res["success"] is False
    assert res["code"] == "MISSING_REFERENCE_PRICE"
    assert len(rest.sent_orders) == 0


@pytest.mark.asyncio
async def test_5_6_7_missing_snapshot_seq_gap_and_disconnect_all_block_open_market(monkeypatch):
    ws = _SimWS([
        {"event": "subscribed", "feed": "heartbeat"},
        {"event": "subscribed", "feed": "book", "product_ids": ["PF_ETHUSD"]},
    ])

    async def _factory(*args, **kwargs):
        return ws

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, rest, _ = _make_engine(monkeypatch, kraken_symbols=["PF_ETHUSD"])
    assert await engine.start_kraken_book_feed() is True

    # 5. Connected & acked, but NO book_snapshot yet -> blocked
    r_nosnap = await wrapper.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert r_nosnap["success"] is False and r_nosnap["code"] == "MISSING_REFERENCE_PRICE"

    # Deliver snapshot -> OPEN MARKET allowed
    feed = engine._kraken_book_feed
    assert feed is not None
    assert feed.process_message({
        "feed": "book_snapshot", "product_id": "PF_ETHUSD", "seq": 10, "timestamp": 1700000000000,
        "bids": [{"price": 2999.5, "qty": 10.0}], "asks": [{"price": 3000.5, "qty": 10.0}],
    }) is True
    r_ok = await wrapper.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert r_ok["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(rest.sent_orders) == 1

    # 6. Sequence gap (seq 12 instead of 11) -> desync -> blocked
    assert feed.process_message({
        "feed": "book", "product_id": "PF_ETHUSD", "side": "sell",
        "seq": 12, "price": 3000.6, "qty": 5.0, "timestamp": 1700000000200,
    }) is False
    r_gap = await wrapper.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert r_gap["success"] is False and r_gap["code"] == "STALE_REFERENCE_PRICE"
    assert len(rest.sent_orders) == 1

    # 7. Disconnect -> invalidates book + triggers wrapper.on_ws_disconnect
    feed.on_disconnect("LINK_LOST")
    assert wrapper._global_entry_lock is True
    r_disc = await wrapper.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert r_disc["success"] is False
    assert len(rest.sent_orders) == 1

    await engine.shutdown()


@pytest.mark.asyncio
async def test_8_reconnection_stops_old_feed_and_requires_fresh_snapshot(monkeypatch):
    sockets: List[_SimWS] = []

    async def _factory(*args, **kwargs):
        ws = _SimWS([
            {"event": "subscribed", "feed": "heartbeat"},
            {"event": "subscribed", "feed": "book", "product_ids": ["PF_ETHUSD"]},
        ])
        sockets.append(ws)
        return ws

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, rest, _ = _make_engine(monkeypatch, kraken_symbols=["PF_ETHUSD"])
    assert await engine.start_kraken_book_feed() is True
    assert engine._kraken_book_feed.process_message({
        "feed": "book_snapshot", "product_id": "PF_ETHUSD", "seq": 1, "timestamp": 1700000000000,
        "bids": [{"price": 2999.5, "qty": 5.0}], "asks": [{"price": 3000.5, "qty": 5.0}],
    }) is True

    # Simulate disconnect & trigger _handle_reconnection
    engine._broker_status[BrokerType.KRAKEN] = BrokerStatus(broker=BrokerType.KRAKEN, connected=False)

    async def _fake_reconcile():
        wrapper._global_entry_lock = False
        wrapper._startup_reconciliation_completed = True
        return {"startup_secure": True, "global_entry_lock": False, "status": "CLEAN"}

    engine.run_kraken_startup_reconciliation = _fake_reconcile  # type: ignore[method-assign]

    await engine._handle_reconnection()

    # Old socket closed, new socket opened, old book NOT reused without fresh snapshot
    assert len(sockets) == 2
    assert sockets[0].closed is True
    assert wrapper._book_feed is engine._kraken_book_feed
    assert wrapper._book_feed.get_best_bid_ask("PF_ETHUSD")[0] is False

    r_blocked = await wrapper.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert r_blocked["success"] is False and r_blocked["code"] == "MISSING_REFERENCE_PRICE"
    assert len(rest.sent_orders) == 0

    await engine.shutdown()
    assert sockets[1].closed is True


@pytest.mark.asyncio
async def test_9_double_startup_and_fastapi_shutdown_zero_orphan_tasks(monkeypatch):
    sockets: List[_SimWS] = []

    async def _factory(*args, **kwargs):
        await asyncio.sleep(0.01)
        ws = _SimWS([
            {"event": "subscribed", "feed": "heartbeat"},
            {"event": "subscribed", "feed": "book", "product_ids": ["PF_ETHUSD"]},
        ])
        sockets.append(ws)
        return ws

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, _, _ = _make_engine(monkeypatch, kraken_symbols=["PF_ETHUSD"])

    # Concurrent double startup -> single socket, single feed, single _recv_task
    r1, r2 = await asyncio.gather(engine.start_kraken_book_feed(), engine.start_kraken_book_feed())
    assert r1 is True and r2 is True
    assert len(sockets) == 1
    recv_task = engine._kraken_book_feed._recv_task
    assert recv_task is not None and not recv_task.done()

    # FastAPI shutdown path calls engine.shutdown() -> cleanly stops feed and cancels _recv_task
    await engine.shutdown()
    assert sockets[0].closed is True
    assert recv_task.done() is True
    assert engine._kraken_book_feed is None
    assert wrapper._book_feed is None


@pytest.mark.asyncio
async def test_10_limit_close_reduce_unaffected_when_book_feed_down(monkeypatch):
    engine_l, w_l, r_l, _ = _make_engine(monkeypatch, rest_pos=0.0)
    assert w_l._book_feed is None
    res_lim = await w_l.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="limit",
        price=2990.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert res_lim["code"] == "UNIT_TEST_REACHED_REST_SEND" and len(r_l.sent_orders) == 1

    engine_c, w_c, r_c, _ = _make_engine(monkeypatch, rest_pos=0.05)
    w_c._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="O1", client_oid="C1", symbol="PF_ETHUSD", side="long",
        filled_qty=0.05, expected_exposure=0.05, entry_price=3000.0, status="OPEN",
    )
    res_red = await w_c.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.02, order_type="market",
        reduce_only=True, intent="reduce",
    )
    res_cls = await w_c.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.05, order_type="market",
        reduce_only=True, intent="close",
    )
    assert res_red["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert res_cls["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(r_c.sent_orders) == 2
