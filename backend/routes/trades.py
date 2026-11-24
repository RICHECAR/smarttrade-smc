"""
Trades API Routes - CRUD operations for trade records

Endpoints:
- GET /api/trades - List trades with date filtering
- POST /api/trades - Create new trade
- GET /api/trades/{trade_id} - Get single trade
- PATCH /api/trades/{trade_id} - Update/close trade
- DELETE /api/trades/{trade_id} - Delete trade

Author: SMC Trading Bot Framework
"""

from fastapi import APIRouter, Query, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import date, datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import uuid
import logging

from backend.db.session import get_db
from backend.models.trade import (
    Trade, TradeIn, TradeOut, TradeUpdate, 
    TradeStatus, TradeResult, Side
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["trades"])


@router.get("/trades", response_model=List[Dict[str, Any]])
def get_trades(
    from_: date = Query(..., alias="from", description="Start date (YYYY-MM-DD)"),
    to: date = Query(..., description="End date (YYYY-MM-DD)"),
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    session_filter: Optional[str] = Query(None, alias="session", description="Filter by session"),
    preset: Optional[str] = Query(None, description="Filter by preset"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    """
    List trades within a date range.
    
    Returns list of trade objects matching the filters.
    """
    try:
        # Convert dates to datetime bounds
        start_dt = datetime.combine(from_, datetime.min.time())
        end_dt = datetime.combine(to, datetime.max.time())
        
        # Build query
        query = db.query(Trade).filter(
            Trade.ts >= start_dt,
            Trade.ts <= end_dt
        )
        
        # Apply optional filters
        if symbol:
            query = query.filter(Trade.symbol == symbol.upper())
        if session_filter:
            query = query.filter(Trade.session == session_filter)
        if preset:
            query = query.filter(Trade.preset == preset)
        if status_filter:
            query = query.filter(Trade.status == status_filter)
        
        # Execute and sort by timestamp descending
        trades = query.order_by(Trade.ts.desc()).all()
        
        logger.info(f"[TRADES] Found {len(trades)} trades from {from_} to {to}")
        return [t.to_dict() for t in trades]
        
    except Exception as e:
        logger.error(f"[TRADES] Error listing trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/trades", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
def create_trade(
    trade_in: TradeIn,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Create a new trade record.
    
    Automatically calculates R:R ratio from entry/stop/tp.
    """
    try:
        # Calculate R:R
        stop_dist = abs(trade_in.entry - trade_in.stop)
        tp_dist = abs(trade_in.tp - trade_in.entry)
        rr = round(tp_dist / stop_dist, 2) if stop_dist > 0 else 0.0
        
        # Create ORM instance
        trade = Trade(
            id=str(uuid.uuid4())[:8],
            ts=datetime.now(timezone.utc),
            symbol=trade_in.symbol.upper(),
            side=trade_in.side.value,
            entry=trade_in.entry,
            stop=trade_in.stop,
            tp=trade_in.tp,
            rr=rr,
            pnl=0.0,
            session=trade_in.session,
            preset=trade_in.preset,
            status="open",
            qty=trade_in.qty,
            leverage=trade_in.leverage,
            notional=trade_in.notional,
            risk_amount=trade_in.risk_amount,
            stop_distance=trade_in.stop_distance,
        )
        
        db.add(trade)
        db.commit()
        db.refresh(trade)
        
        logger.info(f"[TRADES] Created trade {trade.id}: {trade.symbol} {trade.side}")
        return trade.to_dict()
        
    except Exception as e:
        db.rollback()
        logger.error(f"[TRADES] Error creating trade: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trades/{trade_id}", response_model=Dict[str, Any])
def get_trade(
    trade_id: str,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Get a single trade by ID."""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    
    if not trade:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")
    
    return trade.to_dict()


@router.patch("/trades/{trade_id}", response_model=Dict[str, Any])
def update_trade(
    trade_id: str,
    update: TradeUpdate,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Update/close a trade.

    PnL is always recalculated from entry/close_price/qty/side to prevent
    arbitrary overrides.  close_price is required to close a trade.
    """
    try:
        trade = db.query(Trade).filter(Trade.id == trade_id).first()
        
        if not trade:
            raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

        close_price = update.close_price
        if close_price is None:
            raise HTTPException(
                status_code=422,
                detail="close_price is required to close a trade",
            )

        # Recalculate PnL from trade data — never trust user-supplied PnL
        qty = trade.qty or 0.0
        if trade.side and trade.side.lower() == "short":
            computed_pnl = (trade.entry - close_price) * qty
        else:
            computed_pnl = (close_price - trade.entry) * qty

        if update.pnl is not None and abs(update.pnl - computed_pnl) > 0.01:
            logger.warning(
                "[TRADES] PnL override rejected for %s: submitted=%.4f computed=%.4f",
                trade_id, update.pnl, computed_pnl,
            )

        trade.pnl = round(computed_pnl, 6)
        trade.close_price = close_price
        trade.result = update.result.value if update.result else (
            "win" if computed_pnl > 0 else ("loss" if computed_pnl < 0 else "be")
        )
        trade.status = "closed"
        trade.close_ts = datetime.now(timezone.utc)
        
        db.commit()
        db.refresh(trade)
        
        logger.info(f"[TRADES] Closed trade {trade.id}: PnL={trade.pnl}, Result={trade.result}")
        return trade.to_dict()
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"[TRADES] Error updating trade: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/trades/{trade_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trade(
    trade_id: str,
    db: Session = Depends(get_db),
):
    """Delete a trade record."""
    try:
        trade = db.query(Trade).filter(Trade.id == trade_id).first()
        
        if not trade:
            raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")
        
        db.delete(trade)
        db.commit()
        
        logger.info(f"[TRADES] Deleted trade {trade_id}")
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"[TRADES] Error deleting trade: {e}")
        raise HTTPException(status_code=500, detail=str(e))
