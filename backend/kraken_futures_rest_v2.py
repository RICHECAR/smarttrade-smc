"""
Kraken Futures REST Client V2 — Fixed Implementation
═════════════════════════════════════════════════════

⚠️  Ce client est pour Kraken FUTURES uniquement!
    
    Base URL: https://futures.kraken.com
    Docs: https://docs.futures.kraken.com/
    API Keys: https://futures.kraken.com/trade/settings/api (PAS kraken.com!)

Algorithme de signature Kraken Futures:
  1. message = postData + nonce + endpoint (endpoint = /api/v3/...)
  2. hash = SHA256(message)  
  3. signature = HMAC-SHA512(hash, base64_decode(secret))
  4. return base64_encode(signature)

Headers requis:
  - APIKey: Clé publique
  - Nonce: Timestamp millisecondes
  - Authent: Signature base64
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

from core.rate_limiter import get_rate_limiter

load_dotenv()

logger = logging.getLogger("smarttrade.kraken_futures_rest")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

KRAKEN_FUTURES_LIVE_REST_URL = "https://futures.kraken.com"
KRAKEN_FUTURES_DEMO_REST_URL = "https://demo-futures.kraken.com"
KRAKEN_FUTURES_LIVE_WS_URL = "wss://futures.kraken.com/ws/v1"
KRAKEN_FUTURES_DEMO_WS_URL = "wss://demo-futures.kraken.com/ws/v1"
KRAKEN_FUTURES_BASE_URL = KRAKEN_FUTURES_LIVE_REST_URL
KRAKEN_FUTURES_API_VERSION = "/derivatives/api/v3"  # URL path
KRAKEN_FUTURES_SIGN_PATH = "/api/v3"  # Path for SIGNATURE (without /derivatives)
DEFAULT_TIMEOUT = 15.0


class KrakenEnvironmentConfigError(ValueError):
    """Fail-closed error raised on invalid, ambiguous, or mixed LIVE/DEMO Kraken config."""


def parse_kraken_use_demo(use_demo: Optional[bool] = None) -> bool:
    """
    Explicitly resolve LIVE (False) vs DEMO (True) mode.
    Never infers mode from API key presence; rejects ambiguous env values.
    """
    if use_demo is not None:
        if not isinstance(use_demo, bool):
            raise KrakenEnvironmentConfigError(
                f"KRAKEN_USE_DEMO must be bool or None, got {type(use_demo).__name__}"
            )
        return use_demo
    raw = os.getenv("KRAKEN_USE_DEMO")
    if raw is None or raw.strip() == "":
        return False
    norm = raw.strip().lower()
    if norm in ("true", "1", "yes"):
        return True
    if norm in ("false", "0", "no"):
        return False
    raise KrakenEnvironmentConfigError(
        f"Ambiguous KRAKEN_USE_DEMO value: {raw!r} (expected true/false/1/0/yes/no)"
    )


def validate_kraken_urls_for_mode(
    use_demo: bool,
    rest_url: str,
    ws_url: Optional[str] = None,
) -> None:
    """
    Enforce strict URL isolation between LIVE and DEMO.
    DEMO must ONLY use demo-futures.kraken.com; LIVE must ONLY use futures.kraken.com.
    """
    rest_clean = (rest_url or "").strip().rstrip("/")
    if not rest_clean.startswith("https://"):
        raise KrakenEnvironmentConfigError(f"Invalid Kraken REST URL: {rest_url!r}")
    rest_host = urllib.parse.urlparse(rest_clean).hostname or ""

    if use_demo:
        if rest_host != "demo-futures.kraken.com":
            raise KrakenEnvironmentConfigError(
                f"DEMO_LIVE_FALLBACK_BLOCKED: DEMO mode requires demo-futures.kraken.com, got {rest_url!r}"
            )
    else:
        if rest_host != "futures.kraken.com":
            raise KrakenEnvironmentConfigError(
                f"LIVE_DEMO_MIX_BLOCKED: LIVE mode requires futures.kraken.com, got {rest_url!r}"
            )

    if ws_url is not None:
        ws_clean = (ws_url or "").strip()
        if not ws_clean.startswith("wss://"):
            raise KrakenEnvironmentConfigError(f"Invalid Kraken WebSocket URL: {ws_url!r}")
        ws_host = urllib.parse.urlparse(ws_clean).hostname or ""
        if use_demo and ws_host != "demo-futures.kraken.com":
            raise KrakenEnvironmentConfigError(
                f"DEMO_LIVE_FALLBACK_BLOCKED: DEMO mode requires wss://demo-futures.kraken.com/..., got {ws_url!r}"
            )
        if not use_demo and ws_host != "futures.kraken.com":
            raise KrakenEnvironmentConfigError(
                f"LIVE_DEMO_MIX_BLOCKED: LIVE mode requires wss://futures.kraken.com/..., got {ws_url!r}"
            )


def validate_kraken_client_environment(
    client: Any,
    expected_use_demo: bool,
    label: str = "client",
) -> None:
    """Verify a pre-initialized REST/WS client matches the requested LIVE/DEMO environment."""
    if client is None:
        return
    client_demo = getattr(client, "use_demo", None)
    if client_demo is not None and bool(client_demo) != bool(expected_use_demo):
        raise KrakenEnvironmentConfigError(
            f"ENVIRONMENT_MISMATCH: {label} has use_demo={client_demo}, expected use_demo={expected_use_demo}"
        )
    client_rest = getattr(client, "base_url", None)
    client_ws = getattr(client, "ws_url", None)
    if client_rest is not None:
        validate_kraken_urls_for_mode(expected_use_demo, client_rest, client_ws)
    elif client_ws is not None:
        expected_rest = KRAKEN_FUTURES_DEMO_REST_URL if expected_use_demo else KRAKEN_FUTURES_LIVE_REST_URL
        validate_kraken_urls_for_mode(expected_use_demo, expected_rest, client_ws)


def resolve_kraken_environment(
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    use_demo: Optional[bool] = None,
    base_url: Optional[str] = None,
    ws_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Centralized, fail-closed LIVE / DEMO resolver for all Kraken Futures clients.
    Guarantees zero fallback from DEMO to LIVE credentials or URLs, and never
    leaks DEMO credentials into LIVE mode.
    """
    resolved_demo = parse_kraken_use_demo(use_demo)

    if resolved_demo:
        resolved_key = api_key if api_key is not None else (os.getenv("KRAKEN_DEMO_API_KEY") or "")
        resolved_secret = api_secret if api_secret is not None else (os.getenv("KRAKEN_DEMO_API_SECRET") or "")
        if not resolved_key or not resolved_key.strip():
            raise KrakenEnvironmentConfigError(
                "DEMO_CREDENTIALS_MISSING: KRAKEN_USE_DEMO=true requires KRAKEN_DEMO_API_KEY (fallback to LIVE forbidden)"
            )
        if not resolved_secret or not resolved_secret.strip():
            raise KrakenEnvironmentConfigError(
                "DEMO_CREDENTIALS_MISSING: KRAKEN_USE_DEMO=true requires KRAKEN_DEMO_API_SECRET (fallback to LIVE forbidden)"
            )
        live_env_key = os.getenv("KRAKEN_API_KEY") or os.getenv("KRAKEN_FUTURES_API_KEY")
        demo_env_key = os.getenv("KRAKEN_DEMO_API_KEY")
        if live_env_key and resolved_key == live_env_key and resolved_key != demo_env_key:
            raise KrakenEnvironmentConfigError(
                "DEMO_LIVE_FALLBACK_BLOCKED: Refusing to use LIVE KRAKEN_API_KEY in DEMO mode"
            )
        resolved_rest = base_url if base_url is not None else KRAKEN_FUTURES_DEMO_REST_URL
        resolved_ws = ws_url if ws_url is not None else KRAKEN_FUTURES_DEMO_WS_URL
    else:
        resolved_key = (
            api_key
            if api_key is not None
            else (os.getenv("KRAKEN_FUTURES_API_KEY") or os.getenv("KRAKEN_API_KEY") or "")
        )
        resolved_secret = (
            api_secret
            if api_secret is not None
            else (os.getenv("KRAKEN_FUTURES_API_SECRET") or os.getenv("KRAKEN_API_SECRET") or "")
        )
        resolved_rest = base_url if base_url is not None else KRAKEN_FUTURES_LIVE_REST_URL
        resolved_ws = ws_url if ws_url is not None else KRAKEN_FUTURES_LIVE_WS_URL

    validate_kraken_urls_for_mode(resolved_demo, resolved_rest, resolved_ws)

    return {
        "use_demo": resolved_demo,
        "environment": "DEMO" if resolved_demo else "LIVE",
        "api_key": resolved_key,
        "api_secret": resolved_secret,
        "rest_url": resolved_rest.rstrip("/"),
        "ws_url": resolved_ws,
    }


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class KrakenFuturesBalance:
    """Kraken Futures account balance."""
    available: float = 0.0
    total: float = 0.0                # portfolioValue (includes unrealized PnL)
    balance_value: float = 0.0        # balanceValue (pure collateral, no PnL)
    margin_used: float = 0.0          # initialMargin
    maintenance_margin: float = 0.0   # maintenanceMargin
    unrealized_pnl: float = 0.0       # pnl field from API
    unrealized_funding: float = 0.0   # unrealizedFunding from API
    currency: str = "USD"
    timestamp: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": round(self.available, 2),
            "total": round(self.total, 2),
            "balance_value": round(self.balance_value, 2),
            "margin_used": round(self.margin_used, 2),
            "maintenance_margin": round(self.maintenance_margin, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "unrealized_funding": round(self.unrealized_funding, 4),
            "currency": self.currency,
            "timestamp": self.timestamp or datetime.now(timezone.utc).isoformat()
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
        }


