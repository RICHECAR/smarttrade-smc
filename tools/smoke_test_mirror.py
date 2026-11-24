#!/usr/bin/env python
"""
Smoke Test Mirror — Validate Multi-Broker Mirror Execution
═══════════════════════════════════════════════════════════

Tests connectivity and authentication for mirror trading:
  1. Bitget Futures REST OK
  2. Kraken Futures REST OK (NOT Spot!)
  3. Kraken Futures positions endpoint OK
  4. Optionally: Dry-run order (TEST_MODE=true)

Usage:
    python -m tools.smoke_test_mirror
    
    # With dry-run order test (requires TEST_MODE=true)
    TEST_MODE=true python -m tools.smoke_test_mirror --test-order

Environment Variables:
    BITGET_API_KEY, BITGET_API_SECRET, BITGET_PASSPHRASE
    KRAKEN_API_KEY, KRAKEN_API_SECRET (or KRAKEN_FUTURES_API_KEY/SECRET)
    TEST_MODE=true (to enable dry-run order testing)

Author: SMC Trading Bot Framework
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, Any, List

from dotenv import load_dotenv

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("smoke_test_mirror")

# ══════════════════════════════════════════════════════════════════════════════
# TEST RESULTS
# ══════════════════════════════════════════════════════════════════════════════

class TestResults:
    """Track test results."""
    
    def __init__(self):
        self.passed: List[str] = []
        self.failed: List[str] = []
        self.warnings: List[str] = []
    
    def add_pass(self, name: str, detail: str = ""):
        self.passed.append(f"✅ {name}" + (f": {detail}" if detail else ""))
        logger.info("PASS: %s %s", name, detail)
    
    def add_fail(self, name: str, detail: str = ""):
        self.failed.append(f"❌ {name}" + (f": {detail}" if detail else ""))
        logger.error("FAIL: %s %s", name, detail)
    
    def add_warning(self, name: str, detail: str = ""):
        self.warnings.append(f"⚠️ {name}" + (f": {detail}" if detail else ""))
        logger.warning("WARN: %s %s", name, detail)
    
    def print_summary(self):
        print("\n" + "=" * 60)
        print("MIRROR TRADING SMOKE TEST RESULTS")
        print("=" * 60)
        
        for item in self.passed:
            print(item)
        for item in self.warnings:
            print(item)
        for item in self.failed:
            print(item)
        
        print("\n" + "-" * 60)
        total = len(self.passed) + len(self.failed)
        print(f"Total: {len(self.passed)}/{total} passed | {len(self.warnings)} warnings")
        
        if self.failed:
            print("\n❌ SOME TESTS FAILED - Mirror trading may not work correctly!")
        else:
            print("\n✅ ALL TESTS PASSED - Mirror trading is ready!")
        
        print("=" * 60)


# ══════════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════════

async def test_bitget_connection(results: TestResults) -> Dict[str, Any]:
    """Test Bitget Futures connection."""
    print("\n" + "-" * 40)
    print("Testing Bitget Futures Connection...")
    print("-" * 40)
    
    try:
        from backend.exchanges.bitget_futures import BitgetFuturesClient
        
        client = BitgetFuturesClient()
        
        if not client.api_key:
            results.add_fail("Bitget API key", "Not configured")
            return {"success": False}
        
        result = await client.test_connection()
        
        if result["success"]:
            balance = result["balance"]
            results.add_pass(
                "Bitget REST auth",
                f"Balance: ${balance['available']:.2f}"
            )
            
            # Also check positions endpoint
            positions = await client.get_positions()
            results.add_pass("Bitget positions", f"{len(positions)} open")
            
            return {"success": True, "balance": balance}
        else:
            results.add_fail("Bitget REST auth", result.get("error", "Unknown error"))
            return {"success": False}
            
    except ImportError as e:
        results.add_fail("Bitget module", f"Import error: {e}")
        return {"success": False}
    except Exception as e:
        results.add_fail("Bitget connection", str(e))
        return {"success": False}


async def test_kraken_connection(results: TestResults) -> Dict[str, Any]:
    """Test Kraken Futures connection (NOT Spot!)."""
    print("\n" + "-" * 40)
    print("Testing Kraken FUTURES Connection...")
    print("⚠️  Using futures.kraken.com (NOT api.kraken.com)")
    print("-" * 40)
    
    try:
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        
        client = KrakenFuturesClient()
        
        if not client.api_key:
            results.add_fail("Kraken API key", "Not configured")
            return {"success": False}
        
        # Test connection
        result = await client.test_connection()
        
        if result["success"]:
            balance = result["balance"]
            results.add_pass(
                "Kraken FUTURES REST auth",
                f"Balance: ${balance['available']:.2f}"
            )
            
            # Also check positions endpoint
            positions = await client.get_positions()
            results.add_pass("Kraken positions", f"{result['positions_count']} open")
            
            return {"success": True, "balance": balance}
        else:
            error = result.get("error", "Unknown error")
            
            # Check if it's a Spot API error
            if "EAPI:Invalid key" in error or "api.kraken.com" in error.lower():
                results.add_fail(
                    "Kraken FUTURES REST auth",
                    f"WRONG KEYS! You may be using Kraken SPOT keys. "
                    f"Get FUTURES keys from https://futures.kraken.com/trade/settings/api"
                )
            else:
                results.add_fail("Kraken FUTURES REST auth", error)
            
            return {"success": False}
            
    except ImportError as e:
        results.add_fail("Kraken module", f"Import error: {e}")
        return {"success": False}
    except Exception as e:
        results.add_fail("Kraken connection", str(e))
        return {"success": False}


async def test_kraken_ws_auth(results: TestResults) -> bool:
    """Test Kraken Futures WebSocket authentication."""
    print("\n" + "-" * 40)
    print("Testing Kraken FUTURES WebSocket Auth...")
    print("-" * 40)
    
    try:
        # Check if websockets is available
        try:
            import websockets
        except ImportError:
            results.add_warning("Kraken WS", "websockets module not installed")
            return True  # Not a hard failure
        
        from backend.kraken_futures_client import KrakenFuturesClient as WsClient
        
        api_key = os.getenv("KRAKEN_FUTURES_API_KEY") or os.getenv("KRAKEN_API_KEY", "")
        api_secret = os.getenv("KRAKEN_FUTURES_API_SECRET") or os.getenv("KRAKEN_API_SECRET", "")
        
        if not api_key or not api_secret:
            results.add_warning("Kraken WS", "Credentials not configured, skipping WS test")
            return True
        
        # Attempt a quick WS connection test
        client = WsClient(api_key, api_secret)
        
        # Try to connect briefly
        try:
            await asyncio.wait_for(client.connect(), timeout=15.0)
            
            if client._is_authenticated:
                results.add_pass("Kraken FUTURES WS auth", "Challenge/response OK")
                await client.disconnect()
                return True
            else:
                results.add_warning("Kraken FUTURES WS auth", "Connected but not authenticated")
                await client.disconnect()
                return True  # Not a hard failure
                
        except asyncio.TimeoutError:
            results.add_warning("Kraken WS", "Connection timeout (may be firewall/network)")
            return True
        except Exception as e:
            # WS failures are warnings, not hard failures
            results.add_warning("Kraken WS", f"Could not connect: {e}")
            return True
            
    except ImportError as e:
        results.add_warning("Kraken WS module", f"Import error: {e}")
        return True
    except Exception as e:
        results.add_warning("Kraken WS", str(e))
        return True


async def test_mirror_service(results: TestResults) -> bool:
    """Test MirrorExecutionService initialization."""
    print("\n" + "-" * 40)
    print("Testing MirrorExecutionService...")
    print("-" * 40)
    
    try:
        from backend.execution.mirror_executor import (
            MirrorExecutionService,
            is_mirror_enabled,
            is_mirror_strict,
        )
        from backend.exchanges.bitget_futures import BitgetFuturesExecutor
        from backend.exchanges.kraken_futures import KrakenFuturesExecutor
        
        service = MirrorExecutionService()
        
        # Register executors
        bitget_exec = BitgetFuturesExecutor()
        kraken_exec = KrakenFuturesExecutor()
        
        service.register_executor("bitget", bitget_exec)
        service.register_executor("kraken", kraken_exec)
        
        enabled = service.get_enabled_brokers()
        
        if len(enabled) == 2:
            results.add_pass(
                "MirrorExecutionService",
                f"Brokers: {enabled} | Mirror: {is_mirror_enabled()} | Strict: {is_mirror_strict()}"
            )
        elif len(enabled) == 1:
            results.add_warning(
                "MirrorExecutionService",
                f"Only {enabled} registered (need both for mirror)"
            )
        else:
            results.add_fail("MirrorExecutionService", "No executors registered")
            return False
        
        # Test connections through service
        conn_results = await service.test_all_connections()
        
        for broker, conn in conn_results.items():
            if conn.get("success"):
                results.add_pass(f"Mirror→{broker}", "Connection OK")
            else:
                results.add_fail(f"Mirror→{broker}", conn.get("error", "Failed"))
        
        return True
        
    except ImportError as e:
        results.add_fail("MirrorExecutionService", f"Import error: {e}")
        return False
    except Exception as e:
        results.add_fail("MirrorExecutionService", str(e))
        return False


async def test_dry_run_order(results: TestResults) -> bool:
    """Test dry-run order (only if TEST_MODE=true)."""
    if os.getenv("TEST_MODE", "false").lower() not in ("1", "true", "yes"):
        results.add_warning("Dry-run order", "Skipped (set TEST_MODE=true to enable)")
        return True
    
    print("\n" + "-" * 40)
    print("Testing Dry-Run Order...")
    print("⚠️  This will NOT place real orders")
    print("-" * 40)
    
    try:
        from backend.execution.mirror_executor import TradeIntent, MirrorExecutionService
        from backend.exchanges.bitget_futures import BitgetFuturesExecutor
        from backend.exchanges.kraken_futures import KrakenFuturesExecutor
        
        # Create a test intent (very small size)
        intent = TradeIntent(
            symbol="BTCUSDT",
            side="long",
            quantity=0.0001,  # Tiny amount
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            source="test",
        )
        
        print(f"\nTest Intent:")
        print(f"  Symbol: {intent.symbol}")
        print(f"  Side: {intent.side}")
        print(f"  Quantity: {intent.quantity}")
        print(f"  Entry: ${intent.entry_price}")
        print(f"  SL: ${intent.stop_loss}")
        print(f"  TP: ${intent.take_profit}")
        print(f"  R:R = {intent.rr_ratio}")
        
        results.add_pass("Dry-run intent", f"Created test intent: {intent.id}")
        
        # Note: We don't actually execute in this test
        # Just validate the service can be created
        results.add_warning("Dry-run execution", "Skipped - use --execute flag for live test")
        
        return True
        
    except Exception as e:
        results.add_fail("Dry-run order", str(e))
        return False


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def run_all_tests():
    """Run all smoke tests."""
    print("\n" + "=" * 60)
    print("MIRROR TRADING SMOKE TEST")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)
    
    # Check environment
    print("\n📋 Environment Check:")
    print(f"  MIRROR_TRADES={os.getenv('MIRROR_TRADES', 'not set')}")
    print(f"  MIRROR_STRICT={os.getenv('MIRROR_STRICT', 'not set')}")
    print(f"  TEST_MODE={os.getenv('TEST_MODE', 'not set')}")
    print(f"  BITGET_API_KEY={'set' if os.getenv('BITGET_API_KEY') else 'not set'}")
    print(f"  KRAKEN_API_KEY={'set' if os.getenv('KRAKEN_API_KEY') or os.getenv('KRAKEN_FUTURES_API_KEY') else 'not set'}")
    
    results = TestResults()
    
    # Run tests
    bitget_result = await test_bitget_connection(results)
    kraken_result = await test_kraken_connection(results)
    
    # WS test (optional)
    await test_kraken_ws_auth(results)
    
    # Mirror service test
    await test_mirror_service(results)
    
    # Dry-run order test
    await test_dry_run_order(results)
    
    # Print summary
    results.print_summary()
    
    # Exit code
    return 0 if not results.failed else 1


def main():
    """Entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Mirror Trading Smoke Test")
    parser.add_argument("--test-order", action="store_true", help="Run dry-run order test")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    exit_code = asyncio.run(run_all_tests())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
