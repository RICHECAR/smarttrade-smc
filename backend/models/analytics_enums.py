"""
Analytics Enums — Source of Truth for all categorical data

Every enum here is the SINGLE definition used by:
  - Database models (SQLAlchemy)
  - Analytics aggregation service
  - API serialization (Pydantic)
  - Frontend TypeScript types (mirrored)

Author: SMC Trading Bot Framework — Analytics Pipeline
"""

from enum import Enum


# ═══════════════════════════════════════════════════════════════════════════════
# MARKET REGIME
# ═══════════════════════════════════════════════════════════════════════════════

class MarketRegime(str, Enum):
    """Market regime classification — drives risk factor & strategy selection."""
    TREND = "TREND"           # Strong directional move, full risk
    RANGE = "RANGE"           # Sideways / consolidation, reduced risk
    CHOP = "CHOP"             # No structure, minimal or no trading
    RECOVERY = "RECOVERY"     # Post-drawdown cautious mode
    UNKNOWN = "UNKNOWN"       # Initial state or detection failure


# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM MODE
# ═══════════════════════════════════════════════════════════════════════════════

class SystemMode(str, Enum):
    """Operational mode of the trading engine."""
    LIVE = "LIVE"             # Full auto-execution, real orders
    SHADOW = "SHADOW"         # Simulated execution, no real orders
    PAUSED = "PAUSED"         # Engine running but entries blocked
    HANDS_OFF = "HANDS_OFF"   # Manual override, engine idle
    MONITORING = "MONITORING" # Signals only, no execution


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL DECISION
# ═══════════════════════════════════════════════════════════════════════════════

class SignalDecision(str, Enum):
    """Outcome of signal evaluation — ACCEPT means order placed."""
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


class RejectReason(str, Enum):
    """
    Exhaustive list of reasons a signal can be rejected.
    Each reason maps to a specific gate in the pipeline.
    """
    # ── Risk gates ──────────────────────────────────────────────
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    SESSION_LOSS_LIMIT = "SESSION_LOSS_LIMIT"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    MAX_POSITIONS_REACHED = "MAX_POSITIONS_REACHED"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    VOLATILITY_PAUSE = "VOLATILITY_PAUSE"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"

    # ── Market quality gates ────────────────────────────────────
    REGIME_BLOCKED = "REGIME_BLOCKED"           # CHOP regime blocks entry
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    LIQUIDITY_TOO_THIN = "LIQUIDITY_TOO_THIN"
    STALE_DATA = "STALE_DATA"
    FUNDING_RATE_HIGH = "FUNDING_RATE_HIGH"

    # ── Strategy / Signal quality ───────────────────────────────
    RR_TOO_LOW = "RR_TOO_LOW"
    EDGE_SCORE_LOW = "EDGE_SCORE_LOW"
    NO_MTF_ALIGNMENT = "NO_MTF_ALIGNMENT"
    NO_LIQUIDITY_SWEEP = "NO_LIQUIDITY_SWEEP"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    CONFLICTING_POSITION = "CONFLICTING_POSITION"

    # ── Execution gates ─────────────────────────────────────────
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    ORDER_REJECTED = "ORDER_REJECTED"
    SIZING_BELOW_MINIMUM = "SIZING_BELOW_MINIMUM"
    HEALTH_GATE_FAIL = "HEALTH_GATE_FAIL"

    # ── Capital allocation ──────────────────────────────────────
    CAPITAL_NOT_ALLOCATED = "CAPITAL_NOT_ALLOCATED"
    SCALE_READINESS_FAIL = "SCALE_READINESS_FAIL"

    # ── Misc ────────────────────────────────────────────────────
    MANUAL_BLOCK = "MANUAL_BLOCK"
    UNKNOWN = "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════════════════
# EXECUTION MODE
# ═══════════════════════════════════════════════════════════════════════════════

class ExecutionMode(str, Enum):
    """How the order was placed on the exchange."""
    MAKER = "MAKER"           # Post-only limit order
    TAKER = "TAKER"           # Market / aggressive limit
    FALLBACK_TAKER = "FALLBACK_TAKER"  # Maker failed → taker fallback


# ═══════════════════════════════════════════════════════════════════════════════
# BROKER
# ═══════════════════════════════════════════════════════════════════════════════

class BrokerName(str, Enum):
    """Supported brokers."""
    BITGET = "bitget"
    KRAKEN = "kraken"


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY
# ═══════════════════════════════════════════════════════════════════════════════

class StrategyName(str, Enum):
    """Active strategy presets."""
    DEFAULT = "DEFAULT"
    ASIA = "ASIA"
    LONDON = "LONDON"
    NY = "NY"
    WEEKEND = "WEEKEND"


