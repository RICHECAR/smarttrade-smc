"""
Backtest Pipeline Orchestrator
══════════════════════════════
Ties all components together:
  1. Load data (candles from list or MySQL)
  2. Run realistic backtest engine
  3. Walk-forward validation
  4. Monte Carlo simulation
  5. Compute metrics & edge verdict
  6. Format comparison report (old vs new)

Usage:
    from smc_futures.backtest.pipeline import BacktestPipeline, PipelineConfig

    pipeline = BacktestPipeline(PipelineConfig())
    result = pipeline.run(symbol="BTCUSDT", candles=candles, signal_fn=my_strategy)
    print(result.report)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional, Callable, Dict

from smc_futures.backtest.engine import (
    RealisticBacktestEngine,
    RealisticBacktestConfig,
    BTCandle,
    BacktestSignal,
    EngineState,
    RealisticTrade,
)
from smc_futures.backtest.market_model import MarketModel
from smc_futures.backtest.walk_forward import (
    WalkForwardValidator,
    WalkForwardConfig,
    WalkForwardResult,
)
from smc_futures.backtest.monte_carlo import (
    MonteCarloSimulator,
    MonteCarloConfig,
    MonteCarloResult,
)
from smc_futures.backtest.metrics import (
    MetricsCalculator,
    EdgeValidator,
    EdgeVerdict,
    DetailedMetrics,
)

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────
#  Pipeline Config
# ───────────────────────────────────────────

@dataclass
class PipelineConfig:
    """Combined config for the full pipeline."""
    # Backtest engine
    bt_config: RealisticBacktestConfig = field(default_factory=RealisticBacktestConfig)

    # Walk-forward — set to None to skip
    wf_config: Optional[WalkForwardConfig] = field(default_factory=WalkForwardConfig)

    # Monte Carlo — set to None to skip
    mc_config: Optional[MonteCarloConfig] = field(default_factory=MonteCarloConfig)

    # Max holding time (0 = unlimited)
    max_holding_bars: int = 288          # 24 hours of 5m bars

    # Comparison mode
    run_naive_comparison: bool = True    # Also run naive backtest for comparison


# ───────────────────────────────────────────
#  Pipeline Result
# ───────────────────────────────────────────

@dataclass
class PipelineResult:
    """Full pipeline output."""
    symbol: str = ""
    duration_seconds: float = 0.0

    # Realistic backtest
    engine_state: Optional[EngineState] = None
    metrics: Optional[DetailedMetrics] = None

    # Walk-forward
    walk_forward: Optional[WalkForwardResult] = None

    # Monte Carlo
    monte_carlo: Optional[MonteCarloResult] = None

    # Final verdict
    verdict: Optional[EdgeVerdict] = None

    # Naive comparison (if enabled)
    naive_metrics: Optional[DetailedMetrics] = None
    naive_trades: int = 0

    # Report
    report: str = ""


# ═══════════════════════════════════════════
#  BACKTEST PIPELINE
# ═══════════════════════════════════════════

class BacktestPipeline:
    """Full realistic backtest pipeline."""

    def __init__(self, config: PipelineConfig):
        self.config = config

    def run(
        self,
        symbol: str,
        candles: List[BTCandle],
        signal_fn: Callable[[List[BTCandle]], Optional[BacktestSignal]],
    ) -> PipelineResult:
        """
        Execute the complete pipeline.

        Args:
            symbol: Trading pair
            candles: Full OHLCV history
            signal_fn: Strategy function (closed bars only → BacktestSignal or None)

        Returns:
            PipelineResult with metrics, WF, MC, verdict, and report.
        """
        start_time = time.time()
        result = PipelineResult(symbol=symbol)

        logger.info(f"[PIPELINE] {symbol}: Starting realistic backtest ({len(candles)} bars)")

        # ── Step 1: Run realistic backtest ────
        logger.info("[PIPELINE] Step 1/5: Realistic backtest engine...")
        engine = RealisticBacktestEngine(config=self.config.bt_config)
        state = engine.run(
            symbol, candles, signal_fn,
            max_holding_bars=self.config.max_holding_bars,
        )
        result.engine_state = state
        logger.info(f"[PIPELINE] → {len(state.trades)} trades, capital: ${state.capital:.2f}")

        # ── Step 2: Compute detailed metrics ──
        logger.info("[PIPELINE] Step 2/5: Computing metrics...")
        result.metrics = MetricsCalculator.compute(
            state.trades,
            state.equity_curve,
            self.config.bt_config.initial_capital,
        )

        # ── Step 3: Walk-forward validation ───
        if self.config.wf_config and len(candles) >= (
            self.config.wf_config.train_bars
            + self.config.wf_config.validate_bars
            + self.config.wf_config.test_bars
        ):
            logger.info("[PIPELINE] Step 3/5: Walk-forward validation...")
            wf = WalkForwardValidator(self.config.wf_config, self.config.bt_config)
            result.walk_forward = wf.validate(symbol, candles, signal_fn)
            logger.info(
                f"[PIPELINE] → {result.walk_forward.valid_folds} valid folds, "
                f"OOS PF={result.walk_forward.oos_profit_factor:.2f}, "
                f"consistency={result.walk_forward.oos_consistency_ratio:.0%}"
            )
        else:
            logger.info("[PIPELINE] Step 3/5: Walk-forward SKIPPED (not enough data)")

        # ── Step 4: Monte Carlo ───────────────
        if self.config.mc_config and len(state.trades) >= 5:
            logger.info("[PIPELINE] Step 4/5: Monte Carlo simulation...")
            mc = MonteCarloSimulator(self.config.mc_config)
            result.monte_carlo = mc.analyze(
                state.trades,
                self.config.bt_config.initial_capital,
            )
            logger.info(
                f"[PIPELINE] → Robustness={result.monte_carlo.robustness_score:.2f}, "
                f"95th DD={result.monte_carlo.perm_max_dd_95th:.1f}%"
            )
        else:
            logger.info("[PIPELINE] Step 4/5: Monte Carlo SKIPPED (too few trades)")

        # ── Step 5: Edge verdict ──────────────
        logger.info("[PIPELINE] Step 5/5: Edge validation...")
        result.verdict = EdgeValidator.validate(
            result.metrics,
            result.walk_forward,
            result.monte_carlo,
        )

        # ── Naive comparison ──────────────────
        if self.config.run_naive_comparison:
            result.naive_metrics = self._run_naive(symbol, candles, signal_fn)
            if result.naive_metrics:
                result.naive_trades = result.naive_metrics.total_trades

        # ── Generate report ───────────────────
        result.duration_seconds = time.time() - start_time
        result.report = self._format_report(result)

        logger.info(f"[PIPELINE] Done in {result.duration_seconds:.1f}s — {result.verdict.verdict}")
        return result

    # ── Naive backtest (no slippage/fees) ────

    def _run_naive(
        self, symbol: str, candles: List[BTCandle],
        signal_fn: Callable[[List[BTCandle]], Optional[BacktestSignal]],
    ) -> Optional[DetailedMetrics]:
        """Run a 'naive' backtest with zero costs for comparison."""
        from smc_futures.backtest.market_model import SymbolProfile

        naive_profile = SymbolProfile(
            symbol=symbol,
            base_spread_bps=0, spread_vol_mult=0,
            base_slippage_bps=0, slippage_vol_mult=0, slippage_size_mult=0,
            avg_5m_volume_usd=1e12,  # Infinite liquidity
            min_fill_ratio=1.0,
            maker_fee_rate=0, taker_fee_rate=0,
            avg_funding_rate_8h=0,
            base_latency_ms=0, latency_jitter_ms=0,
        )
        naive_model = MarketModel(naive_profile, seed=self.config.bt_config.seed)

        naive_config = RealisticBacktestConfig(
            initial_capital=self.config.bt_config.initial_capital,
            risk_per_trade=self.config.bt_config.risk_per_trade,
            leverage=self.config.bt_config.leverage,
            seed=self.config.bt_config.seed,
            charge_funding=False,
        )
        engine = RealisticBacktestEngine(
            config=naive_config,
            market_models={symbol: naive_model},
        )
        state = engine.run(symbol, candles, signal_fn, self.config.max_holding_bars)

        if state.trades:
            return MetricsCalculator.compute(
                state.trades, state.equity_curve, naive_config.initial_capital,
            )
        return None

    # ── Report formatting ────────────────────

    @staticmethod
    def _format_report(result: PipelineResult) -> str:
        lines = []
        SEP = "═" * 70
        sep = "─" * 70

        lines.append(SEP)
        lines.append(f"  REALISTIC BACKTEST REPORT — {result.symbol}")
        lines.append(SEP)
        lines.append("")

        # ── Verdict ──
        if result.verdict:
            lines.append(EdgeValidator.format_verdict(result.verdict))
            lines.append("")

        # ── Comparison: Naive vs Realistic ──
        if result.naive_metrics and result.metrics:
            lines.append(sep)
            lines.append("  COMPARISON: NAIVE vs REALISTIC")
            lines.append(sep)
            n = result.naive_metrics
            r = result.metrics
            lines.append(f"  {'Metric':<25} {'Naive':>12} {'Realistic':>12} {'Impact':>12}")
            lines.append(f"  {'─'*25} {'─'*12} {'─'*12} {'─'*12}")

            def _row(label, nv, rv, fmt=".2f", pct=False):
                nf = f"{nv:{fmt}}" if not pct else f"{nv:{fmt}}%"
                rf = f"{rv:{fmt}}" if not pct else f"{rv:{fmt}}%"
                if isinstance(nv, (int, float)) and isinstance(rv, (int, float)) and nv != 0:
                    impact = (rv - nv) / abs(nv) * 100 if nv != 0 else 0
                    return f"  {label:<25} {nf:>12} {rf:>12} {impact:>+11.1f}%"
                return f"  {label:<25} {nf:>12} {rf:>12} {'N/A':>12}"

            lines.append(_row("Trades", n.total_trades, r.total_trades, "d"))
            lines.append(_row("Win Rate (%)", n.win_rate, r.win_rate, ".1f", True))
            lines.append(_row("Profit Factor", n.profit_factor, r.profit_factor))
            lines.append(_row("Expectancy ($/trade)", n.expectancy, r.expectancy))
            lines.append(_row("Net PnL ($)", n.total_net_pnl, r.total_net_pnl))
            lines.append(_row("Max Drawdown (%)", n.max_drawdown_pct, r.max_drawdown_pct, ".1f", True))
            lines.append(_row("Sharpe Ratio", n.sharpe_ratio, r.sharpe_ratio))

            if r.total_fees > 0:
                lines.append(f"\n  Realistic cost breakdown:")
                lines.append(f"    Fees      : ${r.total_fees:.2f}")
                lines.append(f"    Funding   : ${r.total_funding:.2f}")
                lines.append(f"    Slippage  : ${r.total_slippage_cost:.2f}")
                total_cost = r.total_fees + r.total_funding + r.total_slippage_cost
                lines.append(f"    TOTAL COST: ${total_cost:.2f}")
                if n.total_net_pnl > 0:
                    eaten = total_cost / n.total_net_pnl * 100
                    lines.append(f"    Costs eat {eaten:.1f}% of naive profits")

            lines.append("")

        # ── Walk-Forward Summary ──
        if result.walk_forward:
            wf = result.walk_forward
            lines.append(sep)
            lines.append("  WALK-FORWARD VALIDATION")
            lines.append(sep)
            lines.append(f"  Total folds       : {wf.total_folds}")
            lines.append(f"  Valid folds       : {wf.valid_folds}")
            lines.append(f"  Overfit folds     : {wf.overfit_folds}")
            lines.append(f"  OOS Trades        : {wf.oos_total_trades}")
            lines.append(f"  OOS Win Rate      : {wf.oos_win_rate:.1f}%")
            lines.append(f"  OOS Profit Factor : {wf.oos_profit_factor:.2f}")
            lines.append(f"  OOS Consistency   : {wf.oos_consistency_ratio:.0%}")
            lines.append(f"  Overfit Score     : {wf.overfit_score:.2f}")
            lines.append(f"  IS Win Rate       : {wf.is_win_rate:.1f}%")
            lines.append(f"  IS Profit Factor  : {wf.is_profit_factor:.2f}")
            lines.append(f"  Robust?           : {'YES' if wf.is_robust else 'NO'}")
            lines.append("")

        # ── Monte Carlo Summary ──
        if result.monte_carlo:
            mc = result.monte_carlo
            lines.append(sep)
            lines.append("  MONTE CARLO ANALYSIS")
            lines.append(sep)
            lines.append(f"  Permutation (order sensitivity):")
            lines.append(f"    95th pctile DD  : {mc.perm_max_dd_95th:.1f}%")
            lines.append(f"    5th pctile PnL  : ${mc.perm_final_pnl_5th:.0f}")
            lines.append(f"    Median PF       : {mc.perm_pf_median:.2f}")
            lines.append(f"  Parameter sensitivity:")
            lines.append(f"    5th pctile PnL  : ${mc.param_pnl_5th:.0f}")
            lines.append(f"    Unprofitable    : {mc.param_unprofitable_pct:.0f}%")
            lines.append(f"  Skip-N stability:")
            lines.append(f"    5th pctile PnL  : ${mc.skip_pnl_5th:.0f}")
            lines.append(f"    Unprofitable    : {mc.skip_unprofitable_pct:.0f}%")
            lines.append(f"  Robustness Score  : {mc.robustness_score:.2f}")
            lines.append(f"  Robust?           : {'YES' if mc.is_robust else 'NO'}")
            lines.append("")

        lines.append(f"  Pipeline duration: {result.duration_seconds:.1f}s")
        lines.append(SEP)

        return "\n".join(lines)
