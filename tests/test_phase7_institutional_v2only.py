"""
Phase 7 Institutional Tests — V2-Only Architecture
═══════════════════════════════════════════════════

Validates:
  1. All flags always True (no env-var gating)
  2. Event bus with dedup rejects duplicates
  3. Risk V2 is sole authority (blocks/reduces trades)
  4. Risk V2 event subscriptions fire on trade events
  5. Reconciliation subscribes to trade lifecycle
  6. Isolation supervisor wraps services
  7. Boot/shutdown lifecycle (no flags needed)
  8. Chaos: service crash → system survives
  9. Chaos: duplicate events → no double processing
  10. Chaos: event bus down → no crash
  11. V1 risk engine is NOT imported in critical path
"""

import asyncio
import os
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from dataclasses import dataclass, field
from typing import List, Dict, Any


# ═══════════════════════════════════════════════════════════════
#  FIXTURES
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clean_singletons():
    """Reset all singletons between tests."""
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
    try:
        from backend.services.trading.risk_engine_v2 import reset_risk_engine_v2
        reset_risk_engine_v2()
    except ImportError:
        pass
    yield


# ═══════════════════════════════════════════════════════════════
#  1. All Flags Always True
# ═══════════════════════════════════════════════════════════════

class TestFlagsAlwaysTrue:
    def test_flags_always_true_no_env(self):
        """Flags are always True — no env vars needed."""
        from core.phase6_wiring import Phase6Flags
        flags = Phase6Flags()
        assert flags.event_bus is True
        assert flags.risk_v2 is True
        assert flags.reconciliation_v2 is True
        assert flags.alert_manager is True
        assert flags.isolation is True
        assert flags.observability is True

    def test_summary_all_true(self):
        from core.phase6_wiring import Phase6Flags
        s = Phase6Flags().summary()
        assert all(v is True for v in s.values())

    def test_init_returns_all_true(self):
        from core.phase6_wiring import init_phase6_flags
        flags = init_phase6_flags()
        assert flags.event_bus is True
        assert flags.risk_v2 is True


# ═══════════════════════════════════════════════════════════════
#  2. Event Bus Dedup
# ═══════════════════════════════════════════════════════════════

class TestEventBusDedup:
    @pytest.mark.asyncio
    async def test_dedup_rejects_same_event_id(self):
        """Publishing the same event_id twice → second is dropped."""
        from core.events.event_bus import get_event_bus
        from core.events.event_types import EventType, SystemEvent

        bus = get_event_bus()
        received = []

        async def _handler(evt):
            received.append(evt)

        bus.subscribe(_handler, {EventType.SYSTEM_HEARTBEAT})
        await bus.start()

        evt = SystemEvent(EventType.SYSTEM_HEARTBEAT, message="ping")
        n1 = bus.publish(evt)
        n2 = bus.publish(evt)  # Same event_id → dedup

        await asyncio.sleep(0.2)
        assert n1 == 1
        assert n2 == 0  # Dedup dropped it
        assert len(received) == 1

        await bus.stop()

    @pytest.mark.asyncio
    async def test_different_ids_both_delivered(self):
        """Different event_ids → both delivered."""
        from core.events.event_bus import get_event_bus
        from core.events.event_types import EventType, SystemEvent

        bus = get_event_bus()
        received = []

        async def _handler(evt):
            received.append(evt)

        bus.subscribe(_handler, {EventType.SYSTEM_HEARTBEAT})
        await bus.start()

        bus.publish(SystemEvent(EventType.SYSTEM_HEARTBEAT, message="a"))
        bus.publish(SystemEvent(EventType.SYSTEM_HEARTBEAT, message="b"))

        await asyncio.sleep(0.2)
        assert len(received) == 2

        await bus.stop()

    def test_dedup_stats_in_get_stats(self):
        """get_stats() reports dedup metrics."""
        from core.events.event_bus import get_event_bus
        from core.events.event_types import EventType, SystemEvent

        bus = get_event_bus()
        evt = SystemEvent(EventType.SYSTEM_HEARTBEAT, message="x")
        bus.publish(evt)
        bus.publish(evt)  # Duplicate

        stats = bus.get_stats()
        assert "dedup_rejected" in stats
        assert stats["dedup_rejected"] == 1


