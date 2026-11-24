#!/usr/bin/env python3
"""
Test Kraken FUTURES connection (NOT Spot).

This script verifies:
1. API URL is futures.kraken.com (NOT api.kraken.com)
2. Signature algorithm is correct for Futures API
3. Connection, authentication, and basic operations work

Usage:
    python tools/test_kraken_futures_connection.py
"""

import os
import sys
import asyncio
import time
import hashlib
import base64
import hmac
from pathlib import Path
from urllib.parse import urlencode

# Add project root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load environment
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
KRAKEN_FUTURES_BASE = "https://futures.kraken.com"  # MUST be futures.kraken.com
KRAKEN_FUTURES_API_KEY = os.getenv("KRAKEN_FUTURES_API_KEY", "")
KRAKEN_FUTURES_API_SECRET = os.getenv("KRAKEN_FUTURES_API_SECRET", "")


def print_section(title: str):
    """Print a formatted section header."""
    print(f"\n{'='*60}")
    print(f" {title}")
    print(f"{'='*60}")


def check_env_vars():
    """Verify environment variables are set."""
    print_section("1. Environment Variables Check")
    
    vars_to_check = [
        ("KRAKEN_FUTURES_API_KEY", KRAKEN_FUTURES_API_KEY),
        ("KRAKEN_FUTURES_API_SECRET", KRAKEN_FUTURES_API_SECRET),
    ]
    
    all_ok = True
    for name, value in vars_to_check:
        if value:
            # Show first/last 4 chars only
            masked = f"{value[:4]}...{value[-4:]}" if len(value) > 8 else "***"
            print(f"  ✓ {name}: {masked}")
        else:
            print(f"  ✗ {name}: NOT SET")
            all_ok = False
    
    return all_ok


def kraken_futures_signature(api_secret: str, endpoint: str, nonce: str, post_data: str = "") -> str:
    """
    Generate Kraken FUTURES API signature.
    
    IMPORTANT: This is DIFFERENT from Spot API signature!
    
    Futures signature = base64(hmac_sha512(base64_decode(secret), sha256(post_data + nonce + endpoint)))
    """
    if not api_secret:
        return ""
    
    # Step 1: Concatenate post_data + nonce + endpoint
    message = post_data + nonce + endpoint
    
    # Step 2: SHA256 hash of the message
    sha256_hash = hashlib.sha256(message.encode("utf-8")).digest()
    
    # Step 3: HMAC-SHA512 with base64-decoded secret
    secret_decoded = base64.b64decode(api_secret)
    signature = hmac.new(secret_decoded, sha256_hash, hashlib.sha512).digest()
    
    # Step 4: Base64 encode the signature
    return base64.b64encode(signature).decode("utf-8")


