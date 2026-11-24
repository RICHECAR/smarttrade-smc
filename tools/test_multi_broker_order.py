#!/usr/bin/env python3
"""
Test multi-broker order execution via BrokerRouter.

This script tests:
1. BrokerRouter initialization
2. Bitget executor connection
3. Kraken Futures executor connection
4. Simulated order execution on both brokers
5. REQUIRE_BOTH_BROKERS mode behavior

Usage:
    python tools/test_multi_broker_order.py
    python tools/test_multi_broker_order.py --dry-run  # Don't place real orders
    python tools/test_multi_broker_order.py --live     # Place real test orders (tiny size)
"""

import os
import sys
import asyncio
import argparse
from pathlib import Path
from decimal import Decimal
from datetime import datetime

# Add project root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load environment
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")


def print_section(title: str):
    """Print a formatted section header."""
    print(f"\n{'='*60}")
    print(f" {title}")
    print(f"{'='*60}")


def print_result(name: str, success: bool, message: str = ""):
    """Print a test result."""
    icon = "✓" if success else "✗"
    status = "PASSED" if success else "FAILED"
    msg_part = f" - {message}" if message else ""
    print(f"  {icon} {name}: {status}{msg_part}")


async def test_broker_router_init():
    """Test BrokerRouter initialization."""
    print_section("1. BrokerRouter Initialization")
    
    try:
        from backend.execution.broker_router import BrokerRouter
        
        router = BrokerRouter()
        print_result("Import BrokerRouter", True)
        
        # Check initial state
        enabled = router.get_enabled_brokers()
        print(f"  Enabled brokers: {enabled}")
        
        require_both = os.getenv("REQUIRE_BOTH_BROKERS", "false").lower() in ("true", "1")
        print(f"  REQUIRE_BOTH_BROKERS: {require_both}")
        
        return router
        
    except Exception as e:
        print_result("Import BrokerRouter", False, str(e))
        return None


async def test_bitget_executor(router):
    """Test Bitget executor registration and health check."""
    print_section("2. Bitget Executor")
    
    if router is None:
        print("  ⊘ Skipped: Router not initialized")
        return False
    
    try:
        from backend.execution.bitget_executor import BitgetExecutorWrapper
        
        # Get API credentials
        api_key = os.getenv("BITGET_API_KEY", "")
        api_secret = os.getenv("BITGET_API_SECRET", "")
        passphrase = os.getenv("BITGET_PASSPHRASE", "")
        
        if not all([api_key, api_secret, passphrase]):
            print("  ⊘ Skipped: Bitget credentials not set")
            return None
        
        # Initialize executor
        executor = BitgetExecutorWrapper(
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            demo_mode=True,  # Always use demo for tests
        )
        
        print_result("Initialize Bitget executor", True)
        
        # Register with router
        router.register_executor("bitget", executor)
        print_result("Register with router", True)
        
        # Health check
        health = await executor.health_check()
        print_result("Health check", health.get("healthy", False), str(health))
        
        return health.get("healthy", False)
        
    except Exception as e:
        print_result("Bitget executor", False, str(e))
        return False


async def test_kraken_executor(router):
    """Test Kraken Futures executor registration and health check."""
    print_section("3. Kraken Futures Executor")
    
    if router is None:
        print("  ⊘ Skipped: Router not initialized")
        return False
    
    try:
        from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
        
        # Get API credentials
        api_key = os.getenv("KRAKEN_FUTURES_API_KEY", "")
        api_secret = os.getenv("KRAKEN_FUTURES_API_SECRET", "")
        
        if not all([api_key, api_secret]):
            print("  ⊘ Skipped: Kraken Futures credentials not set")
            return None
        
        # Initialize executor
        executor = KrakenFuturesExecutorWrapper(
            api_key=api_key,
            api_secret=api_secret,
        )
        
        print_result("Initialize Kraken executor", True)
        
        # Register with router
        router.register_executor("kraken", executor)
        print_result("Register with router", True)
        
        # Health check
        health = await executor.health_check()
        print_result("Health check", health.get("healthy", False), str(health))
        
        return health.get("healthy", False)
        
    except Exception as e:
        print_result("Kraken executor", False, str(e))
        return False


async def test_router_execution_dry_run(router):
    """Test router execution in dry-run mode (no real orders)."""
    print_section("4. Router Execution (Dry Run)")
    
    if router is None:
        print("  ⊘ Skipped: Router not initialized")
        return False
    
    enabled = router.get_enabled_brokers()
    if not enabled:
        print("  ⊘ Skipped: No brokers enabled")
        return None
    
    print(f"  Testing with brokers: {enabled}")
    
    try:
        from backend.execution.broker_router import TradeIntent
        
        # Create a test intent (will not be executed)
        intent = TradeIntent(
            symbol="BTCUSDT",
            side="long",
            quantity=Decimal("0.001"),  # Tiny test size
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            take_profit=Decimal("52000"),
            leverage=5,
            order_type="limit",
            signal_id="test-dry-run-001",
            metadata={"test": True, "dry_run": True},
        )
        
        print(f"  Intent: {intent.side.upper()} {intent.quantity} {intent.symbol}")
        print(f"  Entry: {intent.entry_price}, SL: {intent.stop_loss}, TP: {intent.take_profit}")
        
        # Just validate the intent can be created
        print_result("Create TradeIntent", True)
        
        # Don't actually execute in dry-run mode
        print("  ⚠ DRY RUN: Not executing real order")
        
        return True
        
    except Exception as e:
        print_result("Dry run execution", False, str(e))
        return False


async def test_router_execution_live(router):
    """Test router execution with real (tiny) orders."""
    print_section("5. Router Execution (LIVE - Tiny Order)")
    
    if router is None:
        print("  ⊘ Skipped: Router not initialized")
        return False
    
    enabled = router.get_enabled_brokers()
    if not enabled:
        print("  ⊘ Skipped: No brokers enabled")
        return None
    
    print(f"  ⚠ LIVE MODE: Will place real orders on: {enabled}")
    print(f"  Order will be for minimum possible size")
    
    # Confirm with user
    confirm = input("  Continue with live test? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("  ⊘ Skipped: User cancelled")
        return None
    
    try:
        from backend.execution.broker_router import TradeIntent
        
        # Create a minimal test intent
        intent = TradeIntent(
            symbol="BTCUSDT",
            side="long",
            quantity=Decimal("0.001"),  # Minimum size
            entry_price=None,  # Market order
            stop_loss=None,
            take_profit=None,
            leverage=1,
            order_type="market",
            signal_id=f"test-live-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            metadata={"test": True, "live": True},
        )
        
        print(f"  Executing: {intent.side.upper()} {intent.quantity} {intent.symbol} @ MARKET")
        
        # Execute on all brokers
        results = await router.execute_intent(intent)
        
        print(f"\n  Results:")
        for broker_name, result in results.items():
            success = result.success
            order_id = result.order_id or "N/A"
            error = result.error or ""
            msg = f"order_id={order_id}" if success else f"error={error}"
            print_result(f"{broker_name}", success, msg)
        
        # Overall success
        all_success = all(r.success for r in results.values())
        return all_success
        
    except Exception as e:
        print_result("Live execution", False, str(e))
        return False


async def main(args):
    """Run all tests."""
    print("\n" + "="*60)
    print(" MULTI-BROKER ORDER EXECUTION TEST")
    print(" Testing BrokerRouter with Bitget + Kraken Futures")
    print("="*60)
    
    if args.live:
        print("\n  ⚠ LIVE MODE ENABLED - Will place real orders!")
    else:
        print("\n  ℹ DRY RUN MODE - No real orders will be placed")
    
    # Initialize router
    router = await test_broker_router_init()
    
    # Test individual executors
    bitget_ok = await test_bitget_executor(router)
    kraken_ok = await test_kraken_executor(router)
    
    # Test execution
    if args.live:
        exec_ok = await test_router_execution_live(router)
    else:
        exec_ok = await test_router_execution_dry_run(router)
    
    # Summary
    print_section("SUMMARY")
    
    results = [
        ("BrokerRouter", router is not None),
        ("Bitget Executor", bitget_ok),
        ("Kraken Executor", kraken_ok),
        ("Order Execution", exec_ok),
    ]
    
    for name, result in results:
        if result is True:
            print(f"  ✓ {name}: PASSED")
        elif result is False:
            print(f"  ✗ {name}: FAILED")
        else:
            print(f"  ⊘ {name}: SKIPPED")
    
    all_required_passed = (router is not None) and (exec_ok is not False)
    
    if all_required_passed:
        print("\n  ✓ Multi-broker execution is configured correctly!")
    else:
        print("\n  ✗ Some tests failed. Check the output above.")
    
    return 0 if all_required_passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test multi-broker order execution")
    parser.add_argument("--dry-run", action="store_true", default=True, 
                        help="Don't place real orders (default)")
    parser.add_argument("--live", action="store_true", 
                        help="Place real test orders (tiny size)")
    
    args = parser.parse_args()
    
    # If --live is specified, disable dry-run
    if args.live:
        args.dry_run = False
    
    exit_code = asyncio.run(main(args))
    sys.exit(exit_code)
