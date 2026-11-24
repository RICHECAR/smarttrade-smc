"""
StrategyScorer — Multi-Alpha Signal Ranking
════════════════════════════════════════════
Scores and ranks competing signals from multiple strategies so the
orchestrator always executes the highest-quality opportunity.

Score formula:
  score = (
      w_confidence * confidence
    + w_rr         * normalized_rr
    + w_regime     * regime_alignment
    + w_weight     * strategy_weight
  )

When multiple strategies fire simultaneously, the scorer:
  1. Normalizes all signals to comparable scale
  2. Applies strategy-specific weights (from StrategyWeightTracker)
  3. Detects directional conflicts (long vs short) → can veto
  4. Returns ranked list, best signal first
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── Regime alignment matrix ─────────────────────────────────────
# (strategy_name, regime) → alignment score [0..1]
_DEFAULT_REGIME_ALIGNMENT: Dict[Tuple[str, str], float] = {
    # DefaultSMC → best in TREND
    ("DefaultSMC", "TREND"): 1.0,
    ("DefaultSMC", "RANGE"): 0.4,
    ("DefaultSMC", "HIGH_VOL"): 0.2,
    ("DefaultSMC", "LOW_VOL"): 0.5,
    # AsiaWeekend → best in RANGE / LOW_VOL
    ("AsiaWeekend", "TREND"): 0.2,
    ("AsiaWeekend", "RANGE"): 1.0,
    ("AsiaWeekend", "HIGH_VOL"): 0.1,
    ("AsiaWeekend", "LOW_VOL"): 0.9,
    # FundingReversion → regime-independent (sentiment driven)
    ("FundingReversion", "TREND"): 0.7,
    ("FundingReversion", "RANGE"): 0.8,
    ("FundingReversion", "HIGH_VOL"): 0.5,
    ("FundingReversion", "LOW_VOL"): 0.7,
    # VWAPReversion → best in RANGE
    ("VWAPReversion", "TREND"): 0.2,
    ("VWAPReversion", "RANGE"): 1.0,
    ("VWAPReversion", "HIGH_VOL"): 0.3,
    ("VWAPReversion", "LOW_VOL"): 0.8,
    # SweepScalp → good in most except HIGH_VOL
    ("SweepScalp", "TREND"): 0.6,
    ("SweepScalp", "RANGE"): 0.8,
    ("SweepScalp", "HIGH_VOL"): 0.2,
    ("SweepScalp", "LOW_VOL"): 0.7,
    # LiqCascade → best in HIGH_VOL (event-driven)
    ("LiqCascade", "TREND"): 0.7,
    ("LiqCascade", "RANGE"): 0.4,
    ("LiqCascade", "HIGH_VOL"): 1.0,
    ("LiqCascade", "LOW_VOL"): 0.3,
    # OBImbalance → good everywhere except HIGH_VOL
    ("OBImbalance", "TREND"): 0.8,
    ("OBImbalance", "RANGE"): 0.7,
    ("OBImbalance", "HIGH_VOL"): 0.3,
    ("OBImbalance", "LOW_VOL"): 0.6,
}


@dataclass
class ScoredSignal:
    """A signal with its composite score for ranking."""
    strategy_name: str
    signal_dict: Dict
    score: float
    components: Dict[str, float] = field(default_factory=dict)

    @property
    def direction(self) -> str:
        return self.signal_dict.get("direction", "")

    @property
    def confidence(self) -> float:
        return float(self.signal_dict.get("confidence", 0))

    @property
    def rr_ratio(self) -> float:
        return float(self.signal_dict.get("rr_ratio", 0))


class StrategyScorer:
    """
    Scores, ranks, and resolves competing strategy signals.

    Usage:
        scorer = StrategyScorer()
        scored = scorer.score_signals(candidates, regime="RANGE", weights=tracker.get_weights())
        best = scorer.select_best(scored)
    """

    def __init__(
        self,
        w_confidence: float = 0.40,
        w_rr: float = 0.25,
        w_regime: float = 0.20,
        w_weight: float = 0.15,
        rr_cap: float = 4.0,
        conflict_policy: str = "best_score",
        min_score: float = 0.25,
    ):
        """
        Args:
            w_confidence: Weight for signal confidence component
            w_rr: Weight for risk-reward ratio component
            w_regime: Weight for regime alignment component
            w_weight: Weight for strategy adaptive weight component
            rr_cap: Cap R:R normalization (values above this → 1.0)
            conflict_policy: "best_score" | "no_trade" | "higher_confidence"
            min_score: Minimum composite score to accept a signal
        """
        total = w_confidence + w_rr + w_regime + w_weight
        self.w_confidence = w_confidence / total
        self.w_rr = w_rr / total
        self.w_regime = w_regime / total
        self.w_weight = w_weight / total
        self.rr_cap = rr_cap
        self.conflict_policy = conflict_policy
        self.min_score = min_score
        self._regime_alignment = dict(_DEFAULT_REGIME_ALIGNMENT)

    def score_signals(
        self,
        candidates: List[Dict],
        regime: str = "RANGE",
        strategy_weights: Optional[Dict[str, float]] = None,
    ) -> List[ScoredSignal]:
        """
        Score a list of candidate signal_dicts.

        Args:
            candidates: List of signal_dict (must have 'strategy', 'confidence', 'rr_ratio', 'direction')
            regime: Current MarketRegime value
            strategy_weights: {strategy_name: adaptive_weight} from WeightTracker

        Returns:
            List of ScoredSignal sorted by score descending
        """
        weights = strategy_weights or {}
        scored: List[ScoredSignal] = []

        for sig in candidates:
            strategy_name = sig.get("strategy", "Unknown")
            confidence = float(sig.get("confidence", 0))
            rr = float(sig.get("rr_ratio", 0))
            norm_rr = min(rr / self.rr_cap, 1.0) if self.rr_cap > 0 else 0

            # Regime alignment lookup with default
            regime_key = (strategy_name, regime)
            regime_align = self._regime_alignment.get(regime_key, 0.5)

            # Strategy adaptive weight (default 1.0 if not tracked)
            strat_weight = min(weights.get(strategy_name, 1.0) / 2.0, 1.0)

            # Composite score
            score = (
                self.w_confidence * confidence
                + self.w_rr * norm_rr
                + self.w_regime * regime_align
                + self.w_weight * strat_weight
            )

            components = {
                "confidence": round(confidence, 4),
                "norm_rr": round(norm_rr, 4),
                "regime_align": round(regime_align, 4),
                "strat_weight": round(strat_weight, 4),
                "raw_score": round(score, 4),
            }

            scored.append(ScoredSignal(
                strategy_name=strategy_name,
                signal_dict=sig,
                score=round(score, 4),
                components=components,
            ))

        scored.sort(key=lambda s: s.score, reverse=True)

        if scored:
            logger.info(
                f"[SCORER] Ranked {len(scored)} signals"
                f" | best={scored[0].strategy_name}({scored[0].score:.3f})"
                f" | regime={regime}"
            )

        return scored

    def select_best(
        self,
        scored_signals: List[ScoredSignal],
    ) -> Optional[ScoredSignal]:
        """
        Select the best signal, handling directional conflicts and min score.

        Returns:
            Best ScoredSignal, or None if no signal qualifies
        """
        if not scored_signals:
            return None

        # Filter by minimum score
        eligible = [s for s in scored_signals if s.score >= self.min_score]
        if not eligible:
            logger.info(
                f"[SCORER] All signals below min_score={self.min_score}"
                f" | best_was={scored_signals[0].score:.3f}"
            )
            return None

        # Check for directional conflict
        directions = {s.direction for s in eligible}
        if len(directions) > 1 and "long" in directions and "short" in directions:
            return self._resolve_conflict(eligible)

        return eligible[0]

    def _resolve_conflict(
        self,
        signals: List[ScoredSignal],
    ) -> Optional[ScoredSignal]:
        """Resolve long vs short conflict among top signals."""
        longs = [s for s in signals if s.direction == "long"]
        shorts = [s for s in signals if s.direction == "short"]

        best_long = longs[0] if longs else None
        best_short = shorts[0] if shorts else None

        if self.conflict_policy == "no_trade":
            logger.warning(
                f"[SCORER] CONFLICT VETO | long={best_long.strategy_name if best_long else 'n/a'}"
                f" vs short={best_short.strategy_name if best_short else 'n/a'}"
            )
            return None

        if self.conflict_policy == "higher_confidence":
            lc = best_long.confidence if best_long else 0
            sc = best_short.confidence if best_short else 0
            winner = best_long if lc >= sc else best_short
        else:
            # "best_score" — default
            winner = signals[0]

        logger.info(
            f"[SCORER] CONFLICT RESOLVED | policy={self.conflict_policy}"
            f" | winner={winner.strategy_name}({winner.direction})"
            f" | score={winner.score:.3f}"
        )
        return winner

    def detect_correlated_signals(
        self,
        scored_signals: List[ScoredSignal],
    ) -> List[ScoredSignal]:
        """
        Merge correlated signals (same direction, similar entry) into one.
        Returns deduplicated list, keeping highest-scored per direction.
        """
        if len(scored_signals) <= 1:
            return scored_signals

        seen_directions: Dict[str, ScoredSignal] = {}
        result: List[ScoredSignal] = []

        for sig in scored_signals:
            d = sig.direction
            if d not in seen_directions:
                seen_directions[d] = sig
                result.append(sig)
            else:
                existing = seen_directions[d]
                # Log correlation detection
                logger.info(
                    f"[SCORER] CORRELATED | {sig.strategy_name}({sig.score:.3f})"
                    f" merged into {existing.strategy_name}({existing.score:.3f})"
                    f" | same dir={d}"
                )
                # Boost confidence of winner slightly
                existing.signal_dict["confidence"] = min(
                    existing.confidence + 0.05, 1.0
                )

        return result
