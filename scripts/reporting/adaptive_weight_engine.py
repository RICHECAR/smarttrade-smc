"""
Adaptive Weight Engine — Phase 8

Deterministic Finite State Machine that governs ADAPTIVE_LIVE_WEIGHT
progression: SHADOW(0.0) → LIVE_10(0.1) → LIVE_30(0.3) → LIVE_50(0.5).

Rules:
    - No skipping steps
    - Promotion requires ALL KPI gates on BOTH 7d and 30d windows
    - Rollback is immediate on ANY single failure criterion
    - Cooldowns: 48h between promotions, 24h after rollback before any promotion
    - Max 1 weight change per calendar day

Author: SMC Trading Bot Framework — Phase 8
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple, Dict, Any, Optional, List

from scripts.reporting.regime_objectives import check_regime_promotion_gate

logger = logging.getLogger(__name__)

# ── FSM Weight Steps ──────────────────────────────────────────────────
WEIGHT_STEPS = [0.0, 0.1, 0.3, 0.5]

# ── Cooldown Durations (seconds) ─────────────────────────────────────
COOLDOWN_PROMOTION_SECS = 48 * 3600   # 48h between promotions
COOLDOWN_ROLLBACK_SECS = 24 * 3600    # 24h after rollback before promotion

# ── Promotion Thresholds ─────────────────────────────────────────────
MIN_DECISIONS = 30
MIN_PROFIT_FACTOR_B = 1.2
MAX_WINRATE_DELTA = 0.03       # WR(B) ≥ WR(A) − 3%
MAX_DD_RATIO = 1.2             # DD(B) ≤ 1.2 × DD(A)
MAX_LOSING_STREAK_B = 4        # < 5
# ── Taker / Fallback Promotion Gates (new) ──────────────────────
MIN_TAKER_PF_FOR_PROMOTION = 1.0   # Taker PF must be ≥ 1.0
MAX_FALLBACK_RATE = 0.40           # Fallback rate must be < 40%

# ── Adaptive Kill-Switch at 30% ─────────────────────────────
ADAPTIVE_KS_DD_RATIO_7D = 1.3     # DD(B,7d) > 1.3×DD(A) → revert to 0.1
# ── Rollback Thresholds ─────────────────────────────────────────────
ROLLBACK_MIN_WINRATE_B = 0.40
ROLLBACK_MIN_PF_B = 1.0
ROLLBACK_MAX_DD_RATIO = 1.5    # DD(B) > 1.5 × DD(A)
ROLLBACK_MAX_LOSING_STREAK = 5 # ≥ 6

# ── Confidence Levels ────────────────────────────────────────────────
def compute_confidence(decisions: int, kpis: Dict[str, Any]) -> str:
    """
    LOW:    < 20 comparable decisions
    MEDIUM: ≥ 30 decisions + KPIs stable
    HIGH:   ≥ 50 decisions + clear outperformance
    """
    if decisions < 20:
        return "LOW"
    if decisions >= 50:
        exp_b = kpis.get("expectancy_B", 0)
        exp_a = kpis.get("expectancy_A", 0)
        pf_b = kpis.get("profit_factor_B", 0)
        if exp_b > exp_a and pf_b >= MIN_PROFIT_FACTOR_B:
            return "HIGH"
    if decisions >= 30:
        return "MEDIUM"
    return "LOW"


# ── Core Logic ───────────────────────────────────────────────────────

def _check_rollback(kpis: Dict[str, Any]) -> Optional[str]:
    """
    Check if ANY rollback criterion is met.
    Returns a reason string, or None if no rollback needed.
    One trigger is enough.
    """
    winrate_b = kpis.get("winrate_B", 1.0)
    pf_b = kpis.get("profit_factor_B", 999)
    dd_b = kpis.get("drawdown_B", 0)
    dd_a = kpis.get("drawdown_A", 1)
    losing_streak = kpis.get("losing_streak_B", 0)

    if winrate_b < ROLLBACK_MIN_WINRATE_B:
        return f"WR(B)={winrate_b:.2f} < {ROLLBACK_MIN_WINRATE_B}"

    if pf_b < ROLLBACK_MIN_PF_B:
        return f"PF(B)={pf_b:.2f} < {ROLLBACK_MIN_PF_B}"

    if dd_a > 0 and dd_b > ROLLBACK_MAX_DD_RATIO * dd_a:
        return f"DD(B)={dd_b:.2f} > {ROLLBACK_MAX_DD_RATIO}×DD(A)={dd_a:.2f}"

    if losing_streak >= ROLLBACK_MAX_LOSING_STREAK + 1:  # ≥ 6
        return f"Losing streak B={losing_streak} >= 6"

    return None


def _check_promotion(kpis: Dict[str, Any]) -> bool:
    """
    Returns True only if ALL promotion KPI gates pass.
    """
    decisions = kpis.get("decisions", 0)
    if decisions < MIN_DECISIONS:
        return False

    exp_b = kpis.get("expectancy_B", 0)
    exp_a = kpis.get("expectancy_A", 0)
    if exp_b < exp_a:
        return False

    pf_b = kpis.get("profit_factor_B", 0)
    if pf_b < MIN_PROFIT_FACTOR_B:
        return False

    wr_b = kpis.get("winrate_B", 0)
    wr_a = kpis.get("winrate_A", 0)
    if wr_b < wr_a - MAX_WINRATE_DELTA:
        return False

    dd_b = kpis.get("drawdown_B", 0)
    dd_a = kpis.get("drawdown_A", 1)
    if dd_a > 0 and dd_b > MAX_DD_RATIO * dd_a:
        return False

    losing_streak = kpis.get("losing_streak_B", 0)
    if losing_streak >= MAX_LOSING_STREAK_B + 1:  # must be < 5
        return False

    return True


def next_weight(current: float) -> float:
    """Return the next weight step. No skipping."""
    try:
        idx = WEIGHT_STEPS.index(current)
    except ValueError:
        # If current weight is not a recognized step, snap to nearest lower step
        idx = 0
        for i, w in enumerate(WEIGHT_STEPS):
            if w <= current:
                idx = i
    return WEIGHT_STEPS[min(idx + 1, len(WEIGHT_STEPS) - 1)]


def evaluate_adaptive_weight(
    current_weight: float,
    kpis_7d: Dict[str, Any],
    kpis_30d: Dict[str, Any],
    state: Dict[str, Any],
    exec_quality: Optional[Dict[str, Any]] = None,
    regime_info: Optional[Dict[str, Any]] = None,
) -> Tuple[float, str]:
    """
    Deterministic decision engine for ADAPTIVE_LIVE_WEIGHT.

    Args:
        current_weight: Current ADAPTIVE_LIVE_WEIGHT (0.0 – 0.5)
        kpis_7d:  KPIs computed over last 7 calendar days
        kpis_30d: KPIs computed over last 30 calendar days
        state:    Persistent state dict with keys:
                    - last_change_ts: ISO timestamp of last weight change
                    - last_decision:  "PROMOTE" | "ROLLBACK" | "HOLD" | "COOLDOWN"
                    - cooldown_ok:    bool (pre-computed by caller)
        exec_quality: Optional execution quality dict from analyze_execution_quality().
                      Used for taker degradation and fallback rate gates.
        regime_info:  Optional regime awareness dict from evaluate_regime_awareness().
                      Used for regime-based caps and freeze overrides.

    Returns:
        (new_weight, decision)  where decision is one of
        "PROMOTE", "HOLD", "ROLLBACK", "COOLDOWN"
    """
    # ── 0. Regime freeze override (Phase 9) ────────────────────
    if regime_info and regime_info.get("freeze_adaptive", False):
        logger.info(
            f"[ADAPTIVE_WEIGHT] HOLD: frozen by regime "
            f"({regime_info.get('effective_regime', '?')})"
        )
        return current_weight, "HOLD"

    # ── 1. Rollback check (priority — ignores cooldowns) ─────────────
    reason_7d = _check_rollback(kpis_7d)
    reason_30d = _check_rollback(kpis_30d)

    if reason_7d or reason_30d:
        reason = reason_7d or reason_30d
        logger.warning(
            f"[ADAPTIVE_WEIGHT] ROLLBACK triggered: {reason}  "
            f"(current={current_weight})"
        )
        return 0.0, "ROLLBACK"

    # ── 1b. Adaptive Kill-Switch at 30%: DD(B,7d) > 1.3×DD(A) ──────
    if current_weight >= 0.3:
        dd_b_7d = kpis_7d.get("drawdown_B", 0)
        dd_a_7d = kpis_7d.get("drawdown_A", 1)
        if dd_a_7d > 0 and dd_b_7d > ADAPTIVE_KS_DD_RATIO_7D * dd_a_7d:
            logger.warning(
                f"[ADAPTIVE_WEIGHT] ADAPTIVE_KS_30: DD(B,7d)={dd_b_7d:.2f} > "
                f"{ADAPTIVE_KS_DD_RATIO_7D}×DD(A)={dd_a_7d:.2f} → revert to 0.1"
            )
            return 0.1, "ROLLBACK"

    # ── 2. Cooldown guard ────────────────────────────────────────────
    if not state.get("cooldown_ok", True):
        logger.info(
            f"[ADAPTIVE_WEIGHT] COOLDOWN active — weight stays at {current_weight}"
        )
        return current_weight, "COOLDOWN"

    # ── 3. Promotion check (both windows must pass) ──────────────────
    promo_7d = _check_promotion(kpis_7d)
    promo_30d = _check_promotion(kpis_30d)

    if promo_7d and promo_30d:
        # ── 3b. Taker/Fallback gates block promotion ───────────────
        if exec_quality:
            # Gate 1: TAKER_DEGRADING_EDGE blocks all promotions
            if exec_quality.get("taker_degrading_edge", False):
                reason = exec_quality.get("taker_degrading_reason", "Taker PF degraded")
                logger.info(
                    f"[ADAPTIVE_WEIGHT] HOLD: promotion blocked by TAKER_DEGRADING_EDGE: {reason}"
                )
                return current_weight, "HOLD"

            # Gate 2: Taker PF must be >= 1.0
            taker = exec_quality.get("taker", {})
            taker_trades = taker.get("trades", 0)
            taker_pf = taker.get("profit_factor", 999.0)
            if taker_trades >= 5 and taker_pf < MIN_TAKER_PF_FOR_PROMOTION:
                logger.info(
                    f"[ADAPTIVE_WEIGHT] HOLD: taker PF={taker_pf:.2f} < {MIN_TAKER_PF_FOR_PROMOTION}"
                )
                return current_weight, "HOLD"

            # Gate 3: Fallback rate must be < 40%
            fallback_rate = exec_quality.get("fallback_rate", 0.0)
            if fallback_rate > MAX_FALLBACK_RATE and taker_trades >= 5:
                logger.info(
                    f"[ADAPTIVE_WEIGHT] HOLD: fallback rate={fallback_rate*100:.1f}% > {MAX_FALLBACK_RATE*100:.0f}%"
                )
                return current_weight, "HOLD"

        # ── 3b2. Regime-conditioned promotion gate (Phase 11A) ──
        if regime_info:
            current_regime = regime_info.get("effective_regime", regime_info.get("regime", "RANGE"))
            gate_pass, gate_reason = check_regime_promotion_gate(current_regime, kpis_7d)
            if not gate_pass:
                logger.info(
                    f"[ADAPTIVE_WEIGHT] HOLD: regime promotion gate blocked: {gate_reason}"
                )
                return current_weight, "HOLD"

        new_w = next_weight(current_weight)
        # ── 3c. Regime cap: don't promote above regime cap ─────────
        if regime_info:
            regime_cap = regime_info.get("adaptive_weight_cap", 0.5)
            if new_w > regime_cap:
                logger.info(
                    f"[ADAPTIVE_WEIGHT] HOLD: promotion to {new_w} blocked "
                    f"by regime cap={regime_cap} ({regime_info.get('regime', '?')})"
                )
                return current_weight, "HOLD"
        if new_w != current_weight:
            logger.info(
                f"[ADAPTIVE_WEIGHT] PROMOTE: {current_weight} → {new_w}"
            )
            return new_w, "PROMOTE"
        # Already at max
        return current_weight, "HOLD"

    # ── 4. Default: hold ─────────────────────────────────────────────
    return current_weight, "HOLD"


# ── Persistence helpers ──────────────────────────────────────────────

DECISIONS_DIR = Path("reports") / "adaptive"


def load_state(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load the latest persistent state from reports/adaptive/state.json."""
    state_path = (base_dir or DECISIONS_DIR) / "state.json"
    default = {
        "last_change_ts": "1970-01-01T00:00:00+00:00",
        "last_decision": "HOLD",
        "cooldown_ok": True,
        "current_weight": 0.0,
    }
    if not state_path.exists():
        return default
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**default, **data}
    except Exception as e:
        logger.error(f"[ADAPTIVE_WEIGHT] Failed to load state: {e}")
        return default


