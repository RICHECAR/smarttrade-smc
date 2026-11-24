"""
Kraken Futures REST Client — Corrected Authentication
══════════════════════════════════════════════════════

⚠️  IMPORTANT: Kraken Spot and Kraken Futures use DIFFERENT API keys!
    - Spot keys: https://www.kraken.com/u/security/api  
    - Futures keys: https://futures.kraken.com/trade/settings/api

The Kraken Futures signature algorithm:
  1. Concatenate: postData + nonce + endpoint
  2. SHA-256 hash the message
  3. HMAC-SHA512 sign the hash with base64-decoded secret
  4. Base64 encode the signature

Reference: https://docs.futures.kraken.com/#authentication
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("smarttrade.kraken_rest")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

KRAKEN_FUTURES_URL = "https://futures.kraken.com"
KRAKEN_FUTURES_API = "/derivatives/api/v3"
DEFAULT_TIMEOUT = 15.0


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class KrakenFuturesBalance:
    """Parsed Kraken Futures balance."""
    available: float
    total: float
    margin_used: float
    unrealized_pnl: float
    currency: str = "USD"
    timestamp: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": round(self.available, 2),
            "total": round(self.total, 2),
            "margin_used": round(self.margin_used, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "currency": self.currency,
            "timestamp": self.timestamp or datetime.now(timezone.utc).isoformat()
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
    Sign a Kraken Futures REST API request.
    
    Algorithm (from Kraken Futures docs):
    1. message = postData + nonce + endpoint
    2. hash = SHA256(message)
    3. signature = HMAC-SHA512(hash, base64_decode(secret))
    4. return base64_encode(signature)
    
    Args:
        api_secret: Base64-encoded API secret
        endpoint: Full endpoint path (e.g., /derivatives/api/v3/accounts)
        nonce: Unique nonce (milliseconds timestamp as string)
        post_data: URL-encoded POST data (empty string for GET)
        
    Returns:
        Base64-encoded signature string
    """
    # Step 1: Build message
    message = (post_data + nonce + endpoint).encode("utf-8")
    
    # Step 2: SHA-256 hash
    sha256_hash = hashlib.sha256(message).digest()
    
    # Step 3: Decode secret and HMAC-SHA512 sign
    try:
        secret_bytes = base64.b64decode(api_secret)
    except Exception as e:
        logger.error("[KRAKEN_SIGN] Failed to decode secret: %s", e)
        raise ValueError(f"Invalid API secret format: {e}")
    
    hmac_sig = hmac.new(secret_bytes, sha256_hash, hashlib.sha512).digest()
    
    # Step 4: Base64 encode signature
    return base64.b64encode(hmac_sig).decode("utf-8")


def get_kraken_futures_headers(
    api_key: str,
    api_secret: str,
    endpoint: str,
    post_data: str = ""
) -> Dict[str, str]:
    """
    Generate authenticated headers for Kraken Futures API.
    
    Args:
        api_key: API public key
        api_secret: API secret (base64)
        endpoint: Full endpoint path
        post_data: POST data string (empty for GET)
        
    Returns:
        Dict with APIKey, Nonce, Authent headers
    """
    nonce = str(int(time.time() * 1000))
    signature = sign_kraken_futures_request(api_secret, endpoint, nonce, post_data)
    
    return {
        "APIKey": api_key,
        "Nonce": nonce,
        "Authent": signature,
        "Content-Type": "application/x-www-form-urlencoded"
    }


