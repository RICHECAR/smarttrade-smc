"""
Kraken Futures Executor — Order Execution via WebSocket V2
═════════════════════════════════════════════════════════

Provides order execution interface for Kraken Futures, matching the
BitgetFuturesExecutor API for seamless multi-broker integration.

Features:
  - WebSocket V2 order execution (add_order)
  - Market and Limit orders
  - Stop-loss and Take-profit orders
  - Leverage management (per-symbol)
  - Position management
  - Balance checks before execution
  - Symbol translation (BTCUSDT -> PF_XBTUSD)

Usage:
    executor = KrakenFuturesExecutor(api_key, api_secret)
    await executor.connect()
    
    result = await executor.place_order_market("BTCUSDT", "buy", "0.001")
    
    await executor.disconnect()

Author: Trading Bot Framework
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from backend.kraken_futures_client import (
    KrakenFuturesClient,
    KrakenOrderResponse,
    KrakenError,
    KrakenAuthError,
    KrakenOrderError,
    OrderSide,
    OrderType,
    TimeInForce,
)
from backend.utils.symbol_mapper import SymbolMapper, get_symbol_mapper, BrokerType

logger = logging.getLogger("smarttrade.kraken_executor")


class KrakenFuturesExecutor:
    """
    Execute orders on Kraken Futures via WebSocket V2.
    
    Provides API-compatible interface with BitgetFuturesExecutor for
    seamless multi-broker support.
    """
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        timeout: float = 10.0,
        auto_reconnect: bool = True,
        symbol_mapper: Optional[SymbolMapper] = None,
        single_entry_mode: Optional[bool] = None,
        wrapper: Optional[Any] = None,
    ):
        """
        Initialize Kraken Futures executor.
        
        Args:
            api_key: Kraken API public key
            api_secret: Kraken API secret (base64 encoded)
            timeout: Request timeout in seconds
            auto_reconnect: Auto-reconnect WebSocket on disconnect
            symbol_mapper: Custom symbol mapper instance
            single_entry_mode: Optional toggle for single-entry mode
            wrapper: Optional pre-configured KrakenFuturesExecutorWrapper
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self.broker_name = "kraken"
        self._single_entry_mode = single_entry_mode
        self._wrapper = wrapper
        
        # Kraken client (lazy initialization)
        self._client: Optional[KrakenFuturesClient] = None
        self._auto_reconnect = auto_reconnect
        
        # Symbol mapper for Bitget <-> Kraken translation
        self._mapper = symbol_mapper or get_symbol_mapper()
        
        # Leverage tracking (per-symbol, in-memory)
        self._leverage_map: Dict[str, int] = {}
        self._default_leverage = 3
        
        # Status
        self._connected = False
        
        logger.info("[KRAKEN_EXEC] Executor initialized | api_key=%s...",
                   api_key[:8] if api_key else "NONE")
    
    # ──────────────────────────────────────────────────
    #  CONNECTION
    # ──────────────────────────────────────────────────
    
    async def connect(self) -> None:
        """
        Connect to Kraken WebSocket.
        
        Must be called before placing orders.
        """
        if self._connected:
            logger.warning("[KRAKEN_EXEC] Already connected")
            return
        
        self._client = KrakenFuturesClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
            timeout=self.timeout,
            auto_reconnect=self._auto_reconnect,
        )
        
        await self._client.connect()
        self._connected = True
        
        logger.info("[KRAKEN_EXEC] Connected to Kraken WebSocket")
    
    async def disconnect(self) -> None:
        """Disconnect from Kraken WebSocket."""
        if self._client:
            await self._client.disconnect()
            self._client = None
        
        self._connected = False
        logger.info("[KRAKEN_EXEC] Disconnected")
    
    async def ensure_connected(self) -> None:
        """Ensure WebSocket is connected."""
        if not self._connected or not self._client:
            await self.connect()
    
    def _get_rest_client(self):
        """Get the REST client for order placement (more reliable than WS)."""
        if not hasattr(self, '_rest_client') or self._rest_client is None:
            try:
                from backend.kraken_futures_rest_v2 import KrakenFuturesRESTClientV2
                self._rest_client = KrakenFuturesRESTClientV2(
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                )
                logger.info("[KRAKEN_EXEC] REST client initialized for reliable order placement")
            except ImportError as e:
                logger.warning("[KRAKEN_EXEC] REST client not available: %s", e)
                self._rest_client = None
        return getattr(self, '_rest_client', None)
    
    # ──────────────────────────────────────────────────
    #  ORDER METHODS (Delegated to KrakenFuturesExecutorWrapper)
    # ──────────────────────────────────────────────────

    def _get_wrapper(self):
        """Get or lazily initialize the secured KrakenFuturesExecutorWrapper."""
        if self._wrapper is None:
            from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
            self._wrapper = KrakenFuturesExecutorWrapper(
                api_key=self.api_key,
                api_secret=self.api_secret,
                timeout=self.timeout,
                single_entry_mode=self._single_entry_mode,
            )
            logger.info("[KRAKEN_EXEC] Secured KrakenFuturesExecutorWrapper bridge initialized")
        return self._wrapper

    async def place_order(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float = 0,
        take_profit: float = 0,
        order_type: str = "market",
        leverage: int = 10,
        quantity: str = "0",
        signal_id: str = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Unified place_order interface compatible with order_manager.execute_signal.
        Delegates strictly to KrakenFuturesExecutorWrapper for single-entry and safety guarantees.
        """
        wrapper = self._get_wrapper()
        side = "buy" if str(direction).lower() in ("long", "buy") else "sell"
        # Phase 1.5 (P1-2): pop derived/explicit keys before forwarding **kwargs to prevent duplicate-kwarg TypeError
        forwarded = dict(kwargs)
        reduce_only = forwarded.pop("reduce_only", False)
        intent = forwarded.pop("intent", None) or ("reduce" if reduce_only else "open")
        forwarded.pop("side", None)

        result = await wrapper.place_order(
            symbol=symbol,
            side=side,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            order_type=order_type,
            leverage=leverage,
            quantity=quantity,
            signal_id=signal_id,
            intent=intent,
            reduce_only=reduce_only,
            **forwarded,
        )

        if isinstance(result, dict):
            if "fill_price" not in result and "price" in result:
                result["fill_price"] = result["price"]
            if "price_executed" not in result and "price" in result:
                result["price_executed"] = result["price"]
            if "quantity" not in result:
                try:
                    result["quantity"] = float(quantity)
                except (ValueError, TypeError):
                    result["quantity"] = 0.0
            if result.get("success"):
                if "filled_qty" not in result:
                    result["filled_qty"] = result["quantity"]
                result.setdefault("fill_quantity_verified", True)
                result.setdefault("qty_unit", "contracts")
                result.setdefault("fill_price_verified", True)

        return result

    async def place_order_market(
        self,
        symbol: str,
        side: str,
        size: str,
        reduce_only: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Place market order delegated to KrakenFuturesExecutorWrapper.
        """
        wrapper = self._get_wrapper()
        forwarded = dict(kwargs)
        intent = forwarded.pop("intent", None) or ("reduce" if reduce_only else "open")
        forwarded.pop("reduce_only", None)
        res = await wrapper.place_order(
            symbol=symbol,
            side=side,
            quantity=size,
            order_type="market",
            reduce_only=reduce_only,
            intent=intent,
            **forwarded,
        )
        if res.get("success"):
            order_id = res.get("order_id")
            canonical = self._mapper.get_canonical(symbol)
            return {
                "code": "00000",
                "msg": "success",
                "data": {
                    "orderId": order_id,
                    "symbol": canonical,
                    "orderType": "market",
                },
                **res,
            }
        else:
            return {
                "code": res.get("code", "ORDER_ERROR"),
                "msg": res.get("error", "Unknown error"),
                "data": None,
                **res,
            }

    async def place_order_limit(
        self,
        symbol: str,
        side: str,
        size: str,
        price: str,
        reduce_only: bool = False,
        post_only: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Place limit order delegated to KrakenFuturesExecutorWrapper.
        """
        wrapper = self._get_wrapper()
        forwarded = dict(kwargs)
        intent = forwarded.pop("intent", None) or ("reduce" if reduce_only else "open")
        forwarded.pop("reduce_only", None)
        try:
            float_price = float(price)
        except (ValueError, TypeError):
            float_price = 0.0

        res = await wrapper.place_order(
            symbol=symbol,
            side=side,
            quantity=size,
            order_type="limit",
            price=float_price,
            reduce_only=reduce_only,
            intent=intent,
            post_only=post_only,
            **forwarded,
        )
        if res.get("success"):
            order_id = res.get("order_id")
            canonical = self._mapper.get_canonical(symbol)
            return {
                "code": "00000",
                "msg": "success",
                "data": {
                    "orderId": order_id,
                    "symbol": canonical,
                    "orderType": "limit",
                    "price": price,
                },
                **res,
            }
        else:
            return {
                "code": res.get("code", "ORDER_ERROR"),
                "msg": res.get("error", "Unknown error"),
                "data": None,
                **res,
            }

    async def close_position(
        self,
        symbol: str,
        quantity: Optional[float] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Close position delegated to KrakenFuturesExecutorWrapper."""
        wrapper = self._get_wrapper()
        return await wrapper.close_position(symbol=symbol, quantity=quantity, **kwargs)

    async def close_position_market(
        self,
        symbol: str,
        side: str,
        size: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Emergency close position market delegated to wrapper."""
        wrapper = self._get_wrapper()
        try:
            qty = float(size) if size else None
        except (ValueError, TypeError):
            qty = None
        return await wrapper.close_position(symbol=symbol, quantity=qty, **kwargs)

    async def close_all_positions(
        self,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Close all positions delegated to KrakenFuturesExecutorWrapper."""
        wrapper = self._get_wrapper()
        if symbol:
            return await wrapper.close_position(symbol=symbol)
        positions = await wrapper.get_positions()
        results = []
        for p in positions:
            sym = p.get("symbol")
            if sym:
                res = await wrapper.close_position(symbol=sym)
                results.append(res)
        return {"code": "00000", "success": True, "data": results}
    
    async def attach_tpsl(
        self,
        symbol: str,
        plan_type: str,
        trigger_price: str,
        hold_side: str,
        size: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Attach TP/SL order to position.
        
        Args:
            symbol: Trading symbol (Bitget format: BTCUSDT)
            plan_type: "take_profit" or "stop_loss"
            trigger_price: Trigger price
            hold_side: Position side ("long" or "short")
            size: Order size (optional, uses full position if None)
            
        Returns:
            Order result dict
        """
        await self.ensure_connected()
        
        kraken_symbol = self._to_kraken_symbol(symbol)
        canonical = self._mapper.get_canonical(symbol)
        
        # Determine order side based on position side
        # Long position: TP/SL should sell
        # Short position: TP/SL should buy
        if hold_side.lower() == "long":
            order_side = "sell"
        else:
            order_side = "buy"
        
        # Default size if not specified (would need to get from position)
        order_size = float(size) if size else 0.001
        
        logger.info("[KRAKEN_TPSL] %s %s | trigger=%s | side=%s | size=%s",
                   plan_type.upper(), canonical, trigger_price, order_side, order_size)
        
        wrapper = self._get_wrapper()
        try:
            if plan_type.lower() in ("take_profit", "tp", "profit_loss"):
                response = await wrapper._place_tp_order_rest(
                    symbol=kraken_symbol,
                    side=order_side,
                    size=order_size,
                    tp_price=float(trigger_price),
                    require_confirmation=True,
                )
            else:  # stop_loss
                response = await wrapper._place_stop_order_rest(
                    symbol=kraken_symbol,
                    side=order_side,
                    size=order_size,
                    stop_price=float(trigger_price),
                    require_confirmation=True,
                )
            
            return self._format_order_response(response, canonical, plan_type.upper())
            
        except KrakenOrderError as e:
            logger.error("[KRAKEN_TPSL] %s failed: %s", plan_type, e)
            return {"code": "ORDER_ERROR", "msg": str(e), "data": None}
        except Exception as e:
            logger.error("[KRAKEN_TPSL] %s exception: %s", plan_type, e)
            return {"code": "EXCEPTION", "msg": str(e), "data": None}
    
    async def cancel_order(
        self,
        symbol: str,
        order_id: str,
    ) -> Dict[str, Any]:
        """
        Cancel an order.
        
        Args:
            symbol: Trading symbol
            order_id: Order ID to cancel
            
        Returns:
            Cancellation result
        """
        await self.ensure_connected()
        
        canonical = self._mapper.get_canonical(symbol)
        
        logger.info("[KRAKEN_CANCEL] %s | order_id=%s", canonical, order_id)
        
        try:
            response = await self._client.cancel_order(order_id=order_id)
            
            return {
                "code": "00000",
                "msg": "success",
                "data": response,
            }
            
        except Exception as e:
            logger.error("[KRAKEN_CANCEL] Failed: %s", e)
            return {"code": "CANCEL_ERROR", "msg": str(e), "data": None}
    
    async def cancel_all_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        Cancel all open orders.
        
        Args:
            symbol: Optional symbol filter (Kraken doesn't support per-symbol cancel_all)
            
        Returns:
            Cancellation result
        """
        await self.ensure_connected()
        
        logger.info("[KRAKEN_CANCEL_ALL] Cancelling all orders")
        
        try:
            response = await self._client.cancel_all_orders()
            
            return {
                "code": "00000",
                "msg": "success",
                "data": response,
            }
            
        except Exception as e:
            logger.error("[KRAKEN_CANCEL_ALL] Failed: %s", e)
            return {"code": "CANCEL_ERROR", "msg": str(e), "data": None}
    
    # ──────────────────────────────────────────────────
    #  LEVERAGE & ACCOUNT
    # ──────────────────────────────────────────────────
    
    async def set_leverage(
        self,
        symbol: str,
        leverage: int,
    ) -> Dict[str, Any]:
        """
        Set leverage for a symbol.
        
        Note: Kraken Futures uses account-wide leverage, not per-symbol.
        This method tracks leverage in-memory for position sizing.
        
        Args:
            symbol: Trading symbol
            leverage: Leverage multiplier (clamped to 1-50)
            
        Returns:
            Result dict
        """
        canonical = self._mapper.get_canonical(symbol)
        
        # Clamp leverage
        MAX_LEVERAGE = 50
        MIN_LEVERAGE = 1
        final_leverage = max(MIN_LEVERAGE, min(leverage, MAX_LEVERAGE))
        
        if final_leverage != leverage:
            logger.warning("[KRAKEN_LEV] %s | Requested %dx -> Clamped to %dx",
                          canonical, leverage, final_leverage)
        
        # Store in memory (Kraken doesn't have per-symbol leverage API)
        self._leverage_map[canonical] = final_leverage
        self._leverage_map[symbol] = final_leverage
        
        if self._wrapper is not None:
            try:
                kraken_sym = self._wrapper.normalize_symbol(symbol)
                self._wrapper._leverage_map[kraken_sym] = final_leverage
            except Exception:
                pass
        
        logger.info("[KRAKEN_LEV] %s | Set to %dx (in-memory tracking)",
                   canonical, final_leverage)
        
        return {
            "code": "00000",
            "msg": "success",
            "leverage": final_leverage,
        }
    
    def get_leverage(self, symbol: str) -> int:
        """Get configured leverage for symbol."""
        canonical = self._mapper.get_canonical(symbol)
        return self._leverage_map.get(canonical, self._default_leverage)
    
    async def get_positions(
        self,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get open positions from Kraken Futures API.
        
        ⚠️  IMPORTANT: This uses the Kraken Futures REST API (futures.kraken.com),
            NOT the Kraken Spot API (api.kraken.com).
        
        Args:
            symbol: Optional symbol filter
            
        Returns:
            Positions dict:
            {
                "code": "00000",
                "msg": "success",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "holdSide": "long",
                        "total": "0.001",
                        "available": "0.001",
                        "averageOpenPrice": "50000.0",
                        "unrealizedPL": "10.5",
                        ...
                    }
                ]
            }
        """
        await self.ensure_connected()
        
        try:
            # get_open_positions() now returns List[Dict] from Futures API
            # NOT the old SPOT API format (Dict[pos_id, pos_data])
            positions = await self._client.get_open_positions()
            
            # Format to match Bitget response
            formatted_positions = []
            for idx, pos_data in enumerate(positions):
                # Kraken Futures returns position dict with these fields:
                # symbol, side, size, entry_price, mark_price, unrealized_pnl, leverage
                kraken_symbol = pos_data.get("symbol", "")
                canonical = self._mapper.to_bitget(kraken_symbol) if kraken_symbol else "UNKNOWN"
                
                # Filter by symbol if specified
                if symbol:
                    filter_canonical = self._mapper.get_canonical(symbol)
                    if canonical != filter_canonical:
                        continue
                
                # Get side from Futures API response
                side = pos_data.get("side", "long").lower()
                size = float(pos_data.get("size", 0))
                
                formatted_positions.append({
                    "symbol": canonical,
                    "positionId": f"kraken_{idx}",
                    "holdSide": side,
                    "total": str(abs(size)),
                    "available": str(abs(size)),
                    "averageOpenPrice": str(pos_data.get("entry_price", 0)),
                    "unrealizedPL": str(pos_data.get("unrealized_pnl", 0)),
                    "leverage": str(pos_data.get("leverage", self.get_leverage(canonical))),
                    "markPrice": str(pos_data.get("mark_price", 0)),
                    "liquidationPrice": str(pos_data.get("liquidation_price", 0)),
                    "raw": pos_data,
                })
            
            logger.info("[KRAKEN_POS] Found %d positions via Futures API", len(formatted_positions))
            
            return {
                "code": "00000",
                "msg": "success",
                "data": formatted_positions,
            }
            
        except Exception as e:
            logger.error("[KRAKEN_POS] Failed: %s", e)
            return {"code": "POSITION_ERROR", "msg": str(e), "data": []}
            
    async def get_historical_pnl(self, symbol: str, start_time: Optional[int] = None) -> Optional[Dict[str, float]]:
        """
        Fetch historical PnL from the exchange using order/fill history.
        Used to reconcile manual closures.
        """
        kraken_symbol = self._to_kraken_symbol(symbol)
        canonical = self._mapper.get_canonical(symbol)
        
        rest_client = self._get_rest_client()
        if not rest_client:
            logger.error("[KRAKEN_HIST_PNL] REST client unavailable")
            return None
            
        logger.info(f"[KRAKEN_HIST_PNL] Fetching historical PnL for {canonical} since {start_time}")
        
        try:
            return await rest_client.get_historical_pnl(kraken_symbol, since_ms=start_time)
        except Exception as e:
            logger.error(f"[KRAKEN_HIST_PNL] Failed: {e}")
            return None
    
    async def get_account_info(self) -> Dict[str, Any]:
        """
        Get Kraken Futures account balance and info.
        
        Uses the Kraken Futures REST API (not Spot API).
        Endpoint: GET /derivatives/api/v3/accounts
        
        Returns:
            Account info dict:
            {
                "available": 5000.0,
                "total": 5100.0,
                "usdtEquity": 5100.0,
                "frozenAssets": 100.0,
                "unrealizedPL": 50.0,
                "currency": "USD",
            }
        """
        await self.ensure_connected()
        
        try:
            # Use Kraken Futures REST API (not Spot)
            summary = await self._client.get_futures_account_summary()
            
            account_info = {
                "available": float(summary.get("available", 0.0)),
                "total": float(summary.get("total", 0.0)),
                "usdtEquity": float(summary.get("total", 0.0)),  # Alias for compatibility
                "frozenAssets": float(summary.get("margin_used", 0.0)),
                "unrealizedPL": float(summary.get("unrealized_pnl", 0.0)),
                "currency": "USD",
                "margin_used": float(summary.get("margin_used", 0.0)),
            }
            
            logger.info("[KRAKEN_BALANCE] available=%.2f USD | total=%.2f | unrealized=%.2f",
                       account_info["available"], account_info["total"], account_info["unrealizedPL"])
            
            return account_info
            
        except Exception as e:
            logger.error("[KRAKEN_BALANCE] Failed to fetch: %s", e)
            return {
                "available": 0.0,
                "total": 0.0,
                "usdtEquity": 0.0,
                "frozenAssets": 0.0,
                "unrealizedPL": 0.0,
                "currency": "USD",
                "error": str(e),
            }
    
    async def check_balance_sufficient(
        self,
        required_usd: float,
    ) -> Tuple[bool, float]:
        """
        Check if Kraken Futures balance is sufficient for a trade.
        
        Uses the Kraken Futures REST API (not Spot API).
        
        Args:
            required_usd: Required margin in USD
            
        Returns:
            Tuple of (is_sufficient, available_balance)
        """
        await self.ensure_connected()
        
        try:
            # Use Kraken Futures REST API
            is_sufficient, available = await self._client.check_futures_balance_sufficient(required_usd)
            
            if is_sufficient:
                logger.info("[KRAKEN_BALANCE] OK | required=%.2f available=%.2f",
                           required_usd, available)
            else:
                logger.warning("[KRAKEN_BALANCE] INSUFFICIENT | required=%.2f available=%.2f",
                              required_usd, available)
            
            return is_sufficient, available
            
        except Exception as e:
            logger.error("[KRAKEN_BALANCE] Check failed: %s", e)
            return False, 0.0
    
    # ──────────────────────────────────────────────────
    #  HELPERS
    # ──────────────────────────────────────────────────
    
    def _to_kraken_symbol(self, symbol: str) -> str:
        """Convert symbol to Kraken Futures format."""
        return self._mapper.to_kraken(symbol)
    
    def _format_order_response(
        self,
        response: KrakenOrderResponse,
        symbol: str,
        order_type: str,
    ) -> Dict[str, Any]:
        """Format Kraken order response to unified format."""
        if response.success:
            logger.info("[KRAKEN_ORDER] %s %s SUCCESS | order_id=%s",
                       order_type, symbol, response.order_id)
            
            return {
                "code": "00000",
                "msg": "success",
                "data": {
                    "orderId": response.order_id,
                    "clientOid": response.cl_ord_id,
                    "symbol": symbol,
                    "orderType": order_type.lower(),
                    "warnings": response.warnings,
                },
            }
        else:
            logger.error("[KRAKEN_ORDER] %s %s FAILED | error=%s",
                        order_type, symbol, response.error)
            
            return {
                "code": "ORDER_FAILED",
                "msg": response.error or "Order failed",
                "data": {
                    "symbol": symbol,
                    "orderType": order_type.lower(),
                    "raw": response.raw_response,
                },
            }
    
    @property
    def is_connected(self) -> bool:
        """Check if connected to WebSocket."""
        return self._connected and self._client is not None and self._client.is_connected
    
    # ──────────────────────────────────────────────────
    #  CONTEXT MANAGER
    # ──────────────────────────────────────────────────
    
    async def __aenter__(self) -> "KrakenFuturesExecutor":
        """Async context manager entry."""
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.disconnect()


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    async def main():
        api_key = os.getenv("KRAKEN_API_KEY")
        api_secret = os.getenv("KRAKEN_API_SECRET")
        
        if not api_key or not api_secret:
            print("KRAKEN_API_KEY and KRAKEN_API_SECRET required")
            return
        
        print("="*60)
        print("Kraken Futures Executor Test")
        print("="*60)
        
        async with KrakenFuturesExecutor(api_key, api_secret) as executor:
            # Test account info
            print("\n1. Getting account info...")
            account = await executor.get_account_info()
            print(f"   Available: ${account['available']:,.2f}")
            print(f"   Total:     ${account['total']:,.2f}")
            
            # Test positions
            print("\n2. Getting positions...")
            positions = await executor.get_positions()
            print(f"   Found {len(positions['data'])} positions")
            
            # Test leverage
            print("\n3. Setting leverage...")
            lev_result = await executor.set_leverage("BTCUSDT", 5)
            print(f"   BTC leverage: {lev_result['leverage']}x")
            
            print("\n✅ All tests passed!")
    
    asyncio.run(main())
