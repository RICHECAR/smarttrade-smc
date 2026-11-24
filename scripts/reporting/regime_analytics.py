"""
Regime Analytics Tracker — Phase 10

Accumulates per-regime statistics over time to answer:
  - How much time is spent in each regime (TREND / RANGE / CHOP)?
  - What is the PnL, DD, win-rate per regime?
  - Which regime is responsible for most drawdown?

Design:
  - Pure functions, deterministic, fully testable
  - State-free: operates on lists of daily snapshots
  - Daily snapshots come from the JSON daily reports produced by DailyReporter
  - Intended for weekly / monthly governance review (not real-time)

Author: SMC Trading Bot Framework — Phase 10
"""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# ── Regime labels ─────────────────────────────────────────────────────
REGIMES = ("TREND", "RANGE", "CHOP", "RECOVERY")


def load_daily_snapshots(
    reports_dir: Path,
    from_date: date,
    to_date: date,
) -> List[Dict[str, Any]]:
    """
    Load daily report JSON files for the given date range.

    Each file is expected at:
        reports_dir / daily / daily_report_YYYYMMDD.json

    Returns:
        List of dicts, each with at minimum:
          - date (str)
          - data.performance  (trades, pnl, winrate, max_dd, profit_factor)
          - data.regime       (regime, effective_regime, ...)
          - data.capital_scaling
          - data.kill_switch
    """
    daily_dir = reports_dir / "daily"
    snapshots = []

    if not daily_dir.exists():
        logger.warning(f"[REGIME_ANALYTICS] No daily reports dir: {daily_dir}")
        return snapshots

    current = from_date
    while current <= to_date:
        filename = f"daily_report_{current.strftime('%Y%m%d')}.json"
        fpath = daily_dir / filename
        if fpath.exists():
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["_source_date"] = current.isoformat()
                snapshots.append(data)
            except Exception as e:
                logger.warning(f"[REGIME_ANALYTICS] Failed to load {fpath}: {e}")
        current += timedelta(days=1)

    return snapshots


