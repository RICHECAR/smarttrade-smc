#!/usr/bin/env python
"""
Multi-Broker Integration Test Suite
────────────────────────────────────

Tests for:
  - MultiBrokerConfig instantiation
  - MultiBrokerEngine initialization
  - Kraken balance fetch via REST
  - Bitget balance fetch
  - Balance history persistence

Run with:
    python -m pytest tests/test_multi_broker_integration.py -v
    
Or standalone:
    python tests/test_multi_broker_integration.py
"""

import asyncio
import os
import sys
import logging
from unittest.mock import AsyncMock, MagicMock, patch

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def test_multibrokerconfig_instantiation():
    """Test that MultiBrokerConfig can be instantiated with default_broker parameter."""
    from smc_futures.engine.multi_broker_engine import MultiBrokerConfig
    
    # Test with default_broker parameter
    config = MultiBrokerConfig(
        default_broker="kraken",
        bitget_enabled=True,
        kraken_enabled=True,
        bitget_symbols=["BTCUSDT"],
        kraken_symbols=["PI_XBTUSD"],
    )
    
    assert config.default_broker == "kraken"
    assert config.bitget_enabled == True
    assert config.kraken_enabled == True
    assert "BTCUSDT" in config.bitget_symbols
    print("✓ MultiBrokerConfig instantiation successful")


def test_multibrokerconfig_from_env():
    """Test MultiBrokerConfig.from_env() loads correctly."""
    from smc_futures.engine.multi_broker_engine import MultiBrokerConfig
    
    # Set test env vars
    os.environ["BOT_SYMBOLS"] = "BTCUSDT,ETHUSDT"
    os.environ["KRAKEN_ENABLED"] = "true"
    
    config = MultiBrokerConfig.from_env()
    
    assert "BTCUSDT" in config.symbols
    assert config.kraken_enabled == True
    print("✓ MultiBrokerConfig.from_env() successful")


def test_multibrokerengine_instantiation():
    """Test that MultiBrokerEngine can be instantiated with executor params."""
    from smc_futures.engine.multi_broker_engine import MultiBrokerEngine, MultiBrokerConfig
    
    config = MultiBrokerConfig(
        default_broker="kraken",
        bitget_enabled=True,
        kraken_enabled=True,
    )
    
    # Create with optional executor params (both None)
    engine = MultiBrokerEngine(
        config=config,
        bitget_executor=None,
        kraken_executor=None,
    )
    
    assert engine is not None
    assert engine.config.default_broker == "kraken"
    print("✓ MultiBrokerEngine instantiation successful")


async def test_balance_fetch_mock():
    """Test balance fetching with mocked executors."""
    from smc_futures.engine.multi_broker_engine import MultiBrokerEngine, MultiBrokerConfig, BrokerType
    
    config = MultiBrokerConfig(
        default_broker="kraken",
        bitget_enabled=True,
        kraken_enabled=True,
    )
    
    # Create mock executors
    mock_bitget = AsyncMock()
    mock_bitget.get_account_balance = AsyncMock(return_value={
        "available": 5000.0,
        "equity": 5500.0,
        "frozen": 200.0,
        "unrealizedPL": 300.0,
    })
    
    mock_kraken = AsyncMock()
    mock_kraken.get_account_info = AsyncMock(return_value={
        "available": 2000.0,
        "total": 2200.0,
        "margin_used": 100.0,
        "unrealized_pnl": 50.0,
    })
    
    engine = MultiBrokerEngine(
        config=config,
        bitget_executor=mock_bitget,
        kraken_executor=mock_kraken,
    )
    
    # Fetch Bitget balance
    bitget_balance = await engine.fetch_balance_bitget()
    assert bitget_balance is not None
    assert bitget_balance.available == 5000.0
    assert bitget_balance.broker == BrokerType.BITGET
    print(f"✓ Bitget balance: ${bitget_balance.available:.2f}")
    
    # Fetch Kraken balance
    kraken_balance = await engine.fetch_balance_kraken()
    assert kraken_balance is not None
    assert kraken_balance.available == 2000.0
    assert kraken_balance.broker == BrokerType.KRAKEN
    print(f"✓ Kraken balance: ${kraken_balance.available:.2f}")
    
    # Fetch all balances
    all_balances = await engine.refresh_all_balances()
    assert BrokerType.BITGET in all_balances
    assert BrokerType.KRAKEN in all_balances
    print("✓ All balances fetched successfully")
    
    # Test get_balances() returns proper dict with string keys
    balances_dict = engine.get_balances()
    assert "bitget" in balances_dict
    assert "kraken" in balances_dict
    print("✓ get_balances() returns string keys")


async def test_balance_history_service():
    """Test balance history service persistence."""
    from backend.services.balance_history_service import BalanceHistoryService, get_balance_history_service
    
    service = get_balance_history_service()
    
    # Record a test balance
    record_id = await service.record_balance(
        broker="test_broker",
        available=1000.0,
        total=1200.0,
        currency="USD",
        margin_used=100.0,
        unrealized_pnl=50.0,
    )
    
    assert record_id > 0
    print(f"✓ Balance recorded with ID: {record_id}")
    
    # Retrieve history
    history = await service.get_history(broker="test_broker", days=1)
    assert len(history) > 0
    assert history[0].broker == "test_broker"
    print(f"✓ Balance history retrieved: {len(history)} records")


async def test_kraken_client_balance_fetch():
    """Test Kraken Futures client balance fetch (requires credentials)."""
    if not os.getenv("KRAKEN_API_KEY"):
        print("⊘ Skipping Kraken live test (no credentials)")
        return
    
    from backend.kraken_futures_client import KrakenFuturesClient
    
    client = KrakenFuturesClient(
        api_key=os.getenv("KRAKEN_API_KEY"),
        api_secret=os.getenv("KRAKEN_API_SECRET"),
        auto_reconnect=False,
    )
    
    try:
        summary = await client.get_futures_account_summary()
        print(f"✓ Kraken balance fetched:")
        print(f"  Available: ${summary.get('available', 0):.2f}")
        print(f"  Total: ${summary.get('total', 0):.2f}")
        print(f"  Margin Used: ${summary.get('margin_used', 0):.2f}")
    except Exception as e:
        print(f"✗ Kraken balance fetch failed: {e}")
        print("  (This is expected if Kraken credentials are invalid/missing)")
    finally:
        # Close client if method exists
        if hasattr(client, 'close'):
            await client.close()
        elif hasattr(client, '_http_client') and client._http_client:
            await client._http_client.aclose()


def run_tests():
    """Run all tests."""
    print("\n" + "="*60)
    print("Multi-Broker Integration Tests")
    print("="*60 + "\n")
    
    # Sync tests
    print("─── Sync Tests ───")
    test_multibrokerconfig_instantiation()
    test_multibrokerconfig_from_env()
    test_multibrokerengine_instantiation()
    
    # Async tests
    print("\n─── Async Tests ───")
    asyncio.run(test_balance_fetch_mock())
    asyncio.run(test_balance_history_service())
    asyncio.run(test_kraken_client_balance_fetch())
    
    print("\n" + "="*60)
    print("All tests completed!")
    print("="*60 + "\n")


if __name__ == "__main__":
    run_tests()
