"""
Analytics API Routes — Read-Only Endpoints for the Frontend

The frontend NEVER queries raw tables. It consumes only:
  - Pre-computed daily/weekly aggregates
  - Filtered/paginated views from the analytics service

Endpoints:
  GET /api/analytics/overview            — Period overview (PnL, WR, PF, DD)
  GET /api/analytics/daily-series        — Daily chart data
  GET /api/analytics/weekly-series       — Weekly chart data
  GET /api/analytics/regime-history      — System state timeline
  GET /api/analytics/decision-breakdown  — Signal accept/reject analysis
  GET /api/analytics/execution-quality   — Maker/taker, MAE/MFE, brokers
  GET /api/analytics/risk-events         — Risk event log
  POST /api/analytics/aggregate          — Trigger aggregation (admin)

Author: SMC Trading Bot Framework — Analytics Pipeline
"""

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query, HTTPException

from backend.services.analytics_service import (
    AnalyticsAggregator,
    AnalyticsQuery,
)

logger = logging.getLogger("analytics.api")
router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# ═══════════════════════════════════════════════════════════════════════════════
# 1. OVERVIEW — Period summary
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/overview")
def analytics_overview(
    from_: date = Query(..., alias="from", description="Start date (YYYY-MM-DD)"),
    to: date = Query(..., description="End date (YYYY-MM-DD)"),
):
    """
    Aggregated overview for a date range.
    Returns: PnL, profit_factor, winrate, max_drawdown, equity_curve.
    Source: daily_analytics table (pre-computed, NOT calculated live).
    """
    if from_ > to:
        raise HTTPException(400, "from must be <= to")
    return AnalyticsQuery.get_overview(from_, to)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. DAILY SERIES — Chart data
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/daily-series")
def analytics_daily_series(
    from_: date = Query(..., alias="from", description="Start date"),
    to: date = Query(..., description="End date"),
):
    """
    Daily analytics series for line/bar charts.
    Each day includes PnL, WR, PF, trades, edge score, etc.
    Source: daily_analytics table.
    """
    if from_ > to:
        raise HTTPException(400, "from must be <= to")
    return AnalyticsQuery.get_daily_series(from_, to)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. WEEKLY SERIES
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/weekly-series")
def analytics_weekly_series(
    from_: date = Query(..., alias="from", description="Start date"),
    to: date = Query(..., description="End date"),
):
    """Weekly analytics series."""
    if from_ > to:
        raise HTTPException(400, "from must be <= to")
    return AnalyticsQuery.get_weekly_series(from_, to)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. REGIME HISTORY — System state timeline
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/regime-history")
def analytics_regime_history(
    from_: date = Query(..., alias="from", description="Start date"),
    to: date = Query(..., description="End date"),
    limit: int = Query(500, ge=1, le=5000),
):
    """
    System state snapshots timeline.
    Shows regime transitions, risk factor changes, kill switch events.
    Source: system_state_snapshots table.
    """
    if from_ > to:
        raise HTTPException(400, "from must be <= to")
    return AnalyticsQuery.get_regime_history(from_, to, limit)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. DECISION BREAKDOWN — Signal accept/reject analysis
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/decision-breakdown")
def analytics_decision_breakdown(
    from_: date = Query(..., alias="from", description="Start date"),
    to: date = Query(..., description="End date"),
):
    """
    Signal decision analysis: acceptance rates, rejection reasons,
    per-symbol and per-regime breakdowns.
    Source: signal_decisions table.
    """
    if from_ > to:
        raise HTTPException(400, "from must be <= to")
    return AnalyticsQuery.get_decision_breakdown(from_, to)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. EXECUTION QUALITY — Maker/taker, MAE/MFE, brokers
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/execution-quality")
def analytics_execution_quality(
    from_: date = Query(..., alias="from", description="Start date"),
    to: date = Query(..., description="End date"),
):
    """
    Execution quality metrics: maker vs taker distribution,
    MAE/MFE averages, broker performance, A/B testing results.
    Source: trade_analytics table.
    """
    if from_ > to:
        raise HTTPException(400, "from must be <= to")
    return AnalyticsQuery.get_execution_quality(from_, to)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. RISK EVENTS — Risk event log
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/risk-events")
def analytics_risk_events(
    from_: date = Query(..., alias="from", description="Start date"),
    to: date = Query(..., description="End date"),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    severity: Optional[str] = Query(None, description="Filter by severity"),
    limit: int = Query(100, ge=1, le=1000),
):
    """
    Risk events log with optional filters.
    Source: risk_events table.
    """
    if from_ > to:
        raise HTTPException(400, "from must be <= to")
    return AnalyticsQuery.get_risk_events(from_, to, event_type, severity, limit)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. TRIGGER AGGREGATION — Admin endpoint
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/aggregate")
def trigger_aggregation(
    from_: date = Query(None, alias="from", description="Start date (default: today)"),
    to: date = Query(None, description="End date (default: today)"),
):
    """
    Manually trigger analytics aggregation for a date range.
    If no dates given, aggregates today.
    Admin use only.
    """
    if from_ is None:
        from_ = date.today()
    if to is None:
        to = date.today()
    if from_ > to:
        raise HTTPException(400, "from must be <= to")

    count = AnalyticsAggregator.backfill(from_, to)
    return {
        "status": "ok",
        "days_processed": count,
        "from": from_.isoformat(),
        "to": to.isoformat(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/aggregate-today")
def trigger_aggregate_today():
    """Quick endpoint to aggregate today's data."""
    result = AnalyticsAggregator.aggregate_today()
    return {
        "status": "ok" if result else "no_data",
        "data": result,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 8a. RECONCILE — Sync trade_analytics from live_trades (source of truth)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/reconcile")
def reconcile_analytics(
    from_: date = Query(None, alias="from", description="Start date (optional)"),
    to: date = Query(None, description="End date (optional)"),
):
    """
    Reconcile trade_analytics from live_trades then re-aggregate.
    Fixes divergence between Dashboard (live_trades) and DataExplorer (daily_analytics).
    Admin use only.
    """
    synced = AnalyticsAggregator.reconcile_from_live_trades()
    agg_count = 0
    if synced > 0:
        start = from_ or date(2024, 1, 1)
        end = to or date.today()
        agg_count = AnalyticsAggregator.backfill(start, end)
    return {
        "status": "ok",
        "trades_synced": synced,
        "days_reaggregated": agg_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 8b. PURGE FAILED TRADES — Admin cleanup
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/purge-failed")
def purge_failed_trades():
    """
    Remove all FAILED trades from live_trades and positions tables,
    then re-aggregate daily analytics. Admin use only.
    """
    from backend.services.live_trades_service import LiveTradesService
    from backend.services.positions_service import PersistedPositionsService
    trades_removed = LiveTradesService.purge_failed_trades()
    positions_removed = PersistedPositionsService.purge_failed_positions()
    # Re-aggregate to flush stale counts
    agg_count = AnalyticsAggregator.backfill(date(2024, 1, 1), date.today())
    return {
        "status": "ok",
        "failed_trades_removed": trades_removed,
        "failed_positions_removed": positions_removed,
        "days_reaggregated": agg_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 9. EXECUTION BLOCKED — "Why orders didn't get placed" panel
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/execution-blocked")
def analytics_execution_blocked(
    from_: date = Query(..., alias="from", description="Start date (YYYY-MM-DD)"),
    to: date = Query(..., description="End date (YYYY-MM-DD)"),
    limit: int = Query(500, ge=1, le=5000),
):
    """
    Execution blocked analysis: block rates, reasons breakdown,
    per-symbol stats, daily timeline, recent blocked events.
    Source: execution_attempts table.
    """
    if from_ > to:
        raise HTTPException(400, "from must be <= to")
    return AnalyticsQuery.get_execution_blocked(from_, to, limit)


# ═══════════════════════════════════════════════════════════════════════════════
# 10. OPS EVENTS — Operational event log
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/ops-events")
def analytics_ops_events(
    from_: date = Query(..., alias="from", description="Start date"),
    to: date = Query(..., description="End date"),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    severity: Optional[str] = Query(None, description="Filter by severity"),
    limit: int = Query(100, ge=1, le=1000),
):
    """
    Operational events log: WS disconnects, latency spikes, API errors, etc.
    Source: ops_events table.
    """
    if from_ > to:
        raise HTTPException(400, "from must be <= to")
    return AnalyticsQuery.get_ops_events(from_, to, event_type, severity, limit)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 6 — PERFORMANCE GOVERNOR (Real-time Production Dashboards)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/performance-governor")
def performance_governor_snapshot():
    """
    Real-time production dashboard snapshot.

    Returns 4 dashboards in one payload:
      - rejection_dashboard:  % by filter, symbol, session, hard/soft
      - execution_quality:    slippage avg/p95/max, fill delay, retry rate, reconciler
      - strategy_health:      trades, rejects, winrate/PF by regime and strategy
      - shadow_analytics:     cost of each protection on frequency and PF
    """
    try:
        from smc_futures.observability.performance_governor import governor
        return governor.snapshot()
    except ImportError:
        raise HTTPException(503, "PerformanceGovernor not available")


@router.get("/rejection-dashboard")
def rejection_dashboard():
    """Rejection dashboard only — subset of performance-governor."""
    try:
        from smc_futures.observability.performance_governor import governor
        snap = governor.snapshot()
        return snap["rejection_dashboard"]
    except ImportError:
        raise HTTPException(503, "PerformanceGovernor not available")


@router.get("/execution-quality-live")
def execution_quality_live():
    """Real-time execution quality — subset of performance-governor."""
    try:
        from smc_futures.observability.performance_governor import governor
        snap = governor.snapshot()
        return snap["execution_quality"]
    except ImportError:
        raise HTTPException(503, "PerformanceGovernor not available")


@router.get("/strategy-health")
def strategy_health():
    """Strategy health dashboard — subset of performance-governor."""
    try:
        from smc_futures.observability.performance_governor import governor
        snap = governor.snapshot()
        return snap["strategy_health"]
    except ImportError:
        raise HTTPException(503, "PerformanceGovernor not available")


@router.get("/shadow-analytics")
def shadow_analytics():
    """Shadow mode analytics — cost of each protection filter."""
    try:
        from smc_futures.observability.performance_governor import governor
        snap = governor.snapshot()
        return snap["shadow_analytics"]
    except ImportError:
        raise HTTPException(503, "PerformanceGovernor not available")


@router.get("/adaptive-risk")
def adaptive_risk_dashboard():
    """Phase 7 — Adaptive risk engine decisions and distribution."""
    try:
        from smc_futures.observability.performance_governor import governor
        snap = governor.snapshot()
        return snap["adaptive_risk"]
    except ImportError:
        raise HTTPException(503, "PerformanceGovernor not available")


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 8 — OPERATOR DASHBOARD (12 tuiles consolidées)
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_get(d: dict, *keys, default=None):
    """Safely traverse nested dicts."""
    current = d
    for k in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(k)
        if current is None:
            return default
    return current


@router.get("/operator-dashboard")
def operator_dashboard():
    """
    Consolidated operator dashboard — 12 key tiles in a single response.

    Aggregates data from performance-governor snapshot and governance
    endpoints into a single payload optimised for operational monitoring.
    """
    try:
        from smc_futures.observability.performance_governor import governor
        snap = governor.snapshot()
    except ImportError:
        raise HTTPException(503, "PerformanceGovernor not available")

    rej = snap.get("rejection_dashboard", {})
    exe = snap.get("execution_quality", {})
    strat = snap.get("strategy_health", {})
    are = snap.get("adaptive_risk", {})
    slip = exe.get("slippage", {})
    delay = exe.get("fill_delay", {})
    recon = exe.get("reconciler", {})
    emerg = exe.get("emergency_close", {})
    are_modes = are.get("by_mode", {})
    are_mult = are.get("multiplier", {})

    total_are = sum(are_modes.values()) if are_modes else 0
    blocked_count = are_modes.get("blocked", 0)

    return {
        "generated_at": snap.get("generated_at"),
        "tiles": {
            "rejection_rate_pct": rej.get("rejection_rate_pct", 0.0),
            "slippage_p95_bps": slip.get("p95", 0.0),
            "fill_delay_p95_ms": delay.get("p95", 0.0),
            "orphans": recon.get("orphans_detected", 0),
            "ghosts": recon.get("ghosts_cleaned", 0),
            "emergency_close_failures": emerg.get("failures", 0),
            "are_blocked_pct": round(
                blocked_count / total_are * 100, 1
            ) if total_are > 0 else 0.0,
            "are_multiplier_mean": are_mult.get("mean", 1.0),
            "trades_today": strat.get("trades_opened", 0),
            "winrate_pct": strat.get("winrate_pct", 0.0),
            "pnl_today": strat.get("pnl", 0.0),
            "are_mode_distribution": dict(are_modes),
        },
        "thresholds": {
            "rejection_rate_pct": {"green": 50, "orange": 80},
            "slippage_p95_bps": {"green": 20, "orange": 40},
            "fill_delay_p95_ms": {"green": 1000, "orange": 2000},
            "orphans": {"green": 0, "orange": 1},
            "ghosts": {"green": 0, "orange": 0},
            "emergency_close_failures": {"green": 0, "orange": 0},
            "are_blocked_pct": {"green": 15, "orange": 40},
            "are_multiplier_mean": {"green": 0.80, "orange": 0.50},
            "winrate_pct": {"green": 45, "orange": 35},
        },
    }