# ══════════════════════════════════════════════════════════════════════════════
# SIGNATURE FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def sign_kraken_futures_request(
    api_secret: str,
    endpoint: str,
    nonce: str,
    post_data: str = ""
) -> str:
    """
    Generate signature for Kraken Futures REST API.
    
    Kraken Futures Signature Algorithm:
    1. Concatenate: postData + nonce + endpoint
    2. SHA256 hash the concatenated string
    3. HMAC-SHA512 sign the hash using base64-decoded secret
    4. Base64 encode the result
    
    IMPORTANT: 
    - endpoint must be /api/v3/... (NOT /derivatives/api/v3/...)
    - nonce is millisecond timestamp as string
    - post_data is URL-encoded string (empty for GET)
    
    Args:
        api_secret: Base64-encoded API secret from Kraken Futures
        endpoint: API endpoint (e.g., /api/v3/accounts)
        nonce: Millisecond timestamp as string
        post_data: URL-encoded POST data (empty string for GET)
        
    Returns:
        Base64-encoded signature string
        
    Raises:
        ValueError: If secret cannot be decoded
    """
    # Step 1: Build message = postData + nonce + endpoint
    message = f"{post_data}{nonce}{endpoint}"
    message_bytes = message.encode("utf-8")
    
    logger.debug("[KRAKEN_SIGN] Message to sign: %s", message[:100])
    
    # Step 2: SHA256 hash
    sha256_hash = hashlib.sha256(message_bytes).digest()
    
    # Step 3: Decode secret from base64
    try:
        secret_bytes = base64.b64decode(api_secret)
    except Exception as e:
        logger.error("[KRAKEN_SIGN] Failed to decode API secret: %s", e)
        raise ValueError(f"Invalid API secret format: {e}")
    
    # Step 4: HMAC-SHA512
    hmac_signature = hmac.new(secret_bytes, sha256_hash, hashlib.sha512).digest()
    
    # Step 5: Base64 encode signature
    signature = base64.b64encode(hmac_signature).decode("utf-8")
    
    logger.debug("[KRAKEN_SIGN] Signature generated: %s...", signature[:20])
    return signature