def compute_regime_analytics(
    snapshots: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Aggregate per-regime statistics from a list of daily snapshots.

    Returns:
        Dict with:
          - total_days:    int
          - per_regime:    dict[regime_label] → {
                days, pct_time, total_pnl, avg_pnl,
                total_trades, avg_winrate, max_dd,
                avg_pf, avg_risk_factor,
                kill_switch_blocks, vol_pause_days, recovery_days
            }
          - best_regime:   str (highest avg PnL)
          - worst_regime:  str (lowest avg PnL)
          - regime_transitions: int
          - warnings:      list
    """
    total = len(snapshots)
    if total == 0:
        return _empty_analytics()

    # Accumulators per regime
    acc = {}
    for r in REGIMES:
        acc[r] = {
            "days": 0,
            "total_pnl": 0.0,
            "total_trades": 0,
            "winrate_sum": 0.0,
            "winrate_count": 0,
            "max_dd": 0.0,
            "pf_sum": 0.0,
            "pf_count": 0,
            "risk_factor_sum": 0.0,
            "risk_factor_count": 0,
            "ks_blocks": 0,
            "vol_pause_days": 0,
            "recovery_days": 0,
        }

    transitions = 0
    prev_regime = None
    warnings = []

    for snap in snapshots:
        data = snap.get("data", snap)
        perf = data.get("performance", {})
        regime_data = data.get("regime", {})
        ks = data.get("kill_switch", {})

        effective = regime_data.get("effective_regime", "RANGE")
        # Normalize: if it contains +, take the base for grouping
        if effective == "RECOVERY":
            regime_key = "RECOVERY"
        elif "VOL_PAUSE" in effective:
            # Count in the base regime but flag vol pause
            regime_key = effective.split("+")[0]
        else:
            regime_key = effective

        if regime_key not in acc:
            regime_key = "RANGE"  # fallback

        a = acc[regime_key]
        a["days"] += 1
        a["total_pnl"] += perf.get("pnl_abs", 0.0)
        a["total_trades"] += perf.get("n_trades", 0)

        wr = perf.get("winrate", 0.0)
        if perf.get("n_trades", 0) > 0:
            a["winrate_sum"] += wr
            a["winrate_count"] += 1

        dd = perf.get("max_dd", 0.0)
        a["max_dd"] = max(a["max_dd"], dd)

        pf = perf.get("profit_factor", 0.0)
        if perf.get("n_trades", 0) > 0:
            a["pf_sum"] += pf
            a["pf_count"] += 1

        rf = regime_data.get("risk_reduction_factor", 1.0)
        a["risk_factor_sum"] += rf
        a["risk_factor_count"] += 1

        a["ks_blocks"] += ks.get("block_count", 0)
        if regime_data.get("vol_pause_active", False):
            a["vol_pause_days"] += 1
        if regime_data.get("recovery_active", False):
            a["recovery_days"] += 1

        # Track transitions
        if prev_regime is not None and effective != prev_regime:
            transitions += 1
        prev_regime = effective

    # Build per-regime output
    per_regime = {}
    for r in REGIMES:
        a = acc[r]
        days = a["days"]
        per_regime[r] = {
            "days": days,
            "pct_time": round(days / total * 100, 1) if total > 0 else 0.0,
            "total_pnl": round(a["total_pnl"], 2),
            "avg_pnl": round(a["total_pnl"] / days, 2) if days > 0 else 0.0,
            "total_trades": a["total_trades"],
            "avg_winrate": round(a["winrate_sum"] / a["winrate_count"], 1) if a["winrate_count"] > 0 else 0.0,
            "max_dd": round(a["max_dd"], 2),
            "avg_pf": round(a["pf_sum"] / a["pf_count"], 2) if a["pf_count"] > 0 else 0.0,
            "avg_risk_factor": round(a["risk_factor_sum"] / a["risk_factor_count"], 4) if a["risk_factor_count"] > 0 else 1.0,
            "kill_switch_blocks": a["ks_blocks"],
            "vol_pause_days": a["vol_pause_days"],
            "recovery_days": a["recovery_days"],
        }

    # Best / worst regime by avg PnL (only those with data)
    active_regimes = {r: v for r, v in per_regime.items() if v["days"] > 0}
    best_regime = max(active_regimes, key=lambda r: active_regimes[r]["avg_pnl"]) if active_regimes else "N/A"
    worst_regime = min(active_regimes, key=lambda r: active_regimes[r]["avg_pnl"]) if active_regimes else "N/A"

    # Warnings
    for r, v in per_regime.items():
        if v["days"] >= 5 and v["avg_pnl"] < 0:
            warnings.append(f"{r}: negative avg PnL ({v['avg_pnl']}) over {v['days']} days")
        if v["kill_switch_blocks"] > 3:
            warnings.append(f"{r}: {v['kill_switch_blocks']} kill-switch blocks")

    return {
        "total_days": total,
        "per_regime": per_regime,
        "best_regime": best_regime,
        "worst_regime": worst_regime,
        "regime_transitions": transitions,
        "warnings": warnings,
    }


def _empty_analytics() -> Dict[str, Any]:
    return {
        "total_days": 0,
        "per_regime": {r: {
            "days": 0, "pct_time": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
            "total_trades": 0, "avg_winrate": 0.0, "max_dd": 0.0,
            "avg_pf": 0.0, "avg_risk_factor": 1.0,
            "kill_switch_blocks": 0, "vol_pause_days": 0, "recovery_days": 0,
        } for r in REGIMES},
        "best_regime": "N/A",
        "worst_regime": "N/A",
        "regime_transitions": 0,
        "warnings": [],
    }


def compute_regime_stability_score(analytics: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute a composite stability score (0-100) for governance review.

    Factors:
      - Low transition frequency → higher score
      - No recovery days → higher score
      - No kill-switch blocks → higher score
      - Positive PnL in dominant regime → higher score

    Returns:
        Dict with score (0-100), grade (A-F), breakdown
    """
    total_days = analytics.get("total_days", 0)
    if total_days == 0:
        return {"score": 0, "grade": "N/A", "breakdown": {}, "assessment": "No data"}

    transitions = analytics.get("regime_transitions", 0)
    per_regime = analytics.get("per_regime", {})

    # Factor 1: Transition frequency (0-25 pts)
    # Ideal: < 1 transition per 7 days
    transition_rate = transitions / total_days if total_days > 0 else 0
    transition_score = max(0, 25 - transition_rate * 100)

    # Factor 2: Recovery burden (0-25 pts)
    recovery_days = per_regime.get("RECOVERY", {}).get("days", 0)
    recovery_pct = recovery_days / total_days if total_days > 0 else 0
    recovery_score = max(0, 25 - recovery_pct * 100)

    # Factor 3: Kill-switch events (0-25 pts)
    total_ks = sum(v.get("kill_switch_blocks", 0) for v in per_regime.values())
    ks_rate = total_ks / total_days if total_days > 0 else 0
    ks_score = max(0, 25 - ks_rate * 50)

    # Factor 4: Dominant regime profitability (0-25 pts)
    dominant = max(per_regime.items(), key=lambda x: x[1].get("days", 0)) if per_regime else (None, {})
    dom_pnl = dominant[1].get("avg_pnl", 0) if dominant[0] else 0
    profit_score = min(25, max(0, dom_pnl * 5 + 12.5))

    total_score = round(
        min(100, max(0, transition_score + recovery_score + ks_score + profit_score))
    )

    # Grade
    if total_score >= 85:
        grade = "A"
    elif total_score >= 70:
        grade = "B"
    elif total_score >= 55:
        grade = "C"
    elif total_score >= 40:
        grade = "D"
    else:
        grade = "F"

    assessment = _build_stability_assessment(grade, analytics)

    return {
        "score": total_score,
        "grade": grade,
        "breakdown": {
            "transition_stability": round(transition_score, 1),
            "recovery_burden": round(recovery_score, 1),
            "kill_switch_safety": round(ks_score, 1),
            "dominant_profitability": round(profit_score, 1),
        },
        "assessment": assessment,
    }


def _build_stability_assessment(grade: str, analytics: Dict[str, Any]) -> str:
    """Build a one-line governance assessment."""
    if grade == "A":
        return "Excellent stability — system operating within all parameters. Continue hands-off."
    if grade == "B":
        return "Good stability — minor attention areas. Weekly monitoring sufficient."
    if grade == "C":
        return "Moderate stability — review regime transitions and recovery frequency."
    if grade == "D":
        return "Below target — increased monitoring recommended. Consider parameter review."
    return "Critical — manual review required. Consider pausing adaptive features."
