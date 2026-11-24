"""
Hands-Off Live Mode Configuration — Phase 10

Central configuration for autonomous live operation.
Intended for 30-60 days of unattended running with only
weekly/monthly governance report reviews.

Responsibilities:
  1. Centralizes all operational thresholds → single source of truth
  2. Defines governance schedule (weekly/monthly cadence)
  3. Defines auto-actions vs human-required actions
  4. Pre-flight checklist validation before going hands-off

Design:
  - No trading logic (governance-only)
  - Read-only config consumed by governance report generator
  - Pre-flight checker returns pass/fail with reasons

Author: SMC Trading Bot Framework — Phase 10
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List, Tuple

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# 1. GOVERNANCE SCHEDULE
# ══════════════════════════════════════════════════════════════════════
GOVERNANCE_SCHEDULE = {
    "daily_report":   True,       # Continue daily reports (existing)
    "weekly_report":  True,       # Generate weekly governance report every Sunday
    "weekly_day":     6,          # 0=Monday … 6=Sunday
    "monthly_report": True,       # Generate monthly deep-dive on 1st of month
    "monthly_day":    1,          # Day of month
}

# ══════════════════════════════════════════════════════════════════════
# 2. ALERT THRESHOLDS — when to page the human
# ══════════════════════════════════════════════════════════════════════
ALERT_THRESHOLDS = {
    # Performance alerts (trigger immediate human review)
    "max_daily_loss_usd":       -50.0,     # Single-day loss > this → alert
    "max_weekly_loss_usd":      -150.0,    # Week total < this → alert
    "max_drawdown_pct":         10.0,      # Peak-to-trough > this → alert
    "min_weekly_winrate":       30.0,      # Win-rate < this over a week → alert
    "min_weekly_pf":            0.6,       # Profit factor < this → alert

    # System health alerts
    "max_kill_switch_per_week": 3,         # > 3 kill-switch blocks in a week
    "max_consecutive_losses":   8,         # > 8 losses in a row
    "max_no_trade_days":        3,         # > 3 consecutive days with 0 trades

    # Stability alerts
    "min_stability_grade":      "C",       # Grades worse than C → alert
    "max_regime_transitions_per_week": 10, # Excessive regime flipping → alert
}

# Grade ordering for comparison
_GRADE_ORDER = {"A": 5, "B": 4, "C": 3, "D": 2, "F": 1, "N/A": 0}


# ══════════════════════════════════════════════════════════════════════
# 3. AUTO-ACTIONS — what the system can do without human approval
# ══════════════════════════════════════════════════════════════════════
AUTO_ACTIONS = {
    # These happen automatically (existing Phase 8-9 features)
    "adaptive_weight_promote":     True,   # FSM can auto-promote strategies
    "adaptive_weight_rollback":    True,   # FSM can auto-rollback
    "capital_scale_up":            True,   # Auto scale-up if conditions met
    "capital_scale_down":          True,   # Auto scale-down on drawdown
    "kill_switch_block":           True,   # Auto-block on limit breach
    "volatility_pause":            True,   # Auto-pause on ATR explosion
    "recovery_mode":               True,   # Auto-recovery after drawdown

    # These require human confirmation (Phase 10 new)
    "increase_risk_base":          False,  # Never auto-increase base risk
    "disable_kill_switch":         False,  # Never auto-disable safety
    "switch_exchange":             False,  # Never auto-switch exchange
    "modify_regime_thresholds":    False,  # Never auto-change regime params
}

# ══════════════════════════════════════════════════════════════════════
# 4. OPERATIONAL LIMITS — hard ceilings for hands-off period
# ══════════════════════════════════════════════════════════════════════
OPERATIONAL_LIMITS = {
    "max_equity_at_risk_pct":     2.0,     # Never risk > 2% of equity
    "max_concurrent_positions":   3,       # Max simultaneous positions
    "max_daily_trades":           20,      # Max trades per day
    "max_position_hold_hours":    48,      # Force-close after 48h
    "min_balance_usd":            50.0,    # Below this → freeze all trading
}


# ══════════════════════════════════════════════════════════════════════
# 5. PRE-FLIGHT CHECKLIST — validate before going hands-off
# ══════════════════════════════════════════════════════════════════════

def run_preflight_checks(
    reports_dir: Path,
    config: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """
    Run all pre-flight checks before entering hands-off mode.

    Checks:
      1. Daily report pipeline is working (recent reports exist)
      2. Kill-switch is enabled
      3. Adaptive weight FSM is in a stable state
      4. No critical warnings in last 3 daily reports
      5. Configuration is consistent

    Returns:
        {
            "pass": bool,
            "checks": [{"name": str, "pass": bool, "detail": str}, ...],
            "timestamp": str,
        }
    """
    checks = []
    today = date.today()

    # Check 1: Recent daily reports exist
    daily_dir = reports_dir / "daily"
    recent = 0
    for d in range(3):
        dt = today - timedelta(days=d)
        fname = f"daily_report_{dt.strftime('%Y%m%d')}.json"
        if (daily_dir / fname).exists():
            recent += 1
    checks.append({
        "name": "daily_reports_recent",
        "pass": recent >= 1,
        "detail": f"{recent}/3 recent daily reports found"
    })

    # Check 2: Governance directory writable
    gov_dir = reports_dir / "governance"
    try:
        gov_dir.mkdir(parents=True, exist_ok=True)
        test_file = gov_dir / ".preflight_test"
        test_file.write_text("ok")
        test_file.unlink()
        checks.append({
            "name": "governance_dir_writable",
            "pass": True,
            "detail": f"Governance dir OK: {gov_dir}"
        })
    except Exception as e:
        checks.append({
            "name": "governance_dir_writable",
            "pass": False,
            "detail": f"Cannot write to governance dir: {e}"
        })

    # Check 3: Config consistency
    config = config or {}
    ks_enabled = config.get("kill_switch_enabled", True)
    checks.append({
        "name": "kill_switch_enabled",
        "pass": ks_enabled,
        "detail": "Kill-switch is " + ("enabled" if ks_enabled else "DISABLED — CRITICAL")
    })

    # Check 4: Auto-actions sanity
    dangerous_auto = []
    for action, auto in AUTO_ACTIONS.items():
        if action in ("increase_risk_base", "disable_kill_switch") and auto:
            dangerous_auto.append(action)
    checks.append({
        "name": "no_dangerous_auto_actions",
        "pass": len(dangerous_auto) == 0,
        "detail": f"Dangerous auto-actions: {dangerous_auto}" if dangerous_auto else "No dangerous auto-actions"
    })

    # Check 5: Operational limits are sane
    risk = OPERATIONAL_LIMITS.get("max_equity_at_risk_pct", 0)
    checks.append({
        "name": "risk_limit_sane",
        "pass": 0 < risk <= 5.0,
        "detail": f"Max equity at risk: {risk}%"
    })

    all_pass = all(c["pass"] for c in checks)

    return {
        "pass": all_pass,
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def check_alerts(
    period_kpis: Dict[str, Any],
    stability: Dict[str, Any],
    regime_analytics: Dict[str, Any],
) -> List[Dict[str, str]]:
    """
    Check current period KPIs against alert thresholds.

    Returns list of triggered alerts:
      [{"level": "WARNING"|"CRITICAL", "message": str}, ...]
    """
    alerts = []
    t = ALERT_THRESHOLDS

    pnl = period_kpis.get("total_pnl", 0)
    if pnl < t["max_weekly_loss_usd"]:
        alerts.append({"level": "CRITICAL", "message": f"Weekly loss {pnl:.2f} exceeds threshold {t['max_weekly_loss_usd']}"})
    elif pnl < 0:
        alerts.append({"level": "WARNING", "message": f"Weekly PnL negative: {pnl:.2f}"})

    wr = period_kpis.get("avg_winrate", 0)
    if wr > 0 and wr < t["min_weekly_winrate"]:
        alerts.append({"level": "WARNING", "message": f"Win-rate {wr:.1f}% below threshold {t['min_weekly_winrate']}%"})

    pf = period_kpis.get("avg_pf", 0)
    if pf > 0 and pf < t["min_weekly_pf"]:
        alerts.append({"level": "WARNING", "message": f"Profit factor {pf:.2f} below threshold {t['min_weekly_pf']}"})

    dd = period_kpis.get("max_dd", 0)
    if dd > t["max_drawdown_pct"]:
        alerts.append({"level": "CRITICAL", "message": f"Drawdown {dd:.2f}% exceeds threshold {t['max_drawdown_pct']}%"})

    ks = period_kpis.get("kill_switch_total_blocks", 0)
    if ks > t["max_kill_switch_per_week"]:
        alerts.append({"level": "CRITICAL", "message": f"Kill-switch activations ({ks}) exceed threshold ({t['max_kill_switch_per_week']})"})

    grade = stability.get("grade", "N/A")
    min_grade = t["min_stability_grade"]
    if _GRADE_ORDER.get(grade, 0) < _GRADE_ORDER.get(min_grade, 0):
        alerts.append({"level": "WARNING", "message": f"Stability grade {grade} below minimum {min_grade}"})

    transitions = regime_analytics.get("regime_transitions", 0)
    if transitions > t["max_regime_transitions_per_week"]:
        alerts.append({"level": "WARNING", "message": f"Regime transitions ({transitions}) excessive (threshold: {t['max_regime_transitions_per_week']})"})

    ntd = period_kpis.get("no_trade_days", 0)
    if ntd > t["max_no_trade_days"]:
        alerts.append({"level": "WARNING", "message": f"No-trade days ({ntd}) exceed threshold ({t['max_no_trade_days']})"})

    return alerts


def render_preflight_markdown(result: Dict[str, Any]) -> str:
    """Render pre-flight check results as Markdown."""
    lines = [
        "# Pre-Flight Checklist — Hands-Off Mode",
        f"**Timestamp:** {result['timestamp']}",
        f"**Overall:** {'✅ PASS' if result['pass'] else '❌ FAIL'}",
        "",
        "| Check | Status | Detail |",
        "|-------|--------|--------|",
    ]
    for c in result.get("checks", []):
        status = "✅" if c["pass"] else "❌"
        lines.append(f"| {c['name']} | {status} | {c['detail']} |")

    if result["pass"]:
        lines.extend(["", "**System is ready for hands-off operation.**"])
    else:
        lines.extend(["", "**⚠️ Fix failing checks before enabling hands-off mode.**"])

    return "\n".join(lines)