# ═══════════════════════════════════════════════════════════════
#  3. Risk V2 Sole Authority
# ═══════════════════════════════════════════════════════════════

class TestRiskV2SoleAuthority:
    def test_evaluate_blocks_trade(self):
        """Risk V2 BLOCK verdict."""
        from core.phase6_wiring import evaluate_risk_v2
        import core.phase6_wiring as wiring

        @dataclass
        class MockVerdict:
            allowed: bool = False
            action: str = "BLOCK"
            size_multiplier: float = 0.0
            reasons: List[str] = field(default_factory=lambda: ["daily_loss_exceeded"])
            checks: Dict[str, Any] = field(default_factory=dict)

        mock_v2 = MagicMock()
        mock_v2.evaluate.return_value = MockVerdict()
        mock_sup = MagicMock()
        mock_sup.is_trading_allowed.return_value = (True, "healthy")
        old_sup = wiring._supervisor
        wiring._risk_v2 = mock_v2
        wiring._supervisor = mock_sup
        try:
            allowed, mult, reasons = evaluate_risk_v2("BTCUSDT", "long", 5000, 60000)
            assert allowed is False
            assert "daily_loss_exceeded" in reasons
        finally:
            wiring._supervisor = old_sup

    def test_evaluate_reduces_size(self):
        """Risk V2 REDUCE verdict."""
        from core.phase6_wiring import evaluate_risk_v2
        import core.phase6_wiring as wiring

        @dataclass
        class MockVerdict:
            allowed: bool = True
            action: str = "REDUCE"
            size_multiplier: float = 0.5
            reasons: List[str] = field(default_factory=lambda: ["exposure_near_limit"])
            checks: Dict[str, Any] = field(default_factory=dict)

        mock_v2 = MagicMock()
        mock_v2.evaluate.return_value = MockVerdict()
        mock_sup = MagicMock()
        mock_sup.is_trading_allowed.return_value = (True, "healthy")
        old_sup = wiring._supervisor
        wiring._risk_v2 = mock_v2
        wiring._supervisor = mock_sup
        try:
            allowed, mult, reasons = evaluate_risk_v2("ETHUSDT", "short", 3000, 3500)
            assert allowed is True
            assert mult == 0.5
        finally:
            wiring._supervisor = old_sup

    def test_evaluate_error_fails_open(self):
        """Risk V2 error → trade DENIED (Phase 9: fail-closed for safety)."""
        from core.phase6_wiring import evaluate_risk_v2
        import core.phase6_wiring as wiring

        mock_v2 = MagicMock()
        mock_v2.evaluate.side_effect = RuntimeError("connection lost")
        mock_sup = MagicMock()
        mock_sup.is_trading_allowed.return_value = (True, "healthy")
        old_sup = wiring._supervisor
        wiring._risk_v2 = mock_v2
        wiring._supervisor = mock_sup
        try:
            allowed, mult, reasons = evaluate_risk_v2("BTCUSDT", "long", 1000, 60000)
            assert allowed is False
            assert any("risk_v2_error" in r for r in reasons)
        finally:
            wiring._supervisor = old_sup


# ═══════════════════════════════════════════════════════════════
#  4. Risk V2 Event Subscriptions
# ═══════════════════════════════════════════════════════════════

