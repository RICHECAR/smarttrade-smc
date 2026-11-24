"""
Kraken Futures WebSocket v1 Order Book Feed (Fail-Closed)
═════════════════════════════════════════════════════════

Implements a strict, fail-closed local order book synchronizer for the
Kraken Futures WebSocket v1 public ``book`` and ``heartbeat`` feeds
(``wss://futures.kraken.com/ws/v1``).

Protocol reality (verified against Kraken Futures WS v1 specification):
  - Subscription:
      {"event": "subscribe", "feed": "book", "product_ids": [...]}
      {"event": "subscribe", "feed": "heartbeat"}
  - Initial snapshot: ``feed == "book_snapshot"`` with ``product_id``,
    ``seq``, ``timestamp`` (ms), ``bids`` (list of {price, qty}),
    ``asks`` (list of {price, qty}).
  - Incremental updates: ``feed == "book"`` with ``product_id``, ``side``
    ("buy"/"sell"), ``seq`` (strictly monotonic +1), ``price``, ``qty``
    (0 removes level), ``timestamp`` (ms).
  - Liveness: ``feed == "heartbeat"`` (and WS ping/pong / valid frames).
    An absence of ``book`` delta updates for a quiet symbol does NOT
    invalidate a synchronized book as long as the WS connection and
    heartbeat stream remain healthy.
  - Checksum: Kraken Futures WS v1 ``book`` feed does NOT include a
    checksum field (unlike Kraken Spot v2). Integrity is enforced strictly
    via initial snapshot requirement, contiguous ``seq`` (+1), malformed
    frame rejection, non-crossed book validation, and connection/heartbeat
    liveness.

Phase 1.9.7 / Lot 1 (Option C).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("smarttrade.kraken_futures_book_feed")

KRAKEN_FUTURES_WS_URL = "wss://futures.kraken.com/ws/v1"
DEFAULT_HEARTBEAT_TIMEOUT_SEC = 30.0
_MAX_PENDING_DELTAS = 500


def _is_strict_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def _parse_finite_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


@dataclass
class SymbolBookState:
    """Synchronized local order book state for a single Kraken Futures instrument."""

    product_id: str
    bids: Dict[float, float] = field(default_factory=dict)
    asks: Dict[float, float] = field(default_factory=dict)
    snapshot_received: bool = False
    synced: bool = False
    last_seq: Optional[int] = None
    last_event_timestamp_ms: Optional[int] = None
    last_update_monotonic: Optional[float] = None
    desync_reason: Optional[str] = None
    pending_deltas: List[Dict[str, Any]] = field(default_factory=list)

    def invalidate(self, reason: str, clear_snapshot: bool = False) -> None:
        """Fail-closed invalidation of this symbol's order book."""
        self.synced = False
        if clear_snapshot:
            self.snapshot_received = False
        self.desync_reason = reason
        self.bids.clear()
        self.asks.clear()
        self.pending_deltas.clear()


