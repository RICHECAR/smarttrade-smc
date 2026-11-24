"""
Tests: ACCOUNT_INCOHERENT broker-isolation fix.

Validates that:
  - A disabled Bitget does NOT block Kraken trades
  - KRAKEN_ONLY mode ignores Bitget state entirely
  - The order router never routes to a disabled broker
  - Position sizing uses only the active broker's account
  - Effective account state tracks the active broker only
  - Multi-broker mode still handles per-broker state
"""

import os
import time
import pytest
from unittest.mock import patch

from smc_futures.risk.account_state import (
    AccountStateService,
    KRAKEN_ONLY,
    BITGET_ONLY,
    MULTI_BROKER,
    resolve_execution_broker_mode,
)


# ════════════════════════════════════════════════════════════════════
# 1. CORE: Bitget disabled does not block Kraken
# ════════════════════════════════════════════════════════════════════

class TestAccountIncoherentBitgetDisabled:

    def test_account_incoherent_bitget_disabled_does_not_block_kraken(self):
        """THE critical bug: Bitget equity=0 must NOT block Kraken trades."""
        svc = AccountStateService(execution_broker_mode=KRAKEN_ONLY)
        # Bitget pushes zero equity (disabled account / API error)
        svc.update(available=0.0, equity=0.0, broker="bitget")
        # Kraken pushes valid equity
        svc.update(available=5000.0, equity=5200.0, broker="kraken")

        # Effective state must reflect Kraken, not Bitget
        assert svc.is_coherent
        assert svc.block_reason == ""
        assert svc.total_equity == 5200.0
        assert svc.available_balance == 5000.0

    def test_kraken_only_mode_ignores_bitget_account_state(self):
        """In KRAKEN_ONLY mode, Bitget state is invisible to gates."""
        svc = AccountStateService(execution_broker_mode=KRAKEN_ONLY)
        # Only Bitget initialized (incoherent)
        svc.update(available=0.0, equity=0.0, broker="bitget")
        # Kraken not initialized → should show NOT_INITIALIZED (Kraken is the gate)
        # But the Bitget incoherent must NOT leak
        assert "INCOHERENT" not in svc.block_reason or svc.active_broker_name == "bitget"

        # Now Kraken initializes healthy
        svc.update(available=1000.0, equity=1000.0, broker="kraken")
        assert svc.block_reason == ""
        assert svc.is_coherent

    def test_bitget_incoherent_kraken_healthy_no_block(self):
        """Bitget is incoherent but Kraken is healthy → no block in KRAKEN_ONLY."""
        svc = AccountStateService(execution_broker_mode=KRAKEN_ONLY)
        svc.update(available=-100.0, equity=-50.0, broker="bitget")  # crazy values
        svc.update(available=2000.0, equity=2500.0, broker="kraken")

        assert svc.block_reason == ""
        assert svc.total_equity == 2500.0

    def test_account_incoherent_blocks_when_kraken_itself_is_invalid(self):
        """If Kraken is active AND incoherent → must block (safety preserved)."""
        svc = AccountStateService(execution_broker_mode=KRAKEN_ONLY)
        svc.update(available=0.0, equity=0.0, broker="kraken")

        assert not svc.is_coherent
        assert "INCOHERENT" in svc.block_reason


# ════════════════════════════════════════════════════════════════════
# 2. effective_account_state uses active broker only
# ════════════════════════════════════════════════════════════════════

