"""
Positions API Routes - CRUD and Performance Analytics

Endpoints:
  GET  /api/positions              - List all positions with filters
  GET  /api/positions/{id}         - Get single position
  GET  /api/positions/broker/{b}   - Get positions by broker
  GET  /api/positions/open         - Get all open positions
  POST /api/positions              - Create new position
  POST /api/positions/{id}/close   - Close a position
  GET  /api/positions/stats        - Get performance statistics
  GET  /api/positions/stats/{b}    - Get stats by broker

Author: SMC Trading Bot Framework
"""

import logging
from datetime import datetime, timezone, date, timedelta
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Query, HTTPException, status
from pydantic import BaseModel, Field

from backend.services.positions_service import (
    get_positions_service,
    PersistedPositionsService,
    PerformanceMetrics,
    record_position_open,
    record_position_close,
)
from backend.models.persisted_position import (
    PositionCreate,
    PositionClose,
    PositionOut,
    PerformanceStats,
    PositionListResponse,
    PerformanceResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/positions", tags=["positions"])


# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE MODELS
# ══════════════════════════════════════════════════════════════════════════════

class PositionCreatedResponse(BaseModel):
    """Response when a position is created."""
    success: bool
    message: str
    position: Dict[str, Any]


class PositionClosedResponse(BaseModel):
    """Response when a position is closed."""
    success: bool
    message: str
    position: Dict[str, Any]
    pnl: float
    result: str


class StatsResponse(BaseModel):
    """Response for performance statistics."""
    total_pnl: float
    win_rate: float
    avg_rr: float
    total_trades: int
    max_drawdown: float
    profit_factor: float
    wins: int
    losses: int
    break_even: int
    avg_win: float
    avg_loss: float
    best_trade: float
    worst_trade: float
    avg_hold_time_hours: float
    broker_filter: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    timestamp: str


class BrokerStatsResponse(BaseModel):
    """Response for stats grouped by broker."""
    bitget: StatsResponse
    kraken: StatsResponse
    combined: StatsResponse
    timestamp: str


# ══════════════════════════════════════════════════════════════════════════════
# LIST POSITIONS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("", response_model=Dict[str, Any])
async def get_positions(
    broker: Optional[str] = Query(None, description="Filter by broker: bitget, kraken"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status: OPEN, CLOSED"),
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    from_date: Optional[date] = Query(None, alias="from", description="Start date (YYYY-MM-DD)"),
    to_date: Optional[date] = Query(None, alias="to", description="End date (YYYY-MM-DD)"),
    include_archived: bool = Query(False, description="Include archived positions"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Page size"),
    order_by: str = Query("entry_time", description="Sort by field"),
    order_desc: bool = Query(True, description="Sort descending"),
) -> Dict[str, Any]:
    """
    List positions with filters and pagination.
    
    Returns:
        - positions: List of position objects
        - total: Total count (before pagination)
        - open_count: Count of open positions
        - closed_count: Count of closed positions
        - page: Current page
        - page_size: Page size
    """
    try:
        service = get_positions_service()
        
        # Convert dates to datetime
        date_from = datetime.combine(from_date, datetime.min.time()) if from_date else None
        date_to = datetime.combine(to_date, datetime.max.time()) if to_date else None
        
        # Calculate offset
        offset = (page - 1) * page_size
        
        # Get positions
        positions, total = service.get_positions(
            broker=broker,
            status=status_filter,
            symbol=symbol,
            date_from=date_from,
            date_to=date_to,
            include_archived=include_archived,
            limit=page_size,
            offset=offset,
            order_by=order_by,
            order_desc=order_desc,
        )
        
        # Get counts
        counts = service.get_counts(broker=broker)
        
        logger.info(f"[POSITIONS_API] Listed {len(positions)} positions (total: {total})")
        
        result = {
            "source": "db",
            "positions": [p.to_dict() for p in positions],
            "total": total,
            "open_count": counts["open"],
            "closed_count": counts["closed"],
            "page": page,
            "page_size": page_size,
            "filters": {
                "broker": broker,
                "status": status_filter,
                "symbol": symbol,
                "from": str(from_date) if from_date else None,
                "to": str(to_date) if to_date else None,
                "include_archived": include_archived,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Add divergence warning if DB shows open positions but exchange might be empty
        # This is anomaly B: Exchange=0 vs DB=48
        if counts["open"] > 0:
            try:
                # We try to get exchange status from context if available
                from smc_futures.api_smc_engine import ctx
                bitget_exc = ctx.executors.get("futures")
                kraken_exc = ctx.executors.get("kraken_futures")
                
                exchange_open = 0
                if bitget_exc:
                    bg_res = await bitget_exc.get_positions()
                    bg_pos = bg_res.get("data", []) if isinstance(bg_res, dict) else bg_res
                    exchange_open += len(bg_pos) if isinstance(bg_pos, list) else 0
                
                if kraken_exc:
                    kr_res = await kraken_exc.get_positions()
                    kr_pos = kr_res.get("data", []) if isinstance(kr_res, dict) else kr_res
                    exchange_open += len(kr_pos) if isinstance(kr_pos, list) else 0

                if exchange_open == 0:
                    result["warning"] = (
                        f"DIVERGENCE DETECTED: DB shows {counts['open']} open positions, "
                        f"but Exchange reports 0 active positions. Check for manual closures!"
                    )
            except Exception:
                pass # Best effort

        return result
        
    except Exception as e:
        logger.error(f"[POSITIONS_API] List error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# GET SINGLE POSITION
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/{position_id}", response_model=Dict[str, Any])
def get_position(position_id: str) -> Dict[str, Any]:
    """Get a single position by ID."""
    try:
        service = get_positions_service()
        position = service.get_position(position_id)
        
        if not position:
            raise HTTPException(status_code=404, detail=f"Position {position_id} not found")
        
        return {
            "position": position.to_dict(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[POSITIONS_API] Get error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# GET POSITIONS BY BROKER
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/broker/{broker}", response_model=Dict[str, Any])
def get_positions_by_broker(
    broker: str,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
) -> Dict[str, Any]:
    """Get positions for a specific broker."""
    if broker.lower() not in ("bitget", "kraken"):
        raise HTTPException(status_code=400, detail=f"Invalid broker: {broker}")
    
    try:
        service = get_positions_service()
        
        positions, total = service.get_positions(
            broker=broker,
            status=status_filter,
            limit=limit,
        )
        
        counts = service.get_counts(broker=broker)
        
        return {
            "broker": broker.lower(),
            "positions": [p.to_dict() for p in positions],
            "total": total,
            "open_count": counts["open"],
            "closed_count": counts["closed"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
    except Exception as e:
        logger.error(f"[POSITIONS_API] Get by broker error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# GET OPEN POSITIONS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/open/all", response_model=Dict[str, Any])
def get_open_positions(
    broker: Optional[str] = Query(None, description="Filter by broker"),
) -> Dict[str, Any]:
    """Get all open positions."""
    try:
        service = get_positions_service()
        positions = service.get_open_positions(broker=broker)
        
        # Group by broker
        by_broker: Dict[str, List] = {"bitget": [], "kraken": []}
        for p in positions:
            if p.broker in by_broker:
                by_broker[p.broker].append(p.to_dict())
        
        return {
            "source": "db",
            "status": "OPEN",
            "positions": [p.to_dict() for p in positions],
            "by_broker": by_broker,
            "total": len(positions),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
    except Exception as e:
        logger.error(f"[POSITIONS_API] Get open error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# CREATE POSITION
# ══════════════════════════════════════════════════════════════════════════════

@router.post("", response_model=PositionCreatedResponse, status_code=status.HTTP_201_CREATED)
def create_position(data: PositionCreate) -> PositionCreatedResponse:
    """
    Create a new position record.
    
    Use this endpoint to record a new trade entry.
    """
    try:
        position = record_position_open(
            broker=data.broker,
            symbol=data.symbol,
            side=data.side,
            size=data.size,
            entry_price=data.entry_price,
            leverage=data.leverage,
            stop_loss=data.stop_loss,
            take_profit=data.take_profit,
            order_id=data.order_id,
            session_name=data.session_name,
            preset=data.preset,
            signal_id=data.signal_id,
            raw_response=data.raw_response,
        )
        
        if not position:
            raise HTTPException(status_code=500, detail="Failed to create position")
        
        logger.info(f"[POSITIONS_API] Created position {position.id}")
        
        return PositionCreatedResponse(
            success=True,
            message=f"Position created: {position.symbol} {position.side}",
            position=position.to_dict(),
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[POSITIONS_API] Create error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# CLOSE POSITION
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/{position_id}/close", response_model=PositionClosedResponse)
def close_position(position_id: str, data: PositionClose) -> PositionClosedResponse:
    """
    Close an existing position.
    
    Use this endpoint to record a trade exit.
    """
    try:
        position = record_position_close(
            position_id=position_id,
            exit_price=data.exit_price,
            fee=data.fee,
            close_reason=data.close_reason,
            raw_response=data.raw_response,
        )
        
        if not position:
            raise HTTPException(status_code=404, detail=f"Position {position_id} not found")
        
        logger.info(f"[POSITIONS_API] Closed position {position_id} | PnL={position.pnl}")
        
        return PositionClosedResponse(
            success=True,
            message=f"Position closed: {position.symbol}",
            position=position.to_dict(),
            pnl=position.pnl or 0.0,
            result=position.result or "unknown",
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[POSITIONS_API] Close error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# CLOSE BY ORDER ID
# ══════════════════════════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════════════════════════
# PERFORMANCE STATISTICS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/stats/all", response_model=StatsResponse)
def get_performance_stats(
    broker: Optional[str] = Query(None, description="Filter by broker"),
    from_date: Optional[date] = Query(None, alias="from", description="Start date"),
    to_date: Optional[date] = Query(None, alias="to", description="End date"),
) -> StatsResponse:
    """
    Get performance statistics for closed positions.
    
    Returns:
        - total_pnl: Total realized PnL
        - win_rate: Win rate (0-1)
        - avg_rr: Average R:R ratio
        - total_trades: Total closed trades
        - max_drawdown: Maximum drawdown
        - profit_factor: Gross profit / Gross loss
        - wins/losses/break_even counts
        - avg_win/avg_loss amounts
        - best_trade/worst_trade
        - avg_hold_time_hours
    """
    try:
        service = get_positions_service()
        
        # Convert dates
        date_from = datetime.combine(from_date, datetime.min.time()) if from_date else None
        date_to = datetime.combine(to_date, datetime.max.time()) if to_date else None
        
        metrics = service.calculate_performance(
            broker=broker,
            date_from=date_from,
            date_to=date_to,
        )
        
        logger.info(
            f"[POSITIONS_API] Stats: PnL={metrics.total_pnl:.2f}, "
            f"WR={metrics.win_rate:.2%}, Trades={metrics.total_trades}"
        )
        
        return StatsResponse(
            **metrics.to_dict(),
            broker_filter=broker,
            date_from=str(from_date) if from_date else None,
            date_to=str(to_date) if to_date else None,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
    except Exception as e:
        logger.error(f"[POSITIONS_API] Stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# STATS BY BROKER
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/stats/broker/{broker}", response_model=StatsResponse)
def get_stats_by_broker(
    broker: str,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
) -> StatsResponse:
    """Get performance statistics for a specific broker."""
    if broker.lower() not in ("bitget", "kraken"):
        raise HTTPException(status_code=400, detail=f"Invalid broker: {broker}")
    
    try:
        service = get_positions_service()
        
        date_from = datetime.combine(from_date, datetime.min.time()) if from_date else None
        date_to = datetime.combine(to_date, datetime.max.time()) if to_date else None
        
        metrics = service.calculate_performance(
            broker=broker.lower(),
            date_from=date_from,
            date_to=date_to,
        )
        
        return StatsResponse(
            **metrics.to_dict(),
            broker_filter=broker.lower(),
            date_from=str(from_date) if from_date else None,
            date_to=str(to_date) if to_date else None,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
    except Exception as e:
        logger.error(f"[POSITIONS_API] Stats by broker error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# COMBINED BROKER STATS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/stats/brokers", response_model=Dict[str, Any])
def get_all_broker_stats(
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
) -> Dict[str, Any]:
    """
    Get performance statistics for all brokers, grouped.
    
    Returns stats for each broker plus combined totals.
    """
    try:
        service = get_positions_service()
        
        date_from = datetime.combine(from_date, datetime.min.time()) if from_date else None
        date_to = datetime.combine(to_date, datetime.max.time()) if to_date else None
        
        # Get stats by broker
        broker_metrics = service.get_performance_by_broker(
            date_from=date_from,
            date_to=date_to,
        )
        
        # Get combined stats
        combined = service.calculate_performance(
            date_from=date_from,
            date_to=date_to,
        )
        
        return {
            "bitget": broker_metrics["bitget"].to_dict(),
            "kraken": broker_metrics["kraken"].to_dict(),
            "combined": combined.to_dict(),
            "date_from": str(from_date) if from_date else None,
            "date_to": str(to_date) if to_date else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
    except Exception as e:
        logger.error(f"[POSITIONS_API] All broker stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# QUICK SUMMARY (for dashboard widgets)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/summary", response_model=Dict[str, Any])
def get_positions_summary(
    days: int = Query(30, ge=1, le=365, description="Days to look back"),
) -> Dict[str, Any]:
    """
    Quick summary for dashboard widgets.
    
    Returns recent stats and current open positions count.
    """
    try:
        service = get_positions_service()
        
        # Date range
        date_to = datetime.now(timezone.utc)
        date_from = date_to - timedelta(days=days)
        
        # Get stats
        metrics = service.calculate_performance(date_from=date_from, date_to=date_to)
        
        # Get current open positions
        open_positions = service.get_open_positions()
        
        # Group open by broker
        open_by_broker = {"bitget": 0, "kraken": 0}
        unrealized_pnl = 0.0
        
        for p in open_positions:
            if p.broker in open_by_broker:
                open_by_broker[p.broker] += 1
        
        return {
            "period_days": days,
            "stats": metrics.to_dict(),
            "open_positions": {
                "total": len(open_positions),
                "by_broker": open_by_broker,
            },
            "recent_trades": metrics.total_trades,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
    except Exception as e:
        logger.error(f"[POSITIONS_API] Summary error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# DELETE POSITION (Admin/Testing)
# ══════════════════════════════════════════════════════════════════════════════

@router.delete("/{position_id}", response_model=Dict[str, Any])
def delete_position(position_id: str) -> Dict[str, Any]:
    """Delete a position record (for testing/cleanup)."""
    try:
        service = get_positions_service()
        deleted = service.delete_position(position_id)
        
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Position {position_id} not found")
        
        return {
            "success": True,
            "message": f"Position {position_id} deleted",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[POSITIONS_API] Delete error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
