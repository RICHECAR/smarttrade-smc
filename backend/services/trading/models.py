"""
Trading Models — Shared data structures for the Performance Layer
═══════════════════════════════════════════════════════════════════

All modules in backend/services/trading/ share these types.
No I/O, no side effects — pure data.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════
#  ENUMS
# ═══════════════════════════════════════════════════════════════

class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class SignalStrength(str, Enum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


class RiskAction(str, Enum):
    ALLOW = "ALLOW"
    REDUCE = "REDUCE"
    BLOCK = "BLOCK"
    KILL = "KILL"


class ExecutionAction(str, Enum):
    OPEN = "OPEN"
    PARTIAL_TP = "PARTIAL_TP"
    SCALE_IN = "SCALE_IN"
    TIGHTEN_SL = "TIGHTEN_SL"
    CLOSE = "CLOSE"
    HOLD = "HOLD"


class VolatilityRegime(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EXTREME = "extreme"


# ═══════════════════════════════════════════════════════════════
#  CORE DATA CLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class TradeSignal:
    """Output of DecisionEngine — a proposed trade entry."""
    signal_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    symbol: str = ""
    direction: SignalDirection = SignalDirection.FLAT
    strength: SignalStrength = SignalStrength.WEAK
    confidence: float = 0.0          # 0.0–1.0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0       # Primary TP
    take_profit_2: float = 0.0       # Extended TP
    risk_reward: float = 0.0
    reasons: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "strength": self.strength.value,
            "confidence": round(self.confidence, 3),
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "risk_reward": round(self.risk_reward, 2),
            "reasons": self.reasons,
            "timestamp": self.timestamp,
        }


@dataclass
class SizingResult:
    """Output of PositionSizingEngine."""
    quantity: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0
    confidence_multiplier: float = 1.0
    streak_multiplier: float = 1.0
    volatility_multiplier: float = 1.0
    final_multiplier: float = 1.0
    rejected: bool = False
    reject_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "quantity": round(self.quantity, 6),
            "notional": round(self.notional, 2),
            "risk_amount": round(self.risk_amount, 2),
            "confidence_multiplier": round(self.confidence_multiplier, 3),
            "streak_multiplier": round(self.streak_multiplier, 3),
            "volatility_multiplier": round(self.volatility_multiplier, 3),
            "final_multiplier": round(self.final_multiplier, 3),
            "rejected": self.rejected,
            "reject_reason": self.reject_reason,
        }


@dataclass
class RiskVerdict:
    """Output of RiskEngine — whether a trade is allowed."""
    action: RiskAction = RiskAction.ALLOW
    reason: str = ""
    max_size_pct: float = 100.0      # Cap size to this % of requested
    current_drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    daily_loss_pct: float = 0.0
    kill_switch_active: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "max_size_pct": round(self.max_size_pct, 1),
            "current_drawdown_pct": round(self.current_drawdown_pct, 2),
            "consecutive_losses": self.consecutive_losses,
            "daily_loss_pct": round(self.daily_loss_pct, 2),
            "kill_switch_active": self.kill_switch_active,
        }


@dataclass
class ExecutionPlan:
    """Output of ExecutionEngine — concrete orders to execute."""
    action: ExecutionAction = ExecutionAction.HOLD
    symbol: str = ""
    side: str = ""
    quantity: float = 0.0
    price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    partial_tp_pct: float = 50.0     # % of position to close at TP1
    trailing_stop_pct: float = 0.0
    signal_id: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": round(self.quantity, 6),
            "price": round(self.price, 2),
            "stop_loss": round(self.stop_loss, 2),
            "take_profit": round(self.take_profit, 2),
            "partial_tp_pct": self.partial_tp_pct,
            "trailing_stop_pct": round(self.trailing_stop_pct, 4),
            "signal_id": self.signal_id,
            "reason": self.reason,
        }


@dataclass
class PerformanceSnapshot:
    """Point-in-time performance metrics."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    current_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    net_pnl: float = 0.0
    consecutive_wins: int = 0
    consecutive_losses: int = 0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    avg_hold_seconds: float = 0.0
    volatility_regime: str = "normal"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "profit_factor": round(self.profit_factor, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "current_drawdown_pct": round(self.current_drawdown_pct, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 3),
            "net_pnl": round(self.net_pnl, 2),
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "best_trade": round(self.best_trade, 2),
            "worst_trade": round(self.worst_trade, 2),
            "avg_hold_seconds": round(self.avg_hold_seconds, 1),
            "volatility_regime": self.volatility_regime,
            "timestamp": self.timestamp,
        }


@dataclass
class LoopState:
    """Current state of the trading loop — for monitoring."""
    running: bool = False
    cycle_count: int = 0
    last_cycle_ms: float = 0.0
    last_signal: Optional[TradeSignal] = None
    last_risk_verdict: Optional[RiskVerdict] = None
    last_execution: Optional[ExecutionPlan] = None
    performance: Optional[PerformanceSnapshot] = None
    errors: List[str] = field(default_factory=list)
    started_at: str = ""
    uptime_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "cycle_count": self.cycle_count,
            "last_cycle_ms": round(self.last_cycle_ms, 1),
            "last_signal": self.last_signal.to_dict() if self.last_signal else None,
            "last_risk_verdict": self.last_risk_verdict.to_dict() if self.last_risk_verdict else None,
            "last_execution": self.last_execution.to_dict() if self.last_execution else None,
            "performance": self.performance.to_dict() if self.performance else None,
            "errors": self.errors[-10:],  # Last 10 errors
            "started_at": self.started_at,
            "uptime_seconds": round(self.uptime_seconds, 1),
        }
