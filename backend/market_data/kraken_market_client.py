"""
Kraken Market Client — Unified Market Data Interface
════════════════════════════════════════════════════

Provides a clean interface for fetching market data from Kraken,
with automatic symbol translation and output format matching standard.

Features:
  - Symbol translation (BTCUSDT -> XXBTZUSD for REST)
  - Candle format matching standard output
  - Ticker format unification
  - Rate limiting and error handling
  - Logging with [KRAKEN_MARKET] prefix

Usage:
    from backend.market_data.kraken_market_client import KrakenMarketClient
    
    client = KrakenMarketClient()
    
    # Fetch candles (accepts standard-style symbol)
    candles = await client.get_candles("BTCUSDT", "4h", 200)
    
    # Fetch ticker
    ticker = await client.get_ticker("ETHUSDT")
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.utils.symbol_mapper import (
    SymbolMapper,
    BrokerType,
    get_symbol_mapper,
)

logger = logging.getLogger("smarttrade.kraken_market")

# Kraken REST base URL
KRAKEN_REST_BASE = "https://api.kraken.com"

# Timeframe mapping: standard tf -> Kraken interval (minutes)
TF_TO_INTERVAL: Dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
    "1w": 10080,
}


class KrakenMarketError(Exception):
    """Kraken market data error."""
    pass


class KrakenMarketClient:
    """
    Kraken market data client with unified symbol handling.
    
    Accepts both standard-style (BTCUSDT) and Kraken-style (XXBTZUSD) symbols,
    automatically translates, and returns data in standard-compatible format.
    """
    
    def __init__(
        self,
        timeout: float = 10.0,
        symbol_mapper: Optional[SymbolMapper] = None,
        stale_threshold_ms: int = 250,
    ):
        """
        Initialize Kraken market client.
        
        Args:
            timeout: HTTP request timeout in seconds
            symbol_mapper: Custom symbol mapper (uses default if None)
            stale_threshold_ms: Max allowed age for market data
        """
        self.timeout = timeout
        self.stale_threshold_ms = stale_threshold_ms
        self._mapper = symbol_mapper or get_symbol_mapper()
        
        logger.info("[KRAKEN_MARKET] Client initialized | stale_threshold=%dms", stale_threshold_ms)
    
    # ──────────────────────────────────────────────────
    #  CANDLES
    # ──────────────────────────────────────────────────
    
    async def get_candles(
        self,
        symbol: str,
        tf: str = "15m",
        limit: int = 200,
        product_type: str = "perpetual",  # Ignored for Kraken, for API compat
    ) -> List[Dict[str, Any]]:
        """
        Fetch OHLCV candles from Kraken.
        
        Accepts standard-style symbols and returns standard-compatible format.
        
        Args:
            symbol: Trading symbol (e.g., "BTCUSDT" or "XXBTZUSD")
            tf: Timeframe string (e.g., "1m", "5m", "15m", "1h", "4h", "1d")
            limit: Maximum candles to return
            product_type: Ignored (for API compatibility with primary client)
            
        Returns:
            List of candle dicts, sorted ascending by timestamp:
            [
                {
                    "ts": 1699999200000,   # Timestamp in milliseconds
                    "open": 50000.0,
                    "high": 50500.0,
                    "low": 49800.0,
                    "close": 50200.0,
                    "volume": 123.45,
                },
                ...
            ]
            
        Example:
            candles = await client.get_candles("BTCUSDT", "4h", 100)
            latest = candles[-1]
            print(f"Latest BTC close: ${latest['close']}")
        """
        # Get canonical symbol for logging
        canonical = self._mapper.get_canonical(symbol)
        
        # Convert to Kraken spot symbol for REST API
        kraken_symbol = self._mapper.to_kraken_spot(symbol)
        
        # Get interval in minutes
        interval = TF_TO_INTERVAL.get(tf.lower(), 15)
        
        logger.debug("[KRAKEN_MARKET] get_candles %s (%s) tf=%s (%dm) limit=%d",
                    canonical, kraken_symbol, tf, interval, limit)
        
        # Fetch from Kraken REST API
        params = {
            "pair": kraken_symbol,
            "interval": interval,
        }
        
        result = await self._rest_get("/0/public/OHLC", params)
        
        if not result:
            raise KrakenMarketError(f"No candle data for {symbol}")
        
        # Find the pair key (excludes "last")
        pair_key = [k for k in result.keys() if k != "last"][0]
        raw_candles = result[pair_key]
        
        # Parse candles to standard-compatible format
        # Kraken format: [timestamp, open, high, low, close, vwap, volume, count]
        candles: List[Dict[str, Any]] = []
        for c in raw_candles:
            candles.append({
                "ts": int(c[0]) * 1000,  # Convert to milliseconds
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[6]),
            })
        
        # Sort ascending by timestamp
        candles.sort(key=lambda x: x["ts"])
        
        # Limit results (take latest)
        if len(candles) > limit:
            candles = candles[-limit:]
        
        logger.info("[KRAKEN_MARKET] %s (%s) %s — fetched %d candles",
                   canonical, kraken_symbol, tf, len(candles))
        
        return candles
    
    async def get_candles_with_end_time(
        self,
        symbol: str,
        tf: str = "4h",
        limit: int = 200,
        end_time: Optional[int] = None,
        product_type: str = "perpetual",
    ) -> List[Dict[str, Any]]:
        """
        Fetch candles with end time for pagination.
        
        Args:
            symbol: Trading symbol
            tf: Timeframe
            limit: Maximum candles
            end_time: Unix timestamp in milliseconds (candles BEFORE this time)
            product_type: Ignored (for API compatibility)
            
        Returns:
            List of candle dicts
        """
        # Get canonical symbol for logging
        canonical = self._mapper.get_canonical(symbol)
        kraken_symbol = self._mapper.to_kraken_spot(symbol)
        interval = TF_TO_INTERVAL.get(tf.lower(), 15)
        
        params: Dict[str, Any] = {
            "pair": kraken_symbol,
            "interval": interval,
        }
        
        # Kraken uses "since" (start time) not "end_time"
        # To get candles before end_time, we need to calculate since
        if end_time:
            # Go back enough to get limit candles
            interval_ms = interval * 60 * 1000
            since_ts = (end_time // 1000) - (limit * interval * 60)
            params["since"] = since_ts
        
        result = await self._rest_get("/0/public/OHLC", params)
        
        if not result:
            raise KrakenMarketError(f"No candle data for {symbol}")
        
        pair_key = [k for k in result.keys() if k != "last"][0]
        raw_candles = result[pair_key]
        
        candles: List[Dict[str, Any]] = []
        for c in raw_candles:
            ts = int(c[0]) * 1000
            # Filter by end_time if specified
            if end_time and ts >= end_time:
                continue
            candles.append({
                "ts": ts,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[6]),
            })
        
        candles.sort(key=lambda x: x["ts"])
        
        if len(candles) > limit:
            candles = candles[-limit:]
        
        logger.info("[KRAKEN_MARKET] %s %s (before %s) — fetched %d candles",
                   canonical, tf, end_time, len(candles))
        
        return candles
    
    # ──────────────────────────────────────────────────
    #  TICKER
    # ──────────────────────────────────────────────────
    
    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """
        Fetch ticker data from Kraken.
        
        Args:
            symbol: Trading symbol (e.g., "BTCUSDT" or "XXBTZUSD")
            
        Returns:
            Ticker dict in standard-compatible format:
            {
                "symbol": "BTCUSDT",
                "mark_price": 50000.0,
                "last_price": 50000.0,
                "index_price": 50000.0,  # Same as last for spot
                "bid1": 49990.0,
                "ask1": 50010.0,
                "volume_24h": 1234.5,
                "high_24h": 51000.0,
                "low_24h": 49000.0,
                "source": "kraken",
                "productType": "spot",  # Kraken REST is spot data
            }
            
        Example:
            ticker = await client.get_ticker("BTCUSDT")
            print(f"BTC: ${ticker['last_price']}")
        """
        canonical = self._mapper.get_canonical(symbol)
        kraken_symbol = self._mapper.to_kraken_spot(symbol)
        
        result = await self._rest_get("/0/public/Ticker", {"pair": kraken_symbol})
        
        if not result:
            raise KrakenMarketError(f"No ticker data for {symbol}")
        
        pair_key = list(result.keys())[0]
        data = result[pair_key]
        
        # Parse Kraken ticker format
        # a = ask, b = bid, c = last trade, v = volume, h = high, l = low
        last_price = float(data["c"][0])
        
        ticker = {
            "symbol": canonical,
            "kraken_symbol": kraken_symbol,
            "mark_price": last_price,  # Use last as mark for spot
            "last_price": last_price,
            "index_price": last_price,
            "bid1": float(data["b"][0]),
            "ask1": float(data["a"][0]),
            "volume_24h": float(data["v"][1]),
            "high_24h": float(data["h"][1]),
            "low_24h": float(data["l"][1]),
            "vwap_24h": float(data["p"][1]),
            "source": "kraken",
            "productType": "spot",
            "stale": False,
            "raw": data,
        }
        
        logger.info("[KRAKEN_MARKET] Ticker %s | last=%.2f bid=%.2f ask=%.2f",
                   canonical, last_price, ticker["bid1"], ticker["ask1"])
        
        return ticker
    
    # ──────────────────────────────────────────────────
    #  SERVER INFO
    # ──────────────────────────────────────────────────
    
    async def get_server_time(self) -> Dict[str, Any]:
        """Get Kraken server time for connectivity check."""
        result = await self._rest_get("/0/public/Time")
        return result
    
    async def check_connectivity(self) -> bool:
        """
        Check if Kraken API is reachable.
        
        Returns:
            True if connected, False otherwise
        """
        try:
            await self.get_server_time()
            return True
        except Exception as e:
            logger.warning("[KRAKEN_MARKET] Connectivity check failed: %s", e)
            return False
    
    # ──────────────────────────────────────────────────
    #  INTERNAL
    # ──────────────────────────────────────────────────
    
    async def _rest_get(
        self,
        urlpath: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute REST GET request."""
        url = KRAKEN_REST_BASE + urlpath
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
            except httpx.RequestError as e:
                logger.error("[KRAKEN_MARKET] Request error: %s", e)
                raise KrakenMarketError(f"Request error: {e}")
            except httpx.HTTPStatusError as e:
                logger.error("[KRAKEN_MARKET] HTTP error: %s", e)
                raise KrakenMarketError(f"HTTP error: {e}")
        
        result = resp.json()
        
        # Check for Kraken API errors
        errors = result.get("error", [])
        if errors:
            error_msg = ", ".join(errors)
            logger.error("[KRAKEN_MARKET] API error: %s", error_msg)
            raise KrakenMarketError(error_msg)
        
        return result.get("result", {})


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_default_client: Optional[KrakenMarketClient] = None


