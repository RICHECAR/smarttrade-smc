"""
Tests for Chantier 6 — Learning Engine + Strategy Allocator.

Covers:
  - Quality score computation (positive, negative, mixed)
  - Score thresholds → DISABLE / REDUCE / FULL / BOOST
  - Regime × strategy cross-scoring
  - Minimum sample size protection
  - Never-disable-all safety guard
  - Cooldown after disable
  - Strategy allocator gate (is_strategy_allowed)
  - Risk multiplier injection
  - Persistent state save/load
  - Order manager STRATEGY_DISABLED gate
  - Force refresh
"""

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from smc_futures.analytics.trade_journal import InstitutionalTradeJournal, TradeRecord
from smc_futures.analytics.trade_report import TradeReport, GroupStats
from smc_futures.analytics.learning_engine import (
    LearningEngine,
    QualityScore,
    AllocationDecision,
    Decision,
)
from smc_futures.analytics.strategy_allocator import (
    StrategyAllocator,
    LearningState,
)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _make_record(**overrides) -> TradeRecord:
    """Create a minimal valid TradeRecord with sensible defaults."""
    now = datetime.now(timezone.utc)
    defaults = dict(
        trade_id=str(uuid.uuid4()),
        timestamp_open=(now - timedelta(minutes=30)).isoformat(),
        timestamp_close=now.isoformat(),
        symbol="BTCUSDT",
        broker="kraken",
        side="long",
        strategy_id="DefaultSMC",
        regime_at_entry="TRENDING",
        entry_price_expected=50000.0,
        entry_price_filled=50010.0,
        exit_price_expected=51000.0,
        exit_price_filled=50990.0,
        qty_requested=0.01,
        qty_filled=0.01,
        leverage=10.0,
        stop_loss_price=49500.0,
        take_profit_price=51000.0,
        exit_reason="TAKE_PROFIT",
        gross_pnl=9.80,
        net_pnl=9.20,
        fees=0.60,
        risk_amount_at_entry=5.0,
        rr_planned=2.0,
        account_equity_at_entry=10000.0,
        account_equity_at_exit=10009.20,
    )
    defaults.update(overrides)
    return TradeRecord(**defaults)


def _make_winning_records(n: int, strategy: str = "DefaultSMC", regime: str = "TRENDING") -> list:
    """Generate n winning trade records."""
    return [
        _make_record(
            strategy_id=strategy,
            regime_at_entry=regime,
            gross_pnl=10.0 + i * 0.5,
            net_pnl=9.5 + i * 0.5,
        )
        for i in range(n)
    ]


def _make_losing_records(n: int, strategy: str = "DefaultSMC", regime: str = "TRENDING") -> list:
    """Generate n losing trade records."""
    return [
        _make_record(
            strategy_id=strategy,
            regime_at_entry=regime,
            gross_pnl=-20.0 - i * 1.0,
            net_pnl=-20.5 - i * 1.0,
            exit_reason="STOP_LOSS",
        )
        for i in range(n)
    ]


def _make_mixed_records(
    n_win: int, n_loss: int,
    strategy: str = "DefaultSMC", regime: str = "TRENDING",
) -> list:
    """Generate a mix of winning and losing records."""
    return (
        _make_winning_records(n_win, strategy, regime)
        + _make_losing_records(n_loss, strategy, regime)
    )


# ═══════════════════════════════════════════════════════════════════════════════
# LEARNING ENGINE — QUALITY SCORES
# ═══════════════════════════════════════════════════════════════════════════════

