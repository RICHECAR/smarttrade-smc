"""
Adaptive Policy — Final Go/No-Go Decision Layer
════════════════════════════════════════════════

Orchestrates RegimeDetection, SetupScoring, StrategyAdaptation,
and MetaAnalysis into a single ``evaluate()`` call that wraps
the existing Performance Layer pipeline.

The policy determines the system's stance:
  AGGRESSIVE  — boost size, accept weaker setups (edge confirmed)
  NORMAL      — standard parameters
  CAUTIOUS    — reduce size, raise thresholds (edge weakening)
  DEFENSIVE   — minimum size, only strong setups (drawdown or regime shift)
  BLOCK       — no new trades (kill switch territory)

Aggression is only increased when ALL conditions are met:
  1. Drawdown is controlled (< threshold)
  2. Specific setup is performing above baseline
  3. Market regime matches a historical edge

Config via .env:
  POLICY_ENABLED=true
  POLICY_AGGRESSIVE_MIN_WR=0.65
  POLICY_AGGRESSIVE_MAX_DD=1.5
  POLICY_AGGRESSIVE_MIN_PF=1.8
  POLICY_DEFENSIVE_DD_THRESHOLD=4.0
  POLICY_BLOCK_DD_THRESHOLD=6.0
"""

import logging
import os
from typing import Any, Dict, List, Optional

from backend.services.trading.adaptive_models import (
    AdaptationParams,
    DimensionStats,
    MarketRegime,
    PolicyAction,
    PolicyDecision,
    RegimeSnapshot,
    SetupScore,
)
from backend.services.trading.meta_analyzer import MetaPerformanceAnalyzer
from backend.services.trading.models import (
    PerformanceSnapshot,
    TradeSignal,
)
from backend.services.trading.regime_detection import RegimeDetectionEngine
from backend.services.trading.setup_scoring import SetupScoringEngine
from backend.services.trading.strategy_adaptation import StrategyAdaptationEngine

logger = logging.getLogger("smarttrade.adaptive.policy")