class KrakenFuturesBookFeed:
    """
    Fail-closed Kraken Futures WebSocket v1 ``book`` + ``heartbeat`` synchronizer.

    Can be driven either by a live asyncio WebSocket connection (``connect()``)
    or deterministically offline via ``on_connected()``, ``process_message()``,
    and ``on_disconnect()`` for unit/integration testing.
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        ws_url: str = KRAKEN_FUTURES_WS_URL,
        heartbeat_timeout_sec: float = DEFAULT_HEARTBEAT_TIMEOUT_SEC,
        subscribe_ack_timeout_sec: float = 5.0,
        clock: Optional[Callable[[], float]] = None,
        on_disconnect_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._symbols: List[str] = [s.upper() for s in (symbols or [])]
        self._ws_url: str = ws_url
        if isinstance(heartbeat_timeout_sec, bool) or not math.isfinite(float(heartbeat_timeout_sec)) or float(heartbeat_timeout_sec) <= 0:
            raise ValueError(f"heartbeat_timeout_sec must be finite and > 0, got {heartbeat_timeout_sec!r}")
        if isinstance(subscribe_ack_timeout_sec, bool) or not math.isfinite(float(subscribe_ack_timeout_sec)) or float(subscribe_ack_timeout_sec) <= 0:
            raise ValueError(f"subscribe_ack_timeout_sec must be finite and > 0, got {subscribe_ack_timeout_sec!r}")
        self._heartbeat_timeout_sec: float = float(heartbeat_timeout_sec)
        self._subscribe_ack_timeout_sec: float = float(subscribe_ack_timeout_sec)
        self._clock: Callable[[], float] = clock or time.monotonic
        self.on_disconnect_callback: Optional[Callable[[str], None]] = on_disconnect_callback

        self._books: Dict[str, SymbolBookState] = {}
        self._connected: bool = False
        self._last_liveness_monotonic: Optional[float] = None
        self._ws: Any = None
        self._running: bool = False
        self._recv_task: Optional[asyncio.Task] = None
        self._lifecycle_lock: asyncio.Lock = asyncio.Lock()

    # ──────────────────────────────────────────────────────────
    #  STATE & LIVENESS
    # ──────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def heartbeat_timeout_sec(self) -> float:
        return self._heartbeat_timeout_sec

    def is_alive(self) -> Tuple[bool, Optional[str]]:
        """Check whether the WS connection and heartbeat/liveness stream are healthy."""
        if not self._connected:
            return False, "Kraken WS book feed is not connected"
        if self._last_liveness_monotonic is None:
            return False, "Kraken WS book feed has no liveness timestamp"
        age = self._clock() - self._last_liveness_monotonic
        if age < 0:
            return False, f"Kraken WS book feed clock anomaly (age={age:.3f}s)"
        if age > self._heartbeat_timeout_sec:
            return False, (
                f"Kraken WS book feed heartbeat/liveness timeout "
                f"(age={age:.2f}s > max={self._heartbeat_timeout_sec:.2f}s)"
            )
        return True, None

    def on_connected(self) -> None:
        """Mark WebSocket connection established and initialize liveness clock."""
        self._connected = True
        self._last_liveness_monotonic = self._clock()

    def on_disconnect(self, reason: str = "WS_DISCONNECTED") -> None:
        """Fail-closed disconnect handler: invalidates all synchronized order books."""
        self._connected = False
        for state in self._books.values():
            state.invalidate(reason=f"DISCONNECTED: {reason}", clear_snapshot=True)
        logger.warning("[KRAKEN_BOOK_FEED] Disconnected (%s) — all order books invalidated", reason)
        if self.on_disconnect_callback is not None:
            try:
                self.on_disconnect_callback(reason)
            except Exception as exc:
                logger.error("[KRAKEN_BOOK_FEED] on_disconnect_callback error: %s", exc)

    def get_book_state(self, symbol: str) -> Optional[SymbolBookState]:
        return self._books.get(symbol.upper())

    def _get_or_create_state(self, product_id: str) -> SymbolBookState:
        key = product_id.upper()
        if key not in self._books:
            self._books[key] = SymbolBookState(product_id=key)
        return self._books[key]

    # ──────────────────────────────────────────────────────────
    #  PROTOCOL MESSAGE PROCESSING
    # ──────────────────────────────────────────────────────────

    def process_message(self, data: Any) -> bool:
        """
        Process a single decoded JSON message from Kraken Futures WS v1.

        Returns True if the message was valid and processed cleanly,
        False if malformed or if it triggered a fail-closed desync.
        """
        if not isinstance(data, dict):
            logger.error("[KRAKEN_BOOK_FEED] Malformed non-dict message: %r", type(data).__name__)
            return False

        now = self._clock()
        feed = data.get("feed")
        event = data.get("event")

        # Phase 1.9.7.1 (P2-1): Handle unsubscribed events fail-closed BEFORE liveness checks
        # Never treat an unsubscribed event as a healthy liveness signal.
        if event == "unsubscribed":
            if feed == "heartbeat":
                for state in self._books.values():
                    state.invalidate("UNSUBSCRIBED: heartbeat", clear_snapshot=True)
                logger.warning("[KRAKEN_BOOK_FEED] Heartbeat feed unsubscribed — all order books invalidated")
                return False

            if feed == "book" or feed is None:
                target_ids: List[str] = []
                raw_pids = data.get("product_ids")
                if isinstance(raw_pids, list):
                    for item in raw_pids:
                        if isinstance(item, str) and item.strip():
                            target_ids.append(item.strip().upper())
                raw_pid = data.get("product_id")
                if isinstance(raw_pid, str) and raw_pid.strip():
                    key = raw_pid.strip().upper()
                    if key not in target_ids:
                        target_ids.append(key)

                if target_ids:
                    for sym in target_ids:
                        if sym in self._books:
                            self._books[sym].invalidate("UNSUBSCRIBED: book", clear_snapshot=True)
                        else:
                            self._get_or_create_state(sym).invalidate("UNSUBSCRIBED: book", clear_snapshot=True)
                    logger.warning(
                        "[KRAKEN_BOOK_FEED] Book feed unsubscribed for %s — targeted order book(s) invalidated",
                        target_ids,
                    )
                else:
                    for state in self._books.values():
                        state.invalidate("UNSUBSCRIBED: book (all)", clear_snapshot=True)
                    logger.warning(
                        "[KRAKEN_BOOK_FEED] Book feed unsubscribed without product_ids — all order books invalidated"
                    )
                return False

            logger.warning("[KRAKEN_BOOK_FEED] Unsubscribed event for feed=%r", feed)
            return False

        # Heartbeat & protocol liveness frames
        if feed == "heartbeat" or event in ("pong", "info", "subscribed"):
            if self._connected:
                self._last_liveness_monotonic = now
            return True

        # Protocol error / alert frames
        if event in ("error", "alert"):
            msg = str(data.get("message") or data.get("error") or event)
            pid = data.get("product_id")
            if isinstance(pid, str) and pid.strip():
                self._get_or_create_state(pid.strip()).invalidate(f"PROTOCOL_ERROR: {msg}")
            else:
                for state in self._books.values():
                    state.invalidate(f"PROTOCOL_ERROR: {msg}")
            logger.error("[KRAKEN_BOOK_FEED] Protocol %s: %s", event, msg)
            return False

        if feed == "book_snapshot":
            return self._handle_book_snapshot(data, now)

        if feed == "book":
            return self._handle_book_delta(data, now)

        # Unrelated feed frame on shared socket: ignore without mutating book state
        return True

    def _handle_book_snapshot(self, data: Dict[str, Any], now: float) -> bool:
        pid = data.get("product_id")
        if not isinstance(pid, str) or not pid.strip():
            logger.error("[KRAKEN_BOOK_FEED] Snapshot missing valid product_id")
            return False
        state = self._get_or_create_state(pid.strip())

        seq = data.get("seq")
        if not _is_strict_int(seq) or seq < 0:
            state.invalidate(f"INVALID_SNAPSHOT_SEQ: {seq!r}")
            return False

        ts_raw = data.get("timestamp")
        ts_val = _parse_finite_float(ts_raw)
        if ts_val is None or ts_val <= 0:
            state.invalidate(f"INVALID_SNAPSHOT_TIMESTAMP: {ts_raw!r}")
            return False

        raw_bids = data.get("bids")
        raw_asks = data.get("asks")
        if not isinstance(raw_bids, list) or not isinstance(raw_asks, list):
            state.invalidate("INVALID_SNAPSHOT_LEVELS: bids/asks must be lists")
            return False

        parsed_bids: Dict[float, float] = {}
        for level in raw_bids:
            if not isinstance(level, dict):
                state.invalidate("INVALID_SNAPSHOT_BID_ENTRY")
                return False
            p = _parse_finite_float(level.get("price"))
            q = _parse_finite_float(level.get("qty"))
            if p is None or p <= 0 or q is None or q < 0:
                state.invalidate(f"INVALID_SNAPSHOT_BID_VALUES: price={level.get('price')!r} qty={level.get('qty')!r}")
                return False
            if q > 0:
                parsed_bids[p] = q

        parsed_asks: Dict[float, float] = {}
        for level in raw_asks:
            if not isinstance(level, dict):
                state.invalidate("INVALID_SNAPSHOT_ASK_ENTRY")
                return False
            p = _parse_finite_float(level.get("price"))
            q = _parse_finite_float(level.get("qty"))
            if p is None or p <= 0 or q is None or q < 0:
                state.invalidate(f"INVALID_SNAPSHOT_ASK_VALUES: price={level.get('price')!r} qty={level.get('qty')!r}")
                return False
            if q > 0:
                parsed_asks[p] = q

        # Crossed book check on snapshot
        if parsed_bids and parsed_asks and max(parsed_bids.keys()) >= min(parsed_asks.keys()):
            state.invalidate(
                f"CROSSED_SNAPSHOT_BOOK: best_bid={max(parsed_bids.keys())} >= best_ask={min(parsed_asks.keys())}"
            )
            return False

        pending = list(state.pending_deltas)
        state.bids = parsed_bids
        state.asks = parsed_asks
        state.snapshot_received = True
        state.synced = True
        state.last_seq = seq
        state.last_event_timestamp_ms = int(ts_val)
        state.last_update_monotonic = now
        state.desync_reason = None
        state.pending_deltas.clear()
        if self._connected:
            self._last_liveness_monotonic = now

        # Replay any buffered deltas strictly newer than snapshot seq
        if pending:
            newer = [d for d in pending if _is_strict_int(d.get("seq")) and d["seq"] > seq]
            newer.sort(key=lambda d: d["seq"])
            for delta in newer:
                if not self._apply_validated_delta(state, delta, now):
                    return False

        return True

    def _handle_book_delta(self, data: Dict[str, Any], now: float) -> bool:
        pid = data.get("product_id")
        if not isinstance(pid, str) or not pid.strip():
            logger.error("[KRAKEN_BOOK_FEED] Delta missing valid product_id")
            return False
        state = self._get_or_create_state(pid.strip())

        # Validate structure strictly before touching state
        seq = data.get("seq")
        if not _is_strict_int(seq) or seq < 0:
            state.invalidate(f"INVALID_DELTA_SEQ: {seq!r}")
            return False

        raw_side = data.get("side")
        if not isinstance(raw_side, str) or raw_side.strip().lower() not in ("buy", "bid", "sell", "ask"):
            state.invalidate(f"INVALID_DELTA_SIDE: {raw_side!r}")
            return False

        price = _parse_finite_float(data.get("price"))
        qty = _parse_finite_float(data.get("qty"))
        if price is None or price <= 0 or qty is None or qty < 0:
            state.invalidate(f"INVALID_DELTA_PRICE_QTY: price={data.get('price')!r} qty={data.get('qty')!r}")
            return False

        if "timestamp" in data and data["timestamp"] is not None:
            ts_val = _parse_finite_float(data["timestamp"])
            if ts_val is None or ts_val <= 0:
                state.invalidate(f"INVALID_DELTA_TIMESTAMP: {data.get('timestamp')!r}")
                return False

        # Before initial snapshot: buffer bounded deltas, remain unsynced
        if not state.snapshot_received:
            if len(state.pending_deltas) < _MAX_PENDING_DELTAS:
                state.pending_deltas.append(data)
            else:
                state.invalidate("PENDING_DELTA_BUFFER_OVERFLOW")
            return False

        # Already desynced: refuse silent recovery until a fresh book_snapshot arrives
        if not state.synced or state.last_seq is None:
            return False

        return self._apply_validated_delta(state, data, now)

    def _apply_validated_delta(self, state: SymbolBookState, data: Dict[str, Any], now: float) -> bool:
        seq = data["seq"]
        expected_seq = (state.last_seq + 1) if state.last_seq is not None else seq
        if seq != expected_seq:
            state.invalidate(f"SEQ_MISMATCH: expected={expected_seq}, got={seq}")
            logger.error(
                "[KRAKEN_BOOK_FEED] Desync on %s: expected seq=%d, got seq=%d",
                state.product_id, expected_seq, seq,
            )
            return False

        side_norm = str(data["side"]).strip().lower()
        price = float(data["price"])
        qty = float(data["qty"])

        book_side = state.bids if side_norm in ("buy", "bid") else state.asks
        if qty == 0.0:
            book_side.pop(price, None)
        else:
            book_side[price] = qty

        # Detect crossed book after update
        if state.bids and state.asks and max(state.bids.keys()) >= min(state.asks.keys()):
            state.invalidate(
                f"CROSSED_BOOK_AFTER_UPDATE: best_bid={max(state.bids.keys())} >= best_ask={min(state.asks.keys())}"
            )
            return False

        state.last_seq = seq
        if "timestamp" in data and data["timestamp"] is not None:
            state.last_event_timestamp_ms = int(float(data["timestamp"]))
        state.last_update_monotonic = now
        if self._connected:
            self._last_liveness_monotonic = now
        return True

    # ──────────────────────────────────────────────────────────
    #  FAIL-CLOSED QUOTE QUERY API
    # ──────────────────────────────────────────────────────────

    def get_best_bid_ask(self, symbol: str) -> Tuple[bool, Any, Optional[str]]:
        """
        Return verified top-of-book ``(best_bid, best_ask)`` for *symbol*.

        Fail-closed guarantees:
          - WS connection must be active and heartbeat/liveness within timeout.
          - Initial ``book_snapshot`` must have been received.
          - Book must be synchronized (zero ``seq`` gaps or malformed updates).
          - Book must not be crossed (``bid < ask`` when both present).

        Returns:
            (True, {"symbol", "bid", "ask", "seq", "timestamp", "synced"}, None)
            or (False, error_message, error_code)
        """
        if not isinstance(symbol, str) or not symbol.strip():
            return False, "Invalid symbol for WS book lookup", "INVALID_REFERENCE_PRICE"

        alive, alive_err = self.is_alive()
        if not alive:
            return False, alive_err or "Kraken WS book feed unavailable", "STALE_REFERENCE_PRICE"

        key = symbol.strip().upper()
        state = self._books.get(key)
        if state is None or not state.snapshot_received:
            return False, f"No initial WS book_snapshot received for {key}", "MISSING_REFERENCE_PRICE"

        if not state.synced:
            return (
                False,
                f"WS order book desynchronized for {key}: {state.desync_reason or 'UNSYNCED'}",
                "STALE_REFERENCE_PRICE",
            )

        best_bid = max(state.bids.keys()) if state.bids else None
        best_ask = min(state.asks.keys()) if state.asks else None

        if best_bid is not None and (not math.isfinite(best_bid) or best_bid <= 0):
            return False, f"Invalid best_bid={best_bid!r} in WS book for {key}", "INVALID_REFERENCE_PRICE"
        if best_ask is not None and (not math.isfinite(best_ask) or best_ask <= 0):
            return False, f"Invalid best_ask={best_ask!r} in WS book for {key}", "INVALID_REFERENCE_PRICE"
        if best_bid is not None and best_ask is not None and best_bid >= best_ask:
            return (
                False,
                f"Crossed WS order book for {key}: bid={best_bid} >= ask={best_ask}",
                "INVALID_REFERENCE_PRICE",
            )

        return (
            True,
            {
                "symbol": key,
                "bid": best_bid,
                "ask": best_ask,
                "seq": state.last_seq,
                "timestamp": state.last_event_timestamp_ms,
                "synced": True,
            },
            None,
        )

    # ──────────────────────────────────────────────────────────
    #  LIVE WEBSOCKET TRANSPORT (ASYNCIO)
    # ──────────────────────────────────────────────────────────

    async def _cleanup_locked(self, reason: str, *, emit_disconnect: bool = True) -> None:
        """
        Internal cleanup helper.
        CONTRACT: Caller MUST already hold ``self._lifecycle_lock``.
        Never acquires ``self._lifecycle_lock`` (prevents self-deadlock).
        """
        self._running = False
        recv_task = self._recv_task
        self._recv_task = None
        if recv_task is not None and recv_task is not asyncio.current_task():
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        ws_to_close = self._ws
        self._ws = None
        if ws_to_close is not None:
            try:
                await ws_to_close.close()
            except Exception:
                pass

        if emit_disconnect:
            self.on_disconnect(reason)
        else:
            self._connected = False
            for state in self._books.values():
                state.invalidate(reason=f"CLEANUP: {reason}", clear_snapshot=True)

    async def connect(self) -> bool:
        """
        Connect to Kraken Futures WS v1, subscribe to ``heartbeat`` + ``book``,
        and wait for explicit Kraken protocol ``subscribed`` confirmations.

        Serialized with ``disconnect()`` via ``self._lifecycle_lock``.

        Returns:
            True  if and only if WS connection AND all required protocol
                  subscriptions are confirmed.
            False if connection, send, or subscription confirmation fails.
        """
        async with self._lifecycle_lock:
            if self._running and self._connected and self._ws is not None:
                return True

            if self._ws is not None or self._recv_task is not None:
                await self._cleanup_locked("PRE_CONNECT_CLEANUP", emit_disconnect=False)

            try:
                import websockets  # type: ignore
            except ImportError:
                logger.error("[KRAKEN_BOOK_FEED] websockets library not installed")
                await self._cleanup_locked("WEBSOCKETS_NOT_INSTALLED", emit_disconnect=True)
                return False

            try:
                ws = await websockets.connect(
                    self._ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                )
            except asyncio.CancelledError:
                await self._cleanup_locked("CONNECT_CANCELLED", emit_disconnect=True)
                raise
            except Exception as exc:
                logger.error("[KRAKEN_BOOK_FEED] Connection failed: %s", exc)
                await self._cleanup_locked(f"CONNECT_FAILED: {exc}", emit_disconnect=True)
                return False

            self._ws = ws

            # Phase 1.9.7.2 (P2-1 & P2-2): Send subscriptions and require real Kraken
            # protocol `{"event": "subscribed", ...}` confirmations before returning True.
            early_frames: List[Dict[str, Any]] = []
            try:
                await ws.send(json.dumps({"event": "subscribe", "feed": "heartbeat"}))
                if self._symbols:
                    await ws.send(json.dumps({
                        "event": "subscribe",
                        "feed": "book",
                        "product_ids": self._symbols,
                    }))

                heartbeat_acked = False
                expected_book_syms = set(self._symbols)
                acked_book_syms: set[str] = set()
                deadline = self._clock() + self._subscribe_ack_timeout_sec

                while not (heartbeat_acked and expected_book_syms.issubset(acked_book_syms)):
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"Kraken WS subscription ack timeout (heartbeat={heartbeat_acked}, "
                            f"book_acked={sorted(acked_book_syms)}, expected={sorted(expected_book_syms)})"
                        )
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise ValueError(f"Malformed non-dict WS frame during subscription: {type(msg).__name__}")

                    ev = msg.get("event")
                    fd = msg.get("feed")
                    if ev in ("error", "alert", "unsubscribed"):
                        raise RuntimeError(
                            f"Kraken WS subscription rejected ({ev}): {msg.get('message') or msg.get('error') or msg}"
                        )
                    if ev == "subscribed":
                        if fd == "heartbeat":
                            heartbeat_acked = True
                        elif fd == "book":
                            raw_pids = msg.get("product_ids")
                            if isinstance(raw_pids, list):
                                for item in raw_pids:
                                    if isinstance(item, str) and item.strip():
                                        acked_book_syms.add(item.strip().upper())
                            raw_pid = msg.get("product_id")
                            if isinstance(raw_pid, str) and raw_pid.strip():
                                acked_book_syms.add(raw_pid.strip().upper())
                    else:
                        early_frames.append(msg)
            except asyncio.CancelledError:
                await self._cleanup_locked("SUBSCRIBE_CANCELLED", emit_disconnect=True)
                raise
            except Exception as exc:
                logger.error("[KRAKEN_BOOK_FEED] Subscription failed: %s", exc)
                await self._cleanup_locked(f"SUBSCRIBE_FAILED: {exc}", emit_disconnect=True)
                return False

            self._running = True
            self.on_connected()
            for frame in early_frames:
                self.process_message(frame)
            self._recv_task = asyncio.create_task(self._receive_loop(ws))
            logger.info("[KRAKEN_BOOK_FEED] Connected and confirmed subscriptions for %d symbols", len(self._symbols))
            return True

    async def disconnect(self) -> None:
        """Gracefully close WS connection and invalidate local books (serialized)."""
        async with self._lifecycle_lock:
            await self._cleanup_locked("GRACEFUL_DISCONNECT", emit_disconnect=True)

    async def _receive_loop(self, my_ws: Any) -> None:
        import websockets  # type: ignore

        conn_closed_exc = getattr(websockets, "ConnectionClosed", ())

        while self._running and self._ws is my_ws:
            try:
                raw = await my_ws.recv()
                data = json.loads(raw)
                self.process_message(data)
            except conn_closed_exc as exc:
                if self._ws is my_ws:
                    self._running = False
                    self._ws = None
                    self.on_disconnect(f"CONNECTION_CLOSED: {exc}")
                break
            except json.JSONDecodeError:
                logger.error("[KRAKEN_BOOK_FEED] Malformed JSON frame — invalidating books")
                for state in self._books.values():
                    state.invalidate("MALFORMED_JSON_FRAME")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[KRAKEN_BOOK_FEED] Receive error: %s", exc)
                if self._ws is my_ws:
                    self._running = False
                    self._ws = None
                    self.on_disconnect(f"RECEIVE_ERROR: {exc}")
                break
