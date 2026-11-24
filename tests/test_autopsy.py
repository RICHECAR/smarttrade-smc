"""
Tests — Edge Autopsy (Chantier 9)
══════════════════════════════════
Tests for:
  - PnL decomposition (reconciliation, waterfall)
  - Segmentation (10 dimensions, verdicts)
  - Survival filtering (filter impacts)
  - False edge detection (root cause scoring)
  - Decision matrix (KILL / SALVAGE / RESEARCH / PROMISING)
  - Full autopsy pipeline
"""

import math
import pytest
from typing import List

from smc_futures.backtest.engine import RealisticTrade, BTCandle, BacktestSignal
from smc_futures.backtest.metrics import DetailedMetrics, MetricsCalculator

from smc_futures.backtest.autopsy import (
    PnLDecomposer,
    TradeDecomposition,
    CostAttribution,
    FalseEdgeDetector,
    FalseEdgeScore,
    DecisionMatrix,
    DecisionVerdict,
    AutopsyResult,
    run_autopsy,
    format_autopsy_report,
)
from smc_futures.backtest.segmentation import (
    SegmentationEngine,
    SegmentStats,
    SegmentationResult,
)
from smc_futures.backtest.survival_filter import (
    SurvivalFilterEngine,
    FilterResult,
    SurvivalResult,
)


# ═══════════════════════════════════════════
#  FIXTURES — Synthetic Trades
# ═══════════════════════════════════════════

def _make_trade(
    idx: int = 0,
    direction: str = "long",
    setup_type: str = "reversal_long",
    intended_entry: float = 60000.0,
    actual_entry: float = 60010.0,
    stop_loss: float = 59800.0,
    take_profit: float = 60400.0,
    intended_exit: float = 60400.0,
    actual_exit: float = 60395.0,
    exit_reason: str = "tp",
    qty: float = 0.1,
    entry_fee: float = 3.6,
    exit_fee: float = 3.6,
    funding_cost: float = 0.5,
    entry_slippage_bps: float = 1.67,
    exit_slippage_bps: float = 0.83,
    confidence: float = 0.6,
    holding_bars: int = 50,
    volatility_regime: str = "normal",
    atr_at_entry: float = 150.0,
    entry_timestamp: int = 0,
    mae: float = -10.0,
    mfe: float = 40.0,
) -> RealisticTrade:
    """Create a synthetic trade for testing."""
    if entry_timestamp == 0:
        entry_timestamp = 1_700_000_000_000 + idx * 300_000 * 50

    # Compute raw PnL
    if direction == "long":
        raw_pnl = (actual_exit - actual_entry) * qty
    else:
        raw_pnl = (actual_entry - actual_exit) * qty

    total_fees = entry_fee + exit_fee
    net_pnl = raw_pnl - total_fees - funding_cost

    rr = 0.0
    risk = abs(actual_entry - stop_loss)
    if risk > 0:
        if direction == "long":
            rr = (actual_exit - actual_entry) / risk
        else:
            rr = (actual_entry - actual_exit) / risk

    return RealisticTrade(
        signal_bar_idx=idx * 50,
        signal_price=intended_entry,
        direction=direction,
        setup_type=setup_type,
        confidence=confidence,
        entry_bar_idx=idx * 50 + 1,
        intended_entry=intended_entry,
        actual_entry=actual_entry,
        entry_slippage_bps=entry_slippage_bps,
        entry_fee=entry_fee,
        entry_timestamp=entry_timestamp,
        entry_latency_ms=55.0,
        stop_loss=stop_loss,
        take_profit=take_profit,
        exit_bar_idx=idx * 50 + 1 + holding_bars,
        intended_exit=intended_exit,
        actual_exit=actual_exit,
        exit_slippage_bps=exit_slippage_bps,
        exit_fee=exit_fee,
        exit_timestamp=entry_timestamp + holding_bars * 300_000,
        exit_reason=exit_reason,
        qty=qty,
        notional_usd=qty * actual_entry,
        raw_pnl=raw_pnl,
        total_fees=total_fees,
        funding_cost=funding_cost,
        net_pnl=net_pnl,
        net_pnl_pct=net_pnl / 10000 * 100,
        rr_achieved=rr,
        mae=mae,
        mfe=mfe,
        holding_bars=holding_bars,
        atr_at_entry=atr_at_entry,
        volatility_regime=volatility_regime,
    )


