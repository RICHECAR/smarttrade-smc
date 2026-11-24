"""
Tests for RegimeFilter hysteresis + cooldown — Chantier 4
"""

import pytest
from smc_futures.risk.regime_filter import RegimeFilter, MarketRegime


def _rf(**overrides):
    """Build a RegimeFilter with test-friendly defaults."""
    defaults = dict(
        REGIME_FILTER_ENABLED=True,
        REGIME_TREND_THRESHOLD=4,
        REGIME_HIGH_VOL_ATR_RATIO=2.0,
        REGIME_LOW_VOL_ATR_RATIO=0.5,
        REGIME_LOOKBACK=20,
        REGIME_COMPRESSION_RATIO=1.0,
        REGIME_HYSTERESIS_CYCLES=3,
        REGIME_COOLDOWN_CYCLES=2,
    )
    defaults.update(overrides)
    return RegimeFilter(defaults)


def _candles_trend(n=20):
    """Generate candles with clear HH/HL pattern (bullish trend)."""
    return [{"high": 100 + i * 5, "low": 95 + i * 5} for i in range(n)]


def _candles_range(n=20):
    """Generate candles with no clear direction (range)."""
    return [{"high": 100 + (i % 3), "low": 95 - (i % 3)} for i in range(n)]


class TestHysteresis:
    """A regime change must persist for N cycles before adoption."""

    def test_no_immediate_switch(self):
        rf = _rf(REGIME_HYSTERESIS_CYCLES=3)
        # First call: RANGE (init)
        r1 = rf.detect(_candles_range(), 1.0, 1.0)
        assert r1 == MarketRegime.RANGE

        # Now send TREND signal once — should NOT switch yet
        r2 = rf.detect(_candles_trend(), 1.0, 1.0)
        assert r2 == MarketRegime.RANGE  # still range (1/3 pending)

    def test_switch_after_hysteresis(self):
        rf = _rf(REGIME_HYSTERESIS_CYCLES=3)
        rf.detect(_candles_range(), 1.0, 1.0)  # init RANGE

        # Send TREND 3 times
        for _ in range(2):
            r = rf.detect(_candles_trend(), 1.0, 1.0)
            assert r == MarketRegime.RANGE  # still pending

        # 3rd time → actually switch
        r = rf.detect(_candles_trend(), 1.0, 1.0)
        assert r == MarketRegime.TREND

    def test_pending_reset_on_different_regime(self):
        rf = _rf(REGIME_HYSTERESIS_CYCLES=3)
        rf.detect(_candles_range(), 1.0, 1.0)  # init RANGE

        # 2x TREND
        rf.detect(_candles_trend(), 1.0, 1.0)
        rf.detect(_candles_trend(), 1.0, 1.0)
        # Now RANGE again — resets pending
        rf.detect(_candles_range(), 1.0, 1.0)

        # 2x TREND again — not enough (only 2, not 3)
        rf.detect(_candles_trend(), 1.0, 1.0)
        r = rf.detect(_candles_trend(), 1.0, 1.0)
        assert r == MarketRegime.RANGE  # still range (only 2 after reset)


class TestHighVolInstant:
    """HIGH_VOL bypasses hysteresis (safety override)."""

    def test_high_vol_immediate(self):
        rf = _rf(REGIME_HYSTERESIS_CYCLES=5)
        rf.detect(_candles_range(), 1.0, 1.0)  # init RANGE

        # Single HIGH_VOL signal → immediate switch
        r = rf.detect(_candles_range(), 5.0, 1.0)  # atr_ratio=5.0 >> 2.0
        assert r == MarketRegime.HIGH_VOL


class TestCooldown:
    """After a transition, further transitions are delayed."""

    def test_cooldown_prevents_immediate_reversal(self):
        rf = _rf(REGIME_HYSTERESIS_CYCLES=1, REGIME_COOLDOWN_CYCLES=3)
        rf.detect(_candles_range(), 1.0, 1.0)  # init RANGE

        # Switch to TREND (1 cycle hysteresis)
        r = rf.detect(_candles_trend(), 1.0, 1.0)
        assert r == MarketRegime.TREND

        # Now immediately RANGE signals — cooldown should block
        r = rf.detect(_candles_range(), 1.0, 1.0)
        assert r == MarketRegime.TREND  # cooldown 3-2=1 remaining

        r = rf.detect(_candles_range(), 1.0, 1.0)
        assert r == MarketRegime.TREND  # cooldown 2-1=0 remaining

        r = rf.detect(_candles_range(), 1.0, 1.0)
        assert r == MarketRegime.TREND  # cooldown just expired, pending=1, not enough

        # Now that cooldown has expired, hysteresis starts
        r = rf.detect(_candles_range(), 1.0, 1.0)
        assert r == MarketRegime.RANGE  # pending hit threshold (1)


class TestGatingUnchanged:
    """gate_strategy() backward compatibility."""

    def test_high_vol_blocks_all(self):
        rf = _rf()
        allowed, mult, reason = rf.gate_strategy(MarketRegime.HIGH_VOL, "DefaultSMC")
        assert not allowed

    def test_trend_blocks_asia(self):
        rf = _rf()
        allowed, mult, reason = rf.gate_strategy(MarketRegime.TREND, "AsiaWeekendStrategy")
        assert not allowed

    def test_range_ok_for_asia(self):
        rf = _rf()
        allowed, mult, reason = rf.gate_strategy(MarketRegime.RANGE, "AsiaWeekendStrategy")
        assert allowed
        assert mult == 1.0

    def test_normal_full_confidence(self):
        rf = _rf()
        allowed, mult, reason = rf.gate_strategy(MarketRegime.TREND, "DefaultSMCStrategy")
        assert allowed
        assert mult == 1.0


class TestBypassDiscipline:
    """Verify FORCE_EXECUTE is in HARD_FILTERS (cannot bypass hard gates)."""

    def test_force_execute_in_hard_filters(self):
        from smc_futures.engine.execution.order_manager import OrderManager
        # KILL_SWITCH, DRAWDOWN_GOVERNOR, ACCOUNT_STALE must be hard
        for gate in ["KILL_SWITCH", "DRAWDOWN_GOVERNOR", "ACCOUNT_STALE", "ACCOUNT_INCOHERENT"]:
            assert gate in OrderManager._HARD_FILTERS, f"{gate} missing from _HARD_FILTERS"

    def test_force_execute_does_not_bypass_kill_switch(self):
        from smc_futures.engine.execution.order_manager import OrderManager
        import smc_futures.engine.execution.order_manager as om_mod
        # Mock Phase 9 risk V2 gate so test reaches kill switch check
        om_mod._evaluate_risk_v2 = lambda **kw: (True, 1.0, [])
        config = {
            "AUTO_EXECUTE_ENABLED": True,
            "TRADING_MODE": "live",
            "FORCE_EXECUTE": True,  # enabled!
        }
        om = OrderManager(config)
        om.engage_kill_switch("TEST_REASON")
        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict={"direction": "long", "entry": 60000, "stop_loss": 59000, "take_profit": 62000},
            signal_id="test-force-ks",
            account_balance=1000.0,
            quantity=0.001,
        )
        assert not ok
        assert "KILL_SWITCH" in reason
