"""
Execution Quality Analyzer — Maker vs Taker (Fallback)

Compares performance of trades executed via Maker vs Taker (fallback).
Produces a structured report and detects edge degradation from taker fallback.

Key outputs:
    - Per-group KPIs: win_rate, profit_factor, avg_r, avg_fees, net_pnl, slippage
    - TAKER_DEGRADING_EDGE flag (blocks adaptive weight promotion when active)
    - Fallback rate (% of trades using taker fallback)

Design:
    - Pure functions, deterministic, fully testable
    - Called from DailyReporter.evaluate_execution_quality_step()
    - Trade data comes from persistence layer (list of dicts)

Author: SMC Trading Bot Framework — Phase 8
"""

import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────
TAKER_DEGRADING_MIN_TRADES = 5      # need ≥5 taker trades to flag
TAKER_DEGRADING_PF_THRESHOLD = 1.0  # PF < 1.0 → flag active
TAKER_ALERT_FEE_RATIO = 0.30       # fees > 30% of gross PnL → warning

# Fallback rate threshold: if > 40% of all trades are fallback, warn
FALLBACK_RATE_WARNING = 0.40


# ── Core Analytics ────────────────────────────────────────────────────

def compute_group_kpis(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compute KPIs for a group of closed trades.

    Each trade dict expected keys:
        - pnl:        float (net P&L after fees)
        - pnl_gross:  float (P&L before fees, optional — defaults to pnl)
        - fees:       float (total fees paid, optional — defaults to 0)
        - r_multiple: float (R-multiple, optional)
        - mae:        float (max adverse excursion in %, optional)
        - slippage:   float (slippage in bps, optional)
        - is_win:     bool  (optional — derived from pnl if absent)

    Returns dict of KPIs.
    """
    if not trades:
        return _empty_kpis()

    n = len(trades)
    wins = 0
    total_pnl = 0.0
    total_pnl_gross = 0.0
    total_fees = 0.0
    total_r = 0.0
    total_mae = 0.0
    total_slippage = 0.0
    gross_wins = 0.0
    gross_losses = 0.0

    r_count = 0
    mae_count = 0
    slip_count = 0

    for t in trades:
        pnl = float(t.get("pnl", 0.0))
        pnl_gross = float(t.get("pnl_gross", pnl))
        fees = float(t.get("fees", 0.0))
        is_win = t.get("is_win", pnl > 0)

        total_pnl += pnl
        total_pnl_gross += pnl_gross
        total_fees += fees

        if is_win:
            wins += 1
        if pnl_gross > 0:
            gross_wins += pnl_gross
        else:
            gross_losses += abs(pnl_gross)

        if "r_multiple" in t:
            total_r += float(t["r_multiple"])
            r_count += 1
        if "mae" in t:
            total_mae += float(t["mae"])
            mae_count += 1
        if "slippage" in t:
            total_slippage += float(t["slippage"])
            slip_count += 1

    win_rate = wins / n if n > 0 else 0.0
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else (
        float("inf") if gross_wins > 0 else 0.0
    )
    avg_r = total_r / r_count if r_count > 0 else 0.0
    avg_fees = total_fees / n if n > 0 else 0.0
    avg_mae = total_mae / mae_count if mae_count > 0 else 0.0
    avg_slippage = total_slippage / slip_count if slip_count > 0 else 0.0
    fee_ratio = total_fees / gross_wins if gross_wins > 0 else 0.0

    return {
        "trades": n,
        "wins": wins,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else 999.0,
        "avg_r": round(avg_r, 4),
        "avg_fees": round(avg_fees, 6),
        "total_fees": round(total_fees, 6),
        "net_pnl": round(total_pnl, 6),
        "gross_pnl": round(total_pnl_gross, 6),
        "avg_mae": round(avg_mae, 4),
        "avg_slippage": round(avg_slippage, 4),
        "fee_ratio": round(fee_ratio, 4),
    }


def _empty_kpis() -> Dict[str, Any]:
    return {
        "trades": 0, "wins": 0, "win_rate": 0.0,
        "profit_factor": 0.0, "avg_r": 0.0,
        "avg_fees": 0.0, "total_fees": 0.0,
        "net_pnl": 0.0, "gross_pnl": 0.0,
        "avg_mae": 0.0, "avg_slippage": 0.0,
        "fee_ratio": 0.0,
    }


# ── Split & Compare ──────────────────────────────────────────────────

def split_trades_by_execution_mode(
    trades: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Split trades into maker and taker (fallback) groups.

    Looks for key 'maker_fallback_used' (bool) in trade dict.
    If absent, defaults to maker (False).
    """
    maker_trades = []
    taker_trades = []

    for t in trades:
        if t.get("maker_fallback_used", False):
            taker_trades.append(t)
        else:
            maker_trades.append(t)

    return maker_trades, taker_trades


def analyze_execution_quality(
    trades: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Full Maker vs Taker analysis.

    Returns:
        {
            "maker": { ...kpis },
            "taker": { ...kpis },
            "fallback_rate": float,
            "taker_degrading_edge": bool,
            "taker_degrading_reason": str | None,
            "warnings": [str],
        }
    """
    maker_trades, taker_trades = split_trades_by_execution_mode(trades)

    maker_kpis = compute_group_kpis(maker_trades)
    taker_kpis = compute_group_kpis(taker_trades)

    total = len(trades)
    fallback_rate = len(taker_trades) / total if total > 0 else 0.0

    # ── TAKER_DEGRADING_EDGE detection ─────────────────────────────
    degrading = False
    degrading_reason = None
    warnings = []

    if taker_kpis["trades"] >= TAKER_DEGRADING_MIN_TRADES:
        pf_taker = taker_kpis["profit_factor"]
        if pf_taker < TAKER_DEGRADING_PF_THRESHOLD:
            degrading = True
            degrading_reason = (
                f"Taker PF={pf_taker:.2f} < {TAKER_DEGRADING_PF_THRESHOLD} "
                f"over {taker_kpis['trades']} trades"
            )
            warnings.append(f"TAKER_DEGRADING_EDGE: {degrading_reason}")
            logger.warning(f"[EXEC_QUALITY] {warnings[-1]}")

    if taker_kpis["fee_ratio"] > TAKER_ALERT_FEE_RATIO and taker_kpis["trades"] > 0:
        warnings.append(
            f"TAKER_FEE_HIGH: fees={taker_kpis['fee_ratio']*100:.1f}% "
            f"of gross PnL (threshold={TAKER_ALERT_FEE_RATIO*100:.0f}%)"
        )

    if fallback_rate > FALLBACK_RATE_WARNING and total >= 5:
        warnings.append(
            f"FALLBACK_RATE_HIGH: {fallback_rate*100:.1f}% of trades are taker fallback "
            f"(threshold={FALLBACK_RATE_WARNING*100:.0f}%)"
        )

    result = {
        "maker": maker_kpis,
        "taker": taker_kpis,
        "fallback_rate": round(fallback_rate, 4),
        "taker_degrading_edge": degrading,
        "taker_degrading_reason": degrading_reason,
        "warnings": warnings,
    }

    logger.info(
        f"[EXEC_QUALITY] maker={maker_kpis['trades']} trades (PF={maker_kpis['profit_factor']:.2f}) | "
        f"taker={taker_kpis['trades']} trades (PF={taker_kpis['profit_factor']:.2f}) | "
        f"fallback_rate={fallback_rate*100:.1f}% | degrading={degrading}"
    )

    return result


# ── Gate for Adaptive Weight ──────────────────────────────────────────

def is_taker_blocking_promotion(
    exec_quality: Dict[str, Any],
) -> Tuple[bool, str]:
    """
    Check if taker performance should block adaptive weight promotion.

    Blocking conditions:
        1. TAKER_DEGRADING_EDGE flag is active
        2. Fallback rate > 40% AND taker PF < 1.1

    Returns (is_blocking, reason).
    """
    if exec_quality.get("taker_degrading_edge", False):
        reason = exec_quality.get("taker_degrading_reason", "Taker PF below threshold")
        return True, f"TAKER_DEGRADING_EDGE: {reason}"

    fallback_rate = exec_quality.get("fallback_rate", 0.0)
    taker_pf = exec_quality.get("taker", {}).get("profit_factor", 0.0)
    taker_trades = exec_quality.get("taker", {}).get("trades", 0)

    if fallback_rate > FALLBACK_RATE_WARNING and taker_trades >= TAKER_DEGRADING_MIN_TRADES and taker_pf < 1.1:
        return True, (
            f"HIGH_FALLBACK_RATE: rate={fallback_rate*100:.1f}% + taker PF={taker_pf:.2f} < 1.1"
        )

    return False, ""


# ── Gate for Capital Scaling ──────────────────────────────────────────

def is_taker_blocking_scaleup(
    exec_quality: Dict[str, Any],
) -> Tuple[bool, str]:
    """
    Check if taker performance should block capital scale-up.

    Blocks if taker PF < 1.1 (with enough sample size).
    """
    taker = exec_quality.get("taker", {})
    if taker.get("trades", 0) >= TAKER_DEGRADING_MIN_TRADES:
        pf = taker.get("profit_factor", 0.0)
        if pf < 1.1:
            return True, f"Taker PF={pf:.2f} < 1.1 — capital scale-up blocked"
    return False, ""
