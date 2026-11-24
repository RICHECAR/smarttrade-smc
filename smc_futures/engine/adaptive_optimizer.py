"""
Idle Backtest & Adaptive Optimization Engine
═════════════════════════════════════════════

Architecture en 3 couches:

  Couche A — Backtest continu (quand le moteur est idle)
    → Lance des backtests rolling sur les données récentes
    → Par symbole, session, régime

  Couche B — Comparaison live vs backtest
    → Win rate, PF, slippage, accept rate, drift de performance
    → Détecte quand une stratégie dérive

  Couche C — Adaptation bornée et sûre
    → Ajuste UNIQUEMENT : poids alpha, capital fraction, risk multiplier,
      priorité de stratégie, seuils secondaires bornés
    → Ne touche JAMAIS : logique centrale, SL/TP rules, contraintes de risque dur

SAFETY RULES:
  - Pas d'auto-réécriture de la logique de stratégie
  - Pas de modification des SL/TP core
  - Pas de modification des contraintes de risque dur
  - Chaque ajustement est borné dans une plage configurable
  - Log complet de chaque décision d'adaptation

Author: SMC Trading Bot Framework
"""

import logging
import time
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict

logger = logging.getLogger("adaptive_optimizer")


# ═══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class StrategyPerformance:
    """Performance snapshot for a strategy in a given context."""
    strategy: str
    symbol: str = "ALL"
    regime: str = "ALL"
    session: str = "ALL"

    # Metrics
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_rr: float = 0.0
    max_dd: float = 0.0
    avg_slippage_bps: float = 0.0

    # Timestamps
    period_start: Optional[str] = None
    period_end: Optional[str] = None


@dataclass
class DriftReport:
    """Comparison between live and backtest performance."""
    strategy: str
    symbol: str = "ALL"

    live: Optional[StrategyPerformance] = None
    backtest: Optional[StrategyPerformance] = None

    # Drift metrics (-1 to +1, negative = live underperforming)
    wr_drift: float = 0.0    # live_wr - backtest_wr
    pf_drift: float = 0.0    # live_pf - backtest_pf
    pnl_drift: float = 0.0
    slippage_drift: float = 0.0

    # Overall drift score (0 = identical, >0.3 = concerning)
    drift_score: float = 0.0
    action_needed: bool = False

    def compute_drift(self):
        """Compute drift metrics from live vs backtest."""
        if not self.live or not self.backtest:
            return

        # Win rate drift
        if self.backtest.win_rate > 0:
            self.wr_drift = self.live.win_rate - self.backtest.win_rate

        # Profit factor drift
        if self.backtest.profit_factor > 0:
            self.pf_drift = (self.live.profit_factor - self.backtest.profit_factor) / max(self.backtest.profit_factor, 0.01)

        # PnL drift (normalized)
        if self.backtest.trades > 0 and self.live.trades > 0:
            bt_avg = self.backtest.pnl / self.backtest.trades
            live_avg = self.live.pnl / self.live.trades
            if abs(bt_avg) > 0:
                self.pnl_drift = (live_avg - bt_avg) / abs(bt_avg)

        # Slippage drift
        self.slippage_drift = self.live.avg_slippage_bps - self.backtest.avg_slippage_bps

        # Composite drift score
        self.drift_score = (
            abs(self.wr_drift) * 0.3 +
            abs(self.pf_drift) * 0.3 +
            abs(self.pnl_drift) * 0.2 +
            min(abs(self.slippage_drift) / 20, 1.0) * 0.2
        )
        self.action_needed = self.drift_score > 0.3


# ═══════════════════════════════════════════════════════════════════════════════
# BOUNDED PARAMETER ADJUSTMENTS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ParameterBounds:
    """Safe bounds for adjustable parameters."""
    alpha_weight: Tuple[float, float] = (0.1, 2.0)       # Strategy weight multiplier
    capital_fraction: Tuple[float, float] = (0.3, 1.0)    # Fraction of normal sizing
    risk_multiplier: Tuple[float, float] = (0.5, 1.5)     # Risk per trade factor
    priority: Tuple[float, float] = (0, 10)                # Strategy priority score
    min_rr_adj: Tuple[float, float] = (1.0, 4.0)          # Min R:R threshold
    min_confidence_adj: Tuple[float, float] = (0.3, 0.9)  # Min signal confidence


