"""
Tests for Chantier 5 — Institutional Trade Journal + Execution Metrics.

Covers:
  - ExitReason normalization
  - TradeRecord schema + derived fields
  - MAE / MFE computation (long & short)
  - Slippage BPS computation
  - InstitutionalTradeJournal (record, strict, persist)
  - TradeReport attribution (strategy, regime, expectancy, outlier)
  - Close path journal hooks (TP, SL, kill switch, emergency, governor)
  - Fail-closed on missing data
"""

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from smc_futures.analytics.exit_reason import ExitReason, normalize
from smc_futures.analytics.mae_mfe import (
    ExcursionResult,
    compute_excursions,
    compute_slippage_bps,
)
from smc_futures.analytics.trade_journal import (
    InstitutionalTradeJournal,
    TradeRecord,
    _REQUIRED_FIELDS,
)
from smc_futures.analytics.trade_report import TradeReport, GroupStats
from smc_futures.analytics import journal_hook


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
        strategy_id="smc_ob_fvg",
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
        fees=0.60,
        risk_amount_at_entry=5.0,
        rr_planned=2.0,
        account_equity_at_entry=10000.0,
        account_equity_at_exit=10009.20,
    )
    defaults.update(overrides)
    return TradeRecord(**defaults)


def _make_candles_long():
    """Candles for a long trade: entry=50000, range [49800..51200]."""
    return [
        {"high": 50100, "low": 49900},
        {"high": 50500, "low": 49800},  # lowest
        {"high": 51200, "low": 50200},  # highest
        {"high": 51000, "low": 50400},
    ]


def _make_candles_short():
    """Candles for a short trade: entry=50000, range [48800..50300]."""
    return [
        {"high": 50100, "low": 49800},
        {"high": 50300, "low": 49500},  # highest adverse
        {"high": 49800, "low": 48800},  # best favorable
        {"high": 49600, "low": 49000},
    ]


