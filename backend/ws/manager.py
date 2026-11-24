"""
WebSocket Connection Manager - Real-time trade broadcasting

Manages WebSocket connections and broadcasts trade events to all connected clients.

Usage:
    from backend.ws.manager import ws_manager
    
    # In FastAPI route
    @app.websocket("/ws/trades")
    async def websocket_endpoint(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)
    
    # To broadcast (from anywhere in the app)
    await ws_manager.broadcast_trade_opened(trade)
    await ws_manager.broadcast_trade_closed(trade)

Author: SMC Trading Bot Framework
"""

import asyncio
import logging
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from backend.models.trade import TradeOut, WSTradeMessage

logger = logging.getLogger(__name__)


@dataclass
class ConnectionManager:
    """
    Manages WebSocket connections for real-time trade broadcasts.
    
    Thread-safe singleton pattern for use across the application.
    """
    
    active_connections: List[WebSocket] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _accepted: set = field(default_factory=set)
    
    async def connect(self, websocket: WebSocket) -> None:
        """
        Accept a new WebSocket connection.
        
        Args:
            websocket: FastAPI WebSocket instance
        """
        try:
            # Note: websocket.accept() should be called by the handler before connect()
            # to ensure handshake is finished.
            
            async with self._lock:
                self.active_connections.append(websocket)
                self._accepted.add(id(websocket))
            
            client_count = len(self.active_connections)
            logger.info(f"[WS] Client connected. ID={id(websocket)} Total: {client_count}")
            
            # Send welcome message AFTER accept has completed
            try:
                await websocket.send_json({
                    "type": "connected",
                    "message": "Connected to SMC Trades WebSocket",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "connected_clients": client_count,
                })
            except Exception as e:
                logger.warning(f"[WS] DROP client (send failed during welcome): {e}")
                self.disconnect(websocket)
                
        except Exception as e:
            logger.warning(f"[WS] Connection registration failed: {e}")
            # If accept failed, we shouldn't call disconnect because it wasn't added yet,
            # but we can do a cleanup just in case.
            self._accepted.discard(id(websocket))
    
    def disconnect(self, websocket: WebSocket) -> None:
        """
        Remove a WebSocket connection.
        
        Args:
            websocket: WebSocket to remove
        """
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        self._accepted.discard(id(websocket))
        logger.info(f"[WS] Client disconnected. Total: {len(self.active_connections)}")
    
    async def _send_json(self, websocket: WebSocket, data: Dict[str, Any]) -> bool:
        """
        Send JSON data to a single WebSocket.
        
        Returns:
            True if sent successfully, False on error
        """
        # Guard: never send to non-accepted socket
        if id(websocket) not in self._accepted:
            logger.warning("[WS] DROP client (not accepted) — skipping send")
            return False
        try:
            await websocket.send_json(data)
            return True
        except Exception as e:
            logger.warning(f"[WS] DROP client (closed) — send failed: {e}")
            self._accepted.discard(id(websocket))
            return False
    
    async def broadcast(self, message: Dict[str, Any]) -> int:
        """
        Broadcast a message to all connected clients.
        
        Args:
            message: Dict to send as JSON
        
        Returns:
            Number of clients successfully reached
        """
        if not self.active_connections:
            return 0
        
        success_count = 0
        dead_connections = []
        
        for websocket in self.active_connections:
            # Skip sockets that were never accepted
            if id(websocket) not in self._accepted:
                logger.warning("[WS] DROP client (not accepted) — removing from broadcast list")
                dead_connections.append(websocket)
                continue
            try:
                await websocket.send_json(message)
                success_count += 1
            except Exception as e:
                logger.warning(f"[WS] DROP client (closed) — broadcast failed: {e}")
                dead_connections.append(websocket)
        
        # Clean up dead connections
        for ws in dead_connections:
            self.disconnect(ws)
        
        if dead_connections:
            logger.info(f"[WS] Cleaned {len(dead_connections)} dead connections")
        
        logger.debug(f"[WS] Broadcast to {success_count}/{len(self.active_connections)} clients")
        return success_count
    
    async def broadcast_trade_opened(self, trade: TradeOut) -> int:
        """
        Broadcast a trade_opened event.
        
        Args:
            trade: TradeOut model of the opened trade
        
        Returns:
            Number of clients notified
        """
        message = {
            "type": "trade_opened",
            "trade": trade.model_dump(mode="json"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        logger.info(f"[WS] Broadcasting trade_opened: {trade.symbol} {trade.side}")
        return await self.broadcast(message)
    
    async def broadcast_trade_closed(self, trade: TradeOut) -> int:
        """
        Broadcast a trade_closed event.
        
        Args:
            trade: TradeOut model of the closed trade
        
        Returns:
            Number of clients notified
        """
        message = {
            "type": "trade_closed",
            "trade": trade.model_dump(mode="json"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        logger.info(f"[WS] Broadcasting trade_closed: {trade.symbol} PnL={trade.pnl:.2f}")
        return await self.broadcast(message)
    
    async def broadcast_trade_reduced(self, trade: TradeOut, fill: Dict[str, Any], reduce_pct: Optional[float] = None) -> int:
        """
        Broadcast a trade_reduced event.
        
        Args:
            trade: TradeOut model of the reduced trade
            fill: Fill details (qty, price, pnl, fee)
            reduce_pct: Percentage that was reduced
        
        Returns:
            Number of clients notified
        """
        message = {
            "type": "trade_reduced",
            "trade": trade.model_dump(mode="json"),
            "fill": fill,
            "reduce_pct": reduce_pct,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        logger.info(f"[WS] Broadcasting trade_reduced: {trade.symbol} qty={fill.get('qty', 0)} pnl={fill.get('pnl', 0):.2f}")
        return await self.broadcast(message)
    
    async def broadcast_metrics(self, data: Dict[str, Any]) -> int:
        """
        Broadcast metrics/dashboard update.
        
        Args:
            data: Metrics data (latencies, volatility, regimes, open trades)
        """
        message = {
            "type": "metrics_update",
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast(message)
    
    async def send_heartbeat(self) -> None:
        """Send heartbeat to all connected clients."""
        message = {
            "type": "heartbeat",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "connected_clients": len(self.active_connections),
        }
        await self.broadcast(message)
    
    @property
    def connection_count(self) -> int:
        """Get current number of connections."""
        return len(self.active_connections)


# Singleton instance
ws_manager = ConnectionManager()


# ============================================================================
# HEARTBEAT TASK (optional background task)
# ============================================================================

async def heartbeat_loop(interval_sec: int = 30):
    """
    Background task to send periodic heartbeats.
    
    Start this in FastAPI startup:
        asyncio.create_task(heartbeat_loop())
    """
    while True:
        await asyncio.sleep(interval_sec)
        if ws_manager.connection_count > 0:
            await ws_manager.send_heartbeat()
