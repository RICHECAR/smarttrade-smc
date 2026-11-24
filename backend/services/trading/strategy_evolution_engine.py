"""
Strategy Evolution Engine
═════════════════════════

Analyses which strategies / setups are improving or degrading over time
and automatically adjusts weights, thresholds, and activation conditions.

Pipeline:
  1. Ingest trade results per strategy
  2. Compute rolling performance vs baseline
  3. Classify health (thriving / stable / degrading / critical)
  4. Generate weight & threshold adaptations
  5. Emit EvolutionReport

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from typing import Dict, List

from backend.services.trading.self_evolution_models import (
    AdaptationAction,
    EvolutionDecision,
    EvolutionReport,
    StrategyHealth,
    StrategyPerformance,
)


class StrategyEvolutionEngine:
    """Monitors strategy performance and emits weight/threshold adjustments."""

    # ── Configuration ─────────────────────────────────────────────
    RECENT_WINDOW = 30          # trades for rolling window
    MIN_TRADES = 8              # minimum trades to evaluate
    IMPROVEMENT_THRESHOLD = 0.15  # +15 % win rate → "thriving"
    DEGRADATION_THRESHOLD = -0.15  # −15 % → "degrading"
    CRITICAL_THRESHOLD = -0.30  # −30 % → "critical"
    MAX_WEIGHT_STEP = 0.10      # max single-step weight change
    THRESHOLD_STEP = 0.05       # step for threshold adjustments
    COOLDOWN_TRADES = 10        # min trades between adjustments

    def __init__(self) -> None:
        # strategy_name → deque of (pnl_pct, win_bool, ts)
        self._history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
        # strategy_name → current weight modifier (starts at 1.0)
        self._weights: Dict[str, float] = {}
        # strategy_name → current threshold modifier (starts at 0.0)
        self._thresholds: Dict[str, float] = {}
        # strategy_name → trades since last adjustment
        self._trades_since_adjust: Dict[str, int] = defaultdict(int)
        self._last_report: EvolutionReport | None = None

    # ── Ingest ────────────────────────────────────────────────────
    def ingest_trade(
        self,
        strategy_name: str,
        pnl_pct: float,
        won: bool,
        setup_family: str = "",
    ) -> None:
        """Record a single trade outcome for a strategy."""
        self._history[strategy_name].append((pnl_pct, won, time.time()))
        self._trades_since_adjust[strategy_name] += 1
        if strategy_name not in self._weights:
            self._weights[strategy_name] = 1.0
            self._thresholds[strategy_name] = 0.0

    # ── Evaluate ──────────────────────────────────────────────────
    def evaluate(self) -> EvolutionReport:
        """Analyse all strategies and generate evolution decisions."""
        performances: List[StrategyPerformance] = []
        decisions: List[EvolutionDecision] = []

        for name, history in self._history.items():
            perf = self._assess_strategy(name, history)
            performances.append(perf)

            # Only adjust if cooldown is met
            if self._trades_since_adjust[name] >= self.COOLDOWN_TRADES:
                decs = self._generate_decisions(name, perf)
                decisions.extend(decs)
                if decs:
                    self._trades_since_adjust[name] = 0

        report = EvolutionReport(
            performances=performances,
            decisions=decisions,
            thriving_count=sum(1 for p in performances if p.health == StrategyHealth.THRIVING),
            degrading_count=sum(1 for p in performances
                                if p.health in (StrategyHealth.DEGRADING, StrategyHealth.CRITICAL)),
            total_adaptations=len(decisions),
        )
        self._last_report = report
        return report

    # ── Getters ───────────────────────────────────────────────────
    def get_last_report(self) -> EvolutionReport | None:
        return self._last_report

    def get_weight(self, strategy_name: str) -> float:
        return self._weights.get(strategy_name, 1.0)

    def get_threshold_modifier(self, strategy_name: str) -> float:
        return self._thresholds.get(strategy_name, 0.0)

    def get_all_weights(self) -> Dict[str, float]:
        return dict(self._weights)

    # ── Internal: Strategy Assessment ─────────────────────────────
    def _assess_strategy(self, name: str, history: deque) -> StrategyPerformance:
        all_records = list(history)
        n = len(all_records)

        if n < self.MIN_TRADES:
            return StrategyPerformance(
                strategy_name=name,
                health=StrategyHealth.DORMANT,
                trade_count_recent=n,
                confidence=n / self.MIN_TRADES,
            )

        # Split into recent vs baseline
        recent_n = min(self.RECENT_WINDOW, n)
        recent = all_records[-recent_n:]
        baseline = all_records[:-recent_n] if n > recent_n else all_records

        # Win rates
        wr_recent = sum(1 for _, w, _ in recent if w) / len(recent_ := recent)
        wr_base = sum(1 for _, w, _ in baseline if w) / max(1, len(baseline))

        # Average PnL
        pnl_recent = sum(p for p, _, _ in recent) / len(recent)
        pnl_base = sum(p for p, _, _ in baseline) / max(1, len(baseline))

        # Sharpe-like ratio (mean / std)
        pnls_recent = [p for p, _, _ in recent]
        sharpe_recent = self._sharpe(pnls_recent)
        pnls_base = [p for p, _, _ in baseline]
        sharpe_base = self._sharpe(pnls_base)

        # Trend direction: regression slope on recent PnLs normalised
        trend = self._trend_slope(pnls_recent)

        # Health classification
        wr_delta = wr_recent - wr_base
        pnl_delta_norm = (pnl_recent - pnl_base) / max(0.001, abs(pnl_base)) if pnl_base != 0 else 0.0
        composite = 0.5 * wr_delta + 0.5 * max(-1, min(1, pnl_delta_norm))

        if composite >= self.IMPROVEMENT_THRESHOLD:
            health = StrategyHealth.THRIVING
        elif composite <= self.CRITICAL_THRESHOLD:
            health = StrategyHealth.CRITICAL
        elif composite <= self.DEGRADATION_THRESHOLD:
            health = StrategyHealth.DEGRADING
        else:
            health = StrategyHealth.STABLE

        confidence = min(1.0, n / (self.RECENT_WINDOW * 2))

        return StrategyPerformance(
            strategy_name=name,
            health=health,
            win_rate_recent=wr_recent,
            win_rate_baseline=wr_base,
            avg_pnl_recent=pnl_recent,
            avg_pnl_baseline=pnl_base,
            sharpe_recent=sharpe_recent,
            sharpe_baseline=sharpe_base,
            trade_count_recent=len(recent),
            trend_direction=trend,
            confidence=confidence,
        )

    def _generate_decisions(self, name: str, perf: StrategyPerformance) -> List[EvolutionDecision]:
        decisions: List[EvolutionDecision] = []

        if perf.health == StrategyHealth.DORMANT:
            return decisions

        current_w = self._weights.get(name, 1.0)
        current_t = self._thresholds.get(name, 0.0)

        if perf.health == StrategyHealth.THRIVING:
            # Increase weight, loosen threshold
            new_w = min(2.0, current_w + self.MAX_WEIGHT_STEP * perf.confidence)
            new_t = max(-0.30, current_t - self.THRESHOLD_STEP * perf.confidence)
            if new_w != current_w:
                decisions.append(EvolutionDecision(
                    strategy_name=name,
                    action=AdaptationAction.INCREASE_WEIGHT,
                    parameter_name="weight",
                    old_value=current_w, new_value=new_w,
                    reason=f"thriving: wr={perf.win_rate_recent:.2f} trend={perf.trend_direction:.2f}",
                    confidence=perf.confidence,
                ))
                self._weights[name] = new_w
            if new_t != current_t:
                decisions.append(EvolutionDecision(
                    strategy_name=name,
                    action=AdaptationAction.LOOSEN_THRESHOLD,
                    parameter_name="threshold_modifier",
                    old_value=current_t, new_value=new_t,
                    reason="thriving: lowering entry barrier",
                    confidence=perf.confidence,
                ))
                self._thresholds[name] = new_t

        elif perf.health == StrategyHealth.DEGRADING:
            new_w = max(0.2, current_w - self.MAX_WEIGHT_STEP * perf.confidence)
            new_t = min(0.30, current_t + self.THRESHOLD_STEP * perf.confidence)
            if new_w != current_w:
                decisions.append(EvolutionDecision(
                    strategy_name=name,
                    action=AdaptationAction.DECREASE_WEIGHT,
                    parameter_name="weight",
                    old_value=current_w, new_value=new_w,
                    reason=f"degrading: wr={perf.win_rate_recent:.2f} trend={perf.trend_direction:.2f}",
                    confidence=perf.confidence,
                ))
                self._weights[name] = new_w
            if new_t != current_t:
                decisions.append(EvolutionDecision(
                    strategy_name=name,
                    action=AdaptationAction.TIGHTEN_THRESHOLD,
                    parameter_name="threshold_modifier",
                    old_value=current_t, new_value=new_t,
                    reason="degrading: raising entry barrier",
                    confidence=perf.confidence,
                ))
                self._thresholds[name] = new_t

        elif perf.health == StrategyHealth.CRITICAL:
            new_w = max(0.1, current_w * 0.5)
            decisions.append(EvolutionDecision(
                strategy_name=name,
                action=AdaptationAction.DISABLE_TEMPORARILY,
                parameter_name="weight",
                old_value=current_w, new_value=new_w,
                reason=f"critical: severe underperformance, halving weight",
                confidence=perf.confidence,
            ))
            self._weights[name] = new_w

        return decisions

    # ── Helpers ───────────────────────────────────────────────────
    @staticmethod
    def _sharpe(pnls: List[float]) -> float:
        if len(pnls) < 2:
            return 0.0
        mean = sum(pnls) / len(pnls)
        var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        std = math.sqrt(var) if var > 0 else 0.001
        return mean / std

    @staticmethod
    def _trend_slope(values: List[float]) -> float:
        """Simple linear regression slope normalised to -1..+1."""
        n = len(values)
        if n < 3:
            return 0.0
        x_mean = (n - 1) / 2
        y_mean = sum(values) / n
        num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
        den = sum((i - x_mean) ** 2 for i in range(n))
        if den == 0:
            return 0.0
        slope = num / den
        # Normalise by standard deviation of values
        std = math.sqrt(sum((v - y_mean) ** 2 for v in values) / n) if n > 0 else 1.0
        if std == 0:
            return 0.0
        return max(-1.0, min(1.0, slope / std))
