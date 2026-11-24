"""
Capital Scaling Engine — Phase 8.3

Adjusts risk_per_trade (risk_pct) progressively based on:
  - 30-day rolling performance (PF, DD, trade count)
  - ADAPTIVE_LIVE_WEIGHT (higher weight → more conservative cap)
  - Equity (start-of-day)

Design:
  - Decisions are taken daily in generate_daily_report.py
  - Artifacts written to reports/scaling/scaling_decision_YYYYMMDD.json
  - Respects MIN/MAX caps and cooldown between changes
  - Deterministic, testable, auditable

Config keys:
  - RISK_PCT_BASE       (default 0.5)
  - RISK_PCT_MIN        (default 0.2)
  - RISK_PCT_MAX        (default 0.8)
  - SCALE_UP_STEP       (default 0.05)
  - SCALE_DOWN_STEP     (default 0.10)
  - SCALE_COOLDOWN_HOURS (default 24)
  - SCALE_PF_30D_MIN    (default 1.2)
  - SCALE_DD_30D_MAX    (default 5.0)
  - SCALE_TRADES_30D_MIN (default 30)
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from scripts.reporting.regime_objectives import check_regime_scaling_gate

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────
DEFAULTS = {
    "RISK_PCT_BASE": 0.5,
    "RISK_PCT_MIN": 0.2,
    "RISK_PCT_MAX": 0.8,
    "SCALE_UP_STEP": 0.05,
    "SCALE_DOWN_STEP": 0.10,
    "SCALE_COOLDOWN_HOURS": 24,
    "SCALE_PF_30D_MIN": 1.25,
    "SCALE_DD_30D_MAX": 5.0,
    "SCALE_TRADES_30D_MIN": 50,
    "SCALE_MIN_ADAPTIVE_WEIGHT": 0.3,
    "SCALE_MAX_LOSING_STREAK": 5,
}


def effective_max_risk(risk_pct_max: float, adaptive_weight: float) -> float:
    """
    Compute the effective max risk, reduced by adaptive weight exposure.

    Formula: effective_max = RISK_PCT_MAX * (1.0 - 0.5 * ADAPTIVE_LIVE_WEIGHT)
    Example: weight=0.5 → max risk reduced by 25%
    """
    reduction = 1.0 - 0.5 * max(0.0, min(1.0, adaptive_weight))
    return round(risk_pct_max * reduction, 4)


def effective_risk_pct(risk_pct: float, adaptive_weight: float) -> float:
    """
    Compute effective risk per trade, coupled with adaptive weight.

    Formula: effective = risk_pct * (1 - 0.5 * adaptive_weight)
    More adaptive → more conservative risk sizing.

    Example: risk=0.6%, weight=0.3 → effective ≈ 0.51%
    """
    reduction = 1.0 - 0.5 * max(0.0, min(1.0, adaptive_weight))
    return round(risk_pct * reduction, 4)


def evaluate_scaling(
    current_risk_pct: float,
    kpis_30d: Dict[str, Any],
    adaptive_weight: float = 0.0,
    state: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    exec_quality: Optional[Dict[str, Any]] = None,
    kill_switch_active: bool = False,
    regime_info: Optional[Dict[str, Any]] = None,
) -> Tuple[float, str, str]:
    """
    Deterministic scaling decision engine.

    Args:
        current_risk_pct:  Current risk per trade (%)
        kpis_30d:          Dict with keys: profit_factor, max_dd_pct, trades, winrate,
                           losing_streak (optional)
        adaptive_weight:   Current ADAPTIVE_LIVE_WEIGHT (0.0–0.5)
        state:             Persistent state dict (last_change_ts, last_decision)
        config:            Override defaults for thresholds
        exec_quality:      Optional execution quality dict (for taker PF gate)
        kill_switch_active: Whether the global kill-switch was triggered
        regime_info:       Optional regime awareness dict from evaluate_regime_awareness().
                           Used for regime-based scaling caps and blocks.

    Returns:
        (new_risk_pct, decision, reason)
        decision: "SCALE_UP" | "SCALE_DOWN" | "HOLD"
    """
    cfg = {**DEFAULTS, **(config or {})}
    state = state or {}

    risk_min = cfg["RISK_PCT_MIN"]
    risk_max = cfg["RISK_PCT_MAX"]
    up_step = cfg["SCALE_UP_STEP"]
    down_step = cfg["SCALE_DOWN_STEP"]
    pf_min = cfg["SCALE_PF_30D_MIN"]
    dd_max = cfg["SCALE_DD_30D_MAX"]
    trades_min = cfg["SCALE_TRADES_30D_MIN"]
    cooldown_hours = cfg["SCALE_COOLDOWN_HOURS"]
    min_adaptive = cfg["SCALE_MIN_ADAPTIVE_WEIGHT"]
    max_losing = cfg["SCALE_MAX_LOSING_STREAK"]

    # Effective max (reduced by adaptive weight)
    eff_max = effective_max_risk(risk_max, adaptive_weight)

    # Apply regime scaling cap factor (Phase 9)
    if regime_info:
        regime_scale_factor = regime_info.get("scaling_cap_factor", 1.0)
        eff_max = round(eff_max * regime_scale_factor, 4)
        # Apply regime risk reduction
        regime_risk_factor = regime_info.get("risk_reduction_factor", 1.0)
        if regime_risk_factor < 1.0:
            risk_max = round(risk_max * regime_risk_factor, 4)

    pf = kpis_30d.get("profit_factor", 0.0)
    dd = kpis_30d.get("max_dd_pct", 0.0)
    trades = kpis_30d.get("trades", 0)
    losing_streak = kpis_30d.get("losing_streak", 0)

    # ── 1. SCALE DOWN checks (priority) ──────────────────────────────
    if pf < 1.0 and trades >= 5:
        new_pct = max(risk_min, round(current_risk_pct - down_step, 4))
        reason = f"PF={pf:.2f} < 1.0 → scale down"
        return new_pct, "SCALE_DOWN", reason

    if dd > dd_max:
        new_pct = max(risk_min, round(current_risk_pct - down_step, 4))
        reason = f"DD={dd:.2f}% > {dd_max}% → scale down"
        return new_pct, "SCALE_DOWN", reason

    if kill_switch_active:
        new_pct = max(risk_min, round(current_risk_pct - down_step, 4))
        reason = "Kill-switch triggered → scale down"
        return new_pct, "SCALE_DOWN", reason

    if losing_streak >= max_losing + 1:  # ≥ 6
        new_pct = max(risk_min, round(current_risk_pct - down_step, 4))
        reason = f"Losing streak={losing_streak} >= {max_losing + 1} → scale down"
        return new_pct, "SCALE_DOWN", reason

    # ── 2. Cooldown guard ────────────────────────────────────────────
    if not _cooldown_ok(state, cooldown_hours):
        return current_risk_pct, "HOLD", "Cooldown active"

    # ── 2b. Regime block_scale_up (Phase 9) ───────────────────────
    if regime_info and regime_info.get("block_scale_up", False):
        return current_risk_pct, "HOLD", (
            f"Scale-up blocked by regime: {regime_info.get('effective_regime', '?')}"
        )

    # ── 3. SCALE UP checks ───────────────────────────────────────────
    if trades >= trades_min and pf >= pf_min and dd <= dd_max:
        # Gate: regime-conditioned scaling (Phase 11A)
        if regime_info:
            current_regime = regime_info.get("effective_regime", regime_info.get("regime", "RANGE"))
            regime_gate_pass, regime_gate_reason = check_regime_scaling_gate(current_regime, kpis_30d)
            if not regime_gate_pass:
                return current_risk_pct, "HOLD", (
                    f"Scale-up blocked by regime objectives: {regime_gate_reason}"
                )

        # Gate: adaptive weight must be >= minimum (default 0.3)
        if adaptive_weight < min_adaptive:
            return current_risk_pct, "HOLD", (
                f"Adaptive weight={adaptive_weight:.2f} < {min_adaptive} "
                f"→ scale-up blocked until adaptive stable"
            )

        # Gate: taker PF must be >= 1.1 (if enough taker data)
        if exec_quality:
            taker = exec_quality.get("taker", {})
            if taker.get("trades", 0) >= 5 and taker.get("profit_factor", 999) < 1.1:
                return current_risk_pct, "HOLD", (
                    f"Taker PF={taker['profit_factor']:.2f} < 1.1 "
                    f"→ scale-up blocked by taker quality"
                )

        new_pct = min(eff_max, round(current_risk_pct + up_step, 4))
        if new_pct > current_risk_pct:
            reason = (
                f"PF={pf:.2f}>={pf_min}, DD={dd:.2f}%<={dd_max}%, "
                f"trades={trades}>={trades_min}, adaptive={adaptive_weight:.2f} "
                f"→ scale up (eff_max={eff_max:.2f}%)"
            )
            return new_pct, "SCALE_UP", reason

    # ── 4. Enforce cap even without decision ─────────────────────────
    if current_risk_pct > eff_max:
        new_pct = eff_max
        reason = f"Current {current_risk_pct:.2f}% > eff_max {eff_max:.2f}% → capped"
        return new_pct, "SCALE_DOWN", reason

    return current_risk_pct, "HOLD", "No scaling conditions met"


# ── Cooldown helper ──────────────────────────────────────────────────

def _cooldown_ok(state: Dict[str, Any], cooldown_hours: int) -> bool:
    """Check if enough time has elapsed since last scaling change."""
    last_ts_str = state.get("last_change_ts")
    if not last_ts_str:
        return True
    try:
        last_ts = datetime.fromisoformat(last_ts_str)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds()
        return elapsed >= cooldown_hours * 3600
    except (ValueError, TypeError):
        return True


# ── Persistence ──────────────────────────────────────────────────────

SCALING_DIR = Path("reports") / "scaling"


def load_scaling_state(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load persistent scaling state."""
    dir_path = base_dir or SCALING_DIR
    state_path = dir_path / "state.json"
    default = {
        "current_risk_pct": DEFAULTS["RISK_PCT_BASE"],
        "last_change_ts": None,
        "last_decision": "HOLD",
    }
    if not state_path.exists():
        return default
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return {**default, **json.load(f)}
    except Exception as e:
        logger.error(f"[SCALING] Failed to load state: {e}")
        return default


def save_scaling_state(state: Dict[str, Any], base_dir: Optional[Path] = None) -> None:
    """Persist scaling state."""
    dir_path = base_dir or SCALING_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    try:
        with open(dir_path / "state.json", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"[SCALING] Failed to save state: {e}")


def record_scaling_decision(
    decision: str,
    previous: float,
    new: float,
    reason: str,
    kpis_30d: Dict[str, Any],
    adaptive_weight: float,
    eff_max: float,
    base_dir: Optional[Path] = None,
) -> None:
    """Write audit-trail JSON."""
    dir_path = base_dir or SCALING_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    fpath = dir_path / f"scaling_decision_{date_str}.json"
    entry = {
        "event": "SCALING_DECISION",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "previous_risk_pct": previous,
        "new_risk_pct": new,
        "decision": decision,
        "reason": reason,
        "kpis_30d": kpis_30d,
        "adaptive_weight": adaptive_weight,
        "effective_max_risk": eff_max,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2)
        logger.info(f"[SCALING] Decision recorded: {fpath}")
    except Exception as e:
        logger.error(f"[SCALING] Failed to record decision: {e}")