def _envb(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")

def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class AdaptivePolicy:
    """
    Top-level adaptive layer.  Wraps the existing performance-layer
    modules and adds intelligence.

    Usage in TradingLoop (after signal generation):
        policy_decision = adaptive_policy.evaluate(
            signal, performance, journal_entries
        )
        if policy_decision.action == PolicyAction.BLOCK:
            skip
        # Apply policy_decision.adaptation to sizing/execution
    """

    def __init__(
        self,
        regime_engine: Optional[RegimeDetectionEngine] = None,
        scorer: Optional[SetupScoringEngine] = None,
        adapter: Optional[StrategyAdaptationEngine] = None,
        meta_analyzer: Optional[MetaPerformanceAnalyzer] = None,
    ) -> None:
        self._enabled = _envb("POLICY_ENABLED", True)
        self._aggressive_min_wr = _envf("POLICY_AGGRESSIVE_MIN_WR", 0.65)
        self._aggressive_max_dd = _envf("POLICY_AGGRESSIVE_MAX_DD", 1.5)
        self._aggressive_min_pf = _envf("POLICY_AGGRESSIVE_MIN_PF", 1.8)
        self._defensive_dd = _envf("POLICY_DEFENSIVE_DD_THRESHOLD", 4.0)
        self._block_dd = _envf("POLICY_BLOCK_DD_THRESHOLD", 6.0)

        # Sub-engines (injectable or default-constructed)
        self.regime_engine = regime_engine or RegimeDetectionEngine()
        self.scorer = scorer or SetupScoringEngine()
        self.adapter = adapter or StrategyAdaptationEngine()
        self.meta = meta_analyzer or MetaPerformanceAnalyzer()

        # Cache last decision for monitoring
        self._last_decision: Optional[PolicyDecision] = None

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def evaluate(
        self,
        signal: TradeSignal,
        performance: PerformanceSnapshot,
        journal_entries: Optional[List] = None,
        symbol: Optional[str] = None,
    ) -> PolicyDecision:
        """
        Full adaptive evaluation of a proposed trade.

        Steps:
          1. Detect current market regime
          2. Score the setup
          3. Look up meta-performance for this regime + setup
          4. Compute adapted parameters
          5. Determine policy action (aggressive/normal/cautious/defensive/block)

        Returns PolicyDecision with action + adapted params.
        """
        if not self._enabled:
            return PolicyDecision(
                action=PolicyAction.NORMAL,
                reason="adaptive policy disabled",
            )

        sym = symbol or signal.symbol
        journal_entries = journal_entries or []

        # ── 1. Regime detection ──
        regime = self.regime_engine.detect(sym)

        # ── 2. Setup scoring ──
        setup_score = self.scorer.score(signal, regime, performance)

        # ── 3. Veto check from scoring ──
        if setup_score.veto:
            decision = PolicyDecision(
                action=PolicyAction.BLOCK,
                setup_score=setup_score,
                regime=regime,
                reason=f"setup veto: {setup_score.veto_reason}",
            )
            self._last_decision = decision
            logger.info("[POLICY] BLOCK (veto): %s", decision.reason)
            return decision

        # ── 4. Meta-analysis lookup ──
        regime_stats = self._find_regime_stats(journal_entries, regime)
        setup_stats = self._find_setup_stats(journal_entries, signal)

        # ── 5. Strategy adaptation ──
        adaptation = self.adapter.adapt(
            performance=performance,
            regime=regime,
            regime_stats=regime_stats,
            setup_stats=setup_stats,
        )

        # ── 6. Determine policy action ──
        action = self._determine_action(
            performance, regime, setup_score, regime_stats, setup_stats, adaptation
        )

        # ── 7. Final overrides per action ──
        adaptation = self._apply_action_overrides(action, adaptation)

        decision = PolicyDecision(
            action=action,
            adaptation=adaptation,
            setup_score=setup_score,
            regime=regime,
            reason=adaptation.reason,
        )
        self._last_decision = decision

        logger.info(
            "[POLICY] %s | score=%.3f regime=%s size_mult=%.2f conf_thresh=%.3f",
            action.value, setup_score.score, regime.regime.value,
            adaptation.size_multiplier, adaptation.confidence_threshold,
        )
        return decision

    @property
    def last_decision(self) -> Optional[PolicyDecision]:
        return self._last_decision

    def feed_candle(
        self, symbol: str, open_: float, high: float, low: float, close: float
    ) -> None:
        """Proxy to feed candles to the regime detector."""
        self.regime_engine.ingest(symbol, open_, high, low, close)

    def record_outcome(
        self,
        direction: str,
        strength: str,
        regime: str,
        pnl: float,
    ) -> None:
        """Proxy to record an outcome in the setup scorer."""
        self.scorer.record_outcome(direction, strength, regime, pnl)

    # ──────────────────────────────────────────────────
    #  STATE EXPORT
    # ──────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        """Return current adaptive state for monitoring."""
        return {
            "enabled": self._enabled,
            "last_decision": (
                self._last_decision.to_dict() if self._last_decision else None
            ),
        }

    # ──────────────────────────────────────────────────
    #  INTERNAL
    # ──────────────────────────────────────────────────

    def _determine_action(
        self,
        perf: PerformanceSnapshot,
        regime: RegimeSnapshot,
        score: SetupScore,
        regime_stats: Optional[DimensionStats],
        setup_stats: Optional[DimensionStats],
        adaptation: AdaptationParams,
    ) -> PolicyAction:
        """
        Core policy logic.
        Aggression requires ALL conditions met; defense triggers on ANY red flag.
        """
        dd = perf.current_drawdown_pct

        # ── BLOCK: deep drawdown ──
        if dd >= self._block_dd:
            return PolicyAction.BLOCK

        # ── DEFENSIVE: moderate drawdown or terrible performance ──
        if dd >= self._defensive_dd:
            return PolicyAction.DEFENSIVE
        if perf.total_trades >= 10 and perf.win_rate < 0.30:
            return PolicyAction.DEFENSIVE
        if perf.consecutive_losses >= 4:
            return PolicyAction.DEFENSIVE

        # ── AGGRESSIVE: requires ALL conditions ──
        can_be_aggressive = True
        if dd > self._aggressive_max_dd:
            can_be_aggressive = False
        if perf.total_trades < 10 or perf.win_rate < self._aggressive_min_wr:
            can_be_aggressive = False
        if perf.profit_factor < self._aggressive_min_pf:
            can_be_aggressive = False
        if score.score < 0.65:
            can_be_aggressive = False
        # Regime must show edge too
        if regime_stats and regime_stats.sample_size_ok:
            if regime_stats.win_rate < 0.55 or regime_stats.profit_factor < 1.5:
                can_be_aggressive = False
        else:
            can_be_aggressive = False  # Not enough data → stay cautious

        if can_be_aggressive:
            return PolicyAction.AGGRESSIVE

        # ── CAUTIOUS: some yellow flags ──
        cautious_flags = 0
        if dd > 2.0:
            cautious_flags += 1
        if perf.total_trades >= 10 and perf.win_rate < 0.45:
            cautious_flags += 1
        if perf.consecutive_losses >= 2:
            cautious_flags += 1
        if score.score < 0.45:
            cautious_flags += 1
        if regime.regime == MarketRegime.HIGH_VOLATILITY:
            cautious_flags += 1

        if cautious_flags >= 2:
            return PolicyAction.CAUTIOUS

        # ── NORMAL ──
        return PolicyAction.NORMAL

    def _apply_action_overrides(
        self, action: PolicyAction, params: AdaptationParams
    ) -> AdaptationParams:
        """Apply hard overrides based on final policy action."""
        if action == PolicyAction.AGGRESSIVE:
            params.size_multiplier = max(params.size_multiplier, 1.2)
            params.confidence_threshold = min(params.confidence_threshold, 0.35)
            params.max_concurrent_trades = max(params.max_concurrent_trades, 4)

        elif action == PolicyAction.CAUTIOUS:
            params.size_multiplier = min(params.size_multiplier, 0.8)
            params.confidence_threshold = max(params.confidence_threshold, 0.50)
            params.max_concurrent_trades = min(params.max_concurrent_trades, 2)

        elif action == PolicyAction.DEFENSIVE:
            params.size_multiplier = min(params.size_multiplier, 0.4)
            params.confidence_threshold = max(params.confidence_threshold, 0.65)
            params.max_concurrent_trades = 1
            params.min_risk_reward = max(params.min_risk_reward, 2.5)
            params.cooldown_multiplier = max(params.cooldown_multiplier, 2.5)

        elif action == PolicyAction.BLOCK:
            params.size_multiplier = 0.0
            params.max_concurrent_trades = 0

        return params

    def _find_regime_stats(
        self, entries: List, regime: RegimeSnapshot
    ) -> Optional[DimensionStats]:
        """Look up performance stats for the current regime."""
        if not entries or regime.regime == MarketRegime.UNKNOWN:
            return None

        by_regime = self.meta.analyze_by_regime(entries)
        for stats in by_regime:
            if stats.value == regime.regime.value:
                return stats
        return None

    def _find_setup_stats(
        self, entries: List, signal: TradeSignal
    ) -> Optional[DimensionStats]:
        """Look up performance stats for this setup type."""
        if not entries:
            return None

        setup_key = f"{signal.direction.value}_{signal.strength.value}"
        by_setup = self.meta.analyze_by_setup_type(entries)
        for stats in by_setup:
            if stats.value == setup_key:
                return stats
        return None
