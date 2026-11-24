#!/usr/bin/env python3
"""
CLI tool to test Kraken Futures WebSocket connection and authentication.

Usage:
    # With env vars already set:
    python test_kraken_connection.py

    # With explicit credentials:
    python test_kraken_connection.py --api-key YOUR_KEY --api-secret YOUR_SECRET

    # Verbose mode:
    python test_kraken_connection.py -v

Exit codes:
    0 - Success (connected and authenticated)
    1 - Connection/auth failure
    2 - Missing credentials
"""

import argparse
import asyncio
import logging
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.kraken_futures_client import KrakenFuturesClient, KrakenAuthError


def setup_logging(verbose: bool = False) -> None:
    """Configure logging based on verbosity."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Reduce noise from external libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def test_connection(api_key: str, api_secret: str, timeout: float = 30.0) -> bool:
    """
    Test Kraken Futures WS connection and authentication.
    
    Returns:
        True if successful, False otherwise
    """
    logger = logging.getLogger(__name__)
    
    logger.info("=" * 60)
    logger.info("KRAKEN FUTURES WEBSOCKET CONNECTION TEST")
    logger.info("=" * 60)
    logger.info("API Key: %s...%s", api_key[:4], api_key[-4:])
    logger.info("Endpoint: wss://futures.kraken.com/ws/v1")
    logger.info("-" * 60)
    
    client = KrakenFuturesClient(api_key=api_key, api_secret=api_secret)
    
    try:
        logger.info("[1/3] Connecting to WebSocket...")
        await asyncio.wait_for(client.connect(), timeout=timeout)
        
        logger.info("[2/3] Connection established, verifying auth...")
        
        if client.is_connected:
            logger.info("[3/3] ✓ AUTHENTICATED SUCCESSFULLY")
            logger.info("-" * 60)
            
            # Try to get balance as additional verification
            logger.info("Bonus: Fetching account balance...")
            try:
                balance = await asyncio.wait_for(client.get_balance(), timeout=10.0)
                if balance:
                    logger.info("  ✓ Balance retrieved: %.4f USDT", balance.get("availableBalance", 0))
                else:
                    logger.info("  - Balance: (no data)")
            except Exception as e:
                logger.warning("  - Balance fetch failed: %s", e)
            
            logger.info("-" * 60)
            logger.info("RESULT: SUCCESS")
            return True
        else:
            logger.error("Connection flag not set despite no exception")
            return False
            
    except KrakenAuthError as e:
        logger.error("")
        logger.error("!" * 60)
        logger.error("AUTHENTICATION FAILED: %s", e)
        logger.error("!" * 60)
        logger.error("")
        logger.error("Possible causes:")
        logger.error("  1. Invalid API key or secret")
        logger.error("  2. API key doesn't have Futures permissions")
        logger.error("  3. API key is for Spot, not Futures")
        logger.error("  4. IP restriction on API key")
        return False
        
    except asyncio.TimeoutError:
        logger.error("Connection timed out after %.0fs", timeout)
        return False
        
    except ConnectionError as e:
        logger.error("Connection error: %s", e)
        return False
        
    except Exception as e:
        logger.exception("Unexpected error: %s", e)
        return False
        
    finally:
        # Always disconnect
        if client.is_connected:
            logger.info("Disconnecting...")
            try:
                await client.disconnect()
                logger.info("Disconnected cleanly")
            except Exception as e:
                logger.warning("Disconnect error: %s", e)


def main():
    parser = argparse.ArgumentParser(
        description="Test Kraken Futures WebSocket connection and authentication",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Use environment variables:
    export KRAKEN_API_KEY=your_key
    export KRAKEN_API_SECRET=your_secret
    python test_kraken_connection.py

    # Use command line args:
    python test_kraken_connection.py --api-key KEY --api-secret SECRET

    # Verbose mode (shows all messages):
    python test_kraken_connection.py -v
        """
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("KRAKEN_API_KEY"),
        help="Kraken API key (or set KRAKEN_API_KEY env var)"
    )
    parser.add_argument(
        "--api-secret",
        default=os.getenv("KRAKEN_API_SECRET"),
        help="Kraken API secret (or set KRAKEN_API_SECRET env var)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Connection timeout in seconds (default: 30)"
    )
    
    args = parser.parse_args()
    
    # Check credentials
    if not args.api_key or not args.api_secret:
        print("ERROR: Missing credentials!")
        print("")
        print("Set environment variables:")
        print("  export KRAKEN_API_KEY=your_key")
        print("  export KRAKEN_API_SECRET=your_secret")
        print("")
        print("Or use command line arguments:")
        print("  python test_kraken_connection.py --api-key KEY --api-secret SECRET")
        sys.exit(2)
    
    setup_logging(args.verbose)
    
    # Run async test
    success = asyncio.run(test_connection(
        api_key=args.api_key,
        api_secret=args.api_secret,
        timeout=args.timeout
    ))
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
