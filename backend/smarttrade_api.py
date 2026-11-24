"""
SmartTrade AI — FastAPI Backend (port 5000)

Bitget V2 Mix USDT-FUTURES order execution with shadow mode and guard-rails.

Start:
    cd bitget-smc-auto-trader
    .venv\\Scripts\\python.exe -m uvicorn backend.smarttrade_api:app --host 0.0.0.0 --port 5000
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from core.env_loader import safe_load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.bitget_v2_mix_client import BitgetV2MixClient

# ──────────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("smarttrade")

# ──────────────────────────────────────────────────
#  Config from .env
# ──────────────────────────────────────────────────

safe_load_dotenv()

CONFIG = {
    "AUTO_EXECUTE":           os.getenv("AUTO_EXECUTE", "false").lower(),        # false | shadow | true
    "DEFAULT_PRODUCT_TYPE":   os.getenv("DEFAULT_PRODUCT_TYPE", "USDT-FUTURES"),
    "DEFAULT_MARGIN_COIN":    os.getenv("DEFAULT_MARGIN_COIN", "USDT"),
    "DEFAULT_MARGIN_MODE":    os.getenv("DEFAULT_MARGIN_MODE", "isolated"),
    "DEFAULT_LEVERAGE":       int(os.getenv("DEFAULT_LEVERAGE", "3")),
    "MAX_TRADES_PER_DAY":     int(os.getenv("MAX_TRADES_PER_DAY", "1")),
    "FORCE_TRADE_ENABLED":    os.getenv("FORCE_TRADE_ENABLED", "false").lower() == "true",
    "TRADING_MODE":           os.getenv("TRADING_MODE", "paper"),
}

logger.info("[CONFIG] AUTO_EXECUTE=%s  FORCE_TRADE=%s  MAX_TRADES=%d  LEVERAGE=%d",
            CONFIG["AUTO_EXECUTE"], CONFIG["FORCE_TRADE_ENABLED"],
            CONFIG["MAX_TRADES_PER_DAY"], CONFIG["DEFAULT_LEVERAGE"])

# ──────────────────────────────────────────────────
#  Bitget client (None if no keys)
# ──────────────────────────────────────────────────

_api_key = os.getenv("BITGET_API_KEY", "")
_api_secret = os.getenv("BITGET_API_SECRET", "")
_passphrase = os.getenv("BITGET_PASSPHRASE", "")

bitget: Optional[BitgetV2MixClient] = None
if _api_key and _api_secret:
    bitget = BitgetV2MixClient(_api_key, _api_secret, _passphrase)
else:
    logger.warning("[CONFIG] Bitget API keys not set — live orders disabled")

# ──────────────────────────────────────────────────
#  Daily trade counter (in-memory)
# ──────────────────────────────────────────────────

_daily_trades: Dict[str, int] = {}  # symbol -> count
_daily_date: str = ""


def _check_daily_limit(symbol: str) -> tuple[bool, int, int]:
    """Returns (allowed, used, max)."""
    global _daily_date, _daily_trades
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _daily_date != today:
        _daily_date = today
        _daily_trades = {}
    used = _daily_trades.get(symbol, 0)
    max_d = CONFIG["MAX_TRADES_PER_DAY"]
    return used < max_d, used, max_d


def _record_trade(symbol: str):
    global _daily_trades
    _daily_trades[symbol] = _daily_trades.get(symbol, 0) + 1


# ──────────────────────────────────────────────────
#  FastAPI app
# ──────────────────────────────────────────────────

app = FastAPI(title="SmartTrade AI", version="3.0")

_ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173,http://localhost:8000"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

from backend.auth import ApiKeyMiddleware
app.add_middleware(ApiKeyMiddleware)


# ──────────────────────────────────────────────────
#  GET /health
# ──────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "smarttrade-ai",
        "version": "v3",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────────
#  GET /api/status
# ──────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    return {
        "status": "ok",
        "trading_mode": CONFIG["TRADING_MODE"],
        "auto_execute": CONFIG["AUTO_EXECUTE"],
        "force_trade_enabled": CONFIG["FORCE_TRADE_ENABLED"],
        "default_product_type": CONFIG["DEFAULT_PRODUCT_TYPE"],
        "default_leverage": CONFIG["DEFAULT_LEVERAGE"],
        "max_trades_per_day": CONFIG["MAX_TRADES_PER_DAY"],
        "executor_ready": bitget is not None,
    }


# ──────────────────────────────────────────────────
#  POST /api/v1/order
# ──────────────────────────────────────────────────

class OrderRequest(BaseModel):
    symbol: str = Field(..., min_length=3, max_length=30, pattern=r'^[A-Za-z0-9/:\-_]+$')
    side: str = Field(..., pattern=r'^(buy|sell)$')
    order_type: str = Field("market", pattern=r'^(market|limit)$')
    size: str = Field(..., min_length=1, max_length=30)
    price: Optional[str] = None
    product_type: str = "USDT-FUTURES"
    margin_coin: str = "USDT"
    margin_mode: str = "isolated"
    trade_side: str = "open"
    model_config = ConfigDict(extra="forbid")


@app.post("/api/v1/order")
async def place_order(req: OrderRequest):
    """
    Place an order respecting AUTO_EXECUTE mode:
    - false  → refuse
    - shadow → log WOULD_SEND, return fake orderId
    - true   → send to Bitget, return real orderId
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    mode = CONFIG["AUTO_EXECUTE"]

    logger.info(
        "[ORDER_START] mode=%s symbol=%s side=%s type=%s size=%s",
        mode, req.symbol, req.side, req.order_type, req.size,
    )

    # ── MODE: false → refuse ──────────────────────
    if mode == "false":
        logger.info("[GUARD_REJECT] AUTO_EXECUTE=false — order refused")
        return JSONResponse(status_code=403, content={
            "error": "auto_execute_disabled",
            "message": "AUTO_EXECUTE=false — set to 'shadow' or 'true' to enable",
        })

    # ── Daily limit guard ─────────────────────────
    allowed, used, max_d = _check_daily_limit(req.symbol)
    if not allowed:
        logger.warning("[GUARD_REJECT] daily limit %d/%d for %s", used, max_d, req.symbol)
        return JSONResponse(status_code=429, content={
            "error": "daily_limit",
            "message": f"Max {max_d} trades/day for {req.symbol} (used {used})",
        })

    payload = {
        "symbol": req.symbol,
        "side": req.side,
        "orderType": req.order_type,
        "size": req.size,
        "price": req.price,
        "productType": req.product_type,
        "marginCoin": req.margin_coin,
        "marginMode": req.margin_mode,
        "tradeSide": req.trade_side,
    }
    logger.info("[ORDER_BODY] %s", payload)

    # ── MODE: shadow → log only ───────────────────
    if mode == "shadow":
        fake_id = f"SHADOW-{uuid.uuid4().hex[:12].upper()}"
        logger.info("[ORDER_SEND] WOULD_SEND %s %s %s (shadow)", req.symbol, req.side, req.size)
        _record_trade(req.symbol)
        return {
            "status": "shadow",
            "orderId": fake_id,
            "message": "WOULD_SEND — no real order placed",
            "payload": payload,
            "daily_used": used + 1,
            "daily_max": max_d,
            "timestamp": now_iso,
        }

    # ── MODE: true → send to Bitget ───────────────
    if mode == "true":
        if not bitget:
            logger.error("[GUARD_REJECT] no Bitget client — API keys missing")
            return JSONResponse(status_code=503, content={
                "error": "no_executor",
                "message": "Bitget API keys not configured",
            })

        try:
            if req.order_type == "limit":
                if not req.price:
                    return JSONResponse(status_code=400, content={
                        "error": "missing_price",
                        "message": "price is required for limit orders",
                    })
                result = await bitget.place_order_limit(
                    symbol=req.symbol, side=req.side, size=req.size,
                    price=req.price, product_type=req.product_type,
                    margin_coin=req.margin_coin, margin_mode=req.margin_mode,
                    trade_side=req.trade_side,
                )
            else:
                result = await bitget.place_order_market(
                    symbol=req.symbol, side=req.side, size=req.size,
                    product_type=req.product_type, margin_coin=req.margin_coin,
                    margin_mode=req.margin_mode, trade_side=req.trade_side,
                )

            if result.get("code") == "00000":
                _record_trade(req.symbol)
                order_id = result.get("data", {}).get("orderId", "unknown")
                logger.info("[EXEC_OK] orderId=%s", order_id)
                return {
                    "status": "live",
                    "orderId": order_id,
                    "result": result,
                    "daily_used": used + 1,
                    "daily_max": max_d,
                    "timestamp": now_iso,
                }
            else:
                logger.error("[EXEC_FAIL] code=%s msg=%s", result.get("code"), result.get("msg"))
                return JSONResponse(status_code=502, content={
                    "error": "bitget_error",
                    "code": result.get("code"),
                    "message": result.get("msg", "Unknown Bitget error"),
                })

        except Exception as e:
            logger.error("[EXEC_FAIL] %s", e)
            return JSONResponse(status_code=500, content={
                "error": "execution_error",
                "message": str(e),
            })

    # Unknown mode
    return JSONResponse(status_code=400, content={
        "error": "invalid_mode",
        "message": f"AUTO_EXECUTE='{mode}' is not valid (use false|shadow|true)",
    })


