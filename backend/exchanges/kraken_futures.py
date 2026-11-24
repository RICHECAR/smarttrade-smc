"""
Kraken Futures Exchange Module — CORRECT Implementation
═════════════════════════════════════════════════════════

⚠️  CRITICAL: This module uses ONLY Kraken FUTURES REST API!
    
    ✅ CORRECT: https://futures.kraken.com/derivatives/api/v3/...
    ❌ WRONG:   https://api.kraken.com/0/private/...  (This is SPOT!)

Features:
  - REST API for account/positions/orders (futures.kraken.com)
  - Kraken Futures signature algorithm (SHA256 + HMAC-SHA512)
  - No WebSocket token from Spot API
  - Self-check on startup to validate Futures API keys

Author: SMC Trading Bot Framework
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("smarttrade.exchanges.kraken")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — KRAKEN FUTURES ONLY!
# ══════════════════════════════════════════════════════════════════════════════

# ✅ CORRECT: Kraken FUTURES REST base URL
KRAKEN_FUTURES_REST_BASE = "https://futures.kraken.com"
KRAKEN_FUTURES_API_PATH = "/derivatives/api/v3"   # URL path
KRAKEN_FUTURES_SIGN_PATH = "/api/v3"              # Path for SIGNATURE (no /derivatives!)

# ✅ CORRECT: Kraken FUTURES WebSocket
KRAKEN_FUTURES_WS = "wss://futures.kraken.com/ws/v1"

# ❌ DO NOT USE: Kraken SPOT (these are here as a warning)
_KRAKEN_SPOT_REST = "https://api.kraken.com"  # NEVER USE FOR FUTURES!
_KRAKEN_SPOT_WS = "wss://ws.kraken.com/v2"    # NEVER USE FOR FUTURES!

DEFAULT_TIMEOUT = 15.0


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class KrakenFuturesBalance:
    """Kraken Futures account balance."""
    available: float = 0.0
    total: float = 0.0
    margin_used: float = 0.0
    unrealized_pnl: float = 0.0
    currency: str = "USD"
    timestamp: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": round(self.available, 2),
            "total": round(self.total, 2),
            "margin_used": round(self.margin_used, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "currency": self.currency,
            "broker": "kraken",
            "timestamp": self.timestamp or datetime.now(timezone.utc).isoformat(),
        }


@dataclass
class KrakenFuturesPosition:
    """Kraken Futures open position."""
    symbol: str
    side: str  # "long" or "short"
    size: float
    entry_price: float
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0
    leverage: float = 1.0
    liquidation_price: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "size": round(self.size, 6),
            "entry_price": round(self.entry_price, 2),
            "mark_price": round(self.mark_price, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "leverage": round(self.leverage, 1),
            "liquidation_price": round(self.liquidation_price, 2),
            "broker": "kraken",
        }


@dataclass
class KrakenOrderResult:
    """Result of a Kraken Futures order execution."""
    success: bool
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    symbol: Optional[str] = None
    side: Optional[str] = None
    size: Optional[float] = None
    price: Optional[float] = None
    filled_size: Optional[float] = None
    filled_price: Optional[float] = None
    error: Optional[str] = None
    raw_response: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "broker": "kraken",
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "price": self.price,
            "filled_size": self.filled_size,
            "filled_price": self.filled_price,
            "error": self.error,
            "timestamp": self.timestamp.isoformat(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# SYMBOL MAPPING: Standard → Kraken Futures
# ══════════════════════════════════════════════════════════════════════════════

# Kraken Futures uses different symbol format
SYMBOL_MAP_TO_KRAKEN = {
    "BTCUSDT": "PF_XBTUSD",
    "ETHUSDT": "PF_ETHUSD",
    "SOLUSDT": "PF_SOLUSD",
    "XRPUSDT": "PF_XRPUSD",
    "DOGEUSDT": "PF_DOGEUSD",
    "ADAUSDT": "PF_ADAUSD",
    "LINKUSDT": "PF_LINKUSD",
    "AVAXUSDT": "PF_AVAXUSD",
    "MATICUSDT": "PF_MATICUSD",
    "BNBUSDT": "PF_BNBUSD",
    # Add more as needed
}

SYMBOL_MAP_FROM_KRAKEN = {v: k for k, v in SYMBOL_MAP_TO_KRAKEN.items()}


def to_kraken_symbol(symbol: str) -> Optional[str]:
    """Convert standard symbol to Kraken Futures format."""
    normalized = symbol.upper().replace("/", "").replace(":", "").replace("-", "")
    return SYMBOL_MAP_TO_KRAKEN.get(normalized)


def from_kraken_symbol(symbol: str) -> Optional[str]:
    """Convert Kraken Futures symbol to standard format."""
    return SYMBOL_MAP_FROM_KRAKEN.get(symbol.upper(), symbol)


# ══════════════════════════════════════════════════════════════════════════════
# KRAKEN FUTURES SIGNATURE
# ══════════════════════════════════════════════════════════════════════════════

def sign_kraken_futures_request(
    api_secret: str,
    endpoint: str,
    nonce: str,
    post_data: str = ""
) -> str:
    """
    Generate signature for Kraken FUTURES REST API.
    
    ⚠️  This is DIFFERENT from Kraken Spot signature!
    
    Kraken Futures Signature Algorithm:
    1. message = postData + nonce + endpoint
    2. hash = SHA256(message)
    3. signature = HMAC-SHA512(hash, base64_decode(secret))
    4. return base64_encode(signature)
    
    IMPORTANT:
    - endpoint must be /api/v3/... (NOT /derivatives/api/v3/...)
    - nonce is millisecond timestamp as string
    
    Args:
        api_secret: Base64-encoded API secret from Kraken FUTURES
        endpoint: API endpoint for signing (e.g., /api/v3/accounts)
        nonce: Millisecond timestamp as string
        post_data: URL-encoded POST data (empty for GET)
        
    Returns:
        Base64-encoded signature string
    """
    # Step 1: Build message = postData + nonce + endpoint
    message = f"{post_data}{nonce}{endpoint}"
    message_bytes = message.encode("utf-8")
    
    logger.debug("[KRAKEN_SIGN] Endpoint: %s | Nonce: %s", endpoint, nonce)
    
    # Step 2: SHA256 hash
    sha256_hash = hashlib.sha256(message_bytes).digest()
    
    # Step 3: Decode secret from base64
    try:
        secret_bytes = base64.b64decode(api_secret)
    except Exception as e:
        logger.error("[KRAKEN_SIGN] Failed to decode API secret: %s", e)
        raise ValueError(f"Invalid API secret format (not base64): {e}")
    
    # Step 4: HMAC-SHA512
    hmac_signature = hmac.new(secret_bytes, sha256_hash, hashlib.sha512).digest()
    
    # Step 5: Base64 encode signature
    signature = base64.b64encode(hmac_signature).decode("utf-8")
    
    return signature


def build_kraken_futures_headers(
    api_key: str,
    api_secret: str,
    endpoint: str,
    post_data: str = ""
) -> Dict[str, str]:
    """
    Build authenticated headers for Kraken FUTURES API.
    
    Headers:
    - APIKey: Public API key
    - Nonce: Millisecond timestamp
    - Authent: HMAC-SHA512 signature
    """
    nonce = str(int(time.time() * 1000))
    signature = sign_kraken_futures_request(api_secret, endpoint, nonce, post_data)
    
    return {
        "APIKey": api_key,
        "Nonce": nonce,
        "Authent": signature,
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "SMC-Trading-Bot/1.0",
    }


# ══════════════════════════════════════════════════════════════════════════════
# KRAKEN FUTURES CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class KrakenFuturesClient:
    """
    Async REST client for Kraken FUTURES.
    
    ⚠️  Uses ONLY futures.kraken.com — NO Spot API calls!
    
    Usage:
        client = KrakenFuturesClient()
        
        # Test connection (validates Futures API keys)
        result = await client.test_connection()
        if not result["success"]:
            print(f"Error: {result['error']}")
        
        # Get balance
        balance = await client.get_balance()
        
        # Get positions
        positions = await client.get_positions()
        
        # Place order
        order = await client.place_market_order("PF_XBTUSD", "buy", 0.001)
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        use_demo: Optional[bool] = None,
        base_url: Optional[str] = None,
    ):
        """
        Initialize Kraken Futures client with strict LIVE/DEMO isolation.
        """
        from backend.kraken_futures_rest_v2 import resolve_kraken_environment
        env_cfg = resolve_kraken_environment(
            api_key=api_key,
            api_secret=api_secret,
            use_demo=use_demo,
            base_url=base_url,
        )
        self.use_demo: bool = env_cfg["use_demo"]
        self.environment: str = env_cfg["environment"]
        self.api_key = env_cfg["api_key"]
        self.api_secret = env_cfg["api_secret"]
        self.timeout = timeout
        self.base_url = env_cfg["rest_url"]
        self.ws_url = env_cfg["ws_url"]
        
        # Cache
        self._balance_cache: Optional[KrakenFuturesBalance] = None
        self._balance_cache_time: float = 0
        self._positions_cache: List[KrakenFuturesPosition] = []
        self._positions_cache_time: float = 0
        
        # Status tracking
        self._is_connected: bool = False
        self._last_error: Optional[str] = None
        
        if self.api_key:
            logger.info(
                "[KRAKEN] Client initialized | env=%s | key=%s... | url=%s (FUTURES)",
                self.environment,
                self.api_key[:16] if len(self.api_key) > 16 else self.api_key, 
                self.base_url
            )
        else:
            logger.warning("[KRAKEN] No API key configured! (env=%s)", self.environment)
    
    # ══════════════════════════════════════════════════════════════════════════
    # HTTP REQUEST
    # ══════════════════════════════════════════════════════════════════════════
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        authenticated: bool = True
    ) -> Dict[str, Any]:
        """
        Execute HTTP request to Kraken FUTURES API.
        
        Args:
            method: HTTP method (GET/POST)
            endpoint: API endpoint without /derivatives (e.g., /accounts)
            params: Query parameters for GET
            data: Body data for POST
            authenticated: Whether to sign the request
            
        Returns:
            Parsed JSON response
        """
        from backend.kraken_futures_rest_v2 import validate_kraken_urls_for_mode
        validate_kraken_urls_for_mode(self.use_demo, self.base_url)

        # Build URLs:
        # - URL uses /derivatives/api/v3
        # - Signature uses /api/v3
        url_path = f"{KRAKEN_FUTURES_API_PATH}{endpoint}"
        sign_path = f"{KRAKEN_FUTURES_SIGN_PATH}{endpoint}"
        full_url = f"{self.base_url}{url_path}"
        
        # Build POST data string
        post_data = ""
        if data:
            post_data = urllib.parse.urlencode(data)
        
        # Build headers
        if authenticated:
            if not self.api_key or not self.api_secret:
                raise ValueError("API credentials not configured")
            headers = build_kraken_futures_headers(
                self.api_key, self.api_secret, sign_path, post_data
            )
        else:
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        
        logger.debug("[KRAKEN] %s %s", method.upper(), url_path)
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                if method.upper() == "GET":
                    response = await client.get(full_url, headers=headers, params=params)
                else:
                    response = await client.post(full_url, headers=headers, content=post_data)
                
            except httpx.TimeoutException:
                self._is_connected = False
                self._last_error = f"Request timeout: {endpoint}"
                logger.error("[KRAKEN] Request timeout: %s", endpoint)
                raise ValueError(self._last_error)
            except httpx.RequestError as e:
                self._is_connected = False
                self._last_error = f"Network error: {e}"
                logger.error("[KRAKEN] Network error: %s", e)
                raise ValueError(self._last_error)
        
        # Parse response
        try:
            result = response.json()
        except Exception as e:
            logger.error("[KRAKEN] Invalid JSON response: %s", response.text[:200])
            raise ValueError(f"Invalid JSON response: {e}")
        
        # Check for errors
        if result.get("result") == "error":
            error_msg = result.get("error", "Unknown error")
            self._is_connected = False
            self._last_error = error_msg
            logger.error("[KRAKEN] API error: %s", error_msg)
            
            # Provide helpful messages for common errors
            if "authenticationError" in error_msg.lower():
                raise ValueError(
                    f"Kraken authentication failed: {error_msg} — "
                    "Verify your API keys are from futures.kraken.com (NOT kraken.com/spot)"
                )
            raise ValueError(f"Kraken API error: {error_msg}")
        
        if response.status_code != 200:
            logger.error("[KRAKEN] HTTP %d: %s", response.status_code, response.text[:200])
            raise ValueError(f"HTTP error {response.status_code}")
        
        self._is_connected = True
        return result
    
    # ══════════════════════════════════════════════════════════════════════════
    # ACCOUNT METHODS
    # ══════════════════════════════════════════════════════════════════════════
    
    async def get_accounts_raw(self) -> Dict[str, Any]:
        """
        Get raw accounts data from Kraken Futures.
        
        Endpoint: GET /derivatives/api/v3/accounts
        """
        return await self._request("GET", "/accounts")
    
    async def get_balance(self, force_refresh: bool = False) -> KrakenFuturesBalance:
        """
        Get Kraken Futures account balance.
        
        Returns:
            KrakenFuturesBalance with available, total, margin_used, pnl
        """
        # Check cache (30s TTL)
        cache_age = time.time() - self._balance_cache_time
        if not force_refresh and self._balance_cache and cache_age < 30:
            return self._balance_cache
        
        logger.info("[KRAKEN] Fetching balance...")
        data = await self.get_accounts_raw()
        
        accounts = data.get("accounts", {})
        server_time = data.get("serverTime", "")
        
        # Try flex account first (multi-collateral)
        flex = accounts.get("flex", {})
        if flex:
            balance = KrakenFuturesBalance(
                available=float(flex.get("availableMargin", 0)),
                total=float(flex.get("portfolioValue", 0)),
                margin_used=float(flex.get("initialMargin", 0)),
                unrealized_pnl=float(flex.get("pnl", 0)),
                timestamp=server_time
            )
        else:
            # Fallback to cash account
            cash = accounts.get("cash", {})
            balances = cash.get("balances", {})
            usd = float(balances.get("usd", balances.get("USD", 0)))
            
            balance = KrakenFuturesBalance(
                available=usd,
                total=usd,
                margin_used=0.0,
                unrealized_pnl=0.0,
                timestamp=server_time
            )
        
        logger.info(
            "[KRAKEN] ✓ Balance: available=$%.2f | total=$%.2f | pnl=$%.4f",
            balance.available, balance.total, balance.unrealized_pnl
        )
        
        self._balance_cache = balance
        self._balance_cache_time = time.time()
        
        return balance
    
    async def get_balance_dict(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Get balance as dictionary."""
        balance = await self.get_balance(force_refresh)
        return balance.to_dict()
    
    # ══════════════════════════════════════════════════════════════════════════
    # POSITION METHODS
    # ══════════════════════════════════════════════════════════════════════════
    
    async def get_positions_raw(self) -> Dict[str, Any]:
        """
        Get raw open positions from Kraken Futures.
        
        Endpoint: GET /derivatives/api/v3/openpositions
        """
        return await self._request("GET", "/openpositions")
    
    async def get_positions(self, force_refresh: bool = False) -> List[KrakenFuturesPosition]:
        """
        Get Kraken Futures open positions.
        
        Returns:
            List of KrakenFuturesPosition objects
        """
        # Check cache (10s TTL)
        cache_age = time.time() - self._positions_cache_time
        if not force_refresh and self._positions_cache and cache_age < 10:
            return self._positions_cache
        
        logger.info("[KRAKEN] Fetching positions...")
        data = await self.get_positions_raw()
        
        raw_positions = data.get("openPositions", [])
        positions: List[KrakenFuturesPosition] = []
        
        for pos in raw_positions:
            try:
                symbol = pos.get("symbol", "")
                size = float(pos.get("size", 0))
                side = pos.get("side", "long").lower()
                
                # Kraken may return negative size for shorts
                if size < 0:
                    size = abs(size)
                    side = "short"
                
                position = KrakenFuturesPosition(
                    symbol=symbol,
                    side=side,
                    size=size,
                    entry_price=float(pos.get("price", pos.get("entryPrice", 0))),
                    mark_price=float(pos.get("markPrice", 0)),
                    unrealized_pnl=float(pos.get("unrealizedFunding", 0)) + float(pos.get("pnl", 0)),
                    leverage=float(pos.get("effectiveLeverage", 1)),
                    liquidation_price=float(pos.get("liquidationThreshold", 0)),
                )
                positions.append(position)
                
            except Exception as e:
                logger.warning("[KRAKEN] Failed to parse position: %s", e)
        
        logger.info("[KRAKEN] ✓ %d open positions", len(positions))
        
        self._positions_cache = positions
        self._positions_cache_time = time.time()
        
        return positions
    
    async def get_positions_dict(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Get positions as list of dictionaries."""
        positions = await self.get_positions(force_refresh)
        return [p.to_dict() for p in positions]
    
    # ══════════════════════════════════════════════════════════════════════════
    # ORDER METHODS
    # ══════════════════════════════════════════════════════════════════════════
    
    async def place_market_order(
        self,
        symbol: str,
        side: str,
        size: float,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> KrakenOrderResult:
        """
        Place a market order on Kraken Futures.
        
        Args:
            symbol: Kraken futures symbol (e.g., PF_XBTUSD)
            side: "buy" or "sell"
            size: Order quantity
            reduce_only: Whether to reduce only
            client_order_id: Optional client order ID
            
        Returns:
            KrakenOrderResult with order details
        """
        endpoint = "/sendorder"

        # Format size according to instrument quantity precision (linear multi-collateral)
        try:
            from backend.kraken_contracts import get_contract_specs
            specs = get_contract_specs(symbol.upper())
            qty_precision = specs.get("qty_precision", 0)
        except ImportError:
            qty_precision = 0

        if qty_precision > 0:
            formatted_size = f"{float(size):.{qty_precision}f}"
        else:
            try:
                from backend.risk.size_calculator import ensure_kraken_contracts
                norm = ensure_kraken_contracts(size, symbol.upper())
                formatted_size = str(int(round(float(norm))))
            except ImportError:
                formatted_size = str(max(1, int(round(float(size)))))

        data = {
            "orderType": "mkt",
            "symbol": symbol.upper(),
            "side": side.lower(),
            "size": formatted_size,
        }
        
        if reduce_only:
            data["reduceOnly"] = True
        if client_order_id:
            data["cliOrdId"] = client_order_id
        
        logger.info(
            "[KRAKEN] ORDER_START | MARKET %s %s size=%s units (raw=%.6f)",
            symbol, side.upper(), formatted_size, size
        )
        
        try:
            result = await self._request("POST", endpoint, data=data)
            
            send_status = result.get("sendStatus", {})
            order_id = send_status.get("order_id")
            status = send_status.get("status", "").lower()
            
            if status in ("placed", "filled", "partiallyFilled"):
                order_result = KrakenOrderResult(
                    success=True,
                    order_id=order_id,
                    client_order_id=send_status.get("cliOrdId"),
                    symbol=symbol,
                    side=side.lower(),
                    size=size,
                    filled_size=float(send_status.get("filledSize", 0)),
                    raw_response=result,
                )
                logger.info("[KRAKEN] ORDER_SUCCESS | order_id=%s", order_id)
                return order_result
            else:
                error_msg = send_status.get("reason", status)
                logger.error("[KRAKEN] ORDER_REJECTED | %s", error_msg)
                return KrakenOrderResult(
                    success=False,
                    symbol=symbol,
                    side=side.lower(),
                    size=size,
                    error=error_msg,
                    raw_response=result,
                )
                
        except Exception as e:
            logger.error("[KRAKEN] ORDER_FAILED | %s", e)
            return KrakenOrderResult(
                success=False,
                symbol=symbol,
                side=side.lower(),
                size=size,
                error=str(e),
            )
    
    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        size: float,
        price: float,
        reduce_only: bool = False,
        post_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> KrakenOrderResult:
        """Place a limit order on Kraken Futures."""
        endpoint = "/sendorder"

        # Format size according to instrument quantity precision (linear multi-collateral)
        try:
            from backend.kraken_contracts import get_contract_specs
            specs = get_contract_specs(symbol.upper())
            qty_precision = specs.get("qty_precision", 0)
        except ImportError:
            qty_precision = 0

        if qty_precision > 0:
            formatted_size = f"{float(size):.{qty_precision}f}"
        else:
            try:
                from backend.risk.size_calculator import ensure_kraken_contracts
                norm = ensure_kraken_contracts(size, symbol.upper(), entry_price=price)
                formatted_size = str(int(round(float(norm))))
            except ImportError:
                formatted_size = str(max(1, int(round(float(size)))))

        data = {
            "orderType": "lmt",
            "symbol": symbol.upper(),
            "side": side.lower(),
            "size": formatted_size,
            "limitPrice": str(price),
        }
        
        if reduce_only:
            data["reduceOnly"] = True
        if post_only:
            data["postOnly"] = True
        if client_order_id:
            data["cliOrdId"] = client_order_id
        
        logger.info(
            "[KRAKEN] ORDER_START | LIMIT %s %s size=%s units (raw=%.6f) price=%s",
            symbol, side.upper(), formatted_size, size, price
        )
        
        try:
            result = await self._request("POST", endpoint, data=data)
            
            send_status = result.get("sendStatus", {})
            order_id = send_status.get("order_id")
            status = send_status.get("status", "").lower()
            
            if status in ("placed", "filled", "partiallyFilled"):
                return KrakenOrderResult(
                    success=True,
                    order_id=order_id,
                    client_order_id=send_status.get("cliOrdId"),
                    symbol=symbol,
                    side=side.lower(),
                    size=size,
                    price=price,
                    raw_response=result,
                )
            else:
                return KrakenOrderResult(
                    success=False,
                    symbol=symbol,
                    side=side.lower(),
                    size=size,
                    price=price,
                    error=send_status.get("reason", status),
                    raw_response=result,
                )
                
        except Exception as e:
            logger.error("[KRAKEN] ORDER_FAILED | %s", e)
            return KrakenOrderResult(
                success=False,
                symbol=symbol,
                side=side.lower(),
                size=size,
                price=price,
                error=str(e),
            )
    
    async def place_stop_order(
        self,
        symbol: str,
        side: str,
        size: float,
        stop_price: float,
        limit_price: Optional[float] = None,
        reduce_only: bool = True,
    ) -> KrakenOrderResult:
        """Place a stop order (for SL) on Kraken Futures."""
        endpoint = "/sendorder"
        
        order_type = "stp" if not limit_price else "stop_lmt"
        
        data = {
            "orderType": order_type,
            "symbol": symbol.upper(),
            "side": side.lower(),
            "size": str(size),
            "stopPrice": str(stop_price),
        }
        
        if limit_price:
            data["limitPrice"] = str(limit_price)
        if reduce_only:
            data["reduceOnly"] = True
        
        logger.info(
            "[KRAKEN] ORDER_START | STOP %s %s size=%s stop=%s",
            symbol, side.upper(), size, stop_price
        )
        
        try:
            result = await self._request("POST", endpoint, data=data)
            send_status = result.get("sendStatus", {})
            
            return KrakenOrderResult(
                success=send_status.get("status", "").lower() in ("placed", "filled"),
                order_id=send_status.get("order_id"),
                symbol=symbol,
                side=side.lower(),
                size=size,
                price=stop_price,
                error=send_status.get("reason") if send_status.get("status") not in ("placed", "filled") else None,
                raw_response=result,
            )
        except Exception as e:
            return KrakenOrderResult(
                success=False,
                symbol=symbol,
                side=side.lower(),
                size=size,
                error=str(e),
            )
    
    async def place_take_profit_order(
        self,
        symbol: str,
        side: str,
        size: float,
        tp_price: float,
        reduce_only: bool = True,
    ) -> KrakenOrderResult:
        """Place a take profit order on Kraken Futures."""
        endpoint = "/sendorder"
        
        data = {
            "orderType": "take_profit",
            "symbol": symbol.upper(),
            "side": side.lower(),
            "size": str(size),
            "stopPrice": str(tp_price),  # Kraken uses stopPrice for TP trigger
        }
        
        if reduce_only:
            data["reduceOnly"] = True
        
        logger.info(
            "[KRAKEN] ORDER_START | TP %s %s size=%s tp=%s",
            symbol, side.upper(), size, tp_price
        )
        
        try:
            result = await self._request("POST", endpoint, data=data)
            send_status = result.get("sendStatus", {})
            
            return KrakenOrderResult(
                success=send_status.get("status", "").lower() in ("placed", "filled"),
                order_id=send_status.get("order_id"),
                symbol=symbol,
                side=side.lower(),
                size=size,
                price=tp_price,
                error=send_status.get("reason") if send_status.get("status") not in ("placed", "filled") else None,
                raw_response=result,
            )
        except Exception as e:
            return KrakenOrderResult(
                success=False,
                symbol=symbol,
                side=side.lower(),
                size=size,
                error=str(e),
            )

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "mkt",
        size: float = 0.0,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        reduce_only: bool = False,
        trigger_signal: str = "mark",
        client_order_id: Optional[str] = None,
    ) -> KrakenOrderResult:
        """
        Unified order placement on Kraken Futures via POST /derivatives/api/v3/sendorder.
        Compliant with official Kraken Futures REST API spec.
        """
        endpoint = "/sendorder"

        # Normalize side
        norm_side = side.lower()
        if norm_side in ("long", "open_long"):
            norm_side = "buy"
        elif norm_side in ("short", "open_short"):
            norm_side = "sell"

        # Normalize order_type
        norm_type = order_type.lower()
        if norm_type == "market":
            norm_type = "mkt"
        elif norm_type == "limit":
            norm_type = "lmt"
        elif norm_type in ("stop", "stop_loss", "stop-loss"):
            norm_type = "stp"

        # Respect instrument contract precision specs
        try:
            from backend.kraken_contracts import get_contract_specs
            specs = get_contract_specs(symbol)
            qty_precision = specs.get("qty_precision", 0)
        except ImportError:
            qty_precision = 0

        if qty_precision > 0:
            formatted_size = f"{float(size):.{qty_precision}f}".rstrip('0').rstrip('.')
            if not formatted_size:
                formatted_size = "0"
            contract_qty = float(size)
        else:
            try:
                from backend.risk.size_calculator import ensure_kraken_contracts
                int_size = ensure_kraken_contracts(size, symbol.upper(), entry_price=price or stop_price)
            except ImportError:
                int_size = max(1, int(round(size))) if size >= 1 else 1
            formatted_size = str(int(int_size))
            contract_qty = float(int_size)

        data: Dict[str, Any] = {
            "orderType": norm_type,
            "symbol": symbol.upper(),
            "side": norm_side,
            "size": formatted_size,
        }

        # Stop price for conditional orders (stp, take_profit)
        if stop_price is not None and norm_type in ("stp", "take_profit", "stop_lmt"):
            data["stopPrice"] = str(stop_price)
            # triggerSignal: mark, index, or last
            if trigger_signal in ("mark", "index", "last"):
                data["triggerSignal"] = trigger_signal
            else:
                data["triggerSignal"] = "mark"

        # Limit price for limit orders or stop-limit
        if price is not None and norm_type in ("lmt", "stop_lmt"):
            data["limitPrice"] = str(price)

        if reduce_only:
            data["reduceOnly"] = True

        if client_order_id:
            data["cliOrdId"] = str(client_order_id)

        logger.info(
            "[KRAKEN] ORDER_START | %s %s size=%s contracts stopPrice=%s limitPrice=%s reduceOnly=%s triggerSignal=%s",
            norm_type.upper(), norm_side.upper(), formatted_size,
            data.get("stopPrice"), data.get("limitPrice"),
            data.get("reduceOnly"), data.get("triggerSignal")
        )

        try:
            result = await self._request("POST", endpoint, data=data)
            send_status = result.get("sendStatus", {})
            status = send_status.get("status", "").lower()
            order_id = send_status.get("order_id")

            if status in ("placed", "filled", "partiallyfilled", "untouched"):
                return KrakenOrderResult(
                    success=True,
                    order_id=order_id,
                    client_order_id=send_status.get("cliOrdId"),
                    symbol=symbol,
                    side=norm_side,
                    size=float(contract_qty),
                    price=price or stop_price,
                    raw_response=result,
                )
            else:
                err = send_status.get("reason") or send_status.get("errorMessage") or status
                return KrakenOrderResult(
                    success=False,
                    order_id=order_id,
                    symbol=symbol,
                    side=norm_side,
                    size=float(contract_qty),
                    price=price or stop_price,
                    error=err,
                    raw_response=result,
                )
        except Exception as e:
            logger.error("[KRAKEN] ORDER_FAILED | %s", e)
            return KrakenOrderResult(
                success=False,
                symbol=symbol,
                side=norm_side,
                size=size,
                price=price or stop_price,
                error=str(e),
            )

    async def get_open_orders(
        self,
        symbol: Optional[str] = None,
        raise_errors: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Fetch open orders from Kraken Futures.
        Endpoint: GET /derivatives/api/v3/openorders

        Args:
            symbol: Optional symbol filter (e.g. 'PF_XBTUSD').
            raise_errors: If True, propagate network, auth, and HTTP exceptions
                          instead of returning an empty list. Default False preserves
                          legacy non-breaking behavior.
        """
        try:
            res = await self._request("GET", "/openorders")
            orders = res.get("openOrders", [])
            if symbol:
                norm_sym = symbol.upper()
                orders = [o for o in orders if (o.get("symbol") or "").upper() == norm_sym]
            return orders
        except Exception as e:
            logger.error("[KRAKEN] get_open_orders error: %s", e)
            if raise_errors:
                raise
            return []

    async def cancel_order(
        self,
        order_id: Optional[str] = None,
        cli_ord_id: Optional[str] = None,
        raise_errors: bool = False,
    ) -> Dict[str, Any]:
        """
        Cancel an open order on Kraken Futures.
        Endpoint: POST /derivatives/api/v3/cancelorder

        Args:
            order_id: Kraken order ID (UUID).
            cli_ord_id: Client order ID (e.g. 'BOT_SL_...').
            raise_errors: If True, propagate network, auth, and HTTP exceptions
                          instead of returning an error dict.

        Returns:
            Dict containing:
              - 'success': True if and only if cancellation was confirmed ('cancelled')
              - 'status': Exact status ('cancelled', 'notFound', 'orderAlreadyFilled', 'error', etc.)
              - 'order_id': Resolved order ID
              - 'cli_ord_id': Resolved client order ID
              - 'result': 'success' if confirmed cancelled, otherwise error/status code
              - 'raw_response': Full exchange payload
        """
        target_oid = str(order_id).strip() if order_id else None
        target_clid = str(cli_ord_id).strip() if cli_ord_id else None

        if not target_oid and not target_clid:
            if raise_errors:
                raise ValueError("Either order_id or cli_ord_id must be provided to cancel_order")
            return {
                "success": False,
                "status": "missing_id",
                "result": "error",
                "error": "Missing order_id or cli_ord_id",
            }

        data: Dict[str, Any] = {}
        if target_oid:
            data["order_id"] = target_oid
        if target_clid:
            data["cliOrdId"] = target_clid

        try:
            res = await self._request("POST", "/cancelorder", data=data)
            if not isinstance(res, dict):
                if raise_errors:
                    raise TypeError(f"Unexpected Kraken cancel_order response: expected dict, got {type(res).__name__}")
                return {
                    "success": False,
                    "status": "malformed_response",
                    "result": "error",
                    "error": f"Invalid response format: {type(res).__name__}",
                }

            if res.get("result") == "error" or "error" in res or "errors" in res:
                err_msg = res.get("error") or res.get("errors") or "Kraken API rejection"
                if raise_errors:
                    raise RuntimeError(f"Kraken cancel_order API error: {err_msg}")
                return {
                    "success": False,
                    "status": "error",
                    "result": "error",
                    "error": str(err_msg),
                    "raw_response": res,
                }

            cancel_status = res.get("cancelStatus")
            if not isinstance(cancel_status, dict):
                if raise_errors:
                    raise ValueError(f"Missing cancelStatus in Kraken cancel_order response: {res}")
                return {
                    "success": False,
                    "status": "missing_cancel_status",
                    "result": "error",
                    "error": "Missing cancelStatus in exchange response",
                    "raw_response": res,
                }

            raw_status = str(cancel_status.get("status", "")).strip()
            resp_oid = cancel_status.get("order_id", target_oid)
            resp_clid = cancel_status.get("cliOrdId", target_clid)

            if raw_status == "cancelled":
                logger.info("[KRAKEN] cancel_order SUCCESS: order_id=%s cliOrdId=%s", resp_oid, resp_clid)
                return {
                    "success": True,
                    "status": "cancelled",
                    "result": "success",
                    "order_id": resp_oid,
                    "cli_ord_id": resp_clid,
                    "raw_response": res,
                }
            elif raw_status == "notFound":
                logger.warning("[KRAKEN] cancel_order notFound: order_id=%s cliOrdId=%s (order not active)", resp_oid, resp_clid)
                return {
                    "success": False,
                    "status": "notFound",
                    "result": "notFound",
                    "order_id": resp_oid,
                    "cli_ord_id": resp_clid,
                    "error": "Order not found",
                    "raw_response": res,
                }
            elif raw_status in ("orderAlreadyFilled", "filled"):
                logger.warning("[KRAKEN] cancel_order in-flight fill: order_id=%s cliOrdId=%s", resp_oid, resp_clid)
                return {
                    "success": False,
                    "status": "orderAlreadyFilled",
                    "result": "orderAlreadyFilled",
                    "order_id": resp_oid,
                    "cli_ord_id": resp_clid,
                    "error": "Order already filled",
                    "raw_response": res,
                }
            else:
                logger.warning("[KRAKEN] cancel_order unconfirmed status: %s (order_id=%s)", raw_status, resp_oid)
                return {
                    "success": False,
                    "status": raw_status or "unknown",
                    "result": raw_status or "error",
                    "order_id": resp_oid,
                    "cli_ord_id": resp_clid,
                    "error": f"Unhandled cancelStatus: {raw_status}",
                    "raw_response": res,
                }

        except Exception as e:
            logger.error("[KRAKEN] cancel_order exception: %s", e)
            if raise_errors:
                raise
            return {
                "success": False,
                "status": "error",
                "result": "error",
                "error": str(e),
            }

    async def get_order_status(
        self,
        order_ids: Optional[List[str]] = None,
        cli_ord_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Fetch status of orders via POST /derivatives/api/v3/orders/status
        Note: Per Kraken doc, concerns orders open or filled/cancelled within last 5 seconds.
        """
        try:
            data: Dict[str, Any] = {}
            if order_ids:
                data["orderIds"] = ",".join(order_ids) if isinstance(order_ids, list) else str(order_ids)
            if cli_ord_ids:
                data["cliOrdIds"] = ",".join(cli_ord_ids) if isinstance(cli_ord_ids, list) else str(cli_ord_ids)
            return await self._request("POST", "/orders/status", data=data)
        except Exception as e:
            logger.error("[KRAKEN] get_order_status error: %s", e)
            return {"result": "error", "error": str(e)}

    async def get_fills(self, last_fill_time: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fetch recent fills from Kraken Futures.
        Endpoint: GET /derivatives/api/v3/fills
        """
        try:
            params = {}
            if last_fill_time:
                params["lastFillTime"] = str(last_fill_time)
            res = await self._request("GET", "/fills", params=params)
            return res.get("fills", [])
        except Exception as e:
            logger.error("[KRAKEN] get_fills error: %s", e)
            return []
    
    # ══════════════════════════════════════════════════════════════════════════
    # CONNECTION TEST
    # ══════════════════════════════════════════════════════════════════════════
    
    async def test_connection(self) -> Dict[str, Any]:
        """
        Test Kraken Futures API connection and authentication.
        
        This validates that:
        1. API keys are configured
        2. Keys are for Kraken FUTURES (not Spot!)
        3. Balance can be fetched
        
        Returns:
            {
                "success": bool,
                "authenticated": bool,
                "balance": {...} or None,
                "positions_count": int,
                "error": str or None
            }
        """
        logger.info("[KRAKEN] Testing Futures API connection...")
        
        if not self.api_key or not self.api_secret:
            return {
                "success": False,
                "authenticated": False,
                "balance": None,
                "positions_count": 0,
                "error": "API key or secret not configured. "
                         "Set KRAKEN_API_KEY and KRAKEN_API_SECRET (from futures.kraken.com)"
            }
        
        try:
            # Test balance fetch (validates authentication)
            balance = await self.get_balance(force_refresh=True)
            
            # Also fetch positions to fully validate
            positions = await self.get_positions(force_refresh=True)
            
            logger.info("[KRAKEN] ✓ Futures API connection successful!")
            return {
                "success": True,
                "authenticated": True,
                "balance": balance.to_dict(),
                "positions_count": len(positions),
                "error": None
            }
            
        except ValueError as e:
            error_msg = str(e)
            logger.error("[KRAKEN] ✗ Connection failed: %s", error_msg)
            
            # Provide helpful hints
            hint = ""
            if "authenticationError" in error_msg.lower() or "invalid" in error_msg.lower():
                hint = (
                    "\n\n💡 COMMON FIX: Your API keys might be for Kraken SPOT instead of FUTURES."
                    "\n   → Create FUTURES keys at: https://futures.kraken.com/trade/settings/api"
                    "\n   → NOT at kraken.com!"
                )
            
            return {
                "success": False,
                "authenticated": False,
                "balance": None,
                "positions_count": 0,
                "error": f"{error_msg}{hint}"
            }
        except Exception as e:
            logger.error("[KRAKEN] ✗ Connection error: %s", e)
            return {
                "success": False,
                "authenticated": False,
                "balance": None,
                "positions_count": 0,
                "error": f"Network error: {e}"
            }
    
    def is_connected(self) -> bool:
        """Check if last request was successful."""
        return self._is_connected
    
    def get_last_error(self) -> Optional[str]:
        """Get last error message."""
        return self._last_error


# ══════════════════════════════════════════════════════════════════════════════
# KRAKEN FUTURES EXECUTOR
# ══════════════════════════════════════════════════════════════════════════════

class KrakenFuturesExecutor:
    """
    High-level executor for Kraken Futures trades.
    
    Provides unified interface for MirrorExecutionService.
    Handles symbol translation automatically.
    """
    
    def __init__(self, client: Optional[KrakenFuturesClient] = None):
        """Initialize with a client or create one."""
        self.client = client or KrakenFuturesClient()
        self.broker_name = "kraken"
    
    def translate_symbol(self, symbol: str) -> Optional[str]:
        """Translate standard symbol to Kraken format."""
        return to_kraken_symbol(symbol)
    
    async def get_balance(self) -> Dict[str, Any]:
        """Get account balance."""
        balance = await self.client.get_balance()
        return balance.to_dict()
    
    async def get_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open positions."""
        positions = await self.client.get_positions()
        
        if symbol:
            kraken_symbol = to_kraken_symbol(symbol)
            positions = [p for p in positions if p.symbol.upper() == kraken_symbol]
        
        return [p.to_dict() for p in positions]
    
    async def execute_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: str = "market",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        leverage: int = 10,
    ) -> Dict[str, Any]:
        """
        NEUTRALIZED (Phase 1.5 / P1-1): Direct order execution via legacy
        backend.exchanges.kraken_futures.KrakenFuturesExecutor is strictly forbidden.
        All Kraken order execution MUST go through KrakenFuturesExecutorWrapper.place_order().
        """
        raise RuntimeError(
            "FORBIDDEN_LEGACY_KRAKEN_EXECUTOR: Direct order execution via "
            "backend.exchanges.kraken_futures.KrakenFuturesExecutor.execute_order() is disabled "
            "(bypasses KRAKEN_EXECUTION_ENABLED, pre-flight SL validation, exchange-side SL confirmation, "
            "emergency close, and SQLite persistence). Use KrakenFuturesExecutorWrapper.place_order() instead."
        )
    
    async def test_connection(self) -> Dict[str, Any]:
        """Test connection."""
        return await self.client.test_connection()
    
    def is_connected(self) -> bool:
        """Check if connected."""
        return self.client.is_connected()


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_client_instance: Optional[KrakenFuturesClient] = None


def get_kraken_futures_client() -> KrakenFuturesClient:
    """Get singleton instance of Kraken Futures client."""
    global _client_instance
    if _client_instance is None:
        _client_instance = KrakenFuturesClient()
    return _client_instance


def get_kraken_futures_executor() -> KrakenFuturesExecutor:
    """Get executor with singleton client."""
    return KrakenFuturesExecutor(get_kraken_futures_client())


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP SELF-CHECK
# ══════════════════════════════════════════════════════════════════════════════

async def kraken_futures_startup_check() -> Dict[str, Any]:
    """
    Perform startup self-check for Kraken Futures.
    
    Validates:
    1. API credentials are configured
    2. Credentials are for Futures (not Spot)
    3. Balance and positions endpoints work
    
    Returns:
        {
            "ok": bool,
            "reason": str,
            "balance": float or None,
            "details": dict
        }
    """
    logger.info("[KRAKEN_STARTUP] Running Futures API self-check...")
    
    client = get_kraken_futures_client()
    result = await client.test_connection()
    
    if result["success"]:
        return {
            "ok": True,
            "reason": "Kraken Futures API ready",
            "balance": result["balance"]["available"] if result["balance"] else None,
            "details": result,
        }
    else:
        return {
            "ok": False,
            "reason": result["error"],
            "balance": None,
            "details": result,
        }


# ══════════════════════════════════════════════════════════════════════════════
# CLI TEST
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    """Test Kraken Futures client."""
    print("=" * 60)
    print("KRAKEN FUTURES CLIENT - TEST")
    print("⚠️  Using FUTURES API only (futures.kraken.com)")
    print("=" * 60)
    
    result = await kraken_futures_startup_check()
    
    if result["ok"]:
        print(f"\n✅ SUCCESS: {result['reason']}")
        print(f"   Balance: ${result['balance']:.2f}")
    else:
        print(f"\n❌ FAILED: {result['reason']}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    asyncio.run(main())
