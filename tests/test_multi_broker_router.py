"""
Test Multi-Broker Router — Unit tests and integration tests.

Usage:
    # Unit tests (no network)
    pytest tests/test_multi_broker_router.py -v -k unit
    
    # Integration tests (requires credentials)
    export KRAKEN_API_KEY="your_key"
    export KRAKEN_API_SECRET="your_secret"
    export BITGET_API_KEY="your_key"
    export BITGET_API_SECRET="your_secret"
    export BITGET_PASSPHRASE="your_passphrase"
    pytest tests/test_multi_broker_router.py -v -k integration

Requirements:
    pip install pytest pytest-asyncio
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.engine.execution.multi_broker_router import (
    MultiBrokerRouter,
    BrokerType,
    OrderSide,
    OrderType,
    OrderResult,
    BalanceInfo,
    BalanceError,
    InsufficientBalanceError,
    BrokerNotAvailableError,
    send_order,
)

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def basic_config():
    """Basic config without real credentials."""
    return {
        "DEFAULT_BROKER": "bitget",
        "BALANCE_CHECK_ENABLED": True,
        "KRAKEN_ENABLED": False,
    }


@pytest.fixture
def mock_config():
    """Config with mock credentials."""
    return {
        "BITGET_API_KEY": "test_bitget_key",
        "BITGET_API_SECRET": "test_bitget_secret",
        "BITGET_PASSPHRASE": "test_passphrase",
        "KRAKEN_API_KEY": "test_kraken_key",
        "KRAKEN_API_SECRET": "dGVzdF9zZWNyZXQ=",  # base64
        "KRAKEN_ENABLED": True,
        "DEFAULT_BROKER": "bitget",
        "BALANCE_CHECK_ENABLED": True,
    }


def get_real_config():
    """Get config from environment (for integration tests)."""
    return {
        "BITGET_API_KEY": os.getenv("BITGET_API_KEY"),
        "BITGET_API_SECRET": os.getenv("BITGET_API_SECRET"),
        "BITGET_PASSPHRASE": os.getenv("BITGET_PASSPHRASE"),
        "KRAKEN_API_KEY": os.getenv("KRAKEN_API_KEY"),
        "KRAKEN_API_SECRET": os.getenv("KRAKEN_API_SECRET"),
        "KRAKEN_ENABLED": os.getenv("KRAKEN_ENABLED", "false").lower() == "true",
        "DEFAULT_BROKER": "bitget",
        "BALANCE_CHECK_ENABLED": True,
    }


def has_kraken_credentials():
    """Check if Kraken credentials are available."""
    return all([
        os.getenv("KRAKEN_API_KEY"),
        os.getenv("KRAKEN_API_SECRET"),
    ])


def has_bitget_credentials():
    """Check if Bitget credentials are available."""
    return all([
        os.getenv("BITGET_API_KEY"),
        os.getenv("BITGET_API_SECRET"),
        os.getenv("BITGET_PASSPHRASE"),
    ])


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestMultiBrokerRouterUnit:
    """Unit tests that don't require network."""
    
    def test_router_initialization(self, basic_config):
        """Test router can be instantiated."""
        router = MultiBrokerRouter(basic_config)
        assert router.default_broker == BrokerType.BITGET
        assert router.balance_check_enabled is True
    
    def test_broker_type_enum(self):
        """Test BrokerType enum values."""
        assert BrokerType.BITGET.value == "bitget"
        assert BrokerType.KRAKEN.value == "kraken"
    
    def test_order_side_enum(self):
        """Test OrderSide enum values."""
        assert OrderSide.BUY.value == "buy"
        assert OrderSide.SELL.value == "sell"
    
    def test_order_type_enum(self):
        """Test OrderType enum values."""
        assert OrderType.MARKET.value == "market"
        assert OrderType.LIMIT.value == "limit"
    
    def test_order_result_dataclass(self):
        """Test OrderResult dataclass."""
        result = OrderResult(
            success=True,
            broker=BrokerType.BITGET,
            order_id="test123",
            fill_price=50000.0,
        )
        assert result.success is True
        assert result.executed is True
        assert result.broker == BrokerType.BITGET
        assert result.order_id == "test123"
    
    def test_balance_info_dataclass(self):
        """Test BalanceInfo dataclass."""
        balance = BalanceInfo(
            broker=BrokerType.KRAKEN,
            available=10000.0,
            total=15000.0,
            currency="USD",
        )
        assert balance.available == 10000.0
        assert balance.broker == BrokerType.KRAKEN
    
    def test_insufficient_balance_error(self):
        """Test InsufficientBalanceError."""
        error = InsufficientBalanceError("kraken", 1000.0, 500.0)
        assert error.broker == "kraken"
        assert error.required == 1000.0
        assert error.available == 500.0
        assert "kraken" in str(error)
    
    def test_broker_not_available_error(self):
        """Test BrokerNotAvailableError."""
        error = BrokerNotAvailableError("Broker not initialized")
        assert "not initialized" in str(error)
    
    @pytest.mark.asyncio
    async def test_send_order_broker_not_available(self, basic_config):
        """Test order rejection when broker not available."""
        router = MultiBrokerRouter(basic_config)
        # Don't initialize - broker should not be available
        
        result = await router.send_order(
            symbol="BTCUSDT",
            qty=0.001,
            side="buy",
            broker="bitget",
        )
        
        assert result.success is False
        assert result.error_code == "BROKER_NOT_AVAILABLE"
    
    def test_price_estimation(self, basic_config):
        """Test price estimation for margin calculation."""
        router = MultiBrokerRouter(basic_config)
        
        assert router._estimate_price("BTCUSDT") == 50000.0
        assert router._estimate_price("BTC/USD") == 50000.0
        assert router._estimate_price("ETHUSDT") == 3000.0
        assert router._estimate_price("SOL/USD") == 100.0
        assert router._estimate_price("UNKNOWN") == 1.0
    
    def test_is_broker_available(self, basic_config):
        """Test broker availability check."""
        router = MultiBrokerRouter(basic_config)
        
        assert router.is_broker_available("bitget") is False
        assert router.is_broker_available(BrokerType.BITGET) is False
        assert router.bitget_available is False
        assert router.kraken_available is False
    
    def test_get_active_brokers(self, basic_config):
        """Test getting active brokers list."""
        router = MultiBrokerRouter(basic_config)
        
        assert router.get_active_brokers() == []


