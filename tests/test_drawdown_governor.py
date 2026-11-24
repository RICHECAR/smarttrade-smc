"""
Tests for DrawdownGovernor — every risk tier transition,
hysteresis, force close, weekly limits, consecutive losses.
"""

import pytest
from smc_futures.risk.drawdown_governor import (
    DrawdownGovernor,
    RiskTier,
    TIER_RISK_MULT,
    TIER_MAX_POSITIONS,
)


def _make_gov(**overrides) -> DrawdownGovernor:
    config = {
        "DD_GOV_CAUTION_PCT": 5.0,
        "DD_GOV_DANGER_PCT": 10.0,
        "DD_GOV_CRITICAL_PCT": 15.0,
        "DD_GOV_MAX_WEEKLY_LOSS_PCT": 5.0,
        "DD_GOV_MAX_CONSEC_LOSSES": 5,
        "DD_GOV_RECOVERY_PROFIT_PCT": 2.0,
        "DD_GOV_RECOVERY_TRADES": 3,
        "DD_GOV_HYSTERESIS_CYCLES": 3,
        **overrides,
    }
    gov = DrawdownGovernor(config=config)
    return gov


class TestInitialization:
    def test_init_happy_path(self):
        gov = _make_gov()
        gov.initialize(10_000.0)
        assert gov._peak_equity == 10_000.0
        assert gov._current_equity == 10_000.0
        assert gov._tier == RiskTier.NORMAL

    def test_init_rejects_zero(self):
        gov = _make_gov()
        with pytest.raises(ValueError):
            gov.initialize(0.0)

    def test_init_rejects_negative(self):
        gov = _make_gov()
        with pytest.raises(ValueError):
            gov.initialize(-100.0)

    def test_bad_threshold_order(self):
        with pytest.raises(ValueError):
            _make_gov(DD_GOV_CAUTION_PCT=10.0, DD_GOV_DANGER_PCT=5.0)


class TestDrawdownCalculation:
    def test_no_drawdown_at_peak(self):
        gov = _make_gov()
        gov.initialize(10_000.0)
        result = gov.update_equity(10_000.0)
        assert result["drawdown_from_peak_pct"] == 0.0
        assert result["tier"] == "NORMAL"

    def test_drawdown_3pct_still_normal(self):
        gov = _make_gov()
        gov.initialize(10_000.0)
        result = gov.update_equity(9_700.0)
        assert result["drawdown_from_peak_pct"] == 3.0
        assert result["tier"] == "NORMAL"

    def test_new_high_resets_peak(self):
        gov = _make_gov()
        gov.initialize(10_000.0)
        gov.update_equity(9_500.0)  # 5% DD
        gov.update_equity(11_000.0)  # new high
        assert gov._peak_equity == 11_000.0
        result = gov.update_equity(11_000.0)
        assert result["drawdown_from_peak_pct"] == 0.0


class TestTierTransitions:
    def test_caution_with_hysteresis(self):
        """Must stay at CAUTION-level for 3 cycles before transitioning."""
        # Disable weekly limit to isolate peak DD behavior
        gov = _make_gov(DD_GOV_MAX_WEEKLY_LOSS_PCT=50.0)
        gov.initialize(10_000.0)

        # Cycle 1: 5.5% DD — targets CAUTION
        r1 = gov.update_equity(9_450.0)
        assert r1["tier"] == "NORMAL"  # hysteresis not met yet

        # Cycle 2
        r2 = gov.update_equity(9_450.0)
        assert r2["tier"] == "NORMAL"

        # Cycle 3 — hysteresis met
        r3 = gov.update_equity(9_450.0)
        assert r3["tier"] == "CAUTION"
        assert r3["risk_multiplier"] == 0.5

    def test_critical_is_instant(self):
        """CRITICAL tier transition bypasses hysteresis — safety first."""
        gov = _make_gov()
        gov.initialize(10_000.0)
        result = gov.update_equity(8_400.0)  # 16% DD
        assert result["tier"] == "CRITICAL"
        assert result["blocked"] is True
        assert result["risk_multiplier"] == 0.0

    def test_danger_with_hysteresis(self):
        # Disable weekly limit to isolate peak DD behavior
        gov = _make_gov(DD_GOV_MAX_WEEKLY_LOSS_PCT=50.0)
        gov.initialize(10_000.0)

        # Need to get through CAUTION first
        for _ in range(3):
            gov.update_equity(9_400.0)  # 6% DD → CAUTION
        assert gov._tier == RiskTier.CAUTION

        # Now push to 11% DD
        for _ in range(3):
            gov.update_equity(8_900.0)  # 11% DD → DANGER after 3 cycles
        assert gov._tier == RiskTier.DANGER
        assert gov.get_risk_multiplier() == 0.25


