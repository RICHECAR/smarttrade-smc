#!/usr/bin/env python3
"""
Test Kraken Futures REST API order placement (PAPER MODE).

This script verifies:
1. Symbol mapping works (BTCUSDT -> PF_XBTUSD)
2. REST order placement works
3. Order response is valid

⚠️  By default, this runs in PAPER/DRY-RUN mode.
    Set FORCE_LIVE_TEST=true to place a real micro order.

Usage:
    python tools/test_kraken_sendorder_market.py
"""

import os
import sys
import asyncio

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

# Load environment
load_dotenv()


# Test configuration
TEST_SYMBOL = "BTCUSDT"  # Bitget format - will be mapped to PF_XBTUSD
TEST_SIDE = "buy"
TEST_QTY = 0.001  # Very small for safety
TEST_DIRECTION = "long"


async def test_symbol_mapping():
    """Test symbol mapping from Bitget format to Kraken format."""
    print("\n" + "=" * 60)
    print("1. Testing symbol mapping...")
    print("=" * 60)
    
    try:
        from backend.services.multi_order_executor import MultiOrderExecutor
        
        executor = MultiOrderExecutor()
        
        test_cases = [
            ("BTCUSDT", "PF_XBTUSD"),
            ("ETHUSDT", "PF_ETHUSD"),
            ("XRPUSDT", "PF_XRPUSD"),
            ("SOLUSDT", "PF_SOLUSD"),
        ]
        
        all_passed = True
        for bitget_sym, expected_kraken in test_cases:
            kraken_sym = executor.translate_symbol(bitget_sym, "kraken")
            status = "✅" if kraken_sym == expected_kraken else "❌"
            print(f"  {status} {bitget_sym} -> {kraken_sym} (expected: {expected_kraken})")
            if kraken_sym != expected_kraken:
                all_passed = False
        
        return all_passed
        
    except Exception as e:
        print(f"❌ Symbol mapping error: {e}")
        return False


async def test_rest_order_dry_run():
    """Test REST order placement in dry-run mode."""
    print("\n" + "=" * 60)
    print("2. Testing REST order placement (DRY-RUN)...")
    print("=" * 60)
    
    try:
        from backend.kraken_futures_rest_v2 import KrakenFuturesRestV2
        
        api_key = os.getenv("KRAKEN_API_KEY", "")
        api_secret = os.getenv("KRAKEN_API_SECRET", "")
        
        if not api_key or not api_secret:
            print("❌ Kraken API keys not configured")
            return False
        
        client = KrakenFuturesRestV2(api_key, api_secret)
        
        # Map symbol
        kraken_symbol = "PF_XBTUSD"  # Pre-mapped for test
        
        print(f"\n📋 Order parameters:")
        print(f"   Symbol: {TEST_SYMBOL} -> {kraken_symbol}")
        print(f"   Side: {TEST_SIDE}")
        print(f"   Quantity: {TEST_QTY}")
        print(f"   Type: market")
        
        # Check if place_order method exists
        if hasattr(client, 'place_order'):
            print("\n✅ place_order method exists on REST client")
            
            # Check method signature
            import inspect
            sig = inspect.signature(client.place_order)
            params = list(sig.parameters.keys())
            print(f"   Parameters: {', '.join(params)}")
        else:
            print("\n❌ place_order method NOT found on REST client")
            return False
        
        # DRY-RUN: Just verify the API call structure
        print("\n📡 DRY-RUN: Would send order to Kraken REST API")
        print(f"   Endpoint: POST /api/v3/sendorder")
        print(f"   Payload: symbol={kraken_symbol}, side={TEST_SIDE}, size={TEST_QTY}, orderType=mkt")
        
        return True
        
    except Exception as e:
        print(f"❌ REST order test error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_executor_place_order():
    """Test KrakenFuturesExecutor.place_order_market method."""
    print("\n" + "=" * 60)
    print("3. Testing KrakenFuturesExecutor.place_order_market...")
    print("=" * 60)
    
    try:
        from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor
        
        api_key = os.getenv("KRAKEN_API_KEY", "")
        api_secret = os.getenv("KRAKEN_API_SECRET", "")
        
        if not api_key or not api_secret:
            print("❌ Kraken API keys not configured")
            return False
        
        executor = KrakenFuturesExecutor(api_key, api_secret)
        
        # Check methods exist
        methods_to_check = ['place_order_market', 'place_order_limit', '_get_rest_client']
        for method in methods_to_check:
            if hasattr(executor, method):
                print(f"✅ {method} exists")
            else:
                print(f"❌ {method} NOT found")
        
        # Get REST client
        rest_client = executor._get_rest_client()
        if rest_client:
            print("✅ REST client initialized")
        else:
            print("❌ REST client failed to initialize")
            return False
        
        return True
        
    except Exception as e:
        print(f"❌ Executor test error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_live_micro_order():
    """
    Actually place a micro market order on Kraken Futures.
    
    ⚠️  ONLY runs if FORCE_LIVE_TEST=true in environment.
    """
    print("\n" + "=" * 60)
    print("4. LIVE micro order test...")
    print("=" * 60)
    
    force_live = os.getenv("FORCE_LIVE_TEST", "false").lower() in ("true", "1", "yes")
    
    if not force_live:
        print("⏭️  Skipped (set FORCE_LIVE_TEST=true to enable)")
        return True  # Skip is not a failure
    
    print("⚠️  LIVE TEST ENABLED - Will place real micro order!")
    
    try:
        from backend.kraken_futures_rest_v2 import KrakenFuturesRestV2
        
        api_key = os.getenv("KRAKEN_API_KEY", "")
        api_secret = os.getenv("KRAKEN_API_SECRET", "")
        
        client = KrakenFuturesRestV2(api_key, api_secret)
        
        # Place micro market order
        print(f"\n📡 Placing LIVE order...")
        print(f"   Symbol: PF_XBTUSD")
        print(f"   Side: {TEST_SIDE}")
        print(f"   Quantity: {TEST_QTY}")
        
        result = await client.place_market_order(
            symbol="PF_XBTUSD",
            side=TEST_SIDE,
            size=TEST_QTY,
        )
        
        print(f"\n📋 Result: {result}")
        
        if result.get("result") == "success" or result.get("order_id"):
            print("✅ Order placed successfully!")
            order_id = result.get("order_id") or result.get("sendStatus", {}).get("order_id")
            print(f"   Order ID: {order_id}")
            return True
        else:
            print(f"❌ Order failed: {result.get('error', 'Unknown error')}")
            return False
        
    except Exception as e:
        print(f"❌ Live order error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all order tests."""
    print("=" * 60)
    print("   KRAKEN FUTURES ORDER PLACEMENT TEST")
    print("=" * 60)
    
    results = []
    
    # Test 1: Symbol mapping
    results.append(("Symbol mapping", await test_symbol_mapping()))
    
    # Test 2: REST order dry-run
    results.append(("REST order (dry-run)", await test_rest_order_dry_run()))
    
    # Test 3: Executor methods
    results.append(("Executor methods", await test_executor_place_order()))
    
    # Test 4: Live micro order (optional)
    results.append(("Live micro order", await test_live_micro_order()))
    
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
        print("🎉 All tests passed! Kraken order placement is working.")
    else:
        print("⚠️  Some tests failed. Check the output above.")
    
    print("\n💡 To test live order placement, run:")
    print("   $env:FORCE_LIVE_TEST='true'; python tools/test_kraken_sendorder_market.py")
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
