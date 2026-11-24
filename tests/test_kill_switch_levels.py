"""
Tests for Kill Switch L1/L2/L3 — Chantier 3
"""

import asyncio
import pytest
from datetime import date
from smc_futures.risk.kill_switch import RiskKillSwitch, KillSwitchLevel


def _ks(**overrides):
    defaults = dict(
        max_daily_loss_pct=2.0,
        max_daily_loss_abs=0.0,
        max_session_loss_pct_asia=1.0,
        max_consecutive_losses_asia=6,
        l2_loss_pct=1.5,
        l2_risk_mult=0.5,
        l3_loss_pct=3.0,
    )
    defaults.update(overrides)
    return RiskKillSwitch(**defaults)


class TestL1Original:
    """L1 block entries — backward compatible with old tests."""

    def test_no_block_when_fine(self):
        ks = _ks()
        blocked, _ = ks.is_blocked("london", date.today(), -5, -3, 1000)
        assert not blocked

    def test_block_on_daily_loss_pct(self):
        ks = _ks(max_daily_loss_pct=2.0)
        blocked, reason = ks.is_blocked("london", date.today(), -25, 0, 1000)
        assert blocked
        assert "DAILY_LOSS_PCT" in reason
        assert ks.level == KillSwitchLevel.L1_BLOCK_ENTRIES

    def test_block_on_daily_loss_abs(self):
        # Use a very small loss that triggers abs but not L3 (l3=3%=30)
        ks = _ks(max_daily_loss_abs=10, l3_loss_pct=50.0)
        blocked, reason = ks.is_blocked("london", date.today(), -15, 0, 1000)
        assert blocked
        assert "DAILY_LOSS_ABS" in reason

    def test_block_asia_consecutive(self):
        ks = _ks(max_consecutive_losses_asia=3)
        blocked, reason = ks.is_blocked("asia", date.today(), -5, -5, 1000, consecutive_losses=4)
        assert blocked
        assert "CONSECUTIVE_LOSSES_ASIA" in reason


class TestL2ReduceRisk:
    """L2 reduces risk multiplier but does NOT block."""

    def test_l2_active_when_loss_exceeds_threshold(self):
        ks = _ks(l2_loss_pct=1.0, max_daily_loss_pct=5.0)
        # Daily loss = 15/1000 = 1.5% → above l2 (1.0%) but below L1 (5.0%)
        blocked, _ = ks.is_blocked("london", date.today(), -15, 0, 1000)
        assert not blocked  # L2 does NOT block
        assert ks.level == KillSwitchLevel.L2_REDUCE_RISK
        assert ks.risk_multiplier == 0.5

    def test_l2_risk_mult_configurable(self):
        ks = _ks(l2_loss_pct=1.0, l2_risk_mult=0.3, max_daily_loss_pct=5.0)
        ks.is_blocked("london", date.today(), -15, 0, 1000)
        assert ks.risk_multiplier == 0.3


class TestL3ForceClose:
    """L3 blocks AND triggers force close callback."""

    def test_l3_triggers_on_threshold(self):
        ks = _ks(l3_loss_pct=3.0)
        # 35/1000 = 3.5% ≥ 3.0%
        blocked, reason = ks.is_blocked("london", date.today(), -35, 0, 1000)
        assert blocked
        assert "L3_FORCE_CLOSE" in reason
        assert ks.level == KillSwitchLevel.L3_FORCE_CLOSE
        assert ks.risk_multiplier == 0.0

    def test_l3_force_close_callback_called(self):
        ks = _ks(l3_loss_pct=3.0)
        close_called = []

        async def mock_close():
            close_called.append(True)

        ks.set_force_close_callback(mock_close)

        # Trigger L3
        ks.is_blocked("london", date.today(), -35, 0, 1000)

        # Call force_close_all
        result = asyncio.run(ks.force_close_all("TEST_L3"))
        assert result is True
        assert len(close_called) == 1

    def test_l3_idempotent(self):
        ks = _ks(l3_loss_pct=3.0)
        call_count = []

        async def mock_close():
            call_count.append(1)

        ks.set_force_close_callback(mock_close)

        async def run_all():
            await ks.force_close_all("R1")
            await ks.force_close_all("R2")
            await ks.force_close_all("R3")

        asyncio.run(run_all())
        # Only called once due to idempotence
        assert len(call_count) == 1

    def test_l3_no_callback_returns_false(self):
        ks = _ks(l3_loss_pct=3.0)
        # No callback set
        result = asyncio.run(ks.force_close_all("NO_CB"))
        assert result is False

    def test_reset_allows_new_force_close(self):
        ks = _ks(l3_loss_pct=3.0)
        call_count = []

        async def mock_close():
            call_count.append(1)

        ks.set_force_close_callback(mock_close)

        asyncio.run(ks.force_close_all("R1"))
        assert len(call_count) == 1

        ks.reset_force_close()
        asyncio.run(ks.force_close_all("R2"))
        assert len(call_count) == 2


class TestFromConfig:
    """Factory method backward compat."""

    def test_from_config_defaults(self):
        ks = RiskKillSwitch.from_config({})
        assert ks.max_daily_loss_pct == 2.0
        assert ks.l2_loss_pct == 1.5
        assert ks.l3_loss_pct == 3.0

    def test_from_config_custom(self):
        ks = RiskKillSwitch.from_config({
            "KILL_SWITCH_L2_LOSS_PCT": 1.0,
            "KILL_SWITCH_L3_LOSS_PCT": 2.5,
        })
        assert ks.l2_loss_pct == 1.0
        assert ks.l3_loss_pct == 2.5
