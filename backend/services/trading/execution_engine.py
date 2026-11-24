"""
Execution Engine — Partial TP, Scaling In/Out, Adaptive Stop Loss
═══════════════════════════════════════════════════════════════════

Converts TradeSignals + SizingResults into concrete ExecutionPlans.
Manages open-position lifecycle:
  - Partial take-profit at TP1 (configurable %)
  - Move SL to break-even after TP1 hit
  - Trailing stop based on ATR
  - Scale-in on pullbacks (optional)

Delegates actual order placement to existing BrokerRouter / OrderManager.

Config via .env:
  EXEC_PARTIAL_TP_PCT=50
  EXEC_MOVE_SL_TO_BE=true
  EXEC_TRAILING_ENABLED=true
  EXEC_TRAILING_ATR_MULT=2.0
  EXEC_SCALE_IN_ENABLED=false
  EXEC_SCALE_IN_MAX=2
  EXEC_SCALE_IN_PULLBACK_PCT=0.5
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.services.trading.models import (
    ExecutionAction,
    ExecutionPlan,
    RiskAction,
    RiskVerdict,
    SizingResult,
    TradeSignal,
)

logger = logging.getLogger("smarttrade.trading.execution")


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default

def _envb(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")

def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


# ═══════════════════════════════════════════════════════════════
#  TRACKED POSITION (internal state)
# ═══════════════════════════════════════════════════════════════

@dataclass
class TrackedPosition:
    """Internal state for managing an open position's lifecycle."""
    signal_id: str
    symbol: str
    side: str                  # "long" or "short"
    entry_price: float
    current_sl: float
    current_tp: float
    tp2: float = 0.0
    quantity: float = 0.0
    remaining_qty: float = 0.0
    tp1_hit: bool = False
    sl_at_be: bool = False
    scale_in_count: int = 0
    trailing_active: bool = False
    highest_since_entry: float = 0.0   # For trailing (long)
    lowest_since_entry: float = 999999.0  # For trailing (short)
    opened_at: float = field(default_factory=time.time)