class TestRiskV2EventSubscriptions:
    @pytest.mark.asyncio
    async def test_trade_closed_updates_v2(self):
        """TRADE_CLOSED event → Risk V2 records trade result."""
        from core.events.event_bus import get_event_bus
        from core.events.event_types import EventType, TradeEvent
        import core.phase6_wiring as wiring

        bus = get_event_bus()
        wiring._event_bus = bus

        mock_v2 = MagicMock()
        wiring._risk_v2 = mock_v2

        wiring._wire_risk_v2_subscriptions()
        await bus.start()

        bus.publish(TradeEvent(
            EventType.TRADE_CLOSED,
            symbol="BTCUSDT", broker="bitget",
            side="long", price=61000, pnl=150.0,
        ))

        await asyncio.sleep(0.3)
        mock_v2.record_trade_result.assert_called_once_with("BTCUSDT", 150.0, True)

        await bus.stop()

    @pytest.mark.asyncio
    async def test_order_filled_updates_position(self):
        """ORDER_FILLED event → Risk V2 updates position tracking."""
        from core.events.event_bus import get_event_bus
        from core.events.event_types import EventType, TradeEvent
        import core.phase6_wiring as wiring

        bus = get_event_bus()
        wiring._event_bus = bus

        mock_v2 = MagicMock()
        wiring._risk_v2 = mock_v2

        wiring._wire_risk_v2_subscriptions()
        await bus.start()

        bus.publish(TradeEvent(
            EventType.ORDER_FILLED,
            symbol="ETHUSDT", broker="bitget",
            side="short", price=3500, qty=0.5,
        ))

        await asyncio.sleep(0.3)
        mock_v2.update_position.assert_called_once()
        call_args = mock_v2.update_position.call_args
        assert call_args[0][0] == "ETHUSDT"

        await bus.stop()


# ═══════════════════════════════════════════════════════════════
#  5. Reconciliation Event Subscriptions
# ═══════════════════════════════════════════════════════════════

class TestReconciliationSubscriptions:
    @pytest.mark.asyncio
    async def test_trade_events_trigger_reconciliation(self):
        """TRADE_OPENED/CLOSED → reconciliation check requested."""
        from core.events.event_bus import get_event_bus
        from core.events.event_types import EventType, TradeEvent
        import core.phase6_wiring as wiring

        bus = get_event_bus()
        wiring._event_bus = bus

        mock_recon = MagicMock()
        mock_recon.request_check = MagicMock()
        wiring._reconciliation_v2 = mock_recon

        wiring._wire_reconciliation_subscriptions()
        await bus.start()

        bus.publish(TradeEvent(
            EventType.TRADE_OPENED,
            symbol="BTCUSDT", broker="bitget", side="long", price=60000,
        ))

        await asyncio.sleep(0.3)
        # If reconciliation has request_check method, it should have been called
        if hasattr(mock_recon, 'request_check'):
            mock_recon.request_check.assert_called()

        await bus.stop()


# ═══════════════════════════════════════════════════════════════
#  6. Isolation Supervisor
# ═══════════════════════════════════════════════════════════════

class TestIsolationSupervisor:
    @pytest.mark.asyncio
    async def test_supervisor_wraps_service(self):
        """Services can be wrapped and supervised."""
        from core.isolation import ServiceSupervisor, ServiceState

        supervisor = ServiceSupervisor()
        call_count = 0

        async def dummy_service():
            nonlocal call_count
            call_count += 1
            # Service runs once then exits cleanly
            await asyncio.sleep(0.1)

        supervisor.register("test_svc", dummy_service, max_restarts=3)
        await supervisor.start_all()
        await asyncio.sleep(0.3)
        await supervisor.stop_all()

        assert call_count >= 1
        status = supervisor.get_status()
        assert "test_svc" in status.get("services", status)


# ═══════════════════════════════════════════════════════════════
#  7. Boot/Shutdown Lifecycle (no flags needed)
# ═══════════════════════════════════════════════════════════════

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_shutdown_no_crash(self):
        """Shutdown with nothing started → no crash."""
        from core.phase6_wiring import shutdown_phase6
        await shutdown_phase6()

    def test_publish_no_bus_returns_zero(self):
        """publish_event returns 0 if bus not yet started."""
        from core.phase6_wiring import publish_event
        from core.events.event_types import SystemEvent, EventType
        evt = SystemEvent(EventType.SYSTEM_HEARTBEAT, message="test")
        assert publish_event(evt) == 0


