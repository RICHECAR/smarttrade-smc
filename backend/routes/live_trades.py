"""
Live Trades API Routes - CRUD for LIVE trading persistence

Endpoints:
- GET /api/live/trades - List live trades with filters
- GET /api/live/trades/open - Get currently open trades
- GET /api/live/trades/{trade_id} - Get single trade by ID
- GET /api/live/trades/order/{order_id} - Get trade by Bitget order ID

Author: SMC Trading Bot Framework
"""

from fastapi import APIRouter, Query, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date, datetime, timezone
from typing import List, Dict, Any, Optional
import logging

from backend.db.session import get_db
from backend.services.live_trades_service import LiveTradesService
from backend.models.live_trade import LiveTrade

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/live", tags=["live-trades"])

def _enrich_mirror_status(trade_dicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Enriches the 'status' field for OPEN trades to reflect mirror execution state:
    OPEN_BOTH, OPEN_BITGET_ONLY, OPEN_KRAKEN_ONLY.
    """
    # Group OPEN trades by parent_trade_id (fallback to symbol if missing)
    open_groups = {}
    for td in trade_dicts:
        if td.get("status") == "OPEN":
            key = td.get("signal_id") or td.get("parent_trade_id") or td.get("symbol")
            if key not in open_groups:
                open_groups[key] = set()
            open_groups[key].add(td.get("broker", "kraken").lower())
            
    for td in trade_dicts:
        if td.get("status") == "OPEN":
            key = td.get("signal_id") or td.get("parent_trade_id") or td.get("symbol")
            brokers = open_groups.get(key, set())
            if "bitget" in brokers and "kraken" in brokers:
                td["status"] = "OPEN_BOTH"
            elif "kraken" in brokers and "bitget" not in brokers:
                td["status"] = "OPEN_KRAKEN_ONLY"
            elif "bitget" in brokers and "kraken" not in brokers:
                td["status"] = "OPEN_BITGET_ONLY"
                
    return trade_dicts


@router.get("/trades", response_model=List[Dict[str, Any]])
def get_live_trades(
    from_: date = Query(None, alias="from", description="Start date"),
    to: date = Query(None, description="End date"),
    status: Optional[str] = Query(None, description="Filter by status (OPEN/CLOSED)"),
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    broker: Optional[str] = Query(None, description="Filter by broker (bitget/kraken)"),
    include_archived: bool = Query(False, description="Include archived trades"),
) -> List[Dict[str, Any]]:
    """
    List live trades with optional filters.
    Supports broker filtering for multi-broker setups.
    """
    try:
        # Date range defaults
        if from_ is None:
            from_ = date(2020, 1, 1)
        if to is None:
            to = date(2099, 12, 31)
        
        from_dt = datetime.combine(from_, datetime.min.time()).replace(tzinfo=timezone.utc)
        to_dt = datetime.combine(to, datetime.max.time()).replace(tzinfo=timezone.utc)
        
        # We handle include_archived logically based on explicit requests too
        effective_include_archived = include_archived or (status and status.upper() == "ARCHIVED")
        
        trades = LiveTradesService.get_trades_by_date_range(from_dt, to_dt, status, include_archived=effective_include_archived)
        
        # Exclude ARCHIVED manually if it slips through (legacy code replacement)
        if not effective_include_archived:
            trades = [t for t in trades if getattr(t, 'status', '') != 'ARCHIVED']
        
        # Filter by symbol if provided
        if symbol:
            trades = [t for t in trades if t.symbol == symbol.upper()]
        
        # Filter by broker if provided
        if broker:
            trades = [t for t in trades if getattr(t, 'broker', 'kraken') == broker.lower()]
        
        logger.info(f"[LIVE_API] Found {len(trades)} trades from {from_} to {to} (broker={broker})")
        trade_dicts = [t.to_dict() for t in trades]
        return _enrich_mirror_status(trade_dicts)
        
    except Exception as e:
        logger.error(f"[LIVE_API] Error listing trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trades/open", response_model=List[Dict[str, Any]])
def get_open_trades(
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    broker: Optional[str] = Query(None, description="Filter by broker (bitget/kraken)"),
) -> List[Dict[str, Any]]:
    """
    Get all currently OPEN trades.
    Supports broker filtering for multi-broker setups.
    """
    try:
        trades = LiveTradesService.get_open_trades(symbol, broker=broker.lower() if broker else None)
        
        logger.info(f"[LIVE_API] Found {len(trades)} OPEN trades (broker={broker})")
        trade_dicts = [t.to_dict() for t in trades]
        return _enrich_mirror_status(trade_dicts)
        
    except Exception as e:
        logger.error(f"[LIVE_API] Error getting open trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trades/{trade_id}", response_model=Dict[str, Any])
def get_trade_by_id(trade_id: str) -> Dict[str, Any]:
    """
    Get single trade by internal ID.
    """
    trade = LiveTradesService.get_trade_by_id(trade_id)
    
    if not trade:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")
    
    return trade.to_dict()


@router.get("/trades/order/{order_id}", response_model=Dict[str, Any])
def get_trade_by_order_id(
    order_id: str,
    broker: Optional[str] = Query(None, description="Broker (bitget/kraken) - required if order_id exists on multiple brokers"),
) -> Dict[str, Any]:
    """
    Get trade by exchange order ID.
    For multi-broker setups, specify broker to disambiguate.
    """
    trade = LiveTradesService.get_trade_by_order_id(order_id, broker=broker.lower() if broker else None)
    
    if not trade:
        raise HTTPException(status_code=404, detail=f"Trade with order_id {order_id} not found")
    
    return trade.to_dict()


@router.get("/stats/summary", response_model=Dict[str, Any])
def get_live_stats_summary(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
) -> Dict[str, Any]:
    """
    Get summary statistics for LIVE trades.
    """
    try:
        from_dt = datetime.combine(from_, datetime.min.time()).replace(tzinfo=timezone.utc)
        to_dt = datetime.combine(to, datetime.max.time()).replace(tzinfo=timezone.utc)
        
        trades = LiveTradesService.get_trades_by_date_range(from_dt, to_dt, status="CLOSED")
        
        if not trades:
            return {
                "pnl": 0, "win_rate": 0, "avg_rr": 0,
                "trades": 0, "max_dd": 0, "profit_factor": 0,
                "wins": 0, "losses": 0, "total_fees": 0
            }
        
        # Calculate metrics
        total_pnl = sum(t.pnl or 0 for t in trades)
        total_fees = sum(t.fee or 0 for t in trades)
        
        wins = [t for t in trades if t.result == "win"]
        losses = [t for t in trades if t.result == "loss"]
        
        decisive = len(wins) + len(losses)
        win_rate = len(wins) / decisive if decisive > 0 else 0.0
        avg_rr = sum(t.rr or 0 for t in trades) / len(trades) if trades else 0.0
        
        # Profit factor
        gross_profit = sum(t.pnl for t in wins if (t.pnl or 0) > 0)
        gross_loss = abs(sum(t.pnl for t in losses if (t.pnl or 0) < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else gross_profit if gross_profit > 0 else 0.0
        
        # Max drawdown
        sorted_trades = sorted(trades, key=lambda x: x.ts_open or datetime.min.replace(tzinfo=timezone.utc))
        cum_pnl = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in sorted_trades:
            cum_pnl += (t.pnl or 0)
            peak = max(peak, cum_pnl)
            dd = peak - cum_pnl
            max_dd = max(max_dd, dd)
        
        return {
            "pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 4),
            "avg_rr": round(avg_rr, 2),
            "trades": len(trades),
            "max_dd": round(max_dd, 2),
            "profit_factor": round(profit_factor, 2),
            "wins": len(wins),
            "losses": len(losses),
            "total_fees": round(total_fees, 2),
        }
        
    except Exception as e:
        logger.error(f"[LIVE_API] Error calculating stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trades/mirror/{parent_id}", response_model=List[Dict[str, Any]])
def get_mirror_trades(parent_id: str) -> List[Dict[str, Any]]:
    """
    Get all trades linked to a parent trade ID (mirror execution group).
    Returns trades from all brokers that were executed together.
    """
    try:
        trades = LiveTradesService.get_trades_by_parent_id(parent_id)
        
        if not trades:
            raise HTTPException(status_code=404, detail=f"No trades found for parent_id {parent_id}")
        
        logger.info(f"[LIVE_API] Found {len(trades)} mirror trades for parent {parent_id}")
        trade_dicts = [t.to_dict() for t in trades]
        return _enrich_mirror_status(trade_dicts)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[LIVE_API] Error getting mirror trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats/brokers", response_model=Dict[str, Any])
def get_broker_stats(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
) -> Dict[str, Any]:
    """
    Get statistics broken down by broker.
    Returns per-broker PnL, trade counts, win rates, etc.
    """
    try:
        from_dt = datetime.combine(from_, datetime.min.time()).replace(tzinfo=timezone.utc)
        to_dt = datetime.combine(to, datetime.max.time()).replace(tzinfo=timezone.utc)
        
        trades = LiveTradesService.get_trades_by_date_range(from_dt, to_dt, status="CLOSED")
        
        def compute_broker_stats(broker_trades):
            if not broker_trades:
                return {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "fees": 0}
            
            total_pnl = sum(t.pnl or 0 for t in broker_trades)
            total_fees = sum(t.fee or 0 for t in broker_trades)
            wins = len([t for t in broker_trades if t.result == "win"])
            losses = len([t for t in broker_trades if t.result == "loss"])
            decisive = wins + losses
            win_rate = wins / decisive if decisive > 0 else 0.0
            
            return {
                "pnl": round(total_pnl, 2),
                "trades": len(broker_trades),
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 4),
                "fees": round(total_fees, 2),
            }
        
        # Group by broker
        bitget_trades = [t for t in trades if getattr(t, 'broker', 'kraken') == 'bitget']
        kraken_trades = [t for t in trades if getattr(t, 'broker', 'kraken') == 'kraken']
        
        result = {
            "bitget": compute_broker_stats(bitget_trades),
            "kraken": compute_broker_stats(kraken_trades),
            "combined": compute_broker_stats(trades),
            "date_range": {"from": str(from_), "to": str(to)},
        }
        
        logger.info(f"[LIVE_API] Broker stats: {len(bitget_trades)} Bitget, {len(kraken_trades)} Kraken")
        return result
        
    except Exception as e:
        logger.error(f"[LIVE_API] Error calculating broker stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION CLOSE/REDUCE ROUTES (Unified via ClosePositionService)
# ═══════════════════════════════════════════════════════════════════════════════

# Global position manager reference (set during app startup) - kept for backward compat
_position_manager = None


def set_position_manager(pm):
    """Set the PositionManager reference (backward compatibility)."""
    global _position_manager
    _position_manager = pm
    logger.info("[LIVE_API] PositionManager registered")


def get_position_manager():
    """Get the PositionManager instance or raise error."""
    if _position_manager is None:
        raise HTTPException(status_code=503, detail="PositionManager not initialized")
    return _position_manager


@router.post("/position/close/{symbol}", response_model=Dict[str, Any])
async def close_position_endpoint(
    symbol: str,
    hold_side: Optional[str] = Query(None, description="long or short (auto-detect if omitted)"),
    reason: str = Query("manual_close", description="Close reason for logging"),
    timeout_sec: float = Query(10.0, ge=1.0, le=60.0, description="Confirmation timeout"),
) -> Dict[str, Any]:
    """
    Close entire position with market order (optimized, safe, idempotent).
    
    Features:
    - Per-symbol locking (prevents double-close from UI + bot)
    - Idempotent: returns OK if no position or already closing
    - reduceOnly=True always enforced
    - Polls for confirmation with timeout
    - Persists to DB + broadcasts via WebSocket
    
    Responses:
    - success=True, status="success" → Position fully closed
    - success=True, status="already_closed" → No position (idempotent OK)
    - success=True, status="in_progress" → Another close is running
    - success=False, status="failed" → Execution error
    - success=False, status="timeout" → Order sent but confirmation timed out
    """
    from backend.services.close_position_service import close_position
    
    symbol = symbol.upper().replace("/", "")
    
    logger.info(f"[LIVE_API] CLOSE_POSITION | {symbol} | side={hold_side or 'auto'} | reason={reason}")
    
    try:
        result = await close_position(
            symbol=symbol,
            percent=100.0,
            reason=reason,
            timeout_sec=timeout_sec,
            hold_side=hold_side,
        )
        
        response = result.to_dict()
        
        if result.success:
            logger.info(f"[LIVE_API] CLOSE_POSITION {result.status.value.upper()} | {symbol}")
        else:
            logger.warning(f"[LIVE_API] CLOSE_POSITION {result.status.value.upper()} | {symbol} | {result.message}")
        
        return response
        
    except Exception as e:
        logger.error(f"[LIVE_API] CLOSE_POSITION ERROR | {symbol} | {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/position/reduce/{symbol}", response_model=Dict[str, Any])
async def reduce_position_endpoint(
    symbol: str,
    pct: float = Query(50.0, ge=1.0, le=99.0, description="Percentage to reduce (1-99)"),
    hold_side: Optional[str] = Query(None, description="long or short (auto-detect if omitted)"),
    reason: str = Query("partial_close", description="Reduce reason for logging"),
    timeout_sec: float = Query(10.0, ge=1.0, le=60.0, description="Confirmation timeout"),
) -> Dict[str, Any]:
    """
    Reduce position by percentage with market order (optimized, safe).
    
    Features:
    - Per-symbol locking (prevents double reduce)
    - Validates qty against contract specs (min_qty, qty_step)
    - Auto-converts to full close if remaining would be < min_qty
    - reduceOnly=True always enforced
    - Polls for confirmation with timeout
    - Persists to DB + broadcasts via WebSocket
    
    Responses:
    - success=True, status="partial" → Position reduced successfully
    - success=True, status="success" → Auto-converted to full close
    - success=True, status="already_closed" → No position (idempotent OK)
    - success=False, status="rejected" → Qty validation failed
    - success=False, status="failed" → Execution error
    """
    from backend.services.close_position_service import close_position
    
    symbol = symbol.upper().replace("/", "")
    
    logger.info(f"[LIVE_API] REDUCE_POSITION | {symbol} | pct={pct}% | side={hold_side or 'auto'} | reason={reason}")
    
    try:
        result = await close_position(
            symbol=symbol,
            percent=pct,
            reason=reason,
            timeout_sec=timeout_sec,
            hold_side=hold_side,
        )
        
        response = result.to_dict()
        
        if result.success:
            logger.info(f"[LIVE_API] REDUCE_POSITION {result.status.value.upper()} | {symbol} | {pct}%")
        else:
            logger.warning(f"[LIVE_API] REDUCE_POSITION {result.status.value.upper()} | {symbol} | {result.message}")
        
        return response
        
    except Exception as e:
        logger.error(f"[LIVE_API] REDUCE_POSITION ERROR | {symbol} | {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/position/close-status/{symbol}", response_model=Dict[str, Any])
async def get_close_status(symbol: str) -> Dict[str, Any]:
    """
    Check if a close operation is in progress for a symbol.
    
    Returns:
        {"in_progress": bool, "order_id": str | null, "symbol": str}
    """
    from backend.services.close_position_service import is_close_in_progress, get_active_locks
    
    symbol = symbol.upper().replace("/", "")
    in_progress = is_close_in_progress(symbol)
    active_locks = get_active_locks()
    
    return {
        "in_progress": in_progress,
        "order_id": active_locks.get(symbol),
        "symbol": symbol,
    }


@router.get("/trades/{trade_id}/fills", response_model=List[Dict[str, Any]])
def get_trade_fills(trade_id: str) -> List[Dict[str, Any]]:
    """
    Get all fills for a trade.
    """
    fills = LiveTradesService.get_fills_for_trade(trade_id)
    
    if not fills:
        return []
    
    return [f.to_dict() for f in fills]


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN ENDPOINTS — Force-close legacy OPEN trades
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/admin/force-close-legacy", response_model=Dict[str, Any])
def force_close_legacy(
    max_age_hours: int = Query(24, description="Close OPEN trades older than N hours"),
    dry_run: bool = Query(True, description="If true, only list trades that WOULD be closed"),
) -> Dict[str, Any]:
    """
    Force-close OPEN trades older than max_age_hours.
    
    Use dry_run=true first to preview. Set dry_run=false to execute.
    PnL is set to 0.0 (unknown) with close_reason=legacy_force_close.
    """
    try:
        from backend.services.live_trades_service import get_db_session
        
        cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=max_age_hours)
        
        with get_db_session() as db:
            stale = (
                db.query(LiveTrade)
                .filter(
                    LiveTrade.status == "OPEN",
                    LiveTrade.ts_open < cutoff,
                )
                .all()
            )
            
            candidates = []
            for t in stale:
                candidates.append({
                    "id": t.id,
                    "symbol": t.symbol,
                    "side": t.side,
                    "broker": t.broker or "bitget",
                    "ts_open": t.ts_open.isoformat() if t.ts_open else None,
                    "order_id": t.order_id,
                    "entry": t.entry,
                    "age_hours": round((datetime.now(timezone.utc) - t.ts_open).total_seconds() / 3600, 1) if t.ts_open else None,
                })
            
            closed_count = 0
            if not dry_run:
                now = datetime.now(timezone.utc)
                for t in stale:
                    t.status = "CLOSED"
                    t.ts_close = now
                    t.close_price = t.entry  # No fill data available
                    # NOTE: Do NOT set pnl=0.0 / result="be" — that pollutes
                    # stats with false break-even trades.  Leave pnl=None so
                    # the trade is excluded from statistics.
                    t.pnl = None
                    t.result = None
                    t.close_reason = "legacy_force_close"
                    closed_count += 1
                db.commit()
                logger.warning(
                    "[ADMIN] Force-closed %d legacy OPEN trades older than %dh",
                    closed_count, max_age_hours,
                )
            
        return {
            "dry_run": dry_run,
            "cutoff_hours": max_age_hours,
            "candidates": len(candidates),
            "closed": closed_count,
            "trades": candidates,
        }
        
    except Exception as e:
        logger.error(f"[ADMIN] Force-close error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/force-reconcile", response_model=Dict[str, Any])
async def force_reconcile() -> Dict[str, Any]:
    """
    Trigger a manual reconciliation pass (same as auto sync loop).
    Requires the trade_reconciler to be initialized.
    """
    try:
        from smc_futures.api_smc_engine import trade_reconciler
        
        if not trade_reconciler:
            return {"success": False, "error": "Trade reconciler not initialized (is the bot running in LIVE mode?)"}
        
        await trade_reconciler.sync_positions()
        
        # Return current state after sync
        open_trades = LiveTradesService.get_open_trades()
        return {
            "success": True,
            "remaining_open": len(open_trades),
            "message": f"Reconciliation pass complete. {len(open_trades)} trades still OPEN.",
        }
        
    except Exception as e:
        logger.error(f"[ADMIN] Force-reconcile error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/db-status", response_model=Dict[str, Any])
def get_db_status() -> Dict[str, Any]:
    """
    Diagnostic endpoint — counts trades by status, shows if any CLOSED have PnL.
    Useful for debugging dashboard/stats issues.
    """
    try:
        from backend.services.live_trades_service import get_db_session

        with get_db_session() as db:
            all_trades = db.query(LiveTrade).all()

            by_status: Dict[str, int] = {}
            closed_with_pnl = 0
            closed_unknown_pnl = 0
            sample_open = []
            sample_closed = []

            for t in all_trades:
                s = (t.status or "UNKNOWN").upper()
                by_status[s] = by_status.get(s, 0) + 1

                if s == "CLOSED":
                    if t.pnl is not None:
                        closed_with_pnl += 1
                    else:
                        closed_unknown_pnl += 1
                    if len(sample_closed) < 5:
                        sample_closed.append({
                            "id": t.id[:12], "symbol": t.symbol,
                            "pnl": t.pnl, "result": t.result,
                            "close_reason": t.close_reason,
                        })
                elif s == "OPEN" and len(sample_open) < 5:
                    sample_open.append({
                        "id": t.id[:12], "symbol": t.symbol,
                        "broker": t.broker, "side": t.side,
                    })

            for t in all_trades:
                db.expunge(t)

        return {
            "total_trades": len(all_trades),
            "by_status": by_status,
            "closed_with_pnl": closed_with_pnl,
            "closed_unknown_pnl": closed_unknown_pnl,
            "sample_open": sample_open,
            "sample_closed": sample_closed,
            "stats_eligible": closed_with_pnl,
        }

    except Exception as e:
        logger.error(f"[ADMIN] db-status error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