def make_winning_long() -> RealisticTrade:
    """Long → TP hit, net positive."""
    return _make_trade(
        idx=0, direction="long",
        intended_entry=60000, actual_entry=60010,
        stop_loss=59800, take_profit=60400,
        intended_exit=60400, actual_exit=60395,
        exit_reason="tp", qty=0.1,
        entry_fee=3.6, exit_fee=3.6, funding_cost=0.5,
    )


def make_losing_long() -> RealisticTrade:
    """Long → SL hit, net negative."""
    return _make_trade(
        idx=1, direction="long",
        intended_entry=60000, actual_entry=60010,
        stop_loss=59800, take_profit=60400,
        intended_exit=59800, actual_exit=59780,
        exit_reason="sl", qty=0.1,
        entry_fee=3.6, exit_fee=3.6, funding_cost=0.5,
        exit_slippage_bps=3.33,
        mae=-25.0, mfe=5.0,
        holding_bars=20,
    )


def make_timeout_long() -> RealisticTrade:
    """Long → timeout exit."""
    return _make_trade(
        idx=2, direction="long",
        intended_entry=60000, actual_entry=60010,
        stop_loss=59800, take_profit=60400,
        intended_exit=60400,  # Engine sets this to TP
        actual_exit=60050,
        exit_reason="timeout", qty=0.1,
        entry_fee=3.6, exit_fee=3.6, funding_cost=1.5,
        exit_slippage_bps=2.0,
        holding_bars=288,
    )


def make_winning_short() -> RealisticTrade:
    """Short → TP hit, net positive."""
    return _make_trade(
        idx=3, direction="short", setup_type="reversal_short",
        intended_entry=60000, actual_entry=59990,
        stop_loss=60200, take_profit=59600,
        intended_exit=59600, actual_exit=59605,
        exit_reason="tp", qty=0.1,
        entry_fee=3.6, exit_fee=3.6, funding_cost=0.5,
        entry_slippage_bps=1.67, exit_slippage_bps=0.83,
    )


def make_losing_short() -> RealisticTrade:
    """Short → SL hit, net negative."""
    return _make_trade(
        idx=4, direction="short", setup_type="reversal_short",
        intended_entry=60000, actual_entry=59990,
        stop_loss=60200, take_profit=59600,
        intended_exit=60200, actual_exit=60220,
        exit_reason="sl", qty=0.1,
        entry_fee=3.6, exit_fee=3.6, funding_cost=0.3,
        exit_slippage_bps=3.33,
        mae=-25.0, mfe=5.0, holding_bars=15,
    )