# ──────────────────────────────────────────────────
#  POST /api/test/force-trade
# ──────────────────────────────────────────────────

class ForceTradeRequest(BaseModel):
    symbol: str = Field("BTCUSDT", min_length=3, max_length=30, pattern=r'^[A-Za-z0-9/:\-_]+$')
    side: str = Field("buy", pattern=r'^(buy|sell)$')
    order_type: str = Field("market", pattern=r'^(market|limit)$')
    size: str = Field("0.001", min_length=1, max_length=30)
    price: Optional[str] = None
    mode: Optional[str] = Field(None, pattern=r'^(shadow|true)$')
    model_config = ConfigDict(extra="forbid")


@app.post("/api/test/force-trade")
async def test_force_trade(req: ForceTradeRequest):
    """
    Test endpoint for automation. Requires FORCE_TRADE_ENABLED=true.
    Defaults to shadow mode unless explicitly mode=true.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    logger.info(
        "[TEST_FORCE] request: symbol=%s side=%s type=%s size=%s mode=%s",
        req.symbol, req.side, req.order_type, req.size, req.mode,
    )

    # ── Guard: FORCE_TRADE_ENABLED ────────────────
    if not CONFIG["FORCE_TRADE_ENABLED"]:
        logger.warning("[GUARD_REJECT] FORCE_TRADE_ENABLED=false")
        return JSONResponse(status_code=403, content={
            "error": "force_trade_disabled",
            "message": "Set FORCE_TRADE_ENABLED=true in .env to use this endpoint",
        })

    # ── Guard: daily limit ────────────────────────
    allowed, used, max_d = _check_daily_limit(req.symbol)
    if not allowed:
        logger.warning("[GUARD_REJECT] daily limit %d/%d for %s", used, max_d, req.symbol)
        return JSONResponse(status_code=429, content={
            "error": "daily_limit",
            "message": f"Max {max_d} force-trades/day for {req.symbol} (used {used})",
        })

    # ── Guard: minimum size ───────────────────────
    try:
        size_f = float(req.size)
    except ValueError:
        return JSONResponse(status_code=400, content={
            "error": "invalid_size", "message": f"size='{req.size}' is not a number",
        })

    if size_f <= 0:
        return JSONResponse(status_code=400, content={
            "error": "invalid_size", "message": "size must be > 0",
        })

    # Determine effective mode (default: shadow)
    effective_mode = (req.mode or "shadow").lower()
    if effective_mode not in ("shadow", "true"):
        effective_mode = "shadow"

    payload = {
        "symbol": req.symbol,
        "side": req.side,
        "orderType": req.order_type,
        "size": req.size,
        "price": req.price,
        "productType": CONFIG["DEFAULT_PRODUCT_TYPE"],
        "marginCoin": CONFIG["DEFAULT_MARGIN_COIN"],
        "marginMode": CONFIG["DEFAULT_MARGIN_MODE"],
    }

    # ── Shadow mode ───────────────────────────────
    if effective_mode == "shadow":
        fake_id = f"FORCE-SHADOW-{uuid.uuid4().hex[:12].upper()}"
        logger.info("[ORDER_SEND] WOULD_SEND %s %s %s (force-trade shadow)", req.symbol, req.side, req.size)
        _record_trade(req.symbol)
        return {
            "status": "shadow",
            "orderId": fake_id,
            "message": "Force-trade SHADOW — no real order",
            "payload": payload,
            "daily_used": used + 1,
            "daily_max": max_d,
            "timestamp": now_iso,
        }

    # ── Live mode ─────────────────────────────────
    if not bitget:
        return JSONResponse(status_code=503, content={
            "error": "no_executor", "message": "Bitget API keys not configured",
        })

    try:
        logger.info("[ORDER_SEND] SENDING LIVE force-trade %s %s %s", req.symbol, req.side, req.size)
        if req.order_type == "limit" and req.price:
            result = await bitget.place_order_limit(
                symbol=req.symbol, side=req.side, size=req.size, price=req.price,
                product_type=CONFIG["DEFAULT_PRODUCT_TYPE"],
                margin_coin=CONFIG["DEFAULT_MARGIN_COIN"],
                margin_mode=CONFIG["DEFAULT_MARGIN_MODE"],
            )
        else:
            result = await bitget.place_order_market(
                symbol=req.symbol, side=req.side, size=req.size,
                product_type=CONFIG["DEFAULT_PRODUCT_TYPE"],
                margin_coin=CONFIG["DEFAULT_MARGIN_COIN"],
                margin_mode=CONFIG["DEFAULT_MARGIN_MODE"],
            )

        if result.get("code") == "00000":
            _record_trade(req.symbol)
            order_id = result.get("data", {}).get("orderId", "unknown")
            logger.info("[EXEC_OK] force-trade orderId=%s", order_id)
            return {
                "status": "live",
                "orderId": order_id,
                "result": result,
                "daily_used": used + 1,
                "daily_max": max_d,
                "timestamp": now_iso,
            }
        else:
            logger.error("[EXEC_FAIL] code=%s msg=%s", result.get("code"), result.get("msg"))
            return JSONResponse(status_code=502, content={
                "error": "bitget_error",
                "code": result.get("code"),
                "message": result.get("msg", "Unknown error"),
            })
    except Exception as e:
        logger.error("[EXEC_FAIL] force-trade error: %s", e)
        return JSONResponse(status_code=500, content={
            "error": "execution_error", "message": str(e),
        })