class TestEffectiveAccountState:

    def test_effective_account_state_uses_active_broker_only(self):
        """Effective state must track the broker specified by mode."""
        svc = AccountStateService(execution_broker_mode=KRAKEN_ONLY)
        svc.update(available=100.0, equity=100.0, broker="bitget")
        svc.update(available=9999.0, equity=9999.0, broker="kraken")

        assert svc.total_equity == 9999.0
        assert svc.active_broker_name == "kraken"

    def test_effective_bitget_only(self):
        """BITGET_ONLY returns Bitget data."""
        svc = AccountStateService(execution_broker_mode=BITGET_ONLY)
        svc.update(available=500.0, equity=500.0, broker="bitget")
        svc.update(available=9999.0, equity=9999.0, broker="kraken")

        assert svc.total_equity == 500.0
        assert svc.active_broker_name == "bitget"

    def test_per_broker_access(self):
        """broker_state() gives raw per-broker data."""
        svc = AccountStateService(execution_broker_mode=KRAKEN_ONLY)
        svc.update(available=111.0, equity=111.0, broker="bitget")
        svc.update(available=222.0, equity=222.0, broker="kraken")

        assert svc.broker_state("bitget").total_equity == 111.0
        assert svc.broker_state("kraken").total_equity == 222.0

    def test_snapshot_contains_broker_info(self):
        """Snapshot must include execution mode and per-broker breakdown."""
        svc = AccountStateService(execution_broker_mode=KRAKEN_ONLY)
        svc.update(available=1000, equity=1000, broker="kraken")
        snap = svc.get_snapshot()

        assert snap["execution_broker_mode"] == KRAKEN_ONLY
        assert snap["active_broker"] == "kraken"
        assert "per_broker" in snap
        assert "kraken" in snap["per_broker"]
        assert "bitget" in snap["per_broker"]


# ════════════════════════════════════════════════════════════════════
# 3. Multi-broker mode
# ════════════════════════════════════════════════════════════════════

class TestMultiBrokerMode:

    def test_multi_broker_mode_still_handles_broker_specific_state(self):
        """In MULTI_BROKER, each broker has independent state."""
        svc = AccountStateService(execution_broker_mode=MULTI_BROKER)
        svc.update(available=0.0, equity=0.0, broker="bitget")
        svc.update(available=1000.0, equity=1000.0, broker="kraken")

        # Bitget incoherent
        assert not svc.broker_state("bitget").is_coherent
        # Kraken healthy
        assert svc.broker_state("kraken").is_coherent

        # Effective should use whichever is initialized — kraken is healthy
        assert svc.is_coherent

    def test_mode_setter_validation(self):
        """Invalid mode raises ValueError."""
        svc = AccountStateService(execution_broker_mode=KRAKEN_ONLY)
        with pytest.raises(ValueError):
            svc.execution_broker_mode = "INVALID_MODE"


# ════════════════════════════════════════════════════════════════════
# 4. resolve_execution_broker_mode from env
# ════════════════════════════════════════════════════════════════════

class TestResolveMode:

    def test_explicit_env_wins(self):
        with patch.dict(os.environ, {"EXECUTION_BROKER_MODE": "KRAKEN_ONLY"}):
            assert resolve_execution_broker_mode() == KRAKEN_ONLY

    def test_kraken_only_inferred(self):
        with patch.dict(os.environ, {
            "KRAKEN_ENABLED": "true",
            "BITGET_ENABLED": "false",
            "EXECUTION_BROKER_MODE": "",
        }):
            assert resolve_execution_broker_mode() == KRAKEN_ONLY

    def test_bitget_only_inferred(self):
        with patch.dict(os.environ, {
            "KRAKEN_ENABLED": "false",
            "BITGET_ENABLED": "true",
            "EXECUTION_BROKER_MODE": "",
        }):
            assert resolve_execution_broker_mode() == BITGET_ONLY

    def test_both_enabled_gives_multi(self):
        with patch.dict(os.environ, {
            "KRAKEN_ENABLED": "true",
            "BITGET_ENABLED": "true",
            "EXECUTION_BROKER_MODE": "",
        }):
            assert resolve_execution_broker_mode() == MULTI_BROKER


# ════════════════════════════════════════════════════════════════════
# 5. OrderManager gate integration with broker-aware state
# ════════════════════════════════════════════════════════════════════