def make_many_trades(n: int = 30) -> List[RealisticTrade]:
    """Generate n diverse trades for segmentation/filter tests."""
    trades = []
    regimes = ["low", "normal", "high", "extreme"]
    dirs = ["long", "short"]
    setups = ["reversal_long", "reversal_short"]
    exits = ["sl", "tp", "timeout"]

    for i in range(n):
        direction = dirs[i % 2]
        regime = regimes[i % 4]
        setup = setups[i % 2]
        exit_r = exits[i % 3]

        # Vary results: some winners, some losers
        if i % 3 == 0:
            # Winner
            if direction == "long":
                t = _make_trade(
                    idx=i, direction="long", setup_type=setup,
                    intended_entry=60000, actual_entry=60010,
                    stop_loss=59800, take_profit=60400,
                    intended_exit=60400, actual_exit=60395,
                    exit_reason="tp", qty=0.1,
                    volatility_regime=regime, atr_at_entry=100 + i * 10,
                    confidence=0.3 + (i % 5) * 0.15,
                    holding_bars=10 + i * 5,
                    entry_timestamp=1_700_000_000_000 + i * 300_000 * 50,
                )
            else:
                t = _make_trade(
                    idx=i, direction="short", setup_type=setup,
                    intended_entry=60000, actual_entry=59990,
                    stop_loss=60200, take_profit=59600,
                    intended_exit=59600, actual_exit=59605,
                    exit_reason="tp", qty=0.1,
                    volatility_regime=regime, atr_at_entry=100 + i * 10,
                    confidence=0.3 + (i % 5) * 0.15,
                    holding_bars=10 + i * 5,
                    entry_timestamp=1_700_000_000_000 + i * 300_000 * 50,
                )
        else:
            # Loser
            if direction == "long":
                t = _make_trade(
                    idx=i, direction="long", setup_type=setup,
                    intended_entry=60000, actual_entry=60010,
                    stop_loss=59800, take_profit=60400,
                    intended_exit=59800, actual_exit=59780,
                    exit_reason=exit_r, qty=0.1,
                    volatility_regime=regime, atr_at_entry=100 + i * 10,
                    confidence=0.3 + (i % 5) * 0.15,
                    holding_bars=10 + i * 5,
                    mae=-25.0, mfe=5.0,
                    entry_timestamp=1_700_000_000_000 + i * 300_000 * 50,
                )
            else:
                t = _make_trade(
                    idx=i, direction="short", setup_type=setup,
                    intended_entry=60000, actual_entry=59990,
                    stop_loss=60200, take_profit=59600,
                    intended_exit=60200, actual_exit=60220,
                    exit_reason=exit_r, qty=0.1,
                    volatility_regime=regime, atr_at_entry=100 + i * 10,
                    confidence=0.3 + (i % 5) * 0.15,
                    holding_bars=10 + i * 5,
                    mae=-25.0, mfe=5.0,
                    entry_timestamp=1_700_000_000_000 + i * 300_000 * 50,
                )

        trades.append(t)

    return trades


# ═══════════════════════════════════════════
#  TEST: PnL DECOMPOSITION
# ═══════════════════════════════════════════

class TestPnLDecomposition:
    """Test PnL waterfall decomposition."""

    def test_decompose_winning_long_reconciles(self):
        """Ideal − entry_slip − exit_slip − fees − funding = net_pnl."""
        t = make_winning_long()
        d = PnLDecomposer.decompose_trade(t)

        reconstructed = d.ideal_pnl - d.entry_slippage - d.exit_slippage - d.fees - d.funding
        assert abs(reconstructed - t.net_pnl) < 0.01, (
            f"Reconciliation failed: {reconstructed:.4f} != {t.net_pnl:.4f}"
        )

    def test_decompose_losing_long_reconciles(self):
        t = make_losing_long()
        d = PnLDecomposer.decompose_trade(t)
        reconstructed = d.ideal_pnl - d.entry_slippage - d.exit_slippage - d.fees - d.funding
        assert abs(reconstructed - t.net_pnl) < 0.01

    def test_decompose_winning_short_reconciles(self):
        t = make_winning_short()
        d = PnLDecomposer.decompose_trade(t)
        reconstructed = d.ideal_pnl - d.entry_slippage - d.exit_slippage - d.fees - d.funding
        assert abs(reconstructed - t.net_pnl) < 0.01

    def test_decompose_losing_short_reconciles(self):
        t = make_losing_short()
        d = PnLDecomposer.decompose_trade(t)
        reconstructed = d.ideal_pnl - d.entry_slippage - d.exit_slippage - d.fees - d.funding
        assert abs(reconstructed - t.net_pnl) < 0.01

    def test_decompose_timeout_reconciles(self):
        t = make_timeout_long()
        d = PnLDecomposer.decompose_trade(t)
        # For timeout, we back-calculate reference exit
        reconstructed = d.ideal_pnl - d.entry_slippage - d.exit_slippage - d.fees - d.funding
        # Slightly larger tolerance for timeout (back-calculation rounding)
        assert abs(reconstructed - t.net_pnl) < 0.05, (
            f"Timeout recon: {reconstructed:.4f} != {t.net_pnl:.4f}, error={d.recon_error:.6f}"
        )

    def test_entry_slippage_positive_for_long(self):
        """Long: actual_entry > intended_entry → positive entry slippage cost."""
        t = make_winning_long()
        d = PnLDecomposer.decompose_trade(t)
        assert d.entry_slippage > 0, "Entry slip should be positive for long buy above intended"

    def test_exit_slippage_positive_for_sl(self):
        """SL exit: actual_exit worse than SL → positive exit slippage cost."""
        t = make_losing_long()
        d = PnLDecomposer.decompose_trade(t)
        assert d.exit_slippage > 0, "Exit slip should be positive for SL fill below SL level"

    def test_winning_trade_has_positive_ideal_pnl(self):
        t = make_winning_long()
        d = PnLDecomposer.decompose_trade(t)
        assert d.ideal_pnl > 0

    def test_total_cost_decomposition(self):
        t = make_winning_long()
        d = PnLDecomposer.decompose_trade(t)
        expected = d.entry_slippage + d.exit_slippage + d.fees + d.funding
        assert abs(d.total_cost - expected) < 0.001

    def test_bps_computation(self):
        t = make_winning_long()
        d = PnLDecomposer.decompose_trade(t)
        assert d.entry_slip_bps != 0
        assert d.fees_bps > 0


