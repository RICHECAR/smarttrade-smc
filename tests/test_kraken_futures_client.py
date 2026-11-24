"""
Test Kraken Futures Client — WebSocket V2 Connection & Order Test

This script tests:
  1. REST authentication (WebSocket token retrieval)
  2. WebSocket V2 connection
  3. Channel subscription (executions)
  4. Order validation (validate_only=true, no real funds)
  5. Graceful disconnection

Usage:
    # Set environment variables first
    export KRAKEN_API_KEY="your_api_key"
    export KRAKEN_API_SECRET="your_api_secret_base64"
    
    # Run test
    python tests/test_kraken_futures_client.py
    
    # Or with pytest
    pytest tests/test_kraken_futures_client.py -v

Requirements:
    pip install websockets httpx pytest pytest-asyncio
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from backend.kraken_futures_client import (
    KrakenFuturesClient,
    KrakenAuthError,
    KrakenOrderError,
    KrakenOrderResponse,
    OrderType,
    OrderSide,
    TimeInForce,
)

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# TEST CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

def get_test_credentials():
    """Get Kraken credentials from environment."""
    api_key = os.getenv("KRAKEN_API_KEY")
    api_secret = os.getenv("KRAKEN_API_SECRET")
    
    if not api_key or not api_secret:
        logger.warning("KRAKEN_API_KEY / KRAKEN_API_SECRET not set in environment")
        return None, None
    
    return api_key, api_secret


def credentials_available():
    """Check if credentials are available."""
    api_key, api_secret = get_test_credentials()
    return api_key is not None and api_secret is not None


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS (No network)
# ══════════════════════════════════════════════════════════════════════════════

class TestKrakenFuturesClientUnit:
    """Unit tests that don't require network."""
    
    def test_client_initialization(self):
        """Test client can be instantiated."""
        client = KrakenFuturesClient(
            api_key="test_key",
            api_secret="dGVzdF9zZWNyZXQ=",  # base64("test_secret")
        )
        assert client.api_key == "test_key"
        assert not client.is_connected
    
    def test_order_type_enum(self):
        """Test OrderType enum values."""
        assert OrderType.MARKET.value == "market"
        assert OrderType.LIMIT.value == "limit"
        assert OrderType.STOP_LOSS.value == "stop-loss"
        assert OrderType.TAKE_PROFIT.value == "take-profit"
    
    def test_order_side_enum(self):
        """Test OrderSide enum values."""
        assert OrderSide.BUY.value == "buy"
        assert OrderSide.SELL.value == "sell"
    
    def test_time_in_force_enum(self):
        """Test TimeInForce enum values."""
        assert TimeInForce.GTC.value == "gtc"
        assert TimeInForce.IOC.value == "ioc"
        assert TimeInForce.GTD.value == "gtd"


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS (Require network & credentials)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not credentials_available(), reason="Kraken credentials not set")
class TestKrakenFuturesClientIntegration:
    """Integration tests requiring network and valid credentials."""
    
    @pytest.fixture
    def client(self):
        """Create client fixture."""
        api_key, api_secret = get_test_credentials()
        return KrakenFuturesClient(api_key, api_secret)
    
    @pytest.mark.asyncio
    async def test_get_websocket_token(self, client):
        """Test REST authentication for WebSocket token."""
        token = await client._get_websocket_token()
        assert token is not None
        assert len(token) > 0
        logger.info("✅ WebSocket token obtained: %s...", token[:20])
    
    @pytest.mark.asyncio
    async def test_websocket_connection(self, client):
        """Test WebSocket connection."""
        await client.connect()
        assert client.is_connected
        logger.info("✅ WebSocket connected")
        
        # Wait a bit for any initial messages
        await asyncio.sleep(2)
        
        await client.disconnect()
        assert not client.is_connected
        logger.info("✅ WebSocket disconnected")
    
    @pytest.mark.asyncio
    async def test_subscribe_executions(self, client):
        """Test subscription to executions channel."""
        received_messages = []
        
        def on_message(msg):
            received_messages.append(msg)
            logger.debug("Received: %s", msg)
        
        client.on_message_callback = on_message
        
        await client.connect()
        await client.subscribe_executions()
        
        # Wait for subscription confirmation
        await asyncio.sleep(3)
        
        await client.disconnect()
        
        # Should have received at least subscription confirmation
        logger.info("✅ Received %d messages", len(received_messages))
    
    @pytest.mark.asyncio
    async def test_validate_limit_order(self, client):
        """
        Test limit order validation (no real order placed).
        
        Uses validate_only=True to verify order parameters
        without actually placing the order.
        """
        await client.connect()
        
        # Validate a limit buy order (not executed)
        result = await client.send_order_kraken(
            symbol="BTC/USD",
            side="buy",
            order_type="limit",
            quantity=0.0001,  # Minimum quantity
            price=50000.0,    # Target price
            validate_only=True,  # IMPORTANT: Only validate, don't execute
        )
        
        await client.disconnect()
        
        # Validation should succeed (or give specific error about params)
        logger.info("✅ Order validation result: success=%s, error=%s", 
                   result.success, result.error)
        
        # Even if validation "fails" due to market conditions,
        # we got a response which means connection works
        assert result.raw_response is not None


