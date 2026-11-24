"""
KrakenFuturesClient — Async WebSocket V2 client for Kraken Futures.

Handles:
  - REST authentication to obtain WebSocket token
  - WebSocket V2 connection with auto-reconnect
  - Order placement via add_order method
  - Channel subscriptions (executions, balances, etc.)
  - Error handling (token expired, bad signature, rate limits)

Usage:
    client = KrakenFuturesClient(api_key, api_secret)
    await client.connect()
    result = await client.send_order_kraken(
        symbol="BTC/USD",
        side="buy",
        order_type="limit",
        quantity=0.001,
        price=50000.0
    )
    await client.disconnect()

Reference:
    https://docs.kraken.com/api/docs/websocket-v2/add_order/
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import httpx

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
except ImportError:
    websockets = None  # type: ignore

# Import the corrected REST client V2 for balance operations
from backend.kraken_futures_rest_v2 import (
    KrakenFuturesRESTClientV2 as KrakenFuturesRESTClient,
    get_kraken_futures_client as get_kraken_futures_rest_client,
    sign_kraken_futures_request as sign_kraken_futures,
)

logger = logging.getLogger("smarttrade.kraken_futures")

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

# Kraken Spot REST (NOT USED for Futures - kept for reference)
KRAKEN_REST_BASE = "https://api.kraken.com"
KRAKEN_WS_PUBLIC = "wss://ws.kraken.com/v2"
KRAKEN_WS_AUTH_SPOT = "wss://ws-auth.kraken.com/v2"  # OLD Spot endpoint

# Kraken Futures WebSocket (CORRECT endpoints)
KRAKEN_FUTURES_WS = "wss://futures.kraken.com/ws/v1"  # Auth via challenge/response
KRAKEN_WS_AUTH = KRAKEN_FUTURES_WS  # Use Futures WebSocket for auth

# Kraken Futures REST (for balance, positions, account info)
KRAKEN_FUTURES_REST_BASE = "https://futures.kraken.com"
KRAKEN_FUTURES_API_PATH = "/derivatives/api/v3"

# Token is valid for ~15 minutes, we refresh at 12 min
TOKEN_REFRESH_INTERVAL = 720  # 12 minutes in seconds

# Balance refresh interval (5 minutes)
BALANCE_REFRESH_INTERVAL = 300


# ══════════════════════════════════════════════════════════════════════════════
# ENUMS & DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

class OrderType(str, Enum):
    """Kraken order types."""
    MARKET = "market"
    LIMIT = "limit"
    STOP_LOSS = "stop-loss"
    STOP_LOSS_LIMIT = "stop-loss-limit"
    TAKE_PROFIT = "take-profit"
    TAKE_PROFIT_LIMIT = "take-profit-limit"
    TRAILING_STOP = "trailing-stop"
    TRAILING_STOP_LIMIT = "trailing-stop-limit"


class OrderSide(str, Enum):
    """Order side."""
    BUY = "buy"
    SELL = "sell"


class TimeInForce(str, Enum):
    """Time in force options."""
    GTC = "gtc"  # Good Till Canceled
    GTD = "gtd"  # Good Till Date
    IOC = "ioc"  # Immediate Or Cancel


class TriggerReference(str, Enum):
    """Reference price for triggered orders."""
    INDEX = "index"
    LAST = "last"


@dataclass
class KrakenOrderRequest:
    """Structured order request for Kraken."""
    symbol: str
    side: OrderSide
    order_type: OrderType
    order_qty: float
    limit_price: Optional[float] = None
    time_in_force: TimeInForce = TimeInForce.GTC
    margin: bool = False
    post_only: bool = False
    reduce_only: bool = False
    cl_ord_id: Optional[str] = None
    validate_only: bool = False
    # Trigger parameters
    trigger_price: Optional[float] = None
    trigger_reference: TriggerReference = TriggerReference.LAST
    # Conditional (OTO) parameters
    conditional_order_type: Optional[OrderType] = None
    conditional_limit_price: Optional[float] = None
    conditional_trigger_price: Optional[float] = None


@dataclass
class KrakenOrderResponse:
    """Structured order response from Kraken."""
    success: bool
    order_id: Optional[str] = None
    cl_ord_id: Optional[str] = None
    error: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    time_in: Optional[str] = None
    time_out: Optional[str] = None
    raw_response: Dict[str, Any] = field(default_factory=dict)


class KrakenError(Exception):
    """Base Kraken exception."""
    pass


class KrakenAuthError(KrakenError):
    """Authentication related errors."""
    pass


class KrakenTokenExpiredError(KrakenAuthError):
    """Token expired, needs refresh."""
    pass


class KrakenRateLimitError(KrakenError):
    """Rate limit exceeded."""
    pass


class KrakenOrderError(KrakenError):
    """Order placement error."""
    pass


class KrakenFuturesMisrouteError(KrakenError):
    """
    CRITICAL: A Kraken Futures operation tried to call the Spot API.
    
    This error is raised when code running in Futures mode attempts to call
    api.kraken.com instead of futures.kraken.com. This is a programming error
    and indicates a misrouted API call.
    
    The Kraken broker should be disabled when this error occurs.
    """
    pass


def _check_futures_misroute(url: str, operation: str = "unknown"):
    """
    Guardrail: Raise KrakenFuturesMisrouteError if url contains api.kraken.com.
    
    This prevents accidental SPOT API calls in Futures mode.
    """
    if "api.kraken.com" in url:
        logger.critical(
            "[KRAKEN_FUTURES_MISROUTE] CRITICAL: Operation '%s' tried to call SPOT API: %s | "
            "Kraken Futures should ONLY call futures.kraken.com!",
            operation, url
        )
        raise KrakenFuturesMisrouteError(
            f"Futures operation '{operation}' misrouted to Spot API: {url}. "
            f"Kraken Futures MUST use futures.kraken.com, NOT api.kraken.com!"
        )


# ══════════════════════════════════════════════════════════════════════════════
# KRAKEN FUTURES CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class KrakenFuturesClient:
    """
    Async Kraken Futures client with WebSocket V2 support.
    
    Features:
      - REST authentication for WebSocket token
      - WebSocket V2 connection with auto-reconnect
      - Order placement (market, limit, stop-loss, take-profit)
      - Channel subscriptions
      - Automatic token refresh
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        timeout: float = 10.0,
        auto_reconnect: bool = True,
        on_message_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_disconnect_callback: Optional[Callable[[str], Any]] = None,
        on_reconnect_callback: Optional[Callable[[], Any]] = None,
        use_demo: Optional[bool] = None,
        base_url: Optional[str] = None,
        ws_url: Optional[str] = None,
        rest_client: Optional[Any] = None,
    ):
        """
        Initialize Kraken Futures client with strict LIVE/DEMO isolation.
        """
        if websockets is None:
            raise ImportError("websockets library required: pip install websockets")

        from backend.kraken_futures_rest_v2 import (
            resolve_kraken_environment,
            validate_kraken_client_environment,
        )
        env_cfg = resolve_kraken_environment(
            api_key=api_key,
            api_secret=api_secret,
            use_demo=use_demo,
            base_url=base_url,
            ws_url=ws_url,
        )
        self.use_demo: bool = env_cfg["use_demo"]
        self.environment: str = env_cfg["environment"]
        self.api_key = env_cfg["api_key"]
        self.api_secret = env_cfg["api_secret"]
        self.base_url: str = env_cfg["rest_url"]
        self.ws_url: str = env_cfg["ws_url"]
        if rest_client is not None:
            validate_kraken_client_environment(rest_client, expected_use_demo=self.use_demo, label="rest_client")
        self._rest_client = rest_client
        self.timeout = timeout
        self.auto_reconnect = auto_reconnect
        self.on_message_callback = on_message_callback
        self.on_disconnect_callback = on_disconnect_callback
        self.on_reconnect_callback = on_reconnect_callback
        
        # WebSocket state
        self._ws: Optional[WebSocketClientProtocol] = None
        self._ws_token: Optional[str] = None
        self._token_obtained_at: float = 0
        self._connected = False
        self._running = False
        self._recv_task: Optional[asyncio.Task] = None
        self._token_refresh_task: Optional[asyncio.Task] = None
        
        # Request tracking
        self._req_id_counter = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}
        
        # Subscriptions
        self._subscriptions: List[Dict[str, Any]] = []
        
        logger.info("[KRAKEN] client initialized (key=%s...)", api_key[:8] if api_key else "NONE")
        
        # Futures balance cache
        self._futures_balance_cache: Dict[str, Any] = {}
        self._last_balance_refresh: float = 0

    # ══════════════════════════════════════════════════════════════════════════
    # REST AUTHENTICATION (Kraken Spot - for WebSocket token)
    # ══════════════════════════════════════════════════════════════════════════

    def _sign_rest(self, urlpath: str, data: Dict[str, Any], nonce: str) -> str:
        """
        Generate HMAC-SHA512 signature for Kraken Spot REST API.
        
        Signature = HMAC-SHA512(urlpath + SHA256(nonce + POST_DATA), base64_decode(secret))
        """
        post_data = urllib.parse.urlencode(data)
        message = (nonce + post_data).encode("utf-8")
        sha256_hash = hashlib.sha256(message).digest()
        hmac_message = urlpath.encode("utf-8") + sha256_hash
        
        secret_decoded = base64.b64decode(self.api_secret)
        signature = hmac.new(secret_decoded, hmac_message, hashlib.sha512)
        return base64.b64encode(signature.digest()).decode("utf-8")

    # ══════════════════════════════════════════════════════════════════════════
    # REST AUTHENTICATION (Kraken Futures - for balance/accounts)
    # ══════════════════════════════════════════════════════════════════════════

    def _sign_futures_request(self, endpoint: str, post_data: str = "", nonce: Optional[str] = None) -> str:
        """
        Generate HMAC-SHA512 signature for Kraken Futures REST API.
        
        Kraken Futures Signature Algorithm:
        1. Concatenate: postData + nonce + endpoint
        2. Hash with SHA-256
        3. Sign the hash with HMAC-SHA512 using decoded API secret
        4. Base64 encode the result
        
        Args:
            endpoint: Full endpoint path (e.g., /derivatives/api/v3/accounts)
            post_data: POST data as URL-encoded string (empty for GET)
            nonce: Unique nonce string (milliseconds timestamp)
            
        Returns:
            Base64-encoded HMAC-SHA512 signature
        """
        if nonce is None:
            nonce = str(int(time.time() * 1000))
        
        # Step 1: Concatenate post_data + nonce + endpoint
        message = (post_data + nonce + endpoint).encode("utf-8")
        
        # Step 2: SHA-256 hash of the message
        sha256_hash = hashlib.sha256(message).digest()
        
        # Step 3: Decode the API secret from base64
        secret_decoded = base64.b64decode(self.api_secret)
        
        # Step 4: HMAC-SHA512 sign the SHA-256 hash
        signature = hmac.new(secret_decoded, sha256_hash, hashlib.sha512)
        
        # Step 5: Base64 encode the signature
        return base64.b64encode(signature.digest()).decode("utf-8")

    async def _futures_rest_get(
        self, 
        endpoint: str, 
        params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Execute authenticated GET request to Kraken Futures REST API.
        
        IMPORTANT: Kraken Futures requires:
        - URL uses /derivatives/api/v3/... 
        - Signature uses /api/v3/... (WITHOUT /derivatives!)
        
        Args:
            endpoint: API endpoint (e.g., "/derivatives/api/v3/accounts")
            params: Optional query parameters
            
        Returns:
            API response as dict
            
        Raises:
            KrakenAuthError: On authentication errors
            KrakenError: On other API errors
        """
        nonce = str(int(time.time() * 1000))
        
        # Build query string
        query_string = urllib.parse.urlencode(params) if params else ""
        full_endpoint = endpoint
        if query_string:
            full_endpoint = f"{endpoint}?{query_string}"
        
        # CRITICAL FIX: Signature uses /api/v3/..., NOT /derivatives/api/v3/...
        # Remove /derivatives prefix for signature
        sign_endpoint = endpoint.replace("/derivatives", "")
        signature = self._sign_futures_request(sign_endpoint, "", nonce)
        
        headers = {
            "APIKey": self.api_key,
            "Nonce": nonce,
            "Authent": signature,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        
        from backend.kraken_futures_rest_v2 import validate_kraken_urls_for_mode
        validate_kraken_urls_for_mode(self.use_demo, self.base_url, self.ws_url)

        url = self.base_url + full_endpoint
        
        logger.debug("[KRAKEN_FUTURES_REST] GET %s (sign_path=%s)", endpoint, sign_endpoint)
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error("[KRAKEN_FUTURES_REST] HTTP error: %s | response: %s", e, e.response.text)
                raise KrakenError(f"Futures HTTP error: {e}")
        
        result = resp.json()
        
        # Check for errors
        if result.get("result") == "error":
            error_msg = result.get("error", "Unknown error")
            logger.error("[KRAKEN_FUTURES_REST] API error: %s", error_msg)
            
            if "authenticationError" in str(error_msg).lower():
                raise KrakenAuthError(f"Futures auth error: {error_msg}")
            else:
                raise KrakenError(f"Futures API error: {error_msg}")
        
        return result

    async def get_futures_accounts(self) -> Dict[str, Any]:
        """
        Get Kraken Futures account information and balances.
        
        Endpoint: GET /derivatives/api/v3/accounts
        
        Returns:
            {
                "result": "success",
                "accounts": {
                    "fi_xbtusd": {
                        "auxiliary": {...},
                        "marginRequirements": {...},
                        "triggerEstimates": {...},
                        "balances": {...}
                    },
                    "cash": {
                        "balances": {
                            "USD": 5000.00
                        }
                    }
                },
                "serverTime": "2024-01-01T12:00:00.000Z"
            }
            
        Raises:
            KrakenAuthError: On authentication failure
            KrakenError: On API error
        """
        result = await self._futures_rest_get(f"{KRAKEN_FUTURES_API_PATH}/accounts")
        
        # Cache the result
        self._futures_balance_cache = result
        self._last_balance_refresh = time.time()
        
        logger.info("[KRAKEN_FUTURES_REST] Accounts fetched successfully")
        return result

    async def get_futures_balance_usd(self) -> float:
        """
        Get available USD balance from Kraken Futures.
        
        Returns:
            Available USD balance
            
        Example:
            balance = await client.get_futures_balance_usd()
            logger.info("Available: $%.2f", balance)
        """
        accounts = await self.get_futures_accounts()
        
        # Extract USD balance from cash account
        cash_account = accounts.get("accounts", {}).get("cash", {})
        balances = cash_account.get("balances", {})
        usd_balance = float(balances.get("usd", balances.get("USD", 0.0)))
        
        logger.info("[KRAKEN_FUTURES_BALANCE] USD available: %.2f", usd_balance)
        return usd_balance

    async def get_futures_account_summary(self) -> Dict[str, Any]:
        """
        Get comprehensive Kraken Futures account summary.
        
        Returns:
            {
                "available": float,          # Available USD for trading
                "total": float,              # Total equity
                "margin_used": float,        # Margin used by open positions
                "unrealized_pnl": float,     # Unrealized P&L
                "currency": "USD",
                "positions_value": float,    # Total positions value
                "raw": {...}                 # Raw API response
            }
        """
        accounts = await self.get_futures_accounts()
        
        # Parse accounts structure
        # Kraken Futures has multiple sub-accounts: cash, flex (multi-collateral), and per-contract
        
        cash = accounts.get("accounts", {}).get("cash", {})
        cash_balances = cash.get("balances", {})
        
        # Get flex account (preferred for margin trading)
        flex = accounts.get("accounts", {}).get("flex", {})
        flex_balances = flex.get("balances", {})
        
        # Calculate totals
        # USD can be in cash or flex account
        usd_cash = float(cash_balances.get("usd", cash_balances.get("USD", 0.0)))
        usd_flex = float(flex_balances.get("usd", flex_balances.get("USD", 0.0)))
        
        # Flex account details
        available = float(flex.get("availableMargin", usd_cash + usd_flex))
        pnl = float(flex.get("pnl", 0.0))
        initial_margin = float(flex.get("initialMargin", 0.0))
        
        # Portfolio value
        portfolio_value = float(flex.get("portfolioValue", available + pnl))
        
        summary = {
            "available": available,
            "total": portfolio_value,
            "margin_used": initial_margin,
            "unrealized_pnl": pnl,
            "currency": "USD",
            "positions_value": initial_margin,
            "usd_cash": usd_cash,
            "usd_flex": usd_flex,
            "raw": accounts,
        }
        
        logger.info(
            "[KRAKEN_FUTURES_BALANCE] available=%.2f | total=%.2f | margin_used=%.2f | pnl=%.2f",
            summary["available"], summary["total"], summary["margin_used"], summary["unrealized_pnl"]
        )
        
        return summary

    async def check_futures_balance_sufficient(self, required_usd: float) -> Tuple[bool, float]:
        """
        Check if Kraken Futures balance is sufficient for a trade.
        
        Args:
            required_usd: Required margin in USD
            
        Returns:
            Tuple of (is_sufficient, available_balance)
        """
        try:
            summary = await self.get_futures_account_summary()
            available = summary.get("available", 0.0)
            is_sufficient = available >= required_usd
            
            if not is_sufficient:
                logger.warning(
                    "[KRAKEN_FUTURES_BALANCE] INSUFFICIENT | required=%.2f | available=%.2f",
                    required_usd, available
                )
            
            return is_sufficient, available
            
        except Exception as e:
            logger.error("[KRAKEN_FUTURES_BALANCE] Check failed: %s", e)
            return False, 0.0

    def get_cached_balance(self) -> Optional[Dict[str, Any]]:
        """
        Get cached Futures balance (no API call).
        
        Returns:
            Cached balance info or None if not available
        """
        if not self._futures_balance_cache:
            return None
        
        cache_age = time.time() - self._last_balance_refresh
        
        return {
            "cached": True,
            "age_seconds": cache_age,
            "data": self._futures_balance_cache,
            "stale": cache_age > BALANCE_REFRESH_INTERVAL,
        }

    async def _get_websocket_token(self) -> str:
        """
        Obtain WebSocket token via REST API.
        
        Endpoint: POST /0/private/GetWebSocketsToken
        
        Returns:
            WebSocket session token
            
        Raises:
            KrakenAuthError: On authentication failure
        """
        urlpath = "/0/private/GetWebSocketsToken"
        nonce = str(int(time.time() * 1000))
        data = {"nonce": nonce}
        
        signature = self._sign_rest(urlpath, data, nonce)
        
        headers = {
            "API-Key": self.api_key,
            "API-Sign": signature,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        
        url = KRAKEN_REST_BASE + urlpath
        
        logger.debug("[KRAKEN_REST] POST %s", urlpath)
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(url, data=data, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error("[KRAKEN_REST] HTTP error: %s", e)
                raise KrakenAuthError(f"HTTP error: {e}")
        
        result = resp.json()
        
        # Check for errors
        errors = result.get("error", [])
        if errors:
            error_msg = ", ".join(errors)
            logger.error("[KRAKEN_REST] API error: %s", error_msg)
            
            if "EAPI:Invalid key" in error_msg:
                raise KrakenAuthError("Invalid API key")
            elif "EAPI:Invalid signature" in error_msg:
                raise KrakenAuthError("Invalid signature - check API secret")
            elif "EAPI:Invalid nonce" in error_msg:
                raise KrakenAuthError("Invalid nonce - system time may be incorrect")
            else:
                raise KrakenAuthError(error_msg)
        
        token = result.get("result", {}).get("token")
        if not token:
            raise KrakenAuthError("No token in response")
        
        self._token_obtained_at = time.time()
        logger.info("[KRAKEN_REST] WebSocket token obtained (expires in ~15min)")
        
        return token

    # ══════════════════════════════════════════════════════════════════════════
    # REST API — BALANCE & ACCOUNT
    # ══════════════════════════════════════════════════════════════════════════

    async def _rest_private_post(self, urlpath: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Execute authenticated REST POST request.
        
        Args:
            urlpath: API endpoint path (e.g., "/0/private/Balance")
            data: Optional POST data (nonce is added automatically)
            
        Returns:
            API response as dict
            
        Raises:
            KrakenAuthError: On authentication errors
            KrakenFuturesMisrouteError: If trying to call /OpenPositions (use Futures API instead)
            KrakenError: On other API errors
        """
        # GUARDRAIL: Block /OpenPositions calls via SPOT API 
        # Positions MUST use the Futures API (get_open_positions() delegates to Futures REST)
        if "OpenPositions" in urlpath:
            logger.critical(
                "[KRAKEN_FUTURES_MISROUTE] BLOCKED: Attempt to call SPOT API for positions! "
                "Use get_open_positions() which correctly calls futures.kraken.com"
            )
            raise KrakenFuturesMisrouteError(
                f"BLOCKED: {urlpath} is a SPOT API endpoint. Kraken Futures positions "
                "MUST use futures.kraken.com/derivatives/api/v3/openpositions"
            )
        
        if data is None:
            data = {}
        
        nonce = str(int(time.time() * 1000))
        data["nonce"] = nonce
        
        signature = self._sign_rest(urlpath, data, nonce)
        
        headers = {
            "API-Key": self.api_key,
            "API-Sign": signature,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        
        url = KRAKEN_REST_BASE + urlpath
        
        logger.debug("[KRAKEN_REST] POST %s", urlpath)
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(url, data=data, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error("[KRAKEN_REST] HTTP error: %s", e)
                raise KrakenError(f"HTTP error: {e}")
        
        result = resp.json()
        
        # Check for errors
        errors = result.get("error", [])
        if errors:
            error_msg = ", ".join(errors)
            logger.error("[KRAKEN_REST] API error: %s", error_msg)
            
            if "EAPI:Invalid key" in error_msg:
                raise KrakenAuthError("Invalid API key")
            elif "EAPI:Invalid signature" in error_msg:
                raise KrakenAuthError("Invalid signature")
            elif "EAPI:Invalid nonce" in error_msg:
                raise KrakenAuthError("Invalid nonce")
            else:
                raise KrakenError(error_msg)
        
        return result.get("result", {})

    async def get_balance(self) -> Dict[str, float]:
        """
        Get Kraken Futures account balance via REST API.
        
        Uses the corrected Futures REST authentication (HMAC-SHA512).
        Endpoint: GET /derivatives/api/v3/accounts
        
        Returns:
            Dict with balance info: {"available": ..., "total": ..., "margin_used": ..., "unrealized_pnl": ...}
            
        Raises:
            KrakenAuthError: On authentication failure
            KrakenError: On API error
            
        Example:
            balance = await client.get_balance()
            available = balance.get("available", 0.0)
        """
        try:
            # Use the corrected Futures REST client
            rest_client = KrakenFuturesRESTClient(self.api_key, self.api_secret)
            balance = await rest_client.get_balance()
            
            result = {
                "available": balance.available,
                "total": balance.total,
                "margin_used": balance.margin_used,
                "unrealized_pnl": balance.unrealized_pnl,
                "currency": balance.currency,
            }
            
            logger.info("[KRAKEN_BALANCE] Futures balance: available=%.2f total=%.2f", 
                       balance.available, balance.total)
            return result
            
        except ValueError as e:
            logger.error("[KRAKEN_BALANCE] API error: %s", e)
            raise KrakenAuthError(str(e))
        except Exception as e:
            logger.error("[KRAKEN_BALANCE] Failed: %s", e)
            raise KrakenError(str(e))

    async def get_trade_balance(self, asset: str = "USD") -> Dict[str, float]:
        """
        Get extended trade balance (margin/equity info).
        
        Endpoint: POST /0/private/TradeBalance
        
        Args:
            asset: Base asset for calculations (default: USD)
            
        Returns:
            {
                "eb": 0.0,      # Equivalent balance (all currencies in base)
                "tb": 0.0,      # Trade balance (equity available for trading)
                "m": 0.0,       # Margin amount of open positions
                "n": 0.0,       # Unrealized net P/L
                "c": 0.0,       # Cost basis of open positions
                "v": 0.0,       # Value of open positions
                "e": 0.0,       # Equity = trade balance + unrealized net P/L
                "mf": 0.0,      # Free margin = equity - initial margin
                "ml": 0.0,      # Margin level (if positions open)
            }
            
        Raises:
            KrakenAuthError: On authentication failure
            KrakenError: On API error
        """
        result = await self._rest_private_post("/0/private/TradeBalance", {"asset": asset})
        
        # Convert to float
        trade_balance = {}
        for key, value in result.items():
            try:
                trade_balance[key] = float(value)
            except (ValueError, TypeError):
                trade_balance[key] = 0.0
        
        logger.debug("[KRAKEN_REST] TradeBalance: %s", trade_balance)
        return trade_balance

    async def get_available_balance_usd(self) -> float:
        """
        Get available USD balance for trading (free margin).
        
        Convenience method that returns the amount available to open new positions.
        
        Returns:
            Available balance in USD (free margin if positions open, trade balance otherwise)
            
        Example:
            available = await client.get_available_balance_usd()
            if available >= required_margin:
                # OK to place order
        """
        try:
            trade_bal = await self.get_trade_balance("USD")
            
            # mf = free margin (equity - initial margin)
            # tb = trade balance (if no positions, this is available)
            free_margin = trade_bal.get("mf", 0.0)
            trade_balance = trade_bal.get("tb", 0.0)
            
            # If free margin is reported, use it; otherwise use trade balance
            available = free_margin if free_margin > 0 else trade_balance
            
            logger.info("[KRAKEN_REST] Available USD: %.2f (free_margin=%.2f, trade_bal=%.2f)",
                       available, free_margin, trade_balance)
            
            return available
            
        except Exception as e:
            logger.error("[KRAKEN_REST] Failed to get available balance: %s", e)
            raise

    async def check_balance_sufficient(self, required_usd: float) -> Tuple[bool, float]:
        """
        Check if balance is sufficient for a trade.
        
        Args:
            required_usd: Required margin in USD
            
        Returns:
            Tuple of (is_sufficient, available_balance)
            
        Example:
            sufficient, available = await client.check_balance_sufficient(100.0)
            if not sufficient:
                logger.warning("Insufficient balance: need $100, have $%.2f", available)
        """
        available = await self.get_available_balance_usd()
        is_sufficient = available >= required_usd
        
        if not is_sufficient:
            logger.warning("[KRAKEN_REST] INSUFFICIENT_BALANCE required=%.2f available=%.2f",
                          required_usd, available)
        
        return is_sufficient, available

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """
        Get current open positions from Kraken FUTURES API.
        
        ⚠️  IMPORTANT: This uses the Kraken Futures REST API (futures.kraken.com),
            NOT the Kraken Spot API (api.kraken.com).
        
        Endpoint: GET https://futures.kraken.com/derivatives/api/v3/openpositions
        
        Returns:
            List of position dicts with unified format:
            [
                {
                    "symbol": "PF_XBTUSD",
                    "side": "long",
                    "size": 0.001,
                    "entry_price": 50000.0,
                    "mark_price": 51000.0,
                    "unrealized_pnl": 10.0,
                    "leverage": 5.0,
                    "liquidation_price": 45000.0,
                }
            ]
            
        Raises:
            KrakenAuthError: On authentication failure
            KrakenError: On API error
        """
        try:
            # Use the corrected Futures REST client (NOT Spot!)
            # This ensures we call futures.kraken.com/derivatives/api/v3/openpositions
            rest_client = KrakenFuturesRESTClient(self.api_key, self.api_secret)
            positions = await rest_client.get_positions()
            
            # Convert KrakenFuturesPosition objects to dicts
            result = [p.to_dict() for p in positions]
            
            logger.info("[KRAKEN_POS] Listed %d positions via Futures API", len(result))
            return result
            
        except ValueError as e:
            logger.error("[KRAKEN_POS] Auth error: %s", e)
            raise KrakenAuthError(str(e))
        except Exception as e:
            logger.error("[KRAKEN_POS] Failed: %s", e)
            raise KrakenError(str(e))

    # ══════════════════════════════════════════════════════════════════════════
    # REST API — MARKET DATA (PUBLIC)
    # ══════════════════════════════════════════════════════════════════════════

    async def _rest_public_get(self, urlpath: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Execute public REST GET request.
        
        Args:
            urlpath: API endpoint path (e.g., "/0/public/Ticker")
            params: Optional query parameters
            
        Returns:
            API response as dict
            
        Raises:
            KrakenError: On API errors
        """
        url = KRAKEN_REST_BASE + urlpath
        
        logger.debug("[KRAKEN_REST] GET %s params=%s", urlpath, params)
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error("[KRAKEN_REST] HTTP error: %s", e)
                raise KrakenError(f"HTTP error: {e}")
        
        result = resp.json()
        
        # Check for errors
        errors = result.get("error", [])
        if errors:
            error_msg = ", ".join(errors)
            logger.error("[KRAKEN_REST] API error: %s", error_msg)
            raise KrakenError(error_msg)
        
        return result.get("result", {})

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """
        Get ticker information for a trading pair.
        
        Endpoint: GET /0/public/Ticker?pair=XXBTZUSD
        
        Args:
            symbol: Kraken pair symbol (e.g., "XXBTZUSD", "XETHZUSD")
            
        Returns:
            Dict with ticker info:
            {
                "symbol": "XXBTZUSD",
                "last_price": 50000.0,
                "bid": 49990.0,
                "ask": 50010.0,
                "volume_24h": 1234.5,
                "high_24h": 51000.0,
                "low_24h": 49000.0,
                "vwap_24h": 50200.0,
                "trades_24h": 12345,
                "raw": {...}
            }
            
        Example:
            ticker = await client.get_ticker("XXBTZUSD")
            print(f"BTC price: ${ticker['last_price']}")
        """
        result = await self._rest_public_get("/0/public/Ticker", {"pair": symbol})
        
        if not result:
            raise KrakenError(f"No ticker data for {symbol}")
        
        # Result is keyed by pair name (may differ from input)
        pair_key = list(result.keys())[0]
        data = result[pair_key]
        
        # Parse ticker data
        # a = ask (price, whole lot volume, lot volume)
        # b = bid (price, whole lot volume, lot volume)
        # c = last trade (price, lot volume)
        # v = volume (today, 24h)
        # p = vwap (today, 24h)
        # t = trades (today, 24h)
        # h = high (today, 24h)
        # l = low (today, 24h)
        # o = open (today, 24h)
        
        ticker = {
            "symbol": symbol,
            "pair_key": pair_key,
            "last_price": float(data["c"][0]),
            "bid": float(data["b"][0]),
            "ask": float(data["a"][0]),
            "volume_24h": float(data["v"][1]),
            "high_24h": float(data["h"][1]),
            "low_24h": float(data["l"][1]),
            "vwap_24h": float(data["p"][1]),
            "trades_24h": int(data["t"][1]),
            "open_24h": float(data["o"]) if isinstance(data["o"], str) else float(data["o"]),
            "raw": data,
        }
        
        logger.info("[KRAKEN_MARKET] Ticker %s | last=%.2f bid=%.2f ask=%.2f vol=%.2f",
                   symbol, ticker["last_price"], ticker["bid"], ticker["ask"], ticker["volume_24h"])
        
        return ticker

    async def get_candles(
        self,
        symbol: str,
        interval: int = 15,
        since: Optional[int] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        Get OHLCV candle data for a trading pair.
        
        Endpoint: GET /0/public/OHLC?pair=XXBTZUSD&interval=15
        
        Args:
            symbol: Kraken pair symbol (e.g., "XXBTZUSD", "XETHZUSD")
            interval: Candle interval in minutes (1, 5, 15, 30, 60, 240, 1440, 10080)
            since: Return candles since this UNIX timestamp
            limit: Maximum candles to return (Kraken returns up to 720)
            
        Returns:
            List of candle dicts, sorted ascending by timestamp:
            [
                {
                    "ts": 1699999200000,  # Timestamp in milliseconds
                    "open": 50000.0,
                    "high": 50500.0,
                    "low": 49800.0,
                    "close": 50200.0,
                    "volume": 123.45,
                    "vwap": 50150.0,
                    "trades": 456
                },
                ...
            ]
            
        Example:
            candles = await client.get_candles("XXBTZUSD", interval=60, limit=100)
            latest = candles[-1]
            print(f"Latest close: ${latest['close']}")
        """
        params: Dict[str, Any] = {
            "pair": symbol,
            "interval": interval,
        }
        if since is not None:
            params["since"] = since
        
        result = await self._rest_public_get("/0/public/OHLC", params)
        
        if not result:
            raise KrakenError(f"No candle data for {symbol}")
        
        # Result is keyed by pair name, plus "last" (last timestamp)
        pair_key = [k for k in result.keys() if k != "last"][0]
        raw_candles = result[pair_key]
        
        # Parse candles: [timestamp, open, high, low, close, vwap, volume, count]
        candles: List[Dict[str, Any]] = []
        for c in raw_candles:
            candles.append({
                "ts": int(c[0]) * 1000,  # Convert to milliseconds
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "vwap": float(c[5]),
                "volume": float(c[6]),
                "trades": int(c[7]),
            })
        
        # Sort ascending by timestamp
        candles.sort(key=lambda x: x["ts"])
        
        # Limit results
        if len(candles) > limit:
            candles = candles[-limit:]
        
        logger.info("[KRAKEN_MARKET] Candles %s %dm | fetched %d candles",
                   symbol, interval, len(candles))
        
        return candles

    async def get_candles_tf(
        self,
        symbol: str,
        tf: str = "15m",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        Get candles with timeframe string (convenience wrapper).
        
        Args:
            symbol: Kraken pair symbol
            tf: Timeframe string (e.g., "1m", "5m", "15m", "1h", "4h", "1d")
            limit: Maximum candles
            
        Returns:
            List of candle dicts
        """
        tf_map = {
            "1m": 1, "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "4h": 240, "1d": 1440, "1w": 10080,
        }
        interval = tf_map.get(tf.lower(), 15)
        return await self.get_candles(symbol, interval=interval, limit=limit)

    async def get_server_time(self) -> Dict[str, Any]:
        """
        Get Kraken server time.
        
        Useful for checking connectivity and time sync.
        
        Returns:
            {"unixtime": 1699999999, "rfc1123": "Sun, 14 Nov 2024 ..."}
        """
        result = await self._rest_public_get("/0/public/Time")
        logger.debug("[KRAKEN_REST] Server time: %s", result)
        return result

    async def get_asset_pairs(self, pairs: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Get tradeable asset pair info.
        
        Args:
            pairs: Optional list of pairs to filter (e.g., ["XXBTZUSD", "XETHZUSD"])
            
        Returns:
            Dict of pair info with trading rules, fees, etc.
        """
        params = {}
        if pairs:
            params["pair"] = ",".join(pairs)
        
        result = await self._rest_public_get("/0/public/AssetPairs", params or None)
        logger.debug("[KRAKEN_REST] AssetPairs: %d pairs", len(result))
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # WEBSOCKET CONNECTION — KRAKEN FUTURES (Challenge/Response Auth)
    # ══════════════════════════════════════════════════════════════════════════

    def _sign_challenge(self, challenge: str) -> str:
        """
        Sign a WebSocket challenge for Kraken Futures authentication.
        
        Kraken Futures Derivatives WebSocket signature algorithm:
          signed_challenge = base64( HMAC_SHA512( base64_decode(api_secret), SHA256(challenge) ) )
        
        Steps:
          1. SHA256 hash the challenge string -> 32 bytes
          2. HMAC-SHA512 that hash using base64-decoded api_secret
          3. Base64 encode the result
        
        Reference: https://docs.futures.kraken.com/#websocket-api-authentication
        
        Args:
            challenge: Challenge string from server (UUID format)
            
        Returns:
            Base64 encoded signature string
        """
        # Step 1: SHA256 hash of the challenge message
        sha256_hash = hashlib.sha256(challenge.encode()).digest()
        
        # Step 2: HMAC-SHA512 using decoded secret
        secret_decoded = base64.b64decode(self.api_secret)
        signature = hmac.new(secret_decoded, sha256_hash, hashlib.sha512)
        
        # Step 3: Base64 encode
        return base64.b64encode(signature.digest()).decode()

    async def _wait_for_event(
        self,
        expected_event: str,
        timeout: float = 10.0,
        max_messages: int = 20,
    ) -> Dict[str, Any]:
        """
        Wait for a specific event type, ignoring info/heartbeat messages.
        
        Kraken Futures WebSocket sends various messages upon connection:
        - {"event": "info", "version": 1} - Initial info message (EXPECTED, not an error!)
        - {"event": "subscribed", ...} - Subscription confirmations
        - Heartbeats and other system messages
        
        This method loops until the expected event is received or timeout.
        
        Args:
            expected_event: The event type to wait for (e.g., "challenge")
            timeout: Total timeout in seconds
            max_messages: Maximum messages to process before giving up
            
        Returns:
            The message dict containing the expected event
            
        Raises:
            KrakenAuthError: On timeout, error event, or max messages exceeded
        """
        # Events to silently skip (informational, not errors)
        SKIP_EVENTS = {"info", "subscribed", "heartbeat", "pong"}
        
        start_time = asyncio.get_event_loop().time()
        messages_processed = 0
        skipped_events = []  # Track skipped events for debugging
        
        logger.debug(
            "[KRAKEN_WS_AUTH] Waiting for '%s' event (timeout=%.1fs, max_msg=%d)",
            expected_event, timeout, max_messages
        )
        
        while messages_processed < max_messages:
            remaining_time = timeout - (asyncio.get_event_loop().time() - start_time)
            if remaining_time <= 0:
                raise KrakenAuthError(
                    f"Timeout waiting for '{expected_event}' event after {timeout}s "
                    f"(processed {messages_processed} messages, skipped: {skipped_events})"
                )
            
            try:
                response = await asyncio.wait_for(
                    self._ws.recv(),
                    timeout=min(remaining_time, 5.0)
                )
            except asyncio.TimeoutError:
                raise KrakenAuthError(
                    f"Timeout waiting for '{expected_event}' event - no response from server"
                )
            
            messages_processed += 1
            
            # Parse message defensively
            try:
                data = json.loads(response)
            except json.JSONDecodeError as e:
                logger.warning(
                    "[KRAKEN_WS_AUTH] Non-JSON message received (msg #%d): %s",
                    messages_processed, str(response)[:100]
                )
                continue
            
            event_type = data.get("event", "")
            
            # Log received message at DEBUG level (excluding secrets)
            safe_log_data = {k: v for k, v in data.items() if k not in ("api_key", "signed_challenge")}
            logger.debug(
                "[KRAKEN_WS_AUTH] Message #%d received: event='%s' | data=%s",
                messages_processed, event_type or "(none)", str(safe_log_data)[:200]
            )
            
            # Check for error event
            if event_type == "error":
                error_msg = data.get("message", data.get("error", "Unknown error"))
                logger.error("[KRAKEN_WS_AUTH] Server returned error: %s", error_msg)
                raise KrakenAuthError(f"Server error while waiting for '{expected_event}': {error_msg}")
            
            # Check if this is the expected event
            if event_type == expected_event:
                logger.debug(
                    "[KRAKEN_WS_AUTH] Found expected '%s' event after %d messages",
                    expected_event, messages_processed
                )
                return data
            
            # Skip informational events (this is NORMAL, not an error)
            if event_type in SKIP_EVENTS:
                skipped_events.append(event_type)
                logger.debug(
                    "[KRAKEN_WS_AUTH] Skipping '%s' event (normal), continuing to wait for '%s'... "
                    "(This is expected - Kraken sends 'info' on connect)",
                    event_type, expected_event
                )
                continue
            
            # Unknown event - log and continue (defensive)
            if event_type:
                skipped_events.append(f"unknown:{event_type}")
                logger.debug(
                    "[KRAKEN_WS_AUTH] Unexpected event '%s', continuing to wait for '%s'",
                    event_type, expected_event
                )
                continue
            
            # Message without event field - could be channel data, skip
            logger.debug("[KRAKEN_WS_AUTH] Message without 'event' field (may be data), skipping")
        
        raise KrakenAuthError(
            f"Max messages ({max_messages}) exceeded while waiting for '{expected_event}' event "
            f"(skipped events: {skipped_events})"
        )

    async def _authenticate_ws_futures(self) -> bool:
        """
        Authenticate WebSocket connection using Kraken Futures challenge/response.
        
        Kraken Futures WS authentication flow (per official docs):
        1. Upon connection, server may send {"event": "info", "version": 1}
        2. Client sends challenge request: {"event": "challenge", "api_key": "..."}
        3. Server responds with {"event": "challenge", "message": "<uuid>"}
        4. Client signs challenge: base64(HMAC-SHA512(decode64(secret), SHA256(challenge)))
        5. Client SUBSCRIBES to a private feed with the signature:
           {"event": "subscribe", "feed": "open_orders", "api_key": ..., 
            "original_challenge": ..., "signed_challenge": ...}
        6. Server responds with {"event": "subscribed", "feed": "open_orders"}
        
        NOTE: Kraken requires subscribing to a private feed to complete auth,
        not sending back a challenge event!
        
        This implementation handles:
        - Initial "info" messages sent upon connection
        - Heartbeats and other system messages
        - Defensive parsing with clear error messages
        
        Returns:
            True if authentication successful
            
        Raises:
            KrakenAuthError: On authentication failure
        """
        if not self._ws:
            raise KrakenAuthError("WebSocket not connected")
        
        logger.info("[KRAKEN_WS_AUTH] Starting Kraken Futures challenge/response auth...")
        
        # Step 1: Request challenge
        challenge_request = {
            "event": "challenge",
            "api_key": self.api_key
        }
        logger.debug("[KRAKEN_WS_AUTH] Sending challenge request (api_key=%s...)", self.api_key[:8] if self.api_key else "NONE")
        await self._ws.send(json.dumps(challenge_request))
        
        # Step 2: Wait for challenge response (handles info/heartbeat messages)
        logger.debug("[KRAKEN_WS_AUTH] Waiting for challenge response...")
        try:
            data = await self._wait_for_event("challenge", timeout=15.0)
        except KrakenAuthError:
            raise
        
        # Step 3: Extract and validate challenge message
        challenge_message = data.get("message", "")
        if not challenge_message:
            raise KrakenAuthError(f"Empty challenge message received: {data}")
        
        # Check if already authenticated (edge case - shouldn't happen)
        if challenge_message.lower() == "authenticated":
            logger.info("[KRAKEN_WS_AUTH] Already authenticated (unexpected but valid)")
            return True
        
        logger.debug("[KRAKEN_WS_AUTH] Challenge received: %s", challenge_message[:20] + "...")
        
        # Step 4: Sign the challenge
        try:
            signed = self._sign_challenge(challenge_message)
        except Exception as e:
            raise KrakenAuthError(f"Failed to sign challenge: {e}")
        
        # Step 5: Subscribe to a private feed with signature (THIS IS THE KEY!)
        # Kraken Futures requires subscribing to ANY private feed to complete auth
        subscribe_message = {
            "event": "subscribe",
            "feed": "open_orders",  # Any private feed works
            "api_key": self.api_key,
            "original_challenge": challenge_message,
            "signed_challenge": signed
        }
        logger.debug("[KRAKEN_WS_AUTH] Subscribing to private feed with signed challenge...")
        await self._ws.send(json.dumps(subscribe_message))
        
        # Step 6: Wait for subscription confirmation
        logger.debug("[KRAKEN_WS_AUTH] Waiting for subscription confirmation...")
        try:
            sub_data = await self._wait_for_event("subscribed", timeout=10.0)
        except KrakenAuthError as e:
            # Provide more context on auth failure
            raise KrakenAuthError(f"Subscription/auth failed: {e}")
        
        # Verify we subscribed to the expected feed
        subscribed_feed = sub_data.get("feed", "")
        logger.info(
            "[KRAKEN_WS_AUTH] Authenticated successfully (subscribed to '%s')",
            subscribed_feed
        )
        return True

    async def connect(self) -> None:
        """
        Connect to Kraken Futures WebSocket with challenge/response authentication.
        
        Kraken Futures uses a different auth mechanism than Kraken Spot:
        - NO token required (unlike Spot)
        - Uses challenge/response with HMAC-SHA256 signature
        
        Steps:
        1. Connect to Futures WebSocket endpoint
        2. Send challenge request with API key
        3. Sign received challenge with HMAC-SHA256(challenge, secret)
        4. Send signed challenge
        5. Start message receiver task
        
        Raises:
            KrakenAuthError: On authentication failure
            ConnectionError: On WebSocket connection failure
        """
        if self._connected:
            logger.warning("[KRAKEN_WS] Already connected")
            return
        
        from backend.kraken_futures_rest_v2 import validate_kraken_urls_for_mode
        validate_kraken_urls_for_mode(self.use_demo, self.base_url, self.ws_url)

        # Connect to Kraken FUTURES WebSocket (strictly pinned to LIVE or DEMO env)
        logger.info("[KRAKEN_WS] Connecting to Kraken Futures (%s): %s", self.environment, self.ws_url)
        try:
            self._ws = await websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
            )
        except Exception as e:
            logger.error("[KRAKEN_WS] Connection failed: %s", e)
            raise ConnectionError(f"WebSocket connection failed: {e}")
        
        # Authenticate using challenge/response
        logger.info("[KRAKEN_WS] Authenticating via challenge/response...")
        try:
            await self._authenticate_ws_futures()
        except KrakenAuthError as e:
            logger.error("[KRAKEN_WS] Authentication failed: %s", e)
            await self._ws.close()
            self._ws = None
            raise
        
        self._connected = True
        self._running = True
        self._ws_token = None  # Not used for Futures auth
        self._token_obtained_at = time.time()
        
        # Start receiver task
        self._recv_task = asyncio.create_task(self._receive_loop())
        
        # Note: Token refresh not needed for Futures challenge auth
        # Futures WebSocket stays authenticated until disconnect
        
        # Resubscribe to channels
        for sub in self._subscriptions:
            await self._send_subscription(sub)
        
        logger.info("[KRAKEN_WS] Connected and authenticated successfully")

    async def disconnect(self) -> None:
        """Gracefully disconnect from WebSocket."""
        self._running = False
        
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        
        if self._token_refresh_task:
            self._token_refresh_task.cancel()
            try:
                await self._token_refresh_task
            except asyncio.CancelledError:
                pass
        
        if self._ws:
            await self._ws.close()
            self._ws = None
        
        self._connected = False
        logger.info("[KRAKEN_WS] Disconnected")

    async def _receive_loop(self) -> None:
        """Background task to receive and process WebSocket messages."""
        while self._running and self._ws:
            try:
                message = await self._ws.recv()
                data = json.loads(message)
                await self._handle_message(data)
            except websockets.ConnectionClosed as e:
                logger.warning("[KRAKEN_WS] Connection closed: %s", e)
                self._connected = False
                if self.on_disconnect_callback:
                    try:
                        cb_res = self.on_disconnect_callback(str(e))
                        if asyncio.iscoroutine(cb_res):
                            asyncio.create_task(cb_res)
                    except Exception as cb_err:
                        logger.error("[KRAKEN_WS] on_disconnect_callback error: %s", cb_err)
                if self._running and self.auto_reconnect:
                    await self._reconnect()
                break
            except json.JSONDecodeError as e:
                logger.error("[KRAKEN_WS] JSON decode error: %s", e)
            except Exception as e:
                logger.error("[KRAKEN_WS] Receive error: %s", e)

    async def _reconnect(self) -> None:
        """Attempt to reconnect after disconnect with bounded retries and exponential backoff."""
        MAX_RECONNECT_ATTEMPTS = 10
        backoff_steps = [1, 2, 5, 10, 20, 30, 60, 60, 60, 60]
        attempts = 0
        
        while self._running and attempts < MAX_RECONNECT_ATTEMPTS:
            delay = backoff_steps[min(attempts, len(backoff_steps) - 1)]
            logger.info(
                "[KRAKEN_WS] Reconnecting in %ds (attempt %d/%d)...",
                delay, attempts + 1, MAX_RECONNECT_ATTEMPTS,
            )
            await asyncio.sleep(delay)
            
            try:
                await self.connect()
                logger.info("[KRAKEN_WS] Reconnected successfully after %d attempts", attempts + 1)
                if self.on_reconnect_callback:
                    try:
                        cb_res = self.on_reconnect_callback()
                        if asyncio.iscoroutine(cb_res):
                            asyncio.create_task(cb_res)
                    except Exception as cb_err:
                        logger.error("[KRAKEN_WS] on_reconnect_callback error: %s", cb_err)
                return
            except Exception as e:
                logger.error("[KRAKEN_WS] Reconnect failed (attempt %d/%d): %s",
                             attempts + 1, MAX_RECONNECT_ATTEMPTS, e)
                attempts += 1
        
        if self._running:
            logger.critical(
                "[KRAKEN_WS] RECONNECT EXHAUSTED after %d attempts — "
                "WebSocket is DOWN. Manual intervention required.",
                MAX_RECONNECT_ATTEMPTS,
            )
            if self.on_disconnect_callback:
                try:
                    cb_res = self.on_disconnect_callback("WS_RECONNECT_EXHAUSTED")
                    if asyncio.iscoroutine(cb_res):
                        asyncio.create_task(cb_res)
                except Exception as cb_err:
                    logger.error("[KRAKEN_WS] on_disconnect_callback (exhausted) error: %s", cb_err)

    async def _token_refresh_loop(self) -> None:
        """Background task to refresh WebSocket token before expiry."""
        while self._running:
            await asyncio.sleep(TOKEN_REFRESH_INTERVAL)
            
            if not self._running:
                break
            
            try:
                logger.info("[KRAKEN_WS] Refreshing token...")
                self._ws_token = await self._get_websocket_token()
                logger.info("[KRAKEN_WS] Token refreshed")
            except Exception as e:
                logger.error("[KRAKEN_WS] Token refresh failed: %s", e)

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """
        Handle incoming WebSocket message.
        
        Dispatches to appropriate handler based on message type.
        """
        # Check if this is a response to a pending request
        req_id = data.get("req_id")
        if req_id is not None and req_id in self._pending_requests:
            future = self._pending_requests.pop(req_id)
            if not future.done():
                future.set_result(data)
            return
        
        # Handle channel messages
        channel = data.get("channel")
        method = data.get("method")
        
        if channel:
            logger.debug("[KRAKEN_WS] Channel message: %s", channel)
        elif method:
            logger.debug("[KRAKEN_WS] Method response: %s", method)
        
        # Call user callback if registered
        if self.on_message_callback:
            try:
                self.on_message_callback(data)
            except Exception as e:
                logger.error("[KRAKEN_WS] Callback error: %s", e)

    # ══════════════════════════════════════════════════════════════════════════
    # SUBSCRIPTIONS
    # ══════════════════════════════════════════════════════════════════════════

    async def subscribe_executions(self) -> None:
        """Subscribe to execution reports channel."""
        await self._subscribe("executions", snapshot=True)

    async def subscribe_balances(self) -> None:
        """Subscribe to balances channel."""
        await self._subscribe("balances", snapshot=True)

    async def _subscribe(self, channel: str, **params: Any) -> None:
        """Subscribe to a private channel."""
        sub = {
            "method": "subscribe",
            "params": {
                "channel": channel,
                "token": self._ws_token,
                **params,
            },
        }
        
        # Store for resubscription on reconnect
        self._subscriptions.append(sub)
        await self._send_subscription(sub)

    async def _send_subscription(self, sub: Dict[str, Any]) -> None:
        """Send subscription message."""
        if self._ws and self._connected:
            # Update token in case it was refreshed
            if "params" in sub:
                sub["params"]["token"] = self._ws_token
            await self._ws.send(json.dumps(sub))
            logger.info("[KRAKEN_WS] Subscribed to %s", sub["params"].get("channel"))

    # ══════════════════════════════════════════════════════════════════════════
    # ORDER METHODS
    # ══════════════════════════════════════════════════════════════════════════

    def _get_next_req_id(self) -> int:
        """Generate next request ID."""
        self._req_id_counter += 1
        return self._req_id_counter

    async def _send_request(
        self,
        method: str,
        params: Dict[str, Any],
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Send request and wait for response.
        
        Args:
            method: WebSocket method (e.g., "add_order")
            params: Request parameters
            timeout: Response timeout
            
        Returns:
            Response data
            
        Raises:
            ConnectionError: If not connected
            asyncio.TimeoutError: On response timeout
        """
        if not self._connected or not self._ws:
            raise ConnectionError("Not connected to WebSocket")
        
        req_id = self._get_next_req_id()
        
        message = {
            "method": method,
            "params": {
                **params,
                "token": self._ws_token,
            },
            "req_id": req_id,
        }
        
        # Create future for response
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = future
        
        logger.debug("[KRAKEN_WS] Sending %s (req_id=%d)", method, req_id)
        await self._ws.send(json.dumps(message))
        
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            raise

    async def send_order_kraken(
        self,
        symbol: str,
        side: Union[str, OrderSide],
        order_type: Union[str, OrderType],
        quantity: float,
        price: Optional[float] = None,
        time_in_force: Union[str, TimeInForce] = TimeInForce.GTC,
        margin: bool = False,
        post_only: bool = False,
        reduce_only: bool = False,
        cl_ord_id: Optional[str] = None,
        validate_only: bool = False,
        trigger_price: Optional[float] = None,
        trigger_reference: Union[str, TriggerReference] = TriggerReference.LAST,
        conditional_order_type: Optional[Union[str, OrderType]] = None,
        conditional_limit_price: Optional[float] = None,
        conditional_trigger_price: Optional[float] = None,
    ) -> KrakenOrderResponse:
        """
        Send order via Kraken WebSocket V2 add_order.
        
        Args:
            symbol: Trading pair (e.g., "BTC/USD", "ETH/USD")
            side: Order side ("buy" or "sell")
            order_type: Order type ("market", "limit", "stop-loss", etc.)
            quantity: Order quantity in base asset
            price: Limit price (required for limit orders)
            time_in_force: GTC, GTD, or IOC
            margin: Use margin trading
            post_only: Cancel if would take liquidity
            reduce_only: Only reduce existing position
            cl_ord_id: Client order ID
            validate_only: Validate without executing
            trigger_price: Trigger price for stop/TP orders
            trigger_reference: Reference for trigger ("index" or "last")
            conditional_order_type: OTO secondary order type
            conditional_limit_price: OTO secondary limit price
            conditional_trigger_price: OTO secondary trigger price
            
        Returns:
            KrakenOrderResponse with order details or error
            
        Raises:
            KrakenOrderError: On order error
            ConnectionError: If not connected
            
        Example:
            # Limit buy order
            result = await client.send_order_kraken(
                symbol="BTC/USD",
                side="buy",
                order_type="limit",
                quantity=0.001,
                price=50000.0,
            )
            
            # Market sell order
            result = await client.send_order_kraken(
                symbol="ETH/USD",
                side="sell",
                order_type="market",
                quantity=0.1,
            )
            
            # Stop-loss order
            result = await client.send_order_kraken(
                symbol="BTC/USD",
                side="sell",
                order_type="stop-loss",
                quantity=0.001,
                trigger_price=45000.0,
            )
        """
        # Normalize enums
        if isinstance(side, str):
            side = OrderSide(side.lower())
        if isinstance(order_type, str):
            order_type = OrderType(order_type.lower())
        if isinstance(time_in_force, str):
            time_in_force = TimeInForce(time_in_force.lower())
        if isinstance(trigger_reference, str):
            trigger_reference = TriggerReference(trigger_reference.lower())
        
        # Build params
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side.value,
            "order_type": order_type.value,
            "order_qty": quantity,
            "time_in_force": time_in_force.value,
        }
        
        # Add optional params
        if price is not None:
            params["limit_price"] = price
        if margin:
            params["margin"] = True
        if post_only:
            params["post_only"] = True
        if reduce_only:
            params["reduce_only"] = True
        if cl_ord_id:
            params["cl_ord_id"] = cl_ord_id
        if validate_only:
            params["validate"] = True
        
        # Add trigger params for stop/TP orders
        if trigger_price is not None:
            params["triggers"] = {
                "price": trigger_price,
                "price_type": "static",
                "reference": trigger_reference.value,
            }
        
        # Add conditional (OTO) params
        if conditional_order_type is not None:
            if isinstance(conditional_order_type, str):
                conditional_order_type = OrderType(conditional_order_type.lower())
            params["conditional"] = {
                "order_type": conditional_order_type.value,
            }
            if conditional_limit_price is not None:
                params["conditional"]["limit_price"] = conditional_limit_price
            if conditional_trigger_price is not None:
                params["conditional"]["trigger_price"] = conditional_trigger_price
        
        logger.info(
            "[KRAKEN_ORDER] %s %s %s qty=%s price=%s",
            order_type.value.upper(),
            side.value.upper(),
            symbol,
            quantity,
            price,
        )
        
        try:
            response = await self._send_request("add_order", params)
        except asyncio.TimeoutError:
            logger.error("[KRAKEN_ORDER] Timeout waiting for response")
            raise KrakenOrderError("Order request timeout")
        except Exception as e:
            logger.error("[KRAKEN_ORDER] Request error: %s", e)
            raise
        
        # Parse response
        return self._parse_order_response(response)

    def _parse_order_response(self, response: Dict[str, Any]) -> KrakenOrderResponse:
        """Parse add_order response."""
        success = response.get("success", False)
        error = response.get("error")
        result = response.get("result", {})
        
        if error:
            logger.error("[KRAKEN_ORDER] Error: %s", error)
            
            # Handle specific errors
            if "ESession:Invalid session" in error:
                raise KrakenTokenExpiredError(error)
            elif "EAPI:Rate limit exceeded" in error:
                raise KrakenRateLimitError(error)
            
            return KrakenOrderResponse(
                success=False,
                error=error,
                raw_response=response,
            )
        
        order_response = KrakenOrderResponse(
            success=success,
            order_id=result.get("order_id"),
            cl_ord_id=result.get("cl_ord_id"),
            warnings=result.get("warnings", []),
            time_in=response.get("time_in"),
            time_out=response.get("time_out"),
            raw_response=response,
        )
        
        if success:
            logger.info("[KRAKEN_ORDER] Success: order_id=%s", order_response.order_id)
        
        return order_response

    async def cancel_order(
        self,
        order_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Cancel an order.
        
        Args:
            order_id: Kraken order ID
            cl_ord_id: Client order ID
            
        Returns:
            Cancellation response
        """
        if not order_id and not cl_ord_id:
            raise ValueError("Either order_id or cl_ord_id required")
        
        params: Dict[str, Any] = {}
        if order_id:
            params["order_id"] = order_id
        if cl_ord_id:
            params["cl_ord_id"] = cl_ord_id
        
        logger.info("[KRAKEN_ORDER] Cancelling order: %s", order_id or cl_ord_id)
        
        return await self._send_request("cancel_order", params)

    async def cancel_all_orders(self) -> Dict[str, Any]:
        """Cancel all open orders."""
        logger.info("[KRAKEN_ORDER] Cancelling all orders")
        return await self._send_request("cancel_all", {})

    # ══════════════════════════════════════════════════════════════════════════
    # CONVENIENCE METHODS
    # ══════════════════════════════════════════════════════════════════════════

    async def send_market_order(
        self,
        symbol: str,
        side: Union[str, OrderSide],
        quantity: float,
        margin: bool = False,
        reduce_only: bool = False,
    ) -> KrakenOrderResponse:
        """
        Send a market order.
        
        Args:
            symbol: Trading pair
            side: "buy" or "sell"
            quantity: Order quantity
            margin: Use margin
            reduce_only: Reduce only
            
        Returns:
            Order response
        """
        return await self.send_order_kraken(
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
            margin=margin,
            reduce_only=reduce_only,
        )

    async def send_limit_order(
        self,
        symbol: str,
        side: Union[str, OrderSide],
        quantity: float,
        price: float,
        margin: bool = False,
        post_only: bool = False,
        reduce_only: bool = False,
        time_in_force: Union[str, TimeInForce] = TimeInForce.GTC,
    ) -> KrakenOrderResponse:
        """
        Send a limit order.
        
        Args:
            symbol: Trading pair
            side: "buy" or "sell"
            quantity: Order quantity
            price: Limit price
            margin: Use margin
            post_only: Post-only mode
            reduce_only: Reduce only
            time_in_force: Time in force
            
        Returns:
            Order response
        """
        return await self.send_order_kraken(
            symbol=symbol,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            price=price,
            margin=margin,
            post_only=post_only,
            reduce_only=reduce_only,
            time_in_force=time_in_force,
        )

    async def send_stop_loss_order(
        self,
        symbol: str,
        side: Union[str, OrderSide],
        quantity: float,
        trigger_price: float,
        limit_price: Optional[float] = None,
        margin: bool = False,
        reduce_only: bool = True,
    ) -> KrakenOrderResponse:
        """
        Send a stop-loss order.
        
        Args:
            symbol: Trading pair
            side: "buy" or "sell"
            quantity: Order quantity
            trigger_price: Stop trigger price
            limit_price: Limit price (for stop-limit)
            margin: Use margin
            reduce_only: Reduce only (default True)
            
        Returns:
            Order response
        """
        order_type = OrderType.STOP_LOSS_LIMIT if limit_price else OrderType.STOP_LOSS
        
        return await self.send_order_kraken(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=limit_price,
            trigger_price=trigger_price,
            margin=margin,
            reduce_only=reduce_only,
        )

    async def send_take_profit_order(
        self,
        symbol: str,
        side: Union[str, OrderSide],
        quantity: float,
        trigger_price: float,
        limit_price: Optional[float] = None,
        margin: bool = False,
        reduce_only: bool = True,
    ) -> KrakenOrderResponse:
        """
        Send a take-profit order.
        
        Args:
            symbol: Trading pair
            side: "buy" or "sell"
            quantity: Order quantity
            trigger_price: Take-profit trigger price
            limit_price: Limit price (for TP-limit)
            margin: Use margin
            reduce_only: Reduce only (default True)
            
        Returns:
            Order response
        """
        order_type = OrderType.TAKE_PROFIT_LIMIT if limit_price else OrderType.TAKE_PROFIT
        
        return await self.send_order_kraken(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=limit_price,
            trigger_price=trigger_price,
            margin=margin,
            reduce_only=reduce_only,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # CONTEXT MANAGER
    # ══════════════════════════════════════════════════════════════════════════

    async def __aenter__(self) -> "KrakenFuturesClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.disconnect()

    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._connected
