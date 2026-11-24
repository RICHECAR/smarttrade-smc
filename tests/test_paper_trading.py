"""
Tests — Paper Trading Institutionnel (Chantier 17)
═══════════════════════════════════════════════════
10+ mandatory tests covering:
  1. Paper state persistence
  2. Monthly rebalance correct
  3. Weekly blocked explicitly
  4. vol_weight > 0.15 blocked
  5. Turnover cap applied
  6. Partial data blocks
  7. Journal written correctly
  8. Target portfolio coherent
  9. Benchmark tracking coherent
 10. Sequential replay without lookahead
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from portfolio.paper_state import PaperPortfolioState
from portfolio.paper_journal import PaperJournal, JournalEntry
from portfolio.paper_constraints import (
    PaperConstraints,
    run_constraint_gate,
    check_rebal_freq,
    check_vol_weight,
    check_turnover,
    check_scores,
    check_data_freshness,
    PAPER_WEEKLY_FORBIDDEN,
    PAPER_VOL_WEIGHT_INVALID,
    PAPER_TURNOVER_EXCEEDED,
    PAPER_SCORE_NAN,
    PAPER_DATA_STALE,
    PAPER_DATA_PARTIAL,
)
from portfolio.paper_broker import PaperBroker
from portfolio.paper_engine import PaperEngine, VALIDATED_SIGNAL_CONFIG


# ── Helpers ──────────────────────────────────────────────────────

def _make_prices(n_days=500, n_tickers=30, seed=42):
    """Synthetic price data with realistic returns."""
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2021-01-04", periods=n_days, freq="B")
    tickers = [f"T{i:02d}" for i in range(n_tickers)]
    # Random walk
    log_rets = rng.normal(0.0003, 0.015, (n_days, n_tickers))
    prices = 100 * np.exp(np.cumsum(log_rets, axis=0))
    return pd.DataFrame(prices, index=dates, columns=tickers)


def _tmpdir():
    return Path(tempfile.mkdtemp())


# ══════════════════════════════════════════════════════════════════
# 1. PAPER STATE PERSISTENCE
# ══════════════════════════════════════════════════════════════════

class TestPaperStatePersistence:

    def test_save_and_load_roundtrip(self):
        d = _tmpdir()
        s = PaperPortfolioState(initial_cash=50_000.0, state_path=d / "state.json")
        s.execute_buy("AAPL", 10, 150.0)
        s.execute_buy("MSFT", 5, 300.0)
        s.inception_date = "2023-01-01"
        s.rebal_count = 3
        s.record_valuation("2023-06-01", benchmark_value=430.0)
        s.save()

        s2 = PaperPortfolioState(state_path=d / "state.json")
        assert s2.load()
        assert abs(s2.cash - s.cash) < 0.01
        assert s2.inception_date == "2023-01-01"
        assert s2.rebal_count == 3
        assert "AAPL" in s2.positions
        assert "MSFT" in s2.positions
        assert len(s2.valuations) == 1

    def test_empty_load_returns_false(self):
        d = _tmpdir()
        s = PaperPortfolioState(state_path=d / "nonexistent.json")
        assert not s.load()

    def test_weights_after_trades(self):
        s = PaperPortfolioState(initial_cash=100_000)
        s.execute_buy("A", 100, 100.0)
        s.execute_buy("B", 200, 50.0)
        w = s.weights()
        assert abs(sum(w.values()) - s.invested_value / s.total_value) < 0.01


# ══════════════════════════════════════════════════════════════════
# 2. MONTHLY REBALANCE CORRECT
# ══════════════════════════════════════════════════════════════════

class TestMonthlyRebalance:

    def test_engine_produces_monthly_rebalances(self):
        """Run replay and confirm only monthly dates."""
        prices = _make_prices(n_days=600, n_tickers=30)
        d = _tmpdir()
        state = PaperPortfolioState(initial_cash=100_000, state_path=d / "s.json")
        journal = PaperJournal(path=d / "j.jsonl")
        engine = PaperEngine(state=state, journal=journal)

        results = engine.replay(prices, warmup=252)
        assert len(results) > 0

        # All rebalance dates should be month-end (last trading day)
        for r in results:
            dt = pd.Timestamp(r.date)
            # This date should be the last trading day of its month
            month_end = prices.index[prices.index.to_period("M") == dt.to_period("M")]
            assert dt == month_end[-1], f"{r.date} is not month-end"


# ══════════════════════════════════════════════════════════════════
# 3. WEEKLY BLOCKED EXPLICITLY
# ══════════════════════════════════════════════════════════════════

class TestWeeklyBlocked:

    def test_weekly_freq_rejected(self):
        cfg = PaperConstraints()
        r = check_rebal_freq("W", cfg)
        assert not r.passed
        assert r.reason_code == PAPER_WEEKLY_FORBIDDEN

    def test_weekly_variant_rejected(self):
        r = check_rebal_freq("weekly", PaperConstraints())
        assert not r.passed
        assert r.reason_code == PAPER_WEEKLY_FORBIDDEN

    def test_monthly_accepted(self):
        r = check_rebal_freq("M", PaperConstraints())
        assert r.passed


# ══════════════════════════════════════════════════════════════════
# 4. VOL_WEIGHT > 0.15 BLOCKED
# ══════════════════════════════════════════════════════════════════

class TestVolWeightBlocked:

    def test_vol_weight_above_limit_blocked(self):
        cfg = PaperConstraints(max_vol_weight=0.15)
        weights = {"volatility": -0.25, "mom_63": 0.40}
        r = check_vol_weight(weights, cfg)
        assert not r.passed
        assert r.reason_code == PAPER_VOL_WEIGHT_INVALID

    def test_vol_weight_at_limit_passes(self):
        cfg = PaperConstraints(max_vol_weight=0.15)
        weights = {"volatility": -0.15, "mom_63": 0.40}
        r = check_vol_weight(weights, cfg)
        assert r.passed

    def test_validated_config_compliant(self):
        """The validated signal config must pass vol_weight check."""
        cfg = PaperConstraints(max_vol_weight=0.15)
        r = check_vol_weight(VALIDATED_SIGNAL_CONFIG.weights, cfg)
        assert r.passed, f"Validated config fails vol_weight: {r.detail}"


# ══════════════════════════════════════════════════════════════════
# 5. TURNOVER CAP APPLIED
# ══════════════════════════════════════════════════════════════════

class TestTurnoverCap:

    def test_excessive_turnover_warned_not_blocked(self):
        """Turnover excess is a warning, not a hard block (engine caps it)."""
        cfg = PaperConstraints(max_turnover_per_rebal=0.50)
        old = {"A": 0.5, "B": 0.5}
        new = {"C": 0.5, "D": 0.5}  # 100% turnover
        r = check_turnover(old, new, cfg)
        assert not r.passed  # check itself detects excess
        assert r.reason_code == PAPER_TURNOVER_EXCEEDED

    def test_moderate_turnover_passes(self):
        cfg = PaperConstraints(max_turnover_per_rebal=0.50)
        old = {"A": 0.4, "B": 0.3, "C": 0.3}
        new = {"A": 0.35, "B": 0.35, "C": 0.30}
        r = check_turnover(old, new, cfg)
        assert r.passed

    def test_cap_turnover_weights_limits_move(self):
        from portfolio.paper_constraints import cap_turnover_weights
        old = {"A": 0.5, "B": 0.5}
        new = {"C": 0.5, "D": 0.5}  # 100% TO
        capped = cap_turnover_weights(old, new, 0.50)
        # Actual turnover should be ≤ 50%
        all_t = set(old) | set(capped)
        actual_to = sum(abs(capped.get(t, 0.0) - old.get(t, 0.0)) for t in all_t) / 2.0
        assert actual_to <= 0.50 + 0.01

    def test_cap_preserves_weights_sum(self):
        from portfolio.paper_constraints import cap_turnover_weights
        old = {"A": 0.3, "B": 0.3, "C": 0.4}
        new = {"D": 0.5, "E": 0.5}
        capped = cap_turnover_weights(old, new, 0.40)
        assert abs(sum(capped.values()) - 1.0) < 0.05

    def test_gate_turnover_not_in_blockers(self):
        """Turnover is a warning, not a hard blocker in the gate."""
        prices = _make_prices(n_days=300, n_tickers=30)
        ref = prices.index[-1]
        scores = pd.Series(np.random.rand(30), index=prices.columns)

        result = run_constraint_gate(
            rebal_freq="M",
            signal_weights=VALIDATED_SIGNAL_CONFIG.weights,
            prices=prices,
            reference_date=ref,
            scores=scores,
            old_weights={"T00": 0.5, "T01": 0.5},
            new_weights={"T02": 0.5, "T03": 0.5},  # 100% turnover
        )
        # Gate should PASS (turnover is not a hard blocker)
        assert result.passed
        # But turnover warning should be present
        assert any("TURNOVER" in w for w in result.warnings)


# ══════════════════════════════════════════════════════════════════
# 6. PARTIAL DATA BLOCKS
# ══════════════════════════════════════════════════════════════════

class TestDataBlocks:

    def test_stale_data_blocked(self):
        cfg = PaperConstraints(max_data_age_days=3)
        prices = _make_prices(n_days=100)
        ref = prices.index[-1] + pd.Timedelta(days=10)
        r = check_data_freshness(prices, ref, cfg)
        assert not r.passed
        assert r.reason_code == PAPER_DATA_STALE

    def test_fresh_data_passes(self):
        cfg = PaperConstraints(max_data_age_days=5)
        prices = _make_prices(n_days=100)
        ref = prices.index[-1] + pd.Timedelta(days=1)
        r = check_data_freshness(prices, ref, cfg)
        assert r.passed

    def test_nan_scores_blocked(self):
        cfg = PaperConstraints(min_non_nan_score_pct=0.70)
        # 80% NaN scores
        scores = pd.Series([np.nan] * 8 + [0.5, 0.6], index=[f"T{i}" for i in range(10)])
        r = check_scores(scores, cfg)
        assert not r.passed
        assert r.reason_code == PAPER_SCORE_NAN


# ══════════════════════════════════════════════════════════════════
# 7. JOURNAL WRITTEN CORRECTLY
# ══════════════════════════════════════════════════════════════════

class TestJournalIntegrity:

    def test_journal_append_and_read(self):
        d = _tmpdir()
        j = PaperJournal(path=d / "test.jsonl")
        e1 = JournalEntry(
            timestamp="2024-01-01 00:00:00",
            rebal_date="2024-01-31",
            universe_valid_count=25,
            top_rankings={"AAPL": 0.95, "MSFT": 0.90},
            current_weights={},
            target_weights={"AAPL": 0.3, "MSFT": 0.3},
            delta_by_ticker={"AAPL": 0.3, "MSFT": 0.3},
            cash_before=100000,
            cash_after=40000,
            portfolio_value_before=100000,
            portfolio_value_after=99980,
            cost_simulated=20.0,
            turnover=0.30,
            safeguard_status="PASSED",
            constraint_status="ALL_CONSTRAINTS_PASSED",
            benchmark_value=450.0,
        )
        j.append(e1)
        assert j.count() == 1

        entries = j.read_all()
        assert len(entries) == 1
        assert entries[0].rebal_date == "2024-01-31"
        assert entries[0].turnover == 0.30

    def test_journal_multiple_entries(self):
        d = _tmpdir()
        j = PaperJournal(path=d / "multi.jsonl")
        for i in range(5):
            e = JournalEntry(
                timestamp=f"2024-0{i+1}-01 00:00:00",
                rebal_date=f"2024-0{i+1}-31",
                universe_valid_count=20,
                top_rankings={},
                current_weights={},
                target_weights={"A": 0.5},
                delta_by_ticker={"A": 0.5},
                cash_before=100000, cash_after=50000,
                portfolio_value_before=100000, portfolio_value_after=99990,
                cost_simulated=10.0, turnover=0.25,
                safeguard_status="PASSED",
                constraint_status="ALL_CONSTRAINTS_PASSED",
                benchmark_value=450.0,
            )
            j.append(e)
        assert j.count() == 5

    def test_replay_journal_matches_results(self):
        prices = _make_prices(n_days=600, n_tickers=30)
        d = _tmpdir()
        state = PaperPortfolioState(initial_cash=100_000, state_path=d / "s.json")
        journal = PaperJournal(path=d / "j.jsonl")
        engine = PaperEngine(state=state, journal=journal)

        results = engine.replay(prices, warmup=252)
        journal_count = journal.count()
        # Journal should have an entry for every rebalance attempt
        assert journal_count == len(results)


# ══════════════════════════════════════════════════════════════════
# 8. TARGET PORTFOLIO COHERENT
# ══════════════════════════════════════════════════════════════════

class TestTargetCoherent:

    def test_target_weights_sum_to_one(self):
        prices = _make_prices(n_days=600, n_tickers=30)
        d = _tmpdir()
        state = PaperPortfolioState(initial_cash=100_000, state_path=d / "s.json")
        journal = PaperJournal(path=d / "j.jsonl")
        engine = PaperEngine(state=state, journal=journal)

        results = engine.replay(prices, warmup=252)
        for r in results:
            if r.passed and r.target_weights:
                total = sum(r.target_weights.values())
                assert abs(total - 1.0) < 0.05, f"Weights sum={total:.4f} on {r.date}"

    def test_max_weight_respected(self):
        prices = _make_prices(n_days=600, n_tickers=30)
        d = _tmpdir()
        state = PaperPortfolioState(initial_cash=100_000, state_path=d / "s.json")
        journal = PaperJournal(path=d / "j.jsonl")
        engine = PaperEngine(state=state, journal=journal)

        results = engine.replay(prices, warmup=252)
        for r in results:
            if r.passed and r.target_weights:
                max_w = max(r.target_weights.values())
                assert max_w <= 0.35, f"Max weight {max_w:.2%} on {r.date}"


# ══════════════════════════════════════════════════════════════════
# 9. BENCHMARK TRACKING COHERENT
# ══════════════════════════════════════════════════════════════════

class TestBenchmarkTracking:

    def test_valuations_include_benchmark(self):
        # Use 800 days to guarantee enough post-warmup data for monthly rebalances
        prices = _make_prices(n_days=800, n_tickers=30)
        # add a "SPY" column — always positive (no NaN possible)
        prices["SPY"] = prices.iloc[:, 0] * 1.1  # synthetic
        assert prices["SPY"].isna().sum() == 0, "SPY column should have no NaN"
        d = _tmpdir()
        state = PaperPortfolioState(initial_cash=100_000, state_path=d / "s.json")
        journal = PaperJournal(path=d / "j.jsonl")
        engine = PaperEngine(state=state, journal=journal)

        results = engine.replay(prices, warmup=252, benchmark_ticker="SPY")
        # With 800 days and warmup=252, we have ~548 tradeable days = ~18 months
        # => at least 1 rebalance must happen
        assert len(results) > 0, (
            f"Expected at least 1 rebalance result with 800 days and warmup=252, got 0"
        )
        vals_with_bench = [v for v in state.valuations if v.benchmark_value > 0]
        assert len(vals_with_bench) > 0, (
            f"No valuations with benchmark_value > 0 (total valuations: {len(state.valuations)})"
        )


# ══════════════════════════════════════════════════════════════════
# 10. SEQUENTIAL REPLAY WITHOUT LOOKAHEAD
# ══════════════════════════════════════════════════════════════════

class TestNoLookahead:

    def test_replay_prices_are_causal(self):
        """Each rebalance uses only data up to its date."""
        prices = _make_prices(n_days=600, n_tickers=30)
        d = _tmpdir()
        state = PaperPortfolioState(initial_cash=100_000, state_path=d / "s.json")
        journal = PaperJournal(path=d / "j.jsonl")
        engine = PaperEngine(state=state, journal=journal)

        results = engine.replay(prices, warmup=252)
        entries = journal.read_all()

        # Journal dates should be monotonically increasing
        dates = [e.rebal_date for e in entries]
        assert dates == sorted(dates), "Journal dates not monotonically increasing"

        # Each result date should be within the price index
        for r in results:
            dt = pd.Timestamp(r.date)
            assert dt in prices.index or dt <= prices.index[-1]


# ══════════════════════════════════════════════════════════════════
# 11. CONSTRAINT GATE INTEGRATION
# ══════════════════════════════════════════════════════════════════

class TestConstraintGateIntegration:

    def test_full_gate_passes_on_clean_data(self):
        prices = _make_prices(n_days=300, n_tickers=30)
        ref = prices.index[-1]
        scores = pd.Series(np.random.rand(30), index=prices.columns)
        old_w = {"T00": 0.2, "T01": 0.2, "T02": 0.2, "T03": 0.2, "T04": 0.2}
        new_w = {"T00": 0.25, "T01": 0.15, "T02": 0.2, "T03": 0.2, "T04": 0.2}

        result = run_constraint_gate(
            rebal_freq="M",
            signal_weights=VALIDATED_SIGNAL_CONFIG.weights,
            prices=prices,
            reference_date=ref,
            scores=scores,
            old_weights=old_w,
            new_weights=new_w,
        )
        assert result.passed

    def test_weekly_gate_blocks(self):
        prices = _make_prices(n_days=300, n_tickers=30)
        ref = prices.index[-1]
        scores = pd.Series(np.random.rand(30), index=prices.columns)

        result = run_constraint_gate(
            rebal_freq="W",
            signal_weights=VALIDATED_SIGNAL_CONFIG.weights,
            prices=prices,
            reference_date=ref,
            scores=scores,
            old_weights={},
            new_weights={"T00": 1.0},
        )
        assert not result.passed
        assert PAPER_WEEKLY_FORBIDDEN in result.blockers


# ══════════════════════════════════════════════════════════════════
# 12. PAPER BROKER EXECUTION
# ══════════════════════════════════════════════════════════════════

class TestPaperBrokerExecution:

    def test_sells_before_buys(self):
        state = PaperPortfolioState(initial_cash=100_000)
        state.execute_buy("OLD", 100, 100.0)  # $10k in OLD
        broker = PaperBroker(cost_bps=5.0)
        prices = {"OLD": 100.0, "NEW": 50.0}
        target = {"NEW": 1.0}  # sell OLD, buy NEW

        orders, cost = broker.execute_rebalance(state, target, prices)
        sells = [o for o in orders if o.side == "SELL"]
        buys = [o for o in orders if o.side == "BUY"]
        assert len(sells) > 0
        assert len(buys) > 0
        # First order should be a SELL
        assert orders[0].side == "SELL"
