"""
Phase 6 Wiring Integration Tests
═════════════════════════════════

Tests proving that Phase 6 modules are ACTUALLY wired into the runtime,
not just imported as dead code.

Test matrix:
  1. Feature flags ON → modules start
  2. Feature flags OFF → clean bypass (no crash)
  3. Risk V2 blocks trade in pre_flight_checks
  4. Risk V2 reduces sizing in pre_flight_checks
  5. Event bus receives TRADE_OPENED after record_fill
  6. Event bus receives SIGNAL_GENERATED from orchestrator
  7. Event bus down → no crash (fallback)
  8. Risk V2 error → trade allowed (fail-open)
  9. Boot + shutdown lifecycle
"""

import asyncio
import os
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ═══════════════════════════════════════════════════════════════
#  FIXTURES
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Reset singletons between tests."""
    # Reset singletons between tests
    import core.phase6_wiring as wiring
    wiring._event_bus = None
    wiring._risk_v2 = None
    wiring._alert_manager = None
    wiring._reconciliation_v2 = None
    wiring._supervisor = None
    wiring._health_aggregator = None
    try:
        from core.events.event_bus import reset_event_bus
        reset_event_bus()
    except ImportError:
        pass


@pytest.fixture
def enable_all_flags(monkeypatch):
    """Legacy compat fixture — flags are always on now."""


# ═══════════════════════════════════════════════════════════════
#  1. Feature Flags
# ═══════════════════════════════════════════════════════════════

class TestFeatureFlags:
    def test_all_always_true(self):
        """All flags are always True (V2-only architecture)."""
        from core.phase6_wiring import Phase6Flags
        flags = Phase6Flags()
        assert flags.event_bus is True
        assert flags.risk_v2 is True
        assert flags.reconciliation_v2 is True
        assert flags.alert_manager is True
        assert flags.isolation is True
        assert flags.observability is True

    def test_flags_on(self, enable_all_flags):
        """Flags are always True regardless of env."""
        from core.phase6_wiring import Phase6Flags
        flags = Phase6Flags()
        assert flags.event_bus is True
        assert flags.risk_v2 is True

    def test_init_phase6_flags_returns_flags(self):
        from core.phase6_wiring import init_phase6_flags
        flags = init_phase6_flags()
        assert hasattr(flags, "event_bus")
        assert hasattr(flags, "risk_v2")
        assert isinstance(flags.summary(), dict)


# ═══════════════════════════════════════════════════════════════
#  2. Event Bus Wiring
# ═══════════════════════════════════════════════════════════════

class TestEventBusWiring:
    def test_publish_no_bus_returns_zero(self):
        """If bus is down/disabled, publish_event returns 0 (no crash)."""
        from core.phase6_wiring import publish_event
        from core.events.event_types import SystemEvent, EventType
        evt = SystemEvent(EventType.SYSTEM_HEARTBEAT, message="test")
        result = publish_event(evt)
        assert result == 0

    @pytest.mark.asyncio
    async def test_bus_starts_and_receives_events(self, monkeypatch):
        """Event bus starts and actually receives published events."""
        from core.events.event_bus import get_event_bus
        from core.events.event_types import EventType, SystemEvent

        bus = get_event_bus()
        received = []

        async def _handler(evt):
            received.append(evt)

        bus.subscribe(_handler, {EventType.SYSTEM_HEARTBEAT})
        await bus.start()

        evt = SystemEvent(EventType.SYSTEM_HEARTBEAT, message="ping")
        bus.publish(evt)

        await asyncio.sleep(0.2)
        assert len(received) >= 1
        assert received[0].payload["message"] == "ping"

        await bus.stop()


# ═══════════════════════════════════════════════════════════════
#  3. Risk V2 Integration (Critical Path)
# ═══════════════════════════════════════════════════════════════

class TestRiskV2CriticalPath:
    def test_evaluate_disabled_allows(self):
        """Risk V2 disabled → DENIES trade (Phase 9 safety: no trading without risk check)."""
        from core.phase6_wiring import evaluate_risk_v2
        import core.phase6_wiring as wiring
        old = wiring._risk_v2
        old_sup = wiring._supervisor
        wiring._risk_v2 = None
        mock_sup = MagicMock()
        mock_sup.is_trading_allowed.return_value = (True, "healthy")
        wiring._supervisor = mock_sup
        try:
            allowed, mult, reasons = evaluate_risk_v2("BTCUSDT", "long", 1000, 60000)
            assert allowed is False
            assert mult == 0.0
            assert "risk_engine_unavailable" in reasons
        finally:
            wiring._risk_v2 = old
            wiring._supervisor = old_sup

    def test_evaluate_blocks_trade(self):
        """Risk V2 enabled → can BLOCK a trade."""
        from core.phase6_wiring import evaluate_risk_v2
        import core.phase6_wiring as wiring
        from dataclasses import dataclass, field
        from typing import List, Dict, Any

        @dataclass
        class MockVerdict:
            allowed: bool
            action: str
            size_multiplier: float = 1.0
            reasons: List[str] = field(default_factory=list)
            checks: Dict[str, Any] = field(default_factory=dict)

        mock_v2 = MagicMock()
        mock_v2.evaluate.return_value = MockVerdict(
            allowed=False,
            action="BLOCK",
            size_multiplier=0.0,
            reasons=["daily_loss_exceeded", "max_correlation"],
        )

        old = wiring._risk_v2
        old_sup = wiring._supervisor
        wiring._risk_v2 = mock_v2
        mock_sup = MagicMock()
        mock_sup.is_trading_allowed.return_value = (True, "healthy")
        wiring._supervisor = mock_sup
        try:
            allowed, mult, reasons = evaluate_risk_v2("BTCUSDT", "long", 5000, 60000)
            assert allowed is False
            assert "daily_loss_exceeded" in reasons
            mock_v2.evaluate.assert_called_once()
        finally:
            wiring._risk_v2 = old
            wiring._supervisor = old_sup

    def test_evaluate_reduces_size(self):
        """Risk V2 enabled → can REDUCE position size."""
        from core.phase6_wiring import evaluate_risk_v2
        import core.phase6_wiring as wiring
        from dataclasses import dataclass, field
        from typing import List, Dict, Any

        @dataclass
        class MockVerdict:
            allowed: bool
            action: str
            size_multiplier: float = 1.0
            reasons: List[str] = field(default_factory=list)
            checks: Dict[str, Any] = field(default_factory=dict)

        mock_v2 = MagicMock()
        mock_v2.evaluate.return_value = MockVerdict(
            allowed=True,
            action="REDUCE",
            size_multiplier=0.5,
            reasons=["high_exposure"],
        )

        old = wiring._risk_v2
        old_sup = wiring._supervisor
        wiring._risk_v2 = mock_v2
        mock_sup = MagicMock()
        mock_sup.is_trading_allowed.return_value = (True, "healthy")
        wiring._supervisor = mock_sup
        try:
            allowed, mult, reasons = evaluate_risk_v2("ETHUSDT", "short", 3000, 3500)
            assert allowed is True
            assert mult == 0.5
            assert "high_exposure" in reasons
        finally:
            wiring._risk_v2 = old
            wiring._supervisor = old_sup

    def test_evaluate_error_fails_open(self):
        """Risk V2 error → trade DENIED (Phase 9: fail-closed for safety)."""
        from core.phase6_wiring import evaluate_risk_v2
        import core.phase6_wiring as wiring

        mock_v2 = MagicMock()
        mock_v2.evaluate.side_effect = RuntimeError("Redis connection lost")

        old = wiring._risk_v2
        old_sup = wiring._supervisor
        wiring._risk_v2 = mock_v2
        mock_sup = MagicMock()
        mock_sup.is_trading_allowed.return_value = (True, "healthy")
        wiring._supervisor = mock_sup
        try:
            allowed, mult, reasons = evaluate_risk_v2("BTCUSDT", "long", 1000, 60000)
            assert allowed is False
            assert mult == 0.0
            assert any("risk_v2_error" in r for r in reasons)
        finally:
            wiring._risk_v2 = old
            wiring._supervisor = old_sup


# ═══════════════════════════════════════════════════════════════
#  4. Risk V2 in OrderManager pre_flight_checks
# ═══════════════════════════════════════════════════════════════

class TestRiskV2InOrderManager:
    def test_risk_v2_import_exists(self):
        """Verify the Phase 6 wiring import is accessible from order_manager."""
        import smc_futures.engine.execution.order_manager as om_mod
        assert hasattr(om_mod, "_phase6_wiring_available")

    def test_risk_v2_gate_blocks_in_preflight(self, monkeypatch):
        """Risk V2 BLOCK verdict rejects trade in pre_flight_checks."""
        import smc_futures.engine.execution.order_manager as om_mod

        # Patch the wiring availability flag
        monkeypatch.setattr(om_mod, "_phase6_wiring_available", True)

        # Patch the evaluate function to return BLOCK
        monkeypatch.setattr(
            om_mod, "_evaluate_risk_v2",
            lambda **kw: (False, 0.0, ["test_daily_loss_exceeded"]),
        )

        from smc_futures.engine.execution.order_manager import OrderManager
        mgr = OrderManager(config={
            "AUTO_EXECUTE_ENABLED": True,
            "TRADING_MODE": "live",
            "RISK_PER_TRADE_PCT": 0.01,
            "MAX_TRADES_PER_DAY": 10,
            "TRADE_COOLDOWN_SEC": 0,
        })

        signal = {
            "direction": "long",
            "entry": 60000,
            "stop_loss": 59000,
            "take_profit": 62000,
            "t_signal": time.time(),
        }

        ok, reason, details = mgr.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=signal,
            signal_id="test_risk_v2_block",
            active_trade=None,
            account_balance=10000,
            quantity=0.001,
        )

        assert not ok
        assert "RISK_V2_BLOCKED" in reason


# ═══════════════════════════════════════════════════════════════
#  5. Event Publishing from OrderManager
# ═══════════════════════════════════════════════════════════════

class TestEventPublishingFromOrderManager:
    def test_record_fill_publishes_trade_opened(self, monkeypatch):
        """record_fill() publishes TRADE_OPENED event when wiring available."""
        import smc_futures.engine.execution.order_manager as om_mod
        monkeypatch.setattr(om_mod, "_phase6_wiring_available", True)

        published = []
        monkeypatch.setattr(om_mod, "_p6_publish", lambda e: published.append(e) or 1)

        from smc_futures.engine.execution.order_manager import OrderManager
        mgr = OrderManager(config={
            "MAX_TRADES_PER_DAY": 10,
            "RISK_PER_TRADE_PCT": 0.01,
        })

        signal = {"direction": "long", "entry": 60000}
        exec_result = {"price_executed": 60100, "quantity": 0.001, "entry_order_id": "ORD123", "timestamp": "2025-01-01T00:00:00"}

        mgr.record_fill("BTCUSDT", signal, exec_result, "sig_001")

        assert len(published) == 1
        evt = published[0]
        assert evt.event_type.value == "trade.opened"
        assert evt.payload["symbol"] == "BTCUSDT"
        assert evt.payload["side"] == "long"


# ═══════════════════════════════════════════════════════════════
#  6. Boot / Shutdown Lifecycle
# ═══════════════════════════════════════════════════════════════

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_boot_all_modules_start(self):
        """Boot starts all modules (V2-only, no flags)."""
        from core.phase6_wiring import boot_phase6, shutdown_phase6
        import core.phase6_wiring as wiring

        results = await boot_phase6(initial_capital=10000)
        # Event bus and risk_v2 should start
        assert results["event_bus"] is True
        assert results["risk_v2"] is True

        await shutdown_phase6()

    @pytest.mark.asyncio
    async def test_shutdown_no_crash(self):
        """Shutdown with nothing started → no crash."""
        from core.phase6_wiring import shutdown_phase6
        await shutdown_phase6()  # Must not raise

    @pytest.mark.asyncio
    async def test_boot_and_shutdown(self, monkeypatch):
        """Boot + shutdown cycle works cleanly."""
        from core.phase6_wiring import boot_phase6, shutdown_phase6
        import core.phase6_wiring as wiring

        results = await boot_phase6(initial_capital=5000)
        assert results["event_bus"] is True
        assert results["risk_v2"] is True

        # Cleanup
        await shutdown_phase6()
        wiring._event_bus = None


# ═══════════════════════════════════════════════════════════════
#  7. Orchestrator Wiring
# ═══════════════════════════════════════════════════════════════

class TestOrchestratorWiring:
    def test_phase6_import_accessible(self):
        """Orchestrator has Phase 6 wiring imported."""
        import smc_futures.orchestrator as orch_mod
        assert hasattr(orch_mod, "_p6_wiring_ok")
        assert hasattr(orch_mod, "_p6_publish")


# ═══════════════════════════════════════════════════════════════
#  8. Risk V2 Trade Result Recording
# ═══════════════════════════════════════════════════════════════

class TestRiskV2TradeResult:
    def test_record_disabled_no_crash(self):
        """record_risk_v2_trade_result does nothing when V2 disabled."""
        from core.phase6_wiring import record_risk_v2_trade_result
        import core.phase6_wiring as wiring
        old = wiring._risk_v2
        wiring._risk_v2 = None
        try:
            record_risk_v2_trade_result("BTCUSDT", 150.0, True)  # Must not raise
        finally:
            wiring._risk_v2 = old

    def test_record_calls_v2(self):
        """record_risk_v2_trade_result calls Risk V2 when available."""
        from core.phase6_wiring import record_risk_v2_trade_result
        import core.phase6_wiring as wiring
        mock_v2 = MagicMock()
        old = wiring._risk_v2
        wiring._risk_v2 = mock_v2
        try:
            record_risk_v2_trade_result("ETHUSDT", -50.0, False)
            mock_v2.record_trade_result.assert_called_once_with("ETHUSDT", -50.0, False)
        finally:
            wiring._risk_v2 = old


# ═══════════════════════════════════════════════════════════════
#  9. Supervisor Status
# ═══════════════════════════════════════════════════════════════

class TestSupervisorStatus:
    def test_status_disabled(self):
        from core.phase6_wiring import get_supervisor_status
        status = get_supervisor_status()
        assert status["enabled"] is False