# ═══════════════════════════════════════════════════════════════
#  8. CHAOS: Service Crash → System Survives
# ═══════════════════════════════════════════════════════════════

class TestChaosServiceCrash:
    @pytest.mark.asyncio
    async def test_service_crash_and_restart(self):
        """A crashing service is automatically restarted by supervisor."""
        from core.isolation import ServiceWrapper, ServiceState

        crash_count = 0

        async def crashing_service():
            nonlocal crash_count
            crash_count += 1
            if crash_count <= 2:
                raise RuntimeError(f"Simulated crash #{crash_count}")
            # Third attempt succeeds
            await asyncio.sleep(0.2)

        wrapper = ServiceWrapper(
            "crash_test",
            crashing_service,
            max_restarts=5,
            restart_delay_sec=0.1,
            max_restart_delay_sec=0.2,
        )

        await wrapper.start()
        await asyncio.sleep(1.0)
        await wrapper.stop()

        assert crash_count == 3  # 2 crashes + 1 success
        assert wrapper.stats.crash_count == 2

    @pytest.mark.asyncio
    async def test_max_restarts_exceeded(self):
        """Service that always crashes → stops after max_restarts."""
        from core.isolation import ServiceWrapper, ServiceState

        async def always_crash():
            raise RuntimeError("permanent failure")

        wrapper = ServiceWrapper(
            "permanent_fail",
            always_crash,
            max_restarts=2,
            restart_delay_sec=0.05,
            max_restart_delay_sec=0.1,
        )

        await wrapper.start()
        await asyncio.sleep(1.0)

        assert wrapper.stats.crash_count >= 2
        assert wrapper.state in (ServiceState.CRASHED, ServiceState.STOPPED)


# ═══════════════════════════════════════════════════════════════
#  9. CHAOS: Duplicate Events → No Double Processing
# ═══════════════════════════════════════════════════════════════

class TestChaosDuplicateEvents:
    @pytest.mark.asyncio
    async def test_duplicate_event_not_double_processed(self):
        """Same event published 3 times → handler called only once."""
        from core.events.event_bus import get_event_bus
        from core.events.event_types import EventType, TradeEvent

        bus = get_event_bus()
        call_count = 0

        async def handler(evt):
            nonlocal call_count
            call_count += 1

        bus.subscribe(handler, {EventType.TRADE_OPENED})
        await bus.start()

        evt = TradeEvent(
            EventType.TRADE_OPENED,
            symbol="BTCUSDT", broker="bitget", side="long", price=60000,
        )
        bus.publish(evt)
        bus.publish(evt)  # Duplicate
        bus.publish(evt)  # Duplicate

        await asyncio.sleep(0.3)
        assert call_count == 1  # Dedup worked

        await bus.stop()


# ═══════════════════════════════════════════════════════════════
#  10. CHAOS: Event Bus Down → No Crash
# ═══════════════════════════════════════════════════════════════

class TestChaosEventBusDown:
    def test_publish_without_bus_no_crash(self):
        """Publishing events with no bus → returns 0, no exception."""
        from core.phase6_wiring import publish_event
        from core.events.event_types import SystemEvent, EventType

        # Bus is None (not started)
        result = publish_event(SystemEvent(EventType.SYSTEM_ERROR, message="test"))
        assert result == 0

    def test_risk_v2_evaluate_without_bus(self):
        """Risk V2 can evaluate even if event bus fails."""
        from core.phase6_wiring import evaluate_risk_v2
        import core.phase6_wiring as wiring

        @dataclass
        class MockVerdict:
            allowed: bool = True
            action: str = "ALLOW"
            size_multiplier: float = 1.0
            reasons: List[str] = field(default_factory=list)
            checks: Dict[str, Any] = field(default_factory=dict)

        mock_v2 = MagicMock()
        mock_v2.evaluate.return_value = MockVerdict()
        mock_sup = MagicMock()
        mock_sup.is_trading_allowed.return_value = (True, "healthy")
        old_sup = wiring._supervisor
        wiring._risk_v2 = mock_v2
        wiring._supervisor = mock_sup
        wiring._event_bus = None  # Bus is down
        try:
            allowed, mult, reasons = evaluate_risk_v2("BTCUSDT", "long", 1000, 60000)
            assert allowed is True
        finally:
            wiring._supervisor = old_sup


