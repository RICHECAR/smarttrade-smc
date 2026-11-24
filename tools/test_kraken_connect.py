#!/usr/bin/env python3
"""
Test Kraken Futures REST API connectivity.

This script verifies:
1. API credentials are configured
2. Connection to Kraken Futures API works
3. Account balance can be retrieved
4. Open positions can be fetched

Usage:
    python tools/test_kraken_connect.py
"""

import os
import sys
import asyncio

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

# Load environment
load_dotenv()


def check_env_vars():
    """Check required environment variables."""
    print("\n" + "=" * 60)
    print("1. Checking Kraken Futures environment variables...")
    print("=" * 60)
    
    api_key = os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_API_SECRET", "")
    kraken_enabled = os.getenv("KRAKEN_ENABLED", "0").lower() in ("1", "true", "yes")
    
    if not api_key:
        print("❌ KRAKEN_API_KEY not set")
        return False
    print(f"✅ KRAKEN_API_KEY: {api_key[:8]}...{api_key[-4:]}")
    
    if not api_secret:
        print("❌ KRAKEN_API_SECRET not set")
        return False
    print(f"✅ KRAKEN_API_SECRET: {api_secret[:8]}...")
    
    print(f"ℹ️  KRAKEN_ENABLED: {kraken_enabled}")
    
    return True


async def test_rest_connection():
    """Test REST API connection."""
    print("\n" + "=" * 60)
    print("2. Testing Kraken Futures REST API connection...")
    print("=" * 60)
    
    try:
        from backend.kraken_futures_rest_v2 import KrakenFuturesRestV2
        
        api_key = os.getenv("KRAKEN_API_KEY", "")
        api_secret = os.getenv("KRAKEN_API_SECRET", "")
        
        client = KrakenFuturesRestV2(api_key, api_secret)
        
        # Test 1: Get account balance
        print("\n📡 Fetching account balance...")
        balance = await client.get_balance()
        
        if "error" in balance:
            print(f"❌ Balance error: {balance['error']}")
            return False
        
        print("✅ Balance retrieved successfully!")
        if "accounts" in balance:
            for currency, info in balance.get("accounts", {}).items():
                if isinstance(info, dict):
                    avail = info.get("availableBalance", info.get("available", "N/A"))
                    print(f"   {currency}: {avail}")
        
        # Test 2: Get open positions
        print("\n📡 Fetching open positions...")
        positions = await client.get_positions()
        
        if "error" in positions:
            print(f"⚠️  Positions error: {positions['error']}")
        else:
            pos_list = positions.get("openPositions", [])
            print(f"✅ Positions retrieved: {len(pos_list)} open")
            for pos in pos_list[:3]:  # Show first 3
                symbol = pos.get("symbol", "?")
                size = pos.get("size", 0)
                side = pos.get("side", "?")
                print(f"   {symbol}: {side} {size}")
        
        # Test 3: Get instruments
        print("\n📡 Fetching tradeable instruments...")
        try:
            instruments = await client.get_instruments()
            if instruments and "instruments" in instruments:
                inst_list = instruments["instruments"]
                futures = [i["symbol"] for i in inst_list if i.get("type") == "futures_inverse" or "PF_" in i.get("symbol", "")][:5]
                print(f"✅ Instruments retrieved: {len(inst_list)} total")
                print(f"   Sample futures: {', '.join(futures)}")
        except Exception as e:
            print(f"⚠️  Instruments: {e}")
        
        return True
        
    except ImportError as e:
        print(f"❌ Import error: {e}")
        return False
    except Exception as e:
        print(f"❌ Connection error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_executor():
    """Test KrakenFuturesExecutor initialization."""
    print("\n" + "=" * 60)
    print("3. Testing KrakenFuturesExecutor...")
    print("=" * 60)
    
    try:
        from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor
        
        api_key = os.getenv("KRAKEN_API_KEY", "")
        api_secret = os.getenv("KRAKEN_API_SECRET", "")
        
        executor = KrakenFuturesExecutor(api_key, api_secret)
        print("✅ KrakenFuturesExecutor created")
        
        # Check REST client availability
        rest_client = executor._get_rest_client()
        if rest_client:
            print("✅ REST client available")
        else:
            print("❌ REST client not available")
        
        return True
        
    except ImportError as e:
        print(f"❌ Import error: {e}")
        return False
    except Exception as e:
        print(f"❌ Executor error: {e}")
        return False


async def main():
    """Run all connection tests."""
    print("=" * 60)
    print("   KRAKEN FUTURES CONNECTION TEST")
    print("=" * 60)
    
    results = []
    
    # Test 1: Environment variables
    results.append(("Environment variables", check_env_vars()))
    
    # Test 2: REST connection
    results.append(("REST API connection", await test_rest_connection()))
    
    # Test 3: Executor
    results.append(("KrakenFuturesExecutor", await test_executor()))
    
    # Summary
    print("\n" + "=" * 60)
    print("   SUMMARY")
    print("=" * 60)
    
    all_passed = True
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status}: {name}")
        if not passed:
            all_passed = False
    
    print()
    if all_passed:
        print("🎉 All tests passed! Kraken Futures connection is working.")
    else:
        print("⚠️  Some tests failed. Check your configuration.")
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