class TestCostAttribution:
    """Test aggregate cost attribution."""

    def test_decompose_all_basic(self):
        trades = [make_winning_long(), make_losing_long()]
        attr = PnLDecomposer.decompose_all(trades)
        assert attr.total_trades == 2
        assert len(attr.decompositions) == 2

    def test_cost_shares_sum_to_100(self):
        trades = make_many_trades(20)
        attr = PnLDecomposer.decompose_all(trades)
        total = attr.entry_slip_pct + attr.exit_slip_pct + attr.fees_pct + attr.funding_pct
        assert abs(total - 100) < 0.1, f"Cost shares sum={total}%"

    def test_kill_ranking_ordered(self):
        trades = make_many_trades(20)
        attr = PnLDecomposer.decompose_all(trades)
        assert len(attr.kill_ranking) == 4
        # Verify descending order
        values = [x[1] for x in attr.kill_ranking]
        assert values == sorted(values, reverse=True)

    def test_reconciliation_tight(self):
        trades = make_many_trades(20)
        attr = PnLDecomposer.decompose_all(trades)
        assert attr.max_recon_error < 0.1, f"Max recon error: {attr.max_recon_error}"

    def test_empty_trades(self):
        attr = PnLDecomposer.decompose_all([])
        assert attr.total_trades == 0
        assert attr.total_cost == 0


# ═══════════════════════════════════════════
#  TEST: SEGMENTATION
# ═══════════════════════════════════════════

class TestSegmentation:
    """Test multi-dimensional segmentation."""

    def test_all_10_dimensions(self):
        trades = make_many_trades(30)
        result = SegmentationEngine.analyze(trades)
        assert len(result.dimensions) == 10
        expected_dims = {
            "setup_type", "direction", "volatility_regime", "exit_reason",
            "session", "day_of_week", "holding_bucket", "volatility_bucket",
            "rr_bucket", "mae_mfe_bucket",
        }
        assert set(result.dimensions.keys()) == expected_dims

    def test_direction_has_long_and_short(self):
        trades = make_many_trades(30)
        result = SegmentationEngine.analyze(trades)
        dir_segments = result.dimensions["direction"]
        names = {s.segment_name for s in dir_segments}
        assert "long" in names
        assert "short" in names

    def test_segment_trades_sum(self):
        """Trade counts across segments should sum to total."""
        trades = make_many_trades(30)
        result = SegmentationEngine.analyze(trades)
        for dim_name, segments in result.dimensions.items():
            total = sum(s.trades for s in segments)
            assert total == 30, f"{dim_name}: {total} != 30"

    def test_verdict_assigned(self):
        trades = make_many_trades(30)
        result = SegmentationEngine.analyze(trades)
        for segments in result.dimensions.values():
            for s in segments:
                assert s.verdict in ("KEEP", "REVIEW", "KILL")

    def test_segment_stats_computed(self):
        trades = make_many_trades(30)
        result = SegmentationEngine.analyze(trades)
        for segments in result.dimensions.values():
            for s in segments:
                if s.trades > 0:
                    assert s.total_pnl != 0 or s.expectancy == 0

    def test_report_generated(self):
        trades = make_many_trades(30)
        result = SegmentationEngine.analyze(trades)
        assert len(result.report) > 100
        assert "SEGMENTATION" in result.report

    def test_empty_trades(self):
        result = SegmentationEngine.analyze([])
        assert result.total_segments == 0

    def test_session_bucketing(self):
        """Verify sessions map to correct buckets."""
        # Timestamp at hour 10 UTC → europe
        import datetime
        dt = datetime.datetime(2024, 1, 15, 10, 0, tzinfo=datetime.timezone.utc)
        ts = int(dt.timestamp() * 1000)
        t = _make_trade(idx=0, entry_timestamp=ts)
        seg = SegmentationEngine._get_session(t)
        assert seg == "europe"

    def test_holding_bucket(self):
        short_hold = _make_trade(idx=0, holding_bars=5)
        assert "short" in SegmentationEngine._get_holding_bucket(short_hold).lower()
        long_hold = _make_trade(idx=0, holding_bars=200)
        assert "long" in SegmentationEngine._get_holding_bucket(long_hold).lower()