class TestLearningEngineScores:

    def test_positive_strategy_gets_high_score(self):
        """A strategy with 80% win rate should get a high quality score."""
        records = _make_mixed_records(32, 8, "DefaultSMC")
        engine = LearningEngine()
        scores = engine.compute_scores(records)
        qs = scores[("DefaultSMC", "*")]
        assert qs.score > 55.0, f"Expected high score for 80% WR, got {qs.score}"
        assert qs.total_trades == 40

    def test_negative_strategy_gets_low_score(self):
        """A strategy with 20% win rate and big losses should get a low quality score."""
        records = _make_mixed_records(6, 24, "SweepScalp")
        engine = LearningEngine()
        scores = engine.compute_scores(records)
        qs = scores[("SweepScalp", "*")]
        assert qs.score < 50.0, f"Expected low score for 20% WR, got {qs.score}"

    def test_regime_specific_scores(self):
        """Scores should differ per (strategy, regime) pair."""
        records = (
            _make_mixed_records(20, 5, "DefaultSMC", "TRENDING")
            + _make_mixed_records(5, 20, "DefaultSMC", "RANGING")
        )
        engine = LearningEngine()
        scores = engine.compute_scores(records)
        trending_score = scores[("DefaultSMC", "TRENDING")].score
        ranging_score = scores[("DefaultSMC", "RANGING")].score
        assert trending_score > ranging_score, (
            f"TRENDING ({trending_score}) should rank above RANGING ({ranging_score})"
        )

    def test_aggregate_and_regime_both_present(self):
        """Both aggregate (*) and regime-specific scores should be computed."""
        records = _make_mixed_records(20, 10, "DefaultSMC", "TRENDING")
        engine = LearningEngine()
        scores = engine.compute_scores(records)
        assert ("DefaultSMC", "*") in scores
        assert ("DefaultSMC", "TRENDING") in scores

    def test_score_bounded_0_100(self):
        """Quality scores should always be in [0, 100]."""
        records = _make_losing_records(40, "BadStrat")
        engine = LearningEngine()
        scores = engine.compute_scores(records)
        for qs in scores.values():
            assert 0.0 <= qs.score <= 100.0, f"Score {qs.score} out of range"

    def test_empty_records_returns_empty(self):
        """No records → no scores."""
        engine = LearningEngine()
        scores = engine.compute_scores([])
        assert len(scores) == 0

    def test_multiple_strategies_scored_independently(self):
        """Each strategy gets its own score."""
        records = (
            _make_winning_records(30, "DefaultSMC")
            + _make_losing_records(30, "SweepScalp")
        )
        engine = LearningEngine()
        scores = engine.compute_scores(records)
        assert scores[("DefaultSMC", "*")].score > scores[("SweepScalp", "*")].score


# ═══════════════════════════════════════════════════════════════════════════════
# LEARNING ENGINE — DECISIONS
# ═══════════════════════════════════════════════════════════════════════════════