class TestWeeklyLimit:
    def test_weekly_loss_triggers_danger(self):
        gov = _make_gov(DD_GOV_MAX_WEEKLY_LOSS_PCT=5.0, DD_GOV_HYSTERESIS_CYCLES=1)
        gov.initialize(10_000.0)
        # 6% weekly loss
        for _ in range(1):
            gov.update_equity(9_400.0)
        assert gov._tier == RiskTier.DANGER

    def test_weekly_reset_clears(self):
        gov = _make_gov(DD_GOV_MAX_WEEKLY_LOSS_PCT=5.0, DD_GOV_HYSTERESIS_CYCLES=1)
        gov.initialize(10_000.0)
        gov.update_equity(9_400.0)  # 6% weekly loss → DANGER
        gov.reset_weekly()
        # After reset, weekly DD recalculated from new baseline
        assert gov._week_start_equity == 9_400.0


class TestConsecutiveLosses:
    def test_consec_losses_escalate_to_danger(self):
        gov = _make_gov(DD_GOV_MAX_CONSEC_LOSSES=3)
        gov.initialize(10_000.0)
        gov.record_trade_result(-50)
        gov.record_trade_result(-50)
        assert gov._tier == RiskTier.NORMAL  # only 2
        gov.record_trade_result(-50)
        assert gov._tier == RiskTier.DANGER  # 3 = max

    def test_win_resets_streak(self):
        gov = _make_gov(DD_GOV_MAX_CONSEC_LOSSES=5)
        gov.initialize(10_000.0)
        for _ in range(4):
            gov.record_trade_result(-50)
        assert gov._consecutive_losses == 4
        gov.record_trade_result(100)
        assert gov._consecutive_losses == 0


class TestRecovery:
    def test_recovery_from_danger(self):
        gov = _make_gov(
            DD_GOV_HYSTERESIS_CYCLES=1,
            DD_GOV_RECOVERY_PROFIT_PCT=2.0,
            DD_GOV_RECOVERY_TRADES=3,
            DD_GOV_MAX_WEEKLY_LOSS_PCT=50.0,  # isolate peak DD
        )
        gov.initialize(10_000.0)

        # Push to DANGER (11% DD from peak)
        gov.update_equity(8_900.0)
        assert gov._tier == RiskTier.DANGER

        # Recover equity above danger threshold (need < 10% DD from peak)
        # AND 2% from trough (8900 * 1.02 = 9078)
        # Peak is 10000, danger is 10%, so need equity > 9000
        # 9100 = 9% DD from peak (below danger) AND > 9078 (2% from trough)
        gov.update_equity(9_100.0)
        assert gov._tier == RiskTier.RECOVERY
        assert gov.get_risk_multiplier() == 0.35

    def test_recovery_needs_wins_to_normal(self):
        gov = _make_gov(
            DD_GOV_HYSTERESIS_CYCLES=1,
            DD_GOV_RECOVERY_PROFIT_PCT=2.0,
            DD_GOV_RECOVERY_TRADES=3,
            DD_GOV_MAX_WEEKLY_LOSS_PCT=50.0,  # isolate peak DD
        )
        gov.initialize(10_000.0)
        gov.update_equity(8_900.0)  # DANGER
        gov.update_equity(9_100.0)  # RECOVERY

        gov.record_trade_result(50)
        assert gov._tier == RiskTier.RECOVERY  # 1 win
        gov.record_trade_result(50)
        assert gov._tier == RiskTier.RECOVERY  # 2 wins
        gov.record_trade_result(50)
        assert gov._tier == RiskTier.NORMAL  # 3 wins → NORMAL

    def test_loss_during_recovery_resets_wins(self):
        gov = _make_gov(
            DD_GOV_HYSTERESIS_CYCLES=1,
            DD_GOV_RECOVERY_PROFIT_PCT=2.0,
            DD_GOV_RECOVERY_TRADES=3,
            DD_GOV_MAX_WEEKLY_LOSS_PCT=50.0,  # isolate peak DD
        )
        gov.initialize(10_000.0)
        gov.update_equity(8_900.0)  # DANGER
        gov.update_equity(9_100.0)  # RECOVERY

        gov.record_trade_result(50)
        gov.record_trade_result(50)
        assert gov._recovery_wins == 2
        gov.record_trade_result(-50)  # loss resets
        assert gov._recovery_wins == 0