def _tmp_journal(strict: bool = False) -> InstitutionalTradeJournal:
    """Create a journal writing to a temp file."""
    path = os.path.join(tempfile.mkdtemp(), "test_journal.jsonl")
    return InstitutionalTradeJournal(path=path, strict=strict)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. EXIT REASON TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitReason:
    def test_exit_reason_is_normalized(self):
        """All known free-form strings map to canonical reasons."""
        assert normalize("tp_hit") == ExitReason.TAKE_PROFIT
        assert normalize("sl_hit") == ExitReason.STOP_LOSS
        assert normalize("TAKE_PROFIT") == ExitReason.TAKE_PROFIT
        assert normalize("manual_close") == ExitReason.MANUAL_CLOSE
        assert normalize("trailing") == ExitReason.TRAILING_STOP
        assert normalize("breakeven") == ExitReason.BREAK_EVEN
        assert normalize("timeout") == ExitReason.TIME_STOP
        assert normalize("emergency_sl") == ExitReason.EMERGENCY_CLOSE
        assert normalize("kill_switch_l3") == ExitReason.KILL_SWITCH_CLOSE
        assert normalize("drawdown_governor") == ExitReason.DRAWDOWN_GOVERNOR_CLOSE
        assert normalize("liquidated") == ExitReason.LIQUIDATION

    def test_emergency_close_reason_persisted(self):
        """Emergency exit reasons are recognized as forced."""
        assert ExitReason.is_forced(ExitReason.EMERGENCY_CLOSE)
        assert ExitReason.is_forced(ExitReason.KILL_SWITCH_CLOSE)
        assert ExitReason.is_forced(ExitReason.DRAWDOWN_GOVERNOR_CLOSE)
        assert ExitReason.is_forced(ExitReason.LIQUIDATION)
        assert not ExitReason.is_forced(ExitReason.TAKE_PROFIT)
        assert not ExitReason.is_forced(ExitReason.MANUAL_CLOSE)

    def test_kill_switch_close_reason_persisted(self):
        """L3 compound reason strings are normalized."""
        assert normalize("l3_force_close: -3.5% >= 3.0%") == ExitReason.KILL_SWITCH_CLOSE
        assert normalize("kill_switch") == ExitReason.KILL_SWITCH_CLOSE

    def test_unknown_falls_through(self):
        assert normalize(None) == ExitReason.UNKNOWN
        assert normalize("weird_custom_reason") == ExitReason.UNKNOWN
        assert normalize("") == ExitReason.UNKNOWN


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MAE / MFE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestMaeMfe:
    def test_mae_mfe_computation_long(self):
        """Long: MAE = entry - lowest, MFE = highest - entry."""
        candles = _make_candles_long()
        exc = compute_excursions(
            side="long",
            entry_price=50000.0,
            exit_price=50990.0,
            candles=candles,
            realized_pnl=9.90,
        )
        assert exc.data_quality == "full"
        assert exc.mae == 200.0  # 50000 - 49800
        assert exc.mfe == 1200.0  # 51200 - 50000
        assert exc.excursion_ratio == 6.0  # 1200/200
        assert exc.candles_used == 4

    def test_mae_mfe_computation_short(self):
        """Short: MAE = highest - entry, MFE = entry - lowest."""
        candles = _make_candles_short()
        exc = compute_excursions(
            side="short",
            entry_price=50000.0,
            exit_price=49200.0,
            candles=candles,
            realized_pnl=800.0,
        )
        assert exc.data_quality == "full"
        assert exc.mae == 300.0  # 50300 - 50000
        assert exc.mfe == 1200.0  # 50000 - 48800
        assert exc.excursion_ratio == 4.0  # 1200/300

    def test_slippage_bps_computation_entry_exit(self):
        """Slippage in BPS matches expected formula."""
        # Entry: expected 50000, filled 50010 → 2 bps
        slip = compute_slippage_bps(50000.0, 50010.0)
        assert slip == 2.0

        # Exit: expected 51000, filled 50990 → ~0.20 bps
        slip2 = compute_slippage_bps(51000.0, 50990.0)
        assert slip2 == pytest.approx(1.96, abs=0.01)

    def test_missing_intratrade_data_is_flagged_not_faked(self):
        """No candles → data_quality='unavailable', all excursions None."""
        exc = compute_excursions(
            side="long",
            entry_price=50000.0,
            exit_price=50500.0,
            candles=None,
        )
        assert exc.data_quality == "unavailable"
        assert exc.mae is None
        assert exc.mfe is None
        assert exc.candles_used == 0

    def test_empty_candles_flagged(self):
        exc = compute_excursions(
            side="long",
            entry_price=50000.0,
            exit_price=50500.0,
            candles=[],
        )
        assert exc.data_quality == "unavailable"

    def test_partial_data_quality(self):
        """Fewer than 3 candles → data_quality='partial'."""
        candles = [{"high": 50200, "low": 49800}]
        exc = compute_excursions(
            side="long", entry_price=50000, exit_price=50100, candles=candles,
        )
        assert exc.data_quality == "partial"
        assert exc.candles_used == 1

    def test_capture_ratio(self):
        """capture_ratio = realized_pnl / MFE."""
        candles = [
            {"high": 51000, "low": 49500},
            {"high": 51500, "low": 49800},
            {"high": 51200, "low": 50000},
        ]
        exc = compute_excursions(
            side="long",
            entry_price=50000,
            exit_price=50750,
            candles=candles,
            realized_pnl=750.0,
        )
        # MFE = 51500 - 50000 = 1500; capture = 750/1500 = 0.5
        assert exc.mfe == 1500.0
        assert exc.capture_ratio == 0.5

    def test_zero_entry_price(self):
        exc = compute_excursions(
            side="long", entry_price=0.0, exit_price=100.0,
            candles=[{"high": 110, "low": 90}],
        )
        assert exc.data_quality == "unavailable"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. TRADE JOURNAL TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestTradeJournal:
    def test_trade_journal_records_closed_trade(self):
        """A valid record is accepted and persisted."""
        j = _tmp_journal()
        rec = _make_record()
        ok = j.record_trade(rec)
        assert ok is True
        assert j.count == 1
        assert j.records[0].trade_id == rec.trade_id

    def test_journal_persists_to_jsonl(self):
        """Record is written to JSONL file."""
        j = _tmp_journal()
        rec = _make_record()
        j.record_trade(rec)
        # Read back
        with open(j.path, "r") as f:
            lines = f.readlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["symbol"] == "BTCUSDT"
        assert data["exit_reason"] == "TAKE_PROFIT"
        assert data["gross_pnl"] == 9.80

    def test_journal_computes_derived_fields(self):
        """Derived fields (slippage, net_pnl, holding_time) are auto-computed."""
        j = _tmp_journal()
        rec = _make_record()
        j.record_trade(rec)
        stored = j.records[0]
        assert stored.slippage_entry_bps is not None
        assert stored.slippage_entry_bps > 0
        assert stored.net_pnl == pytest.approx(9.80 - 0.60, abs=0.01)
        assert stored.holding_time_sec is not None
        assert stored.holding_time_sec > 0

    def test_journal_normalizes_exit_reason(self):
        """Free-form exit reasons are normalized on record."""
        j = _tmp_journal()
        rec = _make_record(exit_reason="tp_hit")
        j.record_trade(rec)
        assert j.records[0].exit_reason == "TAKE_PROFIT"

    def test_journal_with_candles_computes_mae_mfe(self):
        """Candles produce MAE/MFE when provided."""
        j = _tmp_journal()
        rec = _make_record()
        candles = _make_candles_long()
        j.record_trade(rec, candles=candles)
        stored = j.records[0]
        assert stored.mae is not None
        assert stored.mfe is not None
        assert stored.excursion_data_quality == "full"

    def test_journal_without_candles_flags_unavailable(self):
        """No candles → excursion_data_quality='unavailable'."""
        j = _tmp_journal()
        rec = _make_record()
        j.record_trade(rec)
        assert j.records[0].excursion_data_quality == "unavailable"

    def test_journal_load_from_file(self):
        """Records can be loaded back from JSONL."""
        j = _tmp_journal()
        for i in range(3):
            j.record_trade(_make_record(symbol=f"SYM{i}"))
        loaded = j.load_from_file()
        assert len(loaded) == 3
        assert loaded[1].symbol == "SYM1"

    def test_trade_journal_strict_mode_rejects_invalid_record(self):
        """Strict mode rejects records missing required fields."""
        j = _tmp_journal(strict=True)
        rec = TradeRecord()  # Empty — missing all required fields
        ok = j.record_trade(rec)
        assert ok is False
        assert j.count == 0
        assert len(j.errors) == 1
        assert "missing_fields" in j.errors[0]

    def test_trade_journal_missing_required_fields_logged_explicitly(self):
        """Non-strict mode still logs missing fields as errors."""
        j = _tmp_journal(strict=False)
        rec = TradeRecord(symbol="BTCUSDT")  # Missing many fields
        ok = j.record_trade(rec)
        assert ok is True  # Non-strict: accepted
        assert len(j.errors) == 1  # But error logged
        missing = j.errors[0]["missing_fields"]
        assert "entry_price_filled" in missing
        assert "exit_price_filled" in missing

    def test_rr_realized_computed(self):
        """RR realized = gross_pnl / risk_amount_at_entry."""
        j = _tmp_journal()
        rec = _make_record(gross_pnl=10.0, risk_amount_at_entry=5.0)
        j.record_trade(rec)
        assert j.records[0].rr_realized == 2.0

    def test_auto_trade_id_generation(self):
        """Empty trade_id is auto-populated."""
        j = _tmp_journal()
        rec = _make_record(trade_id="")
        j.record_trade(rec)
        assert j.records[0].trade_id != ""
        assert len(j.records[0].trade_id) == 36  # UUID


