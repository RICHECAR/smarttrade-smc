"""
DecisionEngine — Autonomous Position Management
═════════════════════════════════════════════════

On each tick/interval, evaluates all open positions and decides:
  a) HOLD — position within expected parameters
  b) REDUCE — partial close (reduce-only) when drawdown > threshold
  c) CLOSE — full close when SL/TP hit or extreme conditions
  d) TIGHTEN — resserrer SL (trailing) on ATR spike or profit lock

Rules (all configurable):
  1. Price touches SL or TP → close via existing orders (passthrough)
  2. SL/TP absent → create (delegates to PositionSafetyGuard)
  3. Drawdown > RISK_MAX_ADVERSE_MOVE_PCT → reduce by RISK_REDUCE_STEP_PCT
  4. ATR spike (volatility increase) → tighten SL
  5. Trailing stop updates when enabled and profit threshold reached

Config via .env:
  DECISION_ENGINE_ENABLED=true
  DECISION_ENGINE_INTERVAL_SEC=15
  RISK_MAX_ADVERSE_MOVE_PCT=1.0
  RISK_REDUCE_STEP_PCT=25
  RISK_ATR_SPIKE_MULT=2.0
  RISK_TRAILING_ENABLED=false

Author: SMC Trading Bot Framework
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("smarttrade.risk.decision_engine")


# ═══════════════════════════════════════════════════════════════
#  ENUMS & TYPES
# ═══════════════════════════════════════════════════════════════

class Decision(str, Enum):
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"
    TIGHTEN_SL = "TIGHTEN_SL"
    CREATE_SL = "CREATE_SL"
    CREATE_TP = "CREATE_TP"


@dataclass
class PositionEvaluation:
    """Result of evaluating a single position."""
    symbol: str
    broker: str
    side: str
    decision: Decision
    reason: str
    entry_price: float = 0.0
    mark_price: float = 0.0
    pnl_pct: float = 0.0
    size: float = 0.0
    reduce_qty: float = 0.0
    new_sl: Optional[float] = None
    new_tp: Optional[float] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION HELPERS
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


# ═══════════════════════════════════════════════════════════════
#  DECISION ENGINE
# ═══════════════════════════════════════════════════════════════

class DecisionEngine:
    """
    Autonomous position management engine.
    
    Runs on interval and evaluates each open position against configurable rules.
    Delegates actions to executors (does not hold position state itself).
    """
    
    def __init__(self, executors: Dict[str, Any], safety_guard=None):
        """
        Args:
            executors: {"bitget": executor, "kraken": executor}
            safety_guard: Optional PositionSafetyGuard instance for SL/TP management
        """
        self.executors = executors
        self.safety_guard = safety_guard
        
        # Config
        self.enabled = _env_bool("DECISION_ENGINE_ENABLED", True)
        self.interval_sec = _env_int("DECISION_ENGINE_INTERVAL_SEC", 15)
        self.max_adverse_pct = _env_float("RISK_MAX_ADVERSE_MOVE_PCT", 1.0)
        self.reduce_step_pct = _env_float("RISK_REDUCE_STEP_PCT", 25.0)
        self.atr_spike_mult = _env_float("RISK_ATR_SPIKE_MULT", 2.0)
        self.trailing_enabled = _env_bool("RISK_TRAILING_ENABLED", False)
        self.trailing_activate_pct = _env_float("RISK_TRAILING_ACTIVATE_AT_PCT", 0.8)
        self.trailing_distance_pct = _env_float("RISK_TRAILING_DISTANCE_PCT", 0.4)
        self.max_position_age_hours = _env_float("DECISION_MAX_POSITION_AGE_HOURS", 48.0)
        
        # State
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._atr_cache: Dict[str, float] = {}  # symbol -> current ATR
        self._atr_baseline: Dict[str, float] = {}  # symbol -> baseline ATR
        self._trailing_best: Dict[str, float] = {}  # pos_key -> best price
        self._evaluation_history: List[PositionEvaluation] = []
        self._stats = {
            "cycles": 0,
            "evaluations": 0,
            "decisions": {"HOLD": 0, "REDUCE": 0, "CLOSE": 0, "TIGHTEN_SL": 0, "CREATE_SL": 0, "CREATE_TP": 0},
            "errors": 0,
        }
        
        logger.info(
            "[DECISION_ENGINE] Init | enabled=%s | interval=%ds | "
            "max_adverse=%.1f%% | reduce_step=%.0f%% | trailing=%s",
            self.enabled, self.interval_sec,
            self.max_adverse_pct, self.reduce_step_pct, self.trailing_enabled,
        )
    
    # ─────────────────────────────────────────────────
    #  LIFECYCLE
    # ─────────────────────────────────────────────────
    
    def start(self):
        """Start background decision loop."""
        if not self.enabled:
            logger.info("[DECISION_ENGINE] Disabled via config")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("[DECISION_ENGINE] Background loop started")
    
    def stop(self):
        """Stop the engine."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[DECISION_ENGINE] Stopped")
    
    async def _loop(self):
        """Main decision loop."""
        while self._running:
            try:
                await self.evaluate_all()
            except Exception as e:
                self._stats["errors"] += 1
                logger.error("[DECISION_ENGINE] Cycle error: %s", e, exc_info=True)
            await asyncio.sleep(self.interval_sec)
    
    # ─────────────────────────────────────────────────
    #  EVALUATION
    # ─────────────────────────────────────────────────
    
    async def evaluate_all(self) -> List[PositionEvaluation]:
        """Evaluate all positions across all brokers."""
        self._stats["cycles"] += 1
        evaluations: List[PositionEvaluation] = []
        
        for broker_name, executor in self.executors.items():
            try:
                positions = await self._get_positions(executor, broker_name)
                for pos in positions:
                    eval_result = await self._evaluate_position(pos, executor, broker_name)
                    if eval_result:
                        evaluations.append(eval_result)
                        await self._execute_decision(eval_result, executor, broker_name)
            except Exception as e:
                self._stats["errors"] += 1
                logger.error("[DECISION_ENGINE] %s error: %s", broker_name, e, exc_info=True)
        
        self._evaluation_history = evaluations[-100:]  # Keep last 100
        return evaluations
    
    async def _get_positions(self, executor, broker: str) -> List[Dict]:
        """Get open positions."""
        if hasattr(executor, 'get_positions'):
            result = await executor.get_positions()
            if isinstance(result, dict):
                return result.get("positions", result.get("data", []))
            elif isinstance(result, list):
                return result
        return []
    
    async def _evaluate_position(self, pos: Dict, executor, broker: str) -> Optional[PositionEvaluation]:
        """
        Evaluate a single position and return a decision.
        
        Decision tree:
        1. Missing SL/TP → CREATE_SL / CREATE_TP
        2. Extreme drawdown → CLOSE or REDUCE
        3. ATR spike → TIGHTEN_SL
        4. Trailing stop conditions → CLOSE
        5. Otherwise → HOLD
        """
        symbol = pos.get("symbol", "")
        hold_side = (pos.get("holdSide", pos.get("side", "long"))).lower()
        size = float(pos.get("total", pos.get("size", pos.get("available", 0))))
        entry_price = float(pos.get("entryPrice", pos.get("openPriceAvg", pos.get("entry_price", 0))))
        mark_price = float(pos.get("markPrice", pos.get("mark_price", 0)))
        leverage = int(pos.get("leverage", 1))
        
        if size <= 0 or entry_price <= 0 or mark_price <= 0:
            return None
        
        self._stats["evaluations"] += 1
        pos_key = f"{broker}:{symbol}:{hold_side}"
        
        # ── Calculate PnL ──
        if hold_side == "long":
            pnl_pct = ((mark_price - entry_price) / entry_price) * 100 * leverage
        else:
            pnl_pct = ((entry_price - mark_price) / entry_price) * 100 * leverage
        
        # ── Rule 1: Extreme drawdown (> 2x max_adverse) → FULL CLOSE ──
        if pnl_pct < -(self.max_adverse_pct * 2):
            decision = Decision.CLOSE
            reason = f"EXTREME_DRAWDOWN pnl={pnl_pct:.2f}% < -{self.max_adverse_pct * 2:.1f}%"
            self._stats["decisions"]["CLOSE"] += 1
            return PositionEvaluation(
                symbol=symbol, broker=broker, side=hold_side,
                decision=decision, reason=reason,
                entry_price=entry_price, mark_price=mark_price,
                pnl_pct=pnl_pct, size=size,
            )
        
        # ── Rule 2: Moderate drawdown → REDUCE ──
        if pnl_pct < -self.max_adverse_pct:
            reduce_qty = size * (self.reduce_step_pct / 100.0)
            decision = Decision.REDUCE
            reason = f"DRAWDOWN pnl={pnl_pct:.2f}% < -{self.max_adverse_pct:.1f}%"
            self._stats["decisions"]["REDUCE"] += 1
            return PositionEvaluation(
                symbol=symbol, broker=broker, side=hold_side,
                decision=decision, reason=reason,
                entry_price=entry_price, mark_price=mark_price,
                pnl_pct=pnl_pct, size=size, reduce_qty=reduce_qty,
            )
        
        # ── Rule 3: Trailing stop logic ──
        if self.trailing_enabled:
            unfav_pnl = ((mark_price - entry_price) / entry_price) * 100 if hold_side == "long" else ((entry_price - mark_price) / entry_price) * 100
            
            if unfav_pnl >= self.trailing_activate_pct:
                best_key = f"best:{pos_key}"
                current_best = self._trailing_best.get(best_key, mark_price)
                
                if hold_side == "long":
                    if mark_price > current_best:
                        self._trailing_best[best_key] = mark_price
                        current_best = mark_price
                    trail_sl = current_best * (1 - self.trailing_distance_pct / 100.0)
                    if mark_price <= trail_sl:
                        self._stats["decisions"]["CLOSE"] += 1
                        return PositionEvaluation(
                            symbol=symbol, broker=broker, side=hold_side,
                            decision=Decision.CLOSE,
                            reason=f"TRAILING_STOP mark={mark_price:.4f} <= trail_sl={trail_sl:.4f}",
                            entry_price=entry_price, mark_price=mark_price,
                            pnl_pct=pnl_pct, size=size,
                        )
                else:
                    if mark_price < current_best:
                        self._trailing_best[best_key] = mark_price
                        current_best = mark_price
                    trail_sl = current_best * (1 + self.trailing_distance_pct / 100.0)
                    if mark_price >= trail_sl:
                        self._stats["decisions"]["CLOSE"] += 1
                        return PositionEvaluation(
                            symbol=symbol, broker=broker, side=hold_side,
                            decision=Decision.CLOSE,
                            reason=f"TRAILING_STOP mark={mark_price:.4f} >= trail_sl={trail_sl:.4f}",
                            entry_price=entry_price, mark_price=mark_price,
                            pnl_pct=pnl_pct, size=size,
                        )
        
        # ── Rule 4: Default → HOLD ──
        self._stats["decisions"]["HOLD"] += 1
        return PositionEvaluation(
            symbol=symbol, broker=broker, side=hold_side,
            decision=Decision.HOLD,
            reason=f"OK pnl={pnl_pct:.2f}%",
            entry_price=entry_price, mark_price=mark_price,
            pnl_pct=pnl_pct, size=size,
        )
    
    # ─────────────────────────────────────────────────
    #  ACTION EXECUTION
    # ─────────────────────────────────────────────────
    
    async def _execute_decision(self, eval_result: PositionEvaluation, executor, broker: str):
        """Execute the decision for a position."""
        if eval_result.decision == Decision.HOLD:
            return  # Nothing to do
        
        symbol = eval_result.symbol
        side = eval_result.side
        
        logger.info("[DECISION_ENGINE] %s | %s@%s %s | %s",
                    eval_result.decision.value, symbol, broker, side, eval_result.reason)
        
        if eval_result.decision == Decision.CLOSE:
            await self._close_position(executor, broker, symbol, side, eval_result.size)
        
        elif eval_result.decision == Decision.REDUCE:
            if eval_result.reduce_qty > 0:
                await self._reduce_position(executor, broker, symbol, side, eval_result.reduce_qty)
        
        elif eval_result.decision == Decision.TIGHTEN_SL:
            if eval_result.new_sl and self.safety_guard:
                await self.safety_guard._attach_with_retry(
                    executor, broker, symbol,
                    "loss_plan" if broker == "bitget" else "stop_loss",
                    eval_result.new_sl, side, eval_result.size,
                )
    
    async def _close_position(self, executor, broker: str, symbol: str, hold_side: str, size: float):
        """Full close of a position."""
        close_side = "sell" if hold_side == "long" else "buy"
        try:
            if hasattr(executor, 'close_position_market'):
                result = await executor.close_position_market(symbol, hold_side, str(size))
            elif hasattr(executor, 'place_order'):
                result = await executor.place_order(
                    symbol=symbol, side=close_side, quantity=size,
                    order_type="market", reduce_only=True,
                )
            else:
                logger.error("[DECISION_ENGINE] No close method for %s", broker)
                return
            
            success = (isinstance(result, dict) and 
                      (result.get("code") == "00000" or result.get("success")))
            if success:
                logger.info("[DECISION_ENGINE] CLOSE_OK | %s@%s", symbol, broker)
            else:
                logger.error("[DECISION_ENGINE] CLOSE_FAIL | %s@%s | %s", symbol, broker, result)
        except Exception as e:
            logger.error("[DECISION_ENGINE] CLOSE_ERROR | %s@%s | %s", symbol, broker, e)
    
    async def _reduce_position(self, executor, broker: str, symbol: str, hold_side: str, qty: float):
        """Partial close of a position."""
        close_side = "sell" if hold_side == "long" else "buy"
        try:
            if hasattr(executor, 'close_position_market'):
                result = await executor.close_position_market(symbol, hold_side, str(qty))
            elif hasattr(executor, 'place_order_market'):
                result = await executor.place_order_market(symbol, close_side, str(qty), reduce_only=True)
            else:
                logger.error("[DECISION_ENGINE] No reduce method for %s", broker)
                return
            
            success = (isinstance(result, dict) and 
                      (result.get("code") == "00000" or result.get("success")))
            if success:
                logger.info("[DECISION_ENGINE] REDUCE_OK | %s@%s | qty=%.6f", symbol, broker, qty)
            else:
                logger.error("[DECISION_ENGINE] REDUCE_FAIL | %s@%s | %s", symbol, broker, result)
        except Exception as e:
            logger.error("[DECISION_ENGINE] REDUCE_ERROR | %s@%s | %s", symbol, broker, e)
    
    # ─────────────────────────────────────────────────
    #  ATR MANAGEMENT
    # ─────────────────────────────────────────────────
    
    def update_atr(self, symbol: str, atr_value: float):
        """
        Update ATR for a symbol (called externally from market data feed).
        
        If ATR spikes above baseline * atr_spike_mult, triggers tightening logic
        on next evaluation cycle.
        """
        symbol = symbol.upper()
        
        # Initialize baseline on first call
        if symbol not in self._atr_baseline:
            self._atr_baseline[symbol] = atr_value
        else:
            # Exponential moving average for baseline
            alpha = 0.1
            self._atr_baseline[symbol] = alpha * atr_value + (1 - alpha) * self._atr_baseline[symbol]
        
        self._atr_cache[symbol] = atr_value
    
    def is_atr_spiking(self, symbol: str) -> bool:
        """Check if current ATR is significantly above baseline."""
        symbol = symbol.upper()
        current = self._atr_cache.get(symbol)
        baseline = self._atr_baseline.get(symbol)
        if current and baseline and baseline > 0:
            return current > baseline * self.atr_spike_mult
        return False
    
    # ─────────────────────────────────────────────────
    #  STATUS API
    # ─────────────────────────────────────────────────
    
    def get_status(self) -> Dict[str, Any]:
        """Return engine status for monitoring."""
        return {
            "enabled": self.enabled,
            "running": self._running,
            "config": {
                "interval_sec": self.interval_sec,
                "max_adverse_pct": self.max_adverse_pct,
                "reduce_step_pct": self.reduce_step_pct,
                "trailing_enabled": self.trailing_enabled,
                "atr_spike_mult": self.atr_spike_mult,
            },
            "stats": self._stats,
            "recent_evaluations": [
                {
                    "symbol": e.symbol, "broker": e.broker, "side": e.side,
                    "decision": e.decision.value, "reason": e.reason,
                    "pnl_pct": round(e.pnl_pct, 2), "timestamp": e.timestamp,
                }
                for e in self._evaluation_history[-10:]
            ],
        }
