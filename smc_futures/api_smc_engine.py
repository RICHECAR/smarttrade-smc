"""
SMC Engine API — FastAPI Backend (port :8000)
═══════════════════════════════════════════════
PRIMARY ENTRY POINT — Use run.py to start.

Serves:
  - WebSocket broadcast (signal generation)
  - Health checks
  - Status & safeguard management
  - Kill-switch control

Start:
    python run.py                   # Production
    python run.py --reload          # Development

DO NOT USE:
    python -m uvicorn smc_futures.api_smc_engine:app ...
    python -m uvicorn smc_futures.orchestrator:app ...

Author: Trading Bot Framework
"""

import logging
import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional, Any, Tuple
import smc_futures.orchestrator as orch_mod
from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor
from smc_futures.settings import settings

from core.env_loader import is_test_mode, safe_load_dotenv
from smc_futures.schemas.orders import DryRunOrderRequest, OrderRequest
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from smc_futures.smc_config import (
    SMC_CONFIG,
    AUTO_EXECUTE_ENABLED,
    SHADOW_MODE,
    is_kraken_execution_enabled,
)

# Import routes and WebSocket manager
from backend.routes.trades import router as trades_router
from backend.routes.stats import router as stats_router
from backend.routes.live_trades import (
    router as live_trades_router,
    set_position_manager as set_route_position_manager,
)
from backend.routes.manual import (
    router as manual_router,
    set_manual_executor,
    set_manual_kraken_executor,
    set_manual_order_manager,
    set_manual_app_context,
)
from backend.routes.market import router as market_router, set_market_context
from backend.routes.multi_broker import router as multi_broker_router, set_multi_broker_engine
from backend.routes.positions import router as positions_router
from backend.routes.kraken_routes import router as kraken_router
from backend.routes.governance import router as governance_router
from backend.routes.analytics import router as analytics_router
from smc_futures.engine.execution.position_manager import PositionManager
from backend.ws.manager import ws_manager, heartbeat_loop
from backend.db.session import init_db as init_trades_db
from backend.services.live_trades_service import init_live_trades_db
from backend.services.trade_persistence_hook import (
    set_ws_manager,
    recover_open_trades,
    sync_with_exchange,
)
from backend.services.close_position_service import (
    set_close_service_executor,
    set_close_service_ws_manager,
)
from smc_futures.app_context import build_app_context

# ──────────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("smc_engine")

# ──────────────────────────────────────────────────
#  Context (lazy singleton shared with orchestrator)
# ──────────────────────────────────────────────────

safe_load_dotenv()

_app_ctx = None


def get_app_context():
    """Return the shared AppContext, constructing it lazily on first access."""
    global _app_ctx
    if _app_ctx is None:
        existing_orch_ctx = getattr(orch_mod, "_app_ctx", None)
        _app_ctx = existing_orch_ctx if existing_orch_ctx is not None else build_app_context()
        if hasattr(_app_ctx, "set_ws_manager"):
            _app_ctx.set_ws_manager(ws_manager)
        if hasattr(orch_mod, "set_app_context"):
            orch_mod.set_app_context(_app_ctx)
        else:
            orch_mod.ctx = _app_ctx
        logger.info("[BOOT] SMC Engine API context initialized lazily | config=%s", type(_app_ctx.config).__name__)
    return _app_ctx


def set_app_context(new_ctx) -> None:
    """Explicitly inject or clear the shared AppContext (for tests/DI)."""
    global _app_ctx
    _app_ctx = new_ctx
    if hasattr(orch_mod, "set_app_context"):
        orch_mod.set_app_context(new_ctx)


class _LazyAppContextProxy:
    """Transparent lazy proxy for module-level `ctx` avoiding import-time build_app_context()."""

    def __getattr__(self, name: str):
        return getattr(get_app_context(), name)

    def __setattr__(self, name: str, value):
        setattr(get_app_context(), name, value)

    def __delattr__(self, name: str):
        delattr(get_app_context(), name)

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        if _app_ctx is None:
            return "<LazyAppContextProxy (uninitialized)>"
        return repr(_app_ctx)


ctx = _LazyAppContextProxy()

# ──────────────────────────────────────────────────
#  Canonical API routers & FastAPI app factory
# ──────────────────────────────────────────────────

_API_ROUTERS = (
    trades_router,
    stats_router,
    live_trades_router,
    manual_router,
    market_router,       # /api/v1/market/ticker, /api/smc/market/candles, /api/v1/positions
    multi_broker_router, # /api/multi/* endpoints
    positions_router,    # /api/positions/* persisted positions
    kraken_router,       # /api/kraken/* balance, positions, status
    governance_router,   # /api/governance/* regime, health, chaos, SLO
    analytics_router,    # /api/analytics/* data-driven analytics pipeline
)

_DEFAULT_FASTAPI_DOC_PATHS = frozenset(
    {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)


def _get_allowed_origins() -> list[str]:
    return os.getenv(
        "ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173,http://localhost:8000"
    ).split(",")


def _register_ws_routes(target_app: FastAPI) -> None:
    """Register the canonical WebSocket endpoints on a FastAPI app."""
    @target_app.websocket("/ws")
    async def _factory_ws_endpoint(websocket: WebSocket):
        await _handle_websocket(websocket, "/ws")

    @target_app.websocket("/ws/trades")
    async def _factory_ws_trades_endpoint(websocket: WebSocket):
        await _handle_websocket(websocket, "/ws/trades")

    @target_app.websocket("/ws/{path:path}")
    async def _factory_ws_catch_all(websocket: WebSocket, path: str):
        await websocket_catch_all(websocket, path)


def create_api_app(
    enable_startup_hooks: Optional[bool] = None,
    include_ws_only: bool = False,
    include_rest_routers: bool = True,
    include_ws_routes: bool = True,
) -> FastAPI:
    """
    FastAPI application factory for SMC Trading Engine.

    Builds a complete FastAPI instance with CORS, ApiKeyMiddleware, all canonical
    REST routers, inline HTTP endpoints, and WebSocket routes.
    When `enable_startup_hooks=False` (default in test mode), production startup
    and shutdown hooks are omitted so tests run without initializing production
    DBs, exchanges, or background tasks.
    Pass `include_ws_only=True` for minimal WebSocket-only test apps.
    """
    from backend.auth import ApiKeyMiddleware

    use_hooks = (not is_test_mode()) if enable_startup_hooks is None else enable_startup_hooks

    new_app = FastAPI(
        title="SMC Trading Engine",
        version="3.1",
        description="SMC Multi-Timeframe Strategy Engine with Auto-Execution",
    )
    new_app.add_middleware(
        CORSMiddleware,
        allow_origins=_get_allowed_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )
    new_app.add_middleware(ApiKeyMiddleware)

    if include_ws_only:
        _register_ws_routes(new_app)
    else:
        if include_rest_routers:
            for router in _API_ROUTERS:
                new_app.include_router(router)
        for route in app.router.routes[_ROUTER_ROUTE_COUNT:]:
            is_http = hasattr(route, "methods")
            if (is_http and include_rest_routers) or (not is_http and include_ws_routes):
                new_app.router.routes.append(route)

    new_app.exception_handlers.update(app.exception_handlers)
    new_app.dependency_overrides.update(app.dependency_overrides)
    new_app.router.redirect_slashes = app.router.redirect_slashes

    if use_hooks:
        new_app.add_event_handler("startup", startup_event)
        new_app.add_event_handler("shutdown", shutdown_event)

    return new_app


app = FastAPI(
    title="SMC Trading Engine",
    version="3.1",
    description="SMC Multi-Timeframe Strategy Engine with Auto-Execution"
)

# ──────────────────────────────────────────────────
#  CORS Middleware (Accept all origins including localhost)
# ──────────────────────────────────────────────────
_ALLOWED_ORIGINS = _get_allowed_origins()

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

# ──────────────────────────────────────────────────
#  API Key Auth Middleware (protects POST/DELETE/PUT)
# ──────────────────────────────────────────────────
from backend.auth import ApiKeyMiddleware
app.add_middleware(ApiKeyMiddleware)
# ──────────────────────────────────────────────────
WS_ALLOWED_ORIGINS = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8000",
    "*",  # Allow all (dev mode) - remove in production
]

# Optional WebSocket token for authenticated connections
# Set WS_AUTH_TOKEN in .env to require token auth
WS_AUTH_TOKEN = os.getenv("WS_AUTH_TOKEN", "")  # Empty = no auth required

# Dev mode: skip all auth checks for WebSocket (useful for local development)
# Explicitly use settings.dev_mode.
WS_DEV_MODE = settings.dev_mode
if WS_DEV_MODE:
    logger.info("[WS] DEV MODE enabled - all WebSocket connections allowed")
else:
    logger.info("[WS] PRODUCTION MODE - origin and token checks active")

# Background Reconciler — V2 only (managed by phase6_wiring)

def _check_ws_origin(websocket: WebSocket) -> bool:
    """Check if WebSocket origin is allowed. Returns True if allowed."""
    if WS_DEV_MODE:
        return True  # Skip origin check in dev mode
    origin = websocket.headers.get("origin", "")
    # Allow if no origin (direct connection) or in whitelist
    if not origin or "*" in WS_ALLOWED_ORIGINS:
        return True
    return any(origin.startswith(allowed) for allowed in WS_ALLOWED_ORIGINS if allowed != "*")

def _check_ws_auth(websocket: WebSocket) -> Tuple[bool, str]:
    """
    Check WebSocket authentication via query string token.
    
    If WS_DEV_MODE is set, allows all connections.
    If WS_AUTH_TOKEN is set, requires ?token=<token> in URL.
    If WS_AUTH_TOKEN is empty, allows all connections.
    
    Returns:
        Tuple of (is_authorized, reason)
    """
    if WS_DEV_MODE:
        return True, "dev_mode"
    
    if not WS_AUTH_TOKEN:
        return True, "no_auth_required"
    
    # Check query params for token
    query_params = dict(websocket.query_params)
    client_token = query_params.get("token", "")
    
    if not client_token:
        return False, "missing_token"
    
    if client_token != WS_AUTH_TOKEN:
        return False, "invalid_token"
    
    return True, "token_valid"

