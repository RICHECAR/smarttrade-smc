"""
Trade Persistence Hook - Integration layer between OrderManager and SQLite/WebSocket

This module provides the bridge between:
- OrderManager (execution engine)
- LiveTradesService (SQLite persistence)
- WebSocket Manager (real-time dashboard updates)

Called at two points:
1. on_trade_opened() - After Bitget order confirmation
2. on_trade_closed() - After TP/SL/Manual close

Author: SMC Trading Bot Framework
"""

import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from backend.services.live_trades_service import (
    LiveTradesService,
    create_live_trade_async,
    close_live_trade_async,
    get_open_trades_async,
    add_fill_async,
)
from backend.models.live_trade import LiveTrade
from backend.models.trade_fill import TradeFill

logger = logging.getLogger(__name__)

# Global WebSocket manager reference (set during app startup)
_ws_manager = None


def set_ws_manager(manager):
    """Set the WebSocket manager reference for broadcasts."""
    global _ws_manager
    _ws_manager = manager
    logger.info("[PERSISTENCE] WebSocket manager registered")


async def broadcast_trade_event(event_type: str, trade: LiveTrade) -> None:
    """
    Broadcast trade event to all connected WebSocket clients.
    
    Args:
        event_type: "trade_opened" or "trade_closed"
        trade: LiveTrade instance
    """
    global _ws_manager
    
    if _ws_manager is None:
        logger.warning("[PERSISTENCE] No WS manager registered, skipping broadcast")
        return
    
    try:
        message = {
            "type": event_type,
            "trade": trade.to_dict(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        # Use broadcast method from ws_manager
        if hasattr(_ws_manager, 'broadcast'):
            await _ws_manager.broadcast(message)
            logger.info(f"[WS_BROADCAST] {event_type} | {trade.symbol} | order_id={trade.order_id}")
        elif hasattr(_ws_manager, 'broadcast_trade_opened') and event_type == "trade_opened":
            await _ws_manager.broadcast_trade_opened(trade.to_dict())
        elif hasattr(_ws_manager, 'broadcast_trade_closed') and event_type == "trade_closed":
            await _ws_manager.broadcast_trade_closed(trade.to_dict())
        else:
            logger.warning(f"[PERSISTENCE] WS manager has no broadcast method")
            
    except Exception as e:
        logger.error(f"[WS_BROADCAST] FAILED: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN HOOKS (Called by OrderManager)
# ═══════════════════════════════════════════════════════════════════════════════

async def on_trade_opened(
    order_id: str,
    symbol: str,
    side: str,
    qty: float,
    entry: float,
    stop: float,
    tp: float,
    leverage: int = 10,
    session_name: Optional[str] = None,
    preset: Optional[str] = None,
    signal_id: Optional[str] = None,
    raw_exchange: Optional[Dict[str, Any]] = None,
    broker: str = "bitget",
    parent_trade_id: Optional[str] = None,
    status: str = "open",
    error_message: Optional[str] = None,
) -> Optional[LiveTrade]:
    """
    Hook called AFTER broker order confirmation.
    Deligates to LiveTradesService.create_live_trade_from_order for standardization,
    or falls back to direct creation if context not fully available.
    
    Args:
        broker: "bitget" or "kraken" - identifies which exchange placed the order
        parent_trade_id: For mirror trades, links to the primary trade ID
        status: Trade status ("open", "closed", "failed")
        error_message: Error message if trade failed
    """
    status_emoji = "✓" if status == "open" else "✗" if status == "failed" else "○"
    logger.info(
        f"[PERSISTENCE] on_trade_opened {status_emoji} | order_id={order_id}"
        f" | {symbol} {side.upper()} | qty={qty} | entry={entry}"
        f" | broker={broker} | status={status}"
    )
    
    # RULE: A position with confirmed SL but failed TP must remain tracked in live_trades as OPEN
    normalized_status = (status or "open").upper()
    if normalized_status == "TP_FAILED_SL_ONLY":
        logger.warning(
            f"[PERSISTENCE] Position has SL confirmed but TP failed | {symbol} | "
            f"Tracking as OPEN with SL protection"
        )
        status = "open"
        error_message = error_message or "TP_FAILED_SL_ONLY"
    elif (
        normalized_status in ("FAILED", "REJECTED", "CANCELLED", "ERROR")
        or normalized_status.startswith("SL_FAILED")
        or normalized_status in ("SL_FAILED_POSITION_EMERGENCY_CLOSED", "SL_FAILED_POSITION_NAKED",
                                 "FILL_UNKNOWN", "NO_FILL", "POSITION_CLOSED_BEFORE_PROTECTION",
                                 "POSITION_CLOSED_BEFORE_SL", "POSITION_REDUCED_BELOW_MIN",
                                 "MANUAL_RECONCILIATION_REQUIRED")
    ):
        logger.warning(
            f"[PERSISTENCE] SKIPPING trade creation for {symbol}"
            f" | status={normalized_status} | error={error_message}"
            f" | An emergency closed, naked or failed order must NOT be persisted as an open trade"
        )
        return None
    
    try:
        # Construct context for helper (standardizing input)
        context = {
            "symbol": symbol,
            "direction": side,
            "entry": entry,
            "stop_loss": stop,
            "take_profit": tp,
            "leverage": leverage,
            "session": session_name,
            "preset": preset,
            "signal_id": signal_id,
            "broker": broker,
            "parent_trade_id": parent_trade_id,
            "status": status,
            "error_message": error_message,
        }
        
        # Construct simulated order data if raw_exchange is basic
        order_data = raw_exchange or {}
        order_data.update({
            "entry_order_id": order_id,
            "price_executed": entry,
            "quantity": qty
        })
        
        # Use new standardized helper
        trade = await LiveTradesService.create_live_trade_from_order(order_data, context)
        
        if trade:
            # Broadcast to dashboard
            await broadcast_trade_event("trade_opened", trade)
            return trade
        else:
            logger.error(f"[PERSISTENCE] Failed to persist trade: order_id={order_id}")
            return None
        
    except Exception as e:
        logger.error(f"[PERSISTENCE] on_trade_opened FAILED: {e}", exc_info=True)
        return None


async def on_trade_closed(
    trade_id: Optional[str] = None,
    order_id: Optional[str] = None,
    close_price: float = 0.0,
    pnl: Optional[float] = None,
    fee: float = 0.0,
    qty: float = 0.0,
    raw_exchange: Optional[Dict[str, Any]] = None,
    close_reason: Optional[str] = None,
) -> Optional[LiveTrade]:
    """
    Hook called AFTER trade close (TP/SL/Manual).
    Uses add_fill for consistent PnL tracking.
    """
    logger.info(
        f"[PERSISTENCE] on_trade_closed | trade_id={trade_id} order_id={order_id}"
        f" | close_price={close_price} | pnl={pnl} | qty={qty}"
    )
    
    try:
        # Resolve trade_id if missing
        if not trade_id and order_id:
            trade = LiveTradesService.get_trade_by_order_id(order_id)
            if trade:
                trade_id = trade.id
        
        if not trade_id:
             logger.error(f"[PERSISTENCE] Cannot close trade: missing trade_id (order_id={order_id})")
             return None

        # Fetch trade to preserve order_id if missing and validate qty
        trade = LiveTradesService.get_trade_by_id(trade_id)
        if trade:
            if not order_id and trade.order_id:
                order_id = trade.order_id
            
            # Fallback: if close qty is 0/missing, use original trade qty
            if qty <= 0 and trade.qty > 0:
                logger.warning(
                    f"[PERSISTENCE] CLOSE_QTY_FALLBACK_TO_ORIGINAL | trade={trade_id}"
                    f" | received_qty={qty} | using original_qty={trade.qty}"
                )
                qty = trade.qty

        # Use add_fill for consistent PnL aggregation with action="CLOSE"
        fill = await add_fill_async(
            trade_id=trade_id,
            action="CLOSE",
            qty=qty, 
            price=close_price,
            pnl=pnl,
            fee=fee,
            order_id=order_id,
            raw_response=raw_exchange,
            close_reason=close_reason,
        )
        
        if fill:
            # Broadcast updated trade state
            trade = LiveTradesService.get_trade_by_id(trade_id)
            if trade:
                await broadcast_trade_event("trade_closed", trade)
            return trade
        else:
            logger.error(f"[PERSISTENCE] Failed to record close fill: trade_id={trade_id}")
            return None
        
    except Exception as e:
        logger.error(f"[PERSISTENCE] on_trade_closed FAILED: {e}", exc_info=True)
        return None


async def on_trade_reduced(
    trade_id: str,
    qty: float,
    price: float,
    pnl: float = 0.0,
    fee: float = 0.0,
    reduce_pct: Optional[float] = None,
    order_id: Optional[str] = None,
    raw_exchange: Optional[Dict[str, Any]] = None,
) -> Optional[TradeFill]:
    """
    Hook called AFTER partial close (reduce).
    Records fill in trade_fills table and broadcasts WS event.
    
    Args:
        trade_id: Parent LiveTrade UUID
        qty: Quantity reduced
        price: Fill price
        pnl: Realized PnL for this fill
        fee: Trading fee
        reduce_pct: Original reduce percentage requested
        order_id: Bitget order ID
        raw_exchange: Raw exchange response
    """
    logger.info(
        f"[PERSISTENCE] on_trade_reduced | trade_id={trade_id[:8]}..."
        f" | qty={qty} | pnl={pnl}"
    )
    
    try:
        # Add fill record
        fill = await add_fill_async(
            trade_id=trade_id,
            action="REDUCE",
            qty=qty,
            price=price,
            fee=fee,
            pnl=pnl,
            order_id=order_id,
            raw_response=raw_exchange,
        )
        
        if fill:
            # Get updated trade for broadcast
            trade = LiveTradesService.get_trade_by_id(trade_id)
            if trade:
                await broadcast_reduce_event(trade, fill, reduce_pct)
            return fill
        else:
            logger.error(f"[PERSISTENCE] Failed to record reduce fill: trade_id={trade_id}")
            return None
        
    except Exception as e:
        logger.error(f"[PERSISTENCE] on_trade_reduced FAILED: {e}", exc_info=True)
        return None


async def broadcast_reduce_event(
    trade: LiveTrade,
    fill: TradeFill,
    reduce_pct: Optional[float] = None,
) -> None:
    """Broadcast trade_reduced event to WebSocket clients."""
    global _ws_manager
    
    if _ws_manager is None:
        logger.warning("[PERSISTENCE] No WS manager registered, skipping broadcast")
        return
    
    try:
        message = {
            "type": "trade_reduced",
            "trade": trade.to_dict(),
            "fill": fill.to_dict(),
            "reduce_pct": reduce_pct,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        if hasattr(_ws_manager, 'broadcast'):
            await _ws_manager.broadcast(message)
            logger.info(
                f"[WS_BROADCAST] trade_reduced | {trade.symbol} | qty={fill.qty}"
                f" | pnl={fill.pnl}"
            )
        else:
            logger.warning("[PERSISTENCE] WS manager has no broadcast method")
            
    except Exception as e:
        logger.error(f"[WS_BROADCAST] FAILED: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# RECOVERY (On restart)
# ═══════════════════════════════════════════════════════════════════════════════

async def recover_open_trades() -> list[LiveTrade]:
    """
    Recover all OPEN trades from SQLite on startup.
    
    Handles schema migration automatically if DB is out of sync.
    
    Returns:
        List of LiveTrade for active positions
    """
    logger.info("[PERSISTENCE] Recovering OPEN trades from SQLite...")
    
    def _attempt_recovery():
        """Synchronous recovery attempt."""
        return LiveTradesService.get_open_trades()
    
    try:
        # First attempt
        loop = asyncio.get_event_loop()
        trades = await loop.run_in_executor(None, _attempt_recovery)
        
    except Exception as first_error:
        # Check if it's a missing column error
        error_str = str(first_error).lower()
        if "no such column" in error_str or "operationalerror" in error_str:
            logger.warning(
                f"[PERSISTENCE] Schema out of sync, triggering migration: {first_error}"
            )
            
            # Trigger schema migration
            try:
                from backend.db.session import get_db_url
                from backend.db.migrations import ensure_schema_up_to_date, ensure_all_columns_exist
                
                db_url = get_db_url()
                # Try versioned migration first
                try:
                    ensure_schema_up_to_date(db_url)
                except Exception:
                    # Fallback to direct column check
                    ensure_all_columns_exist(db_url)
                
                logger.info("[PERSISTENCE] Schema migration applied, retrying recovery...")
                
                # Retry recovery
                trades = await loop.run_in_executor(None, _attempt_recovery)
                
            except Exception as retry_error:
                logger.error(
                    f"[PERSISTENCE] Recovery failed after migration: {retry_error}",
                    exc_info=True
                )
                return {}
        else:
            # Non-schema error
            logger.error(f"[PERSISTENCE] Recovery FAILED: {first_error}", exc_info=True)
            return {}
    
    # Prepare list
    result = []
    for trade in trades:
        # Ignore dry-run test trades and clean DB state
        if trade.order_id and trade.order_id.startswith("DRY_"):
            logger.info(
                f"[PERSISTENCE] Auto-closing dry-run trade during recovery | order_id={trade.order_id}"
            )
            try:
                LiveTradesService.update_trade_status(
                    trade_id=trade.id,
                    status="closed",
                    close_price=trade.entry,
                    pnl=0.0,
                    error_message="Auto-closed dry-run on restart"
                )
            except Exception as e:
                logger.error(f"[PERSISTENCE] Failed to clear dry-run state {trade.order_id}: {e}")
            continue
            
        result.append(trade)
        logger.info(
            f"[PERSISTENCE] Recovered OPEN trade | {trade.symbol} {trade.side.upper()}"
            f" | broker={trade.broker} | order_id={trade.order_id} | entry={trade.entry}"
        )
    
    logger.info(f"[PERSISTENCE] Recovered {len(result)} OPEN trades")
    return result


async def sync_with_exchange(bitget_executor, kraken_executor, open_trades: list[LiveTrade]) -> None:
    """
    Sync recovered trades with exchange positions.
    
    Handles:
    - Trades that closed while bot was down
    - Position size mismatches
    
    Args:
        bitget_executor: BitgetFuturesExecutor instance
        kraken_executor: KrakenFuturesExecutor instance
        open_trades: List from recover_open_trades()
    """
    if not open_trades:
        return
    
    logger.info(f"[PERSISTENCE] Syncing {len(open_trades)} trades with exchanges...")
    
    try:
        # Build set of symbols with open positions
        exchange_positions = {"bitget": {}, "kraken": {}}
        
        # Sync Bitget
        if bitget_executor:
            positions_result = await bitget_executor.get_positions()
            if positions_result.get("code") == "00000":
                for pos in positions_result.get("data", []) or []:
                    symbol = pos.get("symbol", "").upper()
                    total = float(pos.get("total", 0) or 0)
                    if total > 0:
                        exchange_positions["bitget"][symbol] = pos
            else:
                logger.error(f"[PERSISTENCE] Failed to fetch Bitget positions: {positions_result.get('msg')}")
                
        # Sync Kraken
        if kraken_executor:
            try:
                k_positions = await kraken_executor.get_positions()
                k_data = k_positions.get("data", []) if isinstance(k_positions, dict) else k_positions
                for pos in k_data:
                    symbol = pos.get("symbol", "").upper()
                    size = float(pos.get("size", 0) or 0)
                    if size > 0:
                        exchange_positions["kraken"][symbol] = pos
            except Exception as e:
                logger.error(f"[PERSISTENCE] Failed to fetch Kraken positions: {e}")
        
        # Check each recovered trade
        for trade in open_trades:
            broker = trade.broker.lower()
            symbol = trade.symbol
            
            # Skip sync if executor for this broker isn't loaded
            if (broker == "bitget" and not bitget_executor) or (broker == "kraken" and not kraken_executor):
                continue
                
            if symbol not in exchange_positions.get(broker, {}):
                # Position no longer exists on exchange - close it
                import time
                ext_order_id = f"ext_close_{int(time.time())}_{trade.id[:8]}"
                
                # Fetch historical PnL if possible
                fetched_pnl = None
                fetched_fee = 0.0
                try:
                    ts_ms = int(trade.ts_open.timestamp() * 1000) if trade.ts_open else None
                    if broker == "bitget" and bitget_executor:
                        hist = await bitget_executor.get_historical_pnl(symbol, start_time=ts_ms)
                        if hist:
                            fetched_pnl = hist["pnl"]
                            fetched_fee = hist["fee"]
                    elif broker == "kraken" and kraken_executor:
                        hist = await kraken_executor.get_historical_pnl(symbol, start_time=ts_ms)
                        if hist:
                            fetched_pnl = hist["pnl"]
                            fetched_fee = hist["fee"]
                except Exception as e:
                    logger.error(f"[PERSISTENCE] Failed to fetch hist PnL for {symbol}: {e}")
                
                logger.warning(
                    f"[PERSISTENCE] Position closed externally | {symbol} ({broker})"
                    f" | order_id={trade.order_id} | generated_ext_id={ext_order_id} | fetched_pnl={fetched_pnl}"
                )
                
                # Mark as closed with unknown close price (but fetched PnL if available)
                await on_trade_closed(
                    trade_id=trade.id,
                    order_id=ext_order_id,
                    close_price=trade.entry,  # Use entry as fallback
                    pnl=fetched_pnl,  # Fetched PnL or None
                    fee=fetched_fee,
                    qty=trade.qty,  # Pass full qty to pass validation
                    raw_exchange={
                        "sync_note": "closed_externally_while_bot_down"
                    },
                    close_reason="external_close" if fetched_pnl is not None else "external_close_unknown_fill"
                )
            else:
                logger.info(f"[PERSISTENCE] Position confirmed | {symbol} ({broker}) | order_id={trade.order_id}")
        
        logger.info("[PERSISTENCE] Sync complete")
        
    except Exception as e:
        logger.error(f"[PERSISTENCE] Sync FAILED: {e}", exc_info=True)