# ═══════════════════════════════════════════
#  TEST: SURVIVAL FILTERING
# ═══════════════════════════════════════════

class TestSurvivalFilter:
    """Test survival filtering engine."""

    def test_10_filters_tested(self):
        trades = make_many_trades(30)
        result = SurvivalFilterEngine.analyze(trades)
        assert len(result.filters) == 10

    def test_filter_trades_count(self):
        """After filter, trades_after <= trades_before."""
        trades = make_many_trades(30)
        result = SurvivalFilterEngine.analyze(trades)
        for f in result.filters:
            assert f.trades_after <= f.trades_before

    def test_filter_verdict_assigned(self):
        trades = make_many_trades(30)
        result = SurvivalFilterEngine.analyze(trades)
        for f in result.filters:
            assert f.verdict in ("IMPROVES", "DEGRADES", "NEUTRAL")

    def test_combined_filter_stacks(self):
        """Combined filter should keep only trades passing ALL improving filters."""
        trades = make_many_trades(30)
        result = SurvivalFilterEngine.analyze(trades)
        if result.combined:
            assert result.combined.trades_after <= result.combined.trades_before
            assert result.combined.filter_name == "combined_best"

    def test_filter_expectancy_delta(self):
        trades = make_many_trades(30)
        result = SurvivalFilterEngine.analyze(trades)
        for f in result.filters:
            expected_delta = f.expectancy_after - f.expectancy_before
            assert abs(f.expectancy_delta - expected_delta) < 0.01

    def test_empty_trades(self):
        result = SurvivalFilterEngine.analyze([])
        assert len(result.filters) == 0

    def test_report_generated(self):
        trades = make_many_trades(30)
        result = SurvivalFilterEngine.analyze(trades)
        assert len(result.report) > 50
        assert "SURVIVAL" in result.report

    def test_surviving_indices_valid(self):
        trades = make_many_trades(30)
        result = SurvivalFilterEngine.analyze(trades)
        for f in result.filters:
            for idx in f.surviving_indices:
                assert 0 <= idx < len(trades)


# ═══════════════════════════════════════════
#  TEST: FALSE EDGE DETECTION
# ═══════════════════════════════════════════

class TestFalseEdgeDetector:
    """Test root cause scoring."""

    def test_scores_between_0_and_100(self):
        trades = make_many_trades(30)
        attr = PnLDecomposer.decompose_all(trades)
        fe = FalseEdgeDetector.analyze(trades, attr)
        for field_name in ("look_ahead", "fill_overestimation", "spread_underestimation",
                           "overtrading", "bad_sl_tp", "wrong_regime", "weak_alpha"):
            val = getattr(fe, field_name)
            assert 0 <= val <= 100, f"{field_name}={val}"

    def test_primary_cause_identified(self):
        trades = make_many_trades(30)
        attr = PnLDecomposer.decompose_all(trades)
        fe = FalseEdgeDetector.analyze(trades, attr)
        assert fe.primary_cause in (
            "look_ahead", "fill_overestimation", "spread_underestimation",
            "overtrading", "bad_sl_tp", "wrong_regime", "weak_alpha",
        )

    def test_no_look_ahead_for_normal_trades(self):
        trades = make_many_trades(30)
        attr = PnLDecomposer.decompose_all(trades)
        fe = FalseEdgeDetector.analyze(trades, attr)
        assert fe.look_ahead == 0, "No look-ahead expected for normal trades"

    def test_naive_vs_realistic_comparison(self):
        trades = make_many_trades(30)
        attr = PnLDecomposer.decompose_all(trades)
        naive = DetailedMetrics(expectancy=50.0, win_rate=55.0, profit_factor=1.5)
        realistic = DetailedMetrics(expectancy=-20.0, win_rate=35.0, profit_factor=0.5)
        fe = FalseEdgeDetector.analyze(trades, attr, realistic, naive)
        # Fill overestimation should be high since naive is profitable and realistic isn't
        assert fe.fill_overestimation > 50

    def test_details_populated(self):
        trades = make_many_trades(30)
        attr = PnLDecomposer.decompose_all(trades)
        fe = FalseEdgeDetector.analyze(trades, attr)
        assert len(fe.details) >= 5
        assert "look_ahead" in fe.details

    def test_empty_trades(self):
        attr = PnLDecomposer.decompose_all([])
        fe = FalseEdgeDetector.analyze([], attr)
        assert fe.total_score == 0


# ═══════════════════════════════════════════
#  TEST: DECISION MATRIX
# ═══════════════════════════════════════════

class TestDecisionMatrix:
    """Test KILL / SALVAGE / RESEARCH / PROMISING logic."""

    def test_kill_when_no_alpha(self):
        attr = CostAttribution(
            total_ideal_pnl=-1000,
            total_cost=500, total_net_pnl=-1500,
            cost_to_alpha_ratio=0, alpha_survival_pct=0,
        )
        fe = FalseEdgeScore(weak_alpha=80)
        v = DecisionMatrix.evaluate(attr, fe)
        assert v.verdict == "KILL"

    def test_kill_when_costs_dominate(self):
        attr = CostAttribution(
            total_ideal_pnl=100,
            total_cost=300, total_net_pnl=-200,
            cost_to_alpha_ratio=3.0, alpha_survival_pct=-200,
        )
        fe = FalseEdgeScore(spread_underestimation=80)
        v = DecisionMatrix.evaluate(attr, fe)
        assert v.verdict == "KILL"

    def test_research_when_alpha_with_survivors(self):
        attr = CostAttribution(
            total_ideal_pnl=500,
            total_cost=600, total_net_pnl=-100,
            cost_to_alpha_ratio=1.2, alpha_survival_pct=-20,
        )
        fe = FalseEdgeScore(spread_underestimation=40)
        v = DecisionMatrix.evaluate(attr, fe, segment_survivors=3, filter_survivors=2)
        assert v.verdict == "RESEARCH"

    def test_salvage_when_naive_profitable_with_survivors(self):
        attr = CostAttribution(
            total_ideal_pnl=500,
            total_cost=600, total_net_pnl=-100,
            cost_to_alpha_ratio=1.2, alpha_survival_pct=-20,
        )
        fe = FalseEdgeScore(spread_underestimation=40)
        naive = DetailedMetrics(expectancy=10.0)
        v = DecisionMatrix.evaluate(attr, fe, naive_metrics=naive, segment_survivors=1)
        assert v.verdict in ("SALVAGE", "RESEARCH")

    def test_promising_when_realistic_profitable(self):
        attr = CostAttribution(
            total_ideal_pnl=1000,
            total_cost=200, total_net_pnl=800,
            cost_to_alpha_ratio=0.2, alpha_survival_pct=80,
        )
        fe = FalseEdgeScore()
        realistic = DetailedMetrics(expectancy=10.0)
        v = DecisionMatrix.evaluate(attr, fe, realistic_metrics=realistic)
        assert v.verdict == "PROMISING"

    def test_action_plan_not_empty(self):
        attr = CostAttribution()
        fe = FalseEdgeScore()
        v = DecisionMatrix.evaluate(attr, fe)
        assert len(v.action_plan) > 0

    def test_verdict_has_reasons(self):
        attr = CostAttribution(total_ideal_pnl=-100, cost_to_alpha_ratio=0)
        fe = FalseEdgeScore(weak_alpha=80)
        v = DecisionMatrix.evaluate(attr, fe)
        assert len(v.reasons) > 0