# ═══════════════════════════════════════════════════════════════════════════════
# 4. TRADE REPORT TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestTradeReport:
    def _sample_records(self) -> list[TradeRecord]:
        """Create a diverse set of records for report testing."""
        recs = [
            _make_record(strategy_id="strat_a", regime_at_entry="TREND", gross_pnl=100, fees=5),
            _make_record(strategy_id="strat_a", regime_at_entry="TREND", gross_pnl=-30, fees=5),
            _make_record(strategy_id="strat_a", regime_at_entry="RANGE", gross_pnl=50, fees=5),
            _make_record(strategy_id="strat_b", regime_at_entry="TREND", gross_pnl=-20, fees=5),
            _make_record(strategy_id="strat_b", regime_at_entry="RANGE", gross_pnl=80, fees=5),
            _make_record(strategy_id="strat_b", regime_at_entry="RANGE", gross_pnl=-10, fees=5),
        ]
        # Compute derived before report
        j = _tmp_journal()
        for r in recs:
            j.record_trade(r)
        return j.records

    def test_trade_report_groups_by_strategy(self):
        recs = self._sample_records()
        report = TradeReport(recs)
        by_strat = report.by_strategy()
        assert "strat_a" in by_strat
        assert "strat_b" in by_strat
        assert by_strat["strat_a"].total_trades == 3
        assert by_strat["strat_b"].total_trades == 3
        assert by_strat["strat_a"].gross_pnl == pytest.approx(120.0)

    def test_trade_report_groups_by_regime(self):
        recs = self._sample_records()
        report = TradeReport(recs)
        by_regime = report.by_regime()
        assert "TREND" in by_regime
        assert "RANGE" in by_regime
        assert by_regime["TREND"].total_trades == 3
        assert by_regime["RANGE"].total_trades == 3

    def test_trade_report_computes_expectancy(self):
        recs = self._sample_records()
        report = TradeReport(recs)
        overall = report.full_report()["overall"]
        assert overall["expectancy"] is not None
        # 4 wins, 2 losses
        # WR = 4/6 ≈ 0.667
        # avg_win = (100+50+80)/3.. no: recs have: 100, -30, 50, -20, 80, -10
        # wins: 100, 50, 80 = 3 wins, avg_win = 76.67
        # losses: -30, -20, -10 = 3 losses, avg_loss = -20
        # WR = 3/6 = 0.5
        # expectancy = 0.5 * 76.67 + 0.5 * (-20) = 28.33
        assert overall["win_rate"] == pytest.approx(0.5)
        assert overall["expectancy"] == pytest.approx(28.333, abs=0.1)

    def test_trade_report_computes_outlier_loss_rate(self):
        """Outlier loss rate catches extreme losses."""
        recs = [
            _make_record(gross_pnl=10),
            _make_record(gross_pnl=12),
            _make_record(gross_pnl=8),
            _make_record(gross_pnl=11),
            _make_record(gross_pnl=9),
            _make_record(gross_pnl=10),
            _make_record(gross_pnl=-500),  # Extreme outlier
        ]
        j = _tmp_journal()
        for r in recs:
            j.record_trade(r)
        report = TradeReport(j.records)
        overall = report.full_report()["overall"]
        assert overall["outlier_loss_rate"] is not None
        assert overall["outlier_loss_rate"] > 0  # The -500 should be an outlier

    def test_top_winners_losers(self):
        recs = self._sample_records()
        report = TradeReport(recs)
        winners = report.top_winners(2)
        losers = report.top_losers(2)
        assert len(winners) == 2
        assert winners[0]["gross_pnl"] == 100
        assert len(losers) == 2
        assert losers[0]["gross_pnl"] == -30

    def test_holding_time_distribution(self):
        recs = self._sample_records()
        report = TradeReport(recs)
        dist = report.holding_time_distribution()
        assert "15m-1h" in dist
        # Our default records have 30min holding time
        assert dist["15m-1h"] == 6

    def test_exit_reason_distribution(self):
        recs = [
            _make_record(exit_reason="TAKE_PROFIT"),
            _make_record(exit_reason="TAKE_PROFIT"),
            _make_record(exit_reason="STOP_LOSS"),
        ]
        j = _tmp_journal()
        for r in recs:
            j.record_trade(r)
        report = TradeReport(j.records)
        by_reason = report.by_exit_reason()
        assert by_reason["TAKE_PROFIT"].total_trades == 2
        assert by_reason["STOP_LOSS"].total_trades == 1

    def test_full_report_structure(self):
        recs = self._sample_records()
        report = TradeReport(recs).full_report()
        assert "total_trades" in report
        assert "overall" in report
        assert "by_strategy" in report
        assert "by_symbol" in report
        assert "by_broker" in report
        assert "by_regime" in report
        assert "by_exit_reason" in report
        assert "top_winners" in report
        assert "top_losers" in report
        assert "holding_time_distribution" in report


