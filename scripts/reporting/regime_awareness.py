"""
Regime Awareness Orchestrator — Phase 9

Combines the three regime-aware sub-systems into a single evaluation:
  1. Market Regime Detector (TREND / RANGE / CHOP)
  2. Volatility Pause Logic (freeze on ATR explosion)
  3. Drawdown Recovery Mode (progressive return after DD)

Produces a unified regime state that governs adaptive weight & scaling caps.

Priority order (highest → lowest):
  1. Recovery mode (overrides everything)
  2. Volatility pause (overrides regime caps)
  3. Market regime (base caps)

Author: SMC Trading Bot Framework — Phase 9
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List

from scripts.reporting.regime_detector import (
    analyze_market_regime,
    REGIME_TREND,
    REGIME_RANGE,
    REGIME_CHOP,
)
from scripts.reporting.volatility_pause import (
    evaluate_volatility_pause,
    VOL_NORMAL,
    VOL_ELEVATED,
    VOL_EXTREME,
)
from scripts.reporting.drawdown_recovery import (
    evaluate_recovery_mode,
    RECOVERY_PROGRESSIVE_STEPS,
)

logger = logging.getLogger(__name__)


def evaluate_regime_awareness(
    # ── Market regime inputs ──
    adx: float = 0.0,
    atr_ratio: float = 1.0,
    bos_confirm_rate: float = 1.0,
    candles: Optional[List[Dict[str, float]]] = None,
    atr_values: Optional[List[float]] = None,
    bos_total: int = 0,
    bos_confirmed: int = 0,
    # ── Volatility pause inputs ──
    fallback_rate: float = 0.0,
    current_vol_pause: bool = False,
    # ── Drawdown recovery inputs ──
    current_dd_pct: float = 0.0,
    consecutive_win_days: int = 0,
    recovery_pf: float = 0.0,
    recovery_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Unified regime evaluation combining all three sub-systems.

    Returns:
        Dict with keys:
            regime:              str (TREND/RANGE/CHOP)
            regime_reason:       str
            volatility_state:    str (NORMAL/ELEVATED/EXTREME)
            vol_pause_active:    bool
            recovery_active:     bool
            recovery_step:       int
            adaptive_weight_cap: float (final, after all overrides)
            scaling_cap_factor:  float (final)
            risk_reduction_factor: float (final, compound)
            freeze_adaptive:     bool (hard block on promotion)
            block_scale_up:      bool (hard block on scale-up)
            effective_regime:    str (what's actually governing)
            warnings:            list
            details:             dict (sub-system raw outputs)
    """
    # ── 1. Market Regime ──
    regime_result = analyze_market_regime(
        adx=adx,
        atr_ratio=atr_ratio,
        bos_confirm_rate=bos_confirm_rate,
        candles=candles,
        atr_values=atr_values,
        bos_total=bos_total,
        bos_confirmed=bos_confirmed,
    )

    # ── 2. Volatility Pause ──
    vol_result = evaluate_volatility_pause(
        atr_ratio=atr_ratio if atr_values is None else (
            atr_values[-1] / (sum(atr_values) / len(atr_values))
            if atr_values and len(atr_values) > 0 and sum(atr_values) > 0
            else atr_ratio
        ),
        fallback_rate=fallback_rate,
        current_pause_active=current_vol_pause,
    )

    # ── 3. Drawdown Recovery ──
    recovery_result = evaluate_recovery_mode(
        current_dd_pct=current_dd_pct,
        consecutive_win_days=consecutive_win_days,
        recovery_pf=recovery_pf,
        recovery_state=recovery_state,
    )

    # ── Compose final state (priority: recovery > vol_pause > regime) ──
    warnings = list(regime_result.get("warnings", []))

    # Start with regime caps
    adaptive_cap = regime_result["adaptive_weight_cap"]
    scaling_factor = regime_result["scaling_cap_factor"]
    risk_factor = regime_result["risk_reduction_factor"]
    freeze_adaptive = False
    block_scale_up = False
    effective_regime = regime_result["regime"]

    # Apply volatility pause (tighter caps)
    if vol_result["pause_active"]:
        # Vol pause risk factor compounds with regime factor
        risk_factor *= vol_result["risk_factor"]
        freeze_adaptive = True
        scaling_factor = min(scaling_factor, 0.5)  # cap scaling at 50%
        effective_regime = f"{regime_result['regime']}+VOL_PAUSE"
        warnings.append(f"Volatility pause: {vol_result['reason']}")

    # Apply recovery mode (highest priority, most restrictive)
    if recovery_result["recovery_active"]:
        risk_factor *= recovery_result["risk_factor"]
        if recovery_result["freeze_adaptive"]:
            freeze_adaptive = True
        if recovery_result["block_scale_up"]:
            block_scale_up = True
        adaptive_cap = 0.0 if recovery_result["freeze_adaptive"] else min(adaptive_cap, 0.1)
        scaling_factor = 0.0 if recovery_result["block_scale_up"] else min(scaling_factor, 0.5)
        effective_regime = "RECOVERY"
        warnings.append(f"Recovery mode: {recovery_result['reason']}")

    result = {
        # ── Regime classification ──
        "regime": regime_result["regime"],
        "regime_reason": regime_result["reason"],
        # ── Volatility state ──
        "volatility_state": vol_result["volatility_state"],
        "vol_pause_active": vol_result["pause_active"],
        # ── Recovery state ──
        "recovery_active": recovery_result["recovery_active"],
        "recovery_step": recovery_result.get("step", 0),
        "recovery_total_steps": len(RECOVERY_PROGRESSIVE_STEPS),
        # ── Final caps (used by adaptive & scaling engines) ──
        "adaptive_weight_cap": round(adaptive_cap, 2),
        "scaling_cap_factor": round(scaling_factor, 4),
        "risk_reduction_factor": round(risk_factor, 4),
        "freeze_adaptive": freeze_adaptive,
        "block_scale_up": block_scale_up,
        "effective_regime": effective_regime,
        # ── Metadata ──
        "warnings": warnings,
        "details": {
            "regime": regime_result,
            "volatility": vol_result,
            "recovery": recovery_result,
        },
    }

    logger.info(
        f"[REGIME_AWARE] effective={effective_regime}, "
        f"adaptive_cap={adaptive_cap:.2f}, "
        f"risk_factor={risk_factor:.4f}, "
        f"freeze={freeze_adaptive}, block_scale_up={block_scale_up}"
    )

    return result


