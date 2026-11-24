"""
Governance Report Generator — Phase 10

Produces Weekly and Monthly governance reports by aggregating daily snapshots.

- Weekly Report:  7-day rolling window → operational check
- Monthly Report: 30-day window → strategic deep dive

Both reports include:
  1. Summary KPIs (PnL, trades, win-rate, DD, PF)
  2. Regime distribution (time in TREND/RANGE/CHOP/RECOVERY)
  3. PnL per regime
  4. Stability score (A-F)
  5. System health (kill-switch, vol-pause, recovery events)
  6. Actionable governance recommendation

Design:
  - Stateless aggregation over daily JSON reports
  - Produces Markdown + JSON outputs
  - Intended for human review (weekly) and deep analysis (monthly)

Author: SMC Trading Bot Framework — Phase 10
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

from scripts.reporting.regime_analytics import (
    load_daily_snapshots,
    compute_regime_analytics,
    compute_regime_stability_score,
    REGIMES,
)
from scripts.reporting.regime_objectives import (
    compute_edge_report,
    render_edge_report_section,
)
from scripts.reporting.capital_allocator import (
    compute_efficiency_kpis,
    render_efficiency_section,
)
from scripts.reporting.health_gates import (
    render_health_section,
    render_slo_section,
)
from scripts.reporting.chaos_simulator import render_chaos_report

logger = logging.getLogger(__name__)


# ── Report Types ──────────────────────────────────────────────────────
REPORT_WEEKLY = "weekly"
REPORT_MONTHLY = "monthly"


def aggregate_period_kpis(
    snapshots: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Aggregate performance KPIs across a period of daily snapshots.

    Returns:
        Dict with: total_pnl, total_trades, avg_winrate, max_dd,
                   avg_pf, trading_days, no_trade_days,
                   adaptive_weight_changes, scaling_changes,
                   kill_switch_total_blocks, avg_risk_pct
    """
    if not snapshots:
        return _empty_period_kpis()

    total_pnl = 0.0
    total_trades = 0
    winrate_sum = 0.0
    winrate_count = 0
    max_dd = 0.0
    pf_sum = 0.0
    pf_count = 0
    trading_days = 0
    no_trade_days = 0
    aw_changes = 0
    scaling_changes = 0
    ks_blocks = 0
    risk_sum = 0.0
    risk_count = 0
    equity_start = 0.0
    equity_end = 0.0

    for i, snap in enumerate(snapshots):
        data = snap.get("data", snap)
        perf = data.get("performance", {})
        aw = data.get("adaptive_weight", {})
        sc = data.get("capital_scaling", {})
        ks = data.get("kill_switch", {})
        bal = data.get("balance", {})

        pnl = perf.get("pnl_abs", 0.0)
        trades = perf.get("n_trades", 0)

        total_pnl += pnl
        total_trades += trades

        if trades > 0:
            trading_days += 1
            winrate_sum += perf.get("winrate", 0.0)
            winrate_count += 1
            pf_sum += perf.get("profit_factor", 0.0)
            pf_count += 1
        else:
            no_trade_days += 1

        dd = perf.get("max_dd", 0.0)
        max_dd = max(max_dd, dd)

        if aw.get("decision") in ("PROMOTE", "ROLLBACK"):
            aw_changes += 1
        if sc.get("decision") in ("SCALE_UP", "SCALE_DOWN"):
            scaling_changes += 1

        ks_blocks += ks.get("block_count", 0)

        risk_pct = sc.get("current_risk_pct", 0.0)
        if risk_pct > 0:
            risk_sum += risk_pct
            risk_count += 1

        if i == 0:
            equity_start = bal.get("equity_start", bal.get("equity_end", 0.0))
        if i == len(snapshots) - 1:
            equity_end = bal.get("equity_end", bal.get("equity_start", 0.0))

    equity_change_pct = 0.0
    if equity_start > 0:
        equity_change_pct = round((equity_end - equity_start) / equity_start * 100, 2)

    return {
        "total_pnl": round(total_pnl, 2),
        "total_trades": total_trades,
        "avg_winrate": round(winrate_sum / winrate_count, 1) if winrate_count > 0 else 0.0,
        "max_dd": round(max_dd, 2),
        "avg_pf": round(pf_sum / pf_count, 2) if pf_count > 0 else 0.0,
        "trading_days": trading_days,
        "no_trade_days": no_trade_days,
        "equity_start": round(equity_start, 2),
        "equity_end": round(equity_end, 2),
        "equity_change_pct": equity_change_pct,
        "adaptive_weight_changes": aw_changes,
        "scaling_changes": scaling_changes,
        "kill_switch_total_blocks": ks_blocks,
        "avg_risk_pct": round(risk_sum / risk_count, 4) if risk_count > 0 else 0.0,
    }