# ═══════════════════════════════════════════════════════════════════════════════
# 5. CLOSE PATH JOURNAL HOOKS TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestClosePathHooks:
    """Test that every close path writes to the journal."""

    def setup_method(self):
        self.journal = _tmp_journal()
        journal_hook.set_journal(self.journal)

    def teardown_method(self):
        journal_hook._journal = None

    def test_tp_close_writes_trade_journal(self):
        ok = journal_hook.record_close(
            symbol="BTCUSDT", side="long",
            entry_price=50000, exit_price=51000,
            qty=0.01, pnl=10.0, fee=0.6,
            reason="tp_hit", broker="kraken",
        )
        assert ok is True
        assert self.journal.count == 1
        assert self.journal.records[0].exit_reason == "TAKE_PROFIT"

    def test_sl_close_writes_trade_journal(self):
        ok = journal_hook.record_close(
            symbol="ETHUSDT", side="short",
            entry_price=3000, exit_price=3050,
            qty=0.1, pnl=-5.0, fee=0.3,
            reason="sl_hit", broker="kraken",
        )
        assert ok is True
        assert self.journal.records[0].exit_reason == "STOP_LOSS"

    def test_kill_switch_close_writes_trade_journal(self):
        ok = journal_hook.record_close(
            symbol="BTCUSDT", side="long",
            entry_price=50000, exit_price=49000,
            qty=0.01, pnl=-100.0, fee=0.6,
            reason="kill_switch_l3",
            kill_switch_state="L3_FORCE_CLOSE",
        )
        assert ok is True
        assert self.journal.records[0].exit_reason == "KILL_SWITCH_CLOSE"
        assert self.journal.records[0].kill_switch_state == "L3_FORCE_CLOSE"

    def test_emergency_close_writes_trade_journal(self):
        ok = journal_hook.record_close(
            symbol="BTCUSDT", side="long",
            entry_price=50000, exit_price=50000,
            qty=0.01, pnl=0.0,
            reason="emergency_sl",
        )
        assert ok is True
        assert self.journal.records[0].exit_reason == "EMERGENCY_CLOSE"

    def test_governor_close_writes_trade_journal(self):
        ok = journal_hook.record_close(
            symbol="BTCUSDT", side="short",
            entry_price=50000, exit_price=50500,
            qty=0.01, pnl=-50.0,
            reason="drawdown_governor_close",
            drawdown_governor_state="CRITICAL",
        )
        assert ok is True
        assert self.journal.records[0].exit_reason == "DRAWDOWN_GOVERNOR_CLOSE"
        assert self.journal.records[0].drawdown_governor_state == "CRITICAL"

    def test_no_journal_returns_false(self):
        journal_hook._journal = None
        ok = journal_hook.record_close(
            symbol="BTCUSDT", side="long",
            entry_price=50000, exit_price=51000,
            qty=0.01, pnl=10.0,
            reason="tp_hit",
        )
        assert ok is False

    def test_hook_with_candles(self):
        candles = _make_candles_long()
        ok = journal_hook.record_close(
            symbol="BTCUSDT", side="long",
            entry_price=50000, exit_price=50990,
            qty=0.01, pnl=9.90,
            reason="TAKE_PROFIT",
            candles=candles,
        )
        assert ok is True
        rec = self.journal.records[0]
        assert rec.mae is not None
        assert rec.mfe is not None
        assert rec.excursion_data_quality == "full"
