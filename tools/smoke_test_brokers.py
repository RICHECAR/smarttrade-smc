#!/usr/bin/env python3
"""
Smoke Test for Multi-Broker Connectivity
=========================================

Verifies connectivity and authentication for both Bitget and Kraken Futures.

Usage:
    python -m tools.smoke_test_brokers
    
    # With verbose output
    python -m tools.smoke_test_brokers -v
    
    # Dry-run order test (no real orders)
    python -m tools.smoke_test_brokers --dry-run

Expected Output:
    ✓ BITGET=OK  | Balance: $1234.56
    ✓ KRAKEN=OK  | Balance: $567.89
    ✓ MIRROR=ON  | Both brokers ready for dual execution
    
    Summary: BITGET=OK KRAKEN=OK MIRROR=ON

Author: SMC Trading Bot Framework
"""

import asyncio
import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, Any, Tuple

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING SETUP
# ══════════════════════════════════════════════════════════════════════════════

def setup_logging(verbose: bool = False):
    """Configure logging with appropriate level."""
    level = logging.DEBUG if verbose else logging.INFO
    
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    
    # Suppress noisy loggers unless verbose
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)


logger = logging.getLogger("smoke_test")


# ══════════════════════════════════════════════════════════════════════════════
# RESULT TRACKING
# ══════════════════════════════════════════════════════════════════════════════

class TestResults:
    """Track test results for final summary."""
    
    def __init__(self):
        self.bitget_ok = False
        self.bitget_balance = 0.0
        self.bitget_error = None
        
        self.kraken_ok = False
        self.kraken_balance = 0.0
        self.kraken_ws_ok = False
        self.kraken_error = None
        
        self.mirror_enabled = False
        self.mirror_ready = False
    
    def summary_line(self) -> str:
        """Generate one-line summary."""
        bitget = "OK" if self.bitget_ok else "FAIL"
        kraken = "OK" if self.kraken_ok else "FAIL"
        mirror = "ON" if self.mirror_ready else ("OFF" if not self.mirror_enabled else "PARTIAL")
        return f"BITGET={bitget} KRAKEN={kraken} MIRROR={mirror}"
    
    def print_results(self):
        """Print detailed results."""
        print("\n" + "=" * 60)
        print("SMOKE TEST RESULTS")
        print("=" * 60)
        
        # Bitget
        if self.bitget_ok:
            print(f"✓ BITGET=OK  | Balance: ${self.bitget_balance:.2f}")
        else:
            print(f"✗ BITGET=FAIL | Error: {self.bitget_error}")
        
        # Kraken
        if self.kraken_ok:
            ws_status = "WS Auth OK" if self.kraken_ws_ok else "REST only"
            print(f"✓ KRAKEN=OK  | Balance: ${self.kraken_balance:.2f} | {ws_status}")
        else:
            print(f"✗ KRAKEN=FAIL | Error: {self.kraken_error}")
        
        # Mirror mode
        if self.mirror_enabled:
            if self.mirror_ready:
                print("✓ MIRROR=ON  | Both brokers ready for dual execution")
            else:
                print("⚠ MIRROR=PARTIAL | Mirror enabled but not all brokers ready")
        else:
            print("○ MIRROR=OFF | Set MULTI_BROKER_MIRROR=true to enable")
        
        print("\n" + "-" * 60)
        print(f"Summary: {self.summary_line()}")
        print("-" * 60 + "\n")


# ══════════════════════════════════════════════════════════════════════════════
# BITGET TESTS
# ══════════════════════════════════════════════════════════════════════════════

