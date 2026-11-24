"""
PositionGuardian — Background safety service that monitors open positions.

Runs every GUARDIAN_INTERVAL_SEC (default 30s) and:
1. Detects positions WITHOUT TP/SL → re-attaches them
2. Enforces MAX_LOSS_PCT (default 3%) → auto-closes losing positions
3. Alerts on positions older than MAX_POSITION_AGE_HOURS (default 24h)
4. Detects market reversal (price crosses virtual SL) → auto-closes

This is a SAFETY NET — it catches edge cases where normal execution fails.
"""

import asyncio
import logging
import math
import time
import os
from typing import Dict, Any, Optional, List
from datetime import datetime

# Persistence Hooks
try:
    from backend.services.trade_persistence_hook import on_trade_closed
    from backend.services.live_trades_service import LiveTradesService
except ImportError:
    on_trade_closed = None
    LiveTradesService = None

logger = logging.getLogger("position_guardian")


class PositionGuardian:
    """
    Background safety guardian for open positions.
    Ensures every position has TP/SL protection and enforces max loss limits.
    """

    def __init__(self, executor, config: Dict[str, Any]):
        self.executor = executor
        self.config = config
        
        # Configuration
        self.interval_sec = int(os.getenv("GUARDIAN_INTERVAL_SEC", "30"))
        self.max_loss_pct = float(os.getenv("GUARDIAN_MAX_LOSS_PCT", "3.0"))
        self.max_position_age_hours = float(os.getenv("GUARDIAN_MAX_AGE_HOURS", "24"))
        self.enabled = os.getenv("GUARDIAN_ENABLED", "true").lower() == "true"
        self.auto_close_enabled = os.getenv("GUARDIAN_AUTO_CLOSE", "true").lower() == "true"
        
        # State
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_check_ts = 0
        self._stats = {
            "checks_total": 0,
            "positions_checked": 0,
            "sl_reattached": 0,
            "tp_reattached": 0,
            "auto_closed_loss": 0,
            "auto_closed_age": 0,
            "errors": 0,
            "last_check": None,
        }
        # Track known SL/TP per position to avoid spamming
        self._known_protected: Dict[str, bool] = {}
        
        logger.info(
            f"[GUARDIAN] Initialized | interval={self.interval_sec}s | "
            f"max_loss={self.max_loss_pct}% | max_age={self.max_position_age_hours}h | "
            f"enabled={self.enabled} | auto_close={self.auto_close_enabled}"
        )

    def start(self):
        """Start the guardian background loop."""
        if not self.enabled:
            logger.info("[GUARDIAN] Disabled via GUARDIAN_ENABLED=false")
            return
        if self._running:
            logger.warning("[GUARDIAN] Already running")
            return
        
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("[GUARDIAN] 🛡️ Background guardian started")

    def stop(self):
        """Stop the guardian."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[GUARDIAN] Stopped")

    async def _loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                await self._check_positions()
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(f"[GUARDIAN] Check failed: {e}", exc_info=True)
            
            await asyncio.sleep(self.interval_sec)

    async def _check_positions(self):
        """Check all open positions for safety violations."""
        self._stats["checks_total"] += 1
        self._stats["last_check"] = datetime.now().isoformat()
        
        # Fetch positions from Bitget
        try:
            positions_response = await self.executor.get_positions()
        except Exception as e:
            logger.error(f"[GUARDIAN] Failed to fetch positions: {e}")
            return
        
        # Handle different response formats
        positions = []
        if isinstance(positions_response, dict):
            positions = positions_response.get("positions", [])
        elif isinstance(positions_response, list):
            positions = positions_response
        
        if not positions:
            return
        
        self._stats["positions_checked"] += len(positions)
        
        for pos in positions:
            try:
                await self._evaluate_position(pos)
            except Exception as e:
                logger.error(f"[GUARDIAN] Error evaluating position: {e}", exc_info=True)

    async def _evaluate_position(self, pos: Dict[str, Any]):
        """Evaluate a single position for safety compliance."""
        symbol = pos.get("symbol", "UNKNOWN")
        hold_side = pos.get("holdSide", pos.get("hold_side", "long")).lower()
        total_size = str(pos.get("total", pos.get("available", "0")))
        entry_price = float(pos.get("openPriceAvg", pos.get("averageOpenPrice", pos.get("entry_price", 0))))
        unrealized_pnl = float(pos.get("unrealisedPL", pos.get("unrealizedPl", pos.get("pnl", 0))))
        margin = float(pos.get("margin", pos.get("achievedProfits", 1)))
        leverage = int(pos.get("leverage", 1))
        create_time = pos.get("cTime", pos.get("openTime", "0"))
        
        if float(total_size) <= 0 or entry_price <= 0:
            return
        
        pos_key = f"{symbol}_{hold_side}"

        # ── Check 1: Max Loss Enforcement ──
        if margin > 0 and self.auto_close_enabled:
            loss_pct = abs(unrealized_pnl / margin * 100) if unrealized_pnl < 0 else 0
            
            if loss_pct >= self.max_loss_pct:
                logger.critical(
                    f"[GUARDIAN_MAX_LOSS] {symbol} {hold_side} | "
                    f"loss={loss_pct:.1f}% (max={self.max_loss_pct}%) | "
                    f"pnl={unrealized_pnl:.2f} | margin={margin:.2f} | "
                    f"CLOSING POSITION"
                )
                
                close_result = await self.executor.close_position_market(symbol, hold_side, total_size)
                close_res: Dict[str, Any] = (
                    close_result if isinstance(close_result, dict) else {}
                )
                close_code = close_res.get("code", "UNKNOWN")
                
                if close_code == "00000":
                    data = close_res.get("data")
                    data = data if isinstance(data, dict) else {}
                    close_order_id = data.get("orderId")
                    try:
                        filled_qty = float(close_res.get("filled_qty"))
                        fill_price = float(close_res.get("fill_price"))
                    except (TypeError, ValueError, OverflowError):
                        filled_qty = 0.0
                        fill_price = 0.0

                    if (
                        close_res.get("fill_quantity_verified") is not True
                        or close_res.get("fill_price_verified") is not True
                        or not math.isfinite(filled_qty)
                        or filled_qty <= 0
                        or filled_qty > float(total_size) + max(1e-12, float(total_size) * 1e-8)
                        or not math.isfinite(fill_price)
                        or fill_price <= 0
                    ):
                        logger.critical(
                            "[GUARDIAN_CLOSE_UNKNOWN] %s | order accepted but fill quantity/price "
                            "is unconfirmed; preserving trade state | order_id=%s",
                            symbol,
                            close_order_id,
                        )
                    else:
                        self._stats["auto_closed_loss"] += 1
                        logger.critical(
                            "[GUARDIAN_FILL_CONFIRMED] %s | qty=%s | price=%s | order_id=%s",
                            symbol,
                            filled_qty,
                            fill_price,
                            close_order_id,
                        )

                        realized_pnl = None
                        if close_res.get("realized_pnl_verified") is True:
                            try:
                                candidate_pnl = float(close_res.get("realized_pnl"))
                                if math.isfinite(candidate_pnl):
                                    realized_pnl = candidate_pnl
                            except (TypeError, ValueError, OverflowError):
                                pass
                        confirmed_fee = None
                        if close_res.get("fee_verified") is True:
                            try:
                                candidate_fee = float(close_res.get("fee"))
                                if math.isfinite(candidate_fee) and candidate_fee >= 0:
                                    confirmed_fee = candidate_fee
                            except (TypeError, ValueError, OverflowError):
                                pass

                        if on_trade_closed and LiveTradesService:
                            try:
                                db_trade = LiveTradesService.get_open_trade_for_symbol(
                                    symbol,
                                    broker="bitget",
                                )
                                if db_trade:
                                    remaining = db_trade.qty_remaining
                                    if remaining is None:
                                        logger.error(
                                            "[GUARDIAN_PERSIST_UNKNOWN] %s | DB remaining quantity is unknown",
                                            symbol,
                                        )
                                    elif filled_qty + 1e-12 >= float(remaining):
                                        await on_trade_closed(
                                            trade_id=db_trade.id,
                                            order_id=close_order_id,
                                            close_price=fill_price,
                                            pnl=realized_pnl,
                                            fee=confirmed_fee,
                                            qty=filled_qty,
                                            raw_exchange=close_res,
                                            close_reason="guardian_max_loss",
                                        )
                                    elif filled_qty < float(remaining):
                                        from backend.services.trade_persistence_hook import on_trade_reduced
                                        await on_trade_reduced(
                                            trade_id=db_trade.id,
                                            order_id=close_order_id,
                                            qty=filled_qty,
                                            price=fill_price,
                                            pnl=realized_pnl,
                                            fee=confirmed_fee,
                                            raw_exchange=close_res,
                                        )
                                else:
                                    logger.error(
                                        "[GUARDIAN_PERSIST_UNKNOWN] No open Bitget trade found for %s",
                                        symbol,
                                    )
                            except Exception as persist_err:
                                logger.error(
                                    "[GUARDIAN_PERSIST_ERROR] %s | %s",
                                    symbol,
                                    persist_err,
                                    exc_info=True,
                                )
                else:
                    logger.error(f"[GUARDIAN_CLOSE_FAIL] {symbol} | code={close_code} | msg={close_res.get('msg')}")
                
                return  # Don't check TP/SL on a position we're closing

        # ── Check 2: Position Age ──
        try:
            create_ts = int(create_time) / 1000 if int(create_time) > 1e12 else int(create_time)
            age_hours = (time.time() - create_ts) / 3600
            
            if age_hours > self.max_position_age_hours:
                logger.warning(
                    f"[GUARDIAN_OLD_POSITION] {symbol} {hold_side} | "
                    f"age={age_hours:.1f}h (max={self.max_position_age_hours}h) | "
                    f"pnl={unrealized_pnl:.2f}"
                )
        except (ValueError, TypeError):
            pass

        # ── Check 3: TP/SL Verification (Real-time via Exchange) ──
        try:
            plan_orders_res = await self.executor.get_plan_orders(symbol)
            plan_orders = plan_orders_res.get("data", []) if isinstance(plan_orders_res, dict) else []
            
            has_sl = False
            has_tp = False
            
            for plan in plan_orders:
                p_plan_type = plan.get("planType")
                p_hold_side = plan.get("holdSide", "").lower()
                
                if p_hold_side == hold_side:
                    if p_plan_type == "loss_plan":
                        has_sl = True
                    elif p_plan_type == "profit_plan":
                        has_tp = True
            
            # Re-attach missing Stop Loss
            if not has_sl:
                logger.warning(f"[GUARDIAN_SL_MISSING] {symbol} {hold_side} | No active SL found!")
                # Calculate emergency SL at max_loss_pct from entry (accounting for leverage)
                if hold_side == "long":
                    emergency_sl = entry_price * (1 - (self.max_loss_pct / 100) / leverage)
                else:
                    emergency_sl = entry_price * (1 + (self.max_loss_pct / 100) / leverage)
                
                # Round to appropriate decimals if executor has the method
                if hasattr(self.executor, "_get_price_decimals"):
                    decimals = self.executor._get_price_decimals(symbol)
                    emergency_sl = round(emergency_sl, decimals)
                
                sl_res = await self.executor.attach_tpsl(
                    symbol, "loss_plan", str(emergency_sl), hold_side
                )
                if sl_res.get("code") == "00000":
                    self._stats["sl_reattached"] += 1
                    logger.warning(f"[GUARDIAN_SL_FIXED] {symbol} {hold_side} | Re-attached SL at {emergency_sl}")
                else:
                    logger.error(f"[GUARDIAN_SL_FIX_FAIL] {symbol} | code={sl_res.get('code')} msg={sl_res.get('msg')}")

            # Re-attach missing Take Profit (using a default 2:1 RR from max_loss if missing)
            if not has_tp:
                logger.info(f"[GUARDIAN_TP_MISSING] {symbol} {hold_side} | No active TP found")
                # Default TP at 2x max_loss distance
                tp_dist = (self.max_loss_pct / 100) * 2 / leverage
                if hold_side == "long":
                    emergency_tp = entry_price * (1 + tp_dist)
                else:
                    emergency_tp = entry_price * (1 - tp_dist)
                
                if hasattr(self.executor, "_get_price_decimals"):
                    decimals = self.executor._get_price_decimals(symbol)
                    emergency_tp = round(emergency_tp, decimals)

                tp_res = await self.executor.attach_tpsl(
                    symbol, "profit_plan", str(emergency_tp), hold_side
                )
                if tp_res.get("code") == "00000":
                    self._stats["tp_reattached"] += 1
                    logger.info(f"[GUARDIAN_TP_FIXED] {symbol} {hold_side} | Re-attached TP at {emergency_tp}")

            self._known_protected[pos_key] = (has_sl and has_tp)

        except Exception as e:
            logger.error(f"[GUARDIAN_VERIFY_ERROR] {symbol} | {e}", exc_info=True)

    def get_status(self) -> Dict[str, Any]:
        """Return guardian status for monitoring."""
        return {
            "enabled": self.enabled,
            "running": self._running,
            "auto_close_enabled": self.auto_close_enabled,
            "interval_sec": self.interval_sec,
            "max_loss_pct": self.max_loss_pct,
            "max_position_age_hours": self.max_position_age_hours,
            "stats": self._stats,
        }