# ═══════════════════════════════════════════
#  TEST: FULL AUTOPSY PIPELINE
# ═══════════════════════════════════════════

class TestFullAutopsy:
    """Test the complete autopsy pipeline."""

    def test_run_autopsy_basic(self):
        trades = make_many_trades(30)
        result = run_autopsy(trades)
        assert isinstance(result, AutopsyResult)
        assert result.cost_attribution.total_trades == 30
        assert result.decision.verdict in ("KILL", "SALVAGE", "RESEARCH", "PROMISING")

    def test_run_autopsy_with_metrics(self):
        trades = make_many_trades(30)
        naive = DetailedMetrics(
            expectancy=20.0, win_rate=55.0, profit_factor=1.5,
            total_trades=30,
        )
        realistic = DetailedMetrics(
            expectancy=-10.0, win_rate=35.0, profit_factor=0.5,
            total_trades=30,
        )
        result = run_autopsy(
            trades, realistic_metrics=realistic,
            naive_metrics=naive, segment_survivors=2, filter_survivors=1,
        )
        assert result.false_edge.fill_overestimation > 0

    def test_report_format(self):
        trades = make_many_trades(30)
        result = run_autopsy(trades)
        report = result.report

        assert "EDGE AUTOPSY" in report
        assert "PnL DECOMPOSITION" in report
        assert "FALSE EDGE" in report
        assert "ACTION PLAN" in report
        assert "VERDICT" in report

    def test_report_has_waterfall_table(self):
        trades = make_many_trades(30)
        result = run_autopsy(trades)
        assert "Ideal Alpha" in result.report
        assert "Entry Slippage" in result.report
        assert "Exit Slippage" in result.report
        assert "Fees" in result.report
        assert "Funding" in result.report
        assert "NET PnL" in result.report

    def test_report_has_cost_to_alpha(self):
        trades = make_many_trades(30)
        result = run_autopsy(trades)
        assert "Cost-to-Alpha" in result.report

    def test_empty_trades_no_crash(self):
        result = run_autopsy([])
        assert result.decision.verdict == "KILL"
        assert result.cost_attribution.total_trades == 0


# ═══════════════════════════════════════════
#  TEST: EDGE CASES
# ═══════════════════════════════════════════

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_single_trade(self):
        trades = [make_winning_long()]
        attr = PnLDecomposer.decompose_all(trades)
        assert attr.total_trades == 1
        assert attr.total_net_pnl == trades[0].net_pnl

    def test_all_winners(self):
        trades = [make_winning_long() for _ in range(10)]
        attr = PnLDecomposer.decompose_all(trades)
        assert attr.total_net_pnl > 0
        assert attr.total_ideal_pnl > 0

    def test_all_losers(self):
        trades = [make_losing_long() for _ in range(10)]
        attr = PnLDecomposer.decompose_all(trades)
        assert attr.total_net_pnl < 0

    def test_zero_qty_trade(self):
        """Trade with zero qty shouldn't crash."""
        t = _make_trade(idx=0, qty=0, entry_fee=0, exit_fee=0, funding_cost=0)
        d = PnLDecomposer.decompose_trade(t)
        assert d.net_pnl == 0

    def test_mixed_directions(self):
        """Mix of long and short trades."""
        trades = [make_winning_long(), make_winning_short(),
                  make_losing_long(), make_losing_short()]
        attr = PnLDecomposer.decompose_all(trades)
        # All should reconcile
        for d in attr.decompositions:
            assert d.recon_error < 0.1

    def test_segmentation_with_few_trades(self):
        trades = [make_winning_long(), make_losing_long()]
        result = SegmentationEngine.analyze(trades)
        assert result.total_segments > 0

    def test_filter_all_removed(self):
        """Filter that removes everything."""
        trades = make_many_trades(10)
        fr = SurvivalFilterEngine._test_filter(
            trades, "test", "Remove all", lambda t: False,
        )
        assert fr.trades_after == 0
        assert fr.verdict == "NEUTRAL"