# ── Persistence ──────────────────────────────────────────────────────

REGIME_DIR = Path("reports") / "regime"


def load_regime_state(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load persistent regime state (recovery state, vol pause state)."""
    dir_path = base_dir or REGIME_DIR
    state_path = dir_path / "state.json"
    default = {
        "vol_pause_active": False,
        "recovery_state": {
            "active": False,
            "step": 0,
            "days_at_step": 0,
            "entry_dd_pct": 0.0,
        },
    }
    if not state_path.exists():
        return default
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return {**default, **json.load(f)}
    except Exception as e:
        logger.error(f"[REGIME] Failed to load state: {e}")
        return default


def save_regime_state(state: Dict[str, Any], base_dir: Optional[Path] = None) -> None:
    """Persist regime state."""
    dir_path = base_dir or REGIME_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    try:
        with open(dir_path / "state.json", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"[REGIME] Failed to save state: {e}")


def record_regime_decision(
    result: Dict[str, Any],
    base_dir: Optional[Path] = None,
) -> None:
    """Write audit-trail JSON."""
    dir_path = base_dir or REGIME_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    fpath = dir_path / f"regime_decision_{date_str}.json"

    entry = {
        "event": "REGIME_DECISION",
        "ts": datetime.now(timezone.utc).isoformat(),
        "effective_regime": result.get("effective_regime"),
        "regime": result.get("regime"),
        "volatility_state": result.get("volatility_state"),
        "recovery_active": result.get("recovery_active"),
        "adaptive_weight_cap": result.get("adaptive_weight_cap"),
        "scaling_cap_factor": result.get("scaling_cap_factor"),
        "risk_reduction_factor": result.get("risk_reduction_factor"),
        "freeze_adaptive": result.get("freeze_adaptive"),
        "block_scale_up": result.get("block_scale_up"),
        "warnings": result.get("warnings", []),
    }

    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2)
        logger.info(f"[REGIME] Decision recorded: {fpath}")
    except Exception as e:
        logger.error(f"[REGIME] Failed to record decision: {e}")
