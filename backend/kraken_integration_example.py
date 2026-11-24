"""
Kraken Futures Integration Example

This module demonstrates how to use KrakenFuturesClient alongside
the existing Bitget integration in the bot.

UPDATED: Now uses MultiBrokerRouter for unified order handling.

Requirements:
    pip install websockets httpx python-dotenv

Configuration (.env):
    KRAKEN_ENABLED=true
    KRAKEN_API_KEY=your_kraken_api_key
    KRAKEN_API_SECRET=your_kraken_api_secret_base64

Usage:
    # Single order
    from backend.kraken_integration_example import send_trade
    result = await send_trade(
        symbol="BTC/USD",
        side="buy",
        qty=0.001,
        price=50000.0,
        broker="kraken",
    )
    
    # With signal
    from backend.kraken_integration_example import execute_trading_signal
    result = await execute_trading_signal(signal_dict, broker="kraken")
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional

# Load .env before imports
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from backend.kraken_futures_client import (
    KrakenFuturesClient,
    KrakenOrderResponse,
    KrakenAuthError,
    KrakenOrderError,
)
from backend.engine.execution.multi_broker_router import (
    MultiBrokerRouter,
    BrokerType,
    OrderResult,
    send_order,
)

logger = logging.getLogger("smarttrade.kraken_integration")


# ══════════════════════════════════════════════════════════════════════════════
# CLIENT FACTORY
# ══════════════════════════════════════════════════════════════════════════════

def is_kraken_enabled() -> bool:
    """Check if Kraken is enabled in configuration."""
    return os.getenv("KRAKEN_ENABLED", "false").lower() == "true"


def get_kraken_client() -> Optional[KrakenFuturesClient]:
    """
    Get configured Kraken client from environment.
    
    Returns:
        KrakenFuturesClient if enabled and configured, None otherwise.
        
    Raises:
        ValueError: If enabled but credentials missing.
    """
    if not is_kraken_enabled():
        logger.debug("Kraken integration disabled")
        return None
    
    api_key = os.getenv("KRAKEN_API_KEY")
    api_secret = os.getenv("KRAKEN_API_SECRET")
    
    if not api_key or not api_secret:
        raise ValueError(
            "KRAKEN_ENABLED=true but KRAKEN_API_KEY/KRAKEN_API_SECRET not set"
        )
    
    logger.info("Creating Kraken client (key=%s...)", api_key[:8])
    
    return KrakenFuturesClient(
        api_key=api_key,
        api_secret=api_secret,
        auto_reconnect=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def send_kraken_order(
    symbol: str,
    side: str,
    order_type: str,
    quantity: float,
    price: Optional[float] = None,
    validate_only: bool = False,
) -> Optional[KrakenOrderResponse]:
    """
    Send order to Kraken Futures.
    
    This is a convenience function that handles client lifecycle.
    For multiple orders, use client directly with context manager.
    
    Args:
        symbol: Trading pair (e.g., "BTC/USD")
        side: "buy" or "sell"
        order_type: "market", "limit", etc.
        quantity: Order quantity
        price: Limit price (for limit orders)
        validate_only: Only validate, don't execute
        
    Returns:
        KrakenOrderResponse or None if Kraken disabled
        
    Example:
        result = await send_kraken_order(
            symbol="ETH/USD",
            side="buy",
            order_type="limit",
            quantity=0.1,
            price=2500.0,
        )
    """
    client = get_kraken_client()
    if not client:
        return None
    
    async with client:
        return await client.send_order_kraken(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            validate_only=validate_only,
        )


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED TRADING FUNCTIONS (Recommended)
# ══════════════════════════════════════════════════════════════════════════════

def get_broker_config() -> Dict[str, Any]:
    """Get broker configuration from environment."""
    return {
        "BITGET_API_KEY": os.getenv("BITGET_API_KEY"),
        "BITGET_API_SECRET": os.getenv("BITGET_API_SECRET"),
        "BITGET_PASSPHRASE": os.getenv("BITGET_PASSPHRASE"),
        "KRAKEN_API_KEY": os.getenv("KRAKEN_API_KEY"),
        "KRAKEN_API_SECRET": os.getenv("KRAKEN_API_SECRET"),
        "KRAKEN_ENABLED": is_kraken_enabled(),
        "DEFAULT_BROKER": os.getenv("DEFAULT_BROKER", "bitget"),
        "BALANCE_CHECK_ENABLED": True,
    }


async def send_trade(
    symbol: str,
    side: str,
    qty: float,
    price: Optional[float] = None,
    broker: str = "bitget",
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    check_balance: bool = True,
) -> OrderResult:
    """
    Send a trade to specified broker with balance check.
    
    This is the recommended unified function for placing trades.
    Works with both Bitget and Kraken.
    
    Args:
        symbol: Trading pair (e.g., "BTCUSDT" for Bitget, "BTC/USD" for Kraken)
        side: "buy" or "sell"
        qty: Order quantity
        price: Limit price (None for market order)
        broker: "bitget" or "kraken"
        stop_loss: Stop-loss price (optional)
        take_profit: Take-profit price (optional)
        check_balance: Verify balance before sending (default True)
        
    Returns:
        OrderResult with execution details
        
    Example:
        # Market buy on Kraken
        result = await send_trade(
            symbol="BTC/USD",
            side="buy",
            qty=0.001,
            broker="kraken",
        )
        
        # Limit sell on Bitget with TP/SL
        result = await send_trade(
            symbol="BTCUSDT",
            side="sell",
            qty=0.001,
            price=55000.0,
            broker="bitget",
            stop_loss=56000.0,
            take_profit=50000.0,
        )
    """
    config = get_broker_config()
    
    async with MultiBrokerRouter(config) as router:
        return await router.send_order(
            symbol=symbol,
            qty=qty,
            side=side,
            broker=broker,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            check_balance=check_balance,
        )


async def execute_trading_signal(
    signal: Dict[str, Any],
    broker: str = "bitget",
    validate_balance: bool = True,
) -> OrderResult:
    """
    Execute a trading signal on specified broker.
    
    Integrates with the existing signal format from the bot.
    Checks balance before execution if validate_balance=True.
    
    Args:
        signal: Signal dict with keys:
            - symbol: str
            - direction: "long" or "short"
            - entry: float
            - stop_loss: float
            - take_profit: float
            - qty: float
        broker: Target broker ("bitget" or "kraken")
        validate_balance: Check balance before execution
        
    Returns:
        OrderResult with execution details
        
    Example:
        signal = {
            "symbol": "BTC/USD",
            "direction": "long",
            "entry": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "qty": 0.001,
        }
        result = await execute_trading_signal(signal, broker="kraken")
    """
    config = get_broker_config()
    
    async with MultiBrokerRouter(config) as router:
        return await router.execute_signal(
            signal=signal,
            broker=broker,
            validate_balance=validate_balance,
        )


async def check_broker_balance(broker: str = "bitget") -> Dict[str, float]:
    """
    Check available balance on a broker.
    
    Args:
        broker: "bitget" or "kraken"
        
    Returns:
        Dict with balance info: {"available": float, "total": float, "currency": str}
        
    Example:
        balance = await check_broker_balance("kraken")
        print(f"Available: ${balance['available']:.2f}")
    """
    config = get_broker_config()
    
    async with MultiBrokerRouter(config) as router:
        balance_info = await router.get_balance(broker)
        return {
            "available": balance_info.available,
            "total": balance_info.total,
            "currency": balance_info.currency,
            "margin_used": balance_info.margin_used,
            "unrealized_pnl": balance_info.unrealized_pnl,
        }


# ══════════════════════════════════════════════════════════════════════════════
# LEGACY MULTI-BROKER PATTERN (Kept for backwards compatibility)
# ══════════════════════════════════════════════════════════════════════════════

class MultiBrokerOrderManager:
    """
    Example: Managing orders across multiple brokers.
    
    This class demonstrates how to integrate Kraken alongside
    the existing Bitget integration.
    """
    
    def __init__(self):
        self._kraken_client: Optional[KrakenFuturesClient] = None
        # Note: BitgetV2MixClient would be initialized here as well
    
    async def initialize(self) -> None:
        """Initialize all broker connections."""
        # Initialize Kraken if enabled
        if is_kraken_enabled():
            self._kraken_client = get_kraken_client()
            if self._kraken_client:
                await self._kraken_client.connect()
                logger.info("Kraken connected")
    
    async def shutdown(self) -> None:
        """Shutdown all broker connections."""
        if self._kraken_client:
            await self._kraken_client.disconnect()
            logger.info("Kraken disconnected")
    
    async def send_order(
        self,
        broker: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
    ) -> dict:
        """
        Route order to appropriate broker.
        
        Args:
            broker: "kraken" or "bitget"
            symbol: Trading pair
            side: "buy" or "sell"
            order_type: Order type
            quantity: Order quantity
            price: Optional price
            
        Returns:
            Order response dict
        """
        if broker == "kraken":
            if not self._kraken_client:
                raise ValueError("Kraken not initialized")
            
            result = await self._kraken_client.send_order_kraken(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
            )
            return {
                "broker": "kraken",
                "success": result.success,
                "order_id": result.order_id,
                "error": result.error,
            }
        
        elif broker == "bitget":
            # This would use BitgetV2MixClient
            raise NotImplementedError("Use existing Bitget client")
        
        else:
            raise ValueError(f"Unknown broker: {broker}")
    
    async def __aenter__(self):
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.shutdown()


# ══════════════════════════════════════════════════════════════════════════════
# EXAMPLE MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    """Example usage demonstrating multi-broker trading."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    print("=" * 60)
    print("MULTI-BROKER TRADING EXAMPLE")
    print("=" * 60)
    
    config = get_broker_config()
    
    # Show configuration status
    print("\n📋 Configuration Status:")
    print(f"  Kraken enabled: {config['KRAKEN_ENABLED']}")
    print(f"  Kraken key set: {bool(config['KRAKEN_API_KEY'])}")
    print(f"  Bitget key set: {bool(config['BITGET_API_KEY'])}")
    print(f"  Default broker: {config['DEFAULT_BROKER']}")
    
    if not config['KRAKEN_ENABLED'] and not config['BITGET_API_KEY']:
        print("\n❌ No brokers configured!")
        print("\nTo enable brokers, add to your .env file:")
        print("  # Kraken")
        print("  KRAKEN_ENABLED=true")
        print("  KRAKEN_API_KEY=your_api_key")
        print("  KRAKEN_API_SECRET=your_api_secret_base64")
        print("  # Bitget")
        print("  BITGET_API_KEY=your_api_key")
        print("  BITGET_API_SECRET=your_api_secret")
        print("  BITGET_PASSPHRASE=your_passphrase")
        return
    
    # Example 1: Check balance on available brokers
    print("\n" + "-" * 60)
    print("Example 1: Check Broker Balances")
    print("-" * 60)
    
    async with MultiBrokerRouter(config) as router:
        for broker in router.get_active_brokers():
            try:
                balance = await router.get_balance(broker)
                print(f"\n  {broker.value.upper()} Balance:")
                print(f"    Available: ${balance.available:,.2f}")
                print(f"    Total:     ${balance.total:,.2f}")
                print(f"    Margin:    ${balance.margin_used:,.2f}")
            except Exception as e:
                print(f"\n  {broker.value.upper()} Balance: Error - {e}")
    
    # Example 2: Unified send_trade function
    print("\n" + "-" * 60)
    print("Example 2: Unified Trade Function (Kraken)")
    print("-" * 60)
    
    if is_kraken_enabled():
        print("\n  Sending limit buy order (BTC/USD @ $50,000)...")
        print("  ⚠️  This is a VALIDATION ONLY example")
        
        try:
            # Note: In production, this would place a real order
            # Here we demonstrate the API without actually trading
            result = await send_kraken_order(
                symbol="BTC/USD",
                side="buy",
                order_type="limit",
                quantity=0.0001,
                price=50000.0,
                validate_only=True,  # SAFE: Only validates
            )
            if result:
                print(f"  ✅ Validation: success={result.success}")
                if result.error:
                    print(f"  ⚠️  Note: {result.error}")
        except Exception as e:
            print(f"  ❌ Error: {e}")
    else:
        print("  ⏭️  Kraken not enabled, skipping...")
    
    # Example 3: Signal-based trading
    print("\n" + "-" * 60)
    print("Example 3: Signal-Based Trading")
    print("-" * 60)
    
    signal = {
        "symbol": "BTC/USD" if is_kraken_enabled() else "BTCUSDT",
        "direction": "long",
        "entry": 50000.0,
        "stop_loss": 49000.0,
        "take_profit": 52000.0,
        "qty": 0.0001,
        "rr_ratio": 2.0,
    }
    
    print(f"\n  Signal received:")
    print(f"    Symbol:      {signal['symbol']}")
    print(f"    Direction:   {signal['direction'].upper()}")
    print(f"    Entry:       ${signal['entry']:,}")
    print(f"    Stop Loss:   ${signal['stop_loss']:,}")
    print(f"    Take Profit: ${signal['take_profit']:,}")
    print(f"    Quantity:    {signal['qty']}")
    print(f"    R:R Ratio:   {signal['rr_ratio']}")
    
    broker = "kraken" if is_kraken_enabled() else "bitget"
    print(f"\n  Would route to: {broker.upper()}")
    print("  (Not executed - example only)")
    
    # Example 4: Multi-broker routing
    print("\n" + "-" * 60)
    print("Example 4: Multi-Broker Router")
    print("-" * 60)
    
    async with MultiBrokerRouter(config) as router:
        active = router.get_active_brokers()
        print(f"\n  Active brokers: {[b.value for b in active]}")
        print(f"  Bitget available: {router.bitget_available}")
        print(f"  Kraken available: {router.kraken_available}")
        
        if router.kraken_available:
            print("\n  Kraken WebSocket: Connected ✅")
        if router.bitget_available:
            print("  Bitget REST API:  Ready ✅")
    
    print("\n" + "=" * 60)
    print("✅ Examples Complete")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
