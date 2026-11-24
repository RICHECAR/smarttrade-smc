"""
Trading Engine API Routes — FastAPI Endpoints
══════════════════════════════════════════════

Provides REST endpoints for the Performance Layer trading engine.

Endpoints:
  GET  /api/trading/status          — Engine state + performance
  GET  /api/trading/performance     — Detailed performance snapshot
  GET  /api/trading/positions       — Tracked positions
  POST /api/trading/start           — Start the trading loop
  POST /api/trading/stop            — Stop the trading loop
  POST /api/trading/kill-switch     — Toggle kill switch
  POST /api/trading/evaluate/{sym}  — Manual signal evaluation (dry test)

Author: Performance Layer
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.services.trading.trading_loop import get_trading_loop

logger = logging.getLogger("smarttrade.trading.routes")

router = APIRouter(prefix="/api/trading", tags=["Trading Engine"])


# ═══════════════════════════════════════════════════════════════
#  REQUEST / RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════

class KillSwitchRequest(BaseModel):
    enabled: bool = Field(..., description="True to activate, False to reset")
    reason: str = Field(default="manual", description="Reason for toggle")


class EvaluateResponse(BaseModel):
    signal: Optional[Dict[str, Any]] = None
    risk_verdict: Optional[Dict[str, Any]] = None
    sizing: Optional[Dict[str, Any]] = None
    execution_plan: Optional[Dict[str, Any]] = None
    blocked: bool = False
    block_reason: str = ""


# ═══════════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.get("/status")
async def get_trading_status():
    """Full engine state: running, cycle count, last signal, performance."""
    loop = get_trading_loop()
    return {
        "success": True,
        "data": loop.state.to_dict(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/performance")
async def get_performance():
    """Detailed performance metrics snapshot."""
    loop = get_trading_loop()
    snapshot = loop.performance.snapshot()
    return {
        "success": True,
        "data": snapshot.to_dict(),
        "trade_count": loop.performance.trade_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/positions")
async def get_tracked_positions():
    """Positions currently managed by the execution engine."""
    loop = get_trading_loop()
    positions = loop.execution.get_tracked_positions()
    return {
        "success": True,
        "data": positions,
        "count": len(positions),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/start")
async def start_trading():
    """Start the async trading loop."""
    loop = get_trading_loop()
    if loop.state.running:
        return {"success": False, "message": "Already running"}
    await loop.start()
    return {
        "success": True,
        "message": "Trading loop started",
        "dry_run": loop._dry_run,
        "symbols": loop._symbols,
        "interval_sec": loop._interval,
    }


@router.post("/stop")
async def stop_trading():
    """Stop the trading loop gracefully."""
    loop = get_trading_loop()
    if not loop.state.running:
        return {"success": False, "message": "Not running"}
    await loop.stop()
    return {
        "success": True,
        "message": "Trading loop stopped",
        "total_cycles": loop.state.cycle_count,
    }


@router.post("/kill-switch")
async def toggle_kill_switch(req: KillSwitchRequest):
    """Activate or reset the kill switch."""
    loop = get_trading_loop()
    if req.enabled:
        loop.risk._kill_switch = True
        loop.risk._kill_reason = req.reason
        # Emergency close all tracked positions
        prices: Dict[str, float] = {}
        for sym in loop._symbols:
            if loop._price_fetcher:
                try:
                    p = await loop._price_fetcher(sym)
                    if p > 0:
                        prices[sym] = p
                except Exception as e:
                    logger.warning("[ROUTES] Price fetch failed for %s during emergency: %s", sym, e)
        plans = loop.execution.plan_emergency_close_all(prices)
        logger.critical("[ROUTES] Kill switch activated: %s | %d positions to close", req.reason, len(plans))
        return {
            "success": True,
            "message": f"Kill switch ACTIVATED: {req.reason}",
            "emergency_close_count": len(plans),
        }
    else:
        loop.risk.reset_kill_switch()
        return {
            "success": True,
            "message": "Kill switch RESET",
        }


@router.post("/evaluate/{symbol}")
async def evaluate_symbol(
    symbol: str,
    price: float = Query(..., gt=0, description="Current mark price"),
    balance: float = Query(default=1000.0, gt=0, description="Account balance for sizing"),
):
    """
    Dry-run evaluation: run the full pipeline for a symbol without executing.
    Useful for testing signal generation and sizing.
    """
    loop = get_trading_loop()

    # Evaluate signal
    signal = loop.decision.evaluate(symbol.upper(), price)
    if not signal:
        return EvaluateResponse(blocked=True, block_reason="No signal generated")

    # Risk check
    verdict = loop.risk.evaluate(signal, balance)

    if verdict.action.value in ("BLOCK", "KILL"):
        return EvaluateResponse(
            signal=signal.to_dict(),
            risk_verdict=verdict.to_dict(),
            blocked=True,
            block_reason=verdict.reason,
        )

    # Sizing
    vol_regime = loop.decision.get_volatility_regime(symbol.upper())
    perf = loop.performance.snapshot()
    sizing = loop.sizing.compute(
        signal=signal,
        balance=balance,
        consecutive_wins=perf.consecutive_wins,
        consecutive_losses=perf.consecutive_losses,
        volatility_regime=vol_regime,
    )

    # Execution plan (but don't track it — this is read-only)
    plan_dict = None
    if not sizing.rejected:
        plan_dict = {
            "action": "OPEN",
            "symbol": signal.symbol,
            "side": "buy" if signal.direction.value == "long" else "sell",
            "quantity": round(sizing.quantity, 6),
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit_1,
            "reason": "; ".join(signal.reasons[:3]),
        }

    return EvaluateResponse(
        signal=signal.to_dict(),
        risk_verdict=verdict.to_dict(),
        sizing=sizing.to_dict(),
        execution_plan=plan_dict,
        blocked=sizing.rejected,
        block_reason=sizing.reject_reason if sizing.rejected else "",
    )
