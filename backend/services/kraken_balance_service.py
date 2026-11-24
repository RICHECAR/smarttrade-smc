"""
Kraken Futures Balance Service — REST API V3 Authentication
═══════════════════════════════════════════════════════════

⚠️  IMPORTANT: Kraken Spot and Kraken Futures use DIFFERENT API keys!
    - Kraken Spot keys: https://www.kraken.com/u/security/api
    - Kraken Futures keys: https://futures.kraken.com/trade/settings/api

Provides properly authenticated access to Kraken Futures account data
using the correct HMAC-SHA512 signature scheme.

Endpoints:
  GET /derivatives/api/v3/accounts     - Account balances and positions
  GET /derivatives/api/v3/openpositions - Open positions

Reference:
  https://docs.futures.kraken.com/#http-api-trading-v3-api-account-information

Author: Trading Bot Framework
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("smarttrade.kraken_balance")

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

KRAKEN_FUTURES_BASE_URL = "https://futures.kraken.com"
KRAKEN_FUTURES_API_VERSION = "/derivatives/api/v3"

# Timeout for REST requests
DEFAULT_TIMEOUT = 15.0

# Balance refresh interval (seconds)
BALANCE_REFRESH_INTERVAL = 300  # 5 minutes


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class KrakenBalance:
    """Kraken Futures balance data."""
    available: float
    total: float
    margin_used: float
    unrealized_pnl: float
    currency: str = "USD"
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": round(self.available, 2),
            "total": round(self.total, 2),
            "margin_used": round(self.margin_used, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "currency": self.currency,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class KrakenPosition:
    """Kraken Futures position data."""
    symbol: str
    side: str
    size: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: float
    liquidation_price: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "entry_price": self.entry_price,
            "mark_price": self.mark_price,
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "leverage": self.leverage,
            "liquidation_price": self.liquidation_price,
        }


# ══════════════════════════════════════════════════════════════════════════════
# KRAKEN FUTURES BALANCE CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class KrakenFuturesBalanceClient:
    """
    Kraken Futures REST client with correct HMAC-SHA512 authentication.
    
    Usage:
        client = KrakenFuturesBalanceClient(api_key, api_secret)
        balance = await client.get_balance()
        print(f"Available: ${balance.available}")
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """
        Initialize Kraken Futures balance client.
        
        Args:
            api_key: Kraken Futures API key (or from env: KRAKEN_API_KEY)
            api_secret: Kraken Futures API secret (or from env: KRAKEN_API_SECRET)
            timeout: HTTP request timeout in seconds
        """
        self.api_key = api_key or os.getenv("KRAKEN_API_KEY", "")
        self.api_secret = api_secret or os.getenv("KRAKEN_API_SECRET", "")
        self.timeout = timeout
        
        # Balance cache
        self._balance_cache: Optional[KrakenBalance] = None
        self._last_balance_fetch: float = 0
        
        # Validate credentials
        if not self.api_key or not self.api_secret:
            logger.warning("[KRAKEN_BALANCE] Missing API credentials")
        else:
            logger.info("[KRAKEN_BALANCE] Client initialized | key=%s...", 
                       self.api_key[:8] if len(self.api_key) > 8 else "***")
    
    # ──────────────────────────────────────────────────────────────────────────
    #  SIGNATURE GENERATION
    # ──────────────────────────────────────────────────────────────────────────
    
    def _generate_signature(
        self, 
        endpoint: str, 
        nonce: str, 
        post_data: str = ""
    ) -> str:
        """
        Generate HMAC-SHA512 signature for Kraken Futures REST API.
        
        Kraken Futures Signature Algorithm:
        1. Construct message: postData + nonce + endpoint
        2. SHA-256 hash the message
        3. HMAC-SHA512 sign the hash with decoded API secret
        4. Base64 encode the signature
        
        Args:
            endpoint: Full API path (e.g., /derivatives/api/v3/accounts)
            nonce: Unique nonce (milliseconds timestamp)
            post_data: URL-encoded POST data (empty string for GET requests)
            
        Returns:
            Base64-encoded HMAC-SHA512 signature
        """
        # Step 1: Concatenate postData + nonce + endpoint
        message = (post_data + nonce + endpoint).encode("utf-8")
        
        # Step 2: SHA-256 hash of the concatenated message
        sha256_digest = hashlib.sha256(message).digest()
        
        # Step 3: Decode the API secret from base64
        secret_decoded = base64.b64decode(self.api_secret)
        
        # Step 4: HMAC-SHA512 sign the SHA-256 hash
        hmac_signature = hmac.new(
            key=secret_decoded,
            msg=sha256_digest,
            digestmod=hashlib.sha512
        ).digest()
        
        # Step 5: Base64 encode the signature
        return base64.b64encode(hmac_signature).decode("utf-8")
    
    def _get_auth_headers(self, endpoint: str, post_data: str = "") -> Dict[str, str]:
        """
        Generate authenticated headers for Kraken Futures API.
        
        Args:
            endpoint: Full API path
            post_data: POST data (empty for GET)
            
        Returns:
            Headers dict with APIKey, Nonce, and Authent
        """
        nonce = str(int(time.time() * 1000))
        signature = self._generate_signature(endpoint, nonce, post_data)
        
        return {
            "APIKey": self.api_key,
            "Nonce": nonce,
            "Authent": signature,
            "Content-Type": "application/x-www-form-urlencoded",
        }
    
    # ──────────────────────────────────────────────────────────────────────────
    #  REST API METHODS
    # ──────────────────────────────────────────────────────────────────────────
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute authenticated REST request to Kraken Futures.
        
        Args:
            method: HTTP method (GET, POST)
            endpoint: API endpoint (without base URL)
            params: Query parameters
            data: POST data
            
        Returns:
            API response as dict
            
        Raises:
            httpx.HTTPError: On HTTP errors
            ValueError: On API errors
        """
        url = KRAKEN_FUTURES_BASE_URL + endpoint
        
        # Prepare POST data string
        post_data = ""
        if data:
            import urllib.parse
            post_data = urllib.parse.urlencode(data)
        
        # Generate authenticated headers
        headers = self._get_auth_headers(endpoint, post_data)
        
        logger.debug("[KRAKEN_REST] %s %s", method.upper(), endpoint)
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if method.upper() == "GET":
                response = await client.get(url, headers=headers, params=params)
            elif method.upper() == "POST":
                response = await client.post(url, headers=headers, data=post_data)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            
            # Check HTTP status
            if response.status_code != 200:
                logger.error(
                    "[KRAKEN_REST] HTTP %d | %s | Response: %s",
                    response.status_code, endpoint, response.text[:500]
                )
                response.raise_for_status()
            
            result = response.json()
            
            # Check for API-level errors
            if result.get("result") == "error":
                error_msg = result.get("error", "Unknown Kraken API error")
                logger.error("[KRAKEN_REST] API error: %s", error_msg)
                raise ValueError(f"Kraken API error: {error_msg}")
            
            return result
    
    # ──────────────────────────────────────────────────────────────────────────
    #  BALANCE METHODS
    # ──────────────────────────────────────────────────────────────────────────
    
    async def get_accounts_raw(self) -> Dict[str, Any]:
        """
        Get raw accounts data from Kraken Futures API.
        
        Endpoint: GET /derivatives/api/v3/accounts
        
        Returns:
            Raw API response containing all account information
        """
        endpoint = f"{KRAKEN_FUTURES_API_VERSION}/accounts"
        return await self._request("GET", endpoint)
    
    async def get_balance(self, force_refresh: bool = False) -> KrakenBalance:
        """
        Get Kraken Futures account balance.
        
        Parses the accounts response to extract:
        - available: Available margin for trading
        - total: Total portfolio value
        - margin_used: Margin in use by positions
        - unrealized_pnl: Unrealized P&L
        
        Args:
            force_refresh: Bypass cache and fetch fresh data
            
        Returns:
            KrakenBalance dataclass with balance information
        """
        # Check cache
        now = time.time()
        if (
            not force_refresh
            and self._balance_cache is not None
            and (now - self._last_balance_fetch) < BALANCE_REFRESH_INTERVAL
        ):
            logger.debug("[KRAKEN_BALANCE] Returning cached balance")
            return self._balance_cache
        
        # Fetch fresh data
        try:
            accounts_data = await self.get_accounts_raw()
            
            # Parse the accounts structure
            accounts = accounts_data.get("accounts", {})
            
            # Look for flex account (multi-collateral margin)
            flex = accounts.get("flex", {})
            
            # Look for cash account
            cash = accounts.get("cash", {})
            cash_balances = cash.get("balances", {})
            
            # Extract values from flex account (primary for futures)
            available_margin = float(flex.get("availableMargin", 0.0))
            portfolio_value = float(flex.get("portfolioValue", 0.0))
            initial_margin = float(flex.get("initialMargin", 0.0))
            pnl = float(flex.get("pnl", 0.0))
            
            # Fallback to cash account if flex is empty
            if portfolio_value == 0 and cash_balances:
                usd_balance = float(
                    cash_balances.get("usd", cash_balances.get("USD", 0.0))
                )
                available_margin = usd_balance
                portfolio_value = usd_balance
            
            # Create balance object
            balance = KrakenBalance(
                available=available_margin,
                total=portfolio_value,
                margin_used=initial_margin,
                unrealized_pnl=pnl,
                currency="USD",
            )
            
            # Update cache
            self._balance_cache = balance
            self._last_balance_fetch = now
            
            logger.info(
                "[KRAKEN_BALANCE] Fetched | available=%.2f | total=%.2f | "
                "margin_used=%.2f | pnl=%.4f",
                balance.available, balance.total, 
                balance.margin_used, balance.unrealized_pnl
            )
            
            return balance
            
        except Exception as e:
            logger.error("[KRAKEN_BALANCE] Get balance failed: %s", e)
            raise
    
    async def get_balance_dict(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Get balance as dictionary (for API responses).
        
        Returns:
            Dict with keys: available, total, margin_used, unrealized_pnl, currency
        """
        balance = await self.get_balance(force_refresh)
        return balance.to_dict()
    
    # ──────────────────────────────────────────────────────────────────────────
    #  POSITIONS METHODS
    # ──────────────────────────────────────────────────────────────────────────
    
    async def get_positions_raw(self) -> Dict[str, Any]:
        """
        Get raw open positions from Kraken Futures API.
        
        Endpoint: GET /derivatives/api/v3/openpositions
        
        Returns:
            Raw API response with open positions
        """
        endpoint = f"{KRAKEN_FUTURES_API_VERSION}/openpositions"
        return await self._request("GET", endpoint)
    
    async def get_positions(self) -> List[KrakenPosition]:
        """
        Get list of open positions.
        
        Returns:
            List of KrakenPosition dataclasses
        """
        try:
            data = await self.get_positions_raw()
            positions = []
            
            for pos_data in data.get("openPositions", []):
                position = KrakenPosition(
                    symbol=pos_data.get("symbol", ""),
                    side=pos_data.get("side", ""),
                    size=float(pos_data.get("size", 0)),
                    entry_price=float(pos_data.get("price", 0)),
                    mark_price=float(pos_data.get("markPrice", 0)),
                    unrealized_pnl=float(pos_data.get("unrealizedPnl", 0)),
                    leverage=float(pos_data.get("maxFixedLeverage", 1)),
                    liquidation_price=pos_data.get("liquidationThreshold"),
                )
                positions.append(position)
            
            logger.info("[KRAKEN_POSITIONS] Found %d open positions", len(positions))
            return positions
            
        except Exception as e:
            logger.error("[KRAKEN_POSITIONS] Get positions failed: %s", e)
            raise
    
    async def get_positions_dict(self) -> List[Dict[str, Any]]:
        """Get positions as list of dicts (for API responses)."""
        positions = await self.get_positions()
        return [p.to_dict() for p in positions]
    
    # ──────────────────────────────────────────────────────────────────────────
    #  UTILITY METHODS
    # ──────────────────────────────────────────────────────────────────────────
    
    async def check_balance_sufficient(self, required_usd: float) -> tuple[bool, float]:
        """
        Check if available balance is sufficient for a trade.
        
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
                    "[KRAKEN_BALANCE] INSUFFICIENT | required=%.2f | available=%.2f",
                    required_usd, balance.available
                )
            
            return is_sufficient, balance.available
            
        except Exception as e:
            logger.error("[KRAKEN_BALANCE] Check failed: %s", e)
            return False, 0.0
    
    async def test_connection(self) -> Dict[str, Any]:
        """
        Test API connection and authentication.
        
        Returns:
            Dict with connection status and account info
        """
        try:
            balance = await self.get_balance(force_refresh=True)
            
            return {
                "success": True,
                "connected": True,
                "authenticated": True,
                "balance": balance.to_dict(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            
        except ValueError as e:
            # API-level error (authentication issue)
            return {
                "success": False,
                "connected": True,
                "authenticated": False,
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            # Connection error
            return {
                "success": False,
                "connected": False,
                "authenticated": False,
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

_client_instance: Optional[KrakenFuturesBalanceClient] = None


def get_kraken_balance_client() -> KrakenFuturesBalanceClient:
    """Get or create singleton Kraken balance client."""
    global _client_instance
    
    if _client_instance is None:
        _client_instance = KrakenFuturesBalanceClient()
        logger.info("[KRAKEN_BALANCE] Singleton client created")
    
    return _client_instance


# ══════════════════════════════════════════════════════════════════════════════
# TEST FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

async def test_kraken_balance():
    """Test Kraken Futures balance retrieval."""
    print("=" * 60)
    print("KRAKEN FUTURES BALANCE TEST")
    print("=" * 60)
    
    print("\n" + "!" * 60)
    print("IMPORTANT: Kraken Futures requires DIFFERENT API keys than Kraken Spot!")
    print("  - Kraken Spot keys: https://www.kraken.com/u/security/api")
    print("  - Kraken Futures keys: https://futures.kraken.com/trade/settings/api")
    print("!" * 60)
    
    client = KrakenFuturesBalanceClient()
    
    # Test connection
    print("\n[1] Testing connection...")
    result = await client.test_connection()
    print(f"    Success: {result['success']}")
    print(f"    Connected: {result['connected']}")
    print(f"    Authenticated: {result['authenticated']}")
    
    if result['success']:
        print(f"    Balance: ${result['balance']['available']:.2f}")
    else:
        print(f"    Error: {result.get('error', 'Unknown')}")
        if "authenticationError" in str(result.get('error', '')):
            print("\n    HINT: You may be using Kraken Spot API keys instead of Futures keys.")
            print("    Get Kraken Futures keys at: https://futures.kraken.com/trade/settings/api")
    
    # Test balance
    if result['success']:
        print("\n[2] Getting detailed balance...")
        balance = await client.get_balance()
        print(f"    Available:      ${balance.available:.2f}")
        print(f"    Total:          ${balance.total:.2f}")
        print(f"    Margin Used:    ${balance.margin_used:.2f}")
        print(f"    Unrealized PnL: ${balance.unrealized_pnl:.4f}")
        
        # Test positions
        print("\n[3] Getting open positions...")
        positions = await client.get_positions()
        if positions:
            for p in positions:
                print(f"    {p.symbol}: {p.side} {p.size} @ {p.entry_price}")
        else:
            print("    No open positions")
    
    print("\n" + "=" * 60)


if __name__ == "__main__":
    asyncio.run(test_kraken_balance())