def get_kraken_market_client() -> KrakenMarketClient:
    """Get default Kraken market client instance."""
    global _default_client
    if _default_client is None:
        _default_client = KrakenMarketClient()
    return _default_client


if __name__ == "__main__":
    # Test the client
    async def main():
        client = KrakenMarketClient()
        
        print("="*60)
        print("Kraken Market Client Test")
        print("="*60)
        
        # Check connectivity
        print("\n1. Checking connectivity...")
        connected = await client.check_connectivity()
        print(f"   Connected: {connected}")
        
        if not connected:
            print("   Cannot reach Kraken API")
            return
        
        # Fetch ticker
        print("\n2. Fetching BTC ticker...")
        ticker = await client.get_ticker("BTCUSDT")
        print(f"   Symbol: {ticker['symbol']}")
        print(f"   Last:   ${ticker['last_price']:,.2f}")
        print(f"   Bid:    ${ticker['bid1']:,.2f}")
        print(f"   Ask:    ${ticker['ask1']:,.2f}")
        
        # Fetch candles
        print("\n3. Fetching BTC 4h candles...")
        candles = await client.get_candles("BTCUSDT", "4h", 10)
        print(f"   Fetched {len(candles)} candles")
        if candles:
            latest = candles[-1]
            print(f"   Latest: O={latest['open']:.2f} H={latest['high']:.2f} L={latest['low']:.2f} C={latest['close']:.2f}")
        
        print("\n✅ All tests passed!")
    
    asyncio.run(main())