def _empty_period_kpis() -> Dict[str, Any]:
    return {
        "total_pnl": 0.0, "total_trades": 0, "avg_winrate": 0.0,
        "max_dd": 0.0, "avg_pf": 0.0, "trading_days": 0, "no_trade_days": 0,
        "equity_start": 0.0, "equity_end": 0.0, "equity_change_pct": 0.0,
        "adaptive_weight_changes": 0, "scaling_changes": 0,
        "kill_switch_total_blocks": 0, "avg_risk_pct": 0.0,
    }


def build_governance_recommendation(
    period_kpis: Dict[str, Any],
    regime_analytics: Dict[str, Any],
    stability: Dict[str, Any],
    report_type: str = REPORT_WEEKLY,
) -> str:
    """
    Build a governance recommendation text for human review.

    Returns:
        Multi-line recommendation string.
    """
    lines = []
    grade = stability.get("grade", "N/A")
    score = stability.get("score", 0)

    lines.append(f"**Stability Grade: {grade} ({score}/100)**")
    lines.append(f"*{stability.get('assessment', '')}*")
    lines.append("")

    # PnL assessment
    pnl = period_kpis.get("total_pnl", 0)
    if pnl > 0:
        lines.append(f"- PnL: +{pnl:.2f} — system generating alpha")
    elif pnl == 0:
        lines.append("- PnL: flat — monitor for edge degradation")
    else:
        lines.append(f"- PnL: {pnl:.2f} — negative period, review regime-specific loss sources")

    # Kill-switch assessment
    ks = period_kpis.get("kill_switch_total_blocks", 0)
    if ks == 0:
        lines.append("- Kill-switch: no activations — risk managed within limits")
    elif ks <= 2:
        lines.append(f"- Kill-switch: {ks} activation(s) — expected in volatile conditions")
    else:
        lines.append(f"- Kill-switch: {ks} activations — elevated risk events, review sizing")

    # Regime focus
    worst = regime_analytics.get("worst_regime", "N/A")
    worst_data = regime_analytics.get("per_regime", {}).get(worst, {})
    if worst_data.get("days", 0) > 0 and worst_data.get("avg_pnl", 0) < 0:
        lines.append(
            f"- Worst regime: {worst} ({worst_data['days']} days, "
            f"avg PnL: {worst_data['avg_pnl']:.2f}) — consider tighter caps"
        )

    # Action items
    lines.append("")
    if report_type == REPORT_WEEKLY:
        if grade in ("A", "B"):
            lines.append("**Action: No intervention required. Continue hands-off mode.**")
        elif grade == "C":
            lines.append("**Action: Monitor daily for next 3 days. No parameter changes yet.**")
        else:
            lines.append("**Action: Manual review recommended. Consider pausing adaptive features.**")
    else:  # monthly
        if grade in ("A", "B"):
            lines.append("**Action: System validated for continued autonomous operation.**")
            lines.append("Consider increasing RISK_PCT_BASE by +0.05% if equity > 120% of start.")
        elif grade == "C":
            lines.append("**Action: Hold current parameters. Increase weekly monitoring frequency.**")
        else:
            lines.append("**Action: Reduce RISK_PCT_BASE. Freeze adaptive weight. Manual oversight required.**")

    return "\n".join(lines)


