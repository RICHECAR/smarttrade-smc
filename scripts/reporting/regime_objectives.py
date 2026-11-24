"""
Regime-Conditioned Objectives — Phase 11A

Defines per-regime performance targets (PF, DD, WR) and regime-specific
caps for adaptive weight and capital scaling.

Key insight: a system that holds the SAME targets in TREND and CHOP will
either be too conservative in trends or too aggressive in chop.
Phase 11A differentiates objectives by regime so the system adapts its
*expectations*, not just its *exposures*.

Responsibilities:
  1. Per-regime target tables (PF, DD, WR, max trades/day)
  2. Regime-conditioned promotion gates for adaptive weight
  3. Regime-conditioned scaling gates for capital scaling
  4. Edge scores: compare actual vs target per regime
  5. Governance: regime fitness assessment for reports

Design:
  - Pure functions, deterministic, fully testable
  - No side effects, no I/O
  - Consumed by adaptive_weight_engine, capital_scaling_engine,
    governance_report, and edge-per-regime reporting

Author: SMC Trading Bot Framework — Phase 11A
"""

import logging
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# 1. PER-REGIME TARGET TABLES
# ══════════════════════════════════════════════════════════════════════
#
# Each regime has:
#   - target_pf:      minimum profit factor to consider the regime "viable"
#   - max_dd_pct:     maximum tolerable drawdown in this regime
#   - min_winrate:    minimum win-rate threshold
#   - max_trades_day: max trades per day (overtrading guard)
#   - promo_pf_min:   PF needed for adaptive weight promotion in this regime
#   - scale_pf_min:   PF needed for capital scale-up in this regime
#   - risk_factor:    multiplier on base risk sizing (1.0 = full, <1 = reduced)
#
# Philosophy:
#   TREND  → higher targets (more edge available), higher freedom
#   RANGE  → moderate targets, tighter caps
#   CHOP   → minimal targets, very tight caps — survive, don't optimize
#   RECOVERY → strictest targets, no scale-up

REGIME_OBJECTIVES: Dict[str, Dict[str, float]] = {
    "TREND": {
        "target_pf":      1.5,
        "max_dd_pct":     6.0,
        "min_winrate":    55.0,
        "max_trades_day": 15,
        "promo_pf_min":   1.3,
        "scale_pf_min":   1.4,
        "risk_factor":    1.0,
    },
    "RANGE": {
        "target_pf":      1.2,
        "max_dd_pct":     4.0,
        "min_winrate":    50.0,
        "max_trades_day": 10,
        "promo_pf_min":   1.2,
        "scale_pf_min":   1.3,
        "risk_factor":    0.85,
    },
    "CHOP": {
        "target_pf":      1.0,
        "max_dd_pct":     3.0,
        "min_winrate":    45.0,
        "max_trades_day": 5,
        "promo_pf_min":   1.1,
        "scale_pf_min":   1.5,    # very high bar — don't scale in chop
        "risk_factor":    0.7,
    },
    "RECOVERY": {
        "target_pf":      1.0,
        "max_dd_pct":     2.0,
        "min_winrate":    50.0,
        "max_trades_day": 5,
        "promo_pf_min":   999.0,  # never promote during recovery
        "scale_pf_min":   999.0,  # never scale during recovery
        "risk_factor":    0.5,
    },
}

# Fallback for unknown regimes
_DEFAULT_OBJECTIVES = REGIME_OBJECTIVES["RANGE"]


# ══════════════════════════════════════════════════════════════════════
# 2. OBJECTIVE LOOKUP
# ══════════════════════════════════════════════════════════════════════

def get_objectives(regime: str) -> Dict[str, float]:
    """
    Get the objectives dict for a given regime.

    Falls back to RANGE objectives for unknown regimes.
    """
    return REGIME_OBJECTIVES.get(regime, _DEFAULT_OBJECTIVES)


# ══════════════════════════════════════════════════════════════════════
# 3. REGIME-CONDITIONED PROMOTION GATE
# ══════════════════════════════════════════════════════════════════════

