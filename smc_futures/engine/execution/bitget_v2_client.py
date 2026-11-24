"""
BitgetV2Client — Unified Bitget Mix V2 API client for USDT-FUTURES.

All market data (ticker, candles) and account data (positions, balance)
go through this single client.  Every public method returns structured
typed dicts with [TICKER_RAW] / [TICKER_PARSED] / [STALE] logging.

Usage:
    from smc_futures.engine.execution.bitget_v2_client import BitgetV2Client
    client = BitgetV2Client()
    ticker = await client.get_ticker("BTCUSDT")
    candles = await client.get_candles("BTCUSDT", "4h", 200)
"""

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Literal, Optional

import httpx
from smc_futures.observability.metrics import REJECTS_TOTAL, LATENCY_HISTOGRAM

logger = logging.getLogger(__name__)

BITGET_BASE = "https://api.bitget.com"

# Timeframe → Bitget V2 granularity
TF_MAP: Dict[str, str] = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "4H": "4H",
    "6h": "6H", "12h": "12H", "1d": "1D",
}

PriceSource = Literal["mark", "last", "index"]

# Mapping: config value → Bitget response field name
_PRICE_FIELDS: Dict[str, str] = {
    "mark":  "markPrice",
    "last":  "lastPr",
    "index": "indexPrice",
}

# ──────────────────────────────────────────────────
#  Stale guard default: 1000ms (Phase 0 / Phase 4 compliant)
# ──────────────────────────────────────────────────
DEFAULT_STALE_THRESHOLD_MS = 1000

# Only futures product types are allowed — spot is REJECTED
ALLOWED_PRODUCT_TYPES = frozenset({
    "usdt-futures", "USDT-FUTURES",
    "usdc-futures", "USDC-FUTURES",
    "coin-futures", "COIN-FUTURES",
})


def _validate_product_type(pt: str) -> None:
    """Raise immediately if caller tries to use spot/non-futures."""
    if pt not in ALLOWED_PRODUCT_TYPES:
        raise ValueError(
            f"[SPOT_REJECTED] productType='{pt}' is NOT allowed. "
            f"Only futures types accepted: {sorted(ALLOWED_PRODUCT_TYPES)}"
        )


class StaleDataError(Exception):
    """Raised when ticker data is older than the stale threshold."""
    pass


