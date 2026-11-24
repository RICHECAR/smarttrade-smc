"""
Capital Allocator — Phase 11B

Optimizes capital allocation across accepted trade signals WITHOUT
increasing the global risk envelope.

Responsibilities:
  1. Edge-weighted risk distribution across concurrent signals
  2. Correlation-aware exposure reduction (correlated symbols)
  3. Regime-conditioned capital caps (TREND 100%, CHOP 50%, etc.)
  4. Capital utilization tracking (idle capital metric)
  5. Trade skip logic when edge is insufficient

Design principles:
  - Pure functions, deterministic, fully testable
  - Never increases total risk — only redistributes
  - Respects all existing kill-switch / recovery / regime constraints
  - Falls back gracefully when inputs are missing

Inputs:
  - equity, effective_risk_pct (from capital_scaling_engine)
  - list of candidate trade signals with edge_score
  - current regime
  - symbol correlation groups

Output:
  - per-trade adjusted risk allocation
  - skipped trades with reasons
  - capital utilization metrics

Author: SMC Trading Bot Framework — Phase 11B
"""

import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# 1. REGIME CAPITAL CAPS — max % of available capital to deploy
# ══════════════════════════════════════════════════════════════════════

REGIME_CAPITAL_CAPS: Dict[str, float] = {
    "TREND":    1.0,    # 100% — full deployment
    "RANGE":    0.8,    # 80%
    "CHOP":     0.5,    # 50%
    "RECOVERY": 0.3,    # 30%
}

_DEFAULT_CAP = 0.8  # fallback


# ══════════════════════════════════════════════════════════════════════
# 2. CORRELATION GROUPS — V1 simple hardcoded clusters
# ══════════════════════════════════════════════════════════════════════
#
# Symbols in the same group are considered correlated (ρ > 0.7).
# When multiple signals fire in the same group:
#   → allocate to the highest-edge signal only
#   → or reduce per-trade risk across the group

CORRELATION_GROUPS: Dict[str, List[str]] = {
    "BTC_CLUSTER": ["BTCUSDT", "BTCUSD", "BTC/USDT", "BTC/USD"],
    "ETH_CLUSTER": ["ETHUSDT", "ETHUSD", "ETH/USDT", "ETH/USD"],
    "MAJOR_ALT":   ["SOLUSDT", "SOL/USDT", "AVAXUSDT", "AVAX/USDT",
                    "DOGEUSDT", "DOGE/USDT", "ADAUSDT", "ADA/USDT"],
}

# Max simultaneous risk within one correlation group (fraction of total risk)
MAX_GROUP_RISK_FRACTION = 0.5  # no group can consume > 50% of total risk


# ══════════════════════════════════════════════════════════════════════
# 3. ALLOCATION THRESHOLDS
# ══════════════════════════════════════════════════════════════════════

MIN_EDGE_SCORE_TO_TRADE = 40      # edge score < 40 → skip trade
MIN_RISK_PER_TRADE_PCT = 0.05     # never allocate < 0.05% per trade
MAX_RISK_PER_TRADE_PCT = 1.0      # never allocate > 1.0% per trade
MAX_CONCURRENT_ALLOCATIONS = 5    # max trades allocated simultaneously


# ══════════════════════════════════════════════════════════════════════
# 4. CORE ALLOCATION FUNCTION
# ══════════════════════════════════════════════════════════════════════

