"""
Close Position Service - Unified, safe, and robust position closing for Bitget Futures

Features:
- Single entry point: close_position(symbol, percent=100, ...)
- Per-symbol locking (prevents double execution)
- Idempotence (returns OK if no position or already closing)
- reduceOnly=True always enforced
- Contract specs validation (min_qty, qty_step, qty_precision)
- Confirmation polling with timeout
- Full DB persistence + WebSocket broadcast
- Structured logging

Author: SMC Trading Bot Framework
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Dict, Any, Optional, Tuple

from smc_futures.engine.execution.contract_specs import get_contract_specs, ContractSpecs

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS & RESULT TYPES
# ═══════════════════════════════════════════════════════════════════════════════

class CloseStatus(str, Enum):
    """Close operation result status."""
    SUCCESS = "success"              # Position closed/reduced successfully
    ALREADY_CLOSED = "already_closed"  # No position to close (idempotent OK)
    IN_PROGRESS = "in_progress"      # Another close operation is running (lock held)
    PARTIAL = "partial"              # Partial close executed
    FAILED = "failed"                # Execution failed
    TIMEOUT = "timeout"              # Confirmation timed out
    REJECTED = "rejected"            # Qty validation rejected
    UNKNOWN = "unknown"              # Position state unclear after order


@dataclass
class CloseResult:
    """Result of a close_position operation."""
    status: CloseStatus
    success: bool
    symbol: str
    message: str
    
    # Order details (when success)
    order_id: Optional[str] = None
    side: Optional[str] = None  # "long" or "short"
    qty_closed: float = 0.0
    qty_remaining: float = 0.0
    close_price: float = 0.0
    entry_price: float = 0.0
    pnl: float = 0.0
    fee: float = 0.0
    percent_requested: float = 100.0
    
    # Metadata
    close_reason: str = "manual_close"
    trade_id: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for API response."""
        return {
            "status": self.status.value,
            "success": self.success,
            "symbol": self.symbol,
            "message": self.message,
            "order_id": self.order_id,
            "side": self.side,
            "qty_closed": self.qty_closed,
            "qty_remaining": self.qty_remaining,
            "close_price": self.close_price,
            "entry_price": self.entry_price,
            "pnl": round(self.pnl, 2),
            "fee": round(self.fee, 4),
            "percent_requested": self.percent_requested,
            "close_reason": self.close_reason,
            "trade_id": self.trade_id,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# PER-SYMBOL LOCK MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CloseLockManager:
    """
    Manages per-symbol locks for close operations.
    Prevents double-close from UI + bot or double-click.
    """
    _locks: Dict[str, asyncio.Lock] = field(default_factory=dict)
    _active_closes: Dict[str, str] = field(default_factory=dict)  # symbol -> order_id
    
    def get_lock(self, symbol: str) -> asyncio.Lock:
        """Get or create a lock for a symbol."""
        symbol = symbol.upper()
        if symbol not in self._locks:
            self._locks[symbol] = asyncio.Lock()
        return self._locks[symbol]
    
    def is_closing(self, symbol: str) -> bool:
        """Check if a close operation is in progress for this symbol."""
        return symbol.upper() in self._active_closes
    
    def get_active_close_order(self, symbol: str) -> Optional[str]:
        """Get the order_id of the active close operation."""
        return self._active_closes.get(symbol.upper())
    
    def set_closing(self, symbol: str, order_id: str) -> None:
        """Mark symbol as being closed."""
        self._active_closes[symbol.upper()] = order_id
    
    def clear_closing(self, symbol: str) -> None:
        """Clear the closing state for a symbol."""
        self._active_closes.pop(symbol.upper(), None)


# Singleton lock manager
_lock_manager = CloseLockManager()


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL REFERENCES (Set during app startup)
# ═══════════════════════════════════════════════════════════════════════════════

_executor = None           # Bitget executor (default)
_kraken_executor = None    # Kraken executor (optional)
_ws_manager = None
_persistence_enabled = True

# Symbols known to be on Kraken (auto-detected or configured)
_KRAKEN_SYMBOL_PREFIXES = ("PF_", "PI_", "FF_", "FI_")


def _detect_broker(symbol: str) -> str:
    """Detect which broker owns this symbol."""
    upper = symbol.upper().replace("/", "").replace("-", "")
    if upper.startswith(_KRAKEN_SYMBOL_PREFIXES):
        return "kraken"
    if _kraken_executor is not None and hasattr(_kraken_executor, 'normalize_symbol'):
        # Check if it maps to a Kraken symbol
        try:
            from backend.execution.kraken_futures_executor import SYMBOL_MAP_TO_KRAKEN
            if upper in SYMBOL_MAP_TO_KRAKEN:
                return "kraken"  # only if kraken executor is registered
        except ImportError:
            pass
    return "bitget"


def _get_executor_for_symbol(symbol: str):
    """Return the correct executor for a given symbol."""
    broker = _detect_broker(symbol)
    if broker == "kraken" and _kraken_executor is not None:
        return _kraken_executor, "kraken"
    return _executor, "bitget"


def set_close_service_executor(executor):
    """Set the Bitget executor reference."""
    global _executor
    _executor = executor
    logger.info("[CLOSE_SERVICE] Bitget executor registered")


def set_close_service_kraken_executor(executor):
    """Set the Kraken executor reference."""
    global _kraken_executor
    _kraken_executor = executor
    logger.info("[CLOSE_SERVICE] Kraken executor registered")


def set_close_service_ws_manager(manager):
    """Set the WebSocket manager reference."""
    global _ws_manager
    _ws_manager = manager
    logger.info("[CLOSE_SERVICE] WebSocket manager registered")


def set_close_service_persistence_enabled(enabled: bool):
    """Enable/disable persistence (for testing)."""
    global _persistence_enabled
    _persistence_enabled = enabled


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

async def close_position(
    symbol: str,
    percent: float = 100.0,
    reason: str = "manual_close",
    timeout_sec: float = 10.0,
    slippage_bps: int = 20,
    hold_side: Optional[str] = None,
) -> CloseResult:
    """
    Close or reduce a position safely with full validation.
    
    Args:
        symbol: Trading pair (e.g., "BTCUSDT")
        percent: Percentage to close (1-100). 100 = full close.
        reason: Close reason for logging (e.g., "manual_close", "stop_loss", "take_profit")
        timeout_sec: Max seconds to wait for confirmation
        slippage_bps: Acceptable slippage in basis points (not used for market orders)
        hold_side: "long" or "short" (auto-detected if None)
    
    Returns:
        CloseResult with status, success flag, and details
    
    Idempotent:
        - No position found → returns ALREADY_CLOSED (success=True)
        - Close in progress → returns IN_PROGRESS with existing order_id
    """
    symbol = symbol.upper().replace("/", "")
    percent = max(1.0, min(100.0, percent))
    
    logger.info(
        f"[CLOSE] START symbol={symbol} percent={percent}% "
        f"reason={reason} timeout={timeout_sec}s hold_side={hold_side or 'auto'}"
    )
    
    # ─────────────────────────────────────────────────────────────
    # 1. CHECK EXECUTOR
    # ─────────────────────────────────────────────────────────────
    active_executor, broker = _get_executor_for_symbol(symbol)
    if active_executor is None:
        logger.error("[CLOSE] No executor available for broker=%s symbol=%s", broker, symbol)
        return CloseResult(
            status=CloseStatus.FAILED,
            success=False,
            symbol=symbol,
            message=f"Executor not initialized for {broker}",
            close_reason=reason,
        )
    
    # ─────────────────────────────────────────────────────────────
    # 2. ACQUIRE LOCK (non-blocking check first)
    # ─────────────────────────────────────────────────────────────
    if _lock_manager.is_closing(symbol):
        active_order = _lock_manager.get_active_close_order(symbol)
        logger.warning(f"[CLOSE] IN_PROGRESS symbol={symbol} active_order={active_order}")
        return CloseResult(
            status=CloseStatus.IN_PROGRESS,
            success=True,  # Idempotent: not an error
            symbol=symbol,
            message=f"Close already in progress (order_id={active_order})",
            order_id=active_order,
            close_reason=reason,
        )
    
    lock = _lock_manager.get_lock(symbol)
    
    # Try to acquire lock with timeout
    try:
        acquired = await asyncio.wait_for(lock.acquire(), timeout=2.0)
    except asyncio.TimeoutError:
        logger.warning(f"[CLOSE] Lock timeout symbol={symbol}")
        return CloseResult(
            status=CloseStatus.IN_PROGRESS,
            success=True,
            symbol=symbol,
            message="Could not acquire lock (another operation in progress)",
            close_reason=reason,
        )
    
    try:
        # ─────────────────────────────────────────────────────────────
        # 3. GET CURRENT POSITION
        # ─────────────────────────────────────────────────────────────
        position = await _get_position(symbol, hold_side, broker=broker)
        
        if position is None:
            logger.info(f"[CLOSE] ALREADY_CLOSED symbol={symbol} (no position found)")
            return CloseResult(
                status=CloseStatus.ALREADY_CLOSED,
                success=True,  # Idempotent OK
                symbol=symbol,
                message="No open position found",
                close_reason=reason,
            )
        
        pos_size = position["size"]
        pos_side = position["side"]
        entry_price = position["entryPrice"]
        mark_price = position["markPrice"]
        
        if pos_size <= 0:
            logger.info(f"[CLOSE] ALREADY_CLOSED symbol={symbol} (position size is 0)")
            return CloseResult(
                status=CloseStatus.ALREADY_CLOSED,
                success=True,
                symbol=symbol,
                message="Position size is zero",
                close_reason=reason,
            )
        
        logger.info(
            f"[CLOSE] POSITION_FOUND symbol={symbol} side={pos_side} "
            f"size={pos_size} entry={entry_price} mark={mark_price}"
        )
        
        # ─────────────────────────────────────────────────────────────
        # 4. CALCULATE & VALIDATE QUANTITY
        # ─────────────────────────────────────────────────────────────
        if percent >= 100:
            # Full close: use exact position size (no rounding needed)
            close_qty = pos_size
            is_full_close = True
        else:
            # Partial close: calculate and validate
            raw_qty = pos_size * (percent / 100.0)
            close_qty, status, details = _normalize_qty(symbol, raw_qty, pos_size, entry_price)
            
            if close_qty is None:
                logger.warning(f"[CLOSE] REJECTED symbol={symbol} reason={details.get('rejection_reason')}")
                return CloseResult(
                    status=CloseStatus.REJECTED,
                    success=False,
                    symbol=symbol,
                    message=f"Quantity validation failed: {details.get('rejection_reason', 'unknown')}",
                    side=pos_side,
                    percent_requested=percent,
                    close_reason=reason,
                    raw_response=details,
                )
            
            # Check if partial close got converted to full close
            is_full_close = details.get("converted_to_full_close", False)
        
        remaining_qty = pos_size - close_qty
        
        logger.info(
            f"[CLOSE] QTY_CALCULATED symbol={symbol} close_qty={close_qty} "
            f"remaining={remaining_qty} is_full={is_full_close}"
        )
        
        # ─────────────────────────────────────────────────────────────
        # 5. EXECUTE CLOSE ORDER
        # ─────────────────────────────────────────────────────────────
        order_result = await _execute_close_order(
            symbol=symbol,
            hold_side=pos_side,
            qty=close_qty,
            is_full_close=is_full_close,
            broker=broker,
        )
        
        if not order_result.get("success"):
            error_msg = order_result.get("error", "Unknown error")
            logger.error(f"[CLOSE] ORDER_FAILED symbol={symbol} error={error_msg}")
            return CloseResult(
                status=CloseStatus.FAILED,
                success=False,
                symbol=symbol,
                message=f"Order execution failed: {error_msg}",
                side=pos_side,
                percent_requested=percent,
                close_reason=reason,
                raw_response=order_result.get("raw_response"),
            )
        
        order_id = order_result["order_id"]
        _lock_manager.set_closing(symbol, order_id)
        
        logger.info(f"[CLOSE] ORDER_SENT order_id={order_id} reduceOnly=True")
        
        # ─────────────────────────────────────────────────────────────
        # 6. WAIT FOR CONFIRMATION
        # ─────────────────────────────────────────────────────────────
        confirmed, confirm_data = await _wait_for_confirmation(
            symbol=symbol,
            expected_qty_reduction=close_qty,
            original_size=pos_size,
            timeout_sec=timeout_sec,
            broker=broker,
            hold_side=pos_side,
        )
        
        # Get actual close price (from mark or avg fill)
        actual_close_price = confirm_data.get("close_price", mark_price)
        actual_remaining = confirm_data.get("remaining_qty", remaining_qty)
        
        # Calculate PnL
        if pos_side.lower() == "long":
            pnl = (actual_close_price - entry_price) * close_qty
        else:
            pnl = (entry_price - actual_close_price) * close_qty
        
        # Estimate fee (~ 0.06% taker for market orders)
        fee = close_qty * actual_close_price * 0.0006
        
        if not confirmed:
            logger.warning(
                f"[CLOSE] CONFIRMATION_TIMEOUT symbol={symbol} order_id={order_id} "
                f"remaining={actual_remaining}"
            )
            final_status = CloseStatus.UNKNOWN
            final_message = f"Order sent but confirmation timed out (remaining={actual_remaining})"
        else:
            if actual_remaining <= 0 or is_full_close:
                final_status = CloseStatus.SUCCESS
                final_message = "Position fully closed"
            else:
                final_status = CloseStatus.PARTIAL
                final_message = f"Position reduced by {percent}%"
            
            logger.info(
                f"[CLOSE] CONFIRMED symbol={symbol} remaining_qty={actual_remaining} "
                f"pnl={pnl:.2f}"
            )
        
        # ─────────────────────────────────────────────────────────────
        # 7. UPDATE DATABASE
        # ─────────────────────────────────────────────────────────────
        trade_id = None
        if _persistence_enabled:
            trade_id = await _update_database(
                symbol=symbol,
                order_id=order_id,
                close_price=actual_close_price,
                qty_closed=close_qty,
                pnl=pnl,
                fee=fee,
                is_full_close=(actual_remaining <= 0),
                reason=reason,
                percent=percent,
                raw_response=order_result.get("raw_response"),
            )
            logger.info(f"[CLOSE] DB_UPDATED trade_id={trade_id}")
        
        # ─────────────────────────────────────────────────────────────
        # 8a. INSTITUTIONAL TRADE JOURNAL
        # ─────────────────────────────────────────────────────────────
        try:
            from smc_futures.analytics.journal_hook import record_close
            record_close(
                symbol=symbol,
                side=pos_side,
                entry_price=entry_price,
                exit_price=actual_close_price,
                qty=close_qty,
                pnl=pnl,
                fee=fee,
                reason=reason,
                broker=broker,
                trade_id=trade_id,
                order_id=order_id,
                qty_requested=close_qty,
            )
        except Exception as exc:
            logger.error("[CLOSE] Journal write failed: %s", exc)
        
        # ─────────────────────────────────────────────────────────────
        # 8b. BROADCAST WS EVENT
        # ─────────────────────────────────────────────────────────────
        await _broadcast_close_event(
            symbol=symbol,
            side=pos_side,
            qty_closed=close_qty,
            remaining_qty=actual_remaining,
            order_id=order_id,
            close_price=actual_close_price,
            pnl=pnl,
            reason=reason,
            is_full_close=(actual_remaining <= 0),
        )
        logger.info(f"[CLOSE] WS_BROADCAST symbol={symbol} event=position_{'closed' if actual_remaining <= 0 else 'reduced'}")
        
        return CloseResult(
            status=final_status,
            success=final_status in (CloseStatus.SUCCESS, CloseStatus.PARTIAL),
            symbol=symbol,
            message=final_message,
            order_id=order_id,
            side=pos_side,
            qty_closed=close_qty,
            qty_remaining=actual_remaining,
            close_price=actual_close_price,
            entry_price=entry_price,
            pnl=pnl,
            fee=fee,
            percent_requested=percent,
            close_reason=reason,
            trade_id=trade_id,
            raw_response=order_result.get("raw_response"),
        )
        
    finally:
        _lock_manager.clear_closing(symbol)
        lock.release()


# ═══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _get_position(
    symbol: str,
    hold_side: Optional[str] = None,
    broker: str = "bitget",
) -> Optional[Dict[str, Any]]:
    """
    Get current position for symbol from executor.
    
    Supports both Bitget (dict with code/data) and Kraken (list of dicts).
    
    Returns:
        Position dict with: symbol, side, size, entryPrice, markPrice, etc.
        None if no position found.
    """
    active_executor, _ = _get_executor_for_symbol(symbol)
    if active_executor is None:
        return None

    try:
        result = await active_executor.get_positions(symbol)

        # ── Normalize result to a list of position dicts ──
        if isinstance(result, list):
            # Kraken executor returns List[Dict]
            positions = result
        elif isinstance(result, dict):
            # Bitget executor returns {"code": "00000", "data": [...]}
            if result.get("code") != "00000":
                logger.error(f"[CLOSE] Failed to get positions: {result.get('msg')}")
                return None
            positions = result.get("positions", []) or result.get("data", [])
        else:
            positions = []

        for pos in positions:
            pos_symbol = (pos.get("symbol", "") or "").upper()
            # For Kraken, the symbol may be in PF_XBTUSD format — compare normalized
            if broker == "kraken":
                try:
                    from backend.execution.kraken_futures_executor import to_kraken_symbol
                    expected = to_kraken_symbol(symbol)
                    if pos_symbol != expected.upper() and pos_symbol != symbol.upper():
                        continue
                except ImportError:
                    if pos_symbol != symbol.upper():
                        continue
            elif pos_symbol != symbol.upper():
                continue
            
            pos_side = (pos.get("side") or pos.get("holdSide") or "").lower()
            pos_size = float(pos.get("size", 0) or pos.get("total", 0) or 0)
            
            # If hold_side specified, must match
            if hold_side and pos_side != hold_side.lower():
                continue
            
            if pos_size > 0:
                return {
                    "symbol": symbol,
                    "side": pos_side,
                    "size": pos_size,
                    "available": float(pos.get("available", pos_size) or pos_size),
                    "entryPrice": float(
                        pos.get("entryPrice", 0) or pos.get("openPriceAvg", 0)
                        or pos.get("entry_price", 0) or 0
                    ),
                    "markPrice": float(
                        pos.get("markPrice", 0) or pos.get("mark_price", 0) or 0
                    ),
                    "unrealizedPnl": float(
                        pos.get("unrealizedPnl", 0) or pos.get("unrealizedPL", 0) or 0
                    ),
                    "leverage": int(pos.get("leverage", 1) or 1),
                    "broker": broker,
                    "raw": pos,
                }
        
        return None
        
    except Exception as e:
        logger.error(f"[CLOSE] Error getting position: {e}", exc_info=True)
        return None


