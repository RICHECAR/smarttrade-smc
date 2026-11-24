#!/usr/bin/env python
"""
Test Kraken Futures REST Authentication
═══════════════════════════════════════

Simple test script to verify Kraken Futures API authentication is working.
Run with: python tests/test_kraken_auth.py
"""

import asyncio
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


async def test_kraken_balance_service():
    """Test the new KrakenFuturesBalanceClient service."""
    print("=" * 70)
    print("TEST: KrakenFuturesBalanceClient (kraken_balance_service.py)")
    print("=" * 70)
    
    from backend.services.kraken_balance_service import (
        KrakenFuturesBalanceClient,
        get_kraken_balance_client,
    )
    
    # Check credentials
    api_key = os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_API_SECRET", "")
    
    print(f"\n[ENV] KRAKEN_API_KEY:    {'SET (' + api_key[:8] + '...)' if api_key else 'NOT SET'}")
    print(f"[ENV] KRAKEN_API_SECRET: {'SET (hidden)' if api_secret else 'NOT SET'}")
    
    if not api_key or not api_secret:
        print("\n[ERROR] Missing Kraken API credentials in .env file")
        print("  Required: KRAKEN_API_KEY and KRAKEN_API_SECRET")
        return False
    
    # Create client
    client = KrakenFuturesBalanceClient()
    
    # Test 1: Connection test
    print("\n" + "-" * 50)
    print("[TEST 1] Testing connection and authentication...")
    print("-" * 50)
    
    result = await client.test_connection()
    print(f"  Success:       {result['success']}")
    print(f"  Connected:     {result['connected']}")
    print(f"  Authenticated: {result['authenticated']}")
    
    if not result['success']:
        print(f"  Error:         {result.get('error', 'Unknown')}")
        return False
    
    # Test 2: Balance
    print("\n" + "-" * 50)
    print("[TEST 2] Getting account balance...")
    print("-" * 50)
    
    try:
        balance = await client.get_balance()
        print(f"  Available:      ${balance.available:,.2f}")
        print(f"  Total:          ${balance.total:,.2f}")
        print(f"  Margin Used:    ${balance.margin_used:,.2f}")
        print(f"  Unrealized PnL: ${balance.unrealized_pnl:,.4f}")
        print(f"  Currency:       {balance.currency}")
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False
    
    # Test 3: Positions
    print("\n" + "-" * 50)
    print("[TEST 3] Getting open positions...")
    print("-" * 50)
    
    try:
        positions = await client.get_positions()
        if positions:
            for p in positions:
                print(f"  {p.symbol}: {p.side.upper()} {p.size} @ ${p.entry_price:,.2f}")
                print(f"    Mark: ${p.mark_price:,.2f} | PnL: ${p.unrealized_pnl:,.4f}")
        else:
            print("  No open positions")
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False
    
    # Test 4: Margin check
    print("\n" + "-" * 50)
    print("[TEST 4] Checking margin sufficiency...")
    print("-" * 50)
    
    for required in [100, 1000, 10000]:
        is_sufficient, available = await client.check_balance_sufficient(required)
        status = "✓ OK" if is_sufficient else "✗ INSUFFICIENT"
        print(f"  ${required:,}: {status} (available: ${available:,.2f})")
    
    print("\n" + "=" * 70)
    print("ALL TESTS PASSED ✓")
    print("=" * 70)
    
    return True


async def test_legacy_kraken_client():
    """Test the legacy KrakenFuturesClient for comparison."""
    print("\n" + "=" * 70)
    print("TEST: KrakenFuturesClient (kraken_futures_client.py)")
    print("=" * 70)
    
    try:
        from backend.kraken_futures_client import KrakenFuturesClient
        
        client = KrakenFuturesClient()
        
        print("\n[TEST] Getting balance via legacy client...")
        
        balance = await client.get_balance()
        print(f"  Result: {balance}")
        
        return True
        
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


async def main():
    """Run all tests."""
    print("\n" + "=" * 70)
    print("KRAKEN FUTURES REST API AUTHENTICATION TEST")
    print("=" * 70)
    
    # Test new balance service
    success = await test_kraken_balance_service()
    
    if success:
        # Optionally test legacy client
        print("\n[INFO] New balance service is working correctly!")
        print("[INFO] You can use GET /api/kraken/balance to check your balance")
        print("[INFO] You can use GET /api/kraken/status to verify connection")


if __name__ == "__main__":
    asyncio.run(main())
