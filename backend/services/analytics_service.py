"""
Analytics Aggregation Service — The Core Data Pipeline

This service is responsible for:
  1. Recording real-time events (snapshots, decisions, risk events)
  2. Computing daily/weekly aggregates from raw tables
  3. Providing query methods for the analytics API

Design principles:
  - The frontend NEVER calculates critical truths (PF, DD, WR, edge)
  - All aggregates are pre-computed and stored in daily_analytics
  - Raw tables are append-only; aggregates are upserted
  - Every method is safe to call (no exceptions propagated)

Author: SMC Trading Bot Framework — Analytics Pipeline
"""

import json
import logging
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, and_, cast, Date as SADate
from sqlalchemy.orm import Session

from backend.db.session import get_session_factory
from backend.services.log_rate_limit import rate_limited_warning
from backend.models.analytics import (
    DailyAnalytics,
    ExecutionAttempt,
    OpsEvent,
    RiskEvent,
    SignalDecisionRecord,
    SystemStateSnapshot,
    TradeAnalytics,
    WeeklyAnalytics,
)
from backend.models.analytics_enums import (
    MarketRegime,
    RiskEventType,
    RiskSeverity,
    SignalDecision,
)

logger = logging.getLogger("analytics")


def _sanitize_regime_breakdown(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Normalize regime_breakdown value to a dict.

    This field is stored as JSON in the DB, but downstream code may insert
    invalid values (str, list, etc.). This helper ensures we never write a
    bad payload and that aggregation code can safely iterate it.
    """
    if raw is None:
        return {}

    if isinstance(raw, dict):
        return raw

    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    rate_limited_warning(
        logger,
        "[ANALYTICS] Invalid regime_breakdown type=%s, treating as empty",
        type(raw),
    )
    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# CONTEXT MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

_custom_session_factory = None


def set_analytics_session_factory(session_factory=None) -> None:
    """Explicitly inject or clear the session factory for AnalyticsRecorder (tests/DI)."""
    global _custom_session_factory
    _custom_session_factory = session_factory


def _get_session(session_factory=None) -> Session:
    """Create a new database session from injected or global factory."""
    factory = session_factory or _custom_session_factory or get_session_factory()
    return factory()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. RECORDING — Real-time event capture
# ═══════════════════════════════════════════════════════════════════════════════

class AnalyticsRecorder:
    """
    Records events into the analytics tables.
    Called by the trading engine at runtime.
    All methods are static & safe (log errors, never raise).
    """

    @staticmethod
    def record_system_snapshot(
        regime: str,
        risk_factor: float = 1.0,
        mode: str = "LIVE",
        kill_switch_active: bool = False,
        volatility_pause_active: bool = False,
        recovery_step: int = 0,
        adaptive_weight: Optional[float] = None,
        capital_utilization: Optional[float] = None,
        regime_confidence: Optional[float] = None,
        open_positions: int = 0,
        daily_pnl: float = 0.0,
        equity: Optional[float] = None,
    ) -> Optional[str]:
        """Record a system state snapshot. Returns the snapshot ID."""
        try:
            db = _get_session()
            try:
                snap = SystemStateSnapshot(
                    id=str(uuid.uuid4()),
                    ts=datetime.now(timezone.utc),
                    regime=regime,
                    regime_confidence=regime_confidence,
                    risk_factor=risk_factor,
                    adaptive_weight=adaptive_weight,
                    capital_utilization=capital_utilization,
                    mode=mode,
                    kill_switch_active=kill_switch_active,
                    volatility_pause_active=volatility_pause_active,
                    recovery_step=recovery_step,
                    open_positions=open_positions,
                    daily_pnl=daily_pnl,
                    equity=equity,
                )
                db.add(snap)
                db.commit()
                logger.debug(f"[ANALYTICS] Snapshot recorded: regime={regime}, mode={mode}")
                return snap.id
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[ANALYTICS] Failed to record snapshot: {e}")
            return None

    @staticmethod
    def record_signal_decision(
        symbol: str,
        decision: str,
        reason: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        side: Optional[str] = None,
        regime: Optional[str] = None,
        regime_confidence: Optional[float] = None,
        rr_estimated: Optional[float] = None,
        edge_score: Optional[float] = None,
        risk_pct_requested: Optional[float] = None,
        risk_pct_allocated: Optional[float] = None,
        execution_mode: Optional[str] = None,
        broker: Optional[str] = None,
        fallback_used: bool = False,
        trade_id: Optional[str] = None,
    ) -> Optional[str]:
        """Record a signal evaluation (accepted or rejected). Returns the decision ID."""
        try:
            db = _get_session()
            try:
                record = SignalDecisionRecord(
                    id=str(uuid.uuid4()),
                    ts=datetime.now(timezone.utc),
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy=strategy,
                    side=side,
                    regime=regime,
                    regime_confidence=regime_confidence,
                    decision=decision,
                    reason=reason,
                    rr_estimated=rr_estimated,
                    edge_score=edge_score,
                    risk_pct_requested=risk_pct_requested,
                    risk_pct_allocated=risk_pct_allocated,
                    execution_mode=execution_mode,
                    broker=broker,
                    fallback_used=fallback_used,
                    trade_id=trade_id,
                )
                db.add(record)
                db.commit()
                logger.debug(
                    f"[ANALYTICS] Signal decision: {symbol} {decision}"
                    + (f" reason={reason}" if reason else "")
                )
                return record.id
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[ANALYTICS] Failed to record signal decision: {e}")
            return None

    @staticmethod
    def record_trade_analytics(
        trade_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: Optional[float] = None,
        size: Optional[float] = None,
        fees: float = 0.0,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        pnl: Optional[float] = None,
        mae: Optional[float] = None,
        mfe: Optional[float] = None,
        duration_sec: Optional[int] = None,
        execution_mode: Optional[str] = None,
        broker: Optional[str] = None,
        adaptive_version: Optional[str] = None,
        regime_at_entry: Optional[str] = None,
        regime_at_exit: Optional[str] = None,
        ts_entry: Optional[datetime] = None,
        ts_exit: Optional[datetime] = None,
        strategy: Optional[str] = None,
        edge_score_at_entry: Optional[float] = None,
    ) -> bool:
        """Record enriched trade analytics. Returns True on success."""
        try:
            db = _get_session()
            try:
                # Upsert: update if trade_id exists, insert otherwise
                existing = db.query(TradeAnalytics).filter_by(trade_id=trade_id).first()
                if existing:
                    # Update with new data
                    for attr, val in {
                        "exit_price": exit_price,
                        "pnl": pnl,
                        "mae": mae,
                        "mfe": mfe,
                        "duration_sec": duration_sec,
                        "regime_at_exit": regime_at_exit,
                        "ts_exit": ts_exit,
                        "fees": fees,
                    }.items():
                        if val is not None:
                            setattr(existing, attr, val)
                    db.commit()
                    logger.debug(f"[ANALYTICS] Trade analytics updated: {trade_id}")
                else:
                    record = TradeAnalytics(
                        trade_id=trade_id,
                        symbol=symbol,
                        side=side,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        size=size,
                        fees=fees,
                        sl_price=sl_price,
                        tp_price=tp_price,
                        pnl=pnl,
                        mae=mae,
                        mfe=mfe,
                        duration_sec=duration_sec,
                        execution_mode=execution_mode,
                        broker=broker,
                        adaptive_version=adaptive_version,
                        regime_at_entry=regime_at_entry,
                        regime_at_exit=regime_at_exit,
                        ts_entry=ts_entry or datetime.now(timezone.utc),
                        ts_exit=ts_exit,
                        strategy=strategy,
                        edge_score_at_entry=edge_score_at_entry,
                    )
                    db.add(record)
                    db.commit()
                    logger.debug(f"[ANALYTICS] Trade analytics recorded: {trade_id}")
                return True
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[ANALYTICS] Failed to record trade analytics: {e}")
            return False

    @staticmethod
    def record_risk_event(
        event_type: str,
        severity: str = "INFO",
        detail: Optional[dict] = None,
        symbol: Optional[str] = None,
        trade_id: Optional[str] = None,
    ) -> Optional[str]:
        """Record a risk event. Returns the event ID."""
        try:
            db = _get_session()
            try:
                event = RiskEvent(
                    id=str(uuid.uuid4()),
                    ts=datetime.now(timezone.utc),
                    event_type=event_type,
                    severity=severity,
                    detail=json.dumps(detail) if detail else None,
                    symbol=symbol,
                    trade_id=trade_id,
                )
                db.add(event)
                db.commit()
                logger.info(
                    f"[ANALYTICS] Risk event: {event_type} [{severity}]"
                    + (f" symbol={symbol}" if symbol else "")
                )
                return event.id
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[ANALYTICS] Failed to record risk event: {e}")
            return None

    @staticmethod
    def record_execution_attempt(
        symbol: str,
        status: str,
        block_reason: Optional[str] = None,
        block_details: Optional[dict] = None,
        signal_id: Optional[str] = None,
        side: Optional[str] = None,
        broker: Optional[str] = None,
        is_multi_broker: bool = False,
        entry_price: Optional[float] = None,
        quantity: Optional[float] = None,
        account_balance: Optional[float] = None,
        regime: Optional[str] = None,
        order_id: Optional[str] = None,
        fill_price: Optional[float] = None,
        latency_ms: Optional[int] = None,
    ) -> Optional[str]:
        """Record an execution attempt (dispatch/pre-flight/result). Returns the attempt ID."""
        try:
            db = _get_session()
            try:
                record = ExecutionAttempt(
                    id=str(uuid.uuid4()),
                    ts=datetime.now(timezone.utc),
                    symbol=symbol,
                    signal_id=signal_id,
                    side=side,
                    broker=broker,
                    is_multi_broker=is_multi_broker,
                    status=status,
                    block_reason=block_reason,
                    block_details=json.dumps(block_details) if block_details else None,
                    entry_price=entry_price,
                    quantity=quantity,
                    account_balance=account_balance,
                    regime=regime,
                    order_id=order_id,
                    fill_price=fill_price,
                    latency_ms=latency_ms,
                )
                db.add(record)
                db.commit()
                logger.debug(
                    f"[ANALYTICS] Execution attempt: {symbol} {status}"
                    + (f" reason={block_reason}" if block_reason else "")
                )
                return record.id
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[ANALYTICS] Failed to record execution attempt: {e}")
            return None

    @staticmethod
    def record_ops_event(
        event_type: str,
        severity: str = "INFO",
        source: Optional[str] = None,
        message: Optional[str] = None,
        detail: Optional[dict] = None,
        symbol: Optional[str] = None,
        broker: Optional[str] = None,
    ) -> Optional[str]:
        """Record an operational event. Returns the event ID."""
        try:
            db = _get_session()
            try:
                event = OpsEvent(
                    id=str(uuid.uuid4()),
                    ts=datetime.now(timezone.utc),
                    event_type=event_type,
                    severity=severity,
                    source=source,
                    message=message,
                    detail=json.dumps(detail) if detail else None,
                    symbol=symbol,
                    broker=broker,
                )
                db.add(event)
                db.commit()
                logger.debug(
                    f"[ANALYTICS] Ops event: {event_type} [{severity}]"
                    + (f" src={source}" if source else "")
                )
                return event.id
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[ANALYTICS] Failed to record ops event: {e}")
            return None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. AGGREGATION — Pre-compute daily/weekly analytics
# ═══════════════════════════════════════════════════════════════════════════════

class AnalyticsAggregator:
    """
    Computes and stores daily/weekly analytics aggregates.
    Called by:
      - A nightly cron/schedule
      - On-demand via API for backfill
      - Rolling update after each trade closes
    """

    @staticmethod
    def aggregate_day(target_date: date) -> Optional[dict]:
        """
        Compute and upsert daily analytics for a given date.
        Returns the computed metrics dict or None on failure.
        """
        try:
            db = _get_session()
            try:
                start_dt = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=timezone.utc)
                end_dt = datetime.combine(target_date, datetime.max.time()).replace(tzinfo=timezone.utc)

                # ── Trades ────────────────────────────────────────────
                trades = db.query(TradeAnalytics).filter(
                    and_(
                        TradeAnalytics.ts_exit >= start_dt,
                        TradeAnalytics.ts_exit <= end_dt,
                        TradeAnalytics.pnl.isnot(None),
                    )
                ).all()

                total_trades = len(trades)
                wins = [t for t in trades if t.pnl and t.pnl > 0]
                losses = [t for t in trades if t.pnl and t.pnl < 0]
                breakevens = [t for t in trades if t.pnl is not None and t.pnl == 0]

                total_pnl = sum(t.pnl for t in trades if t.pnl)
                win_count = len(wins)
                loss_count = len(losses)

                # Win rate
                decisive = win_count + loss_count
                winrate = win_count / decisive if decisive > 0 else None

                # Profit factor
                gross_profit = sum(t.pnl for t in wins if t.pnl > 0)
                gross_loss = abs(sum(t.pnl for t in losses if t.pnl < 0))
                profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
                    gross_profit if gross_profit > 0 else None
                )

                # Expectancy
                avg_win = gross_profit / win_count if win_count > 0 else 0
                avg_loss = gross_loss / loss_count if loss_count > 0 else 0
                expectancy = None
                if winrate is not None:
                    expectancy = avg_win * winrate - avg_loss * (1 - winrate)

                # Max drawdown (cumulative)
                sorted_trades = sorted(trades, key=lambda t: t.ts_exit or start_dt)
                cum_pnl = 0.0
                peak = 0.0
                max_dd = 0.0
                for t in sorted_trades:
                    cum_pnl += (t.pnl or 0)
                    peak = max(peak, cum_pnl)
                    dd = peak - cum_pnl
                    max_dd = max(max_dd, dd)

                # Average RR & edge score
                rr_values = [t.pnl / abs(t.entry_price - t.sl_price) if t.sl_price and t.entry_price != t.sl_price else None for t in trades]
                rr_values = [r for r in rr_values if r is not None]
                avg_rr = sum(rr_values) / len(rr_values) if rr_values else None

                edge_scores = [t.edge_score_at_entry for t in trades if t.edge_score_at_entry is not None]
                avg_edge = sum(edge_scores) / len(edge_scores) if edge_scores else None

                # MAE/MFE averages
                maes = [t.mae for t in trades if t.mae is not None]
                mfes = [t.mfe for t in trades if t.mfe is not None]
                avg_mae = sum(maes) / len(maes) if maes else None
                avg_mfe = sum(mfes) / len(mfes) if mfes else None

                # Execution mode breakdown
                maker_count = sum(1 for t in trades if t.execution_mode and "MAKER" in t.execution_mode.upper() and "FALLBACK" not in t.execution_mode.upper())
                taker_count = sum(1 for t in trades if t.execution_mode and t.execution_mode.upper() == "TAKER")
                fallback_count = sum(1 for t in trades if t.execution_mode and "FALLBACK" in t.execution_mode.upper())

                # Regime breakdown
                regime_data = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
                for t in trades:
                    regime = t.regime_at_entry or "UNKNOWN"
                    regime_data[regime]["trades"] += 1
                    regime_data[regime]["pnl"] += (t.pnl or 0)
                    if t.pnl and t.pnl > 0:
                        regime_data[regime]["wins"] += 1
                for regime in regime_data:
                    n = regime_data[regime]["trades"]
                    regime_data[regime]["wr"] = regime_data[regime]["wins"] / n if n > 0 else 0

                # Broker breakdown
                broker_data = defaultdict(lambda: {"pnl": 0, "trades": 0})
                for t in trades:
                    b = t.broker or "unknown"
                    broker_data[b]["trades"] += 1
                    broker_data[b]["pnl"] += (t.pnl or 0)

                # ── Signals ───────────────────────────────────────────
                signals = db.query(SignalDecisionRecord).filter(
                    and_(
                        SignalDecisionRecord.ts >= start_dt,
                        SignalDecisionRecord.ts <= end_dt,
                    )
                ).all()

                signals_total = len(signals)
                signals_accepted = sum(1 for s in signals if s.decision == "ACCEPT")
                signals_rejected = sum(1 for s in signals if s.decision == "REJECT")

                # Top reject reasons
                reject_reasons = defaultdict(int)
                for s in signals:
                    if s.decision == "REJECT" and s.reason:
                        reject_reasons[s.reason] += 1
                top_reasons = dict(sorted(reject_reasons.items(), key=lambda x: x[1], reverse=True)[:10])

                # ── Capital efficiency ────────────────────────────────
                # Use last snapshot of the day if available
                last_snap = db.query(SystemStateSnapshot).filter(
                    and_(
                        SystemStateSnapshot.ts >= start_dt,
                        SystemStateSnapshot.ts <= end_dt,
                    )
                ).order_by(SystemStateSnapshot.ts.desc()).first()

                equity = last_snap.equity if last_snap and last_snap.equity else None
                capital_efficiency = total_pnl / equity if equity and equity > 0 else None

                # ── Upsert ────────────────────────────────────────────
                existing = db.query(DailyAnalytics).filter_by(date=target_date).first()
                if existing:
                    row = existing
                else:
                    row = DailyAnalytics(date=target_date)
                    db.add(row)

                row.pnl = total_pnl
                row.profit_factor = profit_factor
                row.winrate = winrate
                row.expectancy = expectancy
                row.max_drawdown = max_dd if max_dd > 0 else None
                row.total_trades = total_trades
                row.wins = win_count
                row.losses = loss_count
                row.avg_edge_score = avg_edge
                row.capital_efficiency = capital_efficiency
                row.avg_rr = avg_rr
                row.maker_count = maker_count
                row.taker_count = taker_count
                row.fallback_count = fallback_count
                row.avg_mae = avg_mae
                row.avg_mfe = avg_mfe
                sanitized = _sanitize_regime_breakdown(regime_data)
                try:
                    row.regime_breakdown = json.dumps(sanitized) if sanitized else None
                except Exception as e:
                    logger.warning(
                        "[ANALYTICS] Failed to serialize regime_breakdown, dropping: %s", e
                    )
                    row.regime_breakdown = None
                row.broker_breakdown = json.dumps(dict(broker_data)) if broker_data else None
                row.signals_total = signals_total
                row.signals_accepted = signals_accepted
                row.signals_rejected = signals_rejected
                row.top_reject_reasons = json.dumps(top_reasons) if top_reasons else None

                # ── Execution blocked ─────────────────────────────
                exec_attempts = db.query(ExecutionAttempt).filter(
                    and_(
                        ExecutionAttempt.ts >= start_dt,
                        ExecutionAttempt.ts <= end_dt,
                    )
                ).all()

                blocked = [a for a in exec_attempts if a.status == "PRE_FLIGHT_REJECT"]
                dispatched = [a for a in exec_attempts if a.status in ("DISPATCH_STARTED", "PRE_FLIGHT_PASS", "EXECUTED")]
                succeeded = [a for a in exec_attempts if a.status == "EXECUTED"]

                blocked_by_reason = defaultdict(int)
                for a in blocked:
                    blocked_by_reason[a.block_reason or "UNKNOWN"] += 1

                row.execution_blocked_total = len(blocked)
                row.execution_blocked_by_reason = json.dumps(dict(sorted(blocked_by_reason.items(), key=lambda x: x[1], reverse=True))) if blocked_by_reason else None
                row.execution_dispatch_total = len(dispatched)
                row.execution_success_total = len(succeeded)

                db.commit()
                logger.info(
                    f"[ANALYTICS] Day aggregated: {target_date} | "
                    f"PnL={total_pnl:.2f} | trades={total_trades} | WR={winrate or 0:.1%}"
                )
                return row.to_dict()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[ANALYTICS] Failed to aggregate day {target_date}: {e}", exc_info=True)
            return None

    @staticmethod
    def aggregate_week(iso_year: int, iso_week: int) -> Optional[dict]:
        """
        Compute and upsert weekly analytics.
        iso_year/iso_week follow ISO week numbering.
        """
        try:
            db = _get_session()
            try:
                week_key = f"{iso_year}-W{iso_week:02d}"
                # Monday of the ISO week
                week_start = datetime.strptime(f"{iso_year}-W{iso_week:02d}-1", "%G-W%V-%u").date()
                week_end = week_start + timedelta(days=6)

                # Collect daily analytics for this week
                days = db.query(DailyAnalytics).filter(
                    and_(
                        DailyAnalytics.date >= week_start,
                        DailyAnalytics.date <= week_end,
                    )
                ).all()

                if not days:
                    return None

                total_pnl = sum(d.pnl for d in days)
                total_trades = sum(d.total_trades for d in days)
                total_wins = sum(d.wins for d in days)
                total_losses = sum(d.losses for d in days)

                decisive = total_wins + total_losses
                winrate = total_wins / decisive if decisive > 0 else None

                edge_scores = [d.avg_edge_score for d in days if d.avg_edge_score is not None]
                avg_edge = sum(edge_scores) / len(edge_scores) if edge_scores else None

                # Profit factor from daily totals
                gross_profit = sum(d.pnl for d in days if d.pnl > 0)
                gross_loss = abs(sum(d.pnl for d in days if d.pnl < 0))
                profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
                    gross_profit if gross_profit > 0 else None
                )

                # Max DD across the week
                max_dd = max((d.max_drawdown or 0) for d in days)

                # Expectancy
                avg_win = gross_profit / total_wins if total_wins > 0 else 0
                avg_loss = gross_loss / total_losses if total_losses > 0 else 0
                expectancy = None
                if winrate is not None:
                    expectancy = avg_win * winrate - avg_loss * (1 - winrate)

                # Capital efficiency (average across days)
                ce_vals = [d.capital_efficiency for d in days if d.capital_efficiency is not None]
                capital_efficiency = sum(ce_vals) / len(ce_vals) if ce_vals else None

                # Merge regime breakdowns
                merged_regime: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
                for d in days:
                    rd = _sanitize_regime_breakdown(d.regime_breakdown)
                    for regime, data in rd.items():
                        merged_regime[regime]["pnl"] += data.get("pnl", 0)
                        merged_regime[regime]["trades"] += data.get("trades", 0)
                        merged_regime[regime]["wins"] += data.get("wins", 0)

                # Merge broker breakdowns
                merged_broker: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"pnl": 0, "trades": 0})
                for d in days:
                    bd = _sanitize_regime_breakdown(d.broker_breakdown)
                    for broker, data in bd.items():
                        merged_broker[broker]["pnl"] += data.get("pnl", 0)
                        merged_broker[broker]["trades"] += data.get("trades", 0)

                # Upsert
                existing = db.query(WeeklyAnalytics).filter_by(week_key=week_key).first()
                if existing:
                    row = existing
                else:
                    row = WeeklyAnalytics(week_key=week_key, week_start=week_start)
                    db.add(row)

                row.pnl = total_pnl
                row.profit_factor = profit_factor
                row.winrate = winrate
                row.expectancy = expectancy
                row.max_drawdown = max_dd if max_dd > 0 else None
                row.total_trades = total_trades
                row.wins = total_wins
                row.losses = total_losses
                row.avg_edge_score = avg_edge
                row.capital_efficiency = capital_efficiency
                row.regime_breakdown = json.dumps(dict(merged_regime)) if merged_regime else None
                row.broker_breakdown = json.dumps(dict(merged_broker)) if merged_broker else None

                db.commit()
                logger.info(f"[ANALYTICS] Week aggregated: {week_key} | PnL={total_pnl:.2f} | trades={total_trades}")
                return row.to_dict()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[ANALYTICS] Failed to aggregate week: {e}", exc_info=True)
            return None

    @staticmethod
    def backfill(start_date: date, end_date: date) -> int:
        """
        Backfill daily analytics for a date range.
        Returns the number of days processed.
        """
        count = 0
        current = start_date
        while current <= end_date:
            result = AnalyticsAggregator.aggregate_day(current)
            if result:
                count += 1
            current += timedelta(days=1)

        # Also aggregate weeks that overlap
        current = start_date
        processed_weeks = set()
        while current <= end_date:
            iso_year, iso_week, _ = current.isocalendar()
            wk = (iso_year, iso_week)
            if wk not in processed_weeks:
                AnalyticsAggregator.aggregate_week(iso_year, iso_week)
                processed_weeks.add(wk)
            current += timedelta(days=1)

        logger.info(f"[ANALYTICS] Backfill complete: {count} days, {len(processed_weeks)} weeks")
        return count

    @staticmethod
    def aggregate_today() -> Optional[dict]:
        """Convenience method to aggregate today's data."""
        return AnalyticsAggregator.aggregate_day(date.today())

    @staticmethod
    def reconcile_from_live_trades(target_date: Optional[date] = None) -> int:
        """
        Reconcile trade_analytics from live_trades (source of truth).
        
        For each CLOSED live_trade with pnl, ensure a matching trade_analytics
        row exists with correct exit data. This fixes divergence between the
        Dashboard (live_trades) and DataExplorer (trade_analytics → daily_analytics).
        
        Returns count of records synced.
        """
        try:
            from backend.models.live_trade import LiveTrade
            db = _get_session()
            try:
                query = db.query(LiveTrade).filter(
                    LiveTrade.status == "CLOSED",
                    LiveTrade.pnl.isnot(None),
                )
                if target_date:
                    start_dt = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=timezone.utc)
                    end_dt = datetime.combine(target_date, datetime.max.time()).replace(tzinfo=timezone.utc)
                    query = query.filter(
                        LiveTrade.ts_close >= start_dt,
                        LiveTrade.ts_close <= end_dt,
                    )

                closed_trades = query.all()
                synced = 0

                for lt in closed_trades:
                    trade_id = lt.order_id or lt.id
                    existing = db.query(TradeAnalytics).filter_by(trade_id=trade_id).first()

                    if existing:
                        # Update exit data if missing
                        if existing.pnl is None and lt.pnl is not None:
                            existing.pnl = lt.pnl
                            existing.exit_price = lt.close_price
                            existing.ts_exit = lt.ts_close
                            synced += 1
                    else:
                        # Create missing trade_analytics row
                        ta = TradeAnalytics(
                            trade_id=trade_id,
                            symbol=lt.symbol,
                            side=lt.side or "unknown",
                            entry_price=lt.entry or 0,
                            exit_price=lt.close_price,
                            size=lt.qty,
                            fees=lt.fee or 0,
                            sl_price=lt.stop_loss,
                            tp_price=lt.take_profit,
                            pnl=lt.pnl,
                            broker=lt.broker,
                            ts_entry=lt.ts_open,
                            ts_exit=lt.ts_close,
                        )
                        db.add(ta)
                        synced += 1

                if synced > 0:
                    db.commit()
                    logger.info(f"[RECONCILE] Synced {synced} trades from live_trades → trade_analytics")

                return synced
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[RECONCILE] Failed: {e}", exc_info=True)
            return 0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. QUERY — Read-only methods for the API layer