def check_regime_promotion_gate(
    regime: str,
    kpis: Dict[str, Any],
) -> Tuple[bool, str]:
    """
    Check if the current regime-conditioned promotion gate passes.

    This is an ADDITIONAL gate on top of the existing adaptive weight
    promotion logic.  The adaptive_weight_engine calls this before
    allowing a PROMOTE.

    Args:
        regime:  Current effective regime ("TREND", "RANGE", "CHOP", "RECOVERY")
        kpis:    KPI dict with profit_factor_B, winrate_B, drawdown_B

    Returns:
        (pass, reason) — True if promotion is allowed, False + reason if blocked
    """
    obj = get_objectives(regime)

    pf_b = kpis.get("profit_factor_B", 0.0)
    wr_b = kpis.get("winrate_B", 0.0)
    dd_b = kpis.get("drawdown_B", 0.0)

    # PF gate — regime-specific
    if pf_b < obj["promo_pf_min"]:
        return False, (
            f"PF(B)={pf_b:.2f} < regime {regime} promo_pf_min={obj['promo_pf_min']:.2f}"
        )

    # Win-rate floor
    if wr_b * 100 < obj["min_winrate"] and wr_b > 0:
        return False, (
            f"WR(B)={wr_b*100:.1f}% < regime {regime} min_winrate={obj['min_winrate']:.1f}%"
        )

    # DD ceiling
    if dd_b > obj["max_dd_pct"]:
        return False, (
            f"DD(B)={dd_b:.2f}% > regime {regime} max_dd={obj['max_dd_pct']:.1f}%"
        )

    return True, f"Regime {regime} promotion gate passed"


# ══════════════════════════════════════════════════════════════════════
# 4. REGIME-CONDITIONED SCALING GATE
# ══════════════════════════════════════════════════════════════════════

def check_regime_scaling_gate(
    regime: str,
    kpis_30d: Dict[str, Any],
) -> Tuple[bool, str]:
    """
    Check if the current regime allows capital scale-up.

    This is an ADDITIONAL gate on top of existing capital_scaling_engine
    logic.

    Args:
        regime:    Current effective regime
        kpis_30d:  30-day KPIs with profit_factor, max_dd_pct, winrate

    Returns:
        (pass, reason)
    """
    obj = get_objectives(regime)

    pf = kpis_30d.get("profit_factor", 0.0)
    dd = kpis_30d.get("max_dd_pct", 0.0)

    if pf < obj["scale_pf_min"]:
        return False, (
            f"PF={pf:.2f} < regime {regime} scale_pf_min={obj['scale_pf_min']:.2f}"
        )

    if dd > obj["max_dd_pct"]:
        return False, (
            f"DD={dd:.2f}% > regime {regime} max_dd={obj['max_dd_pct']:.1f}%"
        )

    return True, f"Regime {regime} scaling gate passed"


# ══════════════════════════════════════════════════════════════════════
# 5. EDGE SCORE — actual vs target per regime
# ══════════════════════════════════════════════════════════════════════

def compute_edge_score(
    regime: str,
    actual_pf: float,
    actual_dd: float,
    actual_wr: float,
) -> Dict[str, Any]:
    """
    Compute an edge score (0-100) comparing actual performance vs
    regime-specific targets.

    Components (weighted):
      - PF ratio  (40%): actual_pf / target_pf, capped at 2.0
      - DD ratio  (30%): (max_dd - actual_dd) / max_dd, floored at 0
      - WR ratio  (30%): actual_wr / min_winrate, capped at 1.5

    Returns:
        {
            "regime": str,
            "score": float (0-100),
            "grade": str (A-F),
            "pf_ratio": float,
            "dd_ratio": float,
            "wr_ratio": float,
            "verdict": str,
        }
    """
    obj = get_objectives(regime)

    # PF component (40 pts)
    pf_target = obj["target_pf"]
    pf_ratio = min(2.0, actual_pf / pf_target) if pf_target > 0 else 0.0
    pf_score = pf_ratio * 40 / 2.0  # normalized to 0-40 (2.0 → 40)

    # DD component (30 pts) — lower DD is better
    dd_max = obj["max_dd_pct"]
    if dd_max > 0:
        dd_headroom = max(0.0, (dd_max - actual_dd) / dd_max)
    else:
        dd_headroom = 1.0 if actual_dd == 0 else 0.0
    dd_score = dd_headroom * 30

    # WR component (30 pts)
    wr_target = obj["min_winrate"]
    wr_ratio = min(1.5, actual_wr / wr_target) if wr_target > 0 else 0.0
    wr_score = wr_ratio * 30 / 1.5  # normalized to 0-30

    total = round(min(100, max(0, pf_score + dd_score + wr_score)))

    # Grade
    if total >= 85:
        grade = "A"
    elif total >= 70:
        grade = "B"
    elif total >= 55:
        grade = "C"
    elif total >= 40:
        grade = "D"
    else:
        grade = "F"

    # Verdict
    if grade == "A":
        verdict = f"{regime}: strong edge — system outperforming targets"
    elif grade == "B":
        verdict = f"{regime}: positive edge — meeting objectives"
    elif grade == "C":
        verdict = f"{regime}: marginal — close to target thresholds"
    elif grade == "D":
        verdict = f"{regime}: below target — consider tighter caps"
    else:
        verdict = f"{regime}: no edge — avoid trading in this regime"

    return {
        "regime": regime,
        "score": total,
        "grade": grade,
        "pf_ratio": round(pf_ratio, 3),
        "dd_ratio": round(dd_headroom, 3),
        "wr_ratio": round(wr_ratio, 3),
        "pf_score": round(pf_score, 1),
        "dd_score": round(dd_score, 1),
        "wr_score": round(wr_score, 1),
        "verdict": verdict,
    }


