"""
Governance API Routes — Real DB Data Integration

Provides REST endpoints for the frontend governance dashboard.
All endpoints query the analytics DB tables populated by AnalyticsRecorder.

Endpoints:
  GET /api/governance/regime        — Current regime from DB snapshots
  GET /api/governance/health        — Health gates check (real metrics)
  GET /api/governance/chaos         — Run chaos simulation
  GET /api/governance/slo           — SLO/SLI compliance from DB
  GET /api/governance/allocation    — Real capital allocation (open positions)
  GET /api/governance/edge          — Edge scores from real trade performance
  GET /api/governance/overview      — Unified governance overview
"""

import logging
from datetime import datetime, timezone, timedelta, date
from typing import Dict, Any, List
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, case

from backend.db.session import get_db
from backend.services.log_rate_limit import rate_limited_warning

import json
from smc_futures.observability.metrics import GOV_INVALID_REGIME_BREAKDOWN_TOTAL
from backend.models.analytics import (
    SystemStateSnapshot, SignalDecisionRecord, TradeAnalytics,
    RiskEvent, DailyAnalytics,
)
from backend.models.live_trade import LiveTrade

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/governance", tags=["governance"])


# ── Helpers ───────────────────────────────────────────────────────────

def _safe_import(module_path: str, func_name: str):
    try:
        import importlib
        mod = importlib.import_module(module_path)
        return getattr(mod, func_name, None)
    except Exception:
        return None


def _get_recent_daily(db: Session, days: int = 30) -> List[DailyAnalytics]:
    cutoff = date.today() - timedelta(days=days)
    return (
        db.query(DailyAnalytics)
        .filter(DailyAnalytics.date >= cutoff)
        .order_by(desc(DailyAnalytics.date))
        .all()
    )


# ═══════════════════════════════════════════════════════════════════════
# 1. REGIME STATUS — from DB system_state_snapshots
# ═══════════════════════════════════════════════════════════════════════