class ExecutionEngine:
    """
    Converts signals into execution plans and manages position lifecycle.

    Workflow:
      1. plan_entry() → creates ExecutionPlan for new position
      2. manage_position() → checks open positions for TP/SL/trailing updates
      3. plan_exit() → handles explicit close requests
    """

    def __init__(self) -> None:
        self._partial_tp_pct = _envf("EXEC_PARTIAL_TP_PCT", 50.0)
        self._move_sl_to_be = _envb("EXEC_MOVE_SL_TO_BE", True)
        self._trailing_enabled = _envb("EXEC_TRAILING_ENABLED", True)
        self._trailing_atr_mult = _envf("EXEC_TRAILING_ATR_MULT", 2.0)
        self._scale_in_enabled = _envb("EXEC_SCALE_IN_ENABLED", False)
        self._scale_in_max = _envi("EXEC_SCALE_IN_MAX", 2)
        self._scale_in_pullback_pct = _envf("EXEC_SCALE_IN_PULLBACK_PCT", 0.5)

        # Active positions being managed
        self._positions: Dict[str, TrackedPosition] = {}

    # ──────────────────────────────────────────────────
    #  ENTRY PLANNING
    # ──────────────────────────────────────────────────

    def plan_entry(
        self,
        signal: TradeSignal,
        sizing: SizingResult,
        risk_verdict: RiskVerdict,
    ) -> Optional[ExecutionPlan]:
        """
        Create an execution plan for a new position entry.
        Returns None if blocked by risk or already in position.
        """
        # Already tracking this symbol?
        if signal.symbol in self._positions:
            logger.info("[EXEC] Already in position for %s, skip entry", signal.symbol)
            return None

        # Risk check
        if risk_verdict.action in (RiskAction.BLOCK, RiskAction.KILL):
            logger.info("[EXEC] Blocked by risk: %s", risk_verdict.reason)
            return None

        # Apply risk size cap
        qty = sizing.quantity
        if risk_verdict.action == RiskAction.REDUCE:
            qty = qty * (risk_verdict.max_size_pct / 100.0)
            logger.info("[EXEC] Size reduced to %.1f%% by risk engine", risk_verdict.max_size_pct)

        if qty <= 0 or sizing.rejected:
            return None

        side = "buy" if signal.direction.value == "long" else "sell"

        # Track internally
        self._positions[signal.symbol] = TrackedPosition(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.direction.value,
            entry_price=signal.entry_price,
            current_sl=signal.stop_loss,
            current_tp=signal.take_profit_1,
            tp2=signal.take_profit_2,
            quantity=qty,
            remaining_qty=qty,
            highest_since_entry=signal.entry_price,
            lowest_since_entry=signal.entry_price,
        )

        plan = ExecutionPlan(
            action=ExecutionAction.OPEN,
            symbol=signal.symbol,
            side=side,
            quantity=qty,
            price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_1,
            partial_tp_pct=self._partial_tp_pct,
            signal_id=signal.signal_id,
            reason=f"entry: {'; '.join(signal.reasons[:3])}",
        )

        logger.info(
            "[EXEC] ENTRY plan: %s %s %.6f @ %.2f SL=%.2f TP=%.2f",
            signal.symbol, side, qty, signal.entry_price,
            signal.stop_loss, signal.take_profit_1,
        )
        return plan

    # ──────────────────────────────────────────────────
    #  POSITION MANAGEMENT (called every cycle)
    # ──────────────────────────────────────────────────

    def manage_positions(
        self,
        current_prices: Dict[str, float],
        atr_values: Optional[Dict[str, float]] = None,
    ) -> List[ExecutionPlan]:
        """
        Evaluate all tracked positions against current prices.
        Returns list of ExecutionPlans (partial TP, trailing SL, etc.)
        """
        plans: List[ExecutionPlan] = []
        to_remove: List[str] = []

        for symbol, pos in self._positions.items():
            price = current_prices.get(symbol, 0)
            if price <= 0:
                continue

            # Update extremes
            if pos.side == "long":
                pos.highest_since_entry = max(pos.highest_since_entry, price)
            else:
                pos.lowest_since_entry = min(pos.lowest_since_entry, price)

            # ── Check SL hit ──
            sl_hit = (pos.side == "long" and price <= pos.current_sl) or \
                     (pos.side == "short" and price >= pos.current_sl)
            if sl_hit:
                plans.append(ExecutionPlan(
                    action=ExecutionAction.CLOSE,
                    symbol=symbol,
                    side="sell" if pos.side == "long" else "buy",
                    quantity=pos.remaining_qty,
                    price=price,
                    stop_loss=pos.current_sl,
                    signal_id=pos.signal_id,
                    reason=f"SL hit at {price:.2f}",
                ))
                to_remove.append(symbol)
                continue

            # ── Check TP1 partial close ──
            tp1_hit = (pos.side == "long" and price >= pos.current_tp) or \
                      (pos.side == "short" and price <= pos.current_tp)

            if tp1_hit and not pos.tp1_hit:
                partial_qty = pos.remaining_qty * (self._partial_tp_pct / 100.0)
                if partial_qty > 0:
                    plans.append(ExecutionPlan(
                        action=ExecutionAction.PARTIAL_TP,
                        symbol=symbol,
                        side="sell" if pos.side == "long" else "buy",
                        quantity=partial_qty,
                        price=price,
                        take_profit=pos.current_tp,
                        partial_tp_pct=self._partial_tp_pct,
                        signal_id=pos.signal_id,
                        reason=f"TP1 hit at {price:.2f}, closing {self._partial_tp_pct}%",
                    ))
                    pos.remaining_qty -= partial_qty
                    pos.tp1_hit = True

                    # Move SL to break-even
                    if self._move_sl_to_be and not pos.sl_at_be:
                        pos.current_sl = pos.entry_price
                        pos.sl_at_be = True
                        plans.append(ExecutionPlan(
                            action=ExecutionAction.TIGHTEN_SL,
                            symbol=symbol,
                            side=pos.side,
                            quantity=pos.remaining_qty,
                            price=pos.entry_price,
                            stop_loss=pos.entry_price,
                            signal_id=pos.signal_id,
                            reason="SL moved to break-even after TP1",
                        ))

                    # Update TP target to TP2
                    if pos.tp2 > 0:
                        pos.current_tp = pos.tp2

            # ── Check TP2 full close ──
            if pos.tp1_hit and pos.tp2 > 0:
                tp2_hit = (pos.side == "long" and price >= pos.tp2) or \
                          (pos.side == "short" and price <= pos.tp2)
                if tp2_hit:
                    plans.append(ExecutionPlan(
                        action=ExecutionAction.CLOSE,
                        symbol=symbol,
                        side="sell" if pos.side == "long" else "buy",
                        quantity=pos.remaining_qty,
                        price=price,
                        take_profit=pos.tp2,
                        signal_id=pos.signal_id,
                        reason=f"TP2 hit at {price:.2f}, full close",
                    ))
                    to_remove.append(symbol)
                    continue

            # ── Trailing stop ──
            if self._trailing_enabled and pos.tp1_hit:
                atr = (atr_values or {}).get(symbol, 0)
                if atr > 0:
                    trail_dist = atr * self._trailing_atr_mult
                    if pos.side == "long":
                        new_sl = pos.highest_since_entry - trail_dist
                        if new_sl > pos.current_sl:
                            pos.current_sl = new_sl
                            pos.trailing_active = True
                            plans.append(ExecutionPlan(
                                action=ExecutionAction.TIGHTEN_SL,
                                symbol=symbol,
                                side=pos.side,
                                quantity=pos.remaining_qty,
                                stop_loss=new_sl,
                                signal_id=pos.signal_id,
                                reason=f"trailing SL → {new_sl:.2f}",
                            ))
                    else:
                        new_sl = pos.lowest_since_entry + trail_dist
                        if new_sl < pos.current_sl:
                            pos.current_sl = new_sl
                            pos.trailing_active = True
                            plans.append(ExecutionPlan(
                                action=ExecutionAction.TIGHTEN_SL,
                                symbol=symbol,
                                side=pos.side,
                                quantity=pos.remaining_qty,
                                stop_loss=new_sl,
                                signal_id=pos.signal_id,
                                reason=f"trailing SL → {new_sl:.2f}",
                            ))

        # Clean up closed positions
        for sym in to_remove:
            del self._positions[sym]

        return plans

    # ──────────────────────────────────────────────────
    #  EXPLICIT EXIT
    # ──────────────────────────────────────────────────

    def plan_exit(self, symbol: str, price: float, reason: str = "manual") -> Optional[ExecutionPlan]:
        """Force-close a tracked position."""
        pos = self._positions.get(symbol)
        if not pos:
            return None

        plan = ExecutionPlan(
            action=ExecutionAction.CLOSE,
            symbol=symbol,
            side="sell" if pos.side == "long" else "buy",
            quantity=pos.remaining_qty,
            price=price,
            signal_id=pos.signal_id,
            reason=reason,
        )
        del self._positions[symbol]
        return plan

    def plan_emergency_close_all(self, current_prices: Dict[str, float]) -> List[ExecutionPlan]:
        """Kill switch: close every tracked position immediately."""
        plans: List[ExecutionPlan] = []
        for symbol, pos in list(self._positions.items()):
            price = current_prices.get(symbol, pos.entry_price)
            plans.append(ExecutionPlan(
                action=ExecutionAction.CLOSE,
                symbol=symbol,
                side="sell" if pos.side == "long" else "buy",
                quantity=pos.remaining_qty,
                price=price,
                signal_id=pos.signal_id,
                reason="EMERGENCY CLOSE — kill switch",
            ))
        self._positions.clear()
        logger.critical("[EXEC] EMERGENCY CLOSE ALL — %d positions closed", len(plans))
        return plans

    # ──────────────────────────────────────────────────
    #  STATE QUERIES
    # ──────────────────────────────────────────────────

    def get_tracked_positions(self) -> Dict[str, Dict[str, Any]]:
        """Return all tracked positions as dicts (for API/debug)."""
        return {
            sym: {
                "signal_id": p.signal_id,
                "side": p.side,
                "entry_price": p.entry_price,
                "current_sl": p.current_sl,
                "current_tp": p.current_tp,
                "quantity": p.quantity,
                "remaining_qty": p.remaining_qty,
                "tp1_hit": p.tp1_hit,
                "sl_at_be": p.sl_at_be,
                "trailing_active": p.trailing_active,
            }
            for sym, p in self._positions.items()
        }

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions
