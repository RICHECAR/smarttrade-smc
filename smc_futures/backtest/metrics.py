"""
Critical Metrics & Edge Validator
═════════════════════════════════
Computes institutional-grade metrics and delivers a binary verdict:
  ✓ EDGE CONFIRMED — strategy has a real, robust edge
  ✗ EDGE INVALIDATED — strategy is an illusion

Rejection criteria (ANY one triggers INVALIDATED):
  1. Net expectancy ≤ 0 after realistic costs
  2. Profit Factor < 1.2 (not enough margin of safety)
  3. Max drawdown > 25% (risk of ruin too high)
  4. Walk-forward OOS consistency < 50%
  5. Walk-forward overfit score > 0.5
  6. Monte Carlo 5th percentile PnL ≤ 0
  7. Monte Carlo robustness score < 0.6
  8. Win rate outside 30%-80% (either random or curve-fitted)
  9. < 30 trades (insufficient sample)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

from smc_futures.backtest.engine import RealisticTrade, EngineState
from smc_futures.backtest.walk_forward import WalkForwardResult
from smc_futures.backtest.monte_carlo import MonteCarloResult


# ───────────────────────────────────────────
#  Detailed Metrics
# ───────────────────────────────────────────

@dataclass
class DetailedMetrics:
    """Institutional-grade backtest metrics."""
    # Basics
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0

    # Win rate
    win_rate: float = 0.0            # %

    # PnL
    total_net_pnl: float = 0.0
    total_gross_pnl: float = 0.0
    total_fees: float = 0.0
    total_funding: float = 0.0
    total_slippage_cost: float = 0.0  # Estimated $ lost to slippage

    # Averages
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_trade: float = 0.0
    median_trade: float = 0.0

    # Risk-adjusted
    profit_factor: float = 0.0
    expectancy: float = 0.0          # Avg $ per trade
    expectancy_per_risk: float = 0.0 # Expectancy / avg risk amount

    # Drawdown
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    avg_drawdown_pct: float = 0.0
    max_drawdown_duration_bars: int = 0

    # Risk of ruin
    risk_of_ruin: float = 0.0       # Kelly-based estimate

    # Ratios
    sharpe_ratio: float = 0.0       # Annualized
    sortino_ratio: float = 0.0      # Downside-only volatility
    calmar_ratio: float = 0.0       # Return / Max DD
    recovery_factor: float = 0.0    # Total PnL / Max DD

    # Distribution
    skewness: float = 0.0           # PnL distribution skew
    kurtosis: float = 0.0           # Fat tails measure
    tail_ratio: float = 0.0         # 95th pctile win / 5th pctile loss

    # Time analysis
    avg_holding_bars: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0

    # MAE/MFE
    avg_mae: float = 0.0
    avg_mfe: float = 0.0
    edge_ratio: float = 0.0        # MFE / MAE (> 1 = good edge)

    # Cost analysis
    fees_as_pct_of_pnl: float = 0.0
    slippage_as_pct_of_pnl: float = 0.0

    # Return
    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0


# ───────────────────────────────────────────
#  Edge Verdict
# ───────────────────────────────────────────

@dataclass
class EdgeVerdict:
    """Final binary verdict on strategy edge."""
    verdict: str = "EDGE INVALIDATED"   # "EDGE CONFIRMED" or "EDGE INVALIDATED"
    confidence: str = "LOW"             # "LOW", "MEDIUM", "HIGH"
    score: float = 0.0                  # 0-100 composite score

    # Sub-scores
    profitability_score: float = 0.0    # 0-25
    risk_score: float = 0.0            # 0-25
    robustness_score: float = 0.0      # 0-25
    consistency_score: float = 0.0     # 0-25

    # Rejection reasons
    rejections: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # Components
    metrics: Optional[DetailedMetrics] = None
    walk_forward: Optional[WalkForwardResult] = None
    monte_carlo: Optional[MonteCarloResult] = None


# ═══════════════════════════════════════════
#  METRICS CALCULATOR
# ═══════════════════════════════════════════

class MetricsCalculator:
    """Compute detailed metrics from a list of RealisticTrade."""

    @staticmethod
    def compute(
        trades: List[RealisticTrade],
        equity_curve: List[float],
        initial_capital: float = 10_000.0,
        bars_per_year: int = 105_120,   # 5m bars in a year
    ) -> DetailedMetrics:
        m = DetailedMetrics()
        if not trades:
            return m

        pnls = [t.net_pnl for t in trades]
        m.total_trades = len(trades)
        m.winning_trades = sum(1 for p in pnls if p > 0)
        m.losing_trades = sum(1 for p in pnls if p < 0)
        m.breakeven_trades = sum(1 for p in pnls if p == 0)
        m.win_rate = m.winning_trades / m.total_trades * 100

        # PnL
        m.total_net_pnl = sum(pnls)
        m.total_gross_pnl = sum(t.raw_pnl for t in trades)
        m.total_fees = sum(t.total_fees for t in trades)
        m.total_funding = sum(t.funding_cost for t in trades)

        # Slippage cost estimate
        for t in trades:
            entry_slip = abs(t.actual_entry - t.intended_entry) * t.qty if t.intended_entry else 0
            m.total_slippage_cost += entry_slip

        # Averages
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        m.avg_win = _mean(wins)
        m.avg_loss = _mean(losses)
        m.avg_trade = _mean(pnls)
        m.median_trade = _percentile(pnls, 50)

        # Profit Factor
        gross_profit = sum(wins) if wins else 0
        gross_loss = abs(sum(losses)) if losses else 0
        m.profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
            float("inf") if gross_profit > 0 else 0
        )

        # Expectancy
        m.expectancy = m.avg_trade

        # Drawdown from equity curve
        if equity_curve:
            peak = equity_curve[0]
            max_dd = 0.0
            max_dd_usd = 0.0
            dd_durations = []
            dd_start = 0
            in_dd = False
            all_dds = []

            for i, eq in enumerate(equity_curve):
                if eq >= peak:
                    peak = eq
                    if in_dd:
                        dd_durations.append(i - dd_start)
                        in_dd = False
                else:
                    if not in_dd:
                        dd_start = i
                        in_dd = True
                    dd_pct = (peak - eq) / peak * 100 if peak > 0 else 0
                    dd_usd = peak - eq
                    all_dds.append(dd_pct)
                    max_dd = max(max_dd, dd_pct)
                    max_dd_usd = max(max_dd_usd, dd_usd)

            m.max_drawdown_pct = max_dd
            m.max_drawdown_usd = max_dd_usd
            m.avg_drawdown_pct = _mean(all_dds) if all_dds else 0
            m.max_drawdown_duration_bars = max(dd_durations) if dd_durations else 0

        # Sharpe ratio (annualized)
        if len(pnls) > 1:
            avg_ret = _mean(pnls)
            std_ret = _stdev(pnls)
            trades_per_year = bars_per_year / max(
                _mean([t.holding_bars for t in trades]), 1
            )
            m.sharpe_ratio = (avg_ret / std_ret * math.sqrt(trades_per_year)
                              if std_ret > 0 else 0)

        # Sortino (downside deviation)
        neg_pnls = [p for p in pnls if p < 0]
        if neg_pnls:
            downside_dev = _stdev(neg_pnls)
            avg_ret = _mean(pnls)
            trades_per_year = bars_per_year / max(
                _mean([t.holding_bars for t in trades]), 1
            )
            m.sortino_ratio = (avg_ret / downside_dev * math.sqrt(trades_per_year)
                               if downside_dev > 0 else 0)

        # Calmar
        total_return_pct = m.total_net_pnl / initial_capital * 100
        m.total_return_pct = total_return_pct
        m.calmar_ratio = total_return_pct / m.max_drawdown_pct if m.max_drawdown_pct > 0 else 0

        # Recovery factor
        m.recovery_factor = m.total_net_pnl / m.max_drawdown_usd if m.max_drawdown_usd > 0 else 0

        # Distribution
        if len(pnls) >= 3:
            m.skewness = _skewness(pnls)
            m.kurtosis = _kurtosis(pnls)

        # Tail ratio
        p95_win = _percentile(wins, 95) if len(wins) >= 2 else 0
        p5_loss = abs(_percentile(losses, 5)) if len(losses) >= 2 else 0
        m.tail_ratio = p95_win / p5_loss if p5_loss > 0 else 0

        # Time
        holding_bars = [t.holding_bars for t in trades]
        m.avg_holding_bars = _mean(holding_bars)

        # Consecutive wins/losses
        m.max_consecutive_wins = _max_consecutive(pnls, positive=True)
        m.max_consecutive_losses = _max_consecutive(pnls, positive=False)

        # MAE/MFE
        maes = [t.mae for t in trades if t.mae != 0]
        mfes = [t.mfe for t in trades if t.mfe != 0]
        m.avg_mae = _mean(maes)
        m.avg_mfe = _mean(mfes)
        m.edge_ratio = abs(m.avg_mfe / m.avg_mae) if m.avg_mae != 0 else 0

        # Cost analysis
        if abs(m.total_gross_pnl) > 0:
            m.fees_as_pct_of_pnl = m.total_fees / abs(m.total_gross_pnl) * 100
            m.slippage_as_pct_of_pnl = m.total_slippage_cost / abs(m.total_gross_pnl) * 100

        # Risk of ruin (simplified Kelly)
        if m.win_rate > 0 and m.avg_loss != 0:
            W = m.win_rate / 100
            R = abs(m.avg_win / m.avg_loss) if m.avg_loss != 0 else 1
            if R > 0 and W > 0:
                q = 1 - W
                if R != 1:
                    m.risk_of_ruin = min(1.0, (q / W) ** 10) if q > W else 0.0
                else:
                    m.risk_of_ruin = 1.0

        return m


# ═══════════════════════════════════════════
#  EDGE VALIDATOR
# ═══════════════════════════════════════════

class EdgeValidator:
    """
    Validates whether a strategy has a real edge.
    Combines backtest metrics + walk-forward + Monte Carlo.
    """

    # ── Thresholds ──
    MIN_TRADES = 30
    MIN_PROFIT_FACTOR = 1.2
    MAX_DRAWDOWN_PCT = 25.0
    MIN_WIN_RATE = 30.0
    MAX_WIN_RATE = 80.0
    MIN_EXPECTANCY = 0.0
    MIN_SHARPE = 0.3
    MIN_WF_CONSISTENCY = 0.50
    MAX_WF_OVERFIT = 0.50
    MIN_MC_ROBUSTNESS = 0.60

    @classmethod
    def validate(
        cls,
        metrics: DetailedMetrics,
        wf_result: Optional[WalkForwardResult] = None,
        mc_result: Optional[MonteCarloResult] = None,
    ) -> EdgeVerdict:
        """
        Produce final edge verdict.

        ANY rejection → EDGE INVALIDATED.
        """
        v = EdgeVerdict(metrics=metrics, walk_forward=wf_result, monte_carlo=mc_result)

        # ── Profitability Score (0-25) ───────
        prof_score = 0.0

        if metrics.total_trades < cls.MIN_TRADES:
            v.rejections.append(
                f"INSUFFICIENT SAMPLE: {metrics.total_trades} trades < {cls.MIN_TRADES} minimum"
            )
        else:
            prof_score += 5

        if metrics.expectancy <= cls.MIN_EXPECTANCY:
            v.rejections.append(
                f"NEGATIVE EXPECTANCY: ${metrics.expectancy:.2f} per trade"
            )
        else:
            prof_score += min(10, metrics.expectancy / max(abs(metrics.avg_loss), 1) * 10)

        if metrics.profit_factor < cls.MIN_PROFIT_FACTOR:
            v.rejections.append(
                f"LOW PROFIT FACTOR: {metrics.profit_factor:.2f} < {cls.MIN_PROFIT_FACTOR}"
            )
        else:
            prof_score += min(10, (metrics.profit_factor - 1) * 10)

        v.profitability_score = min(25, prof_score)

        # ── Risk Score (0-25) ────────────────
        risk_score = 25.0

        if metrics.max_drawdown_pct > cls.MAX_DRAWDOWN_PCT:
            v.rejections.append(
                f"EXCESSIVE DRAWDOWN: {metrics.max_drawdown_pct:.1f}% > {cls.MAX_DRAWDOWN_PCT}%"
            )
            risk_score -= 15

        if metrics.win_rate < cls.MIN_WIN_RATE:
            v.rejections.append(
                f"WIN RATE TOO LOW: {metrics.win_rate:.1f}% < {cls.MIN_WIN_RATE}%"
            )
            risk_score -= 5
        elif metrics.win_rate > cls.MAX_WIN_RATE:
            v.warnings.append(
                f"SUSPICIOUSLY HIGH WIN RATE: {metrics.win_rate:.1f}% (possible curve fitting)"
            )
            risk_score -= 5

        if metrics.sharpe_ratio < cls.MIN_SHARPE:
            v.warnings.append(f"LOW SHARPE: {metrics.sharpe_ratio:.2f}")
            risk_score -= 5

        if metrics.max_consecutive_losses >= 10:
            v.warnings.append(f"Long losing streak: {metrics.max_consecutive_losses}")
            risk_score -= 5

        v.risk_score = max(0, risk_score)

        # ── Robustness Score (0-25) ──────────
        rob_score = 0.0

        if mc_result:
            if mc_result.robustness_score < cls.MIN_MC_ROBUSTNESS:
                v.rejections.append(
                    f"MC ROBUSTNESS FAILED: {mc_result.robustness_score:.2f} < {cls.MIN_MC_ROBUSTNESS}"
                )
            else:
                rob_score += mc_result.robustness_score * 15

            if mc_result.perm_final_pnl_5th <= 0:
                v.rejections.append(
                    f"MC 5th PERCENTILE LOSS: ${mc_result.perm_final_pnl_5th:.0f}"
                )
            else:
                rob_score += 5

            if mc_result.param_unprofitable_pct > 50:
                v.warnings.append(
                    f"Parameter sensitive: unprofitable in {mc_result.param_unprofitable_pct:.0f}% of variations"
                )
            else:
                rob_score += 5

            v.warnings.extend(mc_result.warnings)
        else:
            v.warnings.append("No Monte Carlo analysis performed")

        v.robustness_score = min(25, rob_score)

        # ── Consistency Score (0-25) ─────────
        cons_score = 0.0

        if wf_result:
            if wf_result.oos_consistency_ratio < cls.MIN_WF_CONSISTENCY:
                v.rejections.append(
                    f"WF INCONSISTENT: {wf_result.oos_consistency_ratio:.0%} OOS folds profitable "
                    f"< {cls.MIN_WF_CONSISTENCY:.0%}"
                )
            else:
                cons_score += wf_result.oos_consistency_ratio * 15

            if wf_result.overfit_score > cls.MAX_WF_OVERFIT:
                v.rejections.append(
                    f"WF OVERFIT: {wf_result.overfit_score:.0%} folds show overfitting "
                    f"> {cls.MAX_WF_OVERFIT:.0%}"
                )
            else:
                cons_score += (1 - wf_result.overfit_score) * 10

            if wf_result.oos_profit_factor < 1.0:
                v.rejections.append(
                    f"WF OOS UNPROFITABLE: PF = {wf_result.oos_profit_factor:.2f}"
                )
        else:
            v.warnings.append("No walk-forward validation performed")

        v.consistency_score = min(25, cons_score)

        # ── Final Verdict ────────────────────
        v.score = v.profitability_score + v.risk_score + v.robustness_score + v.consistency_score

        if v.rejections:
            v.verdict = "EDGE INVALIDATED"
            v.confidence = "HIGH" if len(v.rejections) >= 3 else "MEDIUM"
        else:
            v.verdict = "EDGE CONFIRMED"
            if v.score >= 80:
                v.confidence = "HIGH"
            elif v.score >= 60:
                v.confidence = "MEDIUM"
            else:
                v.confidence = "LOW"

        return v

    @staticmethod
    def format_verdict(verdict: EdgeVerdict) -> str:
        """Format verdict as a human-readable report."""
        lines = []
        sep = "═" * 60

        lines.append(sep)
        if verdict.verdict == "EDGE CONFIRMED":
            lines.append(f"  ✓ {verdict.verdict}  (Confidence: {verdict.confidence})")
        else:
            lines.append(f"  ✗ {verdict.verdict}  (Confidence: {verdict.confidence})")
        lines.append(sep)
        lines.append(f"  Score: {verdict.score:.0f}/100")
        lines.append(f"    Profitability : {verdict.profitability_score:.0f}/25")
        lines.append(f"    Risk          : {verdict.risk_score:.0f}/25")
        lines.append(f"    Robustness    : {verdict.robustness_score:.0f}/25")
        lines.append(f"    Consistency   : {verdict.consistency_score:.0f}/25")

        if verdict.metrics:
            m = verdict.metrics
            lines.append("")
            lines.append("  ── Métriques clés ──")
            lines.append(f"    Trades        : {m.total_trades}")
            lines.append(f"    Win Rate      : {m.win_rate:.1f}%")
            lines.append(f"    Profit Factor : {m.profit_factor:.2f}")
            lines.append(f"    Expectancy    : ${m.expectancy:.2f}/trade")
            lines.append(f"    Max Drawdown  : {m.max_drawdown_pct:.1f}%")
            lines.append(f"    Sharpe Ratio  : {m.sharpe_ratio:.2f}")
            lines.append(f"    Total PnL     : ${m.total_net_pnl:.2f}")
            lines.append(f"    Total Fees    : ${m.total_fees:.2f}")
            lines.append(f"    Total Funding : ${m.total_funding:.2f}")
            lines.append(f"    Slippage Cost : ${m.total_slippage_cost:.2f}")

        if verdict.rejections:
            lines.append("")
            lines.append("  ── REJECTIONS ──")
            for r in verdict.rejections:
                lines.append(f"    ✗ {r}")

        if verdict.warnings:
            lines.append("")
            lines.append("  ── Warnings ──")
            for w in verdict.warnings:
                lines.append(f"    ⚠ {w}")

        lines.append(sep)
        return "\n".join(lines)


# ───────────────────────────────────────────
#  Statistics helpers
# ───────────────────────────────────────────

def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0

def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = (pct / 100) * (len(s) - 1)
    low = int(math.floor(idx))
    high = int(math.ceil(idx))
    if low == high:
        return s[low]
    frac = idx - low
    return s[low] * (1 - frac) + s[high] * frac

def _stdev(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = _mean(values)
    variance = sum((v - avg) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)

def _skewness(values: List[float]) -> float:
    n = len(values)
    if n < 3:
        return 0.0
    avg = _mean(values)
    std = _stdev(values)
    if std == 0:
        return 0.0
    return (n / ((n - 1) * (n - 2))) * sum(((v - avg) / std) ** 3 for v in values)

def _kurtosis(values: List[float]) -> float:
    n = len(values)
    if n < 4:
        return 0.0
    avg = _mean(values)
    std = _stdev(values)
    if std == 0:
        return 0.0
    m4 = sum(((v - avg) / std) ** 4 for v in values) / n
    return m4 - 3  # Excess kurtosis

def _max_consecutive(pnls: List[float], positive: bool) -> int:
    max_streak = 0
    current = 0
    for p in pnls:
        if (positive and p > 0) or (not positive and p < 0):
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak
