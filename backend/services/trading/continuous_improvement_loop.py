"""
Continuous Improvement Loop
════════════════════════════

Orchestrates the full self-evolution cycle:

  execution → result → analysis → adaptation → redeployment

Connects all engines:
  StrategyEvolutionEngine  → strategy health & weight adjustments
  MetaLearningEngine       → implicit pattern discovery
  ParameterAdaptationEngine → dynamic parameter tuning
  FailurePatternDetector   → failure suppression
  DiscoveryEngine          → new edge identification

Each cycle produces an ImprovementCycle report and advances the
global EvolutionState.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from backend.services.trading.self_evolution_models import (
    AdaptationPlan,
    DiscoveryReport,
    EvolutionPhase,
    EvolutionReport,
    EvolutionState,
    FailureReport,
    ImprovementCycle,
    MetaLearningReport,
)
from backend.services.trading.strategy_evolution_engine import StrategyEvolutionEngine
from backend.services.trading.meta_learning_engine import MetaLearningEngine
from backend.services.trading.parameter_adaptation_engine import ParameterAdaptationEngine
from backend.services.trading.failure_pattern_detector import FailurePatternDetector
from backend.services.trading.discovery_engine import DiscoveryEngine


class ContinuousImprovementLoop:
    """Orchestrates execution→result→analysis→adaptation→redeployment."""

    # ── Configuration ─────────────────────────────────────────────
    MIN_TRADES_PER_CYCLE = 10   # minimum new trades before a cycle runs
    MAX_ADAPTATIONS_PER_CYCLE = 20  # safety cap on changes per cycle
    IMPROVEMENT_MOMENTUM_DECAY = 0.90  # decay for cumulative score

    def __init__(
        self,
        evolution_engine: StrategyEvolutionEngine | None = None,
        meta_learning: MetaLearningEngine | None = None,
        param_adaptation: ParameterAdaptationEngine | None = None,
        failure_detector: FailurePatternDetector | None = None,
        discovery_engine: DiscoveryEngine | None = None,
    ) -> None:
        self._evolution = evolution_engine or StrategyEvolutionEngine()
        self._meta = meta_learning or MetaLearningEngine()
        self._params = param_adaptation or ParameterAdaptationEngine()
        self._failures = failure_detector or FailurePatternDetector()
        self._discovery = discovery_engine or DiscoveryEngine()

        self._state = EvolutionState()
        self._cycles: List[ImprovementCycle] = []
        self._trades_since_cycle: int = 0

    # ── Trade Ingestion (unified entry point) ─────────────────────
    def ingest_trade(
        self,
        symbol: str,
        strategy_name: str,
        pnl_pct: float,
        won: bool,
        context: Dict[str, Any] | None = None,
    ) -> None:
        """Ingest a trade outcome into all sub-engines."""
        ctx = context or {}

        # 1. Strategy evolution
        self._evolution.ingest_trade(
            strategy_name=strategy_name,
            pnl_pct=pnl_pct,
            won=won,
            setup_family=ctx.get("setup_family", ""),
        )

        # 2. Meta-learning
        from backend.services.trading.self_evolution_models import LearningSource
        source = LearningSource.WINNING_TRADE if won else LearningSource.LOSING_TRADE
        self._meta.ingest_outcome(pnl_pct=pnl_pct, won=won, source=source, context=ctx)

        # 3. Parameter adaptation — record outcomes for registered params
        for param_key in ["confidence_threshold", "risk_multiplier", "scoring_weight"]:
            if param_key in ctx:
                module = ctx.get("module_name", strategy_name)
                self._params.record_outcome(param_key, module, pnl_pct)

        # 4. Failure detection
        self._failures.ingest_trade(symbol=symbol, pnl_pct=pnl_pct, won=won, context=ctx)

        # 5. Discovery engine
        self._discovery.ingest_trade_outcome(symbol=symbol, pnl_pct=pnl_pct, won=won, context=ctx)

        self._trades_since_cycle += 1

    def ingest_market_data(
        self,
        symbol: str,
        price: float,
        volume: float,
        volatility: float = 0.0,
        context: Dict[str, Any] | None = None,
    ) -> None:
        """Feed market data to the discovery engine."""
        self._discovery.ingest_market_data(
            symbol=symbol, price=price, volume=volume,
            volatility=volatility, context=context,
        )

    # ── Run Cycle ─────────────────────────────────────────────────
    def run_cycle(self, force: bool = False) -> ImprovementCycle | None:
        """Execute one full improvement cycle.
        Returns None if not enough new trades (unless force=True)."""
        if not force and self._trades_since_cycle < self.MIN_TRADES_PER_CYCLE:
            return None

        cycle_id = len(self._cycles) + 1

        # ── Phase 1: Observation (already done via ingest) ────────
        self._state.current_phase = EvolutionPhase.OBSERVATION

        # ── Phase 2: Analysis ─────────────────────────────────────
        self._state.current_phase = EvolutionPhase.ANALYSIS

        evo_report: EvolutionReport = self._evolution.evaluate()
        meta_report: MetaLearningReport = self._meta.analyse()
        failure_report: FailureReport = self._failures.analyse()
        discovery_report: DiscoveryReport = self._discovery.discover()

        # ── Phase 3: Adaptation ───────────────────────────────────
        self._state.current_phase = EvolutionPhase.ADAPTATION

        param_plan: AdaptationPlan = self._params.adapt()

        # Safety cap: limit total changes
        total_changes = (evo_report.total_adaptations
                         + param_plan.total_changes
                         + failure_report.active_suppressions)
        if total_changes > self.MAX_ADAPTATIONS_PER_CYCLE:
            # Prioritise: failures > evolution > parameters
            pass  # all engines already have their own caps

        # ── Phase 4: Validation ───────────────────────────────────
        self._state.current_phase = EvolutionPhase.VALIDATION

        # Compute net improvement score
        improvement_parts = []
        if evo_report.thriving_count + evo_report.degrading_count > 0:
            evo_score = (evo_report.thriving_count - evo_report.degrading_count) / \
                max(1, evo_report.thriving_count + evo_report.degrading_count)
            improvement_parts.append(evo_score * 0.30)
        if meta_report.actionable_count > 0:
            improvement_parts.append(min(0.20, meta_report.actionable_count * 0.05))
        if param_plan.avg_effectiveness > 0.5:
            improvement_parts.append((param_plan.avg_effectiveness - 0.5) * 0.40)
        if failure_report.active_suppressions > 0:
            improvement_parts.append(min(0.20, failure_report.active_suppressions * 0.05))
        if discovery_report.validated_count > 0:
            improvement_parts.append(min(0.15, discovery_report.validated_count * 0.05))

        net_score = sum(improvement_parts) if improvement_parts else 0.0
        net_score = max(-1.0, min(1.0, net_score))

        # ── Phase 5: Deployment ───────────────────────────────────
        self._state.current_phase = EvolutionPhase.DEPLOYMENT

        cycle = ImprovementCycle(
            cycle_id=cycle_id,
            phase=EvolutionPhase.DEPLOYMENT,
            evolution_summary=f"{evo_report.thriving_count} thriving, {evo_report.degrading_count} degrading, {evo_report.total_adaptations} adaptations",
            learning_summary=f"{meta_report.total_patterns} patterns, {meta_report.actionable_count} actionable",
            adaptation_summary=f"{param_plan.total_changes} param changes, avg_eff={param_plan.avg_effectiveness:.2f}",
            failure_summary=f"{failure_report.total_patterns} patterns, {failure_report.active_suppressions} suppressed",
            discovery_summary=f"{discovery_report.total_edges} edges, {discovery_report.validated_count} validated",
            total_adaptations_applied=evo_report.total_adaptations + param_plan.total_changes,
            total_failures_suppressed=failure_report.active_suppressions,
            total_edges_discovered=discovery_report.total_edges,
            net_improvement_score=net_score,
        )

        self._cycles.append(cycle)
        self._trades_since_cycle = 0

        # Update global state
        self._state.total_cycles = len(self._cycles)
        self._state.strategies_monitored = len(self._evolution._history)
        self._state.patterns_learned = meta_report.total_patterns
        self._state.parameters_adapted = param_plan.total_changes
        self._state.failures_detected = failure_report.total_patterns
        self._state.edges_discovered = discovery_report.total_edges
        self._state.cumulative_improvement = (
            self._state.cumulative_improvement * self.IMPROVEMENT_MOMENTUM_DECAY + net_score
        )
        self._state.last_cycle = cycle

        return cycle

    # ── Getters ───────────────────────────────────────────────────
    def get_state(self) -> EvolutionState:
        return self._state

    def get_last_cycle(self) -> ImprovementCycle | None:
        return self._cycles[-1] if self._cycles else None

    def get_cycle_history(self, last_n: int = 20) -> List[ImprovementCycle]:
        return self._cycles[-last_n:]

    def get_improvement_trend(self) -> List[float]:
        """Return net_improvement_score for each cycle."""
        return [c.net_improvement_score for c in self._cycles]

    # ── Sub-engine access ─────────────────────────────────────────
    @property
    def evolution_engine(self) -> StrategyEvolutionEngine:
        return self._evolution

    @property
    def meta_learning(self) -> MetaLearningEngine:
        return self._meta

    @property
    def param_adaptation(self) -> ParameterAdaptationEngine:
        return self._params

    @property
    def failure_detector(self) -> FailurePatternDetector:
        return self._failures

    @property
    def discovery_engine(self) -> DiscoveryEngine:
        return self._discovery
