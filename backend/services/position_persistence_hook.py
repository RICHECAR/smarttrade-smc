"""
Position Persistence Hook - Automatic recording of positions

This hook integrates with the trade execution flow to automatically
record positions in the persisted_positions table when trades are
opened or closed.

Usage:
    from backend.services.position_persistence_hook import (
        on_position_opened,
        on_position_closed,
    )
    
    # After trade execution succeeds:
    on_position_opened(
        broker="bitget",
        order_id="123456",
        symbol="BTCUSDT",
        side="long",
        size=0.01,
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit=52000.0,
        leverage=10,
        raw_response={...}
    )
    
    # When position is closed:
    on_position_closed(
        order_id="123456",
        exit_price=51000.0,
        fee=0.5,
        close_reason="TP_HIT",
        raw_response={...}
    )

Author: SMC Trading Bot Framework
"""

import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from backend.services.positions_service import (
    record_position_open,
    record_position_close,
    get_positions_service,
)

logger = logging.getLogger(__name__)


def on_position_opened(
    broker: str,
    symbol: str,
    side: str,
    size: float,
    entry_price: float,
    order_id: Optional[str] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    leverage: int = 10,
    session_name: Optional[str] = None,
    preset: Optional[str] = None,
    signal_id: Optional[str] = None,
    status: str = "OPEN",
    error_message: Optional[str] = None,
    raw_response: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Hook called when a position is opened on any broker.
    
    Call this from your execution callbacks after confirming
    the order was successfully placed.
    
    Args:
        broker: "bitget" or "kraken"
        symbol: Trading pair (e.g., "BTCUSDT")
        side: "long" or "short"
        size: Position size/quantity
        entry_price: Entry/fill price
        order_id: Exchange order ID
        stop_loss: Stop loss price
        take_profit: Take profit price
        leverage: Leverage used
        session_name: Trading session (London/NY/etc)
        preset: Strategy preset name
        signal_id: Signal ID for deduplication
        raw_response: Raw exchange API response
        
    Returns:
        Position ID if created, None on error
    """
    try:
        # RULE: Never create an OPEN position if the entry order was not confirmed
        normalized_status = (status or "OPEN").upper()
        if normalized_status in ("FAILED", "REJECTED", "CANCELLED", "ERROR"):
            logger.warning(
                f"[POSITION_HOOK] SKIPPING position creation for {symbol}"
                f" | status={normalized_status} | error={error_message}"
                f" | A failed/rejected order must NOT create an OPEN position"
            )
            return None

        position = record_position_open(
            broker=broker.lower(),
            symbol=symbol.upper(),
            side=side.lower(),
            size=size,
            entry_price=entry_price,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit=take_profit,
            order_id=order_id,
            session_name=session_name,
            preset=preset,
            signal_id=signal_id,
            status=status,
            error_message=error_message,
            raw_response=raw_response,
        )
        
        if position:
            logger.info(
                f"[POSITION_HOOK] Recorded OPEN: {position.id} | "
                f"{broker} {symbol} {side} @ {entry_price}"
            )
            return position.id
        else:
            logger.warning(
                f"[POSITION_HOOK] Failed to record OPEN: {broker} {symbol}"
            )
            return None
            
    except Exception as e:
        logger.error(f"[POSITION_HOOK] Error recording position open: {e}")
        return None


def on_position_closed(
    order_id: Optional[str] = None,
    position_id: Optional[str] = None,
    exit_price: float = 0.0,
    fee: float = 0.0,
    close_reason: str = "MANUAL",
    raw_response: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Hook called when a position is closed on any broker.
    
    Call this from your execution callbacks when a position
    is closed (TP hit, SL hit, manual close, etc).
    
    Args:
        order_id: Exchange order ID (from open)
        position_id: Internal position ID
        exit_price: Exit/close price
        fee: Trading fee for exit
        close_reason: Reason for close (TP_HIT, SL_HIT, MANUAL, LIQUIDATION)
        raw_response: Raw exchange API response
        
    Returns:
        True if position was closed, False on error
        
    Note:
        Either order_id or position_id must be provided.
    """
    if not order_id and not position_id:
        logger.error("[POSITION_HOOK] Either order_id or position_id required")
        return False
    
    try:
        position = record_position_close(
            position_id=position_id,
            order_id=order_id,
            exit_price=exit_price,
            fee=fee,
            close_reason=close_reason,
            raw_response=raw_response,
        )
        
        if position:
            logger.info(
                f"[POSITION_HOOK] Recorded CLOSE: {position.id} | "
                f"PnL={position.pnl:.2f} | Result={position.result} | "
                f"Reason={close_reason}"
            )
            return True
        else:
            identifier = position_id or order_id
            logger.warning(
                f"[POSITION_HOOK] Failed to close position: {identifier}"
            )
            return False
            
    except Exception as e:
        logger.error(f"[POSITION_HOOK] Error closing position: {e}")
        return False


def on_position_updated(
    order_id: Optional[str] = None,
    position_id: Optional[str] = None,
    current_price: Optional[float] = None,
    unrealized_pnl: Optional[float] = None,
) -> bool:
    """
    Optional hook for updating position status (e.g., for real-time PnL tracking).
    
    Note: This is optional - positions can be tracked without updates.
    The final PnL is calculated on close.
    
    Args:
        order_id: Exchange order ID
        position_id: Internal position ID  
        current_price: Current market price
        unrealized_pnl: Current unrealized PnL
        
    Returns:
        True on success, False on error
    """
    # This is a placeholder for optional real-time tracking
    # The main persistence happens on open/close
    logger.debug(
        f"[POSITION_HOOK] Update: {position_id or order_id} | "
        f"Price={current_price} | uPnL={unrealized_pnl}"
    )
    return True


def sync_positions_from_exchange(
    broker: str,
    exchange_positions: list,
) -> Dict[str, Any]:
    """
    Sync positions from exchange to database.
    
    Use this to reconcile positions on startup or after disconnection.
    
    Args:
        broker: "bitget" or "kraken"
        exchange_positions: List of positions from exchange API
        
    Returns:
        Sync result with counts
    """
    service = get_positions_service()
    
    synced = 0
    skipped = 0
    errors = 0
    
    for pos in exchange_positions:
        try:
            # Extract position data (format depends on exchange)
            order_id = pos.get("orderId") or pos.get("order_id")
            
            # Check if already exists
            existing = service.get_positions(
                broker=broker,
                status="OPEN",
                limit=1000,
            )
            
            existing_ids = {p.order_id for p, _ in existing if p.order_id}
            
            if order_id and order_id not in existing_ids:
                # Create new position
                on_position_opened(
                    broker=broker,
                    symbol=pos.get("symbol", ""),
                    side=pos.get("side", "long"),
                    size=float(pos.get("size", 0) or pos.get("qty", 0)),
                    entry_price=float(pos.get("entry_price", 0) or pos.get("avgPrice", 0)),
                    order_id=order_id,
                    leverage=int(pos.get("leverage", 10)),
                    raw_response=pos,
                )
                synced += 1
            else:
                skipped += 1
                
        except Exception as e:
            logger.error(f"[POSITION_HOOK] Sync error: {e}")
            errors += 1
    
    result = {
        "broker": broker,
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    logger.info(
        f"[POSITION_HOOK] Sync complete: {broker} | "
        f"synced={synced}, skipped={skipped}, errors={errors}"
    )
    
    return result