def allocate_capital(
    equity: float,
    effective_risk_pct: float,
    signals: List[Dict[str, Any]],
    regime: str = "RANGE",
    correlation_groups: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """
    Allocate risk capital across candidate trade signals.

    Each signal dict must have:
      - symbol: str (e.g. "BTCUSDT")
      - edge_score: float (0-100, from compute_edge_score or trade-level)
      - direction: str ("LONG" or "SHORT")

    Optional signal fields:
      - base_risk_pct: float (override for this specific trade)

    Args:
        equity:              Current account equity (USD)
        effective_risk_pct:  Base risk per trade (%, from capital_scaling_engine)
        signals:             List of candidate trade signal dicts
        regime:              Current market regime
        correlation_groups:  Override default correlation groups

    Returns:
        {
            "allocations": [
                {"symbol": str, "risk_pct": float, "risk_usd": float,
                 "edge_score": float, "reason": str},
                ...
            ],
            "skipped": [
                {"symbol": str, "reason": str, "edge_score": float},
                ...
            ],
            "metrics": {
                "total_risk_pct": float,
                "regime_cap_pct": float,
                "capital_utilization": float,  # allocated / available
                "signals_received": int,
                "signals_allocated": int,
                "signals_skipped": int,
                "correlation_reductions": int,
            },
        }
    """
    corr_groups = correlation_groups or CORRELATION_GROUPS

    regime_cap = REGIME_CAPITAL_CAPS.get(regime, _DEFAULT_CAP)
    max_total_risk = effective_risk_pct * regime_cap

    if equity <= 0 or effective_risk_pct <= 0 or not signals:
        return _empty_allocation(len(signals), regime_cap)

    # ── Step 1: Filter by edge score ──────────────────────────────
    eligible = []
    skipped = []

    for sig in signals:
        edge = sig.get("edge_score", 0)
        symbol = sig.get("symbol", "UNKNOWN")

        if edge < MIN_EDGE_SCORE_TO_TRADE:
            skipped.append({
                "symbol": symbol,
                "reason": f"Edge score {edge} < {MIN_EDGE_SCORE_TO_TRADE}",
                "edge_score": edge,
            })
        else:
            eligible.append(sig)

    # ── Step 2: Limit concurrent allocations ─────────────────────
    eligible.sort(key=lambda s: s.get("edge_score", 0), reverse=True)
    if len(eligible) > MAX_CONCURRENT_ALLOCATIONS:
        for sig in eligible[MAX_CONCURRENT_ALLOCATIONS:]:
            skipped.append({
                "symbol": sig.get("symbol", "UNKNOWN"),
                "reason": f"Exceeds max concurrent allocations ({MAX_CONCURRENT_ALLOCATIONS})",
                "edge_score": sig.get("edge_score", 0),
            })
        eligible = eligible[:MAX_CONCURRENT_ALLOCATIONS]

    if not eligible:
        return _build_result([], skipped, 0, regime_cap, len(signals))

    # ── Step 3: Edge-weighted allocation ─────────────────────────
    total_edge = sum(s.get("edge_score", 0) for s in eligible)
    if total_edge == 0:
        total_edge = 1.0  # avoid division by zero

    raw_allocations = []
    for sig in eligible:
        edge = sig.get("edge_score", 0)
        weight = edge / total_edge
        risk_pct = max_total_risk * weight

        # Clamp per-trade risk
        risk_pct = max(MIN_RISK_PER_TRADE_PCT, min(MAX_RISK_PER_TRADE_PCT, risk_pct))

        raw_allocations.append({
            "symbol": sig.get("symbol", "UNKNOWN"),
            "direction": sig.get("direction", "LONG"),
            "edge_score": edge,
            "risk_pct": round(risk_pct, 4),
        })

    # ── Step 4: Correlation reduction ────────────────────────────
    allocations, corr_reductions = _apply_correlation_reduction(
        raw_allocations, corr_groups, max_total_risk
    )

    # ── Step 5: Compute USD amounts and metrics ──────────────────
    total_risk_pct = 0.0
    final_allocs = []
    for alloc in allocations:
        risk_usd = round(equity * alloc["risk_pct"] / 100, 2)
        final_allocs.append({
            "symbol": alloc["symbol"],
            "direction": alloc["direction"],
            "risk_pct": alloc["risk_pct"],
            "risk_usd": risk_usd,
            "edge_score": alloc["edge_score"],
            "reason": "Edge-weighted allocation",
        })
        total_risk_pct += alloc["risk_pct"]

    return _build_result(
        final_allocs, skipped, total_risk_pct, regime_cap,
        len(signals), corr_reductions
    )


# ══════════════════════════════════════════════════════════════════════
# 5. CORRELATION REDUCTION
# ══════════════════════════════════════════════════════════════════════

def _apply_correlation_reduction(
    allocations: List[Dict[str, Any]],
    corr_groups: Dict[str, List[str]],
    max_total_risk: float,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Reduce risk for correlated symbols in the same group.

    Strategy: within each group, keep the highest-edge allocation at full
    risk and reduce others by 50%.

    Returns:
        (adjusted_allocations, reduction_count)
    """
    reductions = 0

    # Build symbol → group mapping
    symbol_to_group = {}
    for group_name, symbols in corr_groups.items():
        for sym in symbols:
            symbol_to_group[sym.upper()] = group_name

    # Group the allocations
    group_allocs: Dict[str, List[int]] = {}  # group → indices
    for i, alloc in enumerate(allocations):
        sym = alloc["symbol"].upper()
        group = symbol_to_group.get(sym)
        if group:
            group_allocs.setdefault(group, []).append(i)

    # Apply reduction within each group
    for group, indices in group_allocs.items():
        if len(indices) <= 1:
            continue

        # Sort by edge score descending — keep best, reduce rest
        indices.sort(key=lambda i: allocations[i]["edge_score"], reverse=True)
        for idx in indices[1:]:  # skip the best
            old_risk = allocations[idx]["risk_pct"]
            new_risk = round(old_risk * 0.5, 4)
            new_risk = max(MIN_RISK_PER_TRADE_PCT, new_risk)
            allocations[idx]["risk_pct"] = new_risk
            allocations[idx]["reason"] = (
                f"Correlation-reduced ({group}): "
                f"{old_risk:.4f}% → {new_risk:.4f}%"
            )
            reductions += 1

    # Enforce group cap: no group > MAX_GROUP_RISK_FRACTION of total
    for group, indices in group_allocs.items():
        group_risk = sum(allocations[i]["risk_pct"] for i in indices)
        group_cap = max_total_risk * MAX_GROUP_RISK_FRACTION
        if group_risk > group_cap and group_risk > 0:
            scale = group_cap / group_risk
            for idx in indices:
                allocations[idx]["risk_pct"] = round(
                    max(MIN_RISK_PER_TRADE_PCT,
                        allocations[idx]["risk_pct"] * scale), 4
                )

    return allocations, reductions


# ══════════════════════════════════════════════════════════════════════
# 6. CAPITAL UTILIZATION METRICS
# ══════════════════════════════════════════════════════════════════════

def compute_capital_utilization(
    allocated_risk_pct: float,
    available_risk_pct: float,
) -> float:
    """
    Compute capital utilization ratio.

    Returns:
        float in [0, 1]. Higher = more capital deployed.
        1.0 = fully deployed, 0.0 = all idle.
    """
    if available_risk_pct <= 0:
        return 0.0
    return round(min(1.0, allocated_risk_pct / available_risk_pct), 4)


def compute_idle_capital_pct(
    equity: float,
    allocated_risk_usd: float,
) -> float:
    """
    Compute % of equity sitting idle (not allocated to any trade).
    """
    if equity <= 0:
        return 100.0
    used_pct = (allocated_risk_usd / equity) * 100
    return round(max(0, 100 - used_pct), 2)


# ══════════════════════════════════════════════════════════════════════
# 7. HELPERS
# ══════════════════════════════════════════════════════════════════════

def _empty_allocation(n_signals: int, regime_cap: float) -> Dict[str, Any]:
    return {
        "allocations": [],
        "skipped": [],
        "metrics": {
            "total_risk_pct": 0.0,
            "regime_cap_pct": regime_cap * 100,
            "capital_utilization": 0.0,
            "signals_received": n_signals,
            "signals_allocated": 0,
            "signals_skipped": n_signals,
            "correlation_reductions": 0,
        },
    }


def _build_result(
    allocations: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
    total_risk_pct: float,
    regime_cap: float,
    total_signals: int,
    corr_reductions: int = 0,
) -> Dict[str, Any]:
    available = regime_cap * 100  # as percentage
    utilization = compute_capital_utilization(total_risk_pct, available / 100)

    return {
        "allocations": allocations,
        "skipped": skipped,
        "metrics": {
            "total_risk_pct": round(total_risk_pct, 4),
            "regime_cap_pct": regime_cap * 100,
            "capital_utilization": utilization,
            "signals_received": total_signals,
            "signals_allocated": len(allocations),
            "signals_skipped": len(skipped),
            "correlation_reductions": corr_reductions,
        },
    }


# ══════════════════════════════════════════════════════════════════════
# 8. GOVERNANCE: EFFICIENCY KPIs
# ══════════════════════════════════════════════════════════════════════

def compute_efficiency_kpis(
    daily_allocations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Compute capital efficiency KPIs over a period of daily allocation results.

    Args:
        daily_allocations: List of allocate_capital() results, one per day.

    Returns:
        Dict with: avg_utilization, avg_signals_per_day,
                   avg_allocated_per_day, total_skipped,
                   total_correlation_reductions, efficiency_grade
    """
    if not daily_allocations:
        return {
            "avg_utilization": 0.0, "avg_signals_per_day": 0.0,
            "avg_allocated_per_day": 0.0, "total_skipped": 0,
            "total_correlation_reductions": 0, "efficiency_grade": "N/A",
        }

    util_sum = 0.0
    signals_sum = 0
    alloc_sum = 0
    skip_sum = 0
    corr_sum = 0

    for day in daily_allocations:
        m = day.get("metrics", {})
        util_sum += m.get("capital_utilization", 0)
        signals_sum += m.get("signals_received", 0)
        alloc_sum += m.get("signals_allocated", 0)
        skip_sum += m.get("signals_skipped", 0)
        corr_sum += m.get("correlation_reductions", 0)

    n = len(daily_allocations)
    avg_util = round(util_sum / n, 4)
    avg_signals = round(signals_sum / n, 1)
    avg_alloc = round(alloc_sum / n, 1)

    # Grade
    if avg_util >= 0.7:
        grade = "A"
    elif avg_util >= 0.5:
        grade = "B"
    elif avg_util >= 0.3:
        grade = "C"
    elif avg_util >= 0.1:
        grade = "D"
    else:
        grade = "F"

    return {
        "avg_utilization": avg_util,
        "avg_signals_per_day": avg_signals,
        "avg_allocated_per_day": avg_alloc,
        "total_skipped": skip_sum,
        "total_correlation_reductions": corr_sum,
        "efficiency_grade": grade,
    }


# ══════════════════════════════════════════════════════════════════════
# 9. RENDER — markdown section for governance
# ══════════════════════════════════════════════════════════════════════

def render_efficiency_section(
    efficiency_kpis: Dict[str, Any],
) -> str:
    """Render capital efficiency KPIs as markdown for governance report."""
    lines = [
        "## Capital Efficiency",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Avg Utilization | {efficiency_kpis.get('avg_utilization', 0)*100:.1f}% |",
        f"| Efficiency Grade | {efficiency_kpis.get('efficiency_grade', 'N/A')} |",
        f"| Avg Signals/Day | {efficiency_kpis.get('avg_signals_per_day', 0):.1f} |",
        f"| Avg Allocated/Day | {efficiency_kpis.get('avg_allocated_per_day', 0):.1f} |",
        f"| Total Skipped | {efficiency_kpis.get('total_skipped', 0)} |",
        f"| Correlation Reductions | {efficiency_kpis.get('total_correlation_reductions', 0)} |",
        "",
    ]
    return "\n".join(lines)
