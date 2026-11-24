"""
Phase 1.9.8-R1 — Adversarial Tests for P2-1 (obsolete callback), P2-2 (spontaneous disconnect),
and P3 (CancelledError cleanup) in MultiBrokerEngine KrakenFuturesBookFeed wiring.

100% offline, simulated WebSocket, zero real Kraken calls, zero .env/SQLite mutations.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
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
    def __init__(self) -> None:
        self.sent_orders: List[Dict[str, Any]] = []

    async def get_positions(self, **kwargs):
        return []

    async def get_open_orders(self, **kwargs):
        return []

    async def get_tickers(self):
        # Deliberately present to verify NO REST fallback ever happens
        return {"PF_ETHUSD": {"symbol": "PF_ETHUSD", "bid": 2999.5, "ask": 3000.5, "mark_price": 3000.0}}


class _FakeAdapter:
    def __init__(self, wrapper: KrakenFuturesExecutorWrapper) -> None:
        self._wrapper = wrapper
        self._client = None

    def _get_wrapper(self) -> KrakenFuturesExecutorWrapper:
        return self._wrapper

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


def _make_engine(monkeypatch) -> tuple[MultiBrokerEngine, KrakenFuturesExecutorWrapper, _FakeRest]:
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", "50.0")

    rest = _FakeRest()
    wrapper = KrakenFuturesExecutorWrapper(api_key="t", api_secret="t", single_entry_mode=False)
    wrapper._rest_client = rest
    wrapper._global_entry_lock = False
    wrapper._global_entry_lock_reason = None
    wrapper._startup_reconciliation_completed = True

    async def _fake_send(**kwargs):
        rest.sent_orders.append(kwargs)
        return {"success": False, "code": "UNIT_TEST_REACHED_REST_SEND", "error": "ok"}

    wrapper._place_order_rest = _fake_send  # type: ignore[method-assign]

    cfg = MultiBrokerConfig(
        default_broker="kraken",
        bitget_enabled=False,
        kraken_enabled=True,
        kraken_symbols=["PF_ETHUSD"],
        reconnect_delay_sec=0.01,
        max_reconnect_attempts=1,
    )
    engine = MultiBrokerEngine(config=cfg, kraken_executor=_FakeAdapter(wrapper))
    return engine, wrapper, rest


def _ok_ws() -> _SimWS:
    return _SimWS([
        {"event": "subscribed", "feed": "heartbeat"},
        {"event": "subscribed", "feed": "book", "product_ids": ["PF_ETHUSD"]},
    ])


def _snap(seq: int = 10) -> Dict[str, Any]:
    return {
        "feed": "book_snapshot",
        "product_id": "PF_ETHUSD",
        "seq": seq,
        "timestamp": 1700000000000,
        "bids": [{"price": 2999.5, "qty": 10.0}],
        "asks": [{"price": 3000.5, "qty": 10.0}],
    }


@pytest.mark.asyncio
async def test_1_obsolete_callback_after_feed_replacement_has_zero_side_effects(monkeypatch):
    async def _factory(*a, **kw):
        return _ok_ws()

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, rest = _make_engine(monkeypatch)

    assert await engine.start_kraken_book_feed() is True
    old_feed = engine._kraken_book_feed
    old_cb = old_feed.on_disconnect_callback
    assert old_cb is not None

    await engine.stop_kraken_book_feed()
    assert old_feed.on_disconnect_callback is None  # disarmed!

    # Start fresh healthy feed
    wrapper._global_entry_lock = False
    wrapper._startup_reconciliation_completed = True
    assert await engine.start_kraken_book_feed() is True
    new_feed = engine._kraken_book_feed
    assert new_feed is not None and new_feed is not old_feed
    assert new_feed.process_message(_snap(seq=1)) is True

    # Invoke both old_feed.on_disconnect AND the captured old_cb directly -> ZERO effect on new_feed / wrapper!
    old_feed.on_disconnect("LATE_OLD_DROP")
    old_cb("STALE_CLOSURE_DROP")

    assert wrapper._global_entry_lock is False
    assert wrapper._book_feed is new_feed
    assert engine._kraken_book_feed is new_feed
    assert new_feed.get_best_bid_ask("PF_ETHUSD")[0] is True

    res = await wrapper.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert res["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(rest.sent_orders) == 1

    await engine.shutdown()


@pytest.mark.asyncio
async def test_2_obsolete_callback_during_reconnection_ignored(monkeypatch):
    async def _factory(*a, **kw):
        return _ok_ws()

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, _ = _make_engine(monkeypatch)
    assert await engine.start_kraken_book_feed() is True
    old_cb = engine._kraken_book_feed.on_disconnect_callback

    engine._broker_status[BrokerType.KRAKEN] = BrokerStatus(broker=BrokerType.KRAKEN, connected=False)

    async def _fake_reconcile():
        # Fire obsolete callback right during reconciliation!
        old_cb("OBSOLETE_DURING_RECON")
        wrapper._global_entry_lock = False
        wrapper._startup_reconciliation_completed = True
        return {"startup_secure": True, "global_entry_lock": False, "status": "CLEAN"}

    engine.run_kraken_startup_reconciliation = _fake_reconcile  # type: ignore[method-assign]
    await engine._handle_reconnection()

    assert wrapper._global_entry_lock is False
    assert wrapper._book_feed is engine._kraken_book_feed
    assert engine._kraken_book_feed is not None

    await engine.shutdown()


@pytest.mark.asyncio
async def test_3_spontaneous_disconnect_locks_entries_and_atomically_detaches_wrapper(monkeypatch):
    async def _factory(*a, **kw):
        return _ok_ws()

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, rest = _make_engine(monkeypatch)
    assert await engine.start_kraken_book_feed() is True
    active_feed = engine._kraken_book_feed
    assert wrapper._book_feed is active_feed
    assert active_feed.process_message(_snap(seq=1)) is True

    # Spontaneous disconnect of the active feed
    active_feed.on_disconnect("SPONTANEOUS_DROP")

    # P2-2 invariants: immediate global entry lock + atomic detach of wrapper._book_feed + engine._kraken_book_feed cleared
    assert wrapper._global_entry_lock is True
    assert wrapper._book_feed is None
    assert engine._kraken_book_feed is None

    # OPEN MARKET strictly refused (no REST fallback!)
    res = await wrapper.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert res["success"] is False
    assert res["code"] == "MISSING_REFERENCE_PRICE"
    assert len(rest.sent_orders) == 0


@pytest.mark.asyncio
async def test_4_spontaneous_disconnect_simultaneous_with_shutdown(monkeypatch):
    sockets: List[_SimWS] = []

    async def _factory(*a, **kw):
        ws = _ok_ws()
        sockets.append(ws)
        return ws

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, _ = _make_engine(monkeypatch)
    assert await engine.start_kraken_book_feed() is True
    active_feed = engine._kraken_book_feed

    # Trigger spontaneous disconnect right alongside shutdown()
    active_feed.on_disconnect("RACE_DROP")
    await asyncio.gather(engine.shutdown(), engine.shutdown())

    assert wrapper._book_feed is None
    assert engine._kraken_book_feed is None
    assert wrapper._global_entry_lock is True


@pytest.mark.asyncio
async def test_5_disconnect_simultaneous_with_replacement_never_detaches_new_feed(monkeypatch):
    async def _factory(*a, **kw):
        return _ok_ws()

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, _ = _make_engine(monkeypatch)
    assert await engine.start_kraken_book_feed() is True
    feed1 = engine._kraken_book_feed
    cb1 = feed1.on_disconnect_callback

    # Replace with feed2
    await engine.stop_kraken_book_feed()
    wrapper._global_entry_lock = False
    wrapper._startup_reconciliation_completed = True
    assert await engine.start_kraken_book_feed() is True
    feed2 = engine._kraken_book_feed
    assert feed2 is not feed1

    # Late disconnect from feed1 cannot detach feed2!
    cb1("RACE_AFTER_REPLACE")
    assert wrapper._book_feed is feed2
    assert engine._kraken_book_feed is feed2
    assert wrapper._global_entry_lock is False

    await engine.shutdown()


@pytest.mark.asyncio
async def test_6_obsolete_callback_after_detach_is_noop(monkeypatch):
    async def _factory(*a, **kw):
        return _ok_ws()

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, _ = _make_engine(monkeypatch)
    assert await engine.start_kraken_book_feed() is True
    cb = engine._kraken_book_feed.on_disconnect_callback

    await engine.stop_kraken_book_feed()
    wrapper._global_entry_lock = False
    wrapper._startup_reconciliation_completed = True

    # Calling saved callback after detach must NOT re-lock wrapper
    cb("POST_DETACH_CALL")
    assert wrapper._global_entry_lock is False
    assert wrapper._book_feed is None


@pytest.mark.asyncio
async def test_7_cancellation_during_ws_connect_cleans_up_and_reraises(monkeypatch):
    connect_entered = asyncio.Event()

    async def _factory(*a, **kw):
        connect_entered.set()
        await asyncio.sleep(10.0)
        return _ok_ws()

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, rest = _make_engine(monkeypatch)

    task = asyncio.create_task(engine.start_kraken_book_feed())
    await connect_entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # P3 invariants: no partial reference left behind, wrapper detached, OPEN MARKET blocked
    assert engine._kraken_book_feed is None
    assert wrapper._book_feed is None
    res = await wrapper.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert res["success"] is False and res["code"] == "MISSING_REFERENCE_PRICE"
    assert len(rest.sent_orders) == 0


@pytest.mark.asyncio
async def test_8_cancellation_after_ws_connect_before_attach_cleans_socket_and_reraises(monkeypatch):
    ack_wait_entered = asyncio.Event()
    ws = _SimWS([])  # never sends ACK

    async def _factory(*a, **kw):
        ack_wait_entered.set()
        return ws

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, rest = _make_engine(monkeypatch)

    task = asyncio.create_task(engine.start_kraken_book_feed())
    await ack_wait_entered.wait()
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert ws.closed is True
    assert engine._kraken_book_feed is None
    assert wrapper._book_feed is None
    assert len(rest.sent_orders) == 0


@pytest.mark.asyncio
async def test_9_double_shutdown_idempotent_zero_orphan_tasks(monkeypatch):
    sockets: List[_SimWS] = []

    async def _factory(*a, **kw):
        ws = _ok_ws()
        sockets.append(ws)
        return ws

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, _ = _make_engine(monkeypatch)
    assert await engine.start_kraken_book_feed() is True
    recv_task = engine._kraken_book_feed._recv_task

    await asyncio.gather(engine.shutdown(), engine.shutdown())
    await engine.shutdown()

    assert sockets[0].closed is True
    assert recv_task is not None and recv_task.done()
    assert engine._kraken_book_feed is None
    assert wrapper._book_feed is None


@pytest.mark.asyncio
async def test_10_concurrent_double_reconnection_serialized_cleanly(monkeypatch):
    sockets: List[_SimWS] = []

    async def _factory(*a, **kw):
        ws = _ok_ws()
        sockets.append(ws)
        return ws

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, _ = _make_engine(monkeypatch)
    assert await engine.start_kraken_book_feed() is True

    async def _fake_reconcile():
        wrapper._global_entry_lock = False
        wrapper._startup_reconciliation_completed = True
        return {"startup_secure": True, "global_entry_lock": False, "status": "CLEAN"}

    engine.run_kraken_startup_reconciliation = _fake_reconcile  # type: ignore[method-assign]
    engine._broker_status[BrokerType.KRAKEN] = BrokerStatus(broker=BrokerType.KRAKEN, connected=False)

    await asyncio.gather(engine._handle_reconnection(), engine._handle_reconnection())

    # Serialized cleanly: first call reconnected & set connected=True; second saw connected=True and cleaned cleanly
    await engine.shutdown()
    assert all(s.closed for s in sockets)
    assert engine._kraken_book_feed is None
    assert wrapper._book_feed is None


@pytest.mark.asyncio
async def test_11_12_new_healthy_feed_after_old_disconnect_requires_fresh_snapshot_no_rest_fallback(monkeypatch):
    async def _factory(*a, **kw):
        return _ok_ws()

    _install_fake_ws(monkeypatch, _factory)
    engine, wrapper, rest = _make_engine(monkeypatch)

    assert await engine.start_kraken_book_feed() is True
    old_feed = engine._kraken_book_feed
    old_feed.on_disconnect("OLD_DROP")
    assert wrapper._book_feed is None
    assert engine._kraken_book_feed is None

    # Simulate clean reconciliation + new feed start
    wrapper._global_entry_lock = False
    wrapper._startup_reconciliation_completed = True
    assert await engine.start_kraken_book_feed() is True
    new_feed = engine._kraken_book_feed
    assert new_feed is not None and new_feed is not old_feed

    # 12. Without snapshot -> OPEN MARKET MUST stay blocked (no REST fallback!)
    r_before = await wrapper.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert r_before["success"] is False and r_before["code"] == "MISSING_REFERENCE_PRICE"
    assert len(rest.sent_orders) == 0

    # Deliver fresh synchronized snapshot -> OPEN MARKET succeeds
    assert new_feed.process_message(_snap(seq=99)) is True
    r_after = await wrapper.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=3000.0, stop_loss=2950.0, reduce_only=False, intent="open",
    )
    assert r_after["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(rest.sent_orders) == 1

    await engine.shutdown()
