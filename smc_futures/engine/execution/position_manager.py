"""
Position Manager — Close/Reduce positions on Bitget USDT-Futures

Features:
- close_position_market(symbol): Full close with reduce_only
- reduce_position_market(symbol, reduce_pct): Partial close
- Validates qty against contract specs (min_qty, qty_step)
- Persists fills to SQLite + broadcasts WS events

Author: SMC Trading Bot Framework
"""

import logging
import math
from typing import Dict, Any, Optional, Tuple
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

from smc_futures.engine.execution.contract_specs import get_contract_specs, ContractSpecs

# Import persistence hooks
try:
    from backend.services.trade_persistence_hook import (
        on_trade_closed,
        on_trade_reduced
    )
    from backend.services.live_trades_service import LiveTradesService
    PERSISTENCE_AVAILABLE = True
except ImportError:
    PERSISTENCE_AVAILABLE = False

# Analytics recording
try:
    from backend.services.analytics_service import AnalyticsRecorder as _AR
    _analytics_available = True
except ImportError:
    _AR = None  # type: ignore
    _analytics_available = False
    
    async def on_trade_closed(*args, **kwargs): return None
    async def on_trade_reduced(*args, **kwargs): return None
    class LiveTradesService:
        @staticmethod
        def get_open_trade_for_symbol(*args, **kwargs): return None

logger = logging.getLogger(__name__)


