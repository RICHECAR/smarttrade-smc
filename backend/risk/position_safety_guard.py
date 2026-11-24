"""
PositionSafetyGuard — Unified Position Protection Service
══════════════════════════════════════════════════════════

Runs on every cycle and ensures:
  1. Every open position has SL + TP attached (re-creates if missing)
  2. If SL/TP creation fails after retries → emergency close
  3. Drawdown monitoring: if adverse move > threshold → partial/full close
  4. Trailing stop support (configurable)
  5. Multi-broker aware (Bitget + Kraken)

Config via .env:
  RISK_GUARD_ENABLED=true
  RISK_MAX_ADVERSE_MOVE_PCT=1.0
  RISK_REDUCE_STEP_PCT=25
  RISK_RETRY_COUNT=3
  RISK_TRAILING_ENABLED=false
  RISK_TRAILING_ACTIVATE_AT_PCT=0.8
  RISK_TRAILING_DISTANCE_PCT=0.4
  RISK_SL_PCT=0.6
  RISK_TP_PCT=1.2
  RISK_USE_ATR=false
  RISK_ATR_PERIOD=14
  RISK_ATR_MULT_SL=1.5
  RISK_ATR_MULT_TP=2.5

Author: SMC Trading Bot Framework
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("smarttrade.risk.safety_guard")


# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default

def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class GuardConfig:
    """Safety guard configuration — all from .env."""
    enabled: bool = field(default_factory=lambda: _env_bool("RISK_GUARD_ENABLED", True))
    interval_sec: int = field(default_factory=lambda: _env_int("RISK_GUARD_INTERVAL_SEC", 30))
    retry_count: int = field(default_factory=lambda: _env_int("RISK_RETRY_COUNT", 3))
    max_adverse_move_pct: float = field(default_factory=lambda: _env_float("RISK_MAX_ADVERSE_MOVE_PCT", 1.0))
    reduce_step_pct: float = field(default_factory=lambda: _env_float("RISK_REDUCE_STEP_PCT", 25.0))
    sl_pct: float = field(default_factory=lambda: _env_float("RISK_SL_PCT", 0.6))
    tp_pct: float = field(default_factory=lambda: _env_float("RISK_TP_PCT", 1.2))
    use_atr: bool = field(default_factory=lambda: _env_bool("RISK_USE_ATR", False))
    atr_period: int = field(default_factory=lambda: _env_int("RISK_ATR_PERIOD", 14))
    atr_mult_sl: float = field(default_factory=lambda: _env_float("RISK_ATR_MULT_SL", 1.5))
    atr_mult_tp: float = field(default_factory=lambda: _env_float("RISK_ATR_MULT_TP", 2.5))
    trailing_enabled: bool = field(default_factory=lambda: _env_bool("RISK_TRAILING_ENABLED", False))
    trailing_activate_at_pct: float = field(default_factory=lambda: _env_float("RISK_TRAILING_ACTIVATE_AT_PCT", 0.8))
    trailing_distance_pct: float = field(default_factory=lambda: _env_float("RISK_TRAILING_DISTANCE_PCT", 0.4))
    emergency_close_on_sl_fail: bool = field(default_factory=lambda: _env_bool("RISK_EMERGENCY_CLOSE_ON_SL_FAIL", True))
    protection_repair_cooldown_sec: float = field(default_factory=lambda: _env_float("RISK_PROTECTION_REPAIR_COOLDOWN_SEC", 15.0))
    max_protection_failures: int = field(default_factory=lambda: _env_int("RISK_MAX_PROTECTION_FAILURES", 5))
    force_close_if_unprotected: bool = field(default_factory=lambda: _env_bool("RISK_FORCE_CLOSE_IF_UNPROTECTED", True))


# ═══════════════════════════════════════════════════════════════
#  SAFETY GUARD
# ═══════════════════════════════════════════════════════════════

class PositionSafetyGuard:
    """
    Monitors all open positions and ensures risk protection.
    
    Usage:
        guard = PositionSafetyGuard(executors={"bitget": bitget_exec, "kraken": kraken_exec})
        guard.start()  # Background loop
        # or
        await guard.check_once()  # Single check
    """
    
    def __init__(self, executors: Dict[str, Any], config: Optional[GuardConfig] = None):
        self.executors = executors  # {"bitget": exec, "kraken": exec}
        self.config = config or GuardConfig()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._trailing_highs: Dict[str, float] = {}  # Track highest price for trailing
        # Per-position asyncio locks to prevent concurrent SL/TP creation
        self._pos_locks: Dict[str, asyncio.Lock] = {}
        # Cooldown: pos_key -> last repair attempt timestamp
        self._last_repair_attempt: Dict[str, float] = {}
        # Failure counter per position (escalation after max failures)
        self._protection_failures: Dict[str, int] = {}
        self._stats = {
            "checks": 0,
            "sl_created": 0,
            "tp_created": 0,
            "partial_closes": 0,
            "full_closes": 0,
            "emergency_closes": 0,
            "trailing_updates": 0,
            "errors": 0,
            "cooldown_skips": 0,
            "dedup_skips": 0,
        }
        logger.info(
            "[SAFETY_GUARD] Init | enabled=%s | interval=%ds | max_adverse=%.1f%% | "
            "reduce_step=%.0f%% | trailing=%s | sl=%.1f%% tp=%.1f%%",
            self.config.enabled, self.config.interval_sec,
            self.config.max_adverse_move_pct, self.config.reduce_step_pct,
            self.config.trailing_enabled, self.config.sl_pct, self.config.tp_pct,
        )
    
    def start(self):
        """Start the background guard loop."""
        if not self.config.enabled:
            logger.info("[SAFETY_GUARD] Disabled via RISK_GUARD_ENABLED=false")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("[SAFETY_GUARD] Background loop started")
    
    def stop(self):
        """Stop the guard."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[SAFETY_GUARD] Stopped")
    
    async def _loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                await self.check_once()
            except Exception as e:
                self._stats["errors"] += 1
                logger.error("[SAFETY_GUARD] Loop error: %s", e, exc_info=True)
            await asyncio.sleep(self.config.interval_sec)
    
    async def check_once(self):
        """Single pass: check all positions across all brokers."""
        self._stats["checks"] += 1
        for broker_name, executor in self.executors.items():
            try:
                positions = await self._get_positions(executor, broker_name)
                for pos in positions:
                    await self._evaluate_position(pos, executor, broker_name)
            except Exception as e:
                self._stats["errors"] += 1
                logger.error("[SAFETY_GUARD] %s check error: %s", broker_name, e, exc_info=True)
    
    async def _get_positions(self, executor, broker: str) -> List[Dict]:
        """Get open positions from executor."""
        if hasattr(executor, 'get_positions'):
            result = await executor.get_positions()
            if isinstance(result, dict):
                # Handle Bitget format
                positions = result.get("positions", result.get("data", []))
                return positions if isinstance(positions, list) else []
            elif isinstance(result, list):
                return result
        return []
    
    async def _evaluate_position(self, pos: Dict, executor, broker: str):
        """Evaluate a single position for safety compliance."""
        symbol = pos.get("symbol", "")
        hold_side = (pos.get("holdSide", pos.get("side", "long"))).lower()
        size = float(pos.get("total", pos.get("size", pos.get("available", 0))))
        entry_price = float(pos.get("entryPrice", pos.get("openPriceAvg", pos.get("entry_price", 0))))
        mark_price = float(pos.get("markPrice", pos.get("mark_price", 0)))
        leverage = int(pos.get("leverage", 1))
        
        if size <= 0 or entry_price <= 0:
            return
        
        pos_key = f"{broker}:{symbol}:{hold_side}"
        
        # ── 1. Check SL/TP existence ──
        await self._ensure_sl_tp(pos, executor, broker, symbol, hold_side, size, entry_price, leverage)
        
        # ── 2. Drawdown monitoring ──
        if mark_price > 0:
            await self._check_drawdown(pos_key, symbol, hold_side, size, entry_price, mark_price, leverage, executor, broker)
        
        # ── 3. Trailing stop ──
        if self.config.trailing_enabled and mark_price > 0:
            await self._check_trailing(pos_key, symbol, hold_side, size, entry_price, mark_price, leverage, executor, broker)
    
    async def _ensure_sl_tp(self, pos: Dict, executor, broker: str,
                            symbol: str, hold_side: str, size: float,
                            entry_price: float, leverage: int):
        """Verify SL/TP exist, create if missing. Per-position lock + cooldown."""
        pos_key = f"{broker}:{symbol}:{hold_side}"

        # ── Per-position lock (prevent concurrent SL/TP creation) ──
        if pos_key not in self._pos_locks:
            self._pos_locks[pos_key] = asyncio.Lock()
        lock = self._pos_locks[pos_key]

        if lock.locked():
            return  # Another coroutine is already working on this position

        async with lock:
            # ── Cooldown check ──
            now = time.monotonic()
            last_attempt = self._last_repair_attempt.get(pos_key, 0)
            if now - last_attempt < self.config.protection_repair_cooldown_sec:
                self._stats["cooldown_skips"] += 1
                return

            # ── Failure escalation ──
            failures = self._protection_failures.get(pos_key, 0)
            if failures >= self.config.max_protection_failures:
                if self.config.force_close_if_unprotected:
                    logger.critical(
                        "[SAFETY_GUARD] MAX_FAILURES_REACHED | %s | failures=%d | EMERGENCY CLOSE",
                        pos_key, failures,
                    )
                    await self._emergency_close(executor, broker, symbol, hold_side, size)
                    self._stats["emergency_closes"] += 1
                    self._protection_failures[pos_key] = 0
                return

            has_sl = False
            has_tp = False

            # Try to check plan orders (Bitget-specific)
            if broker == "bitget" and hasattr(executor, 'get_plan_orders'):
                try:
                    plan_res = await executor.get_plan_orders(symbol)
                    plans = plan_res.get("data", []) if isinstance(plan_res, dict) else []
                    for plan in plans:
                        p_hold = plan.get("holdSide", "").lower()
                        p_type = plan.get("planType", "")
                        if p_hold == hold_side:
                            if p_type == "loss_plan":
                                has_sl = True
                            elif p_type == "profit_plan":
                                has_tp = True
                except Exception as e:
                    logger.warning("[SAFETY_GUARD] Failed to check plan orders: %s", e)
                    return  # Can't verify, skip
            elif broker == "kraken":
                # Check Kraken open orders for existing stop/TP
                has_sl, has_tp = await self._check_kraken_protection_exists(
                    executor, symbol, hold_side
                )
                # Also check internal tracking
                if self._trailing_highs.get(f"protected:{pos_key}"):
                    if has_sl or has_tp:
                        return  # Already marked + verified
            else:
                # Unknown broker, check internal tracking
                if self._trailing_highs.get(f"protected:{pos_key}"):
                    return

            if has_sl and has_tp:
                # Mark as protected (FIX: this was previously never written)
                self._trailing_highs[f"protected:{pos_key}"] = 1.0
                self._protection_failures.pop(pos_key, None)
                return  # All good

            self._last_repair_attempt[pos_key] = now

            # ── Re-create missing SL ──
            if not has_sl:
                sl_price = self._compute_emergency_sl(entry_price, hold_side, leverage)
                logger.warning("[SAFETY_GUARD] SL_MISSING | %s@%s %s | Creating emergency SL at %.4f",
                              symbol, broker, hold_side, sl_price)

                success = await self._attach_with_retry(
                    executor, broker, symbol, "loss_plan" if broker == "bitget" else "stop_loss",
                    sl_price, hold_side, size,
                )
                if success:
                    self._stats["sl_created"] += 1
                    logger.info("[SAFETY_GUARD] SL_CREATED | %s@%s at %.4f", symbol, broker, sl_price)
                else:
                    self._protection_failures[pos_key] = failures + 1
                    if self.config.emergency_close_on_sl_fail:
                        logger.critical("[SAFETY_GUARD] SL_FAIL_EMERGENCY_CLOSE | %s@%s | Closing position!", symbol, broker)
                        await self._emergency_close(executor, broker, symbol, hold_side, size)
                        self._stats["emergency_closes"] += 1
                        return

            # ── Re-create missing TP ──
            if not has_tp:
                tp_price = self._compute_emergency_tp(entry_price, hold_side, leverage)
                logger.info("[SAFETY_GUARD] TP_MISSING | %s@%s %s | Creating TP at %.4f",
                           symbol, broker, hold_side, tp_price)

                success = await self._attach_with_retry(
                    executor, broker, symbol, "profit_plan" if broker == "bitget" else "take_profit",
                    tp_price, hold_side, size,
                )
                if success:
                    self._stats["tp_created"] += 1
                else:
                    self._protection_failures[pos_key] = failures + 1

            # If both now created, mark as protected
            if (has_sl or not has_sl) and (has_tp or not has_tp):
                # Re-check after repair
                pass  # Will be verified on next cycle

    async def _check_kraken_protection_exists(
        self, executor, symbol: str, hold_side: str,
    ) -> tuple:
        """
        Check Kraken open orders for existing SL/TP on this position.
        Returns (has_sl, has_tp).
        """
        has_sl = False
        has_tp = False
        try:
            client = getattr(executor, '_rest_client', None)
            if not client and hasattr(executor, '_get_rest_client'):
                client = executor._get_rest_client()
            if client and hasattr(client, 'get_open_orders'):
                orders = await client.get_open_orders()
                if isinstance(orders, dict):
                    orders = orders.get("openOrders", orders.get("orders", []))
                if isinstance(orders, list):
                    for order in orders:
                        o_sym = (order.get("symbol", "") or "").upper()
                        o_type = (order.get("orderType", "") or "").lower()
                        if o_sym != symbol.upper():
                            continue
                        if o_type in ("stp", "stop", "stop_loss"):
                            has_sl = True
                        elif o_type in ("take_profit", "tp"):
                            has_tp = True
        except Exception as e:
            logger.warning("[SAFETY_GUARD] Kraken open orders check failed: %s", e)
        return has_sl, has_tp

    def _compute_emergency_sl(self, entry_price: float, hold_side: str, leverage: int) -> float:
        """Compute emergency SL price based on config."""
        sl_dist = (self.config.sl_pct / 100.0) / max(leverage, 1)
        if hold_side == "long":
            return round(entry_price * (1 - sl_dist), 6)
        else:
            return round(entry_price * (1 + sl_dist), 6)
    
    def _compute_emergency_tp(self, entry_price: float, hold_side: str, leverage: int) -> float:
        """Compute emergency TP price based on config."""
        tp_dist = (self.config.tp_pct / 100.0) / max(leverage, 1)
        if hold_side == "long":
            return round(entry_price * (1 + tp_dist), 6)
        else:
            return round(entry_price * (1 - tp_dist), 6)
    
    async def _attach_with_retry(self, executor, broker: str, symbol: str,
                                  plan_type: str, price: float, hold_side: str,
                                  size: float) -> bool:
        """Attempt to attach SL/TP with retries."""
        for attempt in range(1, self.config.retry_count + 1):
            try:
                if broker == "bitget" and hasattr(executor, 'attach_tpsl'):
                    result = await executor.attach_tpsl(symbol, plan_type, str(price), hold_side)
                    if result.get("code") == "00000":
                        return True
                    # Already exists is OK
                    if "already exists" in result.get("msg", "").lower():
                        return True
                    logger.warning("[SAFETY_GUARD] attach retry %d/%d: %s", attempt, self.config.retry_count, result.get("msg"))
                
                elif broker == "kraken":
                    # Kraken uses separate stop/TP orders via REST
                    # CRITICAL: Method names must match KrakenFuturesRESTClientV2:
                    #   place_stop_loss() (NOT place_stop_order)
                    #   place_take_profit() (NOT place_take_profit_order)
                    client = getattr(executor, '_rest_client', None) or getattr(executor, 'client', None)
                    if not client and hasattr(executor, '_get_rest_client'):
                        client = executor._get_rest_client()
                    if client:
                        opposite_side = "sell" if hold_side == "long" else "buy"
                        if "loss" in plan_type or "stop" in plan_type:
                            result = await client.place_stop_loss(
                                symbol=symbol, side=opposite_side,
                                size=size, stop_price=price, reduce_only=True,
                            )
                        else:
                            result = await client.place_take_profit(
                                symbol=symbol, side=opposite_side,
                                size=size, price=price, reduce_only=True,
                            )
                        if hasattr(result, 'success') and result.success:
                            return True
                        if isinstance(result, dict) and (
                            result.get("result") == "success" or result.get("success") is True
                        ):
                            return True
                        logger.warning("[SAFETY_GUARD] Kraken attach retry %d/%d", attempt, self.config.retry_count)
                
            except Exception as e:
                logger.error("[SAFETY_GUARD] attach retry %d/%d error: %s", attempt, self.config.retry_count, e)
            
            if attempt < self.config.retry_count:
                await asyncio.sleep(1.5 * attempt)
        
        logger.error("[SAFETY_GUARD] ATTACH_FAILED | %s@%s | %s at %.4f after %d retries",
                    symbol, broker, plan_type, price, self.config.retry_count)
        return False
    
    async def _check_drawdown(self, pos_key: str, symbol: str, hold_side: str,
                               size: float, entry_price: float, mark_price: float,
                               leverage: int, executor, broker: str):
        """Check if position drawdown exceeds threshold → partial close."""
        if hold_side == "long":
            pnl_pct = ((mark_price - entry_price) / entry_price) * 100 * leverage
        else:
            pnl_pct = ((entry_price - mark_price) / entry_price) * 100 * leverage
        
        if pnl_pct < -self.config.max_adverse_move_pct:
            reduce_qty = size * (self.config.reduce_step_pct / 100.0)
            logger.warning(
                "[SAFETY_GUARD] DRAWDOWN | %s@%s | pnl=%.2f%% (threshold=%.1f%%) | "
                "Reducing by %.0f%% (qty=%.6f)",
                symbol, broker, pnl_pct, self.config.max_adverse_move_pct,
                self.config.reduce_step_pct, reduce_qty,
            )
            
            if reduce_qty > 0:
                await self._partial_close(executor, broker, symbol, hold_side, reduce_qty)
                self._stats["partial_closes"] += 1
    
    async def _check_trailing(self, pos_key: str, symbol: str, hold_side: str,
                               size: float, entry_price: float, mark_price: float,
                               leverage: int, executor, broker: str):
        """Trailing stop logic: move SL up as price moves in our favor."""
        if hold_side == "long":
            pnl_pct = ((mark_price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - mark_price) / entry_price) * 100
        
        # Only activate trailing after reaching activation threshold
        if pnl_pct < self.config.trailing_activate_at_pct:
            return
        
        # Track highest favorable price
        best_key = f"best:{pos_key}"
        best = self._trailing_highs.get(best_key, mark_price if hold_side == "long" else mark_price)
        
        if hold_side == "long":
            if mark_price > best:
                self._trailing_highs[best_key] = mark_price
                best = mark_price
            
            # Trailing SL = best_price * (1 - trailing_distance_pct)
            trailing_sl = best * (1 - self.config.trailing_distance_pct / 100.0)
            
            if mark_price <= trailing_sl:
                logger.warning("[SAFETY_GUARD] TRAILING_STOP_HIT | %s@%s | mark=%.4f < trail_sl=%.4f",
                              symbol, broker, mark_price, trailing_sl)
                await self._emergency_close(executor, broker, symbol, hold_side, size)
                self._stats["full_closes"] += 1
                return
        else:
            if mark_price < best:
                self._trailing_highs[best_key] = mark_price
                best = mark_price
            
            trailing_sl = best * (1 + self.config.trailing_distance_pct / 100.0)
            
            if mark_price >= trailing_sl:
                logger.warning("[SAFETY_GUARD] TRAILING_STOP_HIT | %s@%s | mark=%.4f > trail_sl=%.4f",
                              symbol, broker, mark_price, trailing_sl)
                await self._emergency_close(executor, broker, symbol, hold_side, size)
                self._stats["full_closes"] += 1
                return
    
    async def _partial_close(self, executor, broker: str, symbol: str,
                              hold_side: str, qty: float):
        """Reduce position by qty (reduce-only market order)."""
        close_side = "sell" if hold_side == "long" else "buy"
        try:
            if hasattr(executor, 'close_position_market'):
                result = await executor.close_position_market(symbol, hold_side, str(qty))
            elif hasattr(executor, 'place_order_market'):
                result = await executor.place_order_market(symbol, close_side, str(qty), reduce_only=True)
            elif hasattr(executor, 'place_order'):
                result = await executor.place_order(
                    symbol=symbol, side=close_side, quantity=qty,
                    order_type="market", reduce_only=True,
                )
            else:
                logger.error("[SAFETY_GUARD] No close method on %s executor", broker)
                return
            
            code = result.get("code", "") if isinstance(result, dict) else ""
            if code == "00000" or (isinstance(result, dict) and result.get("success")):
                logger.info("[SAFETY_GUARD] PARTIAL_CLOSE_OK | %s@%s | qty=%.6f", symbol, broker, qty)
            else:
                logger.error("[SAFETY_GUARD] PARTIAL_CLOSE_FAIL | %s@%s | %s", symbol, broker, result)
        except Exception as e:
            logger.error("[SAFETY_GUARD] PARTIAL_CLOSE_ERROR | %s@%s | %s", symbol, broker, e)
    
    async def _emergency_close(self, executor, broker: str, symbol: str,
                                hold_side: str, size: float):
        """Full emergency close of a position."""
        logger.critical("[SAFETY_GUARD] EMERGENCY_CLOSE | %s@%s %s | size=%.6f",
                       symbol, broker, hold_side, size)
        await self._partial_close(executor, broker, symbol, hold_side, size)
    
    def get_status(self) -> Dict[str, Any]:
        """Return guard status for monitoring API."""
        return {
            "enabled": self.config.enabled,
            "running": self._running,
            "config": {
                "interval_sec": self.config.interval_sec,
                "max_adverse_move_pct": self.config.max_adverse_move_pct,
                "reduce_step_pct": self.config.reduce_step_pct,
                "trailing_enabled": self.config.trailing_enabled,
                "sl_pct": self.config.sl_pct,
                "tp_pct": self.config.tp_pct,
                "protection_repair_cooldown_sec": self.config.protection_repair_cooldown_sec,
                "max_protection_failures": self.config.max_protection_failures,
                "force_close_if_unprotected": self.config.force_close_if_unprotected,
            },
            "stats": self._stats,
            "protection_failures": dict(self._protection_failures),
        }
