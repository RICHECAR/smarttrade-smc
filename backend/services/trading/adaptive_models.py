"""
Adaptive Models — Data structures for the Adaptive Alpha Layer
══════════════════════════════════════════════════════════════

Extends the Performance Layer with regime, scoring, adaptation,
and journal structures.  Never touches existing models.py.
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

class MarketRegime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    MOMENTUM_EXPANSION = "momentum_expansion"
    UNKNOWN = "unknown"


class PolicyAction(str, Enum):
    AGGRESSIVE = "aggressive"       # Boost size and accept weaker setups
    NORMAL = "normal"               # Standard parameters
    CAUTIOUS = "cautious"           # Reduce size, raise thresholds
    DEFENSIVE = "defensive"         # Minimum size, only strong setups
    BLOCK = "block"                 # No new trades


# ═══════════════════════════════════════════════════════════════
#  REGIME DETECTION
# ═══════════════════════════════════════════════════════════════

@dataclass
class RegimeSnapshot:
    """Point-in-time regime assessment for a symbol."""
    symbol: str = ""
    regime: MarketRegime = MarketRegime.UNKNOWN
    trend_strength: float = 0.0       # 0–1  (ADX-like)
    volatility_percentile: float = 0.5  # 0–1  (current ATR vs. historical)
    momentum_score: float = 0.0       # -1 to +1
    bandwidth: float = 0.0           # Bollinger bandwidth
    regime_age_bars: int = 0         # how many bars in this regime
    confidence: float = 0.0          # how certain the classification is
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "regime": self.regime.value,
            "trend_strength": round(self.trend_strength, 3),
            "volatility_percentile": round(self.volatility_percentile, 3),
            "momentum_score": round(self.momentum_score, 3),
            "bandwidth": round(self.bandwidth, 4),
            "regime_age_bars": self.regime_age_bars,
            "confidence": round(self.confidence, 3),
            "timestamp": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  SETUP SCORING
# ═══════════════════════════════════════════════════════════════

@dataclass
class SetupScore:
    """Composite score for a proposed trade signal."""
    score: float = 0.5               # 0–1 composite
    priority: int = 5                # 1 (highest) to 10 (lowest)
    veto: bool = False
    veto_reason: str = ""
    components: Dict[str, float] = field(default_factory=dict)
    # Component breakdown: regime_fit, setup_history, time_of_day, streak, signal_quality
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "priority": self.priority,
            "veto": self.veto,
            "veto_reason": self.veto_reason,
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "timestamp": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  STRATEGY ADAPTATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class AdaptationParams:
    """Dynamic parameter overrides produced by StrategyAdaptationEngine."""
    confidence_threshold: float = 0.4   # Minimum signal confidence
    size_multiplier: float = 1.0        # Applied on top of sizing engine
    partial_tp_pct: float = 50.0        # Override for ExecutionEngine
    trailing_atr_mult: float = 2.0      # Trailing stop aggressiveness
    max_concurrent_trades: int = 3
    min_risk_reward: float = 1.5
    cooldown_multiplier: float = 1.0    # 1.0 = normal cooldown
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "confidence_threshold": round(self.confidence_threshold, 3),
            "size_multiplier": round(self.size_multiplier, 3),
            "partial_tp_pct": round(self.partial_tp_pct, 1),
            "trailing_atr_mult": round(self.trailing_atr_mult, 2),
            "max_concurrent_trades": self.max_concurrent_trades,
            "min_risk_reward": round(self.min_risk_reward, 2),
            "cooldown_multiplier": round(self.cooldown_multiplier, 2),
            "reason": self.reason,
        }


# ═══════════════════════════════════════════════════════════════
#  ADAPTIVE POLICY
# ═══════════════════════════════════════════════════════════════

@dataclass
class PolicyDecision:
    """Output of AdaptivePolicy — final go/no-go + parameter adjustments."""
    action: PolicyAction = PolicyAction.NORMAL
    adaptation: AdaptationParams = field(default_factory=AdaptationParams)
    setup_score: Optional[SetupScore] = None
    regime: Optional[RegimeSnapshot] = None
    reason: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "adaptation": self.adaptation.to_dict(),
            "setup_score": self.setup_score.to_dict() if self.setup_score else None,
            "regime": self.regime.to_dict() if self.regime else None,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  TRADE JOURNAL
# ═══════════════════════════════════════════════════════════════

@dataclass
class JournalEntry:
    """Complete record of a single trading decision and its outcome."""
    journal_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    signal_id: str = ""
    symbol: str = ""
    direction: str = ""
    timestamp: float = field(default_factory=time.time)

    # Decision context
    signal_confidence: float = 0.0
    signal_strength: str = ""
    signal_reasons: List[str] = field(default_factory=list)
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    risk_reward: float = 0.0

    # Regime & scoring
    regime: str = "unknown"
    regime_confidence: float = 0.0
    setup_score: float = 0.0
    setup_priority: int = 0
    setup_vetoed: bool = False

    # Risk decision
    risk_action: str = ""
    risk_reason: str = ""
    drawdown_pct: float = 0.0
    daily_loss_pct: float = 0.0
    consecutive_losses: int = 0

    # Sizing
    quantity: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0
    size_multiplier: float = 1.0

    # Adaptation
    adapted_confidence_threshold: float = 0.0
    adapted_size_mult: float = 1.0
    policy_action: str = "normal"

    # Execution
    executed: bool = False
    execution_action: str = ""
    fill_price: float = 0.0

    # Outcome (filled after close)
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    hold_seconds: float = 0.0
    closed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "journal_id": self.journal_id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
            "signal_confidence": round(self.signal_confidence, 3),
            "signal_strength": self.signal_strength,
            "signal_reasons": self.signal_reasons,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "risk_reward": round(self.risk_reward, 2),
            "regime": self.regime,
            "regime_confidence": round(self.regime_confidence, 3),
            "setup_score": round(self.setup_score, 3),
            "setup_priority": self.setup_priority,
            "setup_vetoed": self.setup_vetoed,
            "risk_action": self.risk_action,
            "risk_reason": self.risk_reason,
            "drawdown_pct": round(self.drawdown_pct, 2),
            "daily_loss_pct": round(self.daily_loss_pct, 2),
            "consecutive_losses": self.consecutive_losses,
            "quantity": round(self.quantity, 6),
            "notional": round(self.notional, 2),
            "risk_amount": round(self.risk_amount, 2),
            "size_multiplier": round(self.size_multiplier, 3),
            "adapted_confidence_threshold": round(self.adapted_confidence_threshold, 3),
            "adapted_size_mult": round(self.adapted_size_mult, 3),
            "policy_action": self.policy_action,
            "executed": self.executed,
            "execution_action": self.execution_action,
            "fill_price": self.fill_price,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct, 2),
            "hold_seconds": round(self.hold_seconds, 1),
            "closed": self.closed,
        }


# ═══════════════════════════════════════════════════════════════
#  META-ANALYSIS
# ═══════════════════════════════════════════════════════════════

@dataclass
class DimensionStats:
    """Performance stats for a single slice (e.g., a specific regime or hour)."""
    dimension: str = ""          # e.g. "regime"
    value: str = ""              # e.g. "trending_up"
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    avg_pnl: float = 0.0
    profit_factor: float = 0.0
    avg_hold_seconds: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    sample_size_ok: bool = False  # True if enough trades for significance

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension,
            "value": self.value,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "net_pnl": round(self.net_pnl, 2),
            "avg_pnl": round(self.avg_pnl, 2),
            "profit_factor": round(self.profit_factor, 2),
            "avg_hold_seconds": round(self.avg_hold_seconds, 1),
            "best_trade": round(self.best_trade, 2),
            "worst_trade": round(self.worst_trade, 2),
            "sample_size_ok": self.sample_size_ok,
        }