# ──────────────────────────────────────────────────
#  Include API Routers (canonical set shared with create_api_app)
# ──────────────────────────────────────────────────
for _api_router in _API_ROUTERS:
    app.include_router(_api_router)
_ROUTER_ROUTE_COUNT = len(app.router.routes)

# ──────────────────────────────────────────────────
#  GET /health
# ──────────────────────────────────────────────────

@app.get("/health")
@app.get("/api/health")  # Alias for frontend compatibility
async def health():
    """
    Health check endpoint.
    
    Returns:
        {
            "status": "ok",
            "service": "smc-trader",
            "version": "2.0",
            "trading_mode": "live" | "paper",
            "auto_execute_mode": "true" | "shadow" | "false",
            "timestamp": ISO8601
        }
    """
    return {
        "status": "ok",
        "service": "smc-trader",
        "version": "2.0",
        "trading_mode": SMC_CONFIG.get("TRADING_MODE", "paper"),
        "auto_execute_mode": SMC_CONFIG.get("AUTO_EXECUTE_MODE", "false"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────────
#  GET /health/execution
# ──────────────────────────────────────────────────

@app.get("/health/execution")
@app.get("/api/health/execution")  # Alias for frontend compatibility
async def health_execution():
    """Real-time Institutional Execution State."""
    from smc_futures.observability.metrics import REJECTS_TOTAL, ORPHAN_DETECTED_TOTAL, LATE_FILL_TOTAL
    
    order_manager = ctx.order_manager
    last_intent_state = "UNKNOWN"
    if hasattr(order_manager, 'active_intents') and order_manager.active_intents:
        last_intent_state = list(order_manager.active_intents.values())[-1]
        
    # Extract counter values from Prometheus metrics
    stale_rejections = 0
    try:
        stale_rejections = int(REJECTS_TOTAL.labels(reason='STALE_DATA')._value.get())
    except:
        pass

    orphan_count = 0
    try:
        orphan_count = int(ORPHAN_DETECTED_TOTAL._value.get())
    except:
        pass

    late_fill_count = 0
    try:
        late_fill_count = int(LATE_FILL_TOTAL._value.get())
    except:
        pass

    # Latency stats from tracker
    latency_summary = {}
    try:
        latency_summary = ctx.latency_tracker.get_stats()
    except:
        pass

    # Order manager stats
    om_stats = {}
    try:
        om_stats = order_manager.get_stats()
    except:
        pass

    # Risk V2 status (sole authority)
    _risk_v2_status = {}
    _kill_switch = getattr(order_manager, 'kill_switch_active', False)
    try:
        from core.phase6_wiring import get_risk_v2
        _rv2 = get_risk_v2()
        if _rv2:
            _risk_v2_status = _rv2.get_status()
    except Exception:
        pass

    return {
        "execution_mode": SMC_CONFIG.get("EXECUTION_MODE", "ASYMMETRIC_MAKER_TAKER"),
        # Frontend expects primary_broker / secondary_broker
        "primary_broker": getattr(order_manager, 'primary_name', 'kraken'),
        "secondary_broker": getattr(order_manager, 'secondary_name', ''),
        # Keep legacy keys for backward compat
        "primary_exchange": getattr(order_manager, 'primary_name', 'kraken'),
        "secondary_exchange": getattr(order_manager, 'secondary_name', ''),
        "last_intent_state": last_intent_state,
        "last_failure_reason": "",
        "cooldown_active": False,
        "kill_switch_active": _kill_switch,
        "stale_rejection_count": stale_rejections,
        "orphan_count": orphan_count,
        "late_fill_count": late_fill_count,
        "last_orphan_detected": "",
        "capital_regime": "V2",
        "risk_multiplier": 1.0,
        "execution_quality_score": 1.0,
        "risk_v2": _risk_v2_status,
        "latency_summary": latency_summary,
        **om_stats,
    }

# ──────────────────────────────────────────────────
#  GET /health/scale_readiness
# ──────────────────────────────────────────────────

@app.get("/health/scale_readiness")
@app.get("/api/health/scale_readiness")
async def get_scale_readiness(symbol: str = "BTCUSDT"):
    """
    Phase 4.5 Scale Readiness Gate + Phase 5 Allocation.
    Returns GREEN/AMBER/RED status with detailed institutional metrics and allocation.
    """
    try:
        readiness = ctx.scale_gate.check_readiness(symbol)
        
        # Phase 5: Inject Allocation Multiplier
        multiplier = ctx.capital_allocator.get_multiplier(symbol)
        current_alloc = ctx.capital_allocator.get_allocation(symbol)
        
        readiness["allocation"] = {
            "multiplier": round(multiplier, 2),
            "allocated_capital_eur": round(current_alloc, 2),
            "base_capital": ctx.capital_allocator.base_capital,
            "max_capital": ctx.capital_allocator.max_capital
        }
        return readiness
    except Exception as e:
        logger.error(f"Error in scale_readiness: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/health/allocation_status")
async def get_allocation_status(symbol: str = "BTCUSDT"):
    """
    Phase 5 Adaptive Capital Allocation Details.
    """
    try:
        from core.phase6_wiring import get_risk_v2
        
        _rv2 = get_risk_v2()
        _rv2_status = _rv2.get_status() if _rv2 else {}
        
        # Capital allocator is still accessible via ctx
        current_alloc = ctx.capital_allocator.get_allocation(symbol) if hasattr(ctx, 'capital_allocator') else 0
        multiplier = ctx.capital_allocator.get_multiplier(symbol) if hasattr(ctx, 'capital_allocator') else 1.0
        state = {}
        if hasattr(ctx, 'capital_allocator') and hasattr(ctx.capital_allocator, 'store'):
            state = ctx.capital_allocator.store.get_state(symbol) or {}
        
        return {
            "symbol": symbol,
            "multiplier": round(multiplier, 2),
            "allocated_eur": round(current_alloc, 2),
            "regime": state.get("regime", "V2"),
            "last_action": state.get("last_action", "NONE"),
            "last_reason": state.get("reason", "N/A"),
            "last_update": state.get("timestamp", 0),
            "risk_v2": _rv2_status,
        }
    except Exception as e:
        logger.error(f"Error in allocation_status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# NOTE: /api/health/execution is defined above in health_execution()
# Do NOT add a duplicate handler here.

# ──────────────────────────────────────────────────
#  GET /api/status
# ──────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    """
    Detailed engine status with safeguard information.
    
    Returns:
        {
            "trading_mode": "live" | "paper",
            "auto_execute_mode": "true" | "shadow" | "false",
            "auto_execute_enabled": bool,
            "shadow_mode": bool,
            "protection_status": {
                "kill_switch": bool,
                "kill_switch_reason": str
            },
            "symbols": ["BTCUSDT", ...],
            "timeframes": {"htf": "4h", "mtf": "15m", "ltf": "5m"},
            "safeguards": {
                "max_trades_per_day": int,
                "max_open_per_symbol": int,
                "trade_cooldown_sec": int,
                "min_rr": float
            },
            "daily_stats": {...},
            "timestamp": ISO8601
        }
    """
    order_manager = ctx.order_manager
    daily_stats = order_manager.get_daily_stats() if order_manager else {}
    
    return {
        "trading_mode": SMC_CONFIG.get("TRADING_MODE", "paper"),
        "auto_execute_mode": SMC_CONFIG.get("AUTO_EXECUTE_MODE", "false"),
        "auto_execute_enabled": AUTO_EXECUTE_ENABLED,
        "shadow_mode": SHADOW_MODE,
        "protection_status": {
            "kill_switch": order_manager.kill_switch_active if order_manager else False,
            "kill_switch_reason": order_manager.kill_switch_reason if order_manager else ""
        },
        "symbols": SMC_CONFIG.get("SYMBOLS", []),
        "timeframes": {
            "htf": SMC_CONFIG.get("HTF_TF", "4h"),
            "mtf": SMC_CONFIG.get("SETUP_TF", "15m"),
            "ltf": SMC_CONFIG.get("ENTRY_TF", "5m")
        },
        "safeguards": {
            "max_trades_per_day": SMC_CONFIG.get("MAX_TRADES_PER_DAY", 5),
            "max_open_per_symbol": SMC_CONFIG.get("MAX_OPEN_TRADES_PER_SYMBOL", 1),
            "trade_cooldown_sec": SMC_CONFIG.get("TRADE_COOLDOWN_SEC", 300),
            "min_rr": SMC_CONFIG.get("MIN_RR", 1.5)
        },
        "daily_stats": daily_stats,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────────
#  POST /api/bot/kill-switch
# ──────────────────────────────────────────────────

class KillSwitchRequest(BaseModel):
    """Request body for kill-switch endpoint."""
    enabled: bool
    reason: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


@app.post("/api/bot/kill-switch")
async def set_kill_switch(req: KillSwitchRequest):
    """
    Enable/disable kill-switch to block order execution.
    
    Args:
        {
            "enabled": bool,        # True to block, False to allow
            "reason": str (optional)
        }
    
    Returns:
        {
            "status": "ok",
            "kill_switch_enabled": bool,
            "reason": str,
            "timestamp": ISO8601
        }
    """
    order_manager = ctx.order_manager
    if not order_manager:
        logger.error("[KILL_SWITCH] OrderManager not available")
        raise HTTPException(status_code=503, detail="OrderManager not initialized")
    
    reason = req.reason or ("Manual kill-switch" if req.enabled else "Kill-switch disabled")
    if req.enabled:
        order_manager.engage_kill_switch(reason)
    else:
        order_manager.disengage_kill_switch()
    
    logger.info("[KILL_SWITCH_API] enabled=%s reason=%s", req.enabled, reason)
    
    return {
        "status": "ok",
        "kill_switch_enabled": order_manager.kill_switch_active,
        "reason": order_manager.kill_switch_reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────────
#  GET /api/bot/stats
# ──────────────────────────────────────────────────

@app.get("/api/bot/stats")
async def get_bot_stats():
    """
    Return current bot statistics and status.
    
    Returns:
        {
            "daily_trades": int,
            "trades_by_symbol": {...},
            "pnl_usd": float,
            "rejection_count": int,
            "last_trade_time_ms": int,
            "timestamp": ISO8601
        }
    """
    order_manager = ctx.order_manager
    if not order_manager:
        raise HTTPException(status_code=503, detail="OrderManager not initialized")
    
    stats = order_manager.get_status()
    
    return {
        "daily_trades": stats.get("daily_stats", {}).get("trades_count", 0),
        "trades_by_symbol": stats.get("daily_stats", {}).get("trades_by_symbol", {}),
        "pnl_usd": stats.get("daily_stats", {}).get("pnl_usd", 0.0),
        "rejection_count": stats.get("rejections_count", 0),
        "last_trade_time_ms": stats.get("last_trade_time_ms", 0),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────────
#  POST /api/admin/cleanup-failed-trades
# ──────────────────────────────────────────────────

@app.post("/api/admin/cleanup-failed-trades")
async def cleanup_failed_trades(dry_run: bool = True):
    """
    Archive old FAILED/SKIPPED trades by setting status → 'ARCHIVED'.
    
    Args:
        dry_run: If True (default), only preview. If False, apply changes.
    
    Returns:
        Summary of archived trades.
    """
    from backend.db.session import SessionLocal
    from backend.models.trade import Trade

    db = SessionLocal()
    try:
        candidates = (
            db.query(Trade)
            .filter(Trade.status.in_(["FAILED", "failed", "SKIPPED", "skipped"]))
            .all()
        )
        
        if not candidates:
            return {"status": "clean", "message": "No FAILED/SKIPPED trades found", "count": 0}
        
        # Build summary
        by_symbol = {}
        for t in candidates:
            sym = t.symbol or "UNKNOWN"
            by_symbol[sym] = by_symbol.get(sym, 0) + 1
        
        if dry_run:
            return {
                "status": "dry_run",
                "count": len(candidates),
                "by_symbol": by_symbol,
                "message": "Set dry_run=false to apply cleanup",
            }
        
        # Apply
        for t in candidates:
            t.status = "ARCHIVED"
        db.commit()
        
        logger.info(f"[ADMIN] Archived {len(candidates)} FAILED/SKIPPED trades")
        return {
            "status": "applied",
            "count": len(candidates),
            "by_symbol": by_symbol,
            "message": f"Successfully archived {len(candidates)} trades",
        }
    except Exception as e:
        db.rollback()
        logger.error(f"[ADMIN] Cleanup failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ──────────────────────────────────────────────────
#  WebSocket for Trade Broadcasting
# ──────────────────────────────────────────────────

async def _handle_websocket(websocket: WebSocket, route_name: str = "/ws"):
    """
    Shared WebSocket handler for both /ws and /ws/trades.
    
    Includes origin check, optional token auth, and detailed logging for debugging.
    
    Authentication:
        - If WS_AUTH_TOKEN env var is set, requires ?token=<token> in URL
        - If not set, connections are allowed based on origin only
    
    Example URLs:
        - ws://localhost:8000/ws/trades (no auth)
        - ws://localhost:8000/ws/trades?token=mysecret (with auth)
    """
    origin = websocket.headers.get("origin", "no-origin")
    client_host = websocket.client.host if websocket.client else "unknown"
    
    # Check 1: Origin whitelist
    if not _check_ws_origin(websocket):
        logger.warning(
            f"[WS] REJECTED | route={route_name} | reason=origin_not_allowed | "
            f"origin={origin} | client={client_host}"
        )
        await websocket.close(code=4003, reason="Origin not allowed")
        return
    
    # Check 2: Token auth (if enabled)
    auth_ok, auth_reason = _check_ws_auth(websocket)
    if not auth_ok:
        logger.warning(
            f"[WS] REJECTED | route={route_name} | reason={auth_reason} | "
            f"origin={origin} | client={client_host}"
        )
        # Use 4001 for auth failure (4000-4999 are available for app use)
        await websocket.close(code=4001, reason=f"Authentication failed: {auth_reason}")
        return
    
    logger.info(f"[WS] ACCEPTING connection | route={route_name} | origin={origin} | client={client_host}")
    
    # CRITICAL: Always accept at the handler level to avoid race conditions in manager
    try:
        await websocket.accept()
    except Exception as e:
        logger.error(f"[WS] ACCEPT_FAILED | route={route_name} | error={e}")
        return
    
    try:
        await ws_manager.connect(websocket)
        logger.info(f"[WS] CONNECTED | route={route_name} | total_clients={ws_manager.connection_count}")
    except Exception as e:
        logger.error(f"[WS] CONNECT_FAILED | route={route_name} | error={e}")
        return
    
    try:
        while True:
            # Keep connection alive, receive pings
            data = await websocket.receive_text()
            # Echo back pong or handle commands
            if data == "ping":
                await websocket.send_text("pong")
            elif data == "status":
                await websocket.send_json({
                    "type": "status",
                    "connected": True,
                    "clients": ws_manager.connection_count,
                })
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        logger.info(f"[WS] DISCONNECTED (normal) | route={route_name} | remaining={ws_manager.connection_count}")
    except Exception as e:
        ws_manager.disconnect(websocket)
        logger.warning(f"[WS] DISCONNECTED (error) | route={route_name} | error={e} | remaining={ws_manager.connection_count}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint (alias for /ws/trades).
    
    Clients receive:
        {"type": "trade_opened", "trade": {...}, "timestamp": "..."}
        {"type": "trade_closed", "trade": {...}, "timestamp": "..."}
        {"type": "heartbeat", "timestamp": "...", "connected_clients": N}
    """
    await _handle_websocket(websocket, "/ws")


@app.websocket("/ws/trades")
async def websocket_trades_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time trade updates.
    
    Clients receive:
        {"type": "trade_opened", "trade": {...}, "timestamp": "..."}
        {"type": "trade_closed", "trade": {...}, "timestamp": "..."}
        {"type": "heartbeat", "timestamp": "...", "connected_clients": N}
    """
    await _handle_websocket(websocket, "/ws/trades")


@app.websocket("/ws/{path:path}")
async def websocket_catch_all(websocket: WebSocket, path: str):
    """
    Catch-all WebSocket endpoint for debugging wrong paths.
    
    Logs the requested path and closes with 4004 code.
    This helps identify URL construction issues like /ws/ws/trades.
    """
    full_path = f"/ws/{path}"
    origin = websocket.headers.get("origin", "no-origin")
    client_host = websocket.client.host if websocket.client else "unknown"
    
    logger.warning(
        f"[WS] UNKNOWN_PATH | path={full_path} | origin={origin} | client={client_host} | "
        f"hint=Client may be constructing URL incorrectly. "
        f"Expected: /ws or /ws/trades. Check VITE_WS_URL env var."
    )
    
    # Accept the connection briefly to send error, then close
    await websocket.accept()
    await websocket.send_json({
        "type": "error",
        "error": f"Unknown WebSocket path: {full_path}",
        "valid_paths": ["/ws", "/ws/trades"],
        "hint": "Check VITE_WS_URL - should be ws://localhost:8000 (no /ws suffix)",
    })
    await websocket.close(code=4004, reason=f"Unknown path: {full_path}")


@app.get("/ws/info")
async def websocket_info():
    """
    WebSocket endpoint information.
    
    Returns:
        WebSocket connection details
    """
    return {
        "ws_url": "ws://localhost:8000/ws/trades",
        "message": "Connect to WebSocket for real-time trade updates",
        "events": ["trade_opened", "trade_closed", "heartbeat"],
        "connected_clients": ws_manager.connection_count,
    }


# ──────────────────────────────────────────────────
#  POST /api/v2/order/dry-run (Risk Sizing)
# ──────────────────────────────────────────────────

# DryRunOrderRequest imported from smc_futures.schemas.orders


@app.post("/api/v2/order/dry-run")
async def dry_run_order(req: DryRunOrderRequest):
    """
    Pure-computation dry-run: calculate position size from risk parameters.
    ZERO Bitget API calls. Works in any mode (shadow / paper / live).
    
    Returns:
        {
            "ok": bool,
            "orderId": "DRYRUN-...",
            "status": "simulated",
            "payloadPreview": {...},
            "riskCalc": {...}
        }
    """
    import uuid as _uuid
    from smc_futures.engine.execution.contract_specs import get_contract_specs
    
    symbol = req.symbol.replace("/", "").replace("-", "").upper()
    logger.info(
        "[DRY_RUN] REQUEST symbol=%s side=%s entry=%.4f sl=%.4f tp=%.4f risk=%.2f%% lev=%dx",
        symbol, req.side, req.entry_price, req.stop_loss, req.take_profit,
        req.risk_pct, req.leverage,
    )

    # Validate inputs
    if req.entry_price <= 0 or req.stop_loss <= 0:
        return JSONResponse(status_code=400, content={
            "ok": False, "reason": "entry_price and stop_loss must be > 0",
        })

    sl_distance = abs(req.entry_price - req.stop_loss)
    if sl_distance == 0:
        return JSONResponse(status_code=400, content={
            "ok": False, "reason": "stop_loss cannot equal entry_price",
        })

    # Get account balance from executor or env fallback
    account_balance = 0.0
    executor = ctx.executors.get("futures")
    if executor:
        try:
            bal_result = await executor.get_account_balance(marginCoin="USDT")
            account_balance = float(bal_result.get("equity", 0) or bal_result.get("available", 0) or 0)
        except Exception as e:
            logger.warning(f"[DRY_RUN] Failed to fetch balance: {e}")
    
    if account_balance <= 0:
        account_balance = float(os.getenv("ACCOUNT_BALANCE", "10000"))

    # Risk sizing
    risk_amount = account_balance * (req.risk_pct / 100.0)
    raw_size = (risk_amount * req.leverage) / sl_distance

    # Get contract specs (returns ContractSpecs dataclass)
    specs = get_contract_specs(symbol)
    qty_step = specs.qty_step
    min_qty = specs.min_qty

    # Round to step
    from decimal import Decimal, ROUND_DOWN
    step_dec = Decimal(str(qty_step))
    raw_dec = Decimal(str(raw_size))
    rounded = (raw_dec / step_dec).to_integral_value(rounding=ROUND_DOWN) * step_dec
    final_size = float(rounded)
    size_str = str(rounded)

    if final_size < min_qty:
        logger.warning("[DRY_RUN] REJECT size=%.6f < min_qty=%.6f", final_size, min_qty)
        return {
            "ok": False,
            "reason": f"Computed size {final_size} below minimum {min_qty}",
            "payloadPreview": {
                "symbol": symbol,
                "side": req.side,
                "size": final_size,
                "stopLoss": req.stop_loss,
                "takeProfit": req.take_profit,
            },
        }

    # Build preview
    payload = {
        "symbol": symbol,
        "side": req.side,
        "posSide": req.pos_side,
        "size": final_size,
        "sizeStr": size_str,
        "stopLoss": req.stop_loss,
        "takeProfit": req.take_profit,
        "entry": req.entry_price,
        "leverage": req.leverage,
        "productType": "USDT-FUTURES",
        "marginMode": "isolated",
        "tradeSide": "open",
        "orderType": "market",
    }

    logger.info(
        "[DRY_RUN] OK symbol=%s side=%s size=%s sl=%.4f tp=%.4f balance=%.2f",
        symbol, req.side, size_str, req.stop_loss, req.take_profit, account_balance,
    )

    return {
        "ok": True,
        "orderId": f"DRYRUN-{_uuid.uuid4().hex[:12].upper()}",
        "status": "simulated",
        "payloadPreview": payload,
        "riskCalc": {
            "accountBalance": account_balance,
            "riskPct": req.risk_pct,
            "riskAmount": round(risk_amount, 2),
            "slDistance": round(sl_distance, 6),
            "rawSize": round(raw_size, 8),
            "qtyStep": qty_step,
            "minQty": min_qty,
        },
    }


# ──────────────────────────────────────────────────
#  POST /api/v1/order (Order Placement)
# ──────────────────────────────────────────────────

# OrderRequest imported from smc_futures.schemas.orders


@app.post("/api/v1/order")
async def place_order_endpoint(req: OrderRequest):
    """
    Place an order on Bitget.
    
    Requires AUTO_EXECUTE_ENABLED or SHADOW_MODE in config.
    """
    # Global Checks
    if not AUTO_EXECUTE_ENABLED and not SHADOW_MODE:
        return JSONResponse(status_code=403, content={
            "error": "auto_execute_disabled",
            "message": "AUTO_EXECUTE is disabled. Enable in config or use /api/manual/execute.",
        })

    # Shadow Mode -> No real orders
    if SHADOW_MODE:
        import uuid
        fake_id = f"SHADOW-{uuid.uuid4().hex[:12].upper()}"
        logger.info("[SHADOW_ORDER] WOULD_SEND symbol=%s side=%s qty=%s", 
                   req.symbol, req.side, req.quantity or req.size)
        return {
            "success": True,
            "orderId": fake_id,
            "status": "shadow",
            "message": "WOULD_SEND — no real order placed (shadow mode)",
            "fillPrice": req.price or 0.0,
        }

    executor = ctx.executors.get("futures")
    if not executor:
        return JSONResponse(status_code=503, content={
            "error": "no_executor",
            "message": "Bitget API keys not configured",
        })

    # Resolve aliases
    final_qty = req.quantity or req.size
    final_sl = req.stopLoss or req.stop_loss
    final_tp = req.takeProfit or req.take_profit
    final_order_type = req.orderType or req.order_type or "market"

    if not final_qty or final_qty <= 0:
        return JSONResponse(status_code=400, content={
            "error": "missing_quantity",
            "message": "quantity or size is required and must be > 0",
        })

    try:
        result = await executor.place_order(
            symbol=req.symbol,
            direction=req.side,
            entry_price=req.price or 0,
            stop_loss=final_sl or 0,
            take_profit=final_tp or 0,
            order_type=final_order_type,
            leverage=10,  # Default leverage
            quantity=str(final_qty),
        )
        return result
    except Exception as e:
        logger.error(f"[ORDER] Error: {e}")
        return JSONResponse(status_code=500, content={
            "error": "internal_error",
            "route": "/api/v1/order",
            "message": str(e),
        })


# ──────────────────────────────────────────────────
#  GET /api/v2/account/balance
# ──────────────────────────────────────────────────

@app.get("/api/v2/account/balance")
async def get_account_balance_endpoint():
    """
    Get account balance for USDT-FUTURES.
    
    Returns:
        {
            "available": float,
            "equity": float,
            "marginCoin": "USDT"
        }
    """
    executor = ctx.executors.get("futures")
    if not executor:
        return JSONResponse(status_code=503, content={
            "error": "no_executor",
            "message": "Bitget API keys not configured",
        })
    
    try:
        result = await executor.get_account_balance(marginCoin="USDT")
        return {
            "code": "00000",
            "data": [{
                "available": result.get("available", 0.0),
                "equity": result.get("equity", 0.0),
                "marginCoin": "USDT",
            }],
        }
    except Exception as e:
        logger.error(f"[BALANCE] Error: {e}")
        return JSONResponse(status_code=500, content={
            "error": "balance_fetch_failed",
            "message": str(e),
        })


# ──────────────────────────────────────────────────
#  GET /api/v2/position/leverage
# ──────────────────────────────────────────────────

@app.get("/api/v2/position/leverage")
async def get_position_leverage(symbol: str):
    """
    Get leverage for a symbol.
    
    Returns cached leverage or default (10x).
    """
    clean_symbol = symbol.replace("/", "").replace("-", "").upper()
    
    # Try to get from order manager or return default
    order_manager = ctx.order_manager
    if order_manager:
        leverage = order_manager.default_leverage
    else:
        leverage = 10
    
    return {
        "code": "00000",
        "data": {
            "symbol": clean_symbol,
            "leverage": leverage,
            "marginMode": "isolated",
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
#  MULTI-BROKER ENDPOINTS (Kraken + Status)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/brokers/status")
async def get_brokers_status():
    """
    Get status of all configured brokers.
    
    Returns:
        {
            "default_broker": "bitget",
            "brokers": {
                "bitget": {"enabled": true, "connected": true},
                "kraken": {"enabled": true, "connected": true}
            },
            "timestamp": ISO8601
        }
    """
    return {
        **ctx.get_broker_status(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/kraken/balance")
async def get_kraken_balance():
    """
    Get Kraken Futures account balance.
    
    Returns:
        {
            "code": "00000",
            "broker": "kraken",
            "data": {
                "available": float,
                "total": float,
                "currency": "USD",
                "margin_used": float,
                "unrealized_pnl": float
            }
        }
        
    Errors:
        503: Kraken not configured or not connected
    """
    kraken_exc = ctx.executors.get("kraken_futures")
    
    if not kraken_exc:
        return JSONResponse(status_code=503, content={
            "error": "kraken_not_available",
            "message": "Kraken executor not configured. Set KRAKEN_ENABLED=true and provide credentials.",
        })
    
    try:
        balance_info = await kraken_exc.get_account_info()
        return {
            "code": "00000",
            "broker": "kraken",
            "data": balance_info,
        }
    except Exception as e:
        logger.error("[KRAKEN_BALANCE] Error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={
            "error": "balance_fetch_failed",
            "message": str(e),
        })


@app.get("/api/kraken/positions")
async def get_kraken_positions():
    """
    Get open positions on Kraken Futures.
    
    Returns:
        {
            "code": "00000",
            "broker": "kraken",
            "data": [
                {
                    "symbol": "PF_XBTUSD",
                    "side": "long",
                    "size": 0.001,
                    "entry_price": 50000.0,
                    "unrealized_pnl": 10.5
                }
            ]
        }
    """
    kraken_exc = ctx.executors.get("kraken_futures")
    
    if not kraken_exc:
        return JSONResponse(status_code=503, content={
            "error": "kraken_not_available",
            "message": "Kraken executor not configured.",
        })
    
    try:
        positions = await kraken_exc.get_positions()
        return {
            "code": "00000",
            "broker": "kraken",
            "data": positions.get("data", []) if isinstance(positions, dict) else positions,
        }
    except Exception as e:
        logger.error("[KRAKEN_POSITIONS] Error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={
            "error": "positions_fetch_failed",
            "message": str(e),
        })


@app.get("/api/mt5/status")
async def get_mt5_status():
    """Get MetaTrader 5 terminal & account connection status."""
    from backend.execution.mt5_executor import is_mt5_enabled, is_mt5_execution_enabled
    mt5_exc = ctx.executors.get("mt5")
    if not mt5_exc:
        return {
            "broker": "mt5",
            "enabled": is_mt5_enabled(),
            "execution_enabled": is_mt5_execution_enabled(),
            "connected": False,
            "message": "MT5 executor not initialized",
        }
    try:
        info = await mt5_exc.get_account_info()
        return {
            "broker": "mt5",
            "enabled": is_mt5_enabled(),
            "execution_enabled": is_mt5_execution_enabled(),
            "connected": bool(mt5_exc.is_connected),
            "account": info,
        }
    except Exception as e:
        return JSONResponse(status_code=503, content={
            "broker": "mt5",
            "enabled": is_mt5_enabled(),
            "connected": False,
            "error": str(e),
        })


@app.get("/api/mt5/balance")
async def get_mt5_balance():
    """Get MetaTrader 5 account balance."""
    mt5_exc = ctx.executors.get("mt5")
    if not mt5_exc:
        return JSONResponse(status_code=503, content={
            "error": "mt5_not_available",
            "message": "MT5 executor not configured. Set MT5_ENABLED=true and MT5 credentials in .env.",
        })
    try:
        balance_info = await mt5_exc.get_account_info()
        return {"code": "00000", "broker": "mt5", "data": balance_info}
    except Exception as e:
        logger.error("[MT5_BALANCE] Error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": "balance_fetch_failed", "message": str(e)})


@app.get("/api/mt5/positions")
async def get_mt5_positions(symbol: Optional[str] = None):
    """Get open MetaTrader 5 positions."""
    mt5_exc = ctx.executors.get("mt5")
    if not mt5_exc:
        return JSONResponse(status_code=503, content={
            "error": "mt5_not_available",
            "message": "MT5 executor not configured.",
        })
    try:
        positions = await mt5_exc.get_positions(symbol=symbol)
        return {"code": "00000", "broker": "mt5", "count": len(positions), "positions": positions}
    except Exception as e:
        logger.error("[MT5_POSITIONS] Error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": "positions_fetch_failed", "message": str(e)})


class MultiBrokerBalanceRequest(BaseModel):
    """Request for multi-broker balance."""
    broker: Optional[str] = None  # "bitget", "kraken", or None for default
    model_config = ConfigDict(extra="forbid")


@app.get("/api/v2/balance")
async def get_balance_unified(broker: Optional[str] = None):
    """
    Unified balance endpoint supporting both brokers.
    
    Query params:
        broker: "bitget" or "kraken" (optional, defaults to DEFAULT_BROKER)
    
    Returns:
        {
            "code": "00000",
            "broker": "bitget" | "kraken",
            "data": {
                "available": float,
                "equity": float (bitget) | "total": float (kraken),
                "currency": "USDT" | "USD"
            }
        }
    """
    target_broker = broker or ctx.default_broker
    
    if target_broker == "kraken":
        kraken_exc = ctx.executors.get("kraken_futures")
        if not kraken_exc:
            return JSONResponse(status_code=503, content={
                "error": "kraken_not_available",
                "message": "Kraken not configured",
            })
        
        try:
            balance = await kraken_exc.get_account_info()
            return {
                "code": "00000",
                "broker": "kraken",
                "data": {
                    "available": balance.get("available", 0.0),
                    "total": balance.get("total", 0.0),
                    "currency": "USD",
                },
            }
        except Exception as e:
            logger.error("[BALANCE_UNIFIED] Kraken error: %s", e)
            return JSONResponse(status_code=500, content={"error": str(e)})
    
    else:  # Bitget (default)
        bitget_exc = ctx.executors.get("futures")
        if not bitget_exc:
            return JSONResponse(status_code=503, content={
                "error": "bitget_not_available",
                "message": "Bitget not configured",
            })
        
        try:
            result = await bitget_exc.get_account_balance(marginCoin="USDT")
            return {
                "code": "00000",
                "broker": "bitget",
                "data": {
                    "available": result.get("available", 0.0),
                    "equity": result.get("equity", 0.0),
                    "currency": "USDT",
                },
            }
        except Exception as e:
            logger.error("[BALANCE_UNIFIED] Bitget error: %s", e)
            return JSONResponse(status_code=500, content={"error": str(e)})


# ──────────────────────────────────────────────────
#  Background Tasks
# ──────────────────────────────────────────────────

async def auto_archive_failed_loop(interval_sec: int = 60):
    """
    Background loop to auto-archive FAILED/SKIPPED trades older than 5 minutes.
    """
    from backend.db.session import SessionLocal
    from backend.models.trade import Trade
    from backend.models.live_trade import LiveTrade
    from backend.models.persisted_position import PersistedPosition
    from datetime import timedelta
    
    while True:
        try:
            await asyncio.sleep(interval_sec)
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(minutes=5)
            
            db = SessionLocal()
            try:
                # Update live_trades
                candidates_live = db.query(LiveTrade).filter(
                    LiveTrade.status.in_(["FAILED", "SKIPPED"]),
                    LiveTrade.archived == False,
                    LiveTrade.ts_open < cutoff
                ).all()
                
                archived_count = 0
                for t in candidates_live:
                    t.archived = True
                    t.archive_reason = "auto_failed_cleanup"
                    archived_count += 1
                    
                # Update legacy trades
                candidates_legacy = db.query(Trade).filter(
                    Trade.status.in_(["FAILED", "SKIPPED"]),
                    Trade.archived == False,
                    Trade.ts < cutoff
                ).all()
                
                for t in candidates_legacy:
                    t.archived = True
                    t.archive_reason = "auto_failed_cleanup"
                    archived_count += 1
                    
                # Update persisted_positions
                candidates_persisted = db.query(PersistedPosition).filter(
                    PersistedPosition.status.in_(["FAILED", "SKIPPED"]),
                    PersistedPosition.archived == 0,
                    PersistedPosition.entry_time < cutoff
                ).all()
                
                for t in candidates_persisted:
                    t.archived = 1
                    t.archive_reason = "auto_failed_cleanup"
                    archived_count += 1
                    
                if archived_count > 0:
                    db.commit()
                    logger.info(f"[ARCHIVER] Auto-archived {archived_count} old FAILED/SKIPPED trades")
                    # Broadcast refresh
                    from backend.ws.manager import ws_manager
                    await ws_manager.broadcast({
                        "type": "stats_updated",
                        "message": "Archived FAILED trades"
                    })
                    await ws_manager.broadcast({
                        "type": "trade_archived",
                        "count": archived_count
                    })
            except Exception as e:
                db.rollback()
                logger.error(f"[ARCHIVER] DB error: {e}")
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"[ARCHIVER] Loop error: {e}")


# ──────────────────────────────────────────────────
#  Startup & Shutdown Hooks
# ──────────────────────────────────────────────────

def _resolve_multi_broker_executors(
    executors: Dict[str, Any],
) -> Tuple[bool, Optional[Any], bool, Optional[Any], bool]:
    """
    Resolve broker enablement and executor injection for MultiBrokerEngine (Lot A1).

    Fail-closed rule: Kraken executor injection requires BOTH KRAKEN_ENABLED and
    KRAKEN_EXECUTION_ENABLED (via smc_config.is_kraken_execution_enabled) to be
    explicitly enabled. Absent, empty, false, or invalid values withhold the
    Kraken executor while preserving ctx.executors for read-only/reconciliation.
    """
    bitget_enabled = os.getenv("BITGET_ENABLED", "false").strip().lower() in ("1", "true", "yes")
    kraken_enabled = os.getenv("KRAKEN_ENABLED", "false").strip().lower() in ("1", "true", "yes")
    kraken_exec_enabled = is_kraken_execution_enabled()

    bitget_executor = executors.get("futures") if bitget_enabled else None
    kraken_executor = (
        executors.get("kraken_futures")
        if (kraken_enabled and kraken_exec_enabled)
        else None
    )
    return bitget_enabled, bitget_executor, kraken_enabled, kraken_executor, kraken_exec_enabled


@app.on_event("startup")
async def startup_event():
    if is_test_mode() or os.getenv("SMARTTRADE_SKIP_APP_LIFESPAN", "") == "1":
        logger.info("[BOOT_SKIP] Skipping production startup_event in isolated test mode")
        return
    logger.info("=" * 60)
    logger.info("[BOOT_START] SMC Engine API v3.1 starting...")
    logger.info("=" * 60)
    logger.info("[CONFIG] TRADING_MODE=%s AUTO_EXECUTE_MODE=%s",
               SMC_CONFIG.get("TRADING_MODE", "paper"),
               SMC_CONFIG.get("AUTO_EXECUTE_MODE", "false"))
    logger.info("[CONFIG] SYMBOLS=%s", SMC_CONFIG.get("SYMBOLS", []))

    # ════════════════════════════════════════════════════════════════
    # PHASE 6: Boot observability + event bus early (before executors)
    # ════════════════════════════════════════════════════════════════
    from core.phase6_wiring import init_phase6_flags, init_observability, start_event_bus
    init_phase6_flags()
    init_observability()
    await start_event_bus()
    logger.info("[PHASE6] Early boot complete (observability + event bus)")
    
    # ════════════════════════════════════════════════════════════════
    # LOG ALL REGISTERED ROUTES (Debug 404 issues)
    # ════════════════════════════════════════════════════════════════
    logger.info("[ROUTES] Registered API routes:")
    for route in app.routes:
        if hasattr(route, "methods"):
            methods = ",".join(route.methods - {"HEAD", "OPTIONS"})
            logger.info(f"  {methods:8} {route.path}")
        elif hasattr(route, "path"):
            # WebSocket route
            logger.info(f"  WS       {route.path}")
    logger.info("[ROUTES] Total: %d routes", len(app.routes))
    
    # Materialize shared AppContext and inject into orchestrator & market routes
    real_ctx = get_app_context()
    set_market_context(real_ctx)
    logger.info("[BOOT] Market routes context set")
    
    # Initialize databases
    orch_mod.init_db()
    init_trades_db()  # Initialize trades/stats database (SQLAlchemy)
    init_live_trades_db()  # Initialize live_trades table for LIVE trading
    logger.info("[BOOT] All databases initialized")
    
    # Register WebSocket manager for real-time broadcasts
    set_ws_manager(ws_manager)
    set_close_service_ws_manager(ws_manager)  # Also for close_position_service
    logger.info("[BOOT] WebSocket manager registered for trade broadcasts")
    
    logger.info("[BOOT_COMPLETE] SMC Engine API ready")

    # ── Record ENGINE_START ops event ─────────────────
    try:
        from backend.services.analytics_service import AnalyticsRecorder
        AnalyticsRecorder.record_ops_event(
            "ENGINE_START",
            "INFO",
            detail={
                "trading_mode": SMC_CONFIG.get("TRADING_MODE", "paper"),
                "auto_execute_mode": SMC_CONFIG.get("AUTO_EXECUTE_MODE", "false"),
                "symbols": SMC_CONFIG.get("SYMBOLS", []),
            },
        )
    except Exception:
        pass

    # ──────────────────────────────────────────────────
    #  Initialize Executors & Orchestrators
    # ──────────────────────────────────────────────────
    env_key = os.getenv("BITGET_API_KEY")
    env_secret = os.getenv("BITGET_API_SECRET")
    env_pass = os.getenv("BITGET_PASSPHRASE")
    
    # 1. Register Bitget Executor
    if env_key and env_secret:
        exc = BitgetFuturesExecutor(env_key, env_secret, env_pass)
        ctx.executors["futures"] = exc
        logger.info("[BOOT] Bitget executor registered")
        
        # 1b. Initialize and register PositionManager for close/reduce operations
        pm = PositionManager(exc)
        set_route_position_manager(pm)
        logger.info("[BOOT] PositionManager registered for close/reduce API")
        
        # 1c. Register ClosePositionService with executor
        set_close_service_executor(exc)
        logger.info("[BOOT] ClosePositionService registered with executor")
        
        # 1d. Initialize Manual Execution API with executor
        set_manual_executor(exc)
        set_manual_app_context(real_ctx)
        logger.info("[BOOT] Manual execution API initialized")
    
    # ──────────────────────────────────────────────────
    #  1e. Register Kraken Futures Executor (if enabled)
    # ──────────────────────────────────────────────────
    if settings.kraken_configured:
        try:
            kraken_key = settings.get_kraken_api_key()
            kraken_secret = settings.get_kraken_api_secret()
            
            kraken_exc = KrakenFuturesExecutor(
                api_key=kraken_key,
                api_secret=kraken_secret,
                timeout=10.0,
                auto_reconnect=True,
            )
            
            # Connect WebSocket (required for order execution)
            await kraken_exc.connect()
            
            ctx.executors["kraken_futures"] = kraken_exc
            logger.info("[BOOT] Kraken Futures executor registered and connected")

            # ── Lot B: Deterministic Startup Reconciliation on Kraken Wrapper ──
            # Never force _global_entry_lock=False: reconcile_on_startup() keeps
            # the lock active if any QUARANTINED trade or discrepancy exists.
            try:
                _k_wrapper = kraken_exc._get_wrapper() if hasattr(kraken_exc, "_get_wrapper") else kraken_exc
                if hasattr(_k_wrapper, "reconcile_on_startup"):
                    _rec_report = await _k_wrapper.reconcile_on_startup()
                    logger.info(
                        "[BOOT] Kraken startup reconciliation completed | startup_secure=%s | global_entry_lock=%s",
                        _rec_report.get("startup_secure"),
                        _rec_report.get("global_entry_lock"),
                    )
            except Exception as rec_err:
                logger.critical("[BOOT] Kraken startup reconciliation failed (fail-closed lock retained): %s", rec_err)
            
            # ── Kraken Mode: READ_ONLY vs EXECUTE ──────────────────
            # When KRAKEN_EXECUTION_ENABLED=false, Kraken is connected for
            # data only (balance, positions, health) but NOT for execution.
            _exec_enabled = is_kraken_execution_enabled()
            ctx._kraken_mode = "execute" if _exec_enabled else "read_only"
            
            if _exec_enabled:
                # Register with manual execution routes for multi-broker support
                set_manual_kraken_executor(kraken_exc)
                logger.info("[BOOT] Kraken executor registered with manual routes")
                logger.info(
                    "[KRAKEN] Connected | mode=EXECUTE | mirror_trading=%s",
                    "ENABLED" if settings.mirror_trading_enabled else "DISABLED"
                )
            else:
                # Explicitly UNSET manual executor for Kraken to be safe
                set_manual_kraken_executor(None)
                logger.info(
                    "[KRAKEN] Connected | mode=READ_ONLY | execution_disabled | "
                    "config=KRAKEN_EXECUTION_ENABLED=false — Kraken will NOT execute orders"
                )
            
            # Fetch and log initial Kraken balance
            try:
                kraken_balance = await kraken_exc.get_account_info()
                kraken_available = kraken_balance.get("available", 0.0)
                kraken_total = kraken_balance.get("total", 0.0)
                kraken_pnl = kraken_balance.get("unrealizedPL", 0.0)
                ctx._kraken_balance = kraken_balance
                
                logger.info(
                    "[KRAKEN_BALANCE] Startup balance: available=%.2f USD | total=%.2f | pnl=%.2f",
                    kraken_available, kraken_total, kraken_pnl
                )
            except Exception as bal_err:
                logger.warning("[KRAKEN_BALANCE] Failed to fetch startup balance: %s", bal_err)
                ctx._kraken_balance = None
            
            # Log broker status
            logger.info(
                "[BOOT] Multi-broker ready | default=%s | bitget=%s | kraken=%s",
                settings.default_broker,
                "connected" if ctx.executors.get("futures") else "disabled",
                "connected"
            )
        except Exception as e:
            logger.error("[BOOT] Failed to initialize Kraken executor: %s", e, exc_info=True)
            logger.warning("[BOOT] Continuing with Bitget only")
    else:
        logger.info("[BOOT] Kraken disabled (KRAKEN_ENABLED=false or credentials missing)")

    # ──────────────────────────────────────────────────
    #  1f. Register MetaTrader 5 (MT5) Executor (if enabled)
    # ──────────────────────────────────────────────────
    try:
        from backend.execution.mt5_executor import MT5Executor, is_mt5_enabled, is_mt5_execution_enabled
        if is_mt5_enabled():
            mt5_exc = MT5Executor()
            mt5_ok = await mt5_exc.connect()
            if mt5_ok:
                ctx.executors["mt5"] = mt5_exc
                mt5_bal = await mt5_exc.get_account_info()
                ctx._mt5_balance = mt5_bal
                logger.info(
                    "[BOOT] MetaTrader 5 executor registered and connected | server=%s | mode=%s | balance=%.2f %s | exec=%s",
                    mt5_bal.get("server"),
                    mt5_bal.get("trade_mode"),
                    mt5_bal.get("balance", 0.0),
                    mt5_bal.get("currency", "USD"),
                    "ENABLED" if is_mt5_execution_enabled() else "READ_ONLY",
                )
                # Start automatic pre-close / weekend / rollover flatten guard for MT5 Forex positions
                async def _mt5_close_guard_loop():
                    while True:
                        try:
                            if getattr(mt5_exc, "is_connected", False):
                                await mt5_exc.enforce_market_close_flatten()
                        except asyncio.CancelledError:
                            raise
                        except Exception as guard_err:
                            logger.error("[MT5_CLOSE_GUARD] Loop error: %s", guard_err)
                        await asyncio.sleep(30)

                ctx._mt5_close_guard_task = asyncio.create_task(_mt5_close_guard_loop())
                logger.info("[BOOT] MT5 Forex market-close auto-flatten guard started (30s interval)")

                # Start dedicated MT5 Forex/Metals SMC business-day loop (EURUSD.s, GBPUSD.s, USDJPY.s, XAUUSD.s)
                from backend.execution.mt5_forex_smc_loop import run_mt5_forex_smc_loop
                ctx._mt5_forex_smc_task = asyncio.create_task(
                    run_mt5_forex_smc_loop(mt5_exc, ws_manager=ws_manager)
                )
                logger.info("[BOOT] MT5 Forex SMC business-day loop started")
            else:
                logger.error("[BOOT] MetaTrader 5 connection failed: %s", mt5_exc.last_error)
        else:
            logger.info("[BOOT] MetaTrader 5 disabled (MT5_ENABLED=false)")
    except Exception as mt5_err:
        logger.error("[BOOT] Failed to initialize MetaTrader 5 executor: %s", mt5_err, exc_info=True)
    
    # ──────────────────────────────────────────────────
    #  Initialize Multi-Broker Engine
    # ──────────────────────────────────────────────────
    try:
        from smc_futures.engine.multi_broker_engine import MultiBrokerEngine, MultiBrokerConfig
        from backend.execution.mt5_executor import is_mt5_enabled as _is_mt5_on, is_mt5_execution_enabled as _is_mt5_exec_on
        
        # Respect BITGET_ENABLED / KRAKEN_ENABLED / KRAKEN_EXECUTION_ENABLED env vars (Lot A1 fail-closed)
        (
            _bitget_enabled,
            _bitget_executor,
            _kraken_enabled,
            _kraken_executor,
            _kraken_exec_enabled,
        ) = _resolve_multi_broker_executors(ctx.executors)
        _mt5_executor = ctx.executors.get("mt5") if (_is_mt5_on() and _is_mt5_exec_on()) else None
        _mt5_syms_raw = os.getenv("MT5_SYMBOLS", "BTCUSD,ETHUSD,SOLUSD,XRPUSD")
        _mt5_symbols = [s.strip() for s in _mt5_syms_raw.split(",") if s.strip()]
        
        mbe_config = MultiBrokerConfig(
            default_broker=getattr(settings, 'default_broker', 'kraken'),
            bitget_enabled=_bitget_enabled and _bitget_executor is not None,
            kraken_enabled=_kraken_enabled and _kraken_exec_enabled and _kraken_executor is not None,
            mt5_enabled=_mt5_executor is not None,
            bitget_symbols=list(getattr(settings, 'bot_symbols', ['BTCUSDT'])) if isinstance(getattr(settings, 'bot_symbols', ['BTCUSDT']), (list, tuple)) else [getattr(settings, 'bot_symbols', 'BTCUSDT')],
            kraken_symbols=list(getattr(settings, 'kraken_symbols', ['PI_XBTUSD'])) if isinstance(getattr(settings, 'kraken_symbols', ['PI_XBTUSD']), (list, tuple)) else [getattr(settings, 'kraken_symbols', 'PI_XBTUSD')],
            mt5_symbols=_mt5_symbols,
            max_position_size_pct=float(getattr(settings, 'max_position_pct', 0.1)),
            loop_interval_sec=int(getattr(settings, 'loop_interval_seconds', 60)),
        )
        
        multi_engine = MultiBrokerEngine(
            config=mbe_config,
            bitget_executor=_bitget_executor,
            kraken_executor=_kraken_executor,
            mt5_executor=_mt5_executor,
        )
        
        # Initialize market data clients (required for candle fetching)
        await multi_engine.initialize()
        
        set_multi_broker_engine(multi_engine)
        ctx._multi_broker_engine = multi_engine  # Store in context too
        
        _mbe_mode = (
            "DUAL (bitget+kraken)"
            if (mbe_config.bitget_enabled and mbe_config.kraken_enabled)
            else f"SINGLE ({mbe_config.default_broker} only)"
        )
        kraken_exec_status = (
            "ENABLED"
            if mbe_config.kraken_enabled
            else "DISABLED (KRAKEN_EXECUTION_ENABLED=false)"
            if (_kraken_enabled and not _kraken_exec_enabled)
            else "DISABLED"
        )
        bitget_exec_status = "ENABLED" if mbe_config.bitget_enabled else "DISABLED"
        logger.info("[BOOT] Multi-broker engine initialized | mode=%s | bitget=%s | kraken=%s", 
                   _mbe_mode, bitget_exec_status, kraken_exec_status)
    except Exception as e:
        logger.warning("[BOOT] Multi-broker engine init skipped: %s", e)
    
    # 2. Initialize Orchestrators
    symbols = SMC_CONFIG.get("SYMBOLS", [])
    _kraken_boot_avail = 0.0
    if isinstance(getattr(ctx, "_kraken_balance", None), dict):
        try:
            _kraken_boot_avail = float(ctx._kraken_balance.get("available", 0.0) or 0.0)
        except (TypeError, ValueError):
            _kraken_boot_avail = 0.0
    _bitget_is_enabled = os.getenv("BITGET_ENABLED", "false").lower() in ("1", "true", "yes")
    if _kraken_boot_avail > 0 and (not _bitget_is_enabled or getattr(settings, "default_broker", "kraken") == "kraken"):
        account_balance = _kraken_boot_avail
        logger.info(f"[BOOT] Orchestrator initial balance seeded from Kraken: {account_balance:.2f} USD")
    else:
        account_balance = float(os.getenv("ACCOUNT_BALANCE", 10000.0))
    live_trading = (SMC_CONFIG.get("TRADING_MODE", "paper") == "live")
    _pass_bitget_creds = _bitget_is_enabled
    
    for symbol in symbols:
        try:
            orch = orch_mod.SMCFuturesOrchestrator(
                symbol=symbol,
                context=ctx,
                account_balance=account_balance,
                api_key=env_key if _pass_bitget_creds else None,
                secret_key=env_secret if _pass_bitget_creds else None,
                passphrase=env_pass if _pass_bitget_creds else None,
                live_trading=live_trading,
                strategy_mode=SMC_CONFIG.get("MODE", "smc"),
                timeframe="4h"
            )
            ctx.register_orchestrator(symbol, orch)
            logger.info(f"[BOOT] Orchestrator initialized for {symbol}")
        except Exception as e:
            logger.error(f"[BOOT] Failed to initialize orchestrator for {symbol}: {e}")

    # ════════════════════════════════════════════════════════════════
    # RECOVER OPEN TRADES FROM SQLITE (Crash Recovery)
    # ════════════════════════════════════════════════════════════════
    if live_trading:
        try:
            open_trades = await recover_open_trades()
            
            if open_trades:
                logger.info(f"[RECOVERY] Found {len(open_trades)} OPEN trades in database")
                
                # Restore active_trade state in orchestrators
                for trade in open_trades:
                    symbol = trade.symbol
                    orch = ctx.orchestrators.get(symbol)
                    if orch:
                        orch.active_trade = {
                            "order_id": trade.order_id,
                            "entry": trade.entry,
                            "stop": trade.stop,
                            "tp": trade.tp,
                            "side": trade.side,
                            "qty": trade.qty,
                            "leverage": trade.leverage,
                            "ts_open": trade.ts_open.isoformat() if trade.ts_open else None,
                        }
                        logger.info(f"[RECOVERY] Restored active_trade for {symbol} ({trade.broker}) | order_id={trade.order_id}")
                
                # Sync with exchange to verify positions still exist
                bitget_executor = ctx.executors.get("futures")
                kraken_executor = ctx.executors.get("kraken_futures")
                await sync_with_exchange(bitget_executor, kraken_executor, open_trades)
            else:
                logger.info("[RECOVERY] No OPEN trades to recover")
        except Exception as e:
            logger.error(f"[RECOVERY] Failed to recover trades: {e}", exc_info=True)

    # 3. Start Strategy Loop
    asyncio.create_task(orch_mod.strategy_loop())
    logger.info("🚀 Background Strategy Loop Started")
    
    # 4. Start WebSocket Heartbeat
    asyncio.create_task(heartbeat_loop(interval_sec=30))
    logger.info("💓 WebSocket Heartbeat Loop Started")

    # ════════════════════════════════════════════════════════════════
    # PHASE 6: Boot remaining modules (alert, risk_v2, reconciliation, isolation)
    # ════════════════════════════════════════════════════════════════
    from core.phase6_wiring import (
        FLAGS as P6_FLAGS,
        init_alert_manager,
        init_risk_engine_v2,
        start_reconciliation_v2,
        start_isolation,
        publish_event,
    )

    # Alert manager (needs event bus already running)
    init_alert_manager()

    # Risk V2 (needs capital)
    init_risk_engine_v2(initial_capital=account_balance)

    # Reconciliation V2 (only include Bitget when BITGET_ENABLED=true)
    _p6_bitget = ctx.executors.get("futures") if _bitget_is_enabled else None
    _p6_kraken = ctx.executors.get("kraken_futures")
    start_reconciliation_v2(_p6_bitget, _p6_kraken)

    # Trade Reconciler V2 is now sole reconciler (started above via start_reconciliation_v2)
    logger.info("🔄 Reconciliation V2 active (V1 removed)")

    # Isolation Supervisor (mandatory for Phase 7/10 trading gate in evaluate_risk_v2)
    _isolation_services = {}
    async def _risk_v2_health_loop():
        from core.phase6_wiring import get_risk_v2
        while True:
            rv2 = get_risk_v2()
            if rv2 is not None:
                rv2.get_status()
            await asyncio.sleep(30)
    _isolation_services["risk_engine"] = _risk_v2_health_loop
    try:
        from core.long_run_monitor import get_monitor
        _isolation_services["long_run_monitor"] = get_monitor().run
    except Exception:
        pass
    await start_isolation(_isolation_services)
    logger.info("🛡️ Isolation Supervisor started (trading gate active)")

    # 6. Start Archiver Loop
    asyncio.create_task(auto_archive_failed_loop())
    logger.info("🗑️ Auto-Archive Background Loop Started")

    # 7. Start Position Guardian (Safety Net — Kraken primary, fallback Bitget only if Kraken absent)
    if SMC_CONFIG.get("GUARDIAN_ENABLED", True):
        from smc_futures.engine.execution.position_guardian import PositionGuardian
        executor = (
            ctx.executors.get("kraken_futures")
            or ctx.get_active_executor("kraken")
            or ctx.get_active_executor("bitget")
        )
        if executor:
            guardian = PositionGuardian(executor=executor, config=SMC_CONFIG)
            ctx.set_guardian(guardian)
            guardian.start()
            logger.info("🛡️ Position Guardian Background Loop Started (broker=%s)", "kraken" if ctx.executors.get("kraken_futures") else "bitget")
        else:
            logger.warning("🛡️ Position Guardian SKIP: No active executor found")

    # 8. Start Analytics Aggregation Background Loop
    asyncio.create_task(_analytics_aggregation_loop())
    logger.info("📊 Analytics Aggregation Background Loop Started")


async def _analytics_aggregation_loop():
    """
    Background loop that aggregates analytics data every 5 minutes.
    Ensures daily_analytics table stays up-to-date for the Data Explorer frontend.
    """
    from datetime import date, timedelta
    try:
        from backend.services.analytics_service import AnalyticsAggregator
    except ImportError:
        logger.warning("[ANALYTICS] AnalyticsAggregator not available, aggregation loop disabled")
        return

    # Initial backfill: aggregate last 7 days on startup
    await asyncio.sleep(10)  # Wait for DB init
    try:
        today = date.today()
        start = today - timedelta(days=7)
        count = AnalyticsAggregator.backfill(start, today)
        logger.info(f"[ANALYTICS] Startup backfill: {count} days aggregated ({start} → {today})")
    except Exception as e:
        logger.error(f"[ANALYTICS] Startup backfill failed: {e}")

    # Periodic aggregation loop (every 5 minutes)
    while True:
        try:
            await asyncio.sleep(300)  # 5 minutes
            result = AnalyticsAggregator.aggregate_today()
            if result:
                logger.debug("[ANALYTICS] Today aggregated successfully")
        except Exception as e:
            logger.error(f"[ANALYTICS] Aggregation loop error: {e}")
            await asyncio.sleep(60)


@app.on_event("shutdown")
async def shutdown_event():
    if is_test_mode() or os.getenv("SMARTTRADE_SKIP_APP_LIFESPAN", "") == "1":
        logger.info("[SHUTDOWN_SKIP] Skipping production shutdown_event in isolated test mode")
        return
    logger.info("[SHUTDOWN] SMC Engine API stopping...")
    # Phase 1.9.8: Cleanly shutdown MultiBrokerEngine & KrakenFuturesBookFeed
    try:
        _mbe = getattr(ctx, "_multi_broker_engine", None)
        if _mbe is not None and hasattr(_mbe, "shutdown"):
            await _mbe.shutdown()
    except Exception as e:
        logger.error("[SHUTDOWN] MultiBrokerEngine shutdown error: %s", e)
    # Cleanly stop MT5 close guard & disconnect MT5 executor if registered outside MultiBrokerEngine
    try:
        _mt5_guard = getattr(ctx, "_mt5_close_guard_task", None)
        if _mt5_guard is not None and not _mt5_guard.done():
            _mt5_guard.cancel()
        _mt5_fx_task = getattr(ctx, "_mt5_forex_smc_task", None)
        if _mt5_fx_task is not None and not _mt5_fx_task.done():
            _mt5_fx_task.cancel()
        _mt5_exc = ctx.executors.get("mt5")
        if _mt5_exc is not None and getattr(_mt5_exc, "is_connected", False):
            await _mt5_exc.disconnect()
    except Exception as e:
        logger.error("[SHUTDOWN] MT5 disconnect error: %s", e)
    # Phase 6 shutdown (before other services)
    try:
        from core.phase6_wiring import shutdown_phase6
        await shutdown_phase6()
    except Exception as e:
        logger.error("[SHUTDOWN] Phase 6 shutdown error: %s", e)
    # Record ENGINE_STOP ops event
    try:
        from backend.services.analytics_service import AnalyticsRecorder
        AnalyticsRecorder.record_ops_event("ENGINE_STOP", "INFO")
    except Exception:
        pass
    # V1 trade_reconciler removed — V2 reconciler stopped via shutdown_phase6()


# ──────────────────────────────────────────────────
#  Phase 6 Health Endpoint
# ──────────────────────────────────────────────────

@app.get("/api/health/phase6")
async def get_phase6_health():
    """
    Phase 6 module health status.
    Returns flags, module states, event bus stats, risk V2 status, recon status.
    """
    from core.phase6_wiring import (
        FLAGS,
        get_bus,
        get_risk_v2,
        get_reconciliation_v2,
        get_phase6_health_aggregator,
        get_supervisor_status,
    )
    result = {
        "flags": FLAGS.summary(),
        "event_bus": None,
        "risk_v2": None,
        "reconciliation_v2": None,
        "alert_manager": None,
        "supervisor": get_supervisor_status(),
        "health_aggregator": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    bus = get_bus()
    if bus:
        result["event_bus"] = bus.get_stats()

    rv2 = get_risk_v2()
    if rv2:
        result["risk_v2"] = rv2.get_status()

    recon = get_reconciliation_v2()
    if recon:
        result["reconciliation_v2"] = recon.get_status()

    ha = get_phase6_health_aggregator()
    if ha:
        result["health_aggregator"] = await ha.check_all()

    from core.phase6_wiring import _alert_manager
    if _alert_manager:
        result["alert_manager"] = _alert_manager.get_status()

    return result


# ──────────────────────────────────────────────────
#  Preflight / System Status Endpoint
# ──────────────────────────────────────────────────

@app.get("/api/preflight")
async def get_preflight():
    """
    System preflight status — consolidated check for dashboards/CI.
    
    Returns:
        {
            "trading_mode": "live",
            "auto_execute": "true",
            "mirror_enabled": false,
            "brokers": {...},
            "ws_routes": ["/ws", "/ws/trades"],
            "balances": {"bitget": {...}, "kraken": {...}},
            "open_positions": {"bitget": 0, "kraken": 0},
            "timestamp": ISO8601
        }
    """
    kraken_exc = ctx.executors.get("kraken_futures")
    bitget_exc = ctx.executors.get("futures")

    # WS routes
    ws_routes = [
        r.path for r in app.routes
        if hasattr(r, "path") and "ws" in r.path.lower()
    ]

    # Balances (best-effort, non-blocking)
    bitget_balance = kraken_balance = None
    try:
        if bitget_exc and hasattr(bitget_exc, "get_account_balance"):
            bitget_balance = await bitget_exc.get_account_balance(marginCoin="USDT")
    except Exception as e:
        logger.debug("[PREFLIGHT] Bitget balance fetch failed: %s", e)
    try:
        if kraken_exc and hasattr(kraken_exc, "get_account_info"):
            kraken_balance = await kraken_exc.get_account_info()
    except Exception as e:
        logger.debug("[PREFLIGHT] Kraken balance fetch failed: %s", e)

    # Open positions (best-effort)
    bitget_pos_count = kraken_pos_count = 0
    try:
        if bitget_exc and hasattr(bitget_exc, "get_positions"):
            result = await bitget_exc.get_positions()
            positions = result.get("data", []) if isinstance(result, dict) else result
            bitget_pos_count = len(positions) if isinstance(positions, list) else 0
    except Exception as e:
        logger.debug("[PREFLIGHT] Bitget positions fetch failed: %s", e)
    try:
        if kraken_exc and hasattr(kraken_exc, "get_positions"):
            result = await kraken_exc.get_positions()
            positions = result.get("data", []) if isinstance(result, dict) else result
            kraken_pos_count = len(positions) if isinstance(positions, list) else 0
    except Exception as e:
        logger.debug("[PREFLIGHT] Kraken positions fetch failed: %s", e)

    return {
        "trading_mode": SMC_CONFIG.get("TRADING_MODE", "paper"),
        "auto_execute": SMC_CONFIG.get("AUTO_EXECUTE_MODE", "false"),
        "mirror_enabled": settings.mirror_trading_enabled,
        "confirm_live_multi_broker": settings.confirm_live_multi_broker,
        "brokers": ctx.get_broker_status(),
        "broker_modes": {
            "bitget": "execute" if ctx.executors.get("futures") else "disabled",
            "kraken": getattr(ctx, "_kraken_mode", "disabled"),
        },
        "ws_routes": ws_routes,
        "ws_clients_count": ws_manager.connection_count,
        "balances": {
            "bitget": bitget_balance,
            "kraken": kraken_balance,
        },
        "open_positions_exchange": {
            "bitget": bitget_pos_count,
            "kraken": kraken_pos_count,
        },
        "asia_strategy": {
            "range_ratio_multiplier": settings.asia_range_ratio_multiplier,
            "allow_range_too_large": settings.asia_allow_range_too_large,
            "thresholds": {
                "BTC": SMC_CONFIG.get("ASIA_MAX_RANGE_RATIO_BTC", 6.0) * settings.asia_range_ratio_multiplier,
                "ETH": SMC_CONFIG.get("ASIA_MAX_RANGE_RATIO_ETH", 5.0) * settings.asia_range_ratio_multiplier,
                "ALT": SMC_CONFIG.get("ASIA_MAX_RANGE_RATIO_ALT", 4.0) * settings.asia_range_ratio_multiplier,
            }
        },
        "dev_mode": settings.dev_mode,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/guardian/status")
async def get_guardian_status():
    """Return status and stats from the PositionGuardian."""
    if ctx.guardian:
        return ctx.guardian.get_status()
    return {"enabled": False, "running": False, "msg": "Guardian not initialized"}

# ──────────────────────────────────────────────────
#  Root endpoint
# ──────────────────────────────────────────────────

@app.get("/")
async def root():
    """Root endpoint with API documentation."""
    return {
        "name": "SMC Trading Engine",
        "version": "3.0",
        "documentation": "/docs",
        "endpoints": {
            "health": "GET /health",
            "status": "GET /api/status",
            "bot_stats": "GET /api/bot/stats",
            "kill_switch": "POST /api/bot/kill-switch",
            "websocket": "WS /ws/trades",
            "trades": "GET /api/trades?from=YYYY-MM-DD&to=YYYY-MM-DD",
            "trades_open": "GET /api/trades/open",
            "stats_summary": "GET /api/stats/summary?from=...&to=...",
            "stats_rr_series": "GET /api/stats/rr-series?from=...&to=...",
            "stats_heatmap": "GET /api/stats/heatmap?from=...&to=...",
            "stats_sessions": "GET /api/stats/sessions?from=...&to=...",
            "stats_presets": "GET /api/stats/presets?from=...&to=...",
        }
    }

# ──────────────────────────────────────────────────
#  PHASE 4.5 / 5 — Scale Readiness & Execution
# ──────────────────────────────────────────────────

# NOTE: /api/health/scale_readiness and /api/health/execution are defined above.
# Do NOT add duplicate handlers here.

# DEPRECATED: Do not run directly. Use run.py instead.
# If running directly (legacy fallback - NOT RECOMMENDED)
if __name__ == "__main__":
    import sys
    print("="*60, file=sys.stderr)
    print("WARNING: Direct execution is deprecated!", file=sys.stderr)
    print("Use: python run.py", file=sys.stderr)
    print("Or:  python run.py --reload  (for development)", file=sys.stderr)
    print("="*60, file=sys.stderr)
    import uvicorn
    uvicorn.run("smc_futures.api_smc_engine:app", host="0.0.0.0", port=8000, log_level="info")
