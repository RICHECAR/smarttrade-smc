"""
Market Data Routes
==================
Endpoints for market data: ticker, candles, positions, account balance
"""

import os
import time
import asyncio
import sqlite3
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger("smc_engine.routes.market")

router = APIRouter(prefix="/api", tags=["Market"])

# ──────────────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────────────

BITGET_BASE = "https://api.bitget.com"
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "market_data", "market_data.sqlite3")

TF_MAP_V2 = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "4h": "4H", "4H": "4H", "1d": "1D", "1D": "1D",
}


def tf_to_ms(tf: str) -> int:
    """Convert timeframe string to milliseconds."""
    tf_lower = tf.lower()
    multipliers = {
        "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
        "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }
    return multipliers.get(tf_lower, 300_000)


def normalize_symbol(s: str) -> str:
    """Normalize symbol to Bitget format (e.g. BTCUSDT)."""
    return s.replace("-", "").replace("_", "").upper()


# Lazy-loaded context reference
_ctx = None


def set_market_context(ctx):
    """Set the app context (called from api_smc_engine startup)."""
    global _ctx
    _ctx = ctx


def _get_ctx():
    global _ctx
    if _ctx is None:
        # Fallback: try to get from api_smc_engine module
        try:
            from smc_futures.api_smc_engine import ctx as api_ctx
            _ctx = api_ctx
        except ImportError:
            from smc_futures.app_context import build_app_context
            _ctx = build_app_context()
    return _ctx


def _get_executor():
    ctx = _get_ctx()
    return ctx.executors.get("futures") if ctx else None


# ──────────────────────────────────────────────────
#  GET /api/v1/market/ticker
# ──────────────────────────────────────────────────

@router.get("/v1/market/ticker")
async def get_ticker(symbol: str, productType: str = "USDT-FUTURES"):
    """
    Get market ticker for a symbol.
    
    Returns price info with mark/last/index prices.
    """
    ctx = _get_ctx()
    try:
        if ctx.market_client:
            ticker = await ctx.market_client.get_ticker(symbol, productType)
            return ticker
        else:
            # Fallback to direct API call
            url = f"{BITGET_BASE}/api/v2/mix/market/ticker"
            params = {"symbol": normalize_symbol(symbol), "productType": "USDT-FUTURES"}
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, params=params)
            if r.status_code != 200:
                raise HTTPException(502, f"Bitget error: {r.text}")
            body = r.json()
            if body.get("code") != "00000":
                raise HTTPException(502, f"Bitget: {body.get('msg')}")
            return body.get("data", {})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ticker error: {e}")
        raise HTTPException(500, str(e))


# ──────────────────────────────────────────────────
#  GET /api/smc/market/candles
# ──────────────────────────────────────────────────