class TestForceClose:
    def test_critical_triggers_force_close_count(self):
        gov = _make_gov()
        gov.initialize(10_000.0)
        gov.update_equity(8_400.0)  # 16% DD → CRITICAL
        assert gov.total_force_closes == 1

    def test_trading_blocked_in_critical(self):
        gov = _make_gov()
        gov.initialize(10_000.0)
        gov.update_equity(8_400.0)
        allowed, reason = gov.is_trading_allowed()
        assert allowed is False
        assert "CRITICAL" in reason


class TestRiskMultipliers:
    def test_all_tiers_have_multipliers(self):
        for tier in RiskTier:
            assert tier in TIER_RISK_MULT
            assert 0.0 <= TIER_RISK_MULT[tier] <= 1.0

    def test_all_tiers_have_max_positions(self):
        for tier in RiskTier:
            assert tier in TIER_MAX_POSITIONS
            assert TIER_MAX_POSITIONS[tier] >= 0


class TestStateSnapshot:
    def test_snapshot_contains_all_fields(self):
        gov = _make_gov()
        gov.initialize(10_000.0)
        gov.update_equity(9_500.0)
        state = gov.get_state()
        required = {
            "tier", "risk_multiplier", "max_positions", "current_equity",
            "peak_equity", "trough_equity", "drawdown_from_peak_pct",
            "weekly_drawdown_pct", "consecutive_losses", "recovery_wins",
            "worst_drawdown_pct", "total_tier_transitions", "total_force_closes",
            "last_update",
        }
        assert required.issubset(state.keys())


class TestHysteresisReset:
    def test_alternating_levels_dont_trigger(self):
        """If DD bounces around caution threshold, no transition."""
        gov = _make_gov(DD_GOV_HYSTERESIS_CYCLES=3)
        gov.initialize(10_000.0)
        gov.update_equity(9_450.0)  # 5.5% → targets CAUTION (1)
        gov.update_equity(9_600.0)  # 4.0% → targets NORMAL (reset)
        gov.update_equity(9_450.0)  # 5.5% → targets CAUTION (1 again)
        gov.update_equity(9_450.0)  # 5.5% → targets CAUTION (2)
        assert gov._tier == RiskTier.NORMAL  # never hit 3 consecutive


class TestNotInitialized:
    def test_update_before_init_returns_safe(self):
        gov = _make_gov()
        result = gov.update_equity(10_000.0)
        assert result["allowed"] is True
        assert result["reason"] == "NOT_INITIALIZED"


class TestTradeLog:
    def test_trade_log_bounded(self):
        gov = _make_gov()
        gov.initialize(10_000.0)
        for i in range(600):
            gov.record_trade_result(-1)
        assert len(gov._trade_log) <= 500