@dataclass
class Adjustment:
    """A single bounded parameter adjustment."""
    parameter: str
    old_value: float
    new_value: float
    reason: str
    strategy: str
    symbol: str = "ALL"
    timestamp: str = ""
    drift_score: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# COUCHE B — PERFORMANCE COMPARATOR
# ═══════════════════════════════════════════════════════════════════════════════

class PerformanceComparator:
    """
    Compare live performance vs backtest performance.
    Maintains rolling windows of both.
    """

    def __init__(self, lookback_days: int = 30):
        self.lookback_days = lookback_days
        self._live_trades: Dict[str, List[Dict]] = defaultdict(list)
        self._backtest_results: Dict[str, StrategyPerformance] = {}

    def record_live_trade(self, strategy: str, trade: Dict[str, Any]):
        """Record a closed live trade for comparison."""
        self._live_trades[strategy].append({
            "pnl": trade.get("pnl", 0),
            "rr": trade.get("rr", 0),
            "symbol": trade.get("symbol", ""),
            "slippage_bps": trade.get("slippage_bps", 0),
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        # Keep only recent trades
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).isoformat()
        self._live_trades[strategy] = [
            t for t in self._live_trades[strategy] if t["ts"] >= cutoff
        ]

    def set_backtest_result(self, strategy: str, perf: StrategyPerformance):
        """Update backtest performance for a strategy."""
        self._backtest_results[strategy] = perf

    def get_live_performance(self, strategy: str) -> StrategyPerformance:
        """Compute live performance from recent trades."""
        trades = self._live_trades.get(strategy, [])
        perf = StrategyPerformance(strategy=strategy)

        if not trades:
            return perf

        perf.trades = len(trades)
        perf.wins = sum(1 for t in trades if t["pnl"] > 0)
        perf.losses = sum(1 for t in trades if t["pnl"] < 0)
        perf.pnl = sum(t["pnl"] for t in trades)
        perf.win_rate = perf.wins / perf.trades if perf.trades > 0 else 0
        gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        perf.profit_factor = gross_profit / gross_loss if gross_loss > 0 else gross_profit
        rr_vals = [t["rr"] for t in trades if t.get("rr")]
        perf.avg_rr = sum(rr_vals) / len(rr_vals) if rr_vals else 0
        slip_vals = [t["slippage_bps"] for t in trades if t.get("slippage_bps")]
        perf.avg_slippage_bps = sum(slip_vals) / len(slip_vals) if slip_vals else 0

        return perf

    def compute_drift(self, strategy: str) -> DriftReport:
        """Compute drift between live and backtest."""
        report = DriftReport(strategy=strategy)
        report.live = self.get_live_performance(strategy)
        report.backtest = self._backtest_results.get(strategy)
        report.compute_drift()
        return report

    def get_all_drifts(self) -> List[DriftReport]:
        """Compute drift for all tracked strategies."""
        strategies = set(self._live_trades.keys()) | set(self._backtest_results.keys())
        return [self.compute_drift(s) for s in strategies]


# ═══════════════════════════════════════════════════════════════════════════════
# COUCHE C — BOUNDED ADAPTIVE OPTIMIZER
# ═══════════════════════════════════════════════════════════════════════════════

class BoundedAdaptiveOptimizer:
    """
    Safely adjusts strategy parameters based on drift reports.
    
    SAFETY GUARANTEES:
      - All adjustments are bounded within ParameterBounds
      - Step size is capped (max 10% change per cycle)
      - Adjustments are logged for audit
      - No modification of core logic, SL/TP rules, or hard risk limits
    """

    MAX_STEP_PCT = 0.10  # Max 10% change per adjustment cycle

    def __init__(self, bounds: Optional[ParameterBounds] = None):
        self.bounds = bounds or ParameterBounds()
        self.current_params: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {
                "alpha_weight": 1.0,
                "capital_fraction": 1.0,
                "risk_multiplier": 1.0,
                "priority": 5.0,
            }
        )
        self.adjustment_history: List[Adjustment] = []
        self._cycle_count = 0

    def suggest_adjustments(self, drift: DriftReport) -> List[Adjustment]:
        """
        Compute bounded adjustments based on drift.
        Returns list of proposed adjustments.
        """
        adjustments = []

        if not drift.action_needed:
            return adjustments

        strategy = drift.strategy
        params = self.current_params[strategy]

        # Underperforming → reduce exposure
        if drift.wr_drift < -0.1 or drift.pf_drift < -0.2:
            # Reduce alpha weight
            adj = self._compute_bounded_step(
                params["alpha_weight"],
                direction=-1,
                step_factor=abs(drift.drift_score),
                bounds=self.bounds.alpha_weight,
            )
            if adj != params["alpha_weight"]:
                adjustments.append(Adjustment(
                    parameter="alpha_weight",
                    old_value=params["alpha_weight"],
                    new_value=adj,
                    reason=f"WR drift={drift.wr_drift:.2f}, PF drift={drift.pf_drift:.2f}",
                    strategy=strategy,
                    drift_score=drift.drift_score,
                ))

            # Reduce capital fraction
            adj = self._compute_bounded_step(
                params["capital_fraction"],
                direction=-1,
                step_factor=abs(drift.drift_score) * 0.5,
                bounds=self.bounds.capital_fraction,
            )
            if adj != params["capital_fraction"]:
                adjustments.append(Adjustment(
                    parameter="capital_fraction",
                    old_value=params["capital_fraction"],
                    new_value=adj,
                    reason=f"Reducing capital due to drift={drift.drift_score:.2f}",
                    strategy=strategy,
                    drift_score=drift.drift_score,
                ))

        # Outperforming → cautiously increase
        elif drift.wr_drift > 0.05 and drift.pf_drift > 0.1:
            adj = self._compute_bounded_step(
                params["alpha_weight"],
                direction=+1,
                step_factor=abs(drift.drift_score) * 0.5,  # More conservative on increase
                bounds=self.bounds.alpha_weight,
            )
            if adj != params["alpha_weight"]:
                adjustments.append(Adjustment(
                    parameter="alpha_weight",
                    old_value=params["alpha_weight"],
                    new_value=adj,
                    reason=f"Outperforming: WR drift=+{drift.wr_drift:.2f}",
                    strategy=strategy,
                    drift_score=drift.drift_score,
                ))

        return adjustments

    def apply_adjustments(self, adjustments: List[Adjustment]) -> int:
        """Apply adjustments to current params. Returns count applied."""
        applied = 0
        for adj in adjustments:
            params = self.current_params[adj.strategy]
            if adj.parameter in params:
                params[adj.parameter] = adj.new_value
                self.adjustment_history.append(adj)
                applied += 1
                logger.info(
                    f"[ADAPTIVE] {adj.strategy} | {adj.parameter}: "
                    f"{adj.old_value:.3f} → {adj.new_value:.3f} | {adj.reason}"
                )
        self._cycle_count += 1
        return applied

    def get_strategy_params(self, strategy: str) -> Dict[str, float]:
        """Get current adjusted parameters for a strategy."""
        return dict(self.current_params[strategy])

    def get_history(self, limit: int = 50) -> List[Dict]:
        """Get recent adjustment history."""
        return [asdict(a) for a in self.adjustment_history[-limit:]]

    def _compute_bounded_step(
        self, current: float, direction: int, step_factor: float,
        bounds: Tuple[float, float],
    ) -> float:
        """Compute a bounded step change."""
        max_step = current * self.MAX_STEP_PCT
        raw_step = max_step * min(step_factor, 1.0) * direction
        new_val = current + raw_step
        return round(max(bounds[0], min(bounds[1], new_val)), 4)

    def snapshot(self) -> Dict:
        """Full state snapshot for API/dashboard."""
        return {
            "cycle_count": self._cycle_count,
            "strategies": dict(self.current_params),
            "recent_adjustments": self.get_history(20),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# COUCHE A — IDLE BACKTEST SCHEDULER
# ═══════════════════════════════════════════════════════════════════════════════

class IdleBacktestScheduler:
    """
    Runs backtests during idle periods (no active signals).
    
    When the engine has no open positions and no pending signals,
    it can use idle CPU to run rolling backtests on recent data.
    """

    def __init__(
        self,
        comparator: PerformanceComparator,
        optimizer: BoundedAdaptiveOptimizer,
        min_idle_sec: float = 60.0,
        backtest_window_days: int = 30,
    ):
        self.comparator = comparator
        self.optimizer = optimizer
        self.min_idle_sec = min_idle_sec
        self.backtest_window_days = backtest_window_days
        self._last_activity = time.time()
        self._last_backtest = 0.0
        self._backtest_interval = 3600  # Min 1h between backtest cycles
        self._pending_strategies: List[str] = []
        self._results_log: List[Dict] = []

    def record_activity(self):
        """Called when engine processes a signal or trade."""
        self._last_activity = time.time()

    def is_idle(self) -> bool:
        """Check if engine has been idle long enough to backtest."""
        return (time.time() - self._last_activity) >= self.min_idle_sec

    def should_run_backtest(self) -> bool:
        """Check if a backtest cycle should run."""
        if not self.is_idle():
            return False
        if (time.time() - self._last_backtest) < self._backtest_interval:
            return False
        return True

    async def run_cycle(self, strategies: List[str], data_provider=None) -> Dict:
        """
        Run a backtest cycle for the given strategies.
        
        Args:
            strategies: List of strategy names to backtest
            data_provider: Callable that returns historical candles

        Returns:
            Summary of backtest results and adjustments made
        """
        if not self.should_run_backtest():
            return {"status": "skipped", "reason": "not_idle_or_too_recent"}

        self._last_backtest = time.time()
        results = {}
        adjustments_applied = 0

        for strategy in strategies:
            try:
                # Run backtest (delegated to existing backtester)
                bt_perf = await self._run_strategy_backtest(strategy, data_provider)
                if bt_perf:
                    self.comparator.set_backtest_result(strategy, bt_perf)
                    drift = self.comparator.compute_drift(strategy)
                    suggested = self.optimizer.suggest_adjustments(drift)
                    if suggested:
                        count = self.optimizer.apply_adjustments(suggested)
                        adjustments_applied += count
                    results[strategy] = {
                        "backtest": asdict(bt_perf),
                        "drift_score": drift.drift_score,
                        "action_needed": drift.action_needed,
                        "adjustments": len(suggested),
                    }
            except Exception as e:
                logger.error(f"[IDLE_BT] Backtest failed for {strategy}: {e}")
                results[strategy] = {"error": str(e)}

        summary = {
            "status": "completed",
            "strategies_tested": len(results),
            "adjustments_applied": adjustments_applied,
            "results": results,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._results_log.append(summary)
        logger.info(
            f"[IDLE_BT] Cycle complete | strategies={len(results)}"
            f" | adjustments={adjustments_applied}"
        )
        return summary

    async def _run_strategy_backtest(
        self, strategy: str, data_provider=None
    ) -> Optional[StrategyPerformance]:
        """Run a single strategy backtest. Override or extend for real implementation."""
        # Placeholder — real implementation connects to SMCBacktester
        logger.info(f"[IDLE_BT] Would backtest {strategy} on {self.backtest_window_days}d window")
        return None

    def get_log(self, limit: int = 10) -> List[Dict]:
        """Get recent backtest cycle logs."""
        return self._results_log[-limit:]


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL SINGLETONS
# ═══════════════════════════════════════════════════════════════════════════════

comparator = PerformanceComparator(lookback_days=30)
optimizer = BoundedAdaptiveOptimizer()
idle_scheduler = IdleBacktestScheduler(comparator=comparator, optimizer=optimizer)