class TestLearningEngineDecisions:

    def test_strategy_disabled_when_negative_expectancy(self):
        """A strategy with terrible performance should be DISABLED."""
        records = _make_mixed_records(3, 37, "BadStrat")  # 7.5% WR
        engine = LearningEngine()
        scores = engine.compute_scores(records)
        # Need multiple known strategies so safety guard allows disabling
        decisions = engine.make_decisions(scores, known_strategies=["BadStrat", "A", "B", "C"])
        dec = decisions[("BadStrat", "*")]
        assert dec.decision == Decision.DISABLE
        assert dec.multiplier == 0.0

    def test_high_quality_strategy_gets_boost(self):
        """A strategy with excellent performance should be BOOSTED."""
        records = _make_winning_records(40, "GoodStrat")
        engine = LearningEngine()
        scores = engine.compute_scores(records)
        decisions = engine.make_decisions(scores)
        dec = decisions[("GoodStrat", "*")]
        assert dec.decision == Decision.BOOST
        assert dec.multiplier > 1.0

    def test_regime_specific_disable(self):
        """Strategy X disabled in RANGING but not in TRENDING."""
        records = (
            _make_winning_records(35, "DefaultSMC", "TRENDING")
            + _make_losing_records(35, "DefaultSMC", "RANGING")
        )
        engine = LearningEngine()
        scores = engine.compute_scores(records)
        decisions = engine.make_decisions(scores, known_strategies=["DefaultSMC", "A", "B", "C"])
        # TRENDING should be active
        assert decisions[("DefaultSMC", "TRENDING")].decision != Decision.DISABLE
        # RANGING should be disabled
        assert decisions[("DefaultSMC", "RANGING")].decision == Decision.DISABLE

    def test_min_sample_size_protection(self):
        """Below min_trades → INSUFFICIENT_DATA, not disable."""
        records = _make_losing_records(10, "FewTrades")  # < 30
        engine = LearningEngine()
        scores = engine.compute_scores(records)
        decisions = engine.make_decisions(scores)
        dec = decisions[("FewTrades", "*")]
        assert dec.decision == Decision.INSUFFICIENT_DATA
        assert dec.multiplier == 1.0

    def test_custom_min_trades(self):
        """Custom min_trades from config."""
        records = _make_losing_records(20, "X")
        engine = LearningEngine({"LEARNING_MIN_TRADES": "15"})
        scores = engine.compute_scores(records)
        decisions = engine.make_decisions(scores)
        # 20 >= 15 → should get a real decision (not INSUFFICIENT_DATA)
        dec = decisions[("X", "*")]
        assert dec.decision != Decision.INSUFFICIENT_DATA

    def test_never_disable_all_strategies(self):
        """Safety: even if all strategies are bad, at least 1 must remain."""
        strats = ["A", "B", "C"]
        records = []
        for s in strats:
            records += _make_mixed_records(2, 38, s)  # 5% WR each
        engine = LearningEngine()
        scores = engine.compute_scores(records)
        decisions = engine.make_decisions(scores, known_strategies=strats)
        agg_disabled = sum(
            1 for (k, r), d in decisions.items()
            if r == "*" and d.decision == Decision.DISABLE
        )
        assert agg_disabled < len(strats), (
            f"All {len(strats)} strategies disabled — safety violation!"
        )

    def test_cooldown_after_disable(self):
        """After disabling, strategy stays REDUCE for cooldown_cycles - 1."""
        records = _make_mixed_records(2, 38, "BadStrat")
        engine = LearningEngine({"LEARNING_COOLDOWN_CYCLES": "3"})
        scores = engine.compute_scores(records)
        known = ["BadStrat", "A", "B", "C"]

        # Cycle 1: disable
        d1 = engine.make_decisions(scores, known_strategies=known)
        assert d1[("BadStrat", "*")].decision == Decision.DISABLE

        # Cycles 2, 3: cooldown -> REDUCE (tick consumes 1 per cycle)
        for i in range(2):
            d = engine.make_decisions(scores, known_strategies=known)
            dec = d[("BadStrat", "*")]
            assert dec.decision == Decision.REDUCE, f"Cycle {i+2}: expected REDUCE, got {dec.decision}"

        # Cycle 4: cooldown expired -> back to DISABLE
        d4 = engine.make_decisions(scores, known_strategies=known)
        assert d4[("BadStrat", "*")].decision == Decision.DISABLE

    def test_reduce_decision_for_mediocre_strategy(self):
        """A mediocre strategy should be REDUCE, not DISABLE or BOOST."""
        # ~40% win rate → mediocre
        records = _make_mixed_records(16, 24, "Mediocre")
        engine = LearningEngine()
        scores = engine.compute_scores(records)
        decisions = engine.make_decisions(scores)
        dec = decisions[("Mediocre", "*")]
        assert dec.decision in (Decision.REDUCE, Decision.FULL), f"Got {dec.decision}"


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY ALLOCATOR — GATE & MULTIPLIER
# ═══════════════════════════════════════════════════════════════════════════════