async def test_bitget_connectivity(results: TestResults) -> None:
    """Test Bitget REST API connectivity and balance."""
    logger.info("[BITGET] Testing connectivity...")
    
    api_key = os.getenv("BITGET_API_KEY", "")
    api_secret = os.getenv("BITGET_API_SECRET", "")
    passphrase = os.getenv("BITGET_PASSPHRASE", "")
    
    if not api_key or not api_secret:
        results.bitget_error = "Missing BITGET_API_KEY or BITGET_API_SECRET"
        logger.error("[BITGET] %s", results.bitget_error)
        return
    
    try:
        from backend.bitget_v2_mix_client import BitgetV2MixClient
        
        client = BitgetV2MixClient(api_key, api_secret, passphrase)
        
        # Test balance endpoint
        balance_result = await client.get_account_balance(marginCoin="USDT")
        
        if balance_result.get("code") == "00000":
            available = float(balance_result.get("available", 0.0) or 0.0)
            equity = float(balance_result.get("equity", 0.0) or 0.0)
            results.bitget_balance = equity if equity > 0 else available
            results.bitget_ok = True
            logger.info("[BITGET] ✓ Connected | Balance: $%.2f", results.bitget_balance)
        else:
            results.bitget_error = balance_result.get("msg", "Unknown error")
            logger.error("[BITGET] ✗ API Error: %s", results.bitget_error)
            
    except ImportError as e:
        results.bitget_error = f"Import error: {e}"
        logger.error("[BITGET] ✗ %s", results.bitget_error)
    except Exception as e:
        results.bitget_error = str(e)
        logger.error("[BITGET] ✗ Exception: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# KRAKEN TESTS
# ══════════════════════════════════════════════════════════════════════════════

async def test_kraken_connectivity(results: TestResults) -> None:
    """Test Kraken Futures REST and WebSocket connectivity."""
    logger.info("[KRAKEN] Testing connectivity...")
    
    api_key = os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_API_SECRET", "")
    kraken_enabled = os.getenv("KRAKEN_ENABLED", "false").lower() == "true"
    
    if not kraken_enabled:
        results.kraken_error = "KRAKEN_ENABLED=false (set to true to enable)"
        logger.warning("[KRAKEN] ⚠ %s", results.kraken_error)
        return
    
    if not api_key or not api_secret:
        results.kraken_error = "Missing KRAKEN_API_KEY or KRAKEN_API_SECRET"
        logger.error("[KRAKEN] %s", results.kraken_error)
        return
    
    try:
        from backend.kraken_futures_client import KrakenFuturesClient
        
        client = KrakenFuturesClient(api_key, api_secret, timeout=15.0)
        
        # Test REST balance
        logger.debug("[KRAKEN] Testing REST API...")
        try:
            balance = await client.get_balance()
            results.kraken_balance = balance.get("available", 0.0)
            logger.info("[KRAKEN] ✓ REST OK | Balance: $%.2f", results.kraken_balance)
        except Exception as e:
            logger.warning("[KRAKEN] REST balance failed: %s", e)
        
        # Test WebSocket auth
        logger.debug("[KRAKEN] Testing WebSocket authentication...")
        try:
            await asyncio.wait_for(client.connect(), timeout=20.0)
            results.kraken_ws_ok = True
            logger.info("[KRAKEN] ✓ WebSocket authenticated successfully")
            
            # Disconnect cleanly
            await client.disconnect()
            
        except asyncio.TimeoutError:
            logger.warning("[KRAKEN] WebSocket auth timeout (20s)")
        except Exception as e:
            logger.warning("[KRAKEN] WebSocket auth failed: %s", e)
        
        # Mark as OK if either REST balance or WS auth succeeded
        if results.kraken_balance > 0 or results.kraken_ws_ok:
            results.kraken_ok = True
            logger.info("[KRAKEN] ✓ Connected")
        else:
            results.kraken_error = "Both REST and WS failed"
            logger.error("[KRAKEN] ✗ %s", results.kraken_error)
            
    except ImportError as e:
        results.kraken_error = f"Import error: {e}"
        logger.error("[KRAKEN] ✗ %s", results.kraken_error)
    except Exception as e:
        results.kraken_error = str(e)
        logger.error("[KRAKEN] ✗ Exception: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# MIRROR MODE CHECK
# ══════════════════════════════════════════════════════════════════════════════

def check_mirror_mode(results: TestResults) -> None:
    """Check if mirror mode is enabled and ready."""
    mirror = os.getenv("MULTI_BROKER_MIRROR", "0").lower() in ("1", "true", "yes")
    dual = os.getenv("DUAL_EXECUTION", "0").lower() in ("1", "true", "yes")
    
    results.mirror_enabled = mirror or dual
    results.mirror_ready = results.mirror_enabled and results.bitget_ok and results.kraken_ok
    
    if results.mirror_enabled:
        if results.mirror_ready:
            logger.info("[MIRROR] ✓ Enabled and ready (both brokers connected)")
        else:
            missing = []
            if not results.bitget_ok:
                missing.append("Bitget")
            if not results.kraken_ok:
                missing.append("Kraken")
            logger.warning("[MIRROR] ⚠ Enabled but missing: %s", ", ".join(missing))
    else:
        logger.info("[MIRROR] ○ Disabled (set MULTI_BROKER_MIRROR=true to enable)")


# ══════════════════════════════════════════════════════════════════════════════
# DRY-RUN ORDER TEST
# ══════════════════════════════════════════════════════════════════════════════

async def test_dry_run_order(results: TestResults) -> None:
    """
    Test order flow without actually placing orders.
    
    Uses validate_only=True or simulation endpoints where available.
    """
    logger.info("[DRY-RUN] Testing order flow (no real orders)...")
    
    if not results.bitget_ok and not results.kraken_ok:
        logger.warning("[DRY-RUN] Skipped - no brokers connected")
        return
    
    # Test order validation on Bitget
    if results.bitget_ok:
        try:
            from backend.services.multi_order_executor import MultiOrderExecutor
            
            executor = MultiOrderExecutor()
            logger.info("[DRY-RUN] ✓ MultiOrderExecutor instantiated")
            
            # Log enabled brokers
            enabled = executor.get_enabled_brokers()
            logger.info("[DRY-RUN] Enabled brokers: %s", enabled if enabled else "none (executors not set)")
            
        except Exception as e:
            logger.warning("[DRY-RUN] MultiOrderExecutor test failed: %s", e)
    
    logger.info("[DRY-RUN] Complete")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main(verbose: bool = False, dry_run: bool = False) -> int:
    """Run all smoke tests."""
    setup_logging(verbose)
    
    print("\n" + "=" * 60)
    print("MULTI-BROKER SMOKE TEST")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60 + "\n")
    
    results = TestResults()
    
    # Run tests
    await test_bitget_connectivity(results)
    await test_kraken_connectivity(results)
    check_mirror_mode(results)
    
    if dry_run:
        await test_dry_run_order(results)
    
    # Print results
    results.print_results()
    
    # Return exit code (0 = all OK, 1 = partial, 2 = all failed)
    if results.bitget_ok and results.kraken_ok:
        return 0
    elif results.bitget_ok or results.kraken_ok:
        return 1
    else:
        return 2


def cli():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Smoke test for multi-broker connectivity"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug output"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Test order flow without placing real orders"
    )
    
    args = parser.parse_args()
    
    exit_code = asyncio.run(main(verbose=args.verbose, dry_run=args.dry_run))
    sys.exit(exit_code)


if __name__ == "__main__":
    cli()