# ═══════════════════════════════════════════════════════════════════════════════

class AnalyticsQuery:
    """
    Read-only query methods for the analytics API.
    Returns clean dicts ready for JSON serialization.
    """

    @staticmethod
    def get_overview(from_date: date, to_date: date) -> dict:
        """
        Aggregate overview for a date range.
        Used by: GET /api/analytics/overview
        """
        db = _get_session()
        try:
            days = db.query(DailyAnalytics).filter(
                and_(DailyAnalytics.date >= from_date, DailyAnalytics.date <= to_date)
            ).order_by(DailyAnalytics.date).all()

            if not days:
                return {
                    "period": {"from": from_date.isoformat(), "to": to_date.isoformat()},
                    "pnl": 0, "profit_factor": None, "winrate": None,
                    "total_trades": 0, "wins": 0, "losses": 0,
                    "max_drawdown": None, "expectancy": None,
                    "days_count": 0, "equity_curve": [],
                }

            total_pnl = sum(d.pnl for d in days)
            total_trades = sum(d.total_trades for d in days)
            total_wins = sum(d.wins for d in days)
            total_losses = sum(d.losses for d in days)

            decisive = total_wins + total_losses
            winrate = total_wins / decisive if decisive > 0 else None

            gross_profit = sum(d.pnl for d in days if d.pnl > 0)
            gross_loss = abs(sum(d.pnl for d in days if d.pnl < 0))
            pf = gross_profit / gross_loss if gross_loss > 0 else None

            max_dd = max((d.max_drawdown or 0) for d in days) if days else None

            # Equity curve from daily PnL
            equity_curve = []
            cum = 0
            for d in days:
                cum += d.pnl
                equity_curve.append({"date": d.date.isoformat(), "pnl": d.pnl, "cumulative": cum})

            return {
                "period": {"from": from_date.isoformat(), "to": to_date.isoformat()},
                "pnl": total_pnl,
                "profit_factor": pf,
                "winrate": winrate,
                "total_trades": total_trades,
                "wins": total_wins,
                "losses": total_losses,
                "max_drawdown": max_dd,
                "days_count": len(days),
                "equity_curve": equity_curve,
            }
        finally:
            db.close()

    @staticmethod
    def get_regime_history(from_date: date, to_date: date, limit: int = 500) -> list:
        """
        Regime state timeline.
        Used by: GET /api/analytics/regime-history
        """
        db = _get_session()
        try:
            start_dt = datetime.combine(from_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            end_dt = datetime.combine(to_date, datetime.max.time()).replace(tzinfo=timezone.utc)

            snaps = db.query(SystemStateSnapshot).filter(
                and_(SystemStateSnapshot.ts >= start_dt, SystemStateSnapshot.ts <= end_dt)
            ).order_by(SystemStateSnapshot.ts).limit(limit).all()

            return [s.to_dict() for s in snaps]
        finally:
            db.close()

    @staticmethod
    def get_decision_breakdown(from_date: date, to_date: date) -> dict:
        """
        Signal decision analytics.
        Used by: GET /api/analytics/decision-breakdown
        """
        db = _get_session()
        try:
            start_dt = datetime.combine(from_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            end_dt = datetime.combine(to_date, datetime.max.time()).replace(tzinfo=timezone.utc)

            signals = db.query(SignalDecisionRecord).filter(
                and_(SignalDecisionRecord.ts >= start_dt, SignalDecisionRecord.ts <= end_dt)
            ).all()

            total = len(signals)
            accepted = [s for s in signals if s.decision == "ACCEPT"]
            rejected = [s for s in signals if s.decision == "REJECT"]

            # Rejection reasons breakdown
            reasons = defaultdict(int)
            for s in rejected:
                reasons[s.reason or "UNKNOWN"] += 1

            # Per-symbol breakdown
            symbol_stats = defaultdict(lambda: {"total": 0, "accepted": 0, "rejected": 0})
            for s in signals:
                sym = s.symbol
                symbol_stats[sym]["total"] += 1
                if s.decision == "ACCEPT":
                    symbol_stats[sym]["accepted"] += 1
                else:
                    symbol_stats[sym]["rejected"] += 1

            # Per-regime acceptance rate
            regime_acceptance = defaultdict(lambda: {"total": 0, "accepted": 0})
            for s in signals:
                r = s.regime or "UNKNOWN"
                regime_acceptance[r]["total"] += 1
                if s.decision == "ACCEPT":
                    regime_acceptance[r]["accepted"] += 1
            for r in regime_acceptance:
                n = regime_acceptance[r]["total"]
                regime_acceptance[r]["rate"] = regime_acceptance[r]["accepted"] / n if n > 0 else 0

            return {
                "total_signals": total,
                "accepted": len(accepted),
                "rejected": len(rejected),
                "acceptance_rate": len(accepted) / total if total > 0 else 0,
                "reject_reasons": dict(sorted(reasons.items(), key=lambda x: x[1], reverse=True)),
                "by_symbol": dict(symbol_stats),
                "by_regime": dict(regime_acceptance),
            }
        finally:
            db.close()

    @staticmethod
    def get_execution_quality(from_date: date, to_date: date) -> dict:
        """
        Execution quality analytics (maker/taker, MAE/MFE, brokers).
        Used by: GET /api/analytics/execution-quality
        """
        db = _get_session()
        try:
            start_dt = datetime.combine(from_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            end_dt = datetime.combine(to_date, datetime.max.time()).replace(tzinfo=timezone.utc)

            trades = db.query(TradeAnalytics).filter(
                and_(
                    TradeAnalytics.ts_entry >= start_dt,
                    TradeAnalytics.ts_entry <= end_dt,
                )
            ).all()

            if not trades:
                return {"total_trades": 0}

            # Execution mode distribution
            modes = defaultdict(int)
            for t in trades:
                modes[t.execution_mode or "UNKNOWN"] += 1

            # Broker stats
            broker_stats = defaultdict(lambda: {"trades": 0, "pnl": 0, "avg_fees": 0, "fees_total": 0})
            for t in trades:
                b = t.broker or "unknown"
                broker_stats[b]["trades"] += 1
                broker_stats[b]["pnl"] += (t.pnl or 0)
                broker_stats[b]["fees_total"] += (t.fees or 0)
            for b in broker_stats:
                n = broker_stats[b]["trades"]
                broker_stats[b]["avg_fees"] = broker_stats[b]["fees_total"] / n if n > 0 else 0

            # MAE/MFE stats
            maes = [t.mae for t in trades if t.mae is not None]
            mfes = [t.mfe for t in trades if t.mfe is not None]
            durations = [t.duration_sec for t in trades if t.duration_sec is not None]

            # A/B testing breakdown
            ab_stats = defaultdict(lambda: {"trades": 0, "pnl": 0, "wins": 0})
            for t in trades:
                ver = t.adaptive_version or "NONE"
                ab_stats[ver]["trades"] += 1
                ab_stats[ver]["pnl"] += (t.pnl or 0)
                if t.pnl and t.pnl > 0:
                    ab_stats[ver]["wins"] += 1
            for ver in ab_stats:
                n = ab_stats[ver]["trades"]
                ab_stats[ver]["wr"] = ab_stats[ver]["wins"] / n if n > 0 else 0

            return {
                "total_trades": len(trades),
                "execution_modes": dict(modes),
                "broker_stats": dict(broker_stats),
                "mae": {
                    "avg": sum(maes) / len(maes) if maes else None,
                    "min": min(maes) if maes else None,
                    "max": max(maes) if maes else None,
                },
                "mfe": {
                    "avg": sum(mfes) / len(mfes) if mfes else None,
                    "min": min(mfes) if mfes else None,
                    "max": max(mfes) if mfes else None,
                },
                "duration": {
                    "avg_sec": sum(durations) / len(durations) if durations else None,
                    "min_sec": min(durations) if durations else None,
                    "max_sec": max(durations) if durations else None,
                },
                "ab_testing": dict(ab_stats),
            }
        finally:
            db.close()

    @staticmethod
    def get_risk_events(
        from_date: date,
        to_date: date,
        event_type: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        """
        Risk events log.
        Used by: GET /api/analytics/risk-events
        """
        db = _get_session()
        try:
            start_dt = datetime.combine(from_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            end_dt = datetime.combine(to_date, datetime.max.time()).replace(tzinfo=timezone.utc)

            query = db.query(RiskEvent).filter(
                and_(RiskEvent.ts >= start_dt, RiskEvent.ts <= end_dt)
            )
            if event_type:
                query = query.filter(RiskEvent.event_type == event_type)
            if severity:
                query = query.filter(RiskEvent.severity == severity)

            events = query.order_by(RiskEvent.ts.desc()).limit(limit).all()

            result = []
            for e in events:
                d = e.to_dict()
                # Parse JSON detail if stored as string
                if d.get("detail") and isinstance(d["detail"], str):
                    try:
                        d["detail"] = json.loads(d["detail"])
                    except json.JSONDecodeError:
                        pass
                result.append(d)
            return result
        finally:
            db.close()

    @staticmethod
    def get_daily_series(from_date: date, to_date: date) -> list:
        """
        Daily analytics series for charting.
        Used by: GET /api/analytics/daily-series
        """
        db = _get_session()
        try:
            days = db.query(DailyAnalytics).filter(
                and_(DailyAnalytics.date >= from_date, DailyAnalytics.date <= to_date)
            ).order_by(DailyAnalytics.date).all()

            return [d.to_dict() for d in days]
        finally:
            db.close()

    @staticmethod
    def get_weekly_series(from_date: date, to_date: date) -> list:
        """
        Weekly analytics series.
        Used by: GET /api/analytics/weekly-series
        """
        db = _get_session()
        try:
            weeks = db.query(WeeklyAnalytics).filter(
                and_(WeeklyAnalytics.week_start >= from_date, WeeklyAnalytics.week_start <= to_date)
            ).order_by(WeeklyAnalytics.week_start).all()

            return [w.to_dict() for w in weeks]
        finally:
            db.close()

    @staticmethod
    def get_execution_blocked(from_date: date, to_date: date, limit: int = 500) -> dict:
        """
        Execution blocked analysis — the "Execution Blocked" panel data.
        Used by: GET /api/analytics/execution-blocked
        """
        db = _get_session()
        try:
            start_dt = datetime.combine(from_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            end_dt = datetime.combine(to_date, datetime.max.time()).replace(tzinfo=timezone.utc)

            attempts = db.query(ExecutionAttempt).filter(
                and_(ExecutionAttempt.ts >= start_dt, ExecutionAttempt.ts <= end_dt)
            ).order_by(ExecutionAttempt.ts.desc()).limit(limit).all()

            total = len(attempts)
            blocked = [a for a in attempts if a.status == "PRE_FLIGHT_REJECT"]
            passed = [a for a in attempts if a.status in ("PRE_FLIGHT_PASS", "EXECUTED")]
            dispatched = [a for a in attempts if a.status == "DISPATCH_STARTED"]

            # By reason
            by_reason = defaultdict(int)
            for a in blocked:
                by_reason[a.block_reason or "UNKNOWN"] += 1

            # By symbol
            by_symbol = defaultdict(lambda: {"blocked": 0, "passed": 0, "total": 0})
            for a in attempts:
                sym = a.symbol
                by_symbol[sym]["total"] += 1
                if a.status == "PRE_FLIGHT_REJECT":
                    by_symbol[sym]["blocked"] += 1
                elif a.status in ("PRE_FLIGHT_PASS", "EXECUTED"):
                    by_symbol[sym]["passed"] += 1

            # Timeline (daily breakdown)
            daily_blocked = defaultdict(lambda: {"blocked": 0, "passed": 0, "total": 0})
            for a in attempts:
                day = a.ts.strftime("%Y-%m-%d") if a.ts else "unknown"
                daily_blocked[day]["total"] += 1
                if a.status == "PRE_FLIGHT_REJECT":
                    daily_blocked[day]["blocked"] += 1
                elif a.status in ("PRE_FLIGHT_PASS", "EXECUTED"):
                    daily_blocked[day]["passed"] += 1

            # Recent blocked events (for table)
            recent_blocked = [a.to_dict() for a in blocked[:50]]

            return {
                "total_attempts": total,
                "blocked": len(blocked),
                "passed": len(passed),
                "dispatched": len(dispatched),
                "block_rate": len(blocked) / total if total > 0 else 0,
                "by_reason": dict(sorted(by_reason.items(), key=lambda x: x[1], reverse=True)),
                "by_symbol": dict(by_symbol),
                "daily_timeline": dict(sorted(daily_blocked.items())),
                "recent_blocked": recent_blocked,
            }
        finally:
            db.close()

    @staticmethod
    def get_ops_events(
        from_date: date,
        to_date: date,
        event_type: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        """
        Ops events log.
        Used by: GET /api/analytics/ops-events
        """
        db = _get_session()
        try:
            start_dt = datetime.combine(from_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            end_dt = datetime.combine(to_date, datetime.max.time()).replace(tzinfo=timezone.utc)

            query = db.query(OpsEvent).filter(
                and_(OpsEvent.ts >= start_dt, OpsEvent.ts <= end_dt)
            )
            if event_type:
                query = query.filter(OpsEvent.event_type == event_type)
            if severity:
                query = query.filter(OpsEvent.severity == severity)

            events = query.order_by(OpsEvent.ts.desc()).limit(limit).all()

            result = []
            for e in events:
                d = e.to_dict()
                if d.get("detail") and isinstance(d["detail"], str):
                    try:
                        d["detail"] = json.loads(d["detail"])
                    except json.JSONDecodeError:
                        pass
                result.append(d)
            return result
        finally:
            db.close()
