"""
Phase 1.9.7.2 — KrakenFuturesBookFeed Lifecycle Serialization & Explicit Return Tests.

100% offline, simulated WebSocket, zero network calls, zero SQLite/.env mutations.
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
from backend.market_data.kraken_futures_book_feed import KrakenFuturesBookFeed


class _SimWS:
    """Controllable async fake WebSocket for lifecycle/concurrency tests."""

    def __init__(
        self,
        incoming: Optional[List[Dict[str, Any]]] = None,
        *,
        fail_send_on: Optional[int] = None,
        hold_after_incoming: bool = True,
        send_delay: float = 0.0,
    ) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        for m in (incoming or []):
            self.queue.put_nowait(json.dumps(m))
        self.sent: List[Dict[str, Any]] = []
        self.fail_send_on = fail_send_on
        self.hold_after_incoming = hold_after_incoming
        self.send_delay = send_delay
        self.closed = False
        self.close_count = 0

    async def send(self, payload: str) -> None:
        if self.send_delay > 0:
            await asyncio.sleep(self.send_delay)
        self.sent.append(json.loads(payload))
        if self.fail_send_on is not None and len(self.sent) == self.fail_send_on:
            raise RuntimeError(f"Simulated send failure #{self.fail_send_on}")

    async def recv(self) -> str:
        if not self.hold_after_incoming and self.queue.empty():
            raise RuntimeError("EOF")
        return await self.queue.get()

    async def close(self) -> None:
        self.closed = True
        self.close_count += 1


def _install_fake_websockets(monkeypatch, factory):
    fake_mod = types.ModuleType("websockets")

    class _FakeConnClosed(Exception):
        pass

    fake_mod.ConnectionClosed = _FakeConnClosed  # type: ignore[attr-defined]
    fake_mod.connect = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websockets", fake_mod)
    return fake_mod


def _ack_frames(symbols: List[str], *, include_snapshot: bool = False) -> List[Dict[str, Any]]:
    frames: List[Dict[str, Any]] = [
        {"event": "info", "version": 1},
        {"event": "subscribed", "feed": "heartbeat"},
        {"event": "subscribed", "feed": "book", "product_ids": list(symbols)},
    ]
    if include_snapshot:
        frames.append({
            "feed": "book_snapshot",
            "product_id": symbols[0],
            "seq": 10,
            "timestamp": 1700000000000,
            "bids": [{"price": 2999.5, "qty": 10.0}],
            "asks": [{"price": 3000.5, "qty": 10.0}],
        })
    return frames


@pytest.mark.asyncio
async def test_1_concurrent_connect_calls_create_single_socket_and_single_recv_task(monkeypatch):
    created: List[_SimWS] = []

    async def _factory(*args, **kwargs):
        await asyncio.sleep(0.02)
        ws = _SimWS(_ack_frames(["PF_ETHUSD"]))
        created.append(ws)
        return ws

    _install_fake_websockets(monkeypatch, _factory)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], subscribe_ack_timeout_sec=1.0)

    r1, r2 = await asyncio.gather(feed.connect(), feed.connect())
    assert r1 is True
    assert r2 is True
    assert len(created) == 1
    assert feed._running is True
    assert feed._connected is True
    assert feed._recv_task is not None and not feed._recv_task.done()

    await feed.disconnect()
    assert created[0].closed is True
    assert feed._running is False
    assert feed._connected is False
    assert feed._recv_task is None


@pytest.mark.asyncio
async def test_2_connect_returns_true_only_after_protocol_subscribed_confirmations_and_replays_early_snapshot(monkeypatch):
    # Early book_snapshot arrives interleaved BEFORE book subscribed ack
    frames = [
        {"event": "subscribed", "feed": "heartbeat"},
        {
            "feed": "book_snapshot",
            "product_id": "PF_ETHUSD",
            "seq": 42,
            "timestamp": 1700000000000,
            "bids": [{"price": 2999.5, "qty": 5.0}],
            "asks": [{"price": 3000.5, "qty": 5.0}],
        },
        {"event": "subscribed", "feed": "book", "product_ids": ["PF_ETHUSD"]},
    ]
    ws = _SimWS(frames)

    async def _factory(*args, **kwargs):
        return ws

    _install_fake_websockets(monkeypatch, _factory)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], subscribe_ack_timeout_sec=1.0)

    ok = await feed.connect()
    assert ok is True
    assert feed.is_connected is True
    # Early snapshot was preserved & applied!
    q_ok, q, err = feed.get_best_bid_ask("PF_ETHUSD")
    assert q_ok is True and err is None and q["ask"] == 3000.5 and q["seq"] == 42

    await feed.disconnect()


@pytest.mark.asyncio
async def test_3_connect_returns_false_on_transport_error(monkeypatch):
    async def _factory(*args, **kwargs):
        raise OSError("refused")

    _install_fake_websockets(monkeypatch, _factory)
    disc: List[str] = []
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], on_disconnect_callback=lambda r: disc.append(r))

    ok = await feed.connect()
    assert ok is False
    assert feed._running is False
    assert feed._connected is False
    assert feed._ws is None
    assert feed._recv_task is None
    assert len(disc) == 1 and "CONNECT_FAILED" in disc[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_frame", [
    {"event": "error", "message": "Invalid product_id"},
    {"event": "alert", "message": "Service unavailable"},
    {"event": "unsubscribed", "feed": "book", "product_ids": ["PF_ETHUSD"]},
])
async def test_4_connect_returns_false_when_subscription_rejected(monkeypatch, reject_frame):
    ws = _SimWS([{"event": "subscribed", "feed": "heartbeat"}, reject_frame])

    async def _factory(*args, **kwargs):
        return ws

    _install_fake_websockets(monkeypatch, _factory)
    disc: List[str] = []
    feed = KrakenFuturesBookFeed(
        symbols=["PF_ETHUSD"],
        subscribe_ack_timeout_sec=1.0,
        on_disconnect_callback=lambda r: disc.append(r),
    )

    ok = await feed.connect()
    assert ok is False
    assert ws.closed is True
    assert feed._running is False
    assert feed._connected is False
    assert feed._ws is None
    assert feed._recv_task is None
    assert len(disc) == 1


@pytest.mark.asyncio
async def test_5_connect_returns_false_on_missing_subscription_ack_timeout(monkeypatch):
    # Only heartbeat ack arrives; book ack never arrives -> timeout
    ws = _SimWS([{"event": "subscribed", "feed": "heartbeat"}])

    async def _factory(*args, **kwargs):
        return ws

    _install_fake_websockets(monkeypatch, _factory)
    disc: List[str] = []
    feed = KrakenFuturesBookFeed(
        symbols=["PF_ETHUSD"],
        subscribe_ack_timeout_sec=0.05,
        on_disconnect_callback=lambda r: disc.append(r),
    )

    ok = await feed.connect()
    assert ok is False
    assert ws.closed is True
    assert feed._running is False
    assert feed._connected is False
    assert feed._ws is None
    assert feed._recv_task is None
    assert len(disc) == 1


@pytest.mark.asyncio
async def test_6_disconnect_during_connect_serializes_deterministically_no_deadlock(monkeypatch):
    connect_started = asyncio.Event()
    allow_connect_finish = asyncio.Event()
    ws = _SimWS(_ack_frames(["PF_ETHUSD"], include_snapshot=True))

    async def _factory(*args, **kwargs):
        connect_started.set()
        await allow_connect_finish.wait()
        return ws

    _install_fake_websockets(monkeypatch, _factory)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], subscribe_ack_timeout_sec=1.0)

    t_conn = asyncio.create_task(feed.connect())
    await connect_started.wait()
    t_disc = asyncio.create_task(feed.disconnect())
    await asyncio.sleep(0.01)
    allow_connect_finish.set()

    conn_res = await asyncio.wait_for(t_conn, timeout=2.0)
    await asyncio.wait_for(t_disc, timeout=2.0)

    assert conn_res is True
    # Because disconnect acquired the lock immediately after connect, final state MUST be cleanly disconnected
    assert ws.closed is True
    assert feed._running is False
    assert feed._connected is False
    assert feed._ws is None
    assert feed._recv_task is None
    assert feed.get_best_bid_ask("PF_ETHUSD")[0] is False


@pytest.mark.asyncio
async def test_7_cancellation_during_connect_cleans_up_without_deadlock(monkeypatch):
    started = asyncio.Event()
    ws = _SimWS([])  # never sends ack

    async def _factory(*args, **kwargs):
        started.set()
        return ws

    _install_fake_websockets(monkeypatch, _factory)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], subscribe_ack_timeout_sec=5.0)

    t = asyncio.create_task(feed.connect())
    await started.wait()
    await asyncio.sleep(0.01)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t

    assert ws.closed is True
    assert feed._running is False
    assert feed._connected is False
    assert feed._ws is None
    assert feed._recv_task is None

    # Subsequent disconnect must NOT deadlock
    await asyncio.wait_for(feed.disconnect(), timeout=1.0)


@pytest.mark.asyncio
async def test_8_reconnect_closes_previous_socket_and_never_reuses_old_book(monkeypatch):
    sockets: List[_SimWS] = []

    async def _factory(*args, **kwargs):
        ws = _SimWS(_ack_frames(["PF_ETHUSD"]))
        sockets.append(ws)
        return ws

    _install_fake_websockets(monkeypatch, _factory)
    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"], subscribe_ack_timeout_sec=1.0)

    assert await feed.connect() is True
    assert feed.process_message({
        "feed": "book_snapshot", "product_id": "PF_ETHUSD", "seq": 1, "timestamp": 1700000000000,
        "bids": [{"price": 2999.0, "qty": 5.0}], "asks": [{"price": 3001.0, "qty": 5.0}],
    }) is True
    assert feed.get_best_bid_ask("PF_ETHUSD")[0] is True

    await feed.disconnect()
    assert sockets[0].closed is True

    # Reconnect: new socket, old book MUST still be invalid until fresh snapshot arrives
    assert await feed.connect() is True
    assert len(sockets) == 2
    assert feed.get_best_bid_ask("PF_ETHUSD")[0] is False

    # Fresh snapshot restores validity
    assert feed.process_message({
        "feed": "book_snapshot", "product_id": "PF_ETHUSD", "seq": 50, "timestamp": 1700000001000,
        "bids": [{"price": 3000.0, "qty": 5.0}], "asks": [{"price": 3000.5, "qty": 5.0}],
    }) is True
    ok, q, _ = feed.get_best_bid_ask("PF_ETHUSD")
    assert ok is True and q["seq"] == 50

    await feed.disconnect()
    assert sockets[1].closed is True


def test_9_limit_close_reduce_still_unaffected(monkeypatch):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "50.0")

    class _Rest:
        def __init__(self, pos: float) -> None:
            self.pos = pos
            self.sent: List[Dict[str, Any]] = []

        async def get_positions(self, **kwargs):
            return [{"symbol": "PF_ETHUSD", "side": "long", "size": self.pos}] if self.pos > 0 else []

        async def get_open_orders(self, **kwargs):
            return []

    def _mk(pos: float):
        c = _Rest(pos)
        w = KrakenFuturesExecutorWrapper(api_key="t", api_secret="t", single_entry_mode=False)
        w._rest_client = c
        w._global_entry_lock = False
        w._startup_reconciliation_completed = True
        async def _send(**kw):
            c.sent.append(kw)
            return {"success": False, "code": "UNIT_TEST_REACHED_REST_SEND", "error": "ok"}
        w._place_order_rest = _send
        return w, c

    w_lim, c_lim = _mk(0.0)
    r_lim = asyncio.run(w_lim.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="limit",
        price=2990.0, stop_loss=2950.0, reduce_only=False, intent="open",
    ))
    assert r_lim["code"] == "UNIT_TEST_REACHED_REST_SEND" and len(c_lim.sent) == 1

    w_cr, c_cr = _mk(0.05)
    w_cr._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="O1", client_oid="C1", symbol="PF_ETHUSD", side="long",
        filled_qty=0.05, expected_exposure=0.05, entry_price=3000.0, status="OPEN",
    )
    assert asyncio.run(w_cr.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.02, order_type="market",
        reduce_only=True, intent="reduce",
    ))["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert asyncio.run(w_cr.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.05, order_type="market",
        reduce_only=True, intent="close",
    ))["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(c_cr.sent) == 2