def save_state(state: Dict[str, Any], base_dir: Optional[Path] = None) -> None:
    """Persist state to reports/adaptive/state.json."""
    dir_path = base_dir or DECISIONS_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    state_path = dir_path / "state.json"
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"[ADAPTIVE_WEIGHT] Failed to save state: {e}")


def compute_cooldown_ok(state: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """
    Determine if cooldown has elapsed.
    - After PROMOTE: 48h
    - After ROLLBACK: 24h
    - Otherwise: True
    """
    now = now or datetime.now(timezone.utc)
    last_ts_str = state.get("last_change_ts", "1970-01-01T00:00:00+00:00")
    try:
        last_ts = datetime.fromisoformat(last_ts_str)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True

    elapsed = (now - last_ts).total_seconds()

    last_decision = state.get("last_decision", "HOLD")
    if last_decision == "PROMOTE":
        return elapsed >= COOLDOWN_PROMOTION_SECS
    if last_decision == "ROLLBACK":
        return elapsed >= COOLDOWN_ROLLBACK_SECS
    return True


def record_decision(
    decision: str,
    previous_weight: float,
    new_weight: float,
    kpis_7d: Dict[str, Any],
    kpis_30d: Dict[str, Any],
    confidence: str,
    reason: str = "",
    base_dir: Optional[Path] = None,
) -> None:
    """
    Write an audit-trail JSON to reports/adaptive/weight_decisions_YYYYMMDD.json.
    """
    dir_path = base_dir or DECISIONS_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    file_path = dir_path / f"weight_decisions_{date_str}.json"

    entry = {
        "event": "ADAPTIVE_WEIGHT_DECISION",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "previous_weight": previous_weight,
        "new_weight": new_weight,
        "decision": decision,
        "confidence": confidence,
        "reason": reason,
        "kpis_7d": kpis_7d,
        "kpis_30d": kpis_30d,
        "cooldown_ok": True,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2)
        logger.info(f"[ADAPTIVE_WEIGHT] Decision recorded: {file_path}")
    except Exception as e:
        logger.error(f"[ADAPTIVE_WEIGHT] Failed to record decision: {e}")