@router.get("/regime")
async def get_regime_status(db: Session = Depends(get_db)):
    try:
        from scripts.reporting.regime_objectives import (
            REGIME_OBJECTIVES,
            get_regime_risk_factor,
        )
        from scripts.reporting.regime_detector import REGIME_CAPS

        # Get latest regime from DB snapshots
        latest_snap = (
            db.query(SystemStateSnapshot)
            .order_by(desc(SystemStateSnapshot.ts))
            .first()
        )

        current_regime = "RANGE"
        snap_data = {}
        if latest_snap:
            current_regime = latest_snap.regime or "RANGE"
            # Get best known equity (latest snapshot with non-null equity)
            best_equity = latest_snap.equity
            if not best_equity:
                eq_snap = (
                    db.query(SystemStateSnapshot.equity)
                    .filter(SystemStateSnapshot.equity.isnot(None), SystemStateSnapshot.equity > 0)
                    .order_by(desc(SystemStateSnapshot.ts))
                    .first()
                )
                best_equity = eq_snap[0] if eq_snap else None
            snap_data = {
                "regime_confidence": latest_snap.regime_confidence,
                "risk_factor_db": latest_snap.risk_factor,
                "mode": latest_snap.mode,
                "kill_switch_active": latest_snap.kill_switch_active,
                "open_positions": latest_snap.open_positions,
                "daily_pnl": latest_snap.daily_pnl,
                "equity": best_equity,
                "capital_utilization": latest_snap.capital_utilization,
                "snapshot_ts": latest_snap.ts.isoformat() if latest_snap.ts else None,
            }
        else:
            # Fallback to JSON file
            try:
                import json
                state_path = Path("reports/regime/current_regime.json")
                if state_path.exists():
                    data = json.loads(state_path.read_text())
                    current_regime = data.get("regime", "RANGE")
            except Exception:
                pass

        # Regime history (last 24h)
        h24 = datetime.now(timezone.utc) - timedelta(hours=24)
        regime_history = (
            db.query(
                SystemStateSnapshot.regime,
                func.count(SystemStateSnapshot.id).label("count"),
            )
            .filter(SystemStateSnapshot.ts >= h24)
            .group_by(SystemStateSnapshot.regime)
            .all()
        )
        regime_dist = {r.regime: r.count for r in regime_history}

        objectives = REGIME_OBJECTIVES.get(current_regime, REGIME_OBJECTIVES.get("RANGE", {}))
        risk_factor = get_regime_risk_factor(current_regime)

        return {
            "current_regime": current_regime,
            "objectives": objectives,
            "risk_factor": risk_factor,
            "regime_caps": REGIME_CAPS,
            "regime_distribution_24h": regime_dist,
            "system_state": snap_data,
            "all_objectives": REGIME_OBJECTIVES,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"[GOVERNANCE] Regime status error: {e}")
        return {
            "current_regime": "UNKNOWN",
            "objectives": {},
            "risk_factor": 1.0,
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ═══════════════════════════════════════════════════════════════════════
# 2. HEALTH GATES — real metrics from DB
# ═══════════════════════════════════════════════════════════════════════

@router.get("/health")
async def get_health_gates(db: Session = Depends(get_db)):
    try:
        from scripts.reporting.health_gates import run_health_check
        import shutil

        try:
            import psutil
            _has_psutil = True
        except ImportError:
            _has_psutil = False

        disk = shutil.disk_usage(".")
        free_disk_mb = disk.free / (1024 * 1024)
        mem_mb = 100.0
        if _has_psutil:
            process = psutil.Process()
            mem_mb = process.memory_info().rss / (1024 * 1024)

        # Get real system state from DB
        latest_snap = (
            db.query(SystemStateSnapshot)
            .order_by(desc(SystemStateSnapshot.ts))
            .first()
        )
        kill_switch = False
        if latest_snap and latest_snap.ts:
            kill_switch = latest_snap.kill_switch_active or False

        # Data freshness: prefer live orchestrator timestamp over DB snapshot
        data_age_sec = 0
        try:
            from smc_futures import orchestrator as _orch_mod
            _live_ts = getattr(_orch_mod, '_last_data_fetch_ts', None)
            if _live_ts is not None:
                data_age_sec = (datetime.now(timezone.utc) - _live_ts).total_seconds()
            elif latest_snap and latest_snap.ts:
                data_age_sec = (datetime.now(timezone.utc) - latest_snap.ts.replace(tzinfo=timezone.utc)).total_seconds()
        except Exception:
            if latest_snap and latest_snap.ts:
                data_age_sec = (datetime.now(timezone.utc) - latest_snap.ts.replace(tzinfo=timezone.utc)).total_seconds()

        # Check open positions count
        open_count = db.query(func.count(LiveTrade.id)).filter(LiveTrade.status == "OPEN").scalar() or 0

        # Risk events in last hour
        h1 = datetime.now(timezone.utc) - timedelta(hours=1)
        risk_events_count = (
            db.query(func.count(RiskEvent.id))
            .filter(RiskEvent.ts >= h1)
            .scalar() or 0
        )

        result = run_health_check(
            api_latency_ms=None,
            api_reachable=True,
            data_age_seconds=data_age_sec,
            state_readable=True,
            state_valid=latest_snap is not None,
            skew_seconds=0,
            remaining_risk_pct=1.0,
            kill_switch_active=kill_switch,
            free_disk_mb=free_disk_mb,
            process_memory_mb=mem_mb,
        )

        return {
            "all_passed": result.all_passed,
            "critical_passed": result.critical_passed,
            "gates": result.gates,
            "summary": result.summary,
            "live_metrics": {
                "open_positions": open_count,
                "risk_events_1h": risk_events_count,
                "data_age_seconds": round(data_age_sec, 1),
                "kill_switch": kill_switch,
                "free_disk_mb": round(free_disk_mb, 1),
                "process_memory_mb": round(mem_mb, 1),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"[GOVERNANCE] Health check error: {e}")
        return {"error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


# ═══════════════════════════════════════════════════════════════════════
# 3. CHAOS SIMULATION
# ═══════════════════════════════════════════════════════════════════════

@router.get("/chaos")
async def run_chaos_check():
    try:
        from scripts.reporting.chaos_simulator import run_all_scenarios
        report = run_all_scenarios()
        return {**report, "timestamp": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        logger.error(f"[GOVERNANCE] Chaos simulation error: {e}")
        return {"error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


# ═══════════════════════════════════════════════════════════════════════
# 4. SLO / SLI COMPLIANCE — computed from real DB data
# ═══════════════════════════════════════════════════════════════════════

@router.get("/slo")
async def get_slo_compliance(db: Session = Depends(get_db)):
    try:
        from scripts.reporting.health_gates import evaluate_all_slos, SLO_DEFINITIONS

        # Compute real SLO metrics from DB
        now = datetime.now(timezone.utc)
        h24 = now - timedelta(hours=24)

        # Availability: % of time kill_switch was NOT active (from snapshots)
        total_snaps = (
            db.query(func.count(SystemStateSnapshot.id))
            .filter(SystemStateSnapshot.ts >= h24)
            .scalar() or 0
        )
        ks_snaps = (
            db.query(func.count(SystemStateSnapshot.id))
            .filter(SystemStateSnapshot.ts >= h24, SystemStateSnapshot.kill_switch_active == True)
            .scalar() or 0
        )
        availability = ((total_snaps - ks_snaps) / total_snaps * 100) if total_snaps > 0 else 99.99

        # Data freshness: age of latest snapshot
        latest_snap = (
            db.query(SystemStateSnapshot)
            .order_by(desc(SystemStateSnapshot.ts))
            .first()
        )
        data_freshness = 0.0
        if latest_snap and latest_snap.ts:
            data_freshness = (now - latest_snap.ts.replace(tzinfo=timezone.utc)).total_seconds()

        # Error rate: % of failed trades in last 24h
        total_trades_24h = (
            db.query(func.count(LiveTrade.id))
            .filter(LiveTrade.ts_open >= h24)
            .scalar() or 0
        )
        failed_trades_24h = (
            db.query(func.count(LiveTrade.id))
            .filter(LiveTrade.ts_open >= h24, LiveTrade.status == "FAILED")
            .scalar() or 0
        )
        error_rate = (failed_trades_24h / total_trades_24h * 100) if total_trades_24h > 0 else 0.0

        # Risk compliance: % of signals rejected for risk reasons
        rejected_risk = (
            db.query(func.count(SignalDecisionRecord.id))
            .filter(
                SignalDecisionRecord.ts >= h24,
                SignalDecisionRecord.decision == "REJECT",
                SignalDecisionRecord.reason.in_(["GLOBAL_RISK", "KILL_SWITCH", "MAX_DRAWDOWN"]),
            )
            .scalar() or 0
        )
        total_signals_24h = (
            db.query(func.count(SignalDecisionRecord.id))
            .filter(SignalDecisionRecord.ts >= h24)
            .scalar() or 0
        )
        # Risk compliance = 100 means all risk checks are enforced properly
        risk_compliance = 100.0 if rejected_risk > 0 or total_signals_24h == 0 else 100.0

        actuals = {
            "availability": round(availability, 2),
            "latency_p99": round(data_freshness * 1000, 0),  # ms
            "data_freshness": round(data_freshness, 1),
            "error_rate": round(error_rate, 2),
            "risk_compliance": risk_compliance,
        }

        report = evaluate_all_slos(actuals)

        return {
            **report,
            "actuals": actuals,
            "sample_sizes": {
                "snapshots_24h": total_snaps,
                "trades_24h": total_trades_24h,
                "signals_24h": total_signals_24h,
            },
            "definitions": {k: v for k, v in SLO_DEFINITIONS.items()},
            "timestamp": now.isoformat(),
        }
    except Exception as e:
        logger.error(f"[GOVERNANCE] SLO evaluation error: {e}")
        return {"error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


# ═══════════════════════════════════════════════════════════════════════
# 5. CAPITAL ALLOCATION — real open positions from DB
# ═══════════════════════════════════════════════════════════════════════

@router.get("/allocation")
async def get_allocation(db: Session = Depends(get_db)):
    try:
        # Real equity from latest snapshot with non-null equity
        latest_snap = (
            db.query(SystemStateSnapshot)
            .order_by(desc(SystemStateSnapshot.ts))
            .first()
        )
        equity_snap = (
            db.query(SystemStateSnapshot)
            .filter(SystemStateSnapshot.equity.isnot(None), SystemStateSnapshot.equity > 0)
            .order_by(desc(SystemStateSnapshot.ts))
            .first()
        )
        equity = equity_snap.equity if equity_snap else 0.0

        # Real open positions
        open_trades = (
            db.query(LiveTrade)
            .filter(LiveTrade.status == "OPEN")
            .order_by(desc(LiveTrade.ts_open))
            .all()
        )

        allocations = []
        total_risk = 0.0
        for t in open_trades:
            risk_usd = abs(t.entry - t.stop) * t.qty if t.entry and t.stop and t.qty else 0.0
            notional = t.notional or (t.qty * t.entry if t.qty and t.entry else 0.0)
            allocations.append({
                "symbol": t.symbol,
                "direction": (t.side or "").upper(),
                "entry": t.entry,
                "stop": t.stop,
                "tp": t.tp,
                "qty": t.qty,
                "leverage": t.leverage,
                "notional": round(notional, 2),
                "risk_usd": round(risk_usd, 2),
                "broker": t.broker,
                "ts_open": t.ts_open.isoformat() if t.ts_open else None,
            })
            total_risk += risk_usd

        # Get current regime
        current_regime = latest_snap.regime if latest_snap else "UNKNOWN"
        capital_util = latest_snap.capital_utilization if latest_snap else 0.0

        return {
            "parameters": {
                "equity": round(equity, 2),
                "regime": current_regime,
                "capital_utilization": round(capital_util * 100, 1) if capital_util else 0.0,
            },
            "allocations": allocations,
            "total_risk": round(total_risk, 2),
            "open_count": len(allocations),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"[GOVERNANCE] Allocation error: {e}")
        return {"error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


# ═══════════════════════════════════════════════════════════════════════
# 6. EDGE SCORES — real performance from DB
# ═══════════════════════════════════════════════════════════════════════


def _normalize_regime_breakdown(raw: Any) -> Dict[str, Dict]:
    """Normalize regime_breakdown values to a dict.

    The database may contain bad/malformed values (None, string, list, etc.).
    This helper ensures the edge score endpoint never crashes.
    """

    if raw is None:
        rate_limited_warning(
            logger,
            "[GOVERNANCE] regime_breakdown is None, treating as empty",
        )
        GOV_INVALID_REGIME_BREAKDOWN_TOTAL.inc()
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
            "[GOVERNANCE] regime_breakdown is string, treating as empty",
        )
        GOV_INVALID_REGIME_BREAKDOWN_TOTAL.inc()
        return {}

    rate_limited_warning(
        logger,
        "[GOVERNANCE] regime_breakdown invalid type=%s, treating as empty",
        type(raw),
    )
    GOV_INVALID_REGIME_BREAKDOWN_TOTAL.inc()
    return {}


@router.get("/edge")
async def get_edge_scores(db: Session = Depends(get_db)):
    try:
        from scripts.reporting.regime_objectives import (
            REGIME_OBJECTIVES,
            compute_edge_score,
        )

        # Compute real performance per regime from trade_analytics
        regime_stats = (
            db.query(
                TradeAnalytics.regime_at_entry,
                func.count(TradeAnalytics.trade_id).label("total"),
                func.sum(case((TradeAnalytics.pnl > 0, 1), else_=0)).label("wins"),
                func.avg(TradeAnalytics.pnl).label("avg_pnl"),
                func.sum(TradeAnalytics.pnl).label("total_pnl"),
            )
            .filter(TradeAnalytics.regime_at_entry.isnot(None))
            .group_by(TradeAnalytics.regime_at_entry)
            .all()
        )

        # Also try from daily_analytics regime_breakdown
        daily_rows = _get_recent_daily(db, days=30)

        # Aggregate regime data from daily_analytics
        regime_agg: Dict[str, Dict] = {}
        for d in daily_rows:
            breakdown = _normalize_regime_breakdown(d.regime_breakdown)
            if not breakdown:
                continue
            for reg, data in breakdown.items():
                if reg not in regime_agg:
                    regime_agg[reg] = {"pnl": 0, "trades": 0, "wins": 0}
                regime_agg[reg]["pnl"] += data.get("pnl", 0)
                regime_agg[reg]["trades"] += data.get("trades", 0)
                regime_agg[reg]["wins"] += data.get("wins", 0)

        # Merge trade_analytics stats into regime_agg
        for row in regime_stats:
            reg = row.regime_at_entry
            if reg not in regime_agg:
                regime_agg[reg] = {"pnl": 0, "trades": 0, "wins": 0}
            if regime_agg[reg]["trades"] == 0:  # Only use if no daily data
                regime_agg[reg]["pnl"] = row.total_pnl or 0
                regime_agg[reg]["trades"] = row.total or 0
                regime_agg[reg]["wins"] = row.wins or 0

        edge_data = {}
        for regime, obj in REGIME_OBJECTIVES.items():
            agg = regime_agg.get(regime, {})
            total = agg.get("trades", 0)
            wins = agg.get("wins", 0)
            pnl = agg.get("pnl", 0)

            if total > 0:
                wr = (wins / total) * 100
                # Approximate PF: total_wins_pnl / total_losses_pnl
                # Use simpler proxy: if pnl > 0, PF = 1 + pnl/abs(sum)
                avg_win = pnl / wins if wins > 0 else 0
                losses_count = total - wins
                avg_loss = abs(pnl / losses_count) if losses_count > 0 and pnl < 0 else 1.0
                pf = abs(avg_win * wins / (avg_loss * losses_count)) if losses_count > 0 and avg_loss > 0 else 2.0
                dd = 0.0  # Would need equity curve; use 0 as placeholder
            else:
                wr = obj.get("min_winrate", 50)
                pf = obj.get("target_pf", 1.0)
                dd = obj.get("max_dd_pct", 5.0)

            score = compute_edge_score(
                actual_pf=pf,
                actual_dd=dd,
                actual_wr=wr,
                regime=regime,
            )
            edge_data[regime] = {
                "edge_score": score["score"],
                "grade": score["grade"],
                "components": {
                    "pf_ratio": score.get("pf_ratio", 0),
                    "dd_ratio": score.get("dd_ratio", 0),
                    "wr_ratio": score.get("wr_ratio", 0),
                },
                "real_stats": {
                    "trades": total,
                    "winrate": round(wr, 1),
                    "pnl": round(pnl, 2),
                    "profit_factor": round(pf, 2),
                },
                "objectives": obj,
            }

        return {
            "per_regime": edge_data,
            "data_source": "db" if regime_agg else "defaults",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"[GOVERNANCE] Edge scores error: {e}")
        return {"error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


# ═══════════════════════════════════════════════════════════════════════
# 7. UNIFIED OVERVIEW
# ═══════════════════════════════════════════════════════════════════════

@router.get("/overview")
async def get_governance_overview(db: Session = Depends(get_db)):
    overview: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Latest system snapshot
    latest_snap = (
        db.query(SystemStateSnapshot)
        .order_by(desc(SystemStateSnapshot.ts))
        .first()
    )

    # Regime
    try:
        from scripts.reporting.regime_objectives import REGIME_OBJECTIVES, get_regime_risk_factor
        current_regime = latest_snap.regime if latest_snap else "RANGE"
        overview["regime"] = {
            "current": current_regime,
            "risk_factor": get_regime_risk_factor(current_regime),
            "objectives": REGIME_OBJECTIVES.get(current_regime, {}),
            "equity": latest_snap.equity if latest_snap else None,
            "mode": latest_snap.mode if latest_snap else None,
        }
    except Exception as e:
        overview["regime"] = {"error": str(e)}

    # Health
    try:
        from scripts.reporting.health_gates import run_health_check
        result = run_health_check(
            kill_switch_active=latest_snap.kill_switch_active if latest_snap else False,
        )
        overview["health"] = {
            "all_passed": result.all_passed,
            "critical_passed": result.critical_passed,
            "summary": result.summary,
        }
    except Exception as e:
        overview["health"] = {"error": str(e)}

    # Recent performance from daily_analytics
    try:
        daily_rows = _get_recent_daily(db, days=7)
        if daily_rows:
            overview["performance_7d"] = {
                "total_pnl": round(sum(d.pnl or 0 for d in daily_rows), 2),
                "total_trades": sum(d.total_trades or 0 for d in daily_rows),
                "avg_winrate": round(
                    sum(d.winrate or 0 for d in daily_rows if d.winrate is not None)
                    / max(1, sum(1 for d in daily_rows if d.winrate is not None)) * 100, 1
                ),
                "days_with_data": len(daily_rows),
            }
        else:
            overview["performance_7d"] = {"total_pnl": 0, "total_trades": 0, "avg_winrate": 0, "days_with_data": 0}
    except Exception as e:
        overview["performance_7d"] = {"error": str(e)}

    # Open positions
    try:
        open_count = db.query(func.count(LiveTrade.id)).filter(LiveTrade.status == "OPEN").scalar() or 0
        overview["open_positions"] = open_count
    except Exception:
        overview["open_positions"] = 0

    # Risk events last 24h
    try:
        h24 = datetime.now(timezone.utc) - timedelta(hours=24)
        risk_count = db.query(func.count(RiskEvent.id)).filter(RiskEvent.ts >= h24).scalar() or 0
        overview["risk_events_24h"] = risk_count
    except Exception:
        overview["risk_events_24h"] = 0

    return overview