class TestSignalExecution:
    """Test signal-based order execution."""
    
    @pytest.mark.asyncio
    async def test_execute_signal_invalid(self, basic_config):
        """Test signal execution with invalid signal."""
        router = MultiBrokerRouter(basic_config)
        
        # Missing required fields
        result = await router.execute_signal({
            "symbol": "BTCUSDT",
            # Missing: direction, entry, stop_loss, take_profit
        })
        
        assert result.success is False
        assert result.error_code == "INVALID_SIGNAL"
    
    @pytest.mark.asyncio
    async def test_execute_signal_invalid_direction(self, basic_config):
        """Test signal execution with invalid direction."""
        router = MultiBrokerRouter(basic_config)
        
        result = await router.execute_signal({
            "symbol": "BTCUSDT",
            "direction": "invalid",
            "entry": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "qty": 0.001,
        })
        
        assert result.success is False
        assert result.error_code == "INVALID_DIRECTION"


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS (require credentials)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not has_kraken_credentials(), reason="Kraken credentials not set")
class TestKrakenIntegration:
    """Integration tests for Kraken."""
    
    @pytest.fixture
    def config(self):
        config = get_real_config()
        config["KRAKEN_ENABLED"] = True
        return config
    
    @pytest.mark.asyncio
    async def test_kraken_initialization(self, config):
        """Test Kraken client initialization."""
        router = MultiBrokerRouter(config)
        await router._init_kraken()
        
        assert router.kraken_available is True
        
        await router.shutdown()
    
    @pytest.mark.asyncio
    async def test_kraken_balance_check(self, config):
        """Test Kraken balance retrieval."""
        async with MultiBrokerRouter(config) as router:
            if not router.kraken_available:
                pytest.skip("Kraken not available")
            
            try:
                balance = await router.get_balance("kraken")
            except BalanceError as e:
                if "Invalid API key" in str(e):
                    pytest.skip("Kraken credentials present but invalid")
                raise
            
            assert balance.broker == BrokerType.KRAKEN
            assert balance.currency == "USD"
            logger.info("Kraken balance: available=%.2f, total=%.2f",
                       balance.available, balance.total)


@pytest.mark.skipif(not has_bitget_credentials(), reason="Bitget credentials not set")
class TestBitgetIntegration:
    """Integration tests for Bitget."""
    
    @pytest.fixture
    def config(self):
        return get_real_config()
    
    @pytest.mark.asyncio
    async def test_bitget_initialization(self, config):
        """Test Bitget executor initialization."""
        router = MultiBrokerRouter(config)
        await router._init_bitget()
        
        assert router.bitget_available is True
        
        await router.shutdown()