class PositionManager:
    """
    Manages position close/reduce operations.
    
    Usage:
        pm = PositionManager(executor)
        result = await pm.close_position_market("BTCUSDT")
        result = await pm.reduce_position_market("SOLUSDT", reduce_pct=50.0)
    """
    
    def __init__(self, executor):
        """
        Args:
            executor: BitgetFuturesExecutor instance
        """
        self.executor = executor
        self.product_type = getattr(executor, 'product_type', 'USDT-FUTURES')
        
        logger.info("[POS_MANAGER] Initialized")
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # CLOSE POSITION (Full)
    # ═══════════════════════════════════════════════════════════════════════════════
    
    async def close_position_market(
        self,
        symbol: str,
        hold_side: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Close entire position with market order + reduce_only.
        
        Args:
            symbol: Trading pair (e.g., BTCUSDT)
            hold_side: "long" or "short" (auto-detected if not provided)
        
        Returns:
            {
                "success": bool,
                "order_id": str,
                "qty_closed": float,
                "close_price": float,
                "pnl": float,
                "fee": float,
                "error": str (if failed),
                "raw_response": dict,
            }
        """
        symbol = symbol.upper().replace("/", "")
        
        logger.info(f"[CLOSE_POS] Initiating FULL close | {symbol} | side={hold_side or 'auto'}")
        
        # 1. Get current position
        position = await self._get_position(symbol, hold_side)
        if not position:
            return {
                "success": False,
                "error": f"No open position found for {symbol}" + (f" ({hold_side})" if hold_side else ""),
            }
        
        pos_size = position["size"]
        pos_side = position["side"]  # "long" or "short"
        
        if pos_size <= 0:
            return {
                "success": False,
                "error": f"Position size is zero for {symbol} {pos_side}",
            }
        
        # 2. Place close order
        return await self._execute_close_order(
            symbol=symbol,
            hold_side=pos_side,
            qty=pos_size,
            action="CLOSE",
            position=position,
        )
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # REDUCE POSITION (Partial)
    # ═══════════════════════════════════════════════════════════════════════════════
    
    async def reduce_position_market(
        self,
        symbol: str,
        reduce_pct: float = 50.0,
        hold_side: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Reduce position by percentage with market order + reduce_only.
        
        Args:
            symbol: Trading pair (e.g., BTCUSDT)
            reduce_pct: Percentage to close (1-99)
            hold_side: "long" or "short" (auto-detected if not provided)
        
        Returns:
            Same as close_position_market
        """
        symbol = symbol.upper().replace("/", "")
        reduce_pct = min(max(reduce_pct, 1.0), 99.0)  # Clamp 1-99
        
        logger.info(f"[REDUCE_POS] Initiating {reduce_pct}% reduce | {symbol} | side={hold_side or 'auto'}")
        
        # 1. Get current position
        position = await self._get_position(symbol, hold_side)
        if not position:
            return {
                "success": False,
                "error": f"No open position found for {symbol}" + (f" ({hold_side})" if hold_side else ""),
            }
        
        pos_size = position["size"]
        pos_side = position["side"]
        
        if pos_size <= 0:
            return {
                "success": False,
                "error": f"Position size is zero for {symbol} {pos_side}",
            }
        
        # 2. Calculate reduce quantity
        raw_reduce_qty = pos_size * (reduce_pct / 100.0)
        
        # 3. Validate against contract specs
        specs = get_contract_specs(symbol)
        validated_qty, _, details = self._validate_reduce_qty(
            specs=specs,
            raw_qty=raw_reduce_qty,
            position_size=pos_size,
        )
        
        if validated_qty is None:
            return {
                "success": False,
                "error": f"Reduce qty {raw_reduce_qty:.6f} invalid: {details.get('rejection_reason', 'unknown')}",
                "details": details,
            }
        
        # 4. Place reduce order
        return await self._execute_close_order(
            symbol=symbol,
            hold_side=pos_side,
            qty=validated_qty,
            action="REDUCE",
            position=position,
            reduce_pct=reduce_pct,
        )
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ═══════════════════════════════════════════════════════════════════════════════
    
    async def _get_position(
        self,
        symbol: str,
        hold_side: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get current position for symbol.
        
        Returns position dict with: symbol, side, size, entryPrice, unrealizedPnl, etc.
        """
        try:
            result = await self.executor.get_positions(symbol)
            
            if result.get("code") != "00000":
                logger.error(f"[POS_MANAGER] Failed to get positions: {result.get('msg')}")
                return None
            
            positions = result.get("positions", []) or result.get("data", [])
            
            for pos in positions:
                if pos.get("symbol", "").upper() == symbol.upper():
                    pos_side = (pos.get("side") or pos.get("holdSide") or "").lower()
                    pos_size = float(pos.get("size", 0) or pos.get("total", 0) or 0)
                    
                    # If hold_side specified, match it
                    if hold_side and pos_side != hold_side.lower():
                        continue
                    
                    if pos_size > 0:
                        return {
                            "symbol": symbol,
                            "side": pos_side,
                            "size": pos_size,
                            "available": float(pos.get("available", pos_size) or pos_size),
                            "entryPrice": float(pos.get("entryPrice", 0) or pos.get("openPriceAvg", 0) or 0),
                            "markPrice": float(pos.get("markPrice", 0) or 0),
                            "unrealizedPnl": float(pos.get("unrealizedPnl", 0) or pos.get("unrealizedPL", 0) or 0),
                            "leverage": int(pos.get("leverage", 1) or 1),
                            "raw": pos,
                        }
            
            return None
            
        except Exception as e:
            logger.error(f"[POS_MANAGER] Error getting position: {e}", exc_info=True)
            return None
    
    def _validate_reduce_qty(
        self,
        specs: ContractSpecs,
        raw_qty: float,
        position_size: float,
    ) -> Tuple[Optional[float], str, Dict[str, Any]]:
        """
        Validate and normalize reduce quantity.
        
        Returns:
            (validated_qty, status, details)
            validated_qty is None if rejected
        """
        details = {
            "raw_qty": raw_qty,
            "position_size": position_size,
            "min_qty": specs.min_qty,
            "qty_step": specs.qty_step,
        }
        
        # Round to qty_step
        d_qty = Decimal(str(raw_qty))
        d_step = Decimal(str(specs.qty_step))
        rounded_qty = float((d_qty / d_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * d_step)
        
        details["rounded_qty"] = rounded_qty
        
        # Check minimum
        if rounded_qty < specs.min_qty:
            # For reduce, if remaining would be < min_qty, close all instead
            remaining = position_size - rounded_qty
            if remaining < specs.min_qty and remaining > 0:
                # Close everything
                rounded_qty = position_size
                details["converted_to_full_close"] = True
                logger.info(
                    f"[POS_MANAGER] Reduce converted to full close | "
                    f"remaining {remaining:.6f} < min_qty {specs.min_qty}"
                )
            else:
                details["rejection_reason"] = f"qty {rounded_qty:.6f} < min_qty {specs.min_qty}"
                return None, "REJECTED", details
        
        # Check not exceeding position
        if rounded_qty > position_size:
            rounded_qty = position_size
            details["clamped_to_position"] = True
        
        details["final_qty"] = rounded_qty
        return rounded_qty, "OK", details
    
    async def _execute_close_order(
        self,
        symbol: str,
        hold_side: str,
        qty: float,
        action: str,
        position: Dict[str, Any],
        reduce_pct: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Execute the close/reduce order on Bitget.
        
        Uses tradeSide="close" for reduce_only behavior.
        """
        # Determine order side (opposite of position side)
        # Long position → sell to close
        # Short position → buy to close
        order_side = "sell" if hold_side.lower() == "long" else "buy"
        
        # Format quantity
        specs = get_contract_specs(symbol)
        formatted_qty = f"{qty:.{specs.qty_scale}f}"
        
        logger.info(
            f"[POS_MANAGER] {action} ORDER | {symbol} {hold_side.upper()}"
            f" | qty={formatted_qty} | order_side={order_side}"
        )
        
        try:
            # Build close order payload
            payload = {
                "symbol": symbol,
                "productType": self.product_type,
                "marginCoin": "USDT",
                "marginMode": "isolated",
                "side": order_side,
                "orderType": "market",
                "size": formatted_qty,
                "tradeSide": "close",  # CRITICAL: This makes it reduce_only
            }
            
            # Call executor's HTTP POST
            result = await self.executor._http_post(
                "/api/v2/mix/order/place-order",
                payload,
                f"{action}_{symbol}"
            )
            
            if result.get("code") != "00000":
                error_msg = result.get("msg", "Unknown error")
                logger.error(f"[POS_MANAGER] {action} FAILED | {symbol} | {error_msg}")
                return {
                    "success": False,
                    "error": error_msg,
                    "raw_response": result,
                }
            
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            order_id = data.get("orderId", "")
            fill_verified = result.get("fill_quantity_verified") is True
            filled_qty_raw = result.get("filled_qty")
            fill_price_raw = result.get("fill_price")

            try:
                filled_qty = float(filled_qty_raw)
                fill_price = float(fill_price_raw)
            except (TypeError, ValueError, OverflowError):
                filled_qty = 0.0
                fill_price = 0.0

            if (
                not fill_verified
                or result.get("fill_price_verified") is not True
                or not math.isfinite(filled_qty)
                or filled_qty <= 0
                or filled_qty > qty + max(1e-12, qty * 1e-8)
                or not math.isfinite(fill_price)
                or fill_price <= 0
            ):
                logger.critical(
                    "[POS_MANAGER] %s order accepted without a confirmed fill; "
                    "position state remains unknown | %s | order_id=%s",
                    action,
                    symbol,
                    order_id,
                )
                return {
                    "success": False,
                    "status": "unknown",
                    "error": "ORDER_ACCEPTED_FILL_OR_PRICE_UNCONFIRMED",
                    "action": action,
                    "order_id": order_id,
                    "symbol": symbol,
                    "raw_response": result,
                }

            realized_pnl = None
            if result.get("realized_pnl_verified") is True:
                try:
                    candidate_pnl = float(result.get("realized_pnl"))
                    if math.isfinite(candidate_pnl):
                        realized_pnl = candidate_pnl
                except (TypeError, ValueError, OverflowError):
                    pass

            confirmed_fee = None
            if result.get("fee_verified") is True:
                try:
                    candidate_fee = float(result.get("fee"))
                    if math.isfinite(candidate_fee) and candidate_fee >= 0:
                        confirmed_fee = candidate_fee
                except (TypeError, ValueError, OverflowError):
                    pass

            trade = None
            persistence_status = "unavailable"
            if PERSISTENCE_AVAILABLE:
                try:
                    trade = LiveTradesService.get_open_trade_for_symbol(
                        symbol,
                        broker="bitget",
                    )
                    if trade is None:
                        persistence_status = "not_found"
                        logger.error(
                            "[POS_MANAGER] Confirmed fill not persisted: "
                            "no open Bitget trade found for %s",
                            symbol,
                        )
                    elif filled_qty + 1e-12 >= qty:
                        fill = await on_trade_closed(
                            trade_id=trade.id,
                            order_id=order_id,
                            close_price=fill_price,
                            pnl=realized_pnl,
                            fee=confirmed_fee,
                            qty=filled_qty,
                            raw_exchange=result,
                        )
                        persistence_status = "persisted" if fill else "failed"
                    else:
                        fill = await on_trade_reduced(
                            trade_id=trade.id,
                            qty=filled_qty,
                            price=fill_price,
                            pnl=realized_pnl,
                            fee=confirmed_fee,
                            reduce_pct=reduce_pct,
                            order_id=order_id,
                            raw_exchange=result,
                        )
                        persistence_status = "persisted" if fill else "failed"
                except Exception as e:
                    persistence_status = "failed"
                    logger.error("[POS_MANAGER] Persistence Hook FAILED: %s", e, exc_info=True)
            else:
                logger.error(
                    "[POS_MANAGER] Confirmed fill cannot be persisted: persistence service unavailable"
                )

            if (
                realized_pnl is not None
                and confirmed_fee is not None
                and _analytics_available
                and _AR
            ):
                try:
                    trade_id = str(trade.id) if trade else order_id
                    _AR.record_trade_analytics(
                        trade_id=trade_id or symbol,
                        symbol=symbol,
                        side=hold_side,
                        entry_price=position.get("entryPrice"),
                        exit_price=fill_price,
                        size=filled_qty,
                        fees=confirmed_fee,
                        pnl=round(realized_pnl, 2),
                        ts_exit=datetime.now(timezone.utc),
                        broker="bitget",
                    )
                except Exception as e:
                    logger.error("[POS_MANAGER] Analytics recording failed: %s", e)

            return {
                "success": True,
                "status": "filled",
                "action": action,
                "order_id": order_id,
                "symbol": symbol,
                "side": hold_side,
                "order_side": order_side,
                "qty_closed": filled_qty,
                "close_price": fill_price,
                "pnl": realized_pnl,
                "fee": confirmed_fee,
                "reduce_pct": reduce_pct,
                "persistence_status": persistence_status,
                "raw_response": result,
            }
            
        except Exception as e:
            logger.error(f"[POS_MANAGER] {action} EXCEPTION | {symbol} | {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
            }


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL HELPER
# ═══════════════════════════════════════════════════════════════════════════════

_position_manager: Optional[PositionManager] = None


def get_position_manager(executor=None) -> Optional[PositionManager]:
    """Get or create the singleton PositionManager."""
    global _position_manager
    
    if _position_manager is None and executor is not None:
        _position_manager = PositionManager(executor)
    
    return _position_manager


def set_position_manager(executor) -> PositionManager:
    """Initialize the singleton PositionManager."""
    global _position_manager
    _position_manager = PositionManager(executor)
    return _position_manager