async def test_public_endpoint():
    """Test public endpoint (no auth required)."""
    print_section("2. Public Endpoint Test (No Auth)")
    
    import aiohttp
    
    # Test instruments endpoint
    url = f"{KRAKEN_FUTURES_BASE}/derivatives/api/v3/instruments"
    
    print(f"  URL: {url}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                status = resp.status
                data = await resp.json()
                
                if status == 200 and data.get("result") == "success":
                    instruments = data.get("instruments", [])
                    print(f"  ✓ Status: {status}")
                    print(f"  ✓ Instruments count: {len(instruments)}")
                    
                    # Find a few BTC/ETH perpetuals
                    btc_perps = [i["symbol"] for i in instruments if "XBT" in i["symbol"] and "PERP" in i.get("type", "")]
                    eth_perps = [i["symbol"] for i in instruments if "ETH" in i["symbol"] and "PERP" in i.get("type", "")]
                    
                    print(f"  ✓ BTC perpetuals: {btc_perps[:3]}")
                    print(f"  ✓ ETH perpetuals: {eth_perps[:3]}")
                    return True
                else:
                    print(f"  ✗ Status: {status}")
                    print(f"  ✗ Response: {data}")
                    return False
                    
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


async def test_authenticated_endpoint():
    """Test authenticated endpoint (account info)."""
    print_section("3. Authenticated Endpoint Test (Account Info)")
    
    if not KRAKEN_FUTURES_API_KEY or not KRAKEN_FUTURES_API_SECRET:
        print("  ⊘ Skipped: No API credentials set")
        return None
    
    import aiohttp
    
    endpoint = "/derivatives/api/v3/accounts"
    url = f"{KRAKEN_FUTURES_BASE}{endpoint}"
    nonce = str(int(time.time() * 1000))
    post_data = ""  # GET request has no post data
    
    signature = kraken_futures_signature(KRAKEN_FUTURES_API_SECRET, endpoint, nonce, post_data)
    
    headers = {
        "APIKey": KRAKEN_FUTURES_API_KEY,
        "Nonce": nonce,
        "Authent": signature,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    
    print(f"  URL: {url}")
    print(f"  Nonce: {nonce}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                status = resp.status
                data = await resp.json()
                
                if status == 200 and data.get("result") == "success":
                    accounts = data.get("accounts", {})
                    print(f"  ✓ Status: {status}")
                    print(f"  ✓ Account types: {list(accounts.keys())}")
                    
                    # Show balance if available
                    for acc_type, acc_data in accounts.items():
                        balances = acc_data.get("balances", {})
                        if balances:
                            for currency, balance in list(balances.items())[:3]:
                                print(f"    - {acc_type}/{currency}: {balance}")
                    return True
                else:
                    print(f"  ✗ Status: {status}")
                    print(f"  ✗ Response: {data}")
                    
                    if "error" in data:
                        error = data["error"]
                        if "apiLimitExceeded" in str(error):
                            print("  ⚠ Rate limited. Wait and retry.")
                        elif "authenticationError" in str(error):
                            print("  ⚠ Authentication failed. Check API key/secret.")
                            print("  ⚠ Make sure you're using FUTURES API keys, not Spot!")
                    return False
                    
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


async def test_ticker():
    """Test ticker endpoint for a common symbol."""
    print_section("4. Ticker Test (PF_XBTUSD)")
    
    import aiohttp
    
    symbol = "PF_XBTUSD"  # Kraken Futures perpetual BTC/USD
    url = f"{KRAKEN_FUTURES_BASE}/derivatives/api/v3/tickers"
    
    print(f"  URL: {url}")
    print(f"  Looking for: {symbol}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                status = resp.status
                data = await resp.json()
                
                if status == 200 and data.get("result") == "success":
                    tickers = data.get("tickers", [])
                    
                    # Find our symbol
                    btc_ticker = next((t for t in tickers if t.get("symbol") == symbol), None)
                    
                    if btc_ticker:
                        print(f"  ✓ Symbol: {btc_ticker['symbol']}")
                        print(f"  ✓ Last price: {btc_ticker.get('last', 'N/A')}")
                        print(f"  ✓ Bid: {btc_ticker.get('bid', 'N/A')}")
                        print(f"  ✓ Ask: {btc_ticker.get('ask', 'N/A')}")
                        print(f"  ✓ Volume (24h): {btc_ticker.get('vol24h', 'N/A')}")
                        return True
                    else:
                        print(f"  ✗ Symbol {symbol} not found in tickers")
                        available = [t["symbol"] for t in tickers if "XBT" in t.get("symbol", "")][:5]
                        print(f"  Available BTC symbols: {available}")
                        return False
                else:
                    print(f"  ✗ Status: {status}")
                    print(f"  ✗ Response: {data}")
                    return False
                    
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


async def test_open_positions():
    """Test getting open positions (authenticated)."""
    print_section("5. Open Positions Test")
    
    if not KRAKEN_FUTURES_API_KEY or not KRAKEN_FUTURES_API_SECRET:
        print("  ⊘ Skipped: No API credentials set")
        return None
    
    import aiohttp
    
    endpoint = "/derivatives/api/v3/openpositions"
    url = f"{KRAKEN_FUTURES_BASE}{endpoint}"
    nonce = str(int(time.time() * 1000))
    post_data = ""
    
    signature = kraken_futures_signature(KRAKEN_FUTURES_API_SECRET, endpoint, nonce, post_data)
    
    headers = {
        "APIKey": KRAKEN_FUTURES_API_KEY,
        "Nonce": nonce,
        "Authent": signature,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    
    print(f"  URL: {url}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                status = resp.status
                data = await resp.json()
                
                if status == 200 and data.get("result") == "success":
                    positions = data.get("openPositions", [])
                    print(f"  ✓ Status: {status}")
                    print(f"  ✓ Open positions: {len(positions)}")
                    
                    for pos in positions[:5]:
                        symbol = pos.get("symbol", "?")
                        side = pos.get("side", "?")
                        size = pos.get("size", 0)
                        entry = pos.get("price", 0)
                        pnl = pos.get("pnl", 0)
                        print(f"    - {symbol}: {side} {size} @ {entry} (PnL: {pnl})")
                    return True
                else:
                    print(f"  ✗ Status: {status}")
                    print(f"  ✗ Response: {data}")
                    return False
                    
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


async def main():
    """Run all tests."""
    print("\n" + "="*60)
    print(" KRAKEN FUTURES CONNECTION TEST")
    print(" (Must use futures.kraken.com, NOT api.kraken.com)")
    print("="*60)
    
    # Check env vars
    env_ok = check_env_vars()
    
    # Test public endpoint
    public_ok = await test_public_endpoint()
    
    # Test ticker
    ticker_ok = await test_ticker()
    
    # Test authenticated endpoints (if credentials available)
    auth_ok = await test_authenticated_endpoint()
    positions_ok = await test_open_positions()
    
    # Summary
    print_section("SUMMARY")
    
    results = [
        ("Environment", env_ok),
        ("Public API", public_ok),
        ("Ticker", ticker_ok),
        ("Authentication", auth_ok),
        ("Positions", positions_ok),
    ]
    
    all_passed = True
    for name, result in results:
        if result is True:
            print(f"  ✓ {name}: PASSED")
        elif result is False:
            print(f"  ✗ {name}: FAILED")
            all_passed = False
        else:
            print(f"  ⊘ {name}: SKIPPED")
    
    if all_passed:
        print("\n  ✓ All tests passed! Kraken Futures connection is working.")
    else:
        print("\n  ✗ Some tests failed. Check the output above.")
        print("\n  Common issues:")
        print("    1. Using Spot API keys instead of Futures API keys")
        print("    2. Wrong base URL (must be futures.kraken.com)")
        print("    3. Rate limiting (wait and retry)")
        print("    4. API key permissions (needs trade + account access)")
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