class TestOrderManagerBrokerGate:
    """Pre-flight checks must respect execution_broker_mode."""

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

    def test_bitget_disabled_not_polled_for_execution_gates(self):
        """Bitget equity=0 in KRAKEN_ONLY mode → Kraken trade NOT blocked."""
        svc = AccountStateService(execution_broker_mode=KRAKEN_ONLY)
        svc.update(available=0.0, equity=0.0, broker="bitget")
        svc.update(available=5000.0, equity=5000.0, broker="kraken")

        om = self._make_om(account_state=svc)
        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=self._signal(),
            signal_id="test-kraken-only-1",
            account_balance=5000.0,
            quantity=0.001,
        )
        assert "ACCOUNT_INCOHERENT" not in reason
        assert "ACCOUNT_STALE" not in reason

    def test_position_sizing_uses_kraken_account_only_in_kraken_mode(self):
        """AccountStateService returns Kraken balance for sizing."""
        svc = AccountStateService(execution_broker_mode=KRAKEN_ONLY)
        svc.update(available=10.0, equity=10.0, broker="bitget")   # dust
        svc.update(available=50000.0, equity=50000.0, broker="kraken")

        # Sizing reads from effective state
        assert svc.available_balance == 50000.0  # NOT 10.0

    def test_incoherent_blocks_when_active_broker_invalid(self):
        """If the active broker (Kraken) itself is invalid → block correctly."""
        svc = AccountStateService(execution_broker_mode=KRAKEN_ONLY)
        svc.update(available=5000.0, equity=5000.0, broker="bitget")  # healthy but irrelevant
        svc.update(available=0.0, equity=0.0, broker="kraken")       # incoherent

        om = self._make_om(account_state=svc)
        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=self._signal(),
            signal_id="test-kraken-incoh-1",
            account_balance=5000.0,
            quantity=0.001,
        )
        assert not ok
        assert "ACCOUNT_INCOHERENT" in reason


# ════════════════════════════════════════════════════════════════════
# 6. Backward compatibility with legacy callers
# ════════════════════════════════════════════════════════════════════

class TestLegacyCompat:
    """Old callers that don't pass broker= must still work."""

    def test_update_without_broker_defaults_bitget(self):
        svc = AccountStateService(execution_broker_mode=BITGET_ONLY)
        svc.update(available=1000.0, equity=1000.0)  # no broker=
        assert svc.total_equity == 1000.0
        assert svc.block_reason == ""

    def test_old_snapshot_keys_present(self):
        svc = AccountStateService()
        svc.update(available=500, equity=500)
        snap = svc.get_snapshot()
        for key in ("equity", "available", "unrealized_pnl", "stale",
                     "coherent", "age_sec", "blocked", "block_reason", "updates"):
            assert key in snap


# ════════════════════════════════════════════════════════════════════
# 7. Integration: full scenario
# ════════════════════════════════════════════════════════════════════

class TestIntegrationKrakenOnly:
    """
    Full scenario:
      - Bitget disabled
      - Kraken active
      - Bitget state incoherent / empty
      - Kraken state valid
      → trade Kraken authorized, no ACCOUNT_INCOHERENT
    """

    def test_full_scenario_bitget_disabled_kraken_active(self):
        svc = AccountStateService(execution_broker_mode=KRAKEN_ONLY)

        # Bitget: never updated (disabled) → its state = NOT_INITIALIZED
        # But since mode=KRAKEN_ONLY, this is irrelevant

        # Simulate: Bitget refresh returned 0 anyway
        svc.update(available=0.0, equity=0.0, broker="bitget")

        # Kraken: healthy
        svc.update(available=3000.0, equity=3200.0, unrealized_pnl=200.0, broker="kraken")

        # Verify: effective state is Kraken
        assert svc.active_broker_name == "kraken"
        assert svc.total_equity == 3200.0
        assert svc.available_balance == 3000.0
        assert svc.is_coherent
        assert svc.block_reason == ""

        # Verify: Bitget is indeed incoherent (per-broker)
        assert not svc.broker_state("bitget").is_coherent

        # Verify: OrderManager does NOT block
        from smc_futures.engine.execution.order_manager import OrderManager
        config = {
            "AUTO_EXECUTE_ENABLED": True,
            "TRADING_MODE": "live",
            "FORCE_EXECUTE": False,
            "MAX_TRADES_PER_DAY": 100,
            "TRADE_COOLDOWN_SEC": 0,
        }
        om = OrderManager(config)
        om.set_account_state(svc)

        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict={
                "direction": "long",
                "entry": 60000,
                "stop_loss": 59000,
                "take_profit": 62000,
            },
            signal_id="integration-test-1",
            account_balance=3000.0,
            quantity=0.001,
        )
        # Must NOT be blocked by account state
        assert "ACCOUNT_INCOHERENT" not in reason
        assert "ACCOUNT_STALE" not in reason
