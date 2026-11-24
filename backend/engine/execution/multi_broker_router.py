"""
Multi-Broker Order Router — Unified order execution across Bitget and Kraken.

Features:
  - Unified send_order() interface for both brokers
  - Pre-execution balance checks per broker
  - Signal-based routing with risk validation
  - Compatible with existing OrderManager safeguards
  - Error handling with broker-specific recovery

Usage:
    router = MultiBrokerRouter(config)
    await router.initialize()
    
    # Route order based on signal + broker config
    result = await router.send_order(
        symbol="BTCUSDT",
        qty=0.001,
        price=50000.0,
        side="buy",
        broker="kraken"
    )

Author: Trading Bot Framework
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# TYPES & ENUMS
# ══════════════════════════════════════════════════════════════════════════════

class BrokerType(str, Enum):
    """Supported broker types."""
    BITGET = "bitget"
    KRAKEN = "kraken"


class OrderSide(str, Enum):
    """Order side."""
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """Order type."""
    MARKET = "market"
    LIMIT = "limit"


@dataclass
class OrderRequest:
    """Unified order request."""
    symbol: str
    side: OrderSide
    quantity: float
    price: Optional[float] = None
    order_type: OrderType = OrderType.MARKET
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    leverage: int = 1
    reduce_only: bool = False
    client_order_id: Optional[str] = None


@dataclass
class OrderResult:
    """Unified order result."""
    success: bool
    broker: BrokerType
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    filled_qty: Optional[float] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    raw_response: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def executed(self) -> bool:
        """Alias for success."""
        return self.success


@dataclass
class BalanceInfo:
    """Broker balance information."""
    broker: BrokerType
    available: float
    total: float
    currency: str = "USD"
    margin_used: float = 0.0
    unrealized_pnl: float = 0.0


class BalanceError(Exception):
    """Balance check error."""
    pass


class InsufficientBalanceError(BalanceError):
    """Insufficient balance to execute trade."""
    def __init__(self, broker: str, required: float, available: float):
        self.broker = broker
        self.required = required
        self.available = available
        super().__init__(
            f"Insufficient balance on {broker}: required ${required:.2f}, available ${available:.2f}"
        )


class BrokerNotAvailableError(Exception):
    """Broker not initialized or unavailable."""
    pass


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-BROKER ORDER ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class MultiBrokerRouter:
    """
    Routes orders to Bitget or Kraken based on configuration.
    
    Features:
      - Unified order interface
      - Pre-execution balance validation
      - Signal-based routing
      - Graceful fallback on broker errors
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize router with config.
        
        Args:
            config: Dict with broker credentials and settings:
                - BITGET_API_KEY, BITGET_API_SECRET, BITGET_PASSPHRASE
                - KRAKEN_API_KEY, KRAKEN_API_SECRET, KRAKEN_ENABLED
                - DEFAULT_BROKER: "bitget" or "kraken"
                - BALANCE_CHECK_ENABLED: True/False
        """
        self.config = config
        self._bitget_executor = None
        self._kraken_client = None
        self._initialized = False
        
        # Default broker for routing
        self.default_broker = BrokerType(config.get("DEFAULT_BROKER", "kraken").lower())
        
        # Enable balance checks before order
        self.balance_check_enabled = config.get("BALANCE_CHECK_ENABLED", True)
        
        # Track broker status
        self._broker_status: Dict[BrokerType, bool] = {
            BrokerType.BITGET: False,
            BrokerType.KRAKEN: False,
        }
        
        logger.info("[ROUTER] Initialized | default_broker=%s balance_check=%s",
                   self.default_broker.value, self.balance_check_enabled)
    
    # ──────────────────────────────────────────────────
    #  INITIALIZATION
    # ──────────────────────────────────────────────────
    
    async def initialize(self) -> None:
        """
        Initialize broker connections.
        
        Initializes Bitget and Kraken clients based on available credentials.
        """
        if self._initialized:
            logger.warning("[ROUTER] Already initialized")
            return
        
        # Initialize Bitget
        await self._init_bitget()
        
        # Initialize Kraken (if enabled)
        await self._init_kraken()
        
        self._initialized = True
        
        active = [b.value for b, status in self._broker_status.items() if status]
        logger.info("[ROUTER] Brokers active: %s", active or "NONE")
    
    async def _init_bitget(self) -> None:
        """Initialize Bitget executor."""
        api_key = self.config.get("BITGET_API_KEY") or os.getenv("BITGET_API_KEY")
        api_secret = self.config.get("BITGET_API_SECRET") or os.getenv("BITGET_API_SECRET")
        passphrase = self.config.get("BITGET_PASSPHRASE") or os.getenv("BITGET_PASSPHRASE")
        
        if not all([api_key, api_secret, passphrase]):
            logger.warning("[ROUTER] Bitget credentials not configured")
            return
        
        try:
            from backend.engine.execution.bitget_v2_executor import BitgetV2Executor
            
            self._bitget_executor = BitgetV2Executor(
                api_key=api_key,
                api_secret=api_secret,
                passphrase=passphrase,
            )
            self._broker_status[BrokerType.BITGET] = True
            logger.info("[ROUTER] Bitget executor initialized")
        except Exception as e:
            logger.error("[ROUTER] Bitget init failed: %s", e)
    
    async def _init_kraken(self) -> None:
        """Initialize Kraken client."""
        kraken_enabled = self.config.get("KRAKEN_ENABLED", False)
        if isinstance(kraken_enabled, str):
            kraken_enabled = kraken_enabled.lower() == "true"
        
        if not kraken_enabled:
            logger.info("[ROUTER] Kraken disabled in config")
            return
        
        api_key = self.config.get("KRAKEN_API_KEY") or os.getenv("KRAKEN_API_KEY")
        api_secret = self.config.get("KRAKEN_API_SECRET") or os.getenv("KRAKEN_API_SECRET")
        
        if not all([api_key, api_secret]):
            logger.warning("[ROUTER] Kraken credentials not configured")
            return
        
        try:
            from backend.kraken_futures_client import KrakenFuturesClient
            
            self._kraken_client = KrakenFuturesClient(
                api_key=api_key,
                api_secret=api_secret,
                auto_reconnect=True,
            )
            await self._kraken_client.connect()
            self._broker_status[BrokerType.KRAKEN] = True
            logger.info("[ROUTER] Kraken client initialized and connected")
        except Exception as e:
            logger.error("[ROUTER] Kraken init failed: %s", e)
    
    async def shutdown(self) -> None:
        """Shutdown all broker connections."""
        if self._kraken_client:
            try:
                await self._kraken_client.disconnect()
                logger.info("[ROUTER] Kraken disconnected")
            except Exception as e:
                logger.error("[ROUTER] Kraken disconnect error: %s", e)
        
        self._initialized = False
        logger.info("[ROUTER] Shutdown complete")
    
    # ──────────────────────────────────────────────────
    #  BALANCE CHECKS
    # ──────────────────────────────────────────────────
    
    async def get_balance(self, broker: Union[str, BrokerType]) -> BalanceInfo:
        """
        Get balance for specified broker.
        
        Args:
            broker: "bitget" or "kraken"
            
        Returns:
            BalanceInfo with available balance
            
        Raises:
            BrokerNotAvailableError: If broker not initialized
            BalanceError: On API error
        """
        if isinstance(broker, str):
            broker = BrokerType(broker.lower())
        
        if broker == BrokerType.BITGET:
            return await self._get_bitget_balance()
        elif broker == BrokerType.KRAKEN:
            return await self._get_kraken_balance()
        else:
            raise ValueError(f"Unknown broker: {broker}")
    
    async def _get_bitget_balance(self) -> BalanceInfo:
        """Get Bitget account balance."""
        if not self._bitget_executor:
            raise BrokerNotAvailableError("Bitget not initialized")
        
        try:
            # Use Bitget executor to get account info
            account_info = await self._bitget_executor.get_account_info()
            
            available = float(account_info.get("available", 0))
            total = float(account_info.get("usdtEquity", available))
            margin_used = float(account_info.get("frozenAssets", 0))
            unrealized = float(account_info.get("unrealizedPL", 0))
            
            return BalanceInfo(
                broker=BrokerType.BITGET,
                available=available,
                total=total,
                currency="USDT",
                margin_used=margin_used,
                unrealized_pnl=unrealized,
            )
        except Exception as e:
            logger.error("[ROUTER] Bitget balance error: %s", e)
            raise BalanceError(f"Failed to get Bitget balance: {e}")
    
    async def _get_kraken_balance(self) -> BalanceInfo:
        """Get Kraken account balance."""
        if not self._kraken_client:
            raise BrokerNotAvailableError("Kraken not initialized")
        
        try:
            trade_balance = await self._kraken_client.get_trade_balance("USD")
            
            # mf = free margin, tb = trade balance, e = equity
            available = trade_balance.get("mf", 0.0) or trade_balance.get("tb", 0.0)
            total = trade_balance.get("e", available)  # equity
            margin_used = trade_balance.get("m", 0.0)
            unrealized = trade_balance.get("n", 0.0)
            
            return BalanceInfo(
                broker=BrokerType.KRAKEN,
                available=available,
                total=total,
                currency="USD",
                margin_used=margin_used,
                unrealized_pnl=unrealized,
            )
        except Exception as e:
            logger.error("[ROUTER] Kraken balance error: %s", e)
            raise BalanceError(f"Failed to get Kraken balance: {e}")
    
    async def check_balance_for_order(
        self,
        broker: Union[str, BrokerType],
        required_margin: float,
    ) -> Tuple[bool, BalanceInfo]:
        """
        Check if balance is sufficient for order.
        
        Args:
            broker: Target broker
            required_margin: Required margin in USD
            
        Returns:
            Tuple of (is_sufficient, balance_info)
        """
        balance = await self.get_balance(broker)
        is_sufficient = balance.available >= required_margin
        
        if not is_sufficient:
            logger.warning("[ROUTER] INSUFFICIENT_BALANCE broker=%s required=%.2f available=%.2f",
                          broker, required_margin, balance.available)
        
        return is_sufficient, balance
    
    # ──────────────────────────────────────────────────
    #  UNIFIED ORDER INTERFACE
    # ──────────────────────────────────────────────────
    
    async def send_order(
        self,
        symbol: str,
        qty: float,
        side: Union[str, OrderSide],
        broker: Union[str, BrokerType] = None,
        price: Optional[float] = None,
        order_type: Union[str, OrderType] = OrderType.MARKET,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        leverage: int = 1,
        reduce_only: bool = False,
        check_balance: bool = True,
        required_margin: Optional[float] = None,
    ) -> OrderResult:
        """
        Send order to specified broker.
        
        This is the unified entry point for order execution.
        
        Args:
            symbol: Trading pair (e.g., "BTCUSDT" for Bitget, "BTC/USD" for Kraken)
            qty: Order quantity
            side: "buy" or "sell"
            broker: Target broker ("bitget" or "kraken"), defaults to config default
            price: Limit price (None for market orders)
            order_type: "market" or "limit"
            stop_loss: Stop-loss price (optional)
            take_profit: Take-profit price (optional)
            leverage: Leverage to use (default 1)
            reduce_only: Only reduce existing position
            check_balance: Check balance before sending (default True)
            required_margin: Required margin for balance check (auto-calculated if None)
            
        Returns:
            OrderResult with execution details
            
        Raises:
            InsufficientBalanceError: If balance check fails
            BrokerNotAvailableError: If broker not initialized
            
        Example:
            # Market buy on Kraken
            result = await router.send_order(
                symbol="BTC/USD",
                qty=0.001,
                side="buy",
                broker="kraken",
            )
            
            # Limit sell on Bitget
            result = await router.send_order(
                symbol="BTCUSDT",
                qty=0.001,
                side="sell",
                broker="bitget",
                price=55000.0,
                order_type="limit",
            )
        """
        # Normalize inputs
        if isinstance(side, str):
            side = OrderSide(side.lower())
        if isinstance(order_type, str):
            order_type = OrderType(order_type.lower())
        if broker is None:
            broker = self.default_broker
        elif isinstance(broker, str):
            broker = BrokerType(broker.lower())
        
        # Check broker availability
        if not self._broker_status.get(broker, False):
            return OrderResult(
                success=False,
                broker=broker,
                error=f"Broker {broker.value} not available",
                error_code="BROKER_NOT_AVAILABLE",
            )
        
        # Balance check
        if check_balance and self.balance_check_enabled:
            # Calculate required margin if not provided
            if required_margin is None:
                # Rough estimate: qty * price / leverage
                est_price = price if price else self._estimate_price(symbol)
                required_margin = (qty * est_price) / max(leverage, 1)
            
            try:
                is_sufficient, balance = await self.check_balance_for_order(broker, required_margin)
                if not is_sufficient:
                    raise InsufficientBalanceError(broker.value, required_margin, balance.available)
            except BalanceError as e:
                logger.error("[ROUTER] Balance check failed: %s", e)
                return OrderResult(
                    success=False,
                    broker=broker,
                    error=str(e),
                    error_code="BALANCE_CHECK_FAILED",
                )
        
        # Route to appropriate broker
        logger.info("[ROUTER] SEND_ORDER broker=%s symbol=%s side=%s qty=%s price=%s",
                   broker.value, symbol, side.value, qty, price)
        
        try:
            if broker == BrokerType.BITGET:
                return await self._send_bitget_order(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    price=price,
                    order_type=order_type,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    leverage=leverage,
                    reduce_only=reduce_only,
                )
            elif broker == BrokerType.KRAKEN:
                return await self._send_kraken_order(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    price=price,
                    order_type=order_type,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reduce_only=reduce_only,
                )
            else:
                return OrderResult(
                    success=False,
                    broker=broker,
                    error=f"Unknown broker: {broker}",
                    error_code="UNKNOWN_BROKER",
                )
        except InsufficientBalanceError:
            raise
        except Exception as e:
            logger.error("[ROUTER] Order failed: %s", e, exc_info=True)
            return OrderResult(
                success=False,
                broker=broker,
                error=str(e),
                error_code="ORDER_FAILED",
            )
    
    def _estimate_price(self, symbol: str) -> float:
        """Estimate price for margin calculation (fallback)."""
        # Simple fallback - in production, should fetch current price
        symbol_upper = symbol.upper().replace("/", "").replace("-", "")
        if "BTC" in symbol_upper:
            return 50000.0
        elif "ETH" in symbol_upper:
            return 3000.0
        elif "SOL" in symbol_upper:
            return 100.0
        else:
            return 1.0
    
    # ──────────────────────────────────────────────────
    #  BROKER-SPECIFIC ORDER IMPLEMENTATIONS
    # ──────────────────────────────────────────────────
    
    async def _send_bitget_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        price: Optional[float],
        order_type: OrderType,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        leverage: int,
        reduce_only: bool,
    ) -> OrderResult:
        """Send order to Bitget."""
        if not self._bitget_executor:
            return OrderResult(
                success=False,
                broker=BrokerType.BITGET,
                error="Bitget executor not initialized",
                error_code="NOT_INITIALIZED",
            )
        
        try:
            # Map side to Bitget format
            bitget_side = side.value
            
            # Determine trade side (open/close)
            trade_side = "close" if reduce_only else "open"
            
            # Call Bitget executor
            if order_type == OrderType.MARKET:
                result = await self._bitget_executor.place_order_market(
                    symbol=symbol,
                    side=bitget_side,
                    size=str(qty),
                )
            else:
                result = await self._bitget_executor.place_order_limit(
                    symbol=symbol,
                    side=bitget_side,
                    size=str(qty),
                    price=str(price),
                )
            
            # Parse Bitget response
            success = result.get("code") == "00000"
            data = result.get("data", {})
            
            return OrderResult(
                success=success,
                broker=BrokerType.BITGET,
                order_id=data.get("orderId"),
                fill_price=float(data.get("fillPrice", 0)) if data.get("fillPrice") else None,
                filled_qty=float(data.get("fillQty", 0)) if data.get("fillQty") else None,
                error=result.get("msg") if not success else None,
                error_code=result.get("code") if not success else None,
                raw_response=result,
            )
            
        except Exception as e:
            logger.error("[ROUTER] Bitget order error: %s", e)
            return OrderResult(
                success=False,
                broker=BrokerType.BITGET,
                error=str(e),
                error_code="EXCEPTION",
            )
    
    async def _send_kraken_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        price: Optional[float],
        order_type: OrderType,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        reduce_only: bool,
    ) -> OrderResult:
        """
        Send order to Kraken via REST API through the executor wrapper.
        
        CRITICAL FIX: Uses KrakenFuturesExecutorWrapper.place_order() instead of
        the broken WS path (send_order_kraken uses Spot V2 protocol on Futures).
        
        Handles:
          - Symbol translation (BTCUSDT -> PF_XBTUSD)
          - OrderIntent validation (prevents reduce_only on open)
          - TP/SL attachment after entry fill
          - Emergency close if SL creation fails
        """
        if not self._kraken_client:
            return OrderResult(
                success=False,
                broker=BrokerType.KRAKEN,
                error="Kraken client not initialized",
                error_code="NOT_INITIALIZED",
            )
        
        try:
            # Use KrakenFuturesExecutorWrapper if available (preferred path)
            kraken_exec = getattr(self, '_kraken_executor', None)
            if kraken_exec is None:
                try:
                    from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
                    kraken_exec = KrakenFuturesExecutorWrapper(executor=self._kraken_client)
                    self._kraken_executor = kraken_exec  # Cache for reuse
                except ImportError:
                    pass

            if kraken_exec is not None and hasattr(kraken_exec, 'place_order'):
                # Route through the executor wrapper (REST API + OrderIntent validation)
                intent = "close" if reduce_only else "open"
                result = await kraken_exec.place_order(
                    symbol=symbol,
                    side=side.value,
                    quantity=qty,
                    order_type=order_type.value,
                    price=price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reduce_only=reduce_only,
                    entry_price=price,
                    intent=intent,
                )

                success = result.get("code") == "00000" or result.get("success") is True
                order_id = result.get("order_id") or ""
                if not order_id and isinstance(result.get("data"), dict):
                    order_id = result["data"].get("orderId", "")

                if success:
                    logger.info("[KRAKEN_ORDER] SUCCESS via executor | %s | order_id=%s",
                                symbol, order_id)
                else:
                    logger.error("[KRAKEN_ORDER] FAILED via executor | %s | error=%s",
                                symbol, result.get("msg"))

                return OrderResult(
                    success=success,
                    broker=BrokerType.KRAKEN,
                    order_id=order_id or None,
                    error=result.get("msg") if not success else None,
                    error_code=result.get("code") if not success else None,
                    raw_response=result,
                )

            # ── NO FALLBACK: WS path is BROKEN (Spot V2 protocol on Futures) ──
            # If the REST executor wrapper is not available, we MUST fail explicitly.
            # The legacy WS path (send_order_kraken) uses Spot V2 WebSocket protocol
            # which is incompatible with Kraken Futures and silently drops orders.
            logger.critical(
                "[KRAKEN_ORDER] BLOCKED — KrakenFuturesExecutorWrapper unavailable! "
                "Cannot fall back to WS (broken Spot V2 protocol). symbol=%s",
                symbol,
            )
            return OrderResult(
                success=False,
                broker=BrokerType.KRAKEN,
                error="Kraken REST executor not available — WS fallback disabled (protocol mismatch)",
                error_code="EXECUTOR_UNAVAILABLE",
            )
            
        except Exception as e:
            logger.error("[KRAKEN_ORDER] Exception: %s", e, exc_info=True)
            return OrderResult(
                success=False,
                broker=BrokerType.KRAKEN,
                error=str(e),
                error_code="EXCEPTION",
            )
    
    # ──────────────────────────────────────────────────
    #  SIGNAL-BASED ROUTING
    # ──────────────────────────────────────────────────
    
    async def execute_signal(
        self,
        signal: Dict[str, Any],
        broker: Union[str, BrokerType] = None,
        validate_balance: bool = True,
    ) -> OrderResult:
        """
        Execute a trading signal on specified broker.
        
        Integrates with existing signal format from OrderManager.
        
        Args:
            signal: Signal dict with keys:
                - symbol: str
                - direction: "long" or "short"
                - entry: float
                - stop_loss: float
                - take_profit: float
                - qty: float (optional, calculated from risk if missing)
                - rr_ratio: float (optional)
            broker: Target broker (default: config default)
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
            result = await router.execute_signal(signal, broker="kraken")
        """
        symbol = signal.get("symbol")
        direction = signal.get("direction", "").lower()
        entry = float(signal.get("entry", 0))
        stop_loss = float(signal.get("stop_loss", 0))
        take_profit = float(signal.get("take_profit", 0))
        qty = float(signal.get("qty", 0))
        
        # Validate signal
        if not all([symbol, direction, entry, stop_loss, take_profit]):
            return OrderResult(
                success=False,
                broker=broker if broker else self.default_broker,
                error="Invalid signal: missing required fields",
                error_code="INVALID_SIGNAL",
            )
        
        # Map direction to side
        if direction == "long":
            side = OrderSide.BUY
        elif direction == "short":
            side = OrderSide.SELL
        else:
            return OrderResult(
                success=False,
                broker=broker if broker else self.default_broker,
                error=f"Invalid direction: {direction}",
                error_code="INVALID_DIRECTION",
            )
        
        # Calculate required margin
        risk_per_unit = abs(entry - stop_loss)
        required_margin = qty * entry  # Simplified; adjust for leverage
        
        logger.info("[ROUTER] EXECUTE_SIGNAL symbol=%s direction=%s entry=%.4f sl=%.4f tp=%.4f qty=%s",
                   symbol, direction, entry, stop_loss, take_profit, qty)
        
        return await self.send_order(
            symbol=symbol,
            qty=qty,
            side=side,
            broker=broker,
            price=entry,  # Use limit order at entry
            order_type=OrderType.LIMIT,
            stop_loss=stop_loss,
            take_profit=take_profit,
            check_balance=validate_balance,
            required_margin=required_margin,
        )
    
    # ──────────────────────────────────────────────────
    #  CONTEXT MANAGER
    # ──────────────────────────────────────────────────
    
    async def __aenter__(self) -> "MultiBrokerRouter":
        """Async context manager entry."""
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.shutdown()
    
    # ──────────────────────────────────────────────────
    #  STATUS & UTILITIES
    # ──────────────────────────────────────────────────
    
    def is_broker_available(self, broker: Union[str, BrokerType]) -> bool:
        """Check if broker is available."""
        if isinstance(broker, str):
            broker = BrokerType(broker.lower())
        return self._broker_status.get(broker, False)
    
    def get_active_brokers(self) -> List[BrokerType]:
        """Get list of active brokers."""
        return [b for b, status in self._broker_status.items() if status]
    
    @property
    def bitget_available(self) -> bool:
        """Check if Bitget is available."""
        return self._broker_status.get(BrokerType.BITGET, False)
    
    @property
    def kraken_available(self) -> bool:
        """Check if Kraken is available."""
        return self._broker_status.get(BrokerType.KRAKEN, False)


# ══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

async def send_order(
    symbol: str,
    qty: float,
    price: Optional[float],
    side: str,
    broker: str = "bitget",
    config: Optional[Dict[str, Any]] = None,
) -> OrderResult:
    """
    Convenience function to send a single order.
    
    Creates a router, sends order, and cleans up.
    For multiple orders, use MultiBrokerRouter directly.
    
    Args:
        symbol: Trading pair
        qty: Order quantity
        price: Limit price (None for market)
        side: "buy" or "sell"
        broker: "bitget" or "kraken"
        config: Optional config dict (uses env vars if None)
        
    Returns:
        OrderResult
        
    Example:
        result = await send_order(
            symbol="BTC/USD",
            qty=0.001,
            price=50000.0,
            side="buy",
            broker="kraken",
        )
    """
    if config is None:
        config = {
            "BITGET_API_KEY": os.getenv("BITGET_API_KEY"),
            "BITGET_API_SECRET": os.getenv("BITGET_API_SECRET"),
            "BITGET_PASSPHRASE": os.getenv("BITGET_PASSPHRASE"),
            "KRAKEN_API_KEY": os.getenv("KRAKEN_API_KEY"),
            "KRAKEN_API_SECRET": os.getenv("KRAKEN_API_SECRET"),
            "KRAKEN_ENABLED": os.getenv("KRAKEN_ENABLED", "false").lower() == "true",
            "DEFAULT_BROKER": broker,
        }
    
    order_type = OrderType.MARKET if price is None else OrderType.LIMIT
    
    async with MultiBrokerRouter(config) as router:
        return await router.send_order(
            symbol=symbol,
            qty=qty,
            side=side,
            broker=broker,
            price=price,
            order_type=order_type,
        )
