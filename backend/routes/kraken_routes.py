"""
Kraken Futures API Routes — Balance & Account Info
════════════════════════════════════════════════════

FastAPI endpoints for Kraken Futures account data.

Endpoints:
  GET /api/kraken/balance      - Get account balance
  GET /api/kraken/positions    - Get open positions  
  GET /api/kraken/status       - Test connection status
  GET /api/kraken/pnl          - Unrealized PnL (positions + live price)
  GET /api/kraken/pnl/realized - Realized PnL from fills history
  GET /api/kraken/pnl/full     - Unified report (unrealized + realized + risk)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import logging

# Use V2 client with corrected authentication
from backend.kraken_futures_rest_v2 import get_kraken_futures_client
from backend.services.kraken_pnl_service import get_kraken_pnl_service

logger = logging.getLogger("smarttrade.routes.kraken")

router = APIRouter(prefix="/api/kraken", tags=["kraken"])


# ══════════════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ══════════════════════════════════════════════════════════════════════════════

class BalanceResponse(BaseModel):
    """Response model for balance endpoint."""
    available: float
    total: float
    margin_used: float
    unrealized_pnl: float
    currency: str
    timestamp: str


class PositionResponse(BaseModel):
    """Response model for position data."""
    symbol: str
    side: str
    size: float
    entry_price: float
    unrealized_pnl: float


class StatusResponse(BaseModel):
    """Response model for connection status."""
    success: bool
    authenticated: bool
    balance: Optional[BalanceResponse] = None
    error: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/balance", response_model=BalanceResponse)
async def get_kraken_balance(force_refresh: bool = False):
    """
    Get Kraken Futures account balance.
    
    Query Parameters:
        force_refresh: Bypass cache and fetch fresh data
        
    Returns:
        BalanceResponse with available, total, margin_used, unrealized_pnl
    """
    try:
        client = get_kraken_futures_client()
        balance_dict = await client.get_balance_dict(force_refresh)
        
        logger.info("[KRAKEN_ROUTE] Balance: available=%.2f", balance_dict["available"])
        
        return BalanceResponse(**balance_dict)
        
    except ValueError as e:
        logger.error("[KRAKEN_ROUTE] Balance error: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("[KRAKEN_ROUTE] Balance failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to get balance: {e}")


@router.get("/positions")
async def get_kraken_positions(force_refresh: bool = False):
    """
    Get Kraken Futures open positions.
    
    Query Parameters:
        force_refresh: Bypass cache and fetch fresh data
    
    Returns:
        List of position dictionaries
    """
    try:
        client = get_kraken_futures_client()
        positions = await client.get_positions_dict(force_refresh)
        
        logger.info("[KRAKEN_ROUTE] Returned %d positions", len(positions))
        return positions
        
    except ValueError as e:
        logger.error("[KRAKEN_ROUTE] Positions error: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("[KRAKEN_ROUTE] Positions failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to get positions: {e}")


@router.get("/status", response_model=StatusResponse)
async def get_kraken_status():
    """
    Test Kraken Futures API connection and authentication.
    
    Returns:
        StatusResponse with connection status and balance if successful
    """
    try:
        client = get_kraken_futures_client()
        result = await client.test_connection()
        
        balance_response = None
        if result.get("balance"):
            balance_response = BalanceResponse(**result["balance"])
        
        return StatusResponse(
            success=result["success"],
            authenticated=result["authenticated"],
            balance=balance_response,
            error=result.get("error"),
        )
        
    except Exception as e:
        logger.error("[KRAKEN_ROUTE] Status check failed: %s", e)
        return StatusResponse(
            success=False,
            authenticated=False,
            error=str(e),
        )


@router.post("/check-margin")
async def check_margin_sufficient(required_usd: float):
    """
    Check if available margin is sufficient for a trade.
    
    Query Parameters:
        required_usd: Required margin amount in USD
        
    Returns:
        Dict with is_sufficient, available, required
    """
    try:
        client = get_kraken_futures_client()
        is_sufficient, available = await client.check_margin(required_usd)
        
        return {
            "is_sufficient": is_sufficient,
            "available": round(available, 2),
            "required": required_usd,
            "shortfall": max(0, round(required_usd - available, 2)),
        }
        
    except Exception as e:
        logger.error("[KRAKEN_ROUTE] Margin check failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Margin check failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# PNL ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/pnl")
async def get_kraken_pnl(force_refresh: bool = False):
    """
    Get unrealized PnL for all open Kraken Futures positions.

    Uses mark_price from /tickers for accurate futures PnL:
      Long:  (mark_price - entry_price) * size
      Short: (entry_price - mark_price) * size

    Includes estimated round-trip fees, unrealized funding, and risk metrics.

    Query Parameters:
        force_refresh: Bypass cache for position data

    Returns:
        {
            "total_unrealized_pnl": float,
            "total_estimated_fees": float,
            "total_net_pnl": float,
            "position_count": int,
            "positions": [...],
            "risk": { "equity", "drawdown_pct", ... } | null,
            "timestamp": float
        }
    """
    try:
        service = get_kraken_pnl_service()
        summary = await service.get_unrealized_pnl(force_refresh)

        logger.info(
            "[KRAKEN_ROUTE] PnL: %d positions | net=$%.4f",
            len(summary.positions), summary.total_net_pnl,
        )

        return summary.to_dict()

    except ValueError as e:
        logger.error("[KRAKEN_ROUTE] PnL error: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("[KRAKEN_ROUTE] PnL failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to compute PnL: {e}")


@router.get("/pnl/realized")
async def get_kraken_realized_pnl(
    symbol: Optional[str] = None,
    since_ms: Optional[int] = None,
):
    """
    Get realized PnL from Kraken Futures fills/trades history.

    Uses /fills endpoint (NOT ClosedOrders) to extract actual PnL and fees.
    Fees are subtracted per trade to give net_pnl. Includes win/loss stats.

    Query Parameters:
        symbol: Filter by Kraken symbol (e.g. PF_XBTUSD)
        since_ms: Only include fills after this epoch timestamp (ms)

    Returns:
        {
            "total_pnl": float,
            "total_fees": float,
            "net_pnl": float,
            "trade_count": int,
            "winning_trades": int,
            "losing_trades": int,
            "win_rate": float,
            "trades": [...]
        }
    """
    try:
        service = get_kraken_pnl_service()
        summary = await service.get_realized_pnl(symbol=symbol, since_ms=since_ms)

        logger.info(
            "[KRAKEN_ROUTE] Realized PnL: %d fills | net=$%.4f | win_rate=%.1f%%",
            summary.trade_count, summary.net_pnl, summary.win_rate * 100,
        )

        return summary.to_dict()

    except ValueError as e:
        logger.error("[KRAKEN_ROUTE] Realized PnL error: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("[KRAKEN_ROUTE] Realized PnL failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to get realized PnL: {e}")


@router.get("/pnl/full")
async def get_kraken_full_pnl(
    force_refresh: bool = False,
    realized_symbol: Optional[str] = None,
    realized_since_ms: Optional[int] = None,
):
    """
    Unified PnL report: unrealized + realized + risk in one call.

    Combines /pnl and /pnl/realized into a single response.

    Query Parameters:
        force_refresh: Bypass position cache
        realized_symbol: Filter realized fills by symbol
        realized_since_ms: Filter realized fills after this timestamp (ms)

    Returns:
        {
            "unrealized": { "total": float, "positions": [...] },
            "realized": float,
            "realized_details": { "trade_count": int, "win_rate": float, ... },
            "total_pnl": float,
            "risk": { "equity": float, "peak_equity": float, "drawdown": float, ... },
            "timestamp": float
        }
    """
    try:
        service = get_kraken_pnl_service()
        report = await service.get_full_pnl(
            force_refresh=force_refresh,
            realized_symbol=realized_symbol,
            realized_since_ms=realized_since_ms,
        )

        logger.info(
            "[KRAKEN_ROUTE] Full PnL: total=$%.4f | unrealized=$%.4f | realized=$%.4f",
            report.total_pnl,
            report.unrealized_summary.total_net_pnl,
            report.realized_summary.net_pnl,
        )

        return report.to_dict()

    except ValueError as e:
        logger.error("[KRAKEN_ROUTE] Full PnL error: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("[KRAKEN_ROUTE] Full PnL failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to compute full PnL: {e}")