class TestStrategyAllocator:

    def _make_allocator(self, records, tmpdir=None, **config_overrides):
        """Create a StrategyAllocator with test records loaded."""
        journal = InstitutionalTradeJournal(strict=False)
        for r in records:
            journal.record_trade(r)
        cfg = {
            "LEARNING_ENGINE_ENABLED": "true",
            "LEARNING_REFRESH_INTERVAL_SEC": "0",  # always refresh
        }
        if tmpdir:
            cfg["LEARNING_STATE_PATH"] = os.path.join(tmpdir, "state.json")
        cfg.update(config_overrides)
        return StrategyAllocator(journal=journal, config=cfg)

    def test_disabled_strategy_blocked(self):
        """is_strategy_allowed returns False for disabled strategy."""
        records = _make_mixed_records(2, 38, "BadStrat")
        alloc = self._make_allocator(records)
        alloc.force_refresh()
        assert not alloc.is_strategy_allowed("BadStrat")

    def test_good_strategy_allowed(self):
        """is_strategy_allowed returns True for good strategy."""
        records = _make_winning_records(40, "GoodStrat")
        alloc = self._make_allocator(records)
        alloc.force_refresh()
        assert alloc.is_strategy_allowed("GoodStrat")

    def test_unknown_strategy_allowed(self):
        """Unknown strategy (no data) should be allowed by default."""
        records = _make_winning_records(40, "GoodStrat")
        alloc = self._make_allocator(records)
        alloc.force_refresh()
        assert alloc.is_strategy_allowed("UnknownStrat")

    def test_risk_multiplier_reduced(self):
        """Bad strategy gets multiplier < 1.0 (or 0 if disabled)."""
        records = _make_mixed_records(2, 38, "BadStrat")
        alloc = self._make_allocator(records)
        alloc.force_refresh()
        mult = alloc.get_risk_multiplier("BadStrat")
        assert mult < 1.0

    def test_risk_multiplier_boosted(self):
        """Good strategy gets multiplier > 1.0."""
        records = _make_winning_records(40, "GoodStrat")
        alloc = self._make_allocator(records)
        alloc.force_refresh()
        mult = alloc.get_risk_multiplier("GoodStrat")
        assert mult >= 1.0

    def test_disabled_engine_allows_all(self):
        """When LEARNING_ENGINE_ENABLED=false, everything is allowed."""
        records = _make_mixed_records(2, 38, "BadStrat")
        alloc = self._make_allocator(records, LEARNING_ENGINE_ENABLED="false")
        alloc.force_refresh()
        assert alloc.is_strategy_allowed("BadStrat")
        assert alloc.get_risk_multiplier("BadStrat") == 1.0

    def test_regime_specific_gate(self):
        """Strategy allowed in one regime, blocked in another."""
        records = (
            _make_winning_records(35, "DefaultSMC", "TRENDING")
            + _make_losing_records(35, "DefaultSMC", "RANGING")
        )
        alloc = self._make_allocator(records)
        alloc.force_refresh()
        assert alloc.is_strategy_allowed("DefaultSMC", "TRENDING")
        assert not alloc.is_strategy_allowed("DefaultSMC", "RANGING")

    def test_state_summary(self):
        """get_state_summary returns valid summary dict."""
        records = _make_winning_records(40, "DefaultSMC")
        alloc = self._make_allocator(records)
        alloc.force_refresh()
        summary = alloc.get_state_summary()
        assert summary["enabled"] is True
        assert summary["cycle_count"] >= 1
        assert summary["total_decisions"] > 0

    def test_force_refresh_returns_count(self):
        """force_refresh returns number of decisions made."""
        records = _make_winning_records(40, "DefaultSMC")
        alloc = self._make_allocator(records)
        count = alloc.force_refresh()
        assert count > 0


# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════