def generate_governance_report(
    reports_dir: Path,
    to_date: Optional[date] = None,
    report_type: str = REPORT_WEEKLY,
) -> Dict[str, Any]:
    """
    Generate a full governance report (weekly or monthly).

    Args:
        reports_dir:  Path to reports/ directory
        to_date:      End date (defaults to today)
        report_type:  "weekly" (7 days) or "monthly" (30 days)

    Returns:
        Dict with: meta, period_kpis, regime_analytics, stability, recommendation
    """
    end = to_date or date.today()
    days = 7 if report_type == REPORT_WEEKLY else 30
    start = end - timedelta(days=days - 1)

    snapshots = load_daily_snapshots(reports_dir, start, end)

    period_kpis = aggregate_period_kpis(snapshots)
    regime_analytics = compute_regime_analytics(snapshots)
    stability = compute_regime_stability_score(regime_analytics)
    edge_report = compute_edge_report(regime_analytics)
    recommendation = build_governance_recommendation(
        period_kpis, regime_analytics, stability, report_type
    )

    # Phase 11B: Capital efficiency from daily allocation snapshots
    daily_allocs = [
        s.get("capital_allocation") for s in snapshots
        if s.get("capital_allocation")
    ]
    efficiency_kpis = compute_efficiency_kpis(daily_allocs) if daily_allocs else None

    result = {
        "meta": {
            "report_type": report_type,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "days_requested": days,
            "days_with_data": len(snapshots),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "period_kpis": period_kpis,
        "regime_analytics": regime_analytics,
        "stability": stability,
        "edge_report": edge_report,
        "efficiency_kpis": efficiency_kpis,
        "recommendation": recommendation,
    }

    return result


def render_governance_markdown(report: Dict[str, Any]) -> str:
    """
    Render a governance report dict to Markdown.
    """
    meta = report["meta"]
    kpis = report["period_kpis"]
    regime = report["regime_analytics"]
    stability = report["stability"]
    reco = report["recommendation"]

    rtype = "Weekly" if meta["report_type"] == REPORT_WEEKLY else "Monthly"
    lines = [
        f"# {rtype} Governance Report",
        f"**Period:** {meta['period_start']} → {meta['period_end']} "
        f"({meta['days_with_data']}/{meta['days_requested']} days with data)",
        f"**Generated:** {meta['generated_at']}",
        "",
        "---",
        "",
        "## Performance Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total PnL | {kpis['total_pnl']:.2f} |",
        f"| Total Trades | {kpis['total_trades']} |",
        f"| Avg Win Rate | {kpis['avg_winrate']:.1f}% |",
        f"| Max Drawdown | {kpis['max_dd']:.2f}% |",
        f"| Avg Profit Factor | {kpis['avg_pf']:.2f} |",
        f"| Trading Days | {kpis['trading_days']} |",
        f"| No-Trade Days | {kpis['no_trade_days']} |",
        f"| Equity Start | {kpis['equity_start']:.2f} |",
        f"| Equity End | {kpis['equity_end']:.2f} |",
        f"| Equity Change | {kpis['equity_change_pct']:.2f}% |",
        f"| Avg Risk % | {kpis['avg_risk_pct']:.4f}% |",
        "",
        "---",
        "",
        "## System Events",
        "",
        f"| Event | Count |",
        f"|-------|-------|",
        f"| Adaptive Weight Changes | {kpis['adaptive_weight_changes']} |",
        f"| Scaling Changes | {kpis['scaling_changes']} |",
        f"| Kill-Switch Blocks | {kpis['kill_switch_total_blocks']} |",
        "",
        "---",
        "",
        "## Regime Distribution",
        "",
        "| Regime | Days | % Time | Total PnL | Avg PnL/day | Avg WR | Max DD | Avg PF |",
        "|--------|------|--------|-----------|-------------|--------|--------|--------|",
    ]

    for r in REGIMES:
        rd = regime.get("per_regime", {}).get(r, {})
        if rd.get("days", 0) > 0:
            lines.append(
                f"| {r} | {rd['days']} | {rd['pct_time']:.1f}% | "
                f"{rd['total_pnl']:.2f} | {rd['avg_pnl']:.2f} | "
                f"{rd['avg_winrate']:.1f}% | {rd['max_dd']:.2f}% | {rd['avg_pf']:.2f} |"
            )

    lines.extend([
        "",
        f"**Regime Transitions:** {regime.get('regime_transitions', 0)}",
        f"**Best Regime:** {regime.get('best_regime', 'N/A')}",
        f"**Worst Regime:** {regime.get('worst_regime', 'N/A')}",
        "",
        "---",
        "",
        "## Stability Score",
        "",
        f"**Grade: {stability.get('grade', 'N/A')} ({stability.get('score', 0)}/100)**",
        "",
        "| Factor | Score |",
        "|--------|-------|",
    ])

    breakdown = stability.get("breakdown", {})
    for factor, score in breakdown.items():
        label = factor.replace("_", " ").title()
        lines.append(f"| {label} | {score:.1f}/25 |")

    lines.extend([
        "",
        "---",
        "",
        "## Governance Recommendation",
        "",
        reco,
        "",
        "---",
        "",
    ])

    # Phase 11A: Edge per regime section
    edge_report = report.get("edge_report")
    if edge_report and edge_report.get("per_regime"):
        lines.append(render_edge_report_section(edge_report))
        lines.append("")
        lines.append("---")
        lines.append("")

    # Phase 11B: Capital efficiency section
    efficiency_kpis = report.get("efficiency_kpis")
    if efficiency_kpis:
        lines.append(render_efficiency_section(efficiency_kpis))
        lines.append("")
        lines.append("---")
        lines.append("")

    # Phase 11C: Health gates section
    health_result = report.get("health_check")
    if health_result:
        lines.append(render_health_section(health_result))
        lines.append("")
        lines.append("---")
        lines.append("")

    # Phase 11C: SLO compliance section
    slo_report = report.get("slo_report")
    if slo_report:
        lines.append(render_slo_section(slo_report))
        lines.append("")
        lines.append("---")
        lines.append("")

    # Phase 11C: Chaos simulation section
    chaos_report = report.get("chaos_report")
    if chaos_report:
        lines.append(render_chaos_report(chaos_report))
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append(f"*Generated automatically — Phase 11C Governance System*")

    # Add regime-specific warnings
    warnings = regime.get("warnings", [])
    if warnings:
        lines.insert(-2, "")
        lines.insert(-2, "### Warnings")
        for w in warnings:
            lines.insert(-2, f"- ⚠️ {w}")

    return "\n".join(lines)


def save_governance_report(
    report: Dict[str, Any],
    reports_dir: Path,
    report_type: str = REPORT_WEEKLY,
) -> Path:
    """
    Save governance report as JSON + Markdown.

    Returns:
        Path to the Markdown file.
    """
    gov_dir = reports_dir / "governance"
    gov_dir.mkdir(parents=True, exist_ok=True)

    date_str = report["meta"]["period_end"].replace("-", "")
    prefix = "weekly" if report_type == REPORT_WEEKLY else "monthly"

    # JSON
    json_path = gov_dir / f"{prefix}_report_{date_str}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    # Markdown
    md_content = render_governance_markdown(report)
    md_path = gov_dir / f"{prefix}_report_{date_str}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    logger.info(f"[GOVERNANCE] {prefix.upper()} report saved: {md_path}")
    return md_path