# ═══════════════════════════════════════════════════════════════
#  11. V1 Not in Critical Path
# ═══════════════════════════════════════════════════════════════

class TestV1Removed:
    def test_order_manager_no_v1_import(self):
        """order_manager does not import V1 global_risk_engine."""
        import importlib
        import smc_futures.engine.execution.order_manager as om
        src = importlib.util.find_spec(om.__name__)
        if src and src.origin:
            with open(src.origin, "r", encoding="utf-8") as f:
                content = f.read()
            assert "from smc_futures.engine.risk.global_risk_engine import risk_engine" not in content

    def test_strict_oms_no_v1_import(self):
        """strict_oms does not import V1 global_risk_engine."""
        import importlib
        import smc_futures.engine.execution.strict_oms as oms
        src = importlib.util.find_spec(oms.__name__)
        if src and src.origin:
            with open(src.origin, "r", encoding="utf-8") as f:
                content = f.read()
            assert "from smc_futures.engine.risk.global_risk_engine import risk_engine" not in content

    def test_asymmetric_oms_no_v1_import(self):
        """asymmetric_oms does not import V1 global_risk_engine."""
        import importlib
        import smc_futures.engine.execution.asymmetric_oms as oms
        src = importlib.util.find_spec(oms.__name__)
        if src and src.origin:
            with open(src.origin, "r", encoding="utf-8") as f:
                content = f.read()
            assert "from smc_futures.engine.risk.global_risk_engine import risk_engine" not in content

    def test_api_smc_engine_no_v1_reconciler(self):
        """api_smc_engine does not import PositionSyncService."""
        import importlib
        import smc_futures.api_smc_engine as api
        src = importlib.util.find_spec(api.__name__)
        if src and src.origin:
            with open(src.origin, "r", encoding="utf-8") as f:
                content = f.read()
            assert "from backend.services.trade_reconciler import PositionSyncService" not in content


# ═══════════════════════════════════════════════════════════════
#  12. Dead Letter Queue
# ═══════════════════════════════════════════════════════════════

class TestDeadLetterQueue:
    @pytest.mark.asyncio
    async def test_failing_subscriber_goes_to_dead_letter(self):
        """Subscriber that raises → event goes to dead-letter queue."""
        from core.events.event_bus import get_event_bus
        from core.events.event_types import EventType, SystemEvent

        bus = get_event_bus()

        async def failing_handler(evt):
            raise ValueError("handler crashed")

        bus.subscribe(failing_handler, {EventType.SYSTEM_HEARTBEAT}, name="failing_sub")
        await bus.start()

        bus.publish(SystemEvent(EventType.SYSTEM_HEARTBEAT, message="fail"))
        await asyncio.sleep(0.3)

        dead = bus.get_dead_letters()
        assert len(dead) >= 1
        assert dead[0]["subscriber"] == "failing_sub"
        assert "handler crashed" in dead[0]["error"]

        await bus.stop()

    @pytest.mark.asyncio
    async def test_drain_dead_letters(self):
        """drain_dead_letters clears the queue."""
        from core.events.event_bus import get_event_bus
        from core.events.event_types import EventType, SystemEvent

        bus = get_event_bus()

        async def failing_handler(evt):
            raise ValueError("crash")

        bus.subscribe(failing_handler, {EventType.SYSTEM_HEARTBEAT}, name="drain_test")
        await bus.start()

        bus.publish(SystemEvent(EventType.SYSTEM_HEARTBEAT, message="x"))
        await asyncio.sleep(0.3)

        drained = bus.drain_dead_letters()
        assert len(drained) >= 1
        assert len(bus.get_dead_letters()) == 0

        await bus.stop()