# ══════════════════════════════════════════════════════════════════════════════
# MOCK TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestMultiBrokerRouterMocked:
    """Tests with mocked broker clients."""
    
    @pytest.mark.asyncio
    async def test_send_order_with_mocked_kraken(self):
        """Test order flow with mocked Kraken client."""
        config = {
            "DEFAULT_BROKER": "kraken",
            "BALANCE_CHECK_ENABLED": False,
            "KRAKEN_ENABLED": True,
        }
        
        router = MultiBrokerRouter(config)
        
        # Mock the executor wrapper directly (the router creates this internally)
        mock_exec = AsyncMock()
        mock_exec.place_order = AsyncMock(return_value={
            "success": True,
            "order_id": "kraken_order_123",
            "code": "00000",
            "msg": None,
            "data": {},
        })
        
        router._kraken_client = AsyncMock()
        router._kraken_executor = mock_exec
        router._broker_status[BrokerType.KRAKEN] = True
        
        result = await router.send_order(
            symbol="BTC/USD",
            qty=0.001,
            side="buy",
            broker="kraken",
            price=50000.0,
        )
        
        assert result.success is True
        assert result.order_id == "kraken_order_123"
        assert result.broker == BrokerType.KRAKEN
        
        mock_exec.place_order.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_balance_check_flow(self):
        """Test balance check before order."""
        config = {
            "DEFAULT_BROKER": "kraken",
            "BALANCE_CHECK_ENABLED": True,
            "KRAKEN_ENABLED": True,
        }
        
        router = MultiBrokerRouter(config)
        
        # Mock Kraken client with balance
        mock_kraken = AsyncMock()
        mock_kraken.get_trade_balance = AsyncMock(return_value={
            "mf": 500.0,  # Free margin
            "tb": 1000.0,  # Trade balance
            "e": 1000.0,   # Equity
        })
        
        # Mock the executor wrapper directly
        mock_exec = AsyncMock()
        mock_exec.place_order = AsyncMock(return_value={
            "success": True,
            "order_id": "order_123",
            "code": "00000",
            "msg": None,
            "data": {},
        })
        
        router._kraken_client = mock_kraken
        router._kraken_executor = mock_exec
        router._broker_status[BrokerType.KRAKEN] = True
        
        # Order requiring $100 margin should succeed
        result = await router.send_order(
            symbol="BTC/USD",
            qty=0.001,
            side="buy",
            broker="kraken",
            price=50000.0,
            required_margin=100.0,
        )
        
        assert result.success is True
    
    @pytest.mark.asyncio
    async def test_insufficient_balance_rejection(self):
        """Test order rejection when balance insufficient."""
        config = {
            "DEFAULT_BROKER": "kraken",
            "BALANCE_CHECK_ENABLED": True,
            "KRAKEN_ENABLED": True,
        }
        
        router = MultiBrokerRouter(config)
        
        # Mock Kraken client with low balance
        mock_kraken = AsyncMock()
        mock_kraken.get_trade_balance = AsyncMock(return_value={
            "mf": 50.0,  # Only $50 free margin
            "tb": 100.0,
            "e": 100.0,
        })
        
        router._kraken_client = mock_kraken
        router._broker_status[BrokerType.KRAKEN] = True
        
        # Order requiring $500 margin should fail
        # Note: InsufficientBalanceError inherits from BalanceError and is caught
        # internally by the router, returning an OrderResult(success=False)
        result = await router.send_order(
            symbol="BTC/USD",
            qty=0.001,
            side="buy",
            broker="kraken",
            price=50000.0,
            required_margin=500.0,
        )
        
        assert result.success is False
        assert result.error_code == "BALANCE_CHECK_FAILED"


# ══════════════════════════════════════════════════════════════════════════════
# EXAMPLE USAGE
# ══════════════════════════════════════════════════════════════════════════════

async def example_multi_broker_usage():
    """
    Example: Using MultiBrokerRouter with both Bitget and Kraken.
    """
    print("=" * 60)
    print("MULTI-BROKER ROUTER EXAMPLE")
    print("=" * 60)
    
    # Configuration from environment
    config = {
        "BITGET_API_KEY": os.getenv("BITGET_API_KEY"),
        "BITGET_API_SECRET": os.getenv("BITGET_API_SECRET"),
        "BITGET_PASSPHRASE": os.getenv("BITGET_PASSPHRASE"),
        "KRAKEN_API_KEY": os.getenv("KRAKEN_API_KEY"),
        "KRAKEN_API_SECRET": os.getenv("KRAKEN_API_SECRET"),
        "KRAKEN_ENABLED": os.getenv("KRAKEN_ENABLED", "false").lower() == "true",
        "DEFAULT_BROKER": "bitget",
        "BALANCE_CHECK_ENABLED": True,
    }
    
    async with MultiBrokerRouter(config) as router:
        print(f"\n✅ Active brokers: {[b.value for b in router.get_active_brokers()]}")
        
        # Example 1: Check balances
        print("\n--- Balance Check ---")
        for broker in router.get_active_brokers():
            try:
                balance = await router.get_balance(broker)
                print(f"{broker.value}: ${balance.available:.2f} available / ${balance.total:.2f} total")
            except Exception as e:
                print(f"{broker.value}: Error - {e}")
        
        # Example 2: Execute a signal
        print("\n--- Signal Execution (validate only) ---")
        signal = {
            "symbol": "BTC/USD" if router.kraken_available else "BTCUSDT",
            "direction": "long",
            "entry": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "qty": 0.0001,  # Very small quantity for testing
        }
        
        # Note: In real usage, remove validate_balance=False
        print(f"Signal: {signal}")
        
        # Example 3: Direct order (market)
        print("\n--- Direct Order (not executed - example only) ---")
        print("Would send: router.send_order(symbol='BTC/USD', qty=0.001, side='buy', broker='kraken')")
    
    print("\n✅ Example complete")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("MULTI-BROKER ROUTER TEST")
    print("=" * 60)
    
    # Run example
    asyncio.run(example_multi_broker_usage())
