#!/usr/bin/env python3
"""
Test script for Kraken Futures balance retrieval.

This script tests the new Kraken Futures REST API integration for balance retrieval.

Usage:
    # Set environment variables first:
    export KRAKEN_API_KEY="your_api_key"
    export KRAKEN_API_SECRET="your_base64_encoded_secret"
    
    # Run test:
    python tests/test_kraken_futures_balance.py

Expected output:
    [TEST] Kraken Futures Balance Test
    [TEST] API Key: xxxxxxxx...
    [TEST] Connecting to Kraken...
    [KRAKEN_FUTURES_BALANCE] available=1234.56 USD | total=1500.00 | pnl=100.00
    [TEST] SUCCESS: Balance retrieved
    [TEST] Result: {'available': 1234.56, 'total': 1500.0, ...}
"""

import asyncio
import logging
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("test_kraken_balance")


async def test_kraken_futures_balance():
    """Test Kraken Futures balance retrieval using the new REST API."""
    
    api_key = os.getenv("KRAKEN_API_KEY")
    api_secret = os.getenv("KRAKEN_API_SECRET")
    
    if not api_key or not api_secret:
        logger.error("[TEST] KRAKEN_API_KEY or KRAKEN_API_SECRET not set")
        logger.info("[TEST] Please set environment variables:")
        logger.info("  export KRAKEN_API_KEY='your_key'")
        logger.info("  export KRAKEN_API_SECRET='your_base64_secret'")
        return False
    
    logger.info("[TEST] Kraken Futures Balance Test")
    logger.info("[TEST] API Key: %s...", api_key[:8])
    
    try:
        from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor
        
        executor = KrakenFuturesExecutor(
            api_key=api_key,
            api_secret=api_secret,
            timeout=15.0,
            auto_reconnect=False,
        )
        
        logger.info("[TEST] Connecting to Kraken WebSocket...")
        await executor.connect()
        logger.info("[TEST] Connected successfully")
        
        # Test 1: Get account info
        logger.info("[TEST] Fetching account balance...")
        balance = await executor.get_account_info()
        
        if "error" in balance:
            logger.error("[TEST] FAILED: %s", balance.get("error"))
            return False
        
        logger.info("[TEST] SUCCESS: Balance retrieved")
        logger.info("[TEST] Result: %s", balance)
        
        # Test 2: Check balance sufficient
        logger.info("[TEST] Testing balance sufficiency check...")
        is_sufficient, available = await executor.check_balance_sufficient(10.0)
        logger.info("[TEST] Is $10 sufficient? %s (available: $%.2f)", is_sufficient, available)
        
        # Disconnect
        await executor.disconnect()
        logger.info("[TEST] Disconnected")
        
        return True
        
    except Exception as e:
        logger.error("[TEST] EXCEPTION: %s", e, exc_info=True)
        return False


async def test_kraken_futures_client_direct():
    """Test the KrakenFuturesClient directly (lower level)."""
    
    api_key = os.getenv("KRAKEN_API_KEY")
    api_secret = os.getenv("KRAKEN_API_SECRET")
    
    if not api_key or not api_secret:
        logger.error("[TEST] Credentials not set")
        return False
    
    logger.info("[TEST] Testing KrakenFuturesClient directly...")
    
    try:
        from backend.kraken_futures_client import KrakenFuturesClient
        
        client = KrakenFuturesClient(
            api_key=api_key,
            api_secret=api_secret,
            timeout=15.0,
            auto_reconnect=False,
        )
        
        # Test Futures REST API (no WebSocket needed)
        logger.info("[TEST] Fetching Futures accounts (REST API)...")
        
        accounts = await client.get_futures_accounts()
        logger.info("[TEST] Raw accounts response: %s", accounts)
        
        summary = await client.get_futures_account_summary()
        logger.info("[TEST] Account summary: %s", summary)
        
        usd_balance = await client.get_futures_balance_usd()
        logger.info("[TEST] USD balance: %.2f", usd_balance)
        
        return True
        
    except Exception as e:
        logger.error("[TEST] EXCEPTION: %s", e, exc_info=True)
        return False


if __name__ == "__main__":
    print("=" * 60)
    print(" KRAKEN FUTURES BALANCE TEST")
    print("=" * 60)
    
    # Run direct client test first (no WebSocket needed)
    print("\n--- Test 1: Direct Client Test ---")
    result1 = asyncio.run(test_kraken_futures_client_direct())
    
    # Run executor test (requires WebSocket)
    print("\n--- Test 2: Executor Test ---")
    result2 = asyncio.run(test_kraken_futures_balance())
    
    print("\n" + "=" * 60)
    print(" RESULTS")
    print("=" * 60)
    print(f" Direct Client Test: {'PASS' if result1 else 'FAIL'}")
    print(f" Executor Test:      {'PASS' if result2 else 'FAIL'}")
    print("=" * 60)
    
    sys.exit(0 if (result1 and result2) else 1)