class BitgetV2Client:
    """
    Stateless async HTTP client for Bitget Mix V2 (USDT-FUTURES).
    No API keys needed for public endpoints (ticker, candles).
    """

    def __init__(
        self,
        price_source: PriceSource = "mark",
        stale_threshold_ms: int = DEFAULT_STALE_THRESHOLD_MS,
        timeout: float = 10.0,
    ):
        self.price_source = price_source
        self.stale_threshold_ms = stale_threshold_ms
        self.timeout = timeout
        logger.info("[PRICE_SOURCE] configured = %s  stale_threshold = %dms",
                     price_source, stale_threshold_ms)

    # ──────────────────────────────────────────────────
    #  GET /api/v2/mix/market/ticker
    # ──────────────────────────────────────────────────

    async def get_ticker_raw(
        self,
        symbol: str,
        product_type: str = "USDT-FUTURES",
    ) -> tuple[Dict[str, Any], int]:
        """
        Fetch raw ticker JSON from Bitget V2.
        Returns (response_body, received_timestamp_ms).
        """
        _validate_product_type(product_type)
        url = f"{BITGET_BASE}/api/v2/mix/market/ticker"
        params = {"symbol": _normalize(symbol), "productType": product_type}
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params)
            received_ms = int(time.time() * 1000)  # Capture immediately after I/O
            resp.raise_for_status()
            
        body = resp.json()
        
        # Phase 4.5 Latency Tracking
        latency_ms = received_ms - int(time.time() * 1000 - resp.elapsed.total_seconds() * 1000) # approximate start
        LATENCY_HISTOGRAM.labels(type="ticker").observe(resp.elapsed.total_seconds() * 1000)
        
        logger.debug("[TICKER_RAW] %s → %s", symbol, body)
        return body, received_ms

    async def get_ticker(
        self,
        symbol: str,
        product_type: str = "USDT-FUTURES",
        retry_on_stale: bool = True,
    ) -> Dict[str, Any]:
        """
        Parsed ticker with robust timestamp handling, stale check, and auto-retry.
        """
        result = await self._fetch_ticker_once(symbol, product_type)
        
        # In LIVE mode, we DROP immediately if stale. No sleep/retry.
        trading_mode = os.getenv("TRADING_MODE", "paper").lower()
        
        if result.get("stale", False):
            if trading_mode == "live":
                REJECTS_TOTAL.labels(reason="STALE_DATA").inc()
                logger.warning("[STALE_REJECT] %s ticker stale (%dms). REJECTED in live mode.", symbol, result.get("age_ms", 0))
                return result

            # In dev/paper, we can afford a retry
            if retry_on_stale:
                logger.warning("[STALE_RETRY] %s ticker stale (%dms). Retrying in 1s...", symbol, result.get("age_ms", 0))
                await asyncio.sleep(1.0)
                result = await self._fetch_ticker_once(symbol, product_type)
                if result.get("stale", False):
                    logger.error("[STALE_RETRY_FAILED] %s ticker STILL stale (%dms) after retry.", symbol, result.get("age_ms", 0))
                else:
                    logger.info("[STALE_RETRY_SUCCESS] %s ticker fresh after retry.", symbol)
                
        return result

    async def _fetch_ticker_once(
        self,
        symbol: str,
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        body, received_ms = await self.get_ticker_raw(symbol, product_type)

        if body.get("code") != "00000":
            raise RuntimeError(f"Bitget ticker error: {body.get('msg', body)}")

        data_list = body.get("data", [])
        if not data_list:
            raise RuntimeError(f"Bitget ticker: empty data for {symbol}")

        raw = data_list[0]

        # Parse fields
        source_field = _PRICE_FIELDS.get(self.price_source, "lastPr")
        price = _float(raw.get(source_field, raw.get("lastPr", "0")))
        
        # ── Robust Timestamp Logic ───────────────────
        raw_ts = raw.get("ts")
        exchange_ts = _int(raw_ts)

        # 1. Fallback to received_ms if missing/zero
        if not exchange_ts:
            exchange_ts = received_ms
        
        # 2. Normalize seconds -> ms (if < 1 trillion, assume seconds)
        #    Bitget usually sends ms, but some APIs vary. 1e12 ms = year 2001.
        if exchange_ts < 1_000_000_000_000:
            exchange_ts *= 1000

        # 3. Calculate Age & Skew
        age_ms = received_ms - exchange_ts
        
        # 4. Clamp negative age (Clock Skew)
        if age_ms < 0:
            logger.warning(
                "[CLOCK_SKEW] exchange_ts ahead of local! ex=%d loc=%d diff=%dms. Clamping age to 0.",
                exchange_ts, received_ms, age_ms
            )
            age_ms = 0

        mark_price = _float(raw.get("markPrice", "0"))
        last_price = _float(raw.get("lastPr", "0"))
        index_price = _float(raw.get("indexPrice", "0"))

        # Stale check
        is_stale = age_ms > self.stale_threshold_ms

        if is_stale:
            logger.warning(
                "[STALE] %s ticker age=%dms (threshold=%dms) — DATA_STALE",
                symbol, age_ms, self.stale_threshold_ms,
            )
        else:
            logger.info(
                "[TICKER_PARSED] %s price=%.4f source=%s ex_ts=%d rx_ts=%d age=%dms",
                symbol, price, source_field, exchange_ts, received_ms, age_ms,
            )

        return {
            "symbol": _normalize(symbol),
            "price": price,
            "price_ts": exchange_ts,
            "received_ts": received_ms,   # New field
            "source": source_field,
            "source_field": source_field,
            "productType": product_type,  # always USDT-FUTURES
            "mark_price": mark_price,
            "last_price": last_price,
            "index_price": index_price,
            "bid1": _float(raw.get("bestBid", raw.get("bid1", "0"))),
            "ask1": _float(raw.get("bestAsk", raw.get("ask1", "0"))),
            "volume_24h": _float(raw.get("baseVolume", raw.get("vol24h", "0"))),
            "stale": is_stale,
            "age_ms": age_ms,
            "raw": raw,
        }

    def is_stale(self, ticker: Dict[str, Any]) -> bool:
        """Convenience: check stale flag on a parsed ticker dict."""
        return ticker.get("stale", False)

    # ──────────────────────────────────────────────────
    #  GET /api/v2/mix/market/candles
    # ──────────────────────────────────────────────────

    async def get_candles(
        self,
        symbol: str,
        tf: Optional[str] = None,
        limit: int = 200,
        product_type: str = "USDT-FUTURES",
        granularity: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch OHLCV candles from Bitget Mix V2.
        Returns list of {ts, open, high, low, close, volume} dicts,
        sorted ascending by timestamp.
        """
        clean = _normalize(symbol)
        actual_tf = tf or granularity or "5m"
        granularity_param = TF_MAP.get(actual_tf, TF_MAP.get(actual_tf.lower(), actual_tf))

        _validate_product_type(product_type)
        url = f"{BITGET_BASE}/api/v2/mix/market/candles"
        params = {
            "symbol": clean,
            "productType": product_type.lower(),
            "granularity": granularity_param,
            "limit": str(min(limit, 200)),
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            start_t = time.time()
            resp = await client.get(url, params=params)
            elapsed_ms = (time.time() - start_t) * 1000
            LATENCY_HISTOGRAM.labels(type="candles").observe(elapsed_ms)
            resp.raise_for_status()

        body = resp.json()
        if body.get("code") != "00000":
            raise RuntimeError(f"Bitget candles error: {body.get('msg', body)}")

        raw_rows = body.get("data", [])
        out: List[Dict[str, Any]] = []
        for row in raw_rows:
            out.append({
                "ts": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0.0,
            })
        out.sort(key=lambda c: c["ts"])

        logger.info("[CANDLES] %s %s — fetched %d candles", clean, actual_tf, len(out))
        return out

    async def get_candles_with_end_time(
        self,
        symbol: str,
        tf: Optional[str] = None,
        limit: int = 200,
        end_time: int = None,
        product_type: str = "USDT-FUTURES",
        granularity: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch OHLCV candles with endTime for pagination.
        endTime: Unix timestamp in milliseconds. Fetches candles BEFORE this time.
        Returns list of {ts, open, high, low, close, volume} dicts, sorted ascending.
        """
        clean = _normalize(symbol)
        actual_tf = tf or granularity or "4h"
        granularity_param = TF_MAP.get(actual_tf, TF_MAP.get(actual_tf.lower(), actual_tf))

        _validate_product_type(product_type)
        url = f"{BITGET_BASE}/api/v2/mix/market/candles"
        params = {
            "symbol": clean,
            "productType": product_type.lower(),
            "granularity": granularity_param,
            "limit": str(min(limit, 200)),
        }
        if end_time:
            params["endTime"] = str(end_time)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()

        body = resp.json()
        if body.get("code") != "00000":
            raise RuntimeError(f"Bitget candles error: {body.get('msg', body)}")

        raw_rows = body.get("data", [])
        out: List[Dict[str, Any]] = []
        for row in raw_rows:
            out.append({
                "ts": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0.0,
            })
        out.sort(key=lambda c: c["ts"])

        logger.info("[CANDLES_PAGINATED] %s %s endTime=%s — fetched %d candles", clean, actual_tf, end_time, len(out))
        return out

    # ──────────────────────────────────────────────────
    #  Paginated candle fetch (for HTF backfill)
    # ──────────────────────────────────────────────────

    async def get_candles_paginated(
        self,
        symbol: str,
        tf: Optional[str] = None,
        target: int = 500,
        product_type: str = "USDT-FUTURES",
        granularity: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Paginated fetch up to *target* candles using endTime pagination.
        Deduplicates by timestamp and returns ascending sort.
        """
        all_candles: List[Dict[str, Any]] = []
        pages = (target + 199) // 200
        end_time = None

        for page in range(pages):
            if end_time:
                batch = await self.get_candles_with_end_time(symbol, tf, 200, end_time, product_type, granularity)
            else:
                batch = await self.get_candles(symbol, tf, 200, product_type, granularity)
            
            if not batch:
                break
            all_candles.extend(batch)
            if len(batch) < 200:
                break
            
            # Get oldest timestamp for next page
            oldest_ts = min(c.get("ts", 0) for c in batch)
            if oldest_ts > 0:
                end_time = oldest_ts - 1  # Go back further
            else:
                break
            
            await asyncio.sleep(0.3)  # Rate limit

        # Dedup
        seen = set()
        unique = []
        for c in all_candles:
            if c["ts"] not in seen:
                seen.add(c["ts"])
                unique.append(c)
        unique.sort(key=lambda x: x["ts"])
        
        actual_tf = tf or granularity or "4h"
        logger.info("[CANDLES_PAGINATED] %s %s — total=%d (target=%d)", 
                    _normalize(symbol), actual_tf, len(unique), target)
        return unique


# ──────────────────────────────────────────────────
#  Private helpers
# ──────────────────────────────────────────────────

def _normalize(symbol: str) -> str:
    return symbol.replace("/", "").replace(":", "").replace("-", "").upper()

def _float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