# ══════════════════════════════════════════════════════════════════════════════
# KRAKEN FUTURES REST CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class KrakenFuturesRESTClient:
    """
    Kraken Futures REST API client with correct authentication.
    
    Usage:
        client = KrakenFuturesRESTClient()
        balance = await client.get_balance()
        print(f"Available: ${balance.available}")
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT
    ):
        self.api_key = api_key or os.getenv("KRAKEN_API_KEY", "")
        self.api_secret = api_secret or os.getenv("KRAKEN_API_SECRET", "")
        self.timeout = timeout
        self._balance_cache: Optional[KrakenFuturesBalance] = None
        self._cache_time: float = 0
        
        if self.api_key:
            logger.info("[KRAKEN_REST] Client init | key=%s...", self.api_key[:12])
        else:
            logger.warning("[KRAKEN_REST] No API key configured")
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Execute authenticated Kraken Futures REST request.
        
        Args:
            method: HTTP method (GET/POST)
            endpoint: API endpoint path
            params: Query parameters for GET
            data: Body data for POST
            
        Returns:
            Parsed JSON response
            
        Raises:
            ValueError: On API errors
            httpx.HTTPError: On network errors
        """
        # Build full endpoint
        full_endpoint = f"{KRAKEN_FUTURES_API}{endpoint}"
        url = f"{KRAKEN_FUTURES_URL}{full_endpoint}"
        
        # Build POST data string
        post_data = ""
        if data:
            post_data = urllib.parse.urlencode(data)
        
        # Get authenticated headers
        headers = get_kraken_futures_headers(
            self.api_key, self.api_secret, full_endpoint, post_data
        )
        
        logger.debug("[KRAKEN_REST] %s %s", method.upper(), full_endpoint)
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if method.upper() == "GET":
                resp = await client.get(url, headers=headers, params=params)
            else:
                resp = await client.post(url, headers=headers, content=post_data)
            
            if resp.status_code != 200:
                logger.error("[KRAKEN_REST] HTTP %d: %s", resp.status_code, resp.text[:200])
                resp.raise_for_status()
            
            result = resp.json()
            
            # Check API-level errors
            if result.get("result") == "error":
                error = result.get("error", "Unknown error")
                logger.error("[KRAKEN_REST] API Error: %s", error)
                raise ValueError(f"Kraken API error: {error}")
            
            return result
    
    async def get_accounts_raw(self) -> Dict[str, Any]:
        """
        Get raw accounts data from /accounts endpoint.
        
        Returns:
            Raw API response with accounts structure
        """
        return await self._request("GET", "/accounts")
    
    async def get_balance(self, force_refresh: bool = False) -> KrakenFuturesBalance:
        """
        Get Kraken Futures account balance.
        
        Parses the /accounts response to extract balance info.
        Results are cached for 60 seconds.
        
        Args:
            force_refresh: Bypass cache
            
        Returns:
            KrakenFuturesBalance with available, total, margin_used, pnl
        """
        # Check cache (60s TTL)
        if not force_refresh and self._balance_cache:
            if time.time() - self._cache_time < 60:
                logger.debug("[KRAKEN_REST] Returning cached balance")
                return self._balance_cache
        
        # Fetch fresh data
        data = await self.get_accounts_raw()
        
        accounts = data.get("accounts", {})
        
        # Try to get from flex (multi-collateral) first
        flex = accounts.get("flex", {})
        if flex:
            balance = KrakenFuturesBalance(
                available=float(flex.get("availableMargin", 0)),
                total=float(flex.get("portfolioValue", 0)),
                margin_used=float(flex.get("initialMargin", 0)),
                unrealized_pnl=float(flex.get("pnl", 0)),
                timestamp=data.get("serverTime", "")
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
                timestamp=data.get("serverTime", "")
            )
        
        # Update cache
        self._balance_cache = balance
        self._cache_time = time.time()
        
        logger.info(
            "[KRAKEN_REST] Balance: available=%.2f total=%.2f margin=%.2f pnl=%.4f",
            balance.available, balance.total, balance.margin_used, balance.unrealized_pnl
        )
        
        return balance
    
    async def get_balance_dict(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Get balance as dictionary (for API responses)."""
        balance = await self.get_balance(force_refresh)
        return balance.to_dict()
    
    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """
        Get open positions from /openpositions endpoint.
        
        Returns:
            List of position dictionaries
        """
        data = await self._request("GET", "/openpositions")
        return data.get("openPositions", [])
    
    async def test_connection(self) -> Dict[str, Any]:
        """
        Test API connection and authentication.
        
        Returns:
            Status dict with success, authenticated, balance info
        """
        try:
            balance = await self.get_balance(force_refresh=True)
            return {
                "success": True,
                "authenticated": True,
                "balance": balance.to_dict(),
                "error": None
            }
        except ValueError as e:
            return {
                "success": False,
                "authenticated": False,
                "balance": None,
                "error": str(e)
            }
        except Exception as e:
            return {
                "success": False,
                "authenticated": False,
                "balance": None,
                "error": f"Connection error: {e}"
            }


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

_instance: Optional[KrakenFuturesRESTClient] = None


def get_kraken_rest_client() -> KrakenFuturesRESTClient:
    """Get or create singleton Kraken REST client."""
    global _instance
    if _instance is None:
        _instance = KrakenFuturesRESTClient()
    return _instance


# ══════════════════════════════════════════════════════════════════════════════
# STANDALONE TEST
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    """Test Kraken Futures REST authentication."""
    print("=" * 60)
    print("KRAKEN FUTURES REST CLIENT TEST")
    print("=" * 60)
    
    api_key = os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_API_SECRET", "")
    
    print(f"\nAPI Key: {api_key[:16]}..." if api_key else "\nAPI Key: NOT SET")
    print(f"Secret: {len(api_secret)} chars" if api_secret else "Secret: NOT SET")
    
    if not api_key or not api_secret:
        print("\n[ERROR] Set KRAKEN_API_KEY and KRAKEN_API_SECRET in .env")
        return
    
    client = KrakenFuturesRESTClient(api_key, api_secret)
    
    print("\n" + "-" * 40)
    print("Testing connection...")
    print("-" * 40)
    
    result = await client.test_connection()
    
    if result["success"]:
        print("\n✓ SUCCESS - Authentication working!")
        print(f"\n  Available: ${result['balance']['available']:,.2f}")
        print(f"  Total:     ${result['balance']['total']:,.2f}")
        print(f"  Margin:    ${result['balance']['margin_used']:,.2f}")
        print(f"  PnL:       ${result['balance']['unrealized_pnl']:,.4f}")
    else:
        print(f"\n✗ FAILED: {result['error']}")
        print("\nTroubleshooting:")
        print("  1. Verify you have KRAKEN FUTURES keys (not Spot)")
        print("     - Futures: https://futures.kraken.com/trade/settings/api")
        print("     - Spot: https://www.kraken.com/u/security/api")
        print("  2. Check API key has 'Read' permission")
        print("  3. Verify secret is base64 encoded")


if __name__ == "__main__":
    asyncio.run(main())