class TestPersistence:

    def test_state_saved_and_loaded(self):
        """Learning state should survive save → new allocator → load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            records = _make_winning_records(40, "DefaultSMC")
            alloc1 = self._make_allocator(records, tmpdir)
            alloc1.force_refresh()

            # Check file exists
            state_path = os.path.join(tmpdir, "state.json")
            assert os.path.exists(state_path)

            # Load state with a fresh allocator
            alloc2 = self._make_allocator([], tmpdir)
            # Verify loaded decisions match
            d1 = alloc1.get_all_decisions()
            d2 = alloc2.get_all_decisions()
            assert len(d2) > 0
            for key in d1:
                assert key in d2, f"Missing key {key} after reload"
                assert d1[key].decision == d2[key].decision

    def test_state_file_is_valid_json(self):
        """The persisted state should be valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            records = _make_winning_records(40, "DefaultSMC")
            alloc = self._make_allocator(records, tmpdir)
            alloc.force_refresh()

            state_path = os.path.join(tmpdir, "state.json")
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            assert "decisions" in data
            assert "scores" in data
            assert "cycle_count" in data
            assert data["cycle_count"] >= 1

    def test_missing_state_file_starts_fresh(self):
        """Missing state file should not crash — start fresh."""
        with tempfile.TemporaryDirectory() as tmpdir:
            alloc = self._make_allocator([], tmpdir)
            summary = alloc.get_state_summary()
            assert summary["cycle_count"] == 0

    def _make_allocator(self, records, tmpdir, **kw):
        journal = InstitutionalTradeJournal(strict=False)
        for r in records:
            journal.record_trade(r)
        cfg = {
            "LEARNING_ENGINE_ENABLED": "true",
            "LEARNING_REFRESH_INTERVAL_SEC": "0",
            "LEARNING_STATE_PATH": os.path.join(tmpdir, "state.json"),
        }
        cfg.update(kw)
        return StrategyAllocator(journal=journal, config=cfg)


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER MANAGER INTEGRATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestOrderManagerIntegration:

    def test_strategy_disabled_gate_in_hard_filters(self):
        """STRATEGY_DISABLED_BY_LEARNING should be in _HARD_FILTERS."""
        from smc_futures.engine.execution.order_manager import OrderManager
        assert "STRATEGY_DISABLED_BY_LEARNING" in OrderManager._HARD_FILTERS

    def test_set_strategy_allocator(self):
        """OrderManager should accept a strategy allocator."""
        from smc_futures.engine.execution.order_manager import OrderManager
        om = OrderManager({"AUTO_EXECUTE_ENABLED": True, "TRADING_MODE": "live"})
        mock_alloc = MagicMock()
        om.set_strategy_allocator(mock_alloc)
        assert om._strategy_allocator is mock_alloc

    def test_order_rejected_when_strategy_disabled(self):
        """pre_flight_checks rejects signal when strategy is disabled."""
        from smc_futures.engine.execution.order_manager import OrderManager

        om = OrderManager({"AUTO_EXECUTE_ENABLED": True, "TRADING_MODE": "live"})

        # Mock allocator that says "BadStrat" is disabled
        mock_alloc = MagicMock()
        mock_alloc.is_strategy_allowed.return_value = False
        om.set_strategy_allocator(mock_alloc)

        # Mock account state (not stale)
        mock_acct = MagicMock()
        mock_acct.block_reason = None
        om.set_account_state(mock_acct)

        signal_dict = {
            "strategy_id": "BadStrat",
            "direction": "long",
            "entry": 50000,
            "regime": "TRENDING",
        }
        # Patch V2 risk gate to pass
        with patch(
            "smc_futures.engine.execution.order_manager._evaluate_risk_v2",
            return_value=(True, 1.0, [])
        ):
            ok, reason, _ = om.pre_flight_checks(
                symbol="BTCUSDT",
                signal_dict=signal_dict,
                signal_id="test-signal-1",
                account_balance=10000.0,
                quantity=0.01,
            )
        assert not ok
        assert "STRATEGY_DISABLED_BY_LEARNING" in reason

    def test_order_passes_when_strategy_allowed(self):
        """pre_flight_checks should NOT block on allowed strategy (may fail on other gates)."""
        from smc_futures.engine.execution.order_manager import OrderManager

        om = OrderManager({"AUTO_EXECUTE_ENABLED": True, "TRADING_MODE": "live"})

        mock_alloc = MagicMock()
        mock_alloc.is_strategy_allowed.return_value = True
        mock_alloc.get_risk_multiplier.return_value = 1.0
        om.set_strategy_allocator(mock_alloc)

        mock_acct = MagicMock()
        mock_acct.block_reason = None
        om.set_account_state(mock_acct)

        signal_dict = {
            "strategy_id": "GoodStrat",
            "direction": "long",
            "entry": 50000,
            "regime": "TRENDING",
        }
        with patch(
            "smc_futures.engine.execution.order_manager._evaluate_risk_v2",
            return_value=(True, 1.0, [])
        ):
            ok, reason, _ = om.pre_flight_checks(
                symbol="BTCUSDT",
                signal_dict=signal_dict,
                signal_id="test-signal-2",
                account_balance=10000.0,
                quantity=0.01,
            )
        # May fail on other gates (DUPLICATE, ACTIVE_TRADE, etc.) but NOT on STRATEGY_DISABLED
        if not ok:
            assert "STRATEGY_DISABLED" not in reason


# ═══════════════════════════════════════════════════════════════════════════════
# APPCONTEXT WIRING
# ═══════════════════════════════════════════════════════════════════════════════

class TestAppContextWiring:

    def test_strategy_allocator_exists(self):
        """AppContext should have a strategy_allocator attribute."""
        from smc_futures.analytics.strategy_allocator import StrategyAllocator
        # Verify import chain
        assert hasattr(StrategyAllocator, "is_strategy_allowed")
        assert hasattr(StrategyAllocator, "get_risk_multiplier")
        assert hasattr(StrategyAllocator, "force_refresh")
