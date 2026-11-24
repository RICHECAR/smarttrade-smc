"""
Multi-Broker Market Router — Unified Market Data Access
═══════════════════════════════════════════════════════

Routes market data requests to the appropriate broker client,
providing a unified interface for:
  - Candle data (OHLCV)
  - Ticker data
  - Balance information

Features:
  - Automatic broker detection and routing
  - Symbol normalization for each broker
  - Consistent output format across brokers
  - Fallback support (if one broker fails, try another)
  - Comprehensive logging with broker identification

Usage:
    from backend.market_data.multi_broker_market import MultiBrokerMarket, BrokerType
    
    market = MultiBrokerMarket()
    await market.initialize()
    
    # Fetch from specific broker
    candles = await market.get_candles("BTCUSDT", "4h", 100, broker=BrokerType.KRAKEN)
    
    # Fetch from default broker
    ticker = await market.get_ticker("ETHUSDT")
    
    # Get balance
    balance = await market.get_balance(BrokerType.KRAKEN)
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from smc_futures.engine.multi_broker_engine import Candle

logger = logging.getLogger("smarttrade.multi_broker_market")


class BrokerType(str, Enum):
    """Supported broker types."""
    BITGET = "bitget"
    KRAKEN = "kraken"


@dataclass
class BalanceInfo:
    """Unified balance information."""
    broker: BrokerType
    available: float
    total: float
    currency: str = "USD"
    margin_used: float = 0.0
    unrealized_pnl: float = 0.0
    
    @property
    def usable(self) -> float:
        """Alias for available balance."""
        return self.available


class MultiBrokerMarket:
    """
    Unified market data router across multiple brokers.
    
    Provides a single interface for fetching market data from
    Bitget and Kraken, with automatic symbol translation and
    format normalization.
    """
    
    def __init__(
        self,
        default_broker: BrokerType = BrokerType.BITGET,
        enable_fallback: bool = False,
    ):
        """
        Initialize multi-broker market router.
        
        Args:
            default_broker: Default broker for requests without explicit broker
            enable_fallback: If True, try other broker on failure
        """
        self.default_broker = default_broker
        self.enable_fallback = enable_fallback
        
        # Clients (initialized lazily)
        self._bitget_client = None
        self._kraken_client = None
        self._kraken_futures_client = None
        
        # Status tracking
        self._initialized = False
        self._broker_status: Dict[BrokerType, bool] = {
            BrokerType.BITGET: False,
            BrokerType.KRAKEN: False,
        }
        
        logger.info("[MULTI_MARKET] Router created | default=%s fallback=%s",
                   default_broker.value, enable_fallback)
    
    # ──────────────────────────────────────────────────
    #  INITIALIZATION
    # ──────────────────────────────────────────────────
    
    async def initialize(self) -> None:
        """
        Initialize broker clients.
        
        Initializes clients for brokers that have credentials configured.
        """
        if self._initialized:
            return
        
        # Initialize Bitget
        await self._init_bitget()
        
        # Initialize Kraken
        await self._init_kraken()
        
        self._initialized = True
        
        active = [b.value for b, status in self._broker_status.items() if status]
        logger.info("[MULTI_MARKET] Initialized | active brokers: %s", active or "NONE")
    
    async def _init_bitget(self) -> None:
        """Initialize Bitget market client."""
        try:
            from backend.market.bitget_market import BitgetMarket
            
            self._bitget_client = BitgetMarket()
            self._broker_status[BrokerType.BITGET] = True
            logger.info("[MULTI_MARKET] Bitget client initialized")
        except Exception as e:
            logger.warning("[MULTI_MARKET] Bitget init failed: %s", e)
    
    async def _init_kraken(self) -> None:
        """Initialize Kraken clients."""
        kraken_enabled = os.getenv("KRAKEN_ENABLED", "false").lower() == "true"
        
        if not kraken_enabled:
            logger.info("[MULTI_MARKET] Kraken disabled in config")
            return
        
        # Initialize market data client (public, no auth needed)
        try:
            from backend.market.kraken_market import KrakenMarket
            
            self._kraken_client = KrakenMarket()
            self._broker_status[BrokerType.KRAKEN] = True
            logger.info("[MULTI_MARKET] Kraken market client initialized")
        except Exception as e:
            logger.warning("[MULTI_MARKET] Kraken market client init failed: %s", e)
        
        # Initialize futures client (for balance, orders) if credentials available
        api_key = os.getenv("KRAKEN_API_KEY")
        api_secret = os.getenv("KRAKEN_API_SECRET")
        
        if api_key and api_secret:
            try:
                from backend.kraken_futures_client import KrakenFuturesClient
                
                self._kraken_futures_client = KrakenFuturesClient(
                    api_key=api_key,
                    api_secret=api_secret,
                    auto_reconnect=True,
                )
                logger.info("[MULTI_MARKET] Kraken futures client initialized")
            except Exception as e:
                logger.warning("[MULTI_MARKET] Kraken futures client init failed: %s", e)
    
    async def shutdown(self) -> None:
        """Shutdown all clients."""
        if self._kraken_futures_client:
            try:
                await self._kraken_futures_client.disconnect()
            except Exception:
                pass
        
        self._initialized = False
        logger.info("[MULTI_MARKET] Shutdown complete")
    
    # ──────────────────────────────────────────────────
    #  CANDLES
    # ──────────────────────────────────────────────────
    
    async def get_candles(
        self,
        symbol: str,
        tf: str = "15m",
        limit: int = 200,
        broker: Optional[Union[str, BrokerType]] = None,
    ) -> List[Candle]:
        """
        Fetch candles from specified broker.
        
        Args:
            symbol: Trading symbol (in Bitget format: BTCUSDT)
            tf: Timeframe (1m, 5m, 15m, 1h, 4h, 1d)
            limit: Maximum candles to return
            broker: Target broker (uses default if None)
            
        Returns:
            List of Candle objects.
        """
        target = self._resolve_broker(broker)
        
        try:
            if target == BrokerType.KRAKEN:
                return await self._get_candles_kraken(symbol, tf, limit)
            else:
                return await self._get_candles_bitget(symbol, tf, limit)
        except Exception as e:
            logger.error("[MULTI_MARKET] get_candles failed | broker=%s symbol=%s | %s",
                        target.value, symbol, e)
            
            if self.enable_fallback:
                fallback = self._get_fallback_broker(target)
                if fallback and self._broker_status.get(fallback):
                    logger.info("[MULTI_MARKET] Trying fallback broker: %s", fallback.value)
                    return await self.get_candles(symbol, tf, limit, broker=fallback)
            
            raise
    
    async def _get_candles_bitget(
        self,
        symbol: str,
        tf: str,
        limit: int,
    ) -> List[Candle]:
        """Fetch candles from Bitget."""
        if not self._bitget_client:
            raise RuntimeError("Bitget client not initialized")
        
        candles = await self._bitget_client.get_candles(
            symbol=symbol,
            tf=tf,
            limit=limit,
        )
        
        logger.debug("[MULTI_MARKET] Bitget candles | %s %s | %d candles",
                    symbol, tf, len(candles))
        
        return candles
    
    async def _get_candles_kraken(
        self,
        symbol: str,
        tf: str,
        limit: int,
    ) -> List[Candle]:
        """Fetch candles from Kraken."""
        if not self._kraken_client:
            raise RuntimeError("Kraken client not initialized")
        
        candles = await self._kraken_client.get_candles(
            symbol=symbol,
            tf=tf,
            limit=limit,
        )
        
        logger.debug("[MULTI_MARKET] Kraken candles | %s %s | %d candles",
                    symbol, tf, len(candles))
        
        return candles
    
    async def get_candles_with_end_time(
        self,
        symbol: str,
        tf: str = "4h",
        limit: int = 200,
        end_time: Optional[int] = None,
        broker: Optional[Union[str, BrokerType]] = None,
    ) -> List[Candle]:
        """
        Fetch candles with end time for pagination.
        
        Args:
            symbol: Trading symbol
            tf: Timeframe
            limit: Maximum candles
            end_time: Unix timestamp in milliseconds
            broker: Target broker
            
        Returns:
            List of Candle objects
        """
        target = self._resolve_broker(broker)
        
        if target == BrokerType.KRAKEN:
            if not self._kraken_client:
                raise RuntimeError("Kraken client not initialized")
            return await self._kraken_client.get_candles_with_end_time(
                symbol=symbol, tf=tf, limit=limit, end_time=end_time
            )
        else:
            if not self._bitget_client:
                raise RuntimeError("Bitget client not initialized")
            return await self._bitget_client.get_candles_with_end_time(
                symbol=symbol, tf=tf, limit=limit, end_time=end_time
            )
    
    # ──────────────────────────────────────────────────
    #  TICKER
    # ──────────────────────────────────────────────────
    
    async def get_ticker(
        self,
        symbol: str,
        broker: Optional[Union[str, BrokerType]] = None,
    ) -> Dict[str, Any]:
        """
        Fetch ticker from specified broker.
        
        Args:
            symbol: Trading symbol (in Bitget format: BTCUSDT)
            broker: Target broker (uses default if None)
            
        Returns:
            Ticker dict with unified format:
            {
                "symbol": "BTCUSDT",
                "last_price": 50000.0,
                "mark_price": 50000.0,
                "bid1": 49990.0,
                "ask1": 50010.0,
                "volume_24h": 1234.5,
                "source": "bitget" | "kraken",
                ...
            }
        """
        target = self._resolve_broker(broker)
        
        try:
            if target == BrokerType.KRAKEN:
                return await self._get_ticker_kraken(symbol)
            else:
                return await self._get_ticker_bitget(symbol)
        except Exception as e:
            logger.error("[MULTI_MARKET] get_ticker failed | broker=%s symbol=%s | %s",
                        target.value, symbol, e)
            
            if self.enable_fallback:
                fallback = self._get_fallback_broker(target)
                if fallback and self._broker_status.get(fallback):
                    logger.info("[MULTI_MARKET] Trying fallback broker: %s", fallback.value)
                    return await self.get_ticker(symbol, broker=fallback)
            
            raise
    
    async def _get_ticker_bitget(self, symbol: str) -> Dict[str, Any]:
        """Fetch ticker from Bitget."""
        if not self._bitget_client:
            raise RuntimeError("Bitget client not initialized")
        
        ticker = await self._bitget_client.get_ticker(symbol)
        ticker["source"] = "bitget"
        
        logger.debug("[MULTI_MARKET] Bitget ticker | %s | last=%.2f",
                    symbol, ticker.get("last_price", 0))
        
        return ticker
    
    async def _get_ticker_kraken(self, symbol: str) -> Dict[str, Any]:
        """Fetch ticker from Kraken."""
        if not self._kraken_client:
            raise RuntimeError("Kraken client not initialized")
        
        ticker = await self._kraken_client.get_ticker(symbol)
        ticker["source"] = "kraken"
        
        logger.debug("[MULTI_MARKET] Kraken ticker | %s | last=%.2f",
                    symbol, ticker.get("last_price", 0))
        
        return ticker
    
    # ──────────────────────────────────────────────────
    #  BALANCE
    # ──────────────────────────────────────────────────
    
    async def get_balance(
        self,
        broker: Optional[Union[str, BrokerType]] = None,
    ) -> BalanceInfo:
        """
        Get account balance from specified broker.
        
        Requires authenticated client for the broker.
        
        Args:
            broker: Target broker (uses default if None)
            
        Returns:
            BalanceInfo with available and total balance
        """
        target = self._resolve_broker(broker)
        
        if target == BrokerType.KRAKEN:
            return await self._get_balance_kraken()
        else:
            return await self._get_balance_bitget()
    
    async def _get_balance_bitget(self) -> BalanceInfo:
        """Get Bitget balance."""
        if not self._bitget_client:
            raise RuntimeError("Bitget client not initialized")
        
        # Bitget client needs API credentials for balance
        # This would use the authenticated executor
        raise NotImplementedError("Use MultiBrokerRouter for authenticated operations")
    
    async def _get_balance_kraken(self) -> BalanceInfo:
        """Get Kraken balance."""
        if not self._kraken_futures_client:
            raise RuntimeError("Kraken futures client not initialized or not authenticated")
        
        trade_balance = await self._kraken_futures_client.get_trade_balance("USD")
        
        available = trade_balance.get("mf", 0.0) or trade_balance.get("tb", 0.0)
        total = trade_balance.get("e", available)
        margin_used = trade_balance.get("m", 0.0)
        unrealized = trade_balance.get("n", 0.0)
        
        balance = BalanceInfo(
            broker=BrokerType.KRAKEN,
            available=available,
            total=total,
            currency="USD",
            margin_used=margin_used,
            unrealized_pnl=unrealized,
        )
        
        logger.info("[MULTI_MARKET] Kraken balance | available=%.2f total=%.2f",
                   balance.available, balance.total)
        
        return balance
    
    # ──────────────────────────────────────────────────
    #  HELPERS
    # ──────────────────────────────────────────────────
    
    def _resolve_broker(self, broker: Optional[Union[str, BrokerType]]) -> BrokerType:
        """Resolve broker to enum."""
        if broker is None:
            return self.default_broker
        if isinstance(broker, str):
            return BrokerType(broker.lower())
        return broker
    
    def _get_fallback_broker(self, failed: BrokerType) -> Optional[BrokerType]:
        """Get fallback broker."""
        if failed == BrokerType.BITGET:
            return BrokerType.KRAKEN
        elif failed == BrokerType.KRAKEN:
            return BrokerType.BITGET
        return None
    
    def is_broker_available(self, broker: Union[str, BrokerType]) -> bool:
        """Check if broker is available."""
        target = self._resolve_broker(broker)
        return self._broker_status.get(target, False)
    
    @property
    def available_brokers(self) -> List[BrokerType]:
        """List of available brokers."""
        return [b for b, status in self._broker_status.items() if status]


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_default_router: Optional[MultiBrokerMarket] = None


async def get_multi_broker_market() -> MultiBrokerMarket:
    """Get initialized multi-broker market router."""
    global _default_router
    if _default_router is None:
        _default_router = MultiBrokerMarket()
        await _default_router.initialize()
    return _default_router


if __name__ == "__main__":
    async def main():
        print("="*60)
        print("Multi-Broker Market Router Test")
        print("="*60)
        
        market = MultiBrokerMarket(enable_fallback=True)
        await market.initialize()
        
        print(f"\nAvailable brokers: {[b.value for b in market.available_brokers]}")
        
        # Test candles
        if market.is_broker_available(BrokerType.KRAKEN):
            print("\n--- Kraken Candles ---")
            candles = await market.get_candles("BTCUSDT", "1h", 5, broker=BrokerType.KRAKEN)
            print(f"Fetched {len(candles)} candles")
            if candles:
                print(f"Latest: {candles[-1]}")
        
        if market.is_broker_available(BrokerType.BITGET):
            print("\n--- Bitget Candles ---")
            candles = await market.get_candles("BTCUSDT", "1h", 5, broker=BrokerType.BITGET)
            print(f"Fetched {len(candles)} candles")
            if candles:
                print(f"Latest: {candles[-1]}")
        
        await market.shutdown()
        print("\n✅ Test complete")
    
    asyncio.run(main())
