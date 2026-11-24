"""
Tests for AccountStateService — Chantier 1
"""

import time
import pytest
from smc_futures.risk.account_state import AccountStateService, BITGET_ONLY


class TestAccountStateBasic:
    """Core behaviour: update, read, snapshot."""

    def test_initial_state_not_initialized(self):
        svc = AccountStateService(execution_broker_mode=BITGET_ONLY)
        assert svc.block_reason == "ACCOUNT_NOT_INITIALIZED"
        assert svc.total_equity == 0.0
        assert svc.available_balance == 0.0

    def test_update_clears_block(self):
        svc = AccountStateService(execution_broker_mode=BITGET_ONLY)
        svc.update(available=1000.0, equity=1050.0, unrealized_pnl=50.0)
        assert svc.block_reason == ""
        assert svc.total_equity == 1050.0
        assert svc.available_balance == 1000.0
        assert svc.unrealized_pnl == 50.0

    def test_snapshot_keys(self):
        svc = AccountStateService(execution_broker_mode=BITGET_ONLY)
        svc.update(available=500.0, equity=500.0)
        snap = svc.get_snapshot()
        required_keys = {"equity", "available", "unrealized_pnl", "stale", "coherent",
                         "age_sec", "blocked", "block_reason", "updates"}
        assert required_keys.issubset(snap.keys())
        assert snap["updates"] == 1


class TestStaleness:
    """Guard: stale balance blocks trading."""

    def test_not_stale_after_fresh_update(self):
        svc = AccountStateService(max_staleness_sec=60, execution_broker_mode=BITGET_ONLY)
        svc.update(1000, 1000)
        assert not svc.is_stale
        assert svc.block_reason == ""

    def test_stale_after_timeout(self):
        svc = AccountStateService(max_staleness_sec=0.01, execution_broker_mode=BITGET_ONLY)  # 10ms
        svc.update(1000, 1000)
        time.sleep(0.05)
        assert svc.is_stale
        assert "STALE" in svc.block_reason

    def test_stale_before_any_update(self):
        svc = AccountStateService(execution_broker_mode=BITGET_ONLY)
        assert svc.seconds_since_update == float("inf")
        # Not initialized → block reason is NOT_INITIALIZED, not STALE
        assert "NOT_INITIALIZED" in svc.block_reason


class TestCoherence:
    """Guard: incoherent equity blocks trading."""

    def test_zero_equity_incoherent(self):
        svc = AccountStateService(execution_broker_mode=BITGET_ONLY)
        svc.update(available=0.0, equity=0.0)
        assert not svc.is_coherent
        assert "INCOHERENT" in svc.block_reason

    def test_negative_equity_incoherent(self):
        svc = AccountStateService(execution_broker_mode=BITGET_ONLY)
        svc.update(available=-100.0, equity=-50.0)
        assert not svc.is_coherent
        assert "INCOHERENT" in svc.block_reason

    def test_positive_equity_coherent(self):
        svc = AccountStateService(execution_broker_mode=BITGET_ONLY)
        svc.update(available=100.0, equity=200.0)
        assert svc.is_coherent


class TestOrderManagerGate:
    """Verify the gate blocks in pre_flight_checks."""

    def _make_om(self, account_state=None):
        from smc_futures.engine.execution.order_manager import OrderManager
        config = {
            "AUTO_EXECUTE_ENABLED": True,
            "TRADING_MODE": "live",
            "FORCE_EXECUTE": False,
            "MAX_TRADES_PER_DAY": 100,
            "TRADE_COOLDOWN_SEC": 0,
        }
        om = OrderManager(config)
        if account_state is not None:
            om.set_account_state(account_state)
        return om

    def _signal(self):
        return {
            "direction": "long",
            "entry": 60000,
            "stop_loss": 59000,
            "take_profit": 62000,
        }

    def test_stale_blocks_preflight(self):
        svc = AccountStateService(max_staleness_sec=0.01, execution_broker_mode=BITGET_ONLY)
        svc.update(1000, 1000)
        time.sleep(0.05)
        om = self._make_om(account_state=svc)
        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=self._signal(),
            signal_id="test-stale-1",
            account_balance=1000.0,
            quantity=0.001,
        )
        assert not ok
        assert "ACCOUNT_STALE" in reason

    def test_incoherent_blocks_preflight(self):
        svc = AccountStateService(execution_broker_mode=BITGET_ONLY)
        svc.update(available=0.0, equity=0.0)
        om = self._make_om(account_state=svc)
        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=self._signal(),
            signal_id="test-incoh-1",
            account_balance=1000.0,
            quantity=0.001,
        )
        assert not ok
        assert "ACCOUNT_INCOHERENT" in reason

    def test_healthy_passes_gate(self):
        svc = AccountStateService(execution_broker_mode=BITGET_ONLY)
        svc.update(available=1000, equity=1000)
        om = self._make_om(account_state=svc)
        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=self._signal(),
            signal_id="test-healthy-1",
            account_balance=1000.0,
            quantity=0.001,
        )
        # Should NOT be blocked by account state
        assert "ACCOUNT_STALE" not in reason
        assert "ACCOUNT_INCOHERENT" not in reason
