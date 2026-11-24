"""
Multi-Broker API Routes — FastAPI Endpoints for Dual-Broker Trading
═══════════════════════════════════════════════════════════════════

Provides REST API endpoints for managing multi-broker trading operations
across Bitget and Kraken Futures.

Endpoints:
  GET  /api/multi/status         - Status of all brokers
  GET  /api/multi/balances       - Balances from all brokers
  GET  /api/multi/candles        - Candles from both brokers
  POST /api/multi/order          - Execute order on specified broker(s)
  POST /api/multi/order/all      - Execute order on ALL brokers
  GET  /api/multi/positions      - Positions across all brokers
  POST /api/multi/start          - Start multi-broker trading loop
  POST /api/multi/stop           - Stop multi-broker trading loop

Usage:
    from backend.routes.multi_broker import router as multi_broker_router
    app.include_router(multi_broker_router)

Author: SmartTrade AI Framework
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict

logger = logging.getLogger("smarttrade.multi_broker_routes")

router = APIRouter(prefix="/api/multi", tags=["Multi-Broker"])

# ══════════════════════════════════════════════════════════════════════════════
# MODULE STATE
# ══════════════════════════════════════════════════════════════════════════════

# Engine instance (set from main app)
_engine = None
_trading_task = None


def set_multi_broker_engine(engine):
    """Set the multi-broker engine instance."""
    global _engine
    _engine = engine
    logger.info("[ROUTES] Multi-broker engine registered")


def get_engine():
    """Get the engine instance or raise error."""
    if not _engine:
        raise HTTPException(status_code=503, detail="Multi-broker engine not initialized")
    return _engine


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST/RESPONSE MODELS
# ══════════════════════════════════════════════════════════════════════════════

class OrderRequest(BaseModel):
    """Order request payload."""
    symbol: str = Field(..., description="Trading symbol (e.g., BTCUSDT)")
    side: str = Field(..., description="Order side: 'buy' or 'sell'")
    quantity: float = Field(..., gt=0, description="Order quantity")
    order_type: str = Field(default="market", description="Order type: 'market' or 'limit'")
    price: Optional[float] = Field(default=None, description="Limit price (required for limit orders)")
    stop_loss: Optional[float] = Field(default=None, description="Stop-loss price")
    take_profit: Optional[float] = Field(default=None, description="Take-profit price")
    broker: Optional[str] = Field(default=None, description="Specific broker: 'bitget', 'kraken', or None for all")
    
    model_config = ConfigDict(extra="forbid")


class MultiOrderRequest(BaseModel):
    """Order request for all brokers."""
    symbol: str
    side: str
    quantity_per_broker: float = Field(..., gt=0, description="Quantity for each broker")
    order_type: str = "market"
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    
    model_config = ConfigDict(extra="forbid")


# Note: Uses Balance dataclass from multi_broker_engine


# ══════════════════════════════════════════════════════════════════════════════
# STATUS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/status")
async def get_multi_broker_status():
    """
    Get status of all configured brokers (Bitget, Kraken Futures, MetaTrader 5).
    Also surfaces read-only connected brokers from AppContext without granting
    execution authority to MultiBrokerEngine.
    """
    try:
        engine = get_engine()
        status = engine.get_status()
    except HTTPException:
        status = {
            "running": False,
            "brokers": {},
            "statistics": {},
            "error": "Engine not initialized",
        }

    brokers = status.setdefault("brokers", {})

    # Enrich with read-only / connected executors from AppContext for accurate UI visibility
    try:
        from smc_futures.api_smc_engine import get_app_context
        app_ctx = get_app_context()

        # Kraken Futures (may be connected in READ_ONLY mode when KRAKEN_EXECUTION_ENABLED=false)
        k_exc = app_ctx.executors.get("kraken_futures")
        if k_exc is not None and "kraken" not in brokers:
            k_conn = k_exc.is_connected() if callable(getattr(k_exc, "is_connected", None)) else bool(getattr(k_exc, "is_connected", True))
            k_bal = getattr(app_ctx, "_kraken_balance", None)
            if k_bal is None and k_conn and hasattr(k_exc, "get_account_info"):
                try:
                    k_bal = await k_exc.get_account_info()
                    app_ctx._kraken_balance = k_bal
                except Exception:
                    k_bal = None
            brokers["kraken"] = {
                "connected": bool(k_conn),
                "mode": getattr(app_ctx, "_kraken_mode", "read_only"),
                "last_heartbeat": datetime.now(timezone.utc).isoformat() if k_conn else None,
                "error": None,
                "balance": {
                    "available": float(k_bal.get("available", 0.0)),
                    "total": float(k_bal.get("total", k_bal.get("available", 0.0))),
                    "unrealized_pnl": float(k_bal.get("unrealizedPL", k_bal.get("unrealized_pnl", 0.0))),
                    "currency": str(k_bal.get("currency", "USD")),
                } if isinstance(k_bal, dict) else None,
            }

        # MetaTrader 5 (MT5)
        mt5_exc = app_ctx.executors.get("mt5")
        if mt5_exc is not None:
            mt5_bal = getattr(app_ctx, "_mt5_balance", None)
            if mt5_bal is None and getattr(mt5_exc, "is_connected", False):
                try:
                    mt5_bal = await mt5_exc.get_account_info()
                    app_ctx._mt5_balance = mt5_bal
                except Exception:
                    mt5_bal = None
            if "mt5" not in brokers:
                brokers["mt5"] = {
                    "connected": bool(getattr(mt5_exc, "is_connected", False)),
                    "mode": str(mt5_bal.get("trade_mode", "demo")) if isinstance(mt5_bal, dict) else "demo",
                    "server": str(mt5_bal.get("server", "")) if isinstance(mt5_bal, dict) else "",
                    "last_heartbeat": datetime.now(timezone.utc).isoformat() if getattr(mt5_exc, "is_connected", False) else None,
                    "error": getattr(mt5_exc, "last_error", None),
                    "balance": {
                        "available": float(mt5_bal.get("available", 0.0)),
                        "total": float(mt5_bal.get("total", mt5_bal.get("equity", 0.0))),
                        "unrealized_pnl": float(mt5_bal.get("unrealized_pnl", 0.0)),
                        "currency": str(mt5_bal.get("currency", "USD")),
                    } if isinstance(mt5_bal, dict) else None,
                }
            elif isinstance(mt5_bal, dict):
                brokers["mt5"].setdefault("mode", str(mt5_bal.get("trade_mode", "demo")))
                brokers["mt5"].setdefault("server", str(mt5_bal.get("server", "")))
                if brokers["mt5"].get("balance") is None:
                    brokers["mt5"]["balance"] = {
                        "available": float(mt5_bal.get("available", 0.0)),
                        "total": float(mt5_bal.get("total", mt5_bal.get("equity", 0.0))),
                        "unrealized_pnl": float(mt5_bal.get("unrealized_pnl", 0.0)),
                        "currency": str(mt5_bal.get("currency", "USD")),
                    }
    except Exception as enrich_err:
        logger.debug("[ROUTES] /api/multi/status enrichment skipped: %s", enrich_err)

    # Surface real SMC V3 engine state & today's signal/order stats even when Start is not pressed
    try:
        import sqlite3
        from smc_futures.smc_config import SMC_CONFIG
        smc_auto_on = str(SMC_CONFIG.get("AUTO_EXECUTE_MODE", "false")).strip().lower() in ("1", "true", "yes")
        status["running"] = bool(smc_auto_on)
        status["strategy_engine"] = "SMC_V3"

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db_path = "data/trades.db"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        cur = conn.cursor()
        sig_accepted = cur.execute(
            "SELECT COUNT(*) FROM signal_decisions WHERE ts >= ? AND UPPER(decision) = 'ACCEPT'",
            (today_str,),
        ).fetchone()[0]
        cur_stats = status.setdefault("statistics", {})
        cur_stats["total_signals"] = max(int(cur_stats.get("total_signals", 0) or 0), int(sig_accepted or 0))
        conn.close()
    except Exception as stat_err:
        logger.debug("[ROUTES] /api/multi/status SMC stats enrichment skipped: %s", stat_err)

    status["timestamp"] = datetime.now(timezone.utc).isoformat()
    return status


@router.get("/health")
async def multi_broker_health():
    """
    Detailed health check for multi-broker system.
    
    Returns comprehensive status including:
      - Per-broker connection status
      - Last WebSocket message timestamps
      - Mirror mode status
      - Configured symbols
    """
    import os
    from datetime import datetime, timezone
    
    # Check mirror mode
    mirror_enabled = os.getenv("MULTI_BROKER_MIRROR", "0").lower() in ("1", "true", "yes")
    dual_enabled = os.getenv("DUAL_EXECUTION", "0").lower() in ("1", "true", "yes")
    kraken_enabled = os.getenv("KRAKEN_ENABLED", "0").lower() in ("1", "true", "yes")
    
    try:
        engine = get_engine()
        status = engine.get_status()
        
        brokers_status = status.get("brokers", {})
        bitget_info = brokers_status.get("bitget", {})
        kraken_info = brokers_status.get("kraken", {})
        
        bitget_connected = bitget_info.get("connected", False)
        kraken_connected = kraken_info.get("connected", False)
        
        # Get last heartbeat timestamps
        bitget_last_heartbeat = bitget_info.get("last_heartbeat")
        kraken_last_heartbeat = kraken_info.get("last_heartbeat")
        
        connected_count = sum([bitget_connected, kraken_connected])
        
        # Determine overall status
        if connected_count == 0:
            overall_status = "unhealthy"
        elif connected_count == 1 and (mirror_enabled or dual_enabled):
            overall_status = "degraded"  # Mirror mode expects both
        else:
            overall_status = "healthy"
        
        return {
            "status": overall_status,
            "running": status.get("running", False),
            "connected_brokers": connected_count,
            # Per-broker details
            "bitget_connected": bitget_connected,
            "kraken_connected": kraken_connected,
            "last_ws_msg_ts": {
                "bitget": bitget_last_heartbeat,
                "kraken": kraken_last_heartbeat,
            },
            # Configuration
            "mirror_mode": mirror_enabled or dual_enabled,
            "kraken_enabled": kraken_enabled,
            "symbols": status.get("config", {}).get("symbols", []),
            # Errors if any
            "errors": {
                "bitget": bitget_info.get("error"),
                "kraken": kraken_info.get("error"),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
    except HTTPException:
        return {
            "status": "not_initialized",
            "running": False,
            "connected_brokers": 0,
            "bitget_connected": False,
            "kraken_connected": False,
            "last_ws_msg_ts": {"bitget": None, "kraken": None},
            "mirror_mode": mirror_enabled or dual_enabled,
            "kraken_enabled": kraken_enabled,
            "symbols": [],
            "errors": {"bitget": "Engine not initialized", "kraken": "Engine not initialized"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# BALANCE ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/balances")
async def get_all_balances(refresh: bool = Query(default=False, description="Force refresh from exchanges")):
    """
    Get balances from all active brokers.
    
    Query params:
        refresh: If true, fetches fresh balances from exchanges
        
    Returns:
        {
            "balances": {
                "bitget": {"available": 1000.0, "total": 1200.0, "currency": "USDT"},
                "kraken": {"available": 500.0, "total": 600.0, "currency": "USD"}
            },
            "total_available_usd": 1500.0,
            "timestamp": ISO8601
        }
    """
    engine = get_engine()
    
    if refresh:
        await engine.refresh_all_balances()
    
    balances = engine.get_balances()
    
    total_available = sum(b.available for b in balances.values())
    
    return {
        "balances": {
            name: {
                "available": b.available,
                "total": b.total,
                "currency": b.currency,
                "margin_used": b.margin_used,
                "unrealized_pnl": b.unrealized_pnl,
            }
            for name, b in balances.items()
        },
        "total_available_usd": total_available,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/balances/{broker}")
async def get_broker_balance(broker: str, refresh: bool = False):
    """
    Get balance from a specific broker.
    
    Args:
        broker: 'bitget' or 'kraken'
        refresh: Force refresh from exchange
        
    Returns:
        Balance information for the specified broker.
    """
    engine = get_engine()
    broker_lower = broker.lower()
    
    if broker_lower not in ("bitget", "kraken"):
        raise HTTPException(status_code=400, detail=f"Invalid broker: {broker}")
    
    if refresh:
        if broker_lower == "bitget":
            await engine.fetch_balance_bitget()
        else:
            await engine.fetch_balance_kraken()
    
    from smc_futures.engine.multi_broker_engine import BrokerType
    
    broker_type = BrokerType.BITGET if broker_lower == "bitget" else BrokerType.KRAKEN
    balance = engine.get_balance(broker_type)
    
    if not balance:
        raise HTTPException(status_code=404, detail=f"{broker} balance not available")
    
    return {
        "broker": broker_lower,
        "available": balance.available,
        "total": balance.total,
        "currency": balance.currency,
        "margin_used": balance.margin_used,
        "unrealized_pnl": balance.unrealized_pnl,
        "timestamp": balance.timestamp.isoformat(),
    }


@router.get("/balance/history")
async def get_balance_history(
    broker: Optional[str] = Query(default=None, description="Filter by broker: bitget, kraken"),
    days: int = Query(default=7, ge=1, le=90, description="Days of history"),
    limit: int = Query(default=500, ge=1, le=2000, description="Max records"),
):
    """
    Get balance history from SQLite.
    
    Query params:
        broker: Filter by broker (optional)
        days: Number of days to look back (default: 7)
        limit: Maximum records (default: 500)
        
    Returns:
        {
            "history": [
                {"broker": "bitget", "available": 1000.0, "total": 1200.0, ...},
                ...
            ],
            "count": 123,
            "timestamp": ISO8601
        }
    """
    try:
        from backend.services.balance_history_service import get_balance_history_service
        
        service = get_balance_history_service()
        records = await service.get_history(broker=broker, days=days, limit=limit)
        
        return {
            "history": [r.to_dict() for r in records],
            "count": len(records),
            "broker_filter": broker,
            "days": days,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error("[ROUTES] Balance history error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/balance/latest")
async def get_latest_balances():
    """
    Get the most recent balance record for each broker.
    
    Returns:
        {
            "balances": {
                "bitget": {"available": 1000.0, ...},
                "kraken": {"available": 500.0, ...}
            },
            "timestamp": ISO8601
        }
    """
    try:
        from backend.services.balance_history_service import get_balance_history_service
        
        service = get_balance_history_service()
        latest = await service.get_all_latest()
        
        return {
            "balances": {
                broker: record.to_dict()
                for broker, record in latest.items()
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error("[ROUTES] Latest balance error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/balance/record")
async def force_record_balance():
    """
    Force an immediate balance snapshot save to history.
    
    Useful for testing or manual checkpoints.
    
    Returns:
        {"status": "recorded", "timestamp": ISO8601}
    """
    try:
        from backend.services.balance_history_service import get_balance_history_service
        
        engine = get_engine()
        service = get_balance_history_service()
        
        # Refresh and save
        await engine.refresh_all_balances()
        balances = engine.get_balances()
        
        recorded = 0
        for broker_key, balance in balances.items():
            # Handle both string keys and enum keys
            broker_name = broker_key.value if hasattr(broker_key, 'value') else str(broker_key)
            await service.record_balance(
                broker=broker_name,
                available=balance.available,
                total=balance.total,
                currency=balance.currency,
                margin_used=balance.margin_used,
                unrealized_pnl=balance.unrealized_pnl,
            )
            recorded += 1
        
        return {
            "status": "recorded",
            "brokers_recorded": recorded,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[ROUTES] Force record error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# MARKET DATA ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/candles/{symbol}")
async def get_candles_all_brokers(
    symbol: str,
    timeframe: str = Query(default="15m", description="Timeframe: 1m, 5m, 15m, 1h, 4h"),
    limit: int = Query(default=100, ge=1, le=500, description="Number of candles"),
):
    """
    Get candles from all active brokers for comparison.
    
    Args:
        symbol: Trading symbol (e.g., BTCUSDT)
        timeframe: Candle timeframe
        limit: Maximum candles to return
        
    Returns:
        {
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "data": {
                "bitget": [{"ts": ..., "open": ..., ...}, ...],
                "kraken": [{"ts": ..., "open": ..., ...}, ...]
            }
        }
    """
    engine = get_engine()
    
    candles_by_broker = await engine.fetch_candles_all(symbol, timeframe, limit)
    
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "data": {
            broker.value: [
                {
                    "ts": c.timestamp,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
                for c in candles
            ]
            for broker, candles in candles_by_broker.items()
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/candles/{broker}/{symbol}")
async def get_candles_single_broker(
    broker: str,
    symbol: str,
    timeframe: str = "15m",
    limit: int = 100,
):
    """
    Get candles from a specific broker.
    
    Args:
        broker: 'bitget' or 'kraken'
        symbol: Trading symbol
        timeframe: Candle timeframe
        limit: Maximum candles
    """
    engine = get_engine()
    broker_lower = broker.lower()
    
    if broker_lower == "bitget":
        candles = await engine.fetch_candles_bitget(symbol, timeframe, limit)
    elif broker_lower == "kraken":
        candles = await engine.fetch_candles_kraken(symbol, timeframe, limit)
    else:
        raise HTTPException(status_code=400, detail=f"Invalid broker: {broker}")
    
    return {
        "broker": broker_lower,
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": [
            {
                "ts": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ],
        "count": len(candles),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ORDER ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/order")
async def execute_order(req: OrderRequest):
    """
    Execute order on specified broker or default broker.
    
    Args:
        req: Order request with symbol, side, quantity, etc.
        
    Returns:
        Order execution result.
    """
    engine = get_engine()
    
    broker = req.broker.lower() if req.broker else None
    
    if broker == "bitget":
        result = await engine.execute_order_bitget(
            symbol=req.symbol,
            side=req.side,
            quantity=req.quantity,
            order_type=req.order_type,
            price=req.price,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
        )
    elif broker == "kraken":
        from smc_futures.smc_config import is_kraken_execution_enabled
        if not is_kraken_execution_enabled():
            raise HTTPException(
                status_code=403,
                detail="Kraken execution disabled (KRAKEN_EXECUTION_ENABLED=false)",
            )
        result = await engine.execute_order_kraken(
            symbol=req.symbol,
            side=req.side,
            quantity=req.quantity,
            order_type=req.order_type,
            price=req.price,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
        )
    else:
        # Execute on default broker
        from smc_futures.engine.multi_broker_engine import BrokerType
        default = engine.config.default_broker if hasattr(engine.config, 'default_broker') else "bitget"
        
        if default == "kraken":
            from smc_futures.smc_config import is_kraken_execution_enabled
            if not is_kraken_execution_enabled():
                raise HTTPException(
                    status_code=403,
                    detail="Kraken execution disabled (KRAKEN_EXECUTION_ENABLED=false)",
                )
            result = await engine.execute_order_kraken(
                req.symbol, req.side, req.quantity, req.order_type,
                req.price, req.stop_loss, req.take_profit,
            )
        else:
            result = await engine.execute_order_bitget(
                req.symbol, req.side, req.quantity, req.order_type,
                req.price, req.stop_loss, req.take_profit,
            )
    
    return {
        "success": result.success,
        "broker": result.broker.value,
        "symbol": result.symbol,
        "order_id": result.order_id,
        "error": result.error,
        "timestamp": result.timestamp.isoformat(),
    }


@router.post("/order/all")
async def execute_order_all_brokers(req: MultiOrderRequest):
    """
    Execute order on ALL active brokers simultaneously.
    
    Args:
        req: Order request with quantity_per_broker
        
    Returns:
        Results from each broker.
    """
    engine = get_engine()

    # Phase 1.5 (P1-3): Symmetric HTTP 403 guard when Kraken is active on the engine while disabled
    kraken_targeted = bool(
        getattr(engine, "_kraken_executor", None) is not None
        or getattr(getattr(engine, "config", None), "kraken_enabled", False)
    )
    if kraken_targeted:
        from smc_futures.smc_config import is_kraken_execution_enabled
        if not is_kraken_execution_enabled():
            raise HTTPException(
                status_code=403,
                detail="Kraken execution disabled (KRAKEN_EXECUTION_ENABLED=false)",
            )
    
    results = await engine.execute_order_all_brokers(
        symbol=req.symbol,
        side=req.side,
        quantity_per_broker=req.quantity_per_broker,
        order_type=req.order_type,
        price=req.price,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
    )
    
    return {
        "symbol": req.symbol,
        "side": req.side,
        "results": {
            broker.value: {
                "success": result.success,
                "order_id": result.order_id,
                "error": result.error,
            }
            for broker, result in results.items()
        },
        "total_success": sum(1 for r in results.values() if r.success),
        "total_failed": sum(1 for r in results.values() if not r.success),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


class DryRunTestRequest(BaseModel):
    """Dry-run test request payload."""
    symbol: str = Field(default="BTCUSDT", description="Trading symbol to test")
    quantity: float = Field(default=0.001, gt=0, description="Test quantity")
    side: str = Field(default="buy", description="Order side: 'buy' or 'sell'")
    dry_run: bool = Field(default=True, description="If false, actually execute (requires ALLOW_TEST_EXECUTION=1)")
    brokers: Optional[List[str]] = Field(default=None, description="Brokers to test (default: all enabled)")
    
    model_config = ConfigDict(extra="forbid")


@router.post("/order/test")
async def test_multi_broker_execution(req: DryRunTestRequest):
    """
    Test multi-broker execution path.
    
    DRY-RUN (default): Validates configuration without placing orders.
    LIVE (dry_run=false): Actually executes on all brokers (requires ALLOW_TEST_EXECUTION=1).
    
    Validates:
    1. Both brokers are connected and enabled
    2. Symbol mapping works for both brokers
    3. MultiOrderExecutor is properly configured
    4. Account balances are sufficient
    
    Args:
        req: Test request with symbol, quantity, and dry_run flag
        
    Returns:
        Detailed report of each broker's readiness (and execution results if not dry_run)
    """
    import os
    import uuid
    from backend.services.multi_order_executor import get_multi_order_executor
    
    test_signal_id = f"test_{uuid.uuid4().hex[:8]}"
    logger.info("[MULTI_ORDER_TEST] %s | signal_id=%s | dry_run=%s", req.symbol, test_signal_id, req.dry_run)
    
    results = {
        "test_type": "live_execution" if not req.dry_run else "dry_run",
        "signal_id": test_signal_id,
        "symbol": req.symbol,
        "quantity": req.quantity,
        "side": req.side,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "KRAKEN_ENABLED": os.getenv("KRAKEN_ENABLED", "0"),
            "MULTI_BROKER_MIRROR": os.getenv("MULTI_BROKER_MIRROR", "0"),
            "MANUAL_EXECUTE_ALL_BROKERS": os.getenv("MANUAL_EXECUTE_ALL_BROKERS", "0"),
            "TRADING_MODE": os.getenv("TRADING_MODE", "paper"),
            "ALLOW_TEST_EXECUTION": os.getenv("ALLOW_TEST_EXECUTION", "0"),
        },
        "brokers": {},
        "balances": {},
        "executor_status": {},
        "symbol_mapping": {},
        "overall_ready": False,
        "issues": [],
    }
    
    # Get MultiOrderExecutor
    try:
        executor = get_multi_order_executor()
        results["executor_status"]["multi_order_executor"] = "OK"
        results["executor_status"]["enabled_brokers"] = executor.get_enabled_brokers()
    except Exception as e:
        results["executor_status"]["multi_order_executor"] = f"ERROR: {e}"
        results["issues"].append(f"MultiOrderExecutor not available: {e}")
        return results
    
    # Test Bitget
    bitget_ready = False
    try:
        bitget_enabled = executor.is_broker_enabled("bitget")
        bitget_symbol = executor.translate_symbol(req.symbol, "bitget")
        results["brokers"]["bitget"] = {
            "enabled": bitget_enabled,
            "executor_set": executor._bitget_executor is not None,
            "connected": getattr(executor._bitget_executor, '_connected', 'unknown') if executor._bitget_executor else False,
        }
        results["symbol_mapping"]["bitget"] = bitget_symbol or "NOT_SUPPORTED"
        
        # Get Bitget balance if available
        if bitget_enabled and executor._bitget_executor:
            try:
                balance_result = await executor._bitget_executor.get_balance()
                if balance_result:
                    results["balances"]["bitget"] = {
                        "available": balance_result.get("available", 0),
                        "total": balance_result.get("total", 0),
                    }
            except Exception as be:
                results["balances"]["bitget"] = {"error": str(be)}
        
        if bitget_enabled and bitget_symbol:
            bitget_ready = True
            results["brokers"]["bitget"]["ready"] = True
        else:
            results["brokers"]["bitget"]["ready"] = False
            if not bitget_enabled:
                results["issues"].append("Bitget executor not enabled")
            if not bitget_symbol:
                results["issues"].append(f"Symbol {req.symbol} not supported on Bitget")
    except Exception as e:
        results["brokers"]["bitget"] = {"error": str(e), "ready": False}
        results["issues"].append(f"Bitget check failed: {e}")
    
    # Test Kraken
    kraken_ready = False
    try:
        kraken_enabled = executor.is_broker_enabled("kraken")
        kraken_symbol = executor.translate_symbol(req.symbol, "kraken")
        results["brokers"]["kraken"] = {
            "enabled": kraken_enabled,
            "executor_set": executor._kraken_executor is not None,
            "connected": getattr(executor._kraken_executor, '_connected', 'unknown') if executor._kraken_executor else False,
        }
        results["symbol_mapping"]["kraken"] = kraken_symbol or "NOT_SUPPORTED"
        
        # Get Kraken balance if available
        if kraken_enabled and executor._kraken_executor:
            try:
                # Kraken executor may have get_balance or _rest_client.get_balance
                if hasattr(executor._kraken_executor, 'get_balance'):
                    balance_result = await executor._kraken_executor.get_balance()
                    if balance_result:
                        results["balances"]["kraken"] = {
                            "available": balance_result.get("available", 0),
                            "total": balance_result.get("total", 0),
                        }
                elif hasattr(executor._kraken_executor, '_rest_client') and executor._kraken_executor._rest_client:
                    balance_obj = await executor._kraken_executor._rest_client.get_balance()
                    if balance_obj:
                        results["balances"]["kraken"] = {
                            "available": getattr(balance_obj, 'available', 0),
                            "total": getattr(balance_obj, 'total', 0),
                        }
            except Exception as ke:
                results["balances"]["kraken"] = {"error": str(ke)}
        
        if kraken_enabled and kraken_symbol:
            kraken_ready = True
            results["brokers"]["kraken"]["ready"] = True
        else:
            results["brokers"]["kraken"]["ready"] = False
            if not kraken_enabled:
                results["issues"].append("Kraken executor not enabled/connected")
            if not kraken_symbol:
                results["issues"].append(f"Symbol {req.symbol} not supported on Kraken")
    except Exception as e:
        results["brokers"]["kraken"] = {"error": str(e), "ready": False}
        results["issues"].append(f"Kraken check failed: {e}")
    
    # Overall readiness
    results["overall_ready"] = bitget_ready and kraken_ready
    results["ready_count"] = sum([bitget_ready, kraken_ready])
    
    if results["overall_ready"]:
        results["message"] = "✅ READY: Both brokers configured and ready for mirror trading"
        logger.info("[MULTI_ORDER_TEST] SUCCESS: Both brokers ready for %s", req.symbol)
    elif results["ready_count"] == 1:
        results["message"] = "⚠️ PARTIAL: Only one broker ready - mirror trading will be incomplete"
        logger.warning("[MULTI_ORDER_TEST] PARTIAL: Only %d broker(s) ready", results["ready_count"])
    else:
        results["message"] = "❌ NOT READY: No brokers available for trading"
        logger.error("[MULTI_ORDER_TEST] FAILED: No brokers ready")
    
    # Add execution simulation/results
    results["simulation"] = {
        "would_execute_on": [b for b in ["bitget", "kraken"] if results["brokers"].get(b, {}).get("ready")],
        "execution_mode": "parallel (asyncio.gather)",
        "signal_id_format": "mirror_<uuid12>",
        "db_persistence": (
            "disabled_in_dry_run (zero SQLite writes)"
            if req.dry_run
            else "2 rows with same signal_id (one per broker)"
        ),
    }

    # Phase 1.6 Step 4 (P2-2): Strict SQLite isolation for dry_run=True.
    # NEVER call on_trade_opened(), on_position_opened(), or any DB write hook during dry_run.
    if req.dry_run:
        target_brokers = req.brokers or results["simulation"]["would_execute_on"]
        try:
            simulated_results = {}
            side_norm = "long" if req.side.lower() == "buy" else "short"
            entry_price = float(100.12)  # In-memory dummy reference price only
            for broker in target_brokers:
                order_id_str = f"dry_run_{uuid.uuid4().hex[:8]}"
                simulated_results[broker] = {
                    "success": True,
                    "order_id": order_id_str,
                    "symbol": req.symbol,
                    "side": side_norm,
                    "quantity": float(req.quantity),
                    "fill_price": entry_price,
                    "simulated_only": True,
                    "persisted_to_db": False,
                }

            results["dry_run_execution"] = {
                "status": "simulated",
                "signal_id": test_signal_id,
                "results": simulated_results,
                "persisted_to_db": False,
                "dry_run_isolated": True,
            }
            logger.info("[MULTI_ORDER_TEST] DRY RUN IN-MEMORY SIMULATION COMPLETE (0 DB writes) | signal_id=%s", test_signal_id)
        except Exception as e:
            logger.error("[MULTI_ORDER_TEST] DRY RUN SIMULATION FAILED | %s", e, exc_info=True)
            results["dry_run_execution"] = {
                "error": str(e),
                "persisted_to_db": False,
                "dry_run_isolated": True,
            }
        
    # If not dry_run and ALLOW_TEST_EXECUTION=1, actually execute
    elif not req.dry_run:
        allow_test = os.getenv("ALLOW_TEST_EXECUTION", "0").lower() in ("1", "true", "yes")
        if not allow_test:
            results["execution"] = {
                "status": "blocked",
                "reason": "ALLOW_TEST_EXECUTION is not set. Set ALLOW_TEST_EXECUTION=1 to enable live test execution.",
            }
            logger.warning("[MULTI_ORDER_TEST] Live execution blocked - ALLOW_TEST_EXECUTION not set")
        elif results["ready_count"] == 0:
            results["execution"] = {
                "status": "blocked",
                "reason": "No brokers ready for execution",
            }
        else:
            # Actually execute!
            logger.info("[MULTI_ORDER_TEST] LIVE EXECUTION | signal_id=%s | brokers=%s", 
                       test_signal_id, results["simulation"]["would_execute_on"])
            
            target_brokers = req.brokers or results["simulation"]["would_execute_on"]
            
            try:
                execution_results = await executor.execute_on_brokers(
                    symbol=req.symbol,
                    direction="long" if req.side.lower() == "buy" else "short",
                    entry_price=0,  # Market order
                    stop_loss=0,
                    take_profit=0,
                    quantity=str(req.quantity),
                    leverage=10,
                    order_type="market",
                    brokers=target_brokers,
                )
                
                results["execution"] = {
                    "status": "executed",
                    "signal_id": test_signal_id,
                    "results": {
                        broker: {
                            "success": r.success,
                            "order_id": r.order_id,
                            "fill_price": r.fill_price,
                            "error": r.error,
                            "skipped": r.skipped,
                        }
                        for broker, r in execution_results.items()
                    },
                    "success_count": sum(1 for r in execution_results.values() if r.success),
                    "fail_count": sum(1 for r in execution_results.values() if not r.success and not r.skipped),
                }
                
                logger.info("[MULTI_ORDER_TEST] LIVE EXECUTION COMPLETE | success=%d fail=%d",
                           results["execution"]["success_count"], results["execution"]["fail_count"])
                
            except Exception as exec_error:
                results["execution"] = {
                    "status": "error",
                    "error": str(exec_error),
                }
                logger.error("[MULTI_ORDER_TEST] LIVE EXECUTION ERROR: %s", exec_error)
    
    return results


# ══════════════════════════════════════════════════════════════════════════════
# TRADING LOOP CONTROL
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/start")
async def start_trading_loop():
    """
    Enable live automatic execution for the unified SMC V3 engine (Kraken + MT5).
    Never starts the legacy SMA20/50 placeholder loop.
    """
    import smc_futures.smc_config as smc_cfg
    import smc_futures.orchestrator as orch_mod

    smc_cfg.SMC_CONFIG["AUTO_EXECUTE_MODE"] = "true"
    smc_cfg.SMC_CONFIG["AUTO_EXECUTE_ENABLED"] = True
    smc_cfg.AUTO_EXECUTE_ENABLED = True

    try:
        engine = get_engine()
        from backend.services.balance_history_service import get_balance_history_service
        balance_service = get_balance_history_service()
        balance_service.set_engine(engine)
        await balance_service.start_scheduler(interval_seconds=300)
    except Exception as e:
        logger.warning("[ROUTES] Balance scheduler start failed: %s", e)

    logger.info("[ROUTES] SMC V3 live auto-execution ENABLED via /api/multi/start (Kraken + MT5)")
    return {
        "status": "started",
        "running": True,
        "engine": "SMC_V3",
        "message": "SMC V3 auto-execution enabled (Kraken + MT5)",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/stop")
async def stop_trading_loop():
    """
    Pause automatic order execution for SMC V3 while keeping signal generation
    and dashboard display active.
    """
    global _trading_task
    import smc_futures.smc_config as smc_cfg
    import smc_futures.orchestrator as orch_mod

    smc_cfg.SMC_CONFIG["AUTO_EXECUTE_MODE"] = "false"
    smc_cfg.SMC_CONFIG["AUTO_EXECUTE_ENABLED"] = False
    smc_cfg.AUTO_EXECUTE_ENABLED = False

    try:
        engine = get_engine()
        engine._running = False
        if _trading_task:
            _trading_task.cancel()
            try:
                await _trading_task
            except asyncio.CancelledError:
                pass
            _trading_task = None
        stats = engine.get_status().get("statistics", {})
    except Exception:
        stats = {}

    logger.info("[ROUTES] SMC V3 live auto-execution PAUSED via /api/multi/stop (signals remain active)")
    return {
        "status": "stopped",
        "running": False,
        "engine": "SMC_V3",
        "message": "SMC V3 auto-execution paused (signals remain displayed)",
        "statistics": stats,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# POSITIONS ENDPOINT
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/positions")
async def get_all_positions():
    """
    Get open positions from all brokers.
    
    Returns:
        Positions grouped by broker.
    """
    engine = get_engine()
    
    positions = {}
    
    # Fetch from Bitget
    if engine._bitget_executor:
        try:
            bitget_pos = await engine._bitget_executor.get_positions()
            positions["bitget"] = bitget_pos.get("data", []) if isinstance(bitget_pos, dict) else []
        except Exception as e:
            positions["bitget"] = {"error": str(e)}
    
    # Fetch from Kraken
    if engine._kraken_executor:
        try:
            kraken_pos = await engine._kraken_executor.get_positions()
            positions["kraken"] = kraken_pos.get("data", []) if isinstance(kraken_pos, dict) else []
        except Exception as e:
            positions["kraken"] = {"error": str(e)}
    
    return {
        "positions": positions,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/positions/{broker}")
async def get_broker_positions(broker: str):
    """Get positions from a specific broker."""
    engine = get_engine()
    broker_lower = broker.lower()
    
    if broker_lower == "bitget" and engine._bitget_executor:
        result = await engine._bitget_executor.get_positions()
    elif broker_lower == "kraken" and engine._kraken_executor:
        result = await engine._kraken_executor.get_positions()
    else:
        raise HTTPException(status_code=400, detail=f"Broker {broker} not available")
    
    return {
        "broker": broker_lower,
        "positions": result.get("data", []) if isinstance(result, dict) else [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