# ═══════════════════════════════════════════════════════════════════════════════
# RISK EVENTS
# ═══════════════════════════════════════════════════════════════════════════════

class RiskEventType(str, Enum):
    """Types of risk events that get recorded."""
    KILL_SWITCH_TRIGGER = "KILL_SWITCH_TRIGGER"
    KILL_SWITCH_RELEASE = "KILL_SWITCH_RELEASE"
    DRAWDOWN_EVENT = "DRAWDOWN_EVENT"
    RECOVERY_ENTER = "RECOVERY_ENTER"
    RECOVERY_STEP_CHANGE = "RECOVERY_STEP_CHANGE"
    RECOVERY_EXIT = "RECOVERY_EXIT"
    HEALTH_GATE_FAIL = "HEALTH_GATE_FAIL"
    CHAOS_SCENARIO = "CHAOS_SCENARIO"
    SLO_BREACH = "SLO_BREACH"
    VOLATILITY_PAUSE_START = "VOLATILITY_PAUSE_START"
    VOLATILITY_PAUSE_END = "VOLATILITY_PAUSE_END"
    COOLDOWN_START = "COOLDOWN_START"
    COOLDOWN_END = "COOLDOWN_END"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


class RiskSeverity(str, Enum):
    """Severity levels for risk events."""
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"


# ═══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE A/B VERSION
# ═══════════════════════════════════════════════════════════════════════════════

class AdaptiveVersion(str, Enum):
    """A/B testing variant identifier."""
    A = "A"
    B = "B"
    CONTROL = "CONTROL"


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE SIDE (reuse from existing but canonical here)
# ═══════════════════════════════════════════════════════════════════════════════

class TradeSide(str, Enum):
    """Trade direction."""
    LONG = "long"
    SHORT = "short"


# ═══════════════════════════════════════════════════════════════════════════════
# EXECUTION ATTEMPT STATUS
# ═══════════════════════════════════════════════════════════════════════════════

class ExecutionAttemptStatus(str, Enum):
    """Status of a single execution attempt through the order pipeline."""
    DISPATCH_STARTED = "DISPATCH_STARTED"
    PRE_FLIGHT_REJECT = "PRE_FLIGHT_REJECT"
    PRE_FLIGHT_PASS = "PRE_FLIGHT_PASS"
    EXECUTED = "EXECUTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class ExecutionBlockReason(str, Enum):
    """Specific reason an execution was blocked at the order manager level."""
    # Hard rejects
    AUTO_EXECUTE_DISABLED = "AUTO_EXECUTE_DISABLED"
    PAPER_MODE = "PAPER_MODE"
    KILL_SWITCH = "KILL_SWITCH"
    ACTIVE_TRADE = "ACTIVE_TRADE"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    MAX_DAILY_LOSS = "MAX_DAILY_LOSS"
    ZERO_BALANCE = "ZERO_BALANCE"
    INVALID_TPSL = "INVALID_TPSL"
    NOTIONAL_TOO_LOW = "NOTIONAL_TOO_LOW"
    QTY_VALIDATION_FAILED = "QTY_VALIDATION_FAILED"
    # Soft / conditional
    MAX_TRADES_PER_DAY = "MAX_TRADES_PER_DAY"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    COOLDOWN_HARD_REJECT = "COOLDOWN_HARD_REJECT"
    LOW_RR = "LOW_RR"
    DISABLED_SYMBOL = "DISABLED_SYMBOL"
    # Global risk engine
    GLOBAL_RISK = "GLOBAL_RISK"
    MAKER_NOT_VIABLE = "MAKER_NOT_VIABLE"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    # Multi-broker
    MULTI_BROKER_FAILED = "MULTI_BROKER_FAILED"
    # Misc
    STALE_DATA = "STALE_DATA"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    UNKNOWN = "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════════════════
# OPS EVENT TYPE
# ═══════════════════════════════════════════════════════════════════════════════

class OpsEventType(str, Enum):
    """Types of operational events recorded for observability."""
    WS_DISCONNECT = "WS_DISCONNECT"
    WS_RECONNECT = "WS_RECONNECT"
    CLOCK_SKEW = "CLOCK_SKEW"
    LATENCY_SPIKE = "LATENCY_SPIKE"
    API_ERROR = "API_ERROR"
    RATE_LIMIT_HIT = "RATE_LIMIT_HIT"
    CONFIG_RELOAD = "CONFIG_RELOAD"
    ENGINE_START = "ENGINE_START"
    ENGINE_STOP = "ENGINE_STOP"
    HEALTH_CHECK_FAIL = "HEALTH_CHECK_FAIL"
    DB_ERROR = "DB_ERROR"
    BROKER_TIMEOUT = "BROKER_TIMEOUT"


class OpsEventSeverity(str, Enum):
    """Severity levels for ops events."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
