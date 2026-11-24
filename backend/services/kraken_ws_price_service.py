"""
Kraken Futures WebSocket Price Service
══════════════════════════════════════

Real-time mark price updates via Kraken Futures WebSocket ticker feed.

This is an **optional** enhancement for the PnL service.  When connected,
KrakenPriceService will prefer WS prices over REST polling, giving
sub-second latency for PnL updates.

Usage:
    from backend.services.kraken_ws_price_service import KrakenWSPriceService

    ws = KrakenWSPriceService(symbols=["PF_XBTUSD", "PF_ETHUSD"])
    await ws.connect()

    # Inject into PnL service
    pnl_service.price_service.set_ws_source(ws)

    # Or use standalone
    price = await ws.get_mark_price("PF_XBTUSD")

    # Shutdown
    await ws.disconnect()

WebSocket endpoint: wss://futures.kraken.com/ws/v1
Feed: "ticker" (public, no auth required)
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("smarttrade.kraken_ws_price")

KRAKEN_FUTURES_WS_URL = "wss://futures.kraken.com/ws/v1"

# If a WS price hasn't been updated in this many seconds, consider it stale
WS_STALENESS_THRESHOLD_SECONDS = 30.0


class KrakenWSPriceService:
    """Real-time Kraken Futures mark prices via WebSocket.

    Subscribes to the public ``ticker`` feed for the given symbols.
    Stores the latest mark_price, bid, ask, and last per symbol.

    Implements the same interface expected by :class:`KrakenPriceService`
    (``get_mark_price``, ``get_mark_prices``) so it can be injected
    as a WebSocket source.
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        ws_url: str = KRAKEN_FUTURES_WS_URL,
        reconnect: bool = True,
    ) -> None:
        self._symbols = [s.upper() for s in (symbols or [])]
        self._ws_url = ws_url
        self._reconnect = reconnect

        # Latest ticker per symbol (thread-safe for single-writer)
        self._tickers: Dict[str, Dict[str, float]] = {}
        self._last_update: Dict[str, float] = {}

        # State
        self._ws: Any = None
        self._running = False
        self._recv_task: Optional[asyncio.Task] = None

    # ──────────────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────────────

    async def get_mark_price(self, symbol: str) -> float:
        """Return cached mark_price for *symbol*, or 0 if unavailable or stale."""
        key = symbol.upper()
        ticker = self._tickers.get(key)
        if ticker is None:
            return 0.0
        # Check staleness — if data is too old, return 0 to trigger REST fallback
        last_ts = self._last_update.get(key, 0)
        if time.time() - last_ts > WS_STALENESS_THRESHOLD_SECONDS:
            logger.warning("[WS_PRICE] Stale data for %s (%.0fs old), falling back to REST",
                           key, time.time() - last_ts)
            return 0.0
        price = ticker.get("mark_price", 0)
        if price <= 0:
            price = ticker.get("last", 0)
        return price

    async def get_mark_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Return cached mark prices for multiple symbols."""
        prices: Dict[str, float] = {}
        for sym in symbols:
            p = await self.get_mark_price(sym)
            if p > 0:
                prices[sym] = p
        return prices

    @property
    def is_connected(self) -> bool:
        return self._running and self._ws is not None

    @property
    def symbols(self) -> List[str]:
        return list(self._symbols)

    def add_symbol(self, symbol: str) -> None:
        """Add a symbol for tracking.  Subscribes immediately if connected."""
        key = symbol.upper()
        if key not in self._symbols:
            self._symbols.append(key)
            if self._ws and self._running:
                asyncio.create_task(self._subscribe([key]))

    # ──────────────────────────────────────────────────────────
    #  LIFECYCLE
    # ──────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to Kraken Futures WS and subscribe to ticker feed."""
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.error("[WS_PRICE] websockets library not installed — pip install websockets")
            return

        if self._running:
            return

        logger.info("[WS_PRICE] Connecting to %s", self._ws_url)
        try:
            self._ws = await websockets.connect(
                self._ws_url,
                ping_interval=20,
                ping_timeout=10,
            )
        except Exception as e:
            logger.error("[WS_PRICE] Connection failed: %s", e)
            return

        self._running = True

        # Subscribe to ticker feed
        if self._symbols:
            await self._subscribe(self._symbols)

        # Start receiver loop
        self._recv_task = asyncio.create_task(self._receive_loop())
        logger.info("[WS_PRICE] Connected and subscribed to %d symbols", len(self._symbols))

    async def disconnect(self) -> None:
        """Gracefully disconnect."""
        self._running = False
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("[WS_PRICE] Disconnected")

    # ──────────────────────────────────────────────────────────
    #  INTERNAL
    # ──────────────────────────────────────────────────────────

    async def _subscribe(self, symbols: List[str]) -> None:
        """Send subscription request for ticker feed."""
        msg = {
            "event": "subscribe",
            "feed": "ticker",
            "product_ids": symbols,
        }
        if self._ws:
            await self._ws.send(json.dumps(msg))
            logger.debug("[WS_PRICE] Subscribed: %s", symbols)

    async def _receive_loop(self) -> None:
        """Background task: receive WS messages and update ticker cache."""
        import websockets  # type: ignore

        while self._running and self._ws:
            try:
                raw = await self._ws.recv()
                data = json.loads(raw)
                self._handle_message(data)
            except websockets.ConnectionClosed as e:
                logger.warning("[WS_PRICE] Connection closed: %s", e)
                self._running = False
                if self._reconnect:
                    await self._attempt_reconnect()
                break
            except json.JSONDecodeError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[WS_PRICE] Receive error: %s", e)

    def _handle_message(self, data: Dict[str, Any]) -> None:
        """Parse incoming ticker message and update cache."""
        feed = data.get("feed", "")
        if feed not in ("ticker", "ticker_snapshot"):
            return

        symbol = data.get("product_id", "").upper()
        if not symbol:
            return

        self._tickers[symbol] = {
            "mark_price": float(data.get("markPrice", 0)),
            "last": float(data.get("last", 0)),
            "bid": float(data.get("bid", 0)),
            "ask": float(data.get("ask", 0)),
            "volume": float(data.get("volume", 0)),
        }
        self._last_update[symbol] = time.time()

        logger.debug(
            "[WS_PRICE] %s mark=%.2f last=%.2f bid=%.2f ask=%.2f",
            symbol,
            self._tickers[symbol]["mark_price"],
            self._tickers[symbol]["last"],
            self._tickers[symbol]["bid"],
            self._tickers[symbol]["ask"],
        )

    async def _attempt_reconnect(self) -> None:
        """Attempt reconnection with exponential backoff."""
        delays = [1, 2, 5, 10, 30]
        for attempt, delay in enumerate(delays):
            logger.info("[WS_PRICE] Reconnecting in %ds (attempt %d)...", delay, attempt + 1)
            await asyncio.sleep(delay)
            try:
                await self.connect()
                if self._running:
                    logger.info("[WS_PRICE] Reconnected successfully")
                    return
            except Exception as e:
                logger.warning("[WS_PRICE] Reconnect attempt %d failed: %s", attempt + 1, e)
        logger.error("[WS_PRICE] All reconnect attempts exhausted")