# ══════════════════════════════════════════════════════════════════════
# 6. MULTI-REGIME EDGE REPORT
# ══════════════════════════════════════════════════════════════════════

def compute_edge_report(
    regime_analytics: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build a full edge-per-regime report from regime analytics data.

    Args:
        regime_analytics: Output of compute_regime_analytics() from
                          regime_analytics.py. Must have "per_regime" dict.

    Returns:
        {
            "per_regime": {regime: edge_score_dict, ...},
            "best_edge_regime": str,
            "worst_edge_regime": str,
            "regime_fitness_summary": str,
            "actionable_regimes": [str, ...],   # regimes with grade >= C
            "avoid_regimes": [str, ...],         # regimes with grade < C
        }
    """
    per_regime = regime_analytics.get("per_regime", {})
    edge_results = {}

    for regime, data in per_regime.items():
        if data.get("days", 0) == 0:
            continue
        edge = compute_edge_score(
            regime=regime,
            actual_pf=data.get("avg_pf", 0.0),
            actual_dd=data.get("max_dd", 0.0),
            actual_wr=data.get("avg_winrate", 0.0),
        )
        edge_results[regime] = edge

    if not edge_results:
        return {
            "per_regime": {},
            "best_edge_regime": "N/A",
            "worst_edge_regime": "N/A",
            "regime_fitness_summary": "No data — accumulate daily reports",
            "actionable_regimes": [],
            "avoid_regimes": [],
        }

    best = max(edge_results, key=lambda r: edge_results[r]["score"])
    worst = min(edge_results, key=lambda r: edge_results[r]["score"])

    actionable = [r for r, e in edge_results.items() if e["score"] >= 55]
    avoid = [r for r, e in edge_results.items() if e["score"] < 55]

    # Summary
    avg_score = sum(e["score"] for e in edge_results.values()) / len(edge_results)
    if avg_score >= 70:
        summary = "System has positive edge across most regimes. Maintain current strategy."
    elif avg_score >= 50:
        summary = "Mixed edge profile. Focus capital on actionable regimes, reduce exposure in weak ones."
    else:
        summary = "System struggling across regimes. Review core strategy before scaling."

    return {
        "per_regime": edge_results,
        "best_edge_regime": best,
        "worst_edge_regime": worst,
        "avg_edge_score": round(avg_score, 1),
        "regime_fitness_summary": summary,
        "actionable_regimes": actionable,
        "avoid_regimes": avoid,
    }


# ══════════════════════════════════════════════════════════════════════
# 7. REGIME RISK FACTOR (for capital_scaling_engine)
# ══════════════════════════════════════════════════════════════════════

def get_regime_risk_factor(regime: str) -> float:
    """
    Get the risk sizing factor for a given regime.

    Returns a multiplier [0.0, 1.0] applied to the base risk_pct.
    """
    obj = get_objectives(regime)
    return obj["risk_factor"]


# ══════════════════════════════════════════════════════════════════════
# 8. RENDER EDGE REPORT — markdown section for governance
# ══════════════════════════════════════════════════════════════════════

def render_edge_report_section(edge_report: Dict[str, Any]) -> str:
    """
    Render the edge report as a markdown section for governance reports.
    """
    lines = [
        "## Edge per Regime",
        "",
        "| Regime | Score | Grade | PF ratio | DD headroom | WR ratio | Verdict |",
        "|--------|-------|-------|----------|-------------|----------|---------|",
    ]

    for regime in ("TREND", "RANGE", "CHOP", "RECOVERY"):
        edge = edge_report.get("per_regime", {}).get(regime)
        if not edge:
            continue
        lines.append(
            f"| {regime} | {edge['score']} | {edge['grade']} | "
            f"{edge['pf_ratio']:.2f} | {edge['dd_ratio']:.2f} | "
            f"{edge['wr_ratio']:.2f} | {edge['verdict']} |"
        )

    lines.extend([
        "",
        f"**Best edge:** {edge_report.get('best_edge_regime', 'N/A')}",
        f"**Worst edge:** {edge_report.get('worst_edge_regime', 'N/A')}",
        f"**Avg edge score:** {edge_report.get('avg_edge_score', 0)}/100",
        "",
        f"**Actionable regimes:** {', '.join(edge_report.get('actionable_regimes', []))}",
        f"**Avoid regimes:** {', '.join(edge_report.get('avoid_regimes', []))}",
        "",
        f"*{edge_report.get('regime_fitness_summary', '')}*",
    ])

    return "\n".join(lines)
