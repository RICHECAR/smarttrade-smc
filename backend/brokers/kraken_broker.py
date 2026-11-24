"""
Kraken Broker Implementation
"""

import logging
from typing import Optional, Dict, Any, List
from backend.brokers.base import BaseBroker, BrokerHealth, MinOrder, OrderRequest, OrderResult
from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor

logger = logging.getLogger("smarttrade.kraken_broker")

class KrakenBroker(BaseBroker):
    """
    Kraken Futures implementation of the BaseBroker interface.
    Delegates to KrakenFuturesExecutor.
    """
    
    def __init__(self, executor: KrakenFuturesExecutor):
        self._executor = executor
        
    @property
    def name(self) -> str:
        return "kraken"
        
    async def healthcheck(self) -> BrokerHealth:
        try:
            # Using REST API get_account_info to verify credentials and Futures access
            account_info = await self._executor.get_account_info()
            if "error" in account_info and account_info["error"]:
                return BrokerHealth("ERROR", f"Kraken healthcheck failed: {account_info['error']}")
                
            return BrokerHealth("OK", f"Connected to Kraken Futures. Available: {account_info.get('available')} USD")
        except Exception as e:
            return BrokerHealth("ERROR", f"Exception during healthcheck: {str(e)}")

    async def get_min_order(self, symbol: str) -> MinOrder:
        # Kraken Futures uses integer USD contracts (1 contract = 1 USD)
        return MinOrder(
            min_qty=1,
            min_notional=1.0,
            step=1,
            price_precision=2,
            qty_precision=0
        )

    async def place_order(self, order: OrderRequest) -> OrderResult:
        try:
            exec_side = "buy" if order.side.lower() == "long" else "sell"
            
            # CRITICAL FIX: Convert coin quantity to Kraken integer contracts
            from backend.execution.kraken_futures_executor import to_kraken_symbol
            kraken_sym = to_kraken_symbol(order.symbol)
            try:
                from backend.risk.size_calculator import ensure_kraken_contracts
                int_qty = ensure_kraken_contracts(order.qty, kraken_sym)
            except ImportError:
                int_qty = max(1, int(order.qty)) if order.qty >= 1 else 1

            logger.info(
                "[KRAKEN_BROKER] place_order | %s %s | raw_qty=%.6f → contracts=%d",
                order.symbol, exec_side, order.qty, int_qty,
            )

            # Use market order natively
            res = await self._executor.place_order_market(
                symbol=order.symbol,
                side=exec_side,
                size=str(int_qty),
                reduce_only=order.reduce_only
            )
            
            if res.get("code") != "00000":
                return OrderResult(False, error=res.get("msg", "Unknown API error"))
                
            order_id = res.get("data", {}).get("orderId")
            
            # Attach SL/TP if provided — also use contracts for size
            if order.stop_loss and order_id:
                import asyncio
                await asyncio.sleep(0.5)
                await self._executor.attach_tpsl(
                    symbol=order.symbol,
                    plan_type="stop_loss",
                    trigger_price=str(order.stop_loss),
                    hold_side=order.side,
                    size=str(int_qty)
                )
                
            if order.take_profit and order_id:
                import asyncio
                await asyncio.sleep(0.5)
                await self._executor.attach_tpsl(
                    symbol=order.symbol,
                    plan_type="take_profit",
                    trigger_price=str(order.take_profit),
                    hold_side=order.side,
                    size=str(int_qty)
                )

            return OrderResult(success=True, order_id=order_id)
            
        except Exception as e:
            logger.error(f"[KRAKEN_BROKER] place_order failed: {e}")
            return OrderResult(False, error=str(e))

    async def close_position(self, symbol: str) -> OrderResult:
        try:
            # Kraken does not have a "flash close" API, we deduce the side and reduce_only
            positions_res = await self._executor.get_positions(symbol)
            positions = positions_res.get("data", [])
            
            if not positions:
                return OrderResult(success=True) # Nothing to close
                
            pos = positions[0]
            size_to_close = pos.get("available", pos.get("total", "0"))
            opposite_side = "buy" if pos.get("holdSide", "").lower() == "short" else "sell"
            
            res = await self._executor.place_order_market(
                symbol=symbol,
                side=opposite_side,
                size=str(abs(float(size_to_close))),
                reduce_only=True
            )
            
            if res.get("code") == "00000":
                return OrderResult(success=True)
            return OrderResult(success=False, error=res.get("msg"))
            
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    async def get_positions(self) -> List[Dict[str, Any]]:
        try:
            res = await self._executor.get_positions()
            if res.get("code") == "00000":
                return res.get("data", [])
            return []
        except Exception as e:
            logger.error(f"[KRAKEN_BROKER] get_positions failed: {e}")
            return []