def _normalize_qty(
    symbol: str,
    raw_qty: float,
    position_size: float,
    entry_price: float,
) -> Tuple[Optional[float], str, Dict[str, Any]]:
    """
    Normalize quantity to meet contract specifications.
    
    Returns:
        (normalized_qty, status, details)
        normalized_qty is None if rejected
    """
    specs = get_contract_specs(symbol)
    
    details = {
        "raw_qty": raw_qty,
        "position_size": position_size,
        "min_qty": specs.min_qty,
        "qty_step": specs.qty_step,
        "qty_scale": specs.qty_scale,
    }
    
    # Round DOWN to qty_step
    d_qty = Decimal(str(raw_qty))
    d_step = Decimal(str(specs.qty_step))
    rounded_qty = float((d_qty / d_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * d_step)
    
    details["rounded_qty"] = rounded_qty
    
    # Check minimum
    if rounded_qty < specs.min_qty:
        # If remaining would be less than min_qty, close everything instead
        remaining = position_size - rounded_qty
        
        if remaining < specs.min_qty and remaining > 0:
            # Remaining too small to keep - close entire position
            rounded_qty = position_size
            details["converted_to_full_close"] = True
            logger.info(
                f"[CLOSE] QTY auto-converted to full close | "
                f"remaining {remaining:.6f} < min_qty {specs.min_qty}"
            )
        else:
            # Reject: qty too small
            details["rejection_reason"] = f"qty {rounded_qty:.6f} < min_qty {specs.min_qty}"
            return None, "REJECTED", details
    
    # Ensure not exceeding position
    if rounded_qty > position_size:
        rounded_qty = position_size
        details["clamped_to_position"] = True
    
    # Format with correct precision
    final_qty = float(f"{rounded_qty:.{specs.qty_scale}f}")
    details["final_qty"] = final_qty
    
    return final_qty, "OK", details


async def _execute_close_order(
    symbol: str,
    hold_side: str,
    qty: float,
    is_full_close: bool,
    broker: str = "bitget",
) -> Dict[str, Any]:
    """
    Execute the close order on the appropriate broker with reduceOnly.
    
    Returns:
        {"success": bool, "order_id": str, "raw_response": dict, "error": str}
    """
    # Determine order side (opposite of position)
    order_side = "sell" if hold_side.lower() == "long" else "buy"

    # ── KRAKEN PATH ──────────────────────────────────────────────────
    if broker == "kraken" and _kraken_executor is not None:
        logger.info(
            "[CLOSE] KRAKEN_PATH | symbol=%s | side=%s | qty=%s | full=%s",
            symbol, order_side, qty, is_full_close,
        )
        try:
            if is_full_close:
                result = await _kraken_executor.close_position(
                    symbol=symbol,
                    quantity=None,  # Full close
                    reduce_only=True,
                    reason="close_service",
                )
            else:
                result = await _kraken_executor.close_position(
                    symbol=symbol,
                    quantity=qty,
                    reduce_only=True,
                    reason="close_service_partial",
                )

            # Normalize Kraken result to match expected format
            success = (
                result.get("code") == "00000"
                or result.get("success") is True
                or result.get("code") == "NO_POSITION"  # Idempotent
            )
            order_id = result.get("order_id") or ""
            if not order_id and isinstance(result.get("data"), dict):
                order_id = result["data"].get("orderId", "")

            return {
                "success": success,
                "order_id": order_id or f"kraken_close_{symbol}",
                "raw_response": result,
                "error": None if success else result.get("msg", "Kraken close failed"),
            }

        except Exception as e:
            logger.error("[CLOSE] Kraken execution error: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    # ── BITGET PATH (original logic) ─────────────────────────────────
    # Get contract specs for formatting
    specs = get_contract_specs(symbol)
    formatted_qty = f"{qty:.{specs.qty_scale}f}"
    
    # Get product type from executor
    product_type = getattr(_executor, 'product_type', 'USDT-FUTURES')
    
    # Build payload
    payload = {
        "symbol": symbol,
        "productType": product_type,
        "marginCoin": "USDT",
        "marginMode": "isolated",
        "side": order_side,
        "orderType": "market",
        "size": formatted_qty,
        "tradeSide": "close",  # CRITICAL: This enforces reduce_only on Bitget
    }
    
    logger.debug(f"[CLOSE] Sending Bitget order payload: {payload}")
    
    try:
        # Use executor's HTTP POST method
        result = await _executor._http_post(
            "/api/v2/mix/order/place-order",
            payload,
            f"CLOSE_{symbol}"
        )
        
        if result.get("code") != "00000":
            return {
                "success": False,
                "error": result.get("msg", "Unknown API error"),
                "raw_response": result,
            }
        
        order_id = result.get("data", {}).get("orderId", "")
        
        return {
            "success": True,
            "order_id": order_id,
            "raw_response": result,
        }
        
    except Exception as e:
        logger.error(f"[CLOSE] Order execution error: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
        }


async def _wait_for_confirmation(
    symbol: str,
    expected_qty_reduction: float,
    original_size: float,
    timeout_sec: float,
    broker: str = "bitget",
    hold_side: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Poll for position confirmation after order.
    Uses intelligent backoff: 200ms → 400ms → 800ms...
    
    Returns:
        (confirmed, data)
        confirmed = True if position reduced as expected
        data = {"remaining_qty": ..., "close_price": ...}
    """
    start_time = asyncio.get_event_loop().time()
    poll_interval = 0.2  # Start at 200ms
    max_interval = 2.0   # Max 2 seconds between polls
    
    while True:
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed >= timeout_sec:
            # Final check
            position = await _get_position(symbol, hold_side=hold_side, broker=broker)
            remaining = position["size"] if position else 0.0
            close_price = position.get("markPrice", 0.0) if position else 0.0
            return False, {"remaining_qty": remaining, "close_price": close_price}
        
        # Check position
        position = await _get_position(symbol, hold_side=hold_side, broker=broker)
        
        if position is None:
            # Position closed completely
            return True, {"remaining_qty": 0.0, "close_price": 0.0}
        
        current_size = position["size"]
        expected_remaining = original_size - expected_qty_reduction
        
        # Check if reduction happened (with small tolerance for rounding)
        if current_size <= expected_remaining + 0.0001:
            close_price = position.get("markPrice", 0.0)
            return True, {"remaining_qty": current_size, "close_price": close_price}
        
        # Wait with backoff
        await asyncio.sleep(poll_interval)
        poll_interval = min(poll_interval * 2, max_interval)


async def _update_database(
    symbol: str,
    order_id: str,
    close_price: float,
    qty_closed: float,
    pnl: float,
    fee: float,
    is_full_close: bool,
    reason: str,
    percent: float,
    raw_response: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Update trade in database.
    
    Returns:
        trade_id if updated successfully, None otherwise
    """
    try:
        from backend.services.live_trades_service import LiveTradesService
        from backend.services.trade_persistence_hook import (
            on_trade_closed,
            on_trade_reduced,
        )
        
        # Find the active trade
        trade = LiveTradesService.get_open_trade_for_symbol(symbol)
        
        if not trade:
            logger.warning(f"[CLOSE] No open trade found in DB for {symbol}")
            return None
        
        if is_full_close:
            await on_trade_closed(
                trade_id=trade.id,
                order_id=order_id,
                close_price=close_price,
                pnl=pnl,
                fee=fee,
                qty=qty_closed,
                raw_exchange=raw_response,
            )
        else:
            await on_trade_reduced(
                trade_id=trade.id,
                qty=qty_closed,
                price=close_price,
                pnl=pnl,
                fee=fee,
                reduce_pct=percent,
                order_id=order_id,
                raw_exchange=raw_response,
            )
        
        return trade.id
        
    except ImportError:
        logger.warning("[CLOSE] Persistence modules not available")
        return None
    except Exception as e:
        logger.error(f"[CLOSE] Database update failed: {e}", exc_info=True)
        return None


async def _broadcast_close_event(
    symbol: str,
    side: str,
    qty_closed: float,
    remaining_qty: float,
    order_id: str,
    close_price: float,
    pnl: float,
    reason: str,
    is_full_close: bool,
) -> None:
    """Broadcast close/reduce event to WebSocket clients."""
    global _ws_manager
    
    if _ws_manager is None:
        logger.debug("[CLOSE] No WS manager, skipping broadcast")
        return
    
    try:
        event_type = "position_closed" if is_full_close else "position_reduced"
        
        message = {
            "type": event_type,
            "data": {
                "symbol": symbol,
                "side": side,
                "qty_closed": qty_closed,
                "remaining_qty": remaining_qty,
                "order_id": order_id,
                "close_price": close_price,
                "pnl": round(pnl, 2),
                "reason": reason,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        if hasattr(_ws_manager, 'broadcast'):
            await _ws_manager.broadcast(message)
        
    except Exception as e:
        logger.error(f"[CLOSE] WS broadcast failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

async def close_position_full(
    symbol: str,
    reason: str = "manual_close",
    hold_side: Optional[str] = None,
    timeout_sec: float = 10.0,
) -> CloseResult:
    """Convenience: Close 100% of position."""
    return await close_position(
        symbol=symbol,
        percent=100.0,
        reason=reason,
        hold_side=hold_side,
        timeout_sec=timeout_sec,
    )


async def reduce_position(
    symbol: str,
    percent: float,
    reason: str = "partial_close",
    hold_side: Optional[str] = None,
    timeout_sec: float = 10.0,
) -> CloseResult:
    """Convenience: Reduce position by percent (1-99)."""
    percent = max(1.0, min(99.0, percent))
    return await close_position(
        symbol=symbol,
        percent=percent,
        reason=reason,
        hold_side=hold_side,
        timeout_sec=timeout_sec,
    )


def get_active_locks() -> Dict[str, str]:
    """Get all symbols currently being closed."""
    return dict(_lock_manager._active_closes)


def is_close_in_progress(symbol: str) -> bool:
    """Check if a close operation is in progress for this symbol."""
    return _lock_manager.is_closing(symbol.upper())
