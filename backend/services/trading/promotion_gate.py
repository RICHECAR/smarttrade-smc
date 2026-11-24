"""
Promotion Gate — Statistical Barrier for Setup Advancement
═════════════════════════════════════════════════════════

Prevents setup promotion (EMERGING → ACTIVE) unless:
  1. Minimum trade count reached
  2. Robustness score above threshold
  3. Recent performance is NOT an outlier spike

Allows fast downgrade/block (asymmetric):
  - Promotion requires confidence + fragility + window checks
  - Demotion requires only 1 red flag

Robustness score = weighted(confidence, 1 - fragility, edge_trend, window_consistency)

Config via .env:
  GATE_MIN_TRADES=20
  GATE_ROBUSTNESS_THRESHOLD=0.55
  GATE_SPIKE_ZSCORE=2.5
  GATE_SPIKE_LOOKBACK=5
  GATE_FAST_DEMOTE_FRAGILITY=0.70
"""

import logging
import math
import os
from typing import Any, Dict, List, Optional

from backend.services.trading.robustness_models import (
    ConfidenceVerdict,
    EdgeTrend,
    FragilityReport,
    MultiWindowVerdict,
    PromotionDecision,
    PromotionVerdict,
)

logger = logging.getLogger("smarttrade.robustness.promotion_gate")


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class PromotionGate:
    """
    Gatekeeper for setup lifecycle promotions.

    Consumes outputs from:
      - StatisticalConfidenceEngine → ConfidenceVerdict
      - EdgeFragilityAnalyzer       → FragilityReport
      - MultiWindowValidator         → MultiWindowVerdict

    Usage:
        gate = PromotionGate()
        verdict = gate.evaluate(
            total_trades=25,
            recent_pnls=[50, 30, 20, 10, 5],
            confidence=confidence_verdict,
            fragility=fragility_report,
            windows=window_verdict,
        )
        # verdict.decision → APPROVED / DENIED_* / FAST_DOWNGRADE
    """

    def __init__(self) -> None:
        self._min_trades = _envi("GATE_MIN_TRADES", 20)
        self._robustness_thr = _envf("GATE_ROBUSTNESS_THRESHOLD", 0.55)
        self._spike_z = _envf("GATE_SPIKE_ZSCORE", 2.5)
        self._spike_lookback = _envi("GATE_SPIKE_LOOKBACK", 5)
        self._fast_demote_frag = _envf("GATE_FAST_DEMOTE_FRAGILITY", 0.70)

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def evaluate(
        self,
        total_trades: int,
        recent_pnls: List[float],
        confidence: ConfidenceVerdict,
        fragility: FragilityReport,
        windows: MultiWindowVerdict,
    ) -> PromotionVerdict:
        """
        Decide whether a setup should be promoted, held, or fast-downgraded.
        """
        # ── Fast downgrade checks (asymmetric — just one red flag) ──
        if fragility.fragility_score >= self._fast_demote_frag:
            return PromotionVerdict(
                decision=PromotionDecision.FAST_DOWNGRADE,
                robustness_score=0.0,
                required_trades=self._min_trades,
                actual_trades=total_trades,
                reason=f"fragility={fragility.fragility_score:.3f} >= {self._fast_demote_frag}",
            )

        if windows.edge_trend in (EdgeTrend.DECAYING, EdgeTrend.UNSTABLE):
            if confidence.confidence_score < 0.40:
                return PromotionVerdict(
                    decision=PromotionDecision.FAST_DOWNGRADE,
                    robustness_score=0.0,
                    required_trades=self._min_trades,
                    actual_trades=total_trades,
                    reason=f"trend={windows.edge_trend.value}, confidence={confidence.confidence_score:.3f}",
                )

        # ── Minimum trade count ──
        if total_trades < self._min_trades:
            return PromotionVerdict(
                decision=PromotionDecision.DENIED_SAMPLE,
                robustness_score=0.0,
                required_trades=self._min_trades,
                actual_trades=total_trades,
                reason=f"need {self._min_trades} trades, have {total_trades}",
            )

        # ── Spike detection ──
        spike_detected = self._detect_spike(recent_pnls)
        if spike_detected:
            return PromotionVerdict(
                decision=PromotionDecision.DENIED_SPIKE,
                robustness_score=0.0,
                required_trades=self._min_trades,
                actual_trades=total_trades,
                spike_detected=True,
                reason="recent performance is an outlier spike",
            )

        # ── Composite robustness score ──
        robustness = self._compute_robustness(confidence, fragility, windows)

        if robustness < self._robustness_thr:
            return PromotionVerdict(
                decision=PromotionDecision.DENIED_ROBUSTNESS,
                robustness_score=robustness,
                required_trades=self._min_trades,
                actual_trades=total_trades,
                reason=f"robustness={robustness:.3f} < {self._robustness_thr}",
            )

        return PromotionVerdict(
            decision=PromotionDecision.APPROVED,
            robustness_score=robustness,
            required_trades=self._min_trades,
            actual_trades=total_trades,
            reason=f"robustness={robustness:.3f} — all checks passed",
        )

    def should_promote(
        self,
        total_trades: int,
        recent_pnls: List[float],
        confidence: ConfidenceVerdict,
        fragility: FragilityReport,
        windows: MultiWindowVerdict,
    ) -> bool:
        """Convenience: returns True only if promotion is APPROVED."""
        v = self.evaluate(total_trades, recent_pnls, confidence, fragility, windows)
        return v.decision == PromotionDecision.APPROVED

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    def _compute_robustness(
        self,
        confidence: ConfidenceVerdict,
        fragility: FragilityReport,
        windows: MultiWindowVerdict,
    ) -> float:
        """
        Weighted composite:
          confidence_score       × 0.35
          (1 - fragility_score)  × 0.30
          edge_trend_score       × 0.15
          window_consistency     × 0.20
        """
        trend_val = {
            EdgeTrend.PERSISTENT: 1.0,
            EdgeTrend.REGIME_DEPENDENT: 0.5,
            EdgeTrend.DECAYING: 0.2,
            EdgeTrend.UNSTABLE: 0.1,
            EdgeTrend.INSUFFICIENT: 0.3,
        }.get(windows.edge_trend, 0.3)

        score = (
            0.35 * confidence.confidence_score
            + 0.30 * (1.0 - fragility.fragility_score)
            + 0.15 * trend_val
            + 0.20 * windows.consistency
        )
        return round(max(0.0, min(1.0, score)), 4)

    def _detect_spike(self, recent_pnls: List[float]) -> bool:
        """
        Check if the most recent trades are an outlier spike
        relative to the rest of the history.
        """
        if len(recent_pnls) < self._spike_lookback + 5:
            return False  # not enough data

        lookback = recent_pnls[-self._spike_lookback:]
        history = recent_pnls[: -self._spike_lookback]

        if len(history) < 5:
            return False

        hist_mean = sum(history) / len(history)
        hist_var = sum((p - hist_mean) ** 2 for p in history) / len(history)
        hist_std = math.sqrt(hist_var) if hist_var > 0 else 1e-9

        recent_mean = sum(lookback) / len(lookback)

        z = (recent_mean - hist_mean) / hist_std if hist_std > 1e-9 else 0.0

        if z > self._spike_z:
            logger.info(
                "[GATE] Spike detected: recent_mean=%.2f, hist_mean=%.2f, z=%.2f",
                recent_mean, hist_mean, z,
            )
            return True
        return False