@router.get("/smc/market/candles")
async def smc_market_candles(
    symbol: str = Query(..., description="Trading pair symbol (e.g. BTCUSDT)"),
    tf: str = Query("5m", description="Timeframe: 1m, 5m, 15m, 30m, 1h, 4h, 1d"),
    limit: int = Query(200, ge=10, le=1000, description="Number of candles"),
):
    """
    Get OHLCV candles with SQLite caching.
    
    Returns list of candles:
    [{"ts": 1234, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000}]
    """
    mapped_tf = TF_MAP_V2.get(tf, TF_MAP_V2.get(tf.lower(), tf))
    clean_symbol = normalize_symbol(symbol)

    # Try cache first
    if os.path.exists(DB_PATH):
        try:
            def db_check():
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute(
                    "SELECT MAX(ts) FROM candles WHERE symbol=? AND timeframe=?",
                    (clean_symbol, tf)
                )
                row = cur.fetchone()
                max_ts = row[0] if row else None

                fetch_required = True
                data = None

                if max_ts:
                    now_ms = int(time.time() * 1000)
                    interval_ms = tf_to_ms(tf)
                    if (now_ms - max_ts) < (interval_ms * 2):
                        fetch_required = False

                if not fetch_required:
                    cur.execute("""
                        SELECT ts, open, high, low, close, volume
                        FROM candles WHERE symbol=? AND timeframe=?
                        ORDER BY ts DESC LIMIT ?
                    """, (clean_symbol, tf, limit))
                    rows = cur.fetchall()
                    if rows:
                        rows.reverse()
                        data = [
                            {"ts": r[0], "open": r[1], "high": r[2],
                             "low": r[3], "close": r[4], "volume": r[5]}
                            for r in rows
                        ]
                conn.close()
                return fetch_required, data

            need_fetch, cached = await asyncio.to_thread(db_check)
            if not need_fetch and cached:
                return cached
        except Exception as e:
            logger.warning(f"SQLite cache read error: {e}")

    # Live fetch from Bitget
    logger.info(f"Fetching live candles for {clean_symbol} ({tf})")
    url = f"{BITGET_BASE}/api/v2/mix/market/candles"
    params = {
        "symbol": clean_symbol,
        "productType": "usdt-futures",
        "granularity": mapped_tf,
        "limit": str(min(limit, 200)),
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)

        if r.status_code != 200:
            raise HTTPException(502, f"Bitget error: {r.text}")

        body = r.json()
        if body.get("code") != "00000":
            raise HTTPException(502, f"Bitget: {body.get('msg')}")

        raw = body.get("data", [])
        out = []
        db_rows = []
        for row in raw:
            ts = int(row[0])
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            v = float(row[5]) if len(row) > 5 else 0.0
            out.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
            db_rows.append(("bitget", clean_symbol, tf, ts, o, h, l, c, v))

        out.sort(key=lambda x: x["ts"])

        # Upsert into SQLite
        if db_rows and os.path.exists(os.path.dirname(DB_PATH)):
            def db_upsert():
                conn = sqlite3.connect(DB_PATH)
                conn.executemany("""
                    INSERT OR REPLACE INTO candles
                    (exchange, symbol, timeframe, ts, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, db_rows)
                conn.commit()
                conn.close()
            await asyncio.to_thread(db_upsert)
            logger.info(f"Cached {len(db_rows)} candles for {clean_symbol}")

        return out

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Candles fetch error: {e}")
        return []


# ──────────────────────────────────────────────────
#  GET /api/v1/positions
# ──────────────────────────────────────────────────

@router.get("/v1/positions")
async def get_positions(symbol: Optional[str] = None):
    """
    Get open positions via Bitget executor.
    
    Returns:
    {
        "code": "00000",
        "count": N,
        "positions": [...],
        "source": "bitget_v2"
    }
    """
    executor = _get_executor()
    if not executor:
        return JSONResponse(status_code=503, content={
            "error": "no_executor",
            "message": "Bitget API keys not configured",
            "count": 0,
            "positions": [],
            "source": "none"
        })
    try:
        if hasattr(executor, "get_positions"):
            result = await executor.get_positions(symbol)
        elif hasattr(executor, "get_open_positions"):
            result = await executor.get_open_positions(symbol)
        else:
            return JSONResponse(status_code=501, content={
                "error": "not_implemented",
                "message": "Executor has no get_positions method",
                "count": 0,
                "positions": [],
                "source": "none"
            })
        
        if isinstance(result, dict):
            if "count" not in result:
                positions = result.get("positions", result.get("data", []))
                result["count"] = len(positions) if isinstance(positions, list) else 0
            if "source" not in result:
                result["source"] = "exchange"
            if "broker" not in result:
                result["broker"] = "bitget"
            if "status" not in result:
                result["status"] = "OPEN"
            if "positions" not in result and "data" in result:
                result["positions"] = result["data"]
        
        # Best-effort divergence check: exchange vs DB open trades
        try:
            from backend.services.live_trades_service import LiveTradesService
            svc = LiveTradesService()
            db_open = svc.get_open_count() if hasattr(svc, "get_open_count") else None
            if db_open is not None:
                result["db_open_count"] = db_open
                exchange_count = result.get("count", 0)
                if db_open > 0 and exchange_count == 0:
                    result["divergence_warning"] = (
                        f"DB has {db_open} open trades but exchange reports 0 positions"
                    )
        except Exception:
            pass  # Non-critical — don't break positions endpoint
        
        return result
        
    except Exception as e:
        logger.error(f"Positions error: {e}")
        return JSONResponse(status_code=500, content={
            "error": "internal_error",
            "message": str(e),
            "count": 0,
            "positions": [],
            "source": "error"
        })


# ──────────────────────────────────────────────────
#  GET /api/v1/account/futures
# ──────────────────────────────────────────────────

@router.get("/v1/account/futures")
async def get_futures_balance(marginCoin: str = "USDT"):
    """Get futures account balance."""
    executor = _get_executor()
    if not executor:
        return JSONResponse(status_code=503, content={
            "error": "no_executor",
            "message": "Bitget API keys not configured"
        })
    try:
        return await executor.get_account_balance(marginCoin)
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return JSONResponse(status_code=500, content={
            "error": "internal_error",
            "message": str(e)
        })