def build_kraken_futures_headers(
    api_key: str,
    api_secret: str,
    endpoint: str,
    post_data: str = ""
) -> Dict[str, str]:
    """
    Build authenticated headers for Kraken Futures API.
    
    Args:
        api_key: Kraken Futures API public key
        api_secret: Kraken Futures API secret (base64)
        endpoint: API endpoint for signing (e.g., /api/v3/accounts)
        post_data: URL-encoded POST data
        
    Returns:
        Dict with APIKey, Nonce, Authent, Content-Type headers
    """
    nonce = str(int(time.time() * 1000))
    signature = sign_kraken_futures_request(api_secret, endpoint, nonce, post_data)
    
    headers = {
        "APIKey": api_key,
        "Nonce": nonce,
        "Authent": signature,
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "SMC-Trading-Bot/1.0"
    }
    
    logger.debug("[KRAKEN_HEADERS] Key=%s... Nonce=%s", api_key[:12] if api_key else "NONE", nonce)
    return headers


# ══════════════════════════════════════════════════════════════════════════════
# REST CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class KrakenFuturesRESTClientV2:
    """
    Kraken Futures REST API client with corrected authentication.
    
    Usage:
        client = KrakenFuturesRESTClientV2()
        
        # Test connection
        status = await client.test_connection()
        if status["success"]:
            print(f"Connected! Balance: ${status['balance']['available']}")
        
        # Get balance
        balance = await client.get_balance()
        print(f"Available: ${balance.available}")
        
        # Get positions
        positions = await client.get_positions()
        for pos in positions:
            print(f"{pos.symbol}: {pos.size} @ {pos.entry_price}")
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        testnet: bool = False,
        use_demo: Optional[bool] = None,
        base_url: Optional[str] = None,
    ):
        """
        Initialize Kraken Futures REST client with strict LIVE/DEMO isolation.
        """
        effective_demo = True if testnet else use_demo
        env_cfg = resolve_kraken_environment(
            api_key=api_key,
            api_secret=api_secret,
            use_demo=effective_demo,
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
        
        if self.api_key:
            logger.info(
                "[KRAKEN_REST] Client initialized | env=%s | key=%s... | url=%s",
                self.environment, self.api_key[:16], self.base_url
            )
        else:
            logger.warning("[KRAKEN_REST] No API key configured! (env=%s)", self.environment)
    
    # ══════════════════════════════════════════════════════════════════════════
    # CORE REQUEST METHOD
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
        Execute HTTP request to Kraken Futures API.
        
        Args:
            method: HTTP method (GET/POST)
            endpoint: API endpoint without /derivatives prefix (e.g., /accounts)
            params: Query parameters for GET
            data: Body data for POST
            authenticated: Whether to sign the request
            
        Returns:
            Parsed JSON response
            
        Raises:
            ValueError: On API error
            httpx.HTTPError: On network error
        """
        # Runtime guard: guarantee URL still matches LIVE/DEMO mode before every request
        validate_kraken_urls_for_mode(self.use_demo, self.base_url)

        # Build URLs
        # URL uses /derivatives/api/v3, signature uses /api/v3
        url_path = f"{KRAKEN_FUTURES_API_VERSION}{endpoint}"
        sign_path = f"{KRAKEN_FUTURES_SIGN_PATH}{endpoint}"
        full_url = f"{self.base_url}{url_path}"
        
        # Build POST data string
        post_data = ""
        if data:
            post_data = urllib.parse.urlencode(data)
        
        # Build headers
        if authenticated:
            headers = build_kraken_futures_headers(
                self.api_key, self.api_secret, sign_path, post_data
            )
        else:
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        
        logger.info("[KRAKEN_REST] %s %s", method.upper(), url_path)
        
        # Rate limiter: acquire token BEFORE sending request
        limiter_name = "kraken_public" if not authenticated else "kraken_private"
        limiter = get_rate_limiter(limiter_name)
        acquired = await limiter.acquire(timeout=10.0)
        if not acquired:
            raise ValueError(f"Rate limiter timeout — {limiter_name} bucket exhausted")
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                if method.upper() == "GET":
                    response = await client.get(full_url, headers=headers, params=params)
                else:
                    response = await client.post(full_url, headers=headers, content=post_data)
                
                logger.debug("[KRAKEN_REST] HTTP %d", response.status_code)
                
            except httpx.TimeoutException:
                logger.error("[KRAKEN_REST] Request timeout: %s", endpoint)
                raise ValueError(f"Request timeout: {endpoint}")
            except httpx.RequestError as e:
                logger.error("[KRAKEN_REST] Network error: %s", e)
                raise ValueError(f"Network error: {e}")
        
        # Parse response
        try:
            result = response.json()
        except Exception as e:
            logger.error("[KRAKEN_REST] Invalid JSON response: %s", response.text[:200])
            raise ValueError(f"Invalid JSON response: {e}")
        
        # Check for errors
        if result.get("result") == "error":
            error_msg = result.get("error", "Unknown error")
            logger.error("[KRAKEN_REST] API error: %s", error_msg)
            raise ValueError(f"Kraken API error: {error_msg}")
        
        if response.status_code != 200:
            logger.error("[KRAKEN_REST] HTTP %d: %s", response.status_code, response.text[:200])
            raise ValueError(f"HTTP error {response.status_code}")
        
        return result
    
    # ══════════════════════════════════════════════════════════════════════════
    # BALANCE METHODS
    # ══════════════════════════════════════════════════════════════════════════
    
    async def get_accounts_raw(self) -> Dict[str, Any]:
        """
        Get raw accounts data from Kraken Futures.
        
        Endpoint: GET /derivatives/api/v3/accounts
        
        Returns:
            Raw API response with accounts structure
        """
        return await self._request("GET", "/accounts")
    
    async def get_balance(self, force_refresh: bool = False) -> KrakenFuturesBalance:
        """
        Get Kraken Futures account balance.
        
        Parses /accounts response to extract balance info.
        Results are cached for 30 seconds.
        
        Args:
            force_refresh: Bypass cache
            
        Returns:
            KrakenFuturesBalance with available, total, margin_used, pnl
        """
        # Check cache (10s TTL — conservative for active trading decisions)
        cache_age = time.time() - self._balance_cache_time
        if not force_refresh and self._balance_cache and cache_age < 10:
            logger.debug("[KRAKEN_REST] Returning cached balance (age: %.1fs)", cache_age)
            return self._balance_cache
        
        # Fetch fresh data
        logger.info("[KRAKEN_REST] Fetching balance...")
        data = await self.get_accounts_raw()
        
        accounts = data.get("accounts", {})
        server_time = data.get("serverTime", "")
        
        logger.debug("[KRAKEN_REST] Account types: %s", list(accounts.keys()))
        
        # Try flex account first (multi-collateral)
        flex = accounts.get("flex", {})
        if flex:
            balance = KrakenFuturesBalance(
                available=float(flex.get("availableMargin", 0)),
                total=float(flex.get("portfolioValue", 0)),
                balance_value=float(flex.get("balanceValue", 0)),
                margin_used=float(flex.get("initialMargin", 0)),
                maintenance_margin=float(flex.get("maintenanceMargin", 0)),
                unrealized_pnl=float(flex.get("pnl", 0)),
                unrealized_funding=float(flex.get("unrealizedFunding", 0)),
                timestamp=server_time
            )
            logger.info(
                "[KRAKEN_REST] ✓ Balance (flex): available=$%.2f | total=$%.2f | balance_value=$%.2f | pnl=$%.2f",
                balance.available, balance.total, balance.balance_value, balance.unrealized_pnl
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
            logger.info("[KRAKEN_REST] ✓ Balance (cash): available=$%.2f", balance.available)
        
        # Update cache
        self._balance_cache = balance
        self._balance_cache_time = time.time()
        
        return balance
    
    async def get_balance_dict(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Get balance as dictionary (for API responses)."""
        balance = await self.get_balance(force_refresh)
        return balance.to_dict()
    
    # ══════════════════════════════════════════════════════════════════════════
    # POSITIONS METHODS
    # ══════════════════════════════════════════════════════════════════════════
    
    async def get_positions_raw(self) -> Dict[str, Any]:
        """
        Get raw open positions from Kraken Futures.
        
        Endpoint: GET /derivatives/api/v3/openpositions
        
        Returns:
            Raw API response with openPositions list
        """
        return await self._request("GET", "/openpositions")
    
    async def get_positions(self, force_refresh: bool = False) -> List[KrakenFuturesPosition]:
        """
        Get Kraken Futures open positions.
        
        Parses /openpositions response to extract position info.
        Results are cached for 10 seconds.
        
        Args:
            force_refresh: Bypass cache
            
        Returns:
            List of KrakenFuturesPosition objects
        """
        # Check cache (10s TTL)
        cache_age = time.time() - self._positions_cache_time
        if not force_refresh and self._positions_cache and cache_age < 10:
            logger.debug("[KRAKEN_REST] Returning cached positions (age: %.1fs)", cache_age)
            return self._positions_cache
        
        # Fetch fresh data
        logger.info("[KRAKEN_REST] Fetching positions...")
        data = await self.get_positions_raw()
        
        raw_positions = data.get("openPositions", [])
        positions: List[KrakenFuturesPosition] = []
        
        for pos in raw_positions:
            try:
                # Parse position data
                symbol = pos.get("symbol", "")
                size = float(pos.get("size", 0))
                side = pos.get("side", "long").lower()
                
                # Kraken returns negative size for shorts
                if size < 0:
                    size = abs(size)
                    side = "short"
                
                position = KrakenFuturesPosition(
                    symbol=symbol,
                    side=side,
                    size=size,
                    entry_price=float(pos.get("price", pos.get("entryPrice", 0))),
                    mark_price=float(pos.get("markPrice", 0)),
                    unrealized_pnl=float(pos.get("unrealizedFunding", 0)),
                    leverage=float(pos.get("maxFixedLeverage", 0)) or 1.0,
                    liquidation_price=0.0,  # Not in /openpositions — needs /accounts
                )
                positions.append(position)
                
                logger.debug(
                    "[KRAKEN_REST] Position: %s %s %.4f @ %.2f | pnl=%.2f",
                    position.symbol, position.side, position.size, 
                    position.entry_price, position.unrealized_pnl
                )
            except Exception as e:
                logger.warning("[KRAKEN_REST] Failed to parse position: %s | error: %s", pos, e)
        
        logger.info("[KRAKEN_REST] ✓ %d open positions", len(positions))
        
        # Update cache
        self._positions_cache = positions
        self._positions_cache_time = time.time()
        
        return positions
    
    async def get_positions_dict(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Get positions as list of dictionaries."""
        positions = await self.get_positions(force_refresh)
        return [p.to_dict() for p in positions]
    
    # ══════════════════════════════════════════════════════════════════════════
    # TICKER / MARKET PRICE (PUBLIC — NO AUTH)
    # ══════════════════════════════════════════════════════════════════════════
    
    async def get_tickers(self) -> Dict[str, Dict[str, Any]]:
        """
        Fetch all Kraken Futures tickers (public endpoint, no auth).
        
        Endpoint: GET /derivatives/api/v3/tickers
        
        Returns:
            Dict mapping symbol -> ticker data with last, bid, ask, markPrice etc.
        """
        result = await self._request("GET", "/tickers", authenticated=False)
        tickers_raw = result.get("tickers", [])
        
        tickers: Dict[str, Dict[str, Any]] = {}
        for t in tickers_raw:
            sym = t.get("symbol", "")
            if sym:
                tickers[sym.upper()] = {
                    "symbol": sym,
                    "last": float(t.get("last", 0)),
                    "bid": float(t.get("bid", 0)),
                    "ask": float(t.get("ask", 0)),
                    "mark_price": float(t.get("markPrice", 0)),
                    "vol24h": float(t.get("vol24h", 0)),
                    "open24h": float(t.get("open24h", 0)),
                }
        return tickers
    
    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """
        Fetch ticker for a single Kraken Futures symbol.
        
        Args:
            symbol: Kraken Futures symbol (e.g. PF_XBTUSD, PF_ETHUSD)
            
        Returns:
            Ticker dict with last, bid, ask, mark_price
            
        Raises:
            ValueError: If symbol not found in tickers
        """
        tickers = await self.get_tickers()
        key = symbol.upper()
        
        if key not in tickers:
            # Try with PF_ prefix
            if not key.startswith("PF_"):
                key = f"PF_{key}"
            if key not in tickers:
                raise ValueError(f"Ticker not found for symbol: {symbol}")
        
        return tickers[key]
    
    async def get_historical_pnl(self, symbol: str, since_ms: Optional[int] = None) -> Optional[Dict[str, float]]:
        """
        Fetch historical fills to compute PnL for a recent close.
        Endpoint: GET /derivatives/api/v3/fills
        
        Args:
            symbol: Kraken Futures symbol (e.g. PF_XBTUSD)
            since_ms: Optional timestamp in ms to filter fills
            
        Returns:
            Dict with 'pnl' and 'fee', or None if no relevant closing fills found.
        """
        try:
            params = {}
            if since_ms is not None:
                # Kraken accepts lastFillTime (ISO8601) but we'll fetch recent and filter client-side to be safe
                pass
                
            result = await self._request("GET", "/fills", params=params)
            fills = result.get("fills", [])
            
            total_pnl = 0.0
            total_fee = 0.0
            found = False
            
            for fill in fills:
                fill_symbol = fill.get("symbol", "")
                if fill_symbol.upper() != symbol.upper():
                    continue
                
                # Check timeframe if since_ms is provided
                if since_ms:
                    fill_time_str = fill.get("time", "")
                    try:
                        from dateutil import parser
                        fill_time = int(parser.parse(fill_time_str).timestamp() * 1000)
                        if fill_time < since_ms:
                            continue
                    except (ValueError, TypeError, AttributeError) as _parse_err:
                        logger.debug("[KRAKEN_REST] Fill timestamp parse error: %s", _parse_err)
                        pass  # Include fill with unparseable timestamp
                
                # Kraken fills have 'pnl' and 'fee' fields
                pnl = float(fill.get("pnl", 0) or 0)
                fee = float(fill.get("fee", 0) or 0)
                
                # Only care about closing fills that realize PnL
                if pnl != 0 or fee != 0:
                    found = True
                    total_pnl += pnl
                    total_fee += fee
            
            if found:
                logger.info(f"[KRAKEN_REST] Found historical fills for {symbol} | pnl={total_pnl:.2f}, fee={total_fee:.2f}")
                return {"pnl": total_pnl, "fee": total_fee}
            
            return None
            
        except Exception as e:
            logger.error(f"[KRAKEN_REST] Error fetching historical PnL: {e}")
            return None

    # ══════════════════════════════════════════════════════════════════════════
    # CONNECTION TEST
    # ══════════════════════════════════════════════════════════════════════════
    
    async def test_connection(self) -> Dict[str, Any]:
        """
        Test Kraken Futures API connection and authentication.
        
        Returns:
            {
                "success": bool,
                "authenticated": bool,
                "balance": {...} or None,
                "error": str or None
            }
        """
        logger.info("[KRAKEN_REST] Testing connection...")
        
        if not self.api_key or not self.api_secret:
            return {
                "success": False,
                "authenticated": False,
                "balance": None,
                "error": "API key or secret not configured"
            }
        
        try:
            balance = await self.get_balance(force_refresh=True)
            logger.info("[KRAKEN_REST] ✓ Connection successful")
            return {
                "success": True,
                "authenticated": True,
                "balance": balance.to_dict(),
                "error": None
            }
        except ValueError as e:
            error_msg = str(e)
            logger.error("[KRAKEN_REST] ✗ Connection failed: %s", error_msg)
            
            # Provide helpful error messages
            hint = ""
            if "authenticationError" in error_msg.lower() or "invalid" in error_msg.lower():
                hint = " — Verify your API keys are from futures.kraken.com (NOT kraken.com)"
            
            return {
                "success": False,
                "authenticated": False,
                "balance": None,
                "error": f"{error_msg}{hint}"
            }
        except Exception as e:
            logger.error("[KRAKEN_REST] ✗ Connection error: %s", e)
            return {
                "success": False,
                "authenticated": False,
                "balance": None,
                "error": f"Network error: {e}"
            }
    
    # ══════════════════════════════════════════════════════════════════════════
    # ORDER PLACEMENT (REST API - Reliable)
    # ══════════════════════════════════════════════════════════════════════════
    
    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "mkt",
        size: float = 0.0,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Place an order on Kraken Futures via REST API.
        
        This is the PREFERRED method for order placement - more reliable than WS.
        
        Endpoint: POST /derivatives/api/v3/sendorder
        
        Args:
            symbol: Kraken Futures symbol (e.g., PF_XBTUSD)
            side: "buy" or "sell"
            order_type: "mkt" (market), "lmt" (limit), "stp" (stop), "take_profit"
            size: Order size in base currency
            price: Limit price (required for limit orders)
            stop_price: Trigger price for stop/TP orders
            reduce_only: Only reduce existing position
            client_order_id: Optional client order ID
            
        Returns:
            {
                "success": bool,
                "order_id": str or None,
                "status": str,
                "error": str or None,
                "raw_response": dict
            }
        """
        # Normalize side
        side = side.lower()
        if side in ("long", "open_long"):
            side = "buy"
        elif side in ("short", "open_short"):
            side = "sell"
        
        # Normalize order type
        order_type = order_type.lower()
        if order_type == "market":
            order_type = "mkt"
        elif order_type == "limit":
            order_type = "lmt"
        elif order_type in ("stop", "stop_loss", "stop-loss"):
            order_type = "stp"
        
        # Build order data
        # Respect instrument contract precision specs
        try:
            from backend.kraken_contracts import get_contract_specs
            specs = get_contract_specs(symbol)
            qty_precision = specs.get("qty_precision", 0)
        except ImportError:
            qty_precision = 0

        if qty_precision > 0:
            formatted_size = f"{float(size):.{qty_precision}f}"
        else:
            try:
                from backend.risk.size_calculator import ensure_kraken_contracts
                int_size = ensure_kraken_contracts(size, symbol)
            except ImportError:
                int_size = max(1, int(round(size))) if size >= 1 else 1
            formatted_size = str(int_size)

        logger.info(
            "[KRAKEN_REST_ORDER] SIZE_FORMAT | raw_size=%.6f → formatted=%s | symbol=%s",
            size, formatted_size, symbol,
        )

        data = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "size": formatted_size,
        }
        
        if price is not None and order_type in ("lmt", "take_profit"):
            data["limitPrice"] = price
        
        if stop_price is not None and order_type in ("stp", "take_profit"):
            data["stopPrice"] = stop_price
        
        if reduce_only:
            data["reduceOnly"] = True
        
        if client_order_id:
            data["cliOrdId"] = client_order_id
        
        logger.info(
            "[KRAKEN_REST_ORDER] %s %s %s | size=%s units (raw=%.6f) | price=%s | stop=%s",
            order_type.upper(), side.upper(), symbol, formatted_size, size, price, stop_price
        )
        
        try:
            result = await self._request("POST", "/sendorder", data=data)
            
            # Parse response
            send_status = result.get("sendStatus", {})
            order_id = send_status.get("order_id")
            status = send_status.get("status", "unknown")
            
            if status in ("placed", "filled", "partiallyFilled"):
                logger.info(
                    "[KRAKEN_REST_ORDER] SUCCESS | order_id=%s | status=%s",
                    order_id, status
                )
                return {
                    "success": True,
                    "order_id": order_id,
                    "status": status,
                    "error": None,
                    "raw_response": result,
                    "code": "00000",
                    "msg": "success",
                    "data": {"orderId": order_id, "status": status},
                }
            else:
                error_msg = send_status.get("errorMessage", f"Unknown status: {status}")
                logger.error("[KRAKEN_REST_ORDER] FAILED | status=%s | error=%s", status, error_msg)
                return {
                    "success": False,
                    "order_id": order_id,
                    "status": status,
                    "error": error_msg,
                    "raw_response": result,
                    "code": "ORDER_FAILED",
                    "msg": error_msg,
                    "data": None,
                }
                
        except ValueError as e:
            logger.error("[KRAKEN_REST_ORDER] API error: %s", e)
            return {
                "success": False,
                "order_id": None,
                "status": "error",
                "error": str(e),
                "raw_response": {},
                "code": "API_ERROR",
                "msg": str(e),
                "data": None,
            }
        except Exception as e:
            logger.error("[KRAKEN_REST_ORDER] Exception: %s", e)
            return {
                "success": False,
                "order_id": None,
                "status": "exception",
                "error": str(e),
                "raw_response": {},
                "code": "EXCEPTION",
                "msg": str(e),
                "data": None,
            }
    
    async def place_market_order(
        self,
        symbol: str,
        side: str,
        size: float,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """Place a market order (convenience wrapper)."""
        return await self.place_order(
            symbol=symbol,
            side=side,
            order_type="mkt",
            size=size,
            reduce_only=reduce_only,
        )
    
    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        size: float,
        price: float,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """Place a limit order (convenience wrapper)."""
        return await self.place_order(
            symbol=symbol,
            side=side,
            order_type="lmt",
            size=size,
            price=price,
            reduce_only=reduce_only,
        )
    
    async def place_stop_loss(
        self,
        symbol: str,
        side: str,
        size: float,
        stop_price: float,
        reduce_only: bool = True,
    ) -> Dict[str, Any]:
        """Place a stop-loss order (convenience wrapper)."""
        return await self.place_order(
            symbol=symbol,
            side=side,
            order_type="stp",
            size=size,
            stop_price=stop_price,
            reduce_only=reduce_only,
        )
    
    async def place_take_profit(
        self,
        symbol: str,
        side: str,
        size: float,
        price: float,
        reduce_only: bool = True,
    ) -> Dict[str, Any]:
        """Place a take-profit order (convenience wrapper)."""
        return await self.place_order(
            symbol=symbol,
            side=side,
            order_type="take_profit",
            size=size,
            price=price,
            reduce_only=reduce_only,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # UTILITY METHODS
    # ══════════════════════════════════════════════════════════════════════════
    
    async def check_margin(self, required_usd: float) -> Tuple[bool, float]:
        """
        Check if available margin is sufficient for a trade.
        
        Args:
            required_usd: Required margin in USD
            
        Returns:
            Tuple of (is_sufficient, available_balance)
        """
        try:
            balance = await self.get_balance()
            is_sufficient = balance.available >= required_usd
            
            if not is_sufficient:
                logger.warning(
                    "[KRAKEN_REST] INSUFFICIENT margin: need $%.2f, have $%.2f",
                    required_usd, balance.available
                )
            
            return is_sufficient, balance.available
        except Exception as e:
            logger.error("[KRAKEN_REST] Margin check failed: %s", e)
            return False, 0.0

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fetch open orders from Kraken Futures.
        Endpoint: GET /derivatives/api/v3/openorders
        """
        try:
            res = await self._request("GET", "/openorders")
            orders = res.get("openOrders", [])
            if symbol:
                norm_sym = symbol.upper()
                orders = [o for o in orders if (o.get("symbol") or "").upper() == norm_sym]
            return orders
        except Exception as e:
            logger.error("[KRAKEN_REST] get_open_orders error: %s", e)
            return []

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
            logger.error("[KRAKEN_REST] get_order_status error: %s", e)
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
            logger.error("[KRAKEN_REST] get_fills error: %s", e)
            return []


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

_client_instance: Optional[KrakenFuturesRESTClientV2] = None


def get_kraken_futures_client() -> KrakenFuturesRESTClientV2:
    """Get singleton instance of Kraken Futures REST client."""
    global _client_instance
    if _client_instance is None:
        _client_instance = KrakenFuturesRESTClientV2()
    return _client_instance


def reset_kraken_futures_client() -> None:
    """Reset singleton instance (for testing)."""
    global _client_instance
    _client_instance = None


# ══════════════════════════════════════════════════════════════════════════════
# CLI TEST
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    """Test Kraken Futures REST client."""
    print("=" * 60)
    print("KRAKEN FUTURES REST CLIENT V2 - TEST")
    print("=" * 60)
    
    api_key = os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_API_SECRET", "")
    
    print(f"\nAPI Key: {api_key[:20]}..." if len(api_key) > 20 else f"\nAPI Key: {api_key or 'NOT SET'}")
    print(f"Secret: {'*' * min(len(api_secret), 20)}..." if api_secret else "Secret: NOT SET")
    
    if not api_key or not api_secret:
        print("\n❌ API keys not configured in .env")
        print("   Create keys at: https://futures.kraken.com/trade/settings/api")
        return
    
    client = KrakenFuturesRESTClientV2()
    
    print("\n" + "-" * 40)
    print("Testing connection...")
    print("-" * 40)
    
    result = await client.test_connection()
    
    if result["success"]:
        print("\n✅ SUCCESS - Authentication OK!")
        b = result["balance"]
        print(f"\n  Available:   ${b['available']:,.2f}")
        print(f"  Total:       ${b['total']:,.2f}")
        print(f"  Margin Used: ${b['margin_used']:,.2f}")
        print(f"  PnL:         ${b['unrealized_pnl']:,.4f}")
        
        print("\n" + "-" * 40)
        print("Fetching positions...")
        print("-" * 40)
        
        positions = await client.get_positions()
        if positions:
            print(f"\n  {len(positions)} open position(s):")
            for pos in positions:
                print(f"    {pos.symbol}: {pos.side} {pos.size} @ ${pos.entry_price:.2f} | PnL: ${pos.unrealized_pnl:.2f}")
        else:
            print("\n  No open positions")
    else:
        print(f"\n❌ FAILED: {result['error']}")
        
        if "authenticationError" in str(result["error"]).lower():
            print("\n  Possible causes:")
            print("  1. Using Kraken SPOT keys instead of FUTURES")
            print("     → Create keys at: https://futures.kraken.com/trade/settings/api")
            print("  2. 'Read' permission not enabled on API key")
            print("  3. API key expired or revoked")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    asyncio.run(main())
