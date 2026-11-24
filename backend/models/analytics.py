"""
Analytics Models — SQLAlchemy ORM for the Analytics Pipeline

Tables:
  1. system_state_snapshots — Regime & system state timeline
  2. signal_decisions       — Every signal evaluated (accepted + rejected)
  3. trade_analytics        — Enriched trade facts (MAE/MFE, regime context)
  4. risk_events            — Kill-switch, drawdown, recovery, SLO breaches
  5. daily_analytics        — Pre-computed aggregates for the frontend

Design:
  - These tables are APPEND-ONLY (except daily_analytics which is UPSERT)
  - The frontend NEVER queries raw tables — only daily_analytics + API views
  - All enums come from analytics_enums.py (single source of truth)

Author: SMC Trading Bot Framework — Analytics Pipeline
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Float, Integer, DateTime, Boolean, Date, Text, Index,
    JSON, UniqueConstraint,
)

from backend.db.base import Base


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 1 — system_state_snapshots
# ═══════════════════════════════════════════════════════════════════════════════

class SystemStateSnapshot(Base):
    """
    Point-in-time snapshot of the trading system state.
    Recorded every cycle or on significant state change.
    Enables: regime timeline, system behavior audit, post-mortem.
    """
    __tablename__ = "system_state_snapshots"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ts = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    # Market regime
    regime = Column(String(20), nullable=False)        # MarketRegime enum
    regime_confidence = Column(Float, nullable=True)    # 0.0–1.0

    # Risk parameters in effect
    risk_factor = Column(Float, nullable=False, default=1.0)
    adaptive_weight = Column(Float, nullable=True)
    capital_utilization = Column(Float, nullable=True)  # 0.0–1.0

    # System mode
    mode = Column(String(20), nullable=False, default="LIVE")  # SystemMode enum
    kill_switch_active = Column(Boolean, nullable=False, default=False)
    volatility_pause_active = Column(Boolean, nullable=False, default=False)
    recovery_step = Column(Integer, nullable=False, default=0)  # 0 = normal, 1–4 = recovery stages

    # Context
    open_positions = Column(Integer, nullable=True, default=0)
    daily_pnl = Column(Float, nullable=True, default=0.0)
    equity = Column(Float, nullable=True)

    __table_args__ = (
        Index("idx_sss_ts_regime", "ts", "regime"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "regime": self.regime,
            "regime_confidence": self.regime_confidence,
            "risk_factor": self.risk_factor,
            "adaptive_weight": self.adaptive_weight,
            "capital_utilization": self.capital_utilization,
            "mode": self.mode,
            "kill_switch_active": self.kill_switch_active,
            "volatility_pause_active": self.volatility_pause_active,
            "recovery_step": self.recovery_step,
            "open_positions": self.open_positions,
            "daily_pnl": self.daily_pnl,
            "equity": self.equity,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 2 — signal_decisions
# ═══════════════════════════════════════════════════════════════════════════════

class SignalDecisionRecord(Base):
    """
    Every signal evaluated by the engine — accepted OR rejected.
    Rejected signals are as important as accepted ones for analytics.
    """
    __tablename__ = "signal_decisions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ts = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    # Signal identification
    symbol = Column(String(20), nullable=False, index=True)
    timeframe = Column(String(10), nullable=True)
    strategy = Column(String(20), nullable=True)           # StrategyName enum
    side = Column(String(10), nullable=True)                # TradeSide enum

    # Market context at decision time
    regime = Column(String(20), nullable=True)              # MarketRegime enum
    regime_confidence = Column(Float, nullable=True)

    # Decision
    decision = Column(String(10), nullable=False)           # ACCEPT / REJECT
    reason = Column(String(50), nullable=True)              # RejectReason enum (null if ACCEPT)

    # Signal quality metrics
    rr_estimated = Column(Float, nullable=True)
    edge_score = Column(Float, nullable=True)

    # Risk allocation
    risk_pct_requested = Column(Float, nullable=True)       # What the signal wanted
    risk_pct_allocated = Column(Float, nullable=True)       # What the allocator gave

    # Execution plan
    execution_mode = Column(String(20), nullable=True)      # ExecutionMode enum
    broker = Column(String(20), nullable=True)              # BrokerName enum
    fallback_used = Column(Boolean, nullable=False, default=False)

    # Link to resulting trade (if ACCEPT)
    trade_id = Column(String(36), nullable=True, index=True)

    __table_args__ = (
        Index("idx_sd_ts_symbol", "ts", "symbol"),
        Index("idx_sd_decision", "decision"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy": self.strategy,
            "side": self.side,
            "regime": self.regime,
            "decision": self.decision,
            "reason": self.reason,
            "rr_estimated": self.rr_estimated,
            "edge_score": self.edge_score,
            "risk_pct_requested": self.risk_pct_requested,
            "risk_pct_allocated": self.risk_pct_allocated,
            "execution_mode": self.execution_mode,
            "broker": self.broker,
            "fallback_used": self.fallback_used,
            "trade_id": self.trade_id,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 3 — trade_analytics (enriched fact table)
# ═══════════════════════════════════════════════════════════════════════════════

class TradeAnalytics(Base):
    """
    Enriched trade record with analytics-specific fields.
    Complements the existing live_trades table with MAE/MFE, regime context, etc.
    One row per completed trade.
    """
    __tablename__ = "trade_analytics"

    trade_id = Column(String(36), primary_key=True)

    # Core trade data (denormalized from live_trades for query independence)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    size = Column(Float, nullable=True)
    fees = Column(Float, nullable=True, default=0.0)
    sl_price = Column(Float, nullable=True)
    tp_price = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)

    # Analytics enrichment
    mae = Column(Float, nullable=True)           # Maximum Adverse Excursion
    mfe = Column(Float, nullable=True)           # Maximum Favorable Excursion
    duration_sec = Column(Integer, nullable=True)

    # Execution context
    execution_mode = Column(String(20), nullable=True)   # MAKER / TAKER / FALLBACK_TAKER
    broker = Column(String(20), nullable=True)
    adaptive_version = Column(String(10), nullable=True) # A / B / CONTROL

    # Regime context
    regime_at_entry = Column(String(20), nullable=True)
    regime_at_exit = Column(String(20), nullable=True)

    # Timestamps
    ts_entry = Column(DateTime, nullable=True, index=True)
    ts_exit = Column(DateTime, nullable=True)

    # Strategy
    strategy = Column(String(20), nullable=True)
    edge_score_at_entry = Column(Float, nullable=True)

    __table_args__ = (
        Index("idx_ta_symbol_ts", "symbol", "ts_entry"),
        Index("idx_ta_regime", "regime_at_entry"),
        Index("idx_ta_broker", "broker"),
    )

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "size": self.size,
            "fees": self.fees,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "pnl": self.pnl,
            "mae": self.mae,
            "mfe": self.mfe,
            "duration_sec": self.duration_sec,
            "execution_mode": self.execution_mode,
            "broker": self.broker,
            "adaptive_version": self.adaptive_version,
            "regime_at_entry": self.regime_at_entry,
            "regime_at_exit": self.regime_at_exit,
            "ts_entry": self.ts_entry.isoformat() if self.ts_entry else None,
            "ts_exit": self.ts_exit.isoformat() if self.ts_exit else None,
            "strategy": self.strategy,
            "edge_score_at_entry": self.edge_score_at_entry,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 4 — risk_events
# ═══════════════════════════════════════════════════════════════════════════════

class RiskEvent(Base):
    """
    Audit log for all risk-related events.
    Kill-switch triggers, drawdowns, recovery transitions, SLO breaches, chaos tests.
    """
    __tablename__ = "risk_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ts = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    event_type = Column(String(40), nullable=False, index=True)    # RiskEventType enum
    severity = Column(String(20), nullable=False, default="INFO")  # RiskSeverity enum

    # Flexible detail payload
    detail = Column(JSON, nullable=True)   # Arbitrary JSON context

    # Optional links
    symbol = Column(String(20), nullable=True)
    trade_id = Column(String(36), nullable=True)

    __table_args__ = (
        Index("idx_re_type_ts", "event_type", "ts"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "event_type": self.event_type,
            "severity": self.severity,
            "detail": self.detail,
            "symbol": self.symbol,
            "trade_id": self.trade_id,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 5 — daily_analytics (pre-computed for frontend)
# ═══════════════════════════════════════════════════════════════════════════════

class DailyAnalytics(Base):
    """
    Pre-computed daily aggregates. The frontend reads ONLY this table.
    Updated by the analytics aggregation service (nightly or rolling).

    Rule: the frontend NEVER recalculates PF, WR, DD, edge scores.
    """
    __tablename__ = "daily_analytics"

    date = Column(Date, primary_key=True)

    # Core performance
    pnl = Column(Float, nullable=False, default=0.0)
    profit_factor = Column(Float, nullable=True)
    winrate = Column(Float, nullable=True)             # 0.0–1.0
    expectancy = Column(Float, nullable=True)           # avg win * WR - avg loss * (1-WR)
    max_drawdown = Column(Float, nullable=True)
    total_trades = Column(Integer, nullable=False, default=0)
    wins = Column(Integer, nullable=False, default=0)
    losses = Column(Integer, nullable=False, default=0)

    # Edge & quality
    avg_edge_score = Column(Float, nullable=True)
    capital_efficiency = Column(Float, nullable=True)   # PnL / capital_deployed
    avg_rr = Column(Float, nullable=True)

    # Execution quality
    maker_count = Column(Integer, nullable=False, default=0)
    taker_count = Column(Integer, nullable=False, default=0)
    fallback_count = Column(Integer, nullable=False, default=0)
    avg_mae = Column(Float, nullable=True)
    avg_mfe = Column(Float, nullable=True)

    # Per-regime breakdown (JSON for flexibility)
    regime_breakdown = Column(JSON, nullable=True)
    # Example: {"TREND": {"pnl": 150, "trades": 5, "wr": 0.8}, "RANGE": {...}}

    # Per-broker breakdown
    broker_breakdown = Column(JSON, nullable=True)
    # Example: {"bitget": {"pnl": 100, "trades": 8}, "kraken": {"pnl": 50, "trades": 3}}

    # Signals analysis
    signals_total = Column(Integer, nullable=True, default=0)
    signals_accepted = Column(Integer, nullable=True, default=0)
    signals_rejected = Column(Integer, nullable=True, default=0)
    top_reject_reasons = Column(JSON, nullable=True)
    # Example: {"REGIME_BLOCKED": 12, "RR_TOO_LOW": 8, "SPREAD_TOO_WIDE": 3}

    # Execution blocked metrics (aggregated from execution_attempts)
    execution_blocked_total = Column(Integer, nullable=True, default=0)
    execution_blocked_by_reason = Column(JSON, nullable=True)
    # Example: {"GLOBAL_RISK": 5, "COOLDOWN_ACTIVE": 3, "KILL_SWITCH": 1}
    execution_dispatch_total = Column(Integer, nullable=True, default=0)
    execution_success_total = Column(Integer, nullable=True, default=0)

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat() if self.date else None,
            "pnl": self.pnl,
            "profit_factor": self.profit_factor,
            "winrate": self.winrate,
            "expectancy": self.expectancy,
            "max_drawdown": self.max_drawdown,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "avg_edge_score": self.avg_edge_score,
            "capital_efficiency": self.capital_efficiency,
            "avg_rr": self.avg_rr,
            "maker_count": self.maker_count,
            "taker_count": self.taker_count,
            "fallback_count": self.fallback_count,
            "avg_mae": self.avg_mae,
            "avg_mfe": self.avg_mfe,
            "regime_breakdown": self.regime_breakdown,
            "broker_breakdown": self.broker_breakdown,
            "signals_total": self.signals_total,
            "signals_accepted": self.signals_accepted,
            "signals_rejected": self.signals_rejected,
            "top_reject_reasons": self.top_reject_reasons,
            "execution_blocked_total": self.execution_blocked_total,
            "execution_blocked_by_reason": self.execution_blocked_by_reason,
            "execution_dispatch_total": self.execution_dispatch_total,
            "execution_success_total": self.execution_success_total,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 6 — weekly_analytics (optional higher granularity)
# ═══════════════════════════════════════════════════════════════════════════════

class WeeklyAnalytics(Base):
    """
    Pre-computed weekly aggregates for longer-term trend analysis.
    """
    __tablename__ = "weekly_analytics"

    # ISO year + week number as key (e.g. "2026-W09")
    week_key = Column(String(10), primary_key=True)
    week_start = Column(Date, nullable=False, index=True)

    pnl = Column(Float, nullable=False, default=0.0)
    profit_factor = Column(Float, nullable=True)
    winrate = Column(Float, nullable=True)
    expectancy = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    total_trades = Column(Integer, nullable=False, default=0)
    wins = Column(Integer, nullable=False, default=0)
    losses = Column(Integer, nullable=False, default=0)
    avg_edge_score = Column(Float, nullable=True)
    capital_efficiency = Column(Float, nullable=True)
    regime_breakdown = Column(JSON, nullable=True)
    broker_breakdown = Column(JSON, nullable=True)

    def to_dict(self) -> dict:
        return {
            "week_key": self.week_key,
            "week_start": self.week_start.isoformat() if self.week_start else None,
            "pnl": self.pnl,
            "profit_factor": self.profit_factor,
            "winrate": self.winrate,
            "expectancy": self.expectancy,
            "max_drawdown": self.max_drawdown,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "avg_edge_score": self.avg_edge_score,
            "capital_efficiency": self.capital_efficiency,
            "regime_breakdown": self.regime_breakdown,
            "broker_breakdown": self.broker_breakdown,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 7 — execution_attempts
# ═══════════════════════════════════════════════════════════════════════════════

class ExecutionAttempt(Base):
    """
    Every execution attempt that reaches the order manager.
    Captures dispatch → pre-flight → outcome for every signal
    that passes signal_builder/orchestrator and enters execution pipeline.
    
    Primary use: "Execution Blocked" panel showing WHY orders don't get placed.
    """
    __tablename__ = "execution_attempts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ts = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    # Signal identification
    symbol = Column(String(20), nullable=False, index=True)
    signal_id = Column(String(50), nullable=True)
    side = Column(String(10), nullable=True)          # long / short

    # Execution path
    broker = Column(String(20), nullable=True)          # BrokerName enum
    is_multi_broker = Column(Boolean, nullable=False, default=False)

    # Outcome
    status = Column(String(30), nullable=False, index=True)  # ExecutionAttemptStatus enum
    block_reason = Column(String(60), nullable=True)         # ExecutionBlockReason enum
    block_details = Column(JSON, nullable=True)              # Arbitrary context

    # Market context at attempt time
    entry_price = Column(Float, nullable=True)
    quantity = Column(Float, nullable=True)
    account_balance = Column(Float, nullable=True)
    regime = Column(String(20), nullable=True)

    # Result (only if EXECUTED)
    order_id = Column(String(50), nullable=True)
    fill_price = Column(Float, nullable=True)
    latency_ms = Column(Integer, nullable=True)       # dispatch → fill time

    __table_args__ = (
        Index("idx_ea_ts_status", "ts", "status"),
        Index("idx_ea_symbol_ts", "symbol", "ts"),
        Index("idx_ea_block_reason", "block_reason"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "symbol": self.symbol,
            "signal_id": self.signal_id,
            "side": self.side,
            "broker": self.broker,
            "is_multi_broker": self.is_multi_broker,
            "status": self.status,
            "block_reason": self.block_reason,
            "block_details": self.block_details,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "account_balance": self.account_balance,
            "regime": self.regime,
            "order_id": self.order_id,
            "fill_price": self.fill_price,
            "latency_ms": self.latency_ms,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 8 — ops_events
# ═══════════════════════════════════════════════════════════════════════════════

class OpsEvent(Base):
    """
    Operational events: WS disconnects, clock skew, latency spikes,
    API errors, broker timeouts, etc.
    
    Replaces log-grep for ops visibility.
    """
    __tablename__ = "ops_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ts = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    event_type = Column(String(40), nullable=False, index=True)    # OpsEventType enum
    severity = Column(String(20), nullable=False, default="INFO")  # OpsEventSeverity enum
    source = Column(String(50), nullable=True)                     # Module/component name
    message = Column(Text, nullable=True)                          # Human-readable description
    detail = Column(JSON, nullable=True)                           # Arbitrary JSON payload

    # Optional context
    symbol = Column(String(20), nullable=True)
    broker = Column(String(20), nullable=True)

    __table_args__ = (
        Index("idx_oe_type_ts", "event_type", "ts"),
        Index("idx_oe_severity", "severity"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "event_type": self.event_type,
            "severity": self.severity,
            "source": self.source,
            "message": self.message,
            "detail": self.detail,
            "symbol": self.symbol,
            "broker": self.broker,
        }