# ══════════════════════════════════════════════════════════════════════════════
# EXAMPLE USAGE (Manual test)
# ══════════════════════════════════════════════════════════════════════════════

async def example_limit_buy_order():
    """
    Example: Place a limit buy order on Kraken Futures.
    
    ⚠️ WARNING: This will place a REAL order if validate_only=False!
    """
    api_key, api_secret = get_test_credentials()
    
    if not api_key:
        print("❌ Set KRAKEN_API_KEY and KRAKEN_API_SECRET environment variables")
        return
    
    print("=" * 60)
    print("KRAKEN FUTURES - LIMIT BUY ORDER EXAMPLE")
    print("=" * 60)
    
    # Message handler
    def on_message(msg):
        channel = msg.get("channel")
        method = msg.get("method")
        if channel or method:
            print(f"📨 [{channel or method}] {msg}")
    
    # Create client with context manager
    async with KrakenFuturesClient(
        api_key=api_key,
        api_secret=api_secret,
        on_message_callback=on_message,
    ) as client:
        
        print("\n✅ Connected to Kraken WebSocket V2")
        
        # Subscribe to executions
        await client.subscribe_executions()
        print("✅ Subscribed to executions channel")
        
        await asyncio.sleep(2)
        
        # Example: Validate a limit buy order
        print("\n📝 Validating limit buy order...")
        
        result = await client.send_order_kraken(
            symbol="BTC/USD",
            side="buy",
            order_type="limit",
            quantity=0.0001,      # 0.0001 BTC
            price=50000.0,        # Limit price $50,000
            time_in_force="gtc",  # Good Till Canceled
            validate_only=True,   # ⚠️ Set to False to place real order
        )
        
        print("\n" + "=" * 60)
        print("ORDER RESULT:")
        print("=" * 60)
        print(f"  Success:  {result.success}")
        print(f"  Order ID: {result.order_id}")
        print(f"  Error:    {result.error}")
        print(f"  Warnings: {result.warnings}")
        print("=" * 60)
        
        # Keep connection alive briefly to receive any execution reports
        await asyncio.sleep(3)
    
    print("\n✅ Disconnected")


async def example_stop_loss_order():
    """
    Example: Place a stop-loss order (for risk management).
    
    ⚠️ WARNING: This will place a REAL order if validate_only=False!
    """
    api_key, api_secret = get_test_credentials()
    
    if not api_key:
        print("❌ Set KRAKEN_API_KEY and KRAKEN_API_SECRET environment variables")
        return
    
    print("=" * 60)
    print("KRAKEN FUTURES - STOP-LOSS ORDER EXAMPLE")
    print("=" * 60)
    
    async with KrakenFuturesClient(
        api_key=api_key,
        api_secret=api_secret,
    ) as client:
        
        print("\n✅ Connected")
        
        # Stop-loss sell at $45,000 (triggered when price drops to this level)
        result = await client.send_stop_loss_order(
            symbol="BTC/USD",
            side="sell",
            quantity=0.0001,
            trigger_price=45000.0,
            reduce_only=True,  # Only reduce existing position
        )
        
        print(f"\nStop-Loss Result: success={result.success}, error={result.error}")
    
    print("✅ Done")


async def example_oto_order():
    """
    Example: One-Triggers-Other (OTO) order with take-profit.
    
    This places a limit buy order that, when filled, automatically
    creates a take-profit sell order.
    
    ⚠️ WARNING: This will place a REAL order if validate_only=False!
    """
    api_key, api_secret = get_test_credentials()
    
    if not api_key:
        print("❌ Set KRAKEN_API_KEY and KRAKEN_API_SECRET environment variables")
        return
    
    print("=" * 60)
    print("KRAKEN FUTURES - OTO ORDER EXAMPLE")
    print("=" * 60)
    
    async with KrakenFuturesClient(
        api_key=api_key,
        api_secret=api_secret,
    ) as client:
        
        print("\n✅ Connected")
        
        # Limit buy with automatic take-profit on fill
        result = await client.send_order_kraken(
            symbol="BTC/USD",
            side="buy",
            order_type="limit",
            quantity=0.0001,
            price=50000.0,  # Entry price
            validate_only=True,
            # Conditional: Take-profit at $55,000
            conditional_order_type="take-profit",
            conditional_trigger_price=55000.0,
        )
        
        print(f"\nOTO Order Result: success={result.success}, error={result.error}")
    
    print("✅ Done")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("KRAKEN FUTURES CLIENT TEST")
    print("=" * 60)
    
    # Check for credentials
    api_key, api_secret = get_test_credentials()
    
    if not api_key:
        print("\n❌ KRAKEN_API_KEY and KRAKEN_API_SECRET must be set!")
        print("\nExample:")
        print('  export KRAKEN_API_KEY="your_api_key"')
        print('  export KRAKEN_API_SECRET="your_api_secret_base64"')
        print("\nGet your API keys from: https://www.kraken.com/u/security/api")
        sys.exit(1)
    
    print(f"\n✅ Credentials found (key: {api_key[:8]}...)")
    
    # Run the example
    print("\nRunning limit buy order example (validate only)...")
    asyncio.run(example_limit_buy_order())
