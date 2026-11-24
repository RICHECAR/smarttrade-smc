"""
AdaptiveRiskEngine — Phase 7: Dynamic Risk Scaling
════════════════════════════════════════════════════
Adjusts position sizing, leverage, and risk per trade based on:

  1. Volatility (ATR ratio)
  2. Recent drawdown
  3. Recent winrate & profit factor
  4. Market regime
  5. Execution quality (slippage, fill delay, kills)

Design:
  - Pure function: evaluate(inputs) → RiskDecision
  - No side effects on strategy logic
  - Acts only on size / risk / leverage / block
  - Deterministic, auditable, conservative by default
  - Never boosts above nominal (max_mult=1.0 default)
  - Falls back to NORMAL on missing data

Integration point:
  OrderManager.execute() — between pre_flight_checks() and execute_signal()
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("adaptive_risk")


# ═══════════════════════════════════════════════════════════════════════
# Configuration (from ENV with safe defaults)
# ═══════════════════════════════════════════════════════════════════════

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, str(default)).lower()
    return val in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AdaptiveRiskConfig:
    """Immutable configuration — loaded once from ENV."""

    enabled: bool = True

    # ── Multiplier bounds ─────────────────────────────────
    min_mult: float = 0.25
    max_mult: float = 1.00       # Never boost above nominal
    defensive_mult: float = 0.50

    # ── Volatility thresholds (ATR ratio) ─────────────────
    atr_reduced: float = 1.5
    atr_defensive: float = 2.0
    atr_block: float = 2.8
    block_on_extreme_vol: bool = True

    # ── Drawdown thresholds (% of equity) ─────────────────
    dd_reduced_pct: float = 1.5
    dd_defensive_pct: float = 2.5
    dd_block_pct: float = 3.5

    # ── Performance window ────────────────────────────────
    wr_window: int = 20
    min_wr: float = 0.45
    min_pf: float = 1.05

    # ── Execution quality ─────────────────────────────────
    max_recent_kills: int = 2
    max_recent_slippage_bps: float = 35.0
    max_recent_fill_delay_ms: float = 2000.0
    block_on_execution_degrade: bool = True

    # ── Regime adjustments ────────────────────────────────
    reduce_in_low_vol: bool = True
    reduce_in_range_for_breakout: bool = True
    reduce_in_trend_for_meanrev: bool = True

    @classmethod
    def from_env(cls) -> "AdaptiveRiskConfig":
        return cls(
            enabled=_env_bool("ADAPTIVE_RISK_ENABLED", True),
            min_mult=_env_float("ADAPTIVE_RISK_MIN_MULT", 0.25),
            max_mult=_env_float("ADAPTIVE_RISK_MAX_MULT", 1.00),
            defensive_mult=_env_float("ADAPTIVE_RISK_DEFENSIVE_MULT", 0.50),
            atr_reduced=_env_float("ADAPTIVE_RISK_ATR_REDUCED", 1.5),
            atr_defensive=_env_float("ADAPTIVE_RISK_ATR_DEFENSIVE", 2.0),
            atr_block=_env_float("ADAPTIVE_RISK_ATR_BLOCK", 2.8),
            block_on_extreme_vol=_env_bool("ADAPTIVE_RISK_BLOCK_ON_EXTREME_VOL", True),
            dd_reduced_pct=_env_float("ADAPTIVE_RISK_DD_REDUCED_PCT", 1.5),
            dd_defensive_pct=_env_float("ADAPTIVE_RISK_DD_DEFENSIVE_PCT", 2.5),
            dd_block_pct=_env_float("ADAPTIVE_RISK_DD_BLOCK_PCT", 3.5),
            wr_window=_env_int("ADAPTIVE_RISK_WR_WINDOW", 20),
            min_wr=_env_float("ADAPTIVE_RISK_MIN_WR", 0.45),
            min_pf=_env_float("ADAPTIVE_RISK_MIN_PF", 1.05),
            max_recent_kills=_env_int("ADAPTIVE_RISK_MAX_RECENT_KILLS", 2),
            max_recent_slippage_bps=_env_float("ADAPTIVE_RISK_MAX_RECENT_SLIPPAGE_BPS", 35.0),
            max_recent_fill_delay_ms=_env_float("ADAPTIVE_RISK_MAX_RECENT_FILL_DELAY_MS", 2000.0),
            block_on_execution_degrade=_env_bool("ADAPTIVE_RISK_BLOCK_ON_EXECUTION_DEGRADE", True),
            reduce_in_low_vol=_env_bool("ADAPTIVE_RISK_REDUCE_IN_LOW_VOL", True),
            reduce_in_range_for_breakout=_env_bool("ADAPTIVE_RISK_REDUCE_IN_RANGE_FOR_BREAKOUT", True),
            reduce_in_trend_for_meanrev=_env_bool("ADAPTIVE_RISK_REDUCE_IN_TREND_FOR_MEANREV", True),
        )


# ═══════════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class RiskInputs:
    """All inputs the engine considers."""

    # Volatility
    atr_ratio: float = 1.0        # current_atr / avg_atr

    # Drawdown
    daily_dd_pct: float = 0.0     # Current daily drawdown %
    rolling_dd_pct: float = 0.0   # Rolling N-trade drawdown %

    # Recent performance
    recent_wr: Optional[float] = None     # 0.0–1.0, None = insufficient data
    recent_pf: Optional[float] = None     # Profit factor, None = insufficient
    recent_trades_count: int = 0          # How many trades in window

    # Regime
    regime: str = "NORMAL"        # TREND / RANGE / HIGH_VOL / LOW_VOL / NORMAL
    strategy_type: str = ""       # "breakout" / "mean_reversion" / etc.

    # Execution quality
    avg_slippage_bps: float = 0.0
    avg_fill_delay_ms: float = 0.0
    recent_kills: int = 0

    # Base config
    base_risk_pct: float = 0.01   # From config
    base_leverage: int = 3


@dataclass
class RiskDecision:
    """Output of the adaptive risk evaluation."""

    risk_multiplier: float = 1.0
    effective_risk_pct: float = 0.01
    effective_leverage: int = 3
    size_multiplier: float = 1.0
    mode: str = "normal"          # normal / reduced / defensive / blocked
    reasons: List[str] = field(default_factory=list)
    inputs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "risk_multiplier": round(self.risk_multiplier, 4),
            "effective_risk_pct": round(self.effective_risk_pct, 6),
            "effective_leverage": self.effective_leverage,
            "size_multiplier": round(self.size_multiplier, 4),
            "mode": self.mode,
            "reasons": self.reasons,
            "inputs": self.inputs,
        }


# ═══════════════════════════════════════════════════════════════════════
# Modes priority (highest = worst)
# ═══════════════════════════════════════════════════════════════════════

_MODE_PRIORITY = {"normal": 0, "reduced": 1, "defensive": 2, "blocked": 3}


def _worst_mode(a: str, b: str) -> str:
    """Return the more restrictive mode."""
    return a if _MODE_PRIORITY.get(a, 0) >= _MODE_PRIORITY.get(b, 0) else b


# ═══════════════════════════════════════════════════════════════════════
# Engine
# ═══════════════════════════════════════════════════════════════════════

class AdaptiveRiskEngine:
    """
    Evaluates risk conditions and produces a RiskDecision.

    Pure computation — no side effects, no DB, no executor calls.
    All recording (Governor, Prometheus) is done by the caller.
    """

    def __init__(self, config: Optional[AdaptiveRiskConfig] = None):
        self.cfg = config or AdaptiveRiskConfig.from_env()
        logger.info(
            f"[ADAPTIVE_RISK] Init | enabled={self.cfg.enabled}"
            f" | mult=[{self.cfg.min_mult}..{self.cfg.max_mult}]"
            f" | defensive={self.cfg.defensive_mult}"
        )

    def evaluate(self, inputs: RiskInputs) -> RiskDecision:
        """
        Run all risk assessments and produce a final decision.

        Scoring: start at 1.0, apply multiplicative adjustments,
        clamp to [min_mult, max_mult], determine mode from final value.
        """
        if not self.cfg.enabled:
            return self._passthrough(inputs)

        score = 1.0
        mode = "normal"
        reasons: List[str] = []

        # ── Factor 1: Volatility ──────────────────────────
        score, mode, reasons = self._assess_volatility(
            score, mode, reasons, inputs.atr_ratio
        )

        # ── Factor 2: Drawdown ────────────────────────────
        score, mode, reasons = self._assess_drawdown(
            score, mode, reasons, inputs.daily_dd_pct, inputs.rolling_dd_pct
        )

        # ── Factor 3: Recent performance ──────────────────
        score, mode, reasons = self._assess_performance(
            score, mode, reasons, inputs
        )

        # ── Factor 4: Regime ──────────────────────────────
        score, mode, reasons = self._assess_regime(
            score, mode, reasons, inputs.regime, inputs.strategy_type
        )

        # ── Factor 5: Execution quality ───────────────────
        score, mode, reasons = self._assess_execution_quality(
            score, mode, reasons, inputs
        )

        # ── Final clamping ────────────────────────────────
        if mode == "blocked":
            score = 0.0
        else:
            score = max(self.cfg.min_mult, min(self.cfg.max_mult, score))
            # Determine mode from score if not already escalated
            if score <= self.cfg.min_mult + 0.01:
                mode = _worst_mode(mode, "defensive")
            elif score <= self.cfg.defensive_mult:
                mode = _worst_mode(mode, "defensive")
            elif score < 0.9:
                mode = _worst_mode(mode, "reduced")

        # ── Build decision ────────────────────────────────
        effective_risk = inputs.base_risk_pct * score
        effective_leverage = inputs.base_leverage
        if mode == "defensive":
            effective_leverage = max(1, inputs.base_leverage // 2)
        elif mode == "blocked":
            effective_leverage = 0

        if not reasons:
            reasons.append("all_factors_nominal")

        decision = RiskDecision(
            risk_multiplier=score,
            effective_risk_pct=effective_risk,
            effective_leverage=effective_leverage,
            size_multiplier=score,
            mode=mode,
            reasons=reasons,
            inputs={
                "atr_ratio": round(inputs.atr_ratio, 3),
                "daily_dd_pct": round(inputs.daily_dd_pct, 2),
                "rolling_dd_pct": round(inputs.rolling_dd_pct, 2),
                "recent_wr": round(inputs.recent_wr, 3) if inputs.recent_wr is not None else None,
                "recent_pf": round(inputs.recent_pf, 3) if inputs.recent_pf is not None else None,
                "regime": inputs.regime,
                "avg_slippage_bps": round(inputs.avg_slippage_bps, 1),
                "avg_fill_delay_ms": round(inputs.avg_fill_delay_ms, 0),
                "recent_kills": inputs.recent_kills,
            },
        )

        logger.info(
            f"[ADAPTIVE_RISK] {decision.mode.upper()}"
            f" | mult={decision.risk_multiplier:.2f}"
            f" | risk={decision.effective_risk_pct:.4f}"
            f" | lev={decision.effective_leverage}"
            f" | reasons={decision.reasons}"
        )

        return decision

    # ─────────────────────────────────────────────────────────────────
    # Factor assessments
    # ─────────────────────────────────────────────────────────────────

    def _assess_volatility(
        self, score: float, mode: str, reasons: List[str], atr_ratio: float
    ):
        """Adjust based on ATR ratio."""
        if atr_ratio >= self.cfg.atr_block and self.cfg.block_on_extreme_vol:
            return 0.0, "blocked", reasons + [f"vol_extreme(atr_ratio={atr_ratio:.2f})"]
        if atr_ratio >= self.cfg.atr_defensive:
            mult = self.cfg.defensive_mult
            return (
                score * mult,
                _worst_mode(mode, "defensive"),
                reasons + [f"vol_defensive(atr_ratio={atr_ratio:.2f})"],
            )
        if atr_ratio >= self.cfg.atr_reduced:
            mult = 0.75
            return (
                score * mult,
                _worst_mode(mode, "reduced"),
                reasons + [f"vol_reduced(atr_ratio={atr_ratio:.2f})"],
            )
        # atr_ratio < atr_reduced → normal, no adjustment
        return score, mode, reasons

    def _assess_drawdown(
        self,
        score: float,
        mode: str,
        reasons: List[str],
        daily_dd_pct: float,
        rolling_dd_pct: float,
    ):
        """Adjust based on drawdown."""
        dd = max(daily_dd_pct, rolling_dd_pct)
        if dd >= self.cfg.dd_block_pct:
            return 0.0, "blocked", reasons + [f"dd_critical(dd={dd:.1f}%)"]
        if dd >= self.cfg.dd_defensive_pct:
            mult = self.cfg.defensive_mult
            return (
                score * mult,
                _worst_mode(mode, "defensive"),
                reasons + [f"dd_defensive(dd={dd:.1f}%)"],
            )
        if dd >= self.cfg.dd_reduced_pct:
            mult = 0.70
            return (
                score * mult,
                _worst_mode(mode, "reduced"),
                reasons + [f"dd_reduced(dd={dd:.1f}%)"],
            )
        return score, mode, reasons

    def _assess_performance(
        self,
        score: float,
        mode: str,
        reasons: List[str],
        inputs: RiskInputs,
    ):
        """Adjust based on recent WR and PF."""
        # Insufficient data → no adjustment (conservative neutral)
        if inputs.recent_trades_count < max(5, self.cfg.wr_window // 2):
            return score, mode, reasons

        adjustments = []

        if inputs.recent_wr is not None and inputs.recent_wr < self.cfg.min_wr:
            # Scale reduction: the further below min_wr, the stronger
            wr_ratio = inputs.recent_wr / self.cfg.min_wr
            mult = max(0.5, wr_ratio)  # Never below 0.5 from WR alone
            score *= mult
            adjustments.append(f"low_wr({inputs.recent_wr:.2f}<{self.cfg.min_wr})")

        if inputs.recent_pf is not None and inputs.recent_pf < self.cfg.min_pf:
            if inputs.recent_pf < 1.0:
                # PF < 1.0 → losing edge, strong reduction
                score *= 0.60
                adjustments.append(f"losing_edge(pf={inputs.recent_pf:.2f})")
                mode = _worst_mode(mode, "defensive")
            else:
                # PF between 1.0 and min_pf → mild reduction
                score *= 0.85
                adjustments.append(f"weak_pf(pf={inputs.recent_pf:.2f})")
                mode = _worst_mode(mode, "reduced")

        if adjustments:
            reasons.extend(adjustments)
        return score, mode, reasons

    def _assess_regime(
        self,
        score: float,
        mode: str,
        reasons: List[str],
        regime: str,
        strategy_type: str,
    ):
        """Adjust based on regime + strategy compatibility."""
        regime_upper = regime.upper()

        # HIGH_VOL → never boost, apply mild reduction
        if regime_upper == "HIGH_VOL":
            score *= 0.85
            reasons.append("regime_high_vol")
            mode = _worst_mode(mode, "reduced")

        # LOW_VOL → reduce if configured
        elif regime_upper == "LOW_VOL" and self.cfg.reduce_in_low_vol:
            score *= 0.90
            reasons.append("regime_low_vol")

        # RANGE + breakout strategy → reduce if configured
        elif (
            regime_upper == "RANGE"
            and strategy_type.lower() in ("breakout", "bos_breakout")
            and self.cfg.reduce_in_range_for_breakout
        ):
            score *= 0.80
            reasons.append("regime_range_vs_breakout")
            mode = _worst_mode(mode, "reduced")

        # TREND + mean_reversion strategy → reduce if configured
        elif (
            regime_upper == "TREND"
            and strategy_type.lower() in ("mean_reversion", "mean_rev", "meanrev")
            and self.cfg.reduce_in_trend_for_meanrev
        ):
            score *= 0.80
            reasons.append("regime_trend_vs_meanrev")
            mode = _worst_mode(mode, "reduced")

        return score, mode, reasons

    def _assess_execution_quality(
        self,
        score: float,
        mode: str,
        reasons: List[str],
        inputs: RiskInputs,
    ):
        """Adjust based on recent execution performance."""
        adjustments = []

        # Kill switch events
        if inputs.recent_kills >= self.cfg.max_recent_kills:
            if self.cfg.block_on_execution_degrade:
                return (
                    0.0,
                    "blocked",
                    reasons + [f"exec_kills({inputs.recent_kills}>={self.cfg.max_recent_kills})"],
                )
            score *= 0.50
            adjustments.append(f"exec_kills({inputs.recent_kills})")
            mode = _worst_mode(mode, "defensive")

        # Slippage
        if inputs.avg_slippage_bps > self.cfg.max_recent_slippage_bps:
            score *= 0.75
            adjustments.append(
                f"exec_slippage({inputs.avg_slippage_bps:.0f}bps"
                f">{self.cfg.max_recent_slippage_bps:.0f})"
            )
            mode = _worst_mode(mode, "reduced")

        # Fill delay
        if inputs.avg_fill_delay_ms > self.cfg.max_recent_fill_delay_ms:
            score *= 0.80
            adjustments.append(
                f"exec_fill_delay({inputs.avg_fill_delay_ms:.0f}ms"
                f">{self.cfg.max_recent_fill_delay_ms:.0f})"
            )
            mode = _worst_mode(mode, "reduced")

        if adjustments:
            reasons.extend(adjustments)
        return score, mode, reasons

    # ─────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────

    def _passthrough(self, inputs: RiskInputs) -> RiskDecision:
        """Return neutral decision when engine is disabled."""
        return RiskDecision(
            risk_multiplier=1.0,
            effective_risk_pct=inputs.base_risk_pct,
            effective_leverage=inputs.base_leverage,
            size_multiplier=1.0,
            mode="normal",
            reasons=["adaptive_risk_disabled"],
            inputs={},
        )


# ═══════════════════════════════════════════════════════════════════════
# Helper: build RiskInputs from Governor + signal_dict
# ═══════════════════════════════════════════════════════════════════════

def build_risk_inputs(
    signal_dict: Dict[str, Any],
    base_risk_pct: float = 0.01,
    base_leverage: int = 3,
    governor=None,
) -> RiskInputs:
    """
    Build RiskInputs from available data sources.

    Pulls live metrics from Governor if available, otherwise uses
    signal_dict values. Safe fallback on any missing field.
    """
    # Volatility
    atr_info = signal_dict.get("atr_info", {})
    current_atr = atr_info.get("current_atr", signal_dict.get("atr", 0))
    avg_atr = atr_info.get("avg_atr", current_atr or 1)
    atr_ratio = current_atr / avg_atr if avg_atr > 0 else 1.0

    # Regime
    regime = signal_dict.get("regime", "NORMAL")
    strategy_type = signal_dict.get("setup", "")

    # Defaults for governor-sourced data
    daily_dd_pct = 0.0
    avg_slippage = 0.0
    avg_fill_delay = 0.0
    recent_kills = 0
    recent_wr = None
    recent_pf = None
    recent_count = 0

    if governor:
        try:
            gov = governor
            # Drawdown: compute from daily PnL
            with gov._lock:
                if gov._daily_pnl < 0 and hasattr(gov, '_account_equity'):
                    equity = getattr(gov, '_account_equity', 0)
                    if equity > 0:
                        daily_dd_pct = abs(gov._daily_pnl) / equity * 100

                # Slippage & fill delay averages
                slip_samples = list(gov._daily_slippage_samples)
                delay_samples = list(gov._daily_fill_delay_samples)
                if slip_samples:
                    avg_slippage = sum(slip_samples) / len(slip_samples)
                if delay_samples:
                    avg_fill_delay = sum(delay_samples) / len(delay_samples)

                # Recent kills (emergency close events)
                recent_kills = gov._daily_emergency_attempts

                # Winrate & PF from strategy data
                total_closed = gov._daily_wins + gov._daily_losses
                if total_closed >= 5:
                    recent_wr = gov._daily_wins / total_closed
                    recent_count = total_closed
                    # Compute PF from strategy breakdown
                    gross_win = sum(
                        v for v in gov._strategy_pnl.values() if v > 0
                    )
                    gross_loss = abs(
                        sum(v for v in gov._strategy_pnl.values() if v < 0)
                    )
                    recent_pf = (
                        gross_win / gross_loss if gross_loss > 0 else 2.0
                    )
        except Exception as e:
            logger.warning(f"[ADAPTIVE_RISK] Governor read error: {e}")

    return RiskInputs(
        atr_ratio=atr_ratio,
        daily_dd_pct=daily_dd_pct,
        rolling_dd_pct=daily_dd_pct,  # Same as daily for now
        recent_wr=recent_wr,
        recent_pf=recent_pf,
        recent_trades_count=recent_count,
        regime=regime,
        strategy_type=strategy_type,
        avg_slippage_bps=avg_slippage,
        avg_fill_delay_ms=avg_fill_delay,
        recent_kills=recent_kills,
        base_risk_pct=base_risk_pct,
        base_leverage=base_leverage,
    )


# ═══════════════════════════════════════════════════════════════════════
# Module-level singleton
# ═══════════════════════════════════════════════════════════════════════

adaptive_risk_engine = AdaptiveRiskEngine()
