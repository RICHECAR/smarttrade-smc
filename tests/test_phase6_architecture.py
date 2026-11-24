"""
Phase 6 Tests — Event Bus, Reconciliation V2, Risk V2, Isolation, Observability.
═══════════════════════════════════════════════════════════════════════════════════

Institutional-grade test coverage for the Phase 6 architecture transformation.
"""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ═══════════════════════════════════════════════════════════════
#  1. EVENT BUS TESTS
# ═══════════════════════════════════════════════════════════════

class TestEventBus:
    """Event bus — publish, subscribe, priority, dead-letter, lifecycle."""

    def setup_method(self):
        from core.events.event_bus import reset_event_bus
        reset_event_bus()

    def teardown_method(self):
        from core.events.event_bus import reset_event_bus
        reset_event_bus()

    def test_singleton_returns_same_instance(self):
        from core.events.event_bus import get_event_bus
        bus1 = get_event_bus()
        bus2 = get_event_bus()
        assert bus1 is bus2

    def test_reset_creates_new_instance(self):
        from core.events.event_bus import get_event_bus, reset_event_bus
        bus1 = get_event_bus()
        reset_event_bus()
        bus2 = get_event_bus()
        assert bus1 is not bus2

    @pytest.mark.asyncio
    async def test_publish_subscribe_basic(self):
        from core.events.event_bus import get_event_bus
        from core.events.event_types import Event, EventType, EventPriority

        bus = get_event_bus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(handler, {EventType.TRADE_OPENED}, name="test_sub")
        await bus.start()

        event = Event(
            event_type=EventType.TRADE_OPENED,
            payload={"symbol": "BTCUSDT"},
            priority=EventPriority.HIGH,
            source="test",
        )
        bus.publish(event)
        await bus.wait_idle(timeout=2.0)
        await bus.stop()

        assert len(received) == 1
        assert received[0].payload["symbol"] == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_event_type_filtering(self):
        from core.events.event_bus import get_event_bus
        from core.events.event_types import Event, EventType, EventPriority

        bus = get_event_bus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(handler, {EventType.TRADE_OPENED}, name="trade_sub")
        await bus.start()

        # Publish a trade event (should match)
        bus.publish(Event(event_type=EventType.TRADE_OPENED, payload={"a": 1}))
        # Publish a risk event (should NOT match)
        bus.publish(Event(event_type=EventType.RISK_KILL_SWITCH, payload={"b": 2}))

        await bus.wait_idle(timeout=2.0)
        await bus.stop()

        assert len(received) == 1
        assert received[0].payload["a"] == 1

    @pytest.mark.asyncio
    async def test_wildcard_subscriber(self):
        from core.events.event_bus import get_event_bus
        from core.events.event_types import Event, EventType

        bus = get_event_bus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(handler, name="wildcard")  # No event_types = all
        await bus.start()

        bus.publish(Event(event_type=EventType.TRADE_OPENED, payload={}))
        bus.publish(Event(event_type=EventType.RISK_KILL_SWITCH, payload={}))

        await bus.wait_idle(timeout=2.0)
        await bus.stop()

        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_priority_filtering(self):
        from core.events.event_bus import get_event_bus
        from core.events.event_types import Event, EventType, EventPriority

        bus = get_event_bus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(
            handler,
            {EventType.TRADE_OPENED},
            name="high_only",
            min_priority=EventPriority.HIGH,
        )
        await bus.start()

        bus.publish(Event(event_type=EventType.TRADE_OPENED, payload={"p": "low"}, priority=EventPriority.LOW))
        bus.publish(Event(event_type=EventType.TRADE_OPENED, payload={"p": "high"}, priority=EventPriority.HIGH))

        await bus.wait_idle(timeout=2.0)
        await bus.stop()

        assert len(received) == 1
        assert received[0].payload["p"] == "high"

    @pytest.mark.asyncio
    async def test_dead_letter_on_timeout(self):
        from core.events.event_bus import get_event_bus
        from core.events.event_types import Event, EventType

        bus = get_event_bus()

        async def slow_handler(event):
            await asyncio.sleep(20)  # Will timeout

        bus.subscribe(slow_handler, {EventType.TRADE_OPENED}, name="slow")
        await bus.start()

        bus.publish(Event(event_type=EventType.TRADE_OPENED, payload={}))
        # Wait for timeout (dispatch loop has 10s timeout)
        await asyncio.sleep(12)
        await bus.stop()

        dls = bus.get_dead_letters()
        assert len(dls) >= 1
        assert dls[0]["error"] == "timeout"

    @pytest.mark.asyncio
    async def test_dead_letter_on_exception(self):
        from core.events.event_bus import get_event_bus
        from core.events.event_types import Event, EventType

        bus = get_event_bus()

        async def bad_handler(event):
            raise ValueError("test error")

        bus.subscribe(bad_handler, {EventType.TRADE_OPENED}, name="bad")
        await bus.start()

        bus.publish(Event(event_type=EventType.TRADE_OPENED, payload={}))
        await bus.wait_idle(timeout=2.0)
        await bus.stop()

        dls = bus.get_dead_letters()
        assert len(dls) == 1
        assert "test error" in dls[0]["error"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        from core.events.event_bus import get_event_bus
        from core.events.event_types import Event, EventType

        bus = get_event_bus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(handler, {EventType.TRADE_OPENED}, name="removable")
        result = bus.unsubscribe("removable")
        assert result is True

        await bus.start()
        bus.publish(Event(event_type=EventType.TRADE_OPENED, payload={}))
        await bus.wait_idle(timeout=1.0)
        await bus.stop()

        assert len(received) == 0

    def test_stats(self):
        from core.events.event_bus import get_event_bus
        from core.events.event_types import Event, EventType

        bus = get_event_bus()

        async def noop(e):
            pass

        bus.subscribe(noop, {EventType.TRADE_OPENED}, name="stats_sub")
        bus.publish(Event(event_type=EventType.TRADE_OPENED, payload={}))

        stats = bus.get_stats()
        assert stats["total_published"] == 1
        assert stats["total_delivered"] == 1
        assert len(stats["subscribers"]) == 1


# ═══════════════════════════════════════════════════════════════
#  2. EVENT TYPE TESTS
# ═══════════════════════════════════════════════════════════════

class TestEventTypes:
    """Event type factories produce valid, typed events."""

    def test_trade_event_factory(self):
        from core.events.event_types import TradeEvent, EventType, EventPriority

        evt = TradeEvent(
            EventType.TRADE_OPENED,
            symbol="BTCUSDT",
            broker="kraken",
            side="long",
            qty=0.01,
            price=50000.0,
        )
        assert evt.event_type == EventType.TRADE_OPENED
        assert evt.payload["symbol"] == "BTCUSDT"
        assert evt.priority == EventPriority.HIGH
        assert evt.trace_id  # auto-generated

    def test_risk_event_factory(self):
        from core.events.event_types import RiskEvent, EventType, EventPriority

        evt = RiskEvent(
            EventType.RISK_KILL_SWITCH,
            reason="drawdown breach",
            symbol="ETHUSDT",
            current_value=6.5,
            threshold=5.0,
        )
        assert evt.priority == EventPriority.CRITICAL
        assert evt.payload["reason"] == "drawdown breach"

    def test_reconciliation_event_factory(self):
        from core.events.event_types import ReconciliationEvent, EventType

        evt = ReconciliationEvent(
            EventType.RECONCILIATION_DIVERGENCE,
            broker="kraken",
            details={"symbol": "BTCUSDT"},
            severity="warning",
        )
        assert evt.payload["broker"] == "kraken"

    def test_event_immutability(self):
        from core.events.event_types import Event, EventType

        evt = Event(event_type=EventType.SYSTEM_HEARTBEAT, payload={"a": 1})
        with pytest.raises(AttributeError):
            evt.event_type = EventType.TRADE_OPENED  # frozen dataclass

    def test_event_with_trace(self):
        from core.events.event_types import Event, EventType

        evt1 = Event(event_type=EventType.SIGNAL_GENERATED, payload={})
        evt2 = evt1.with_trace(evt1.trace_id)
        assert evt2.trace_id == evt1.trace_id
        assert evt2.event_id != evt1.event_id  # new event ID


# ═══════════════════════════════════════════════════════════════
#  3. RISK ENGINE V2 TESTS
# ═══════════════════════════════════════════════════════════════

class TestRiskEngineV2:
    """Per-symbol risk limits, global exposure, correlation."""

    def setup_method(self):
        from backend.services.trading.risk_engine_v2 import reset_risk_engine_v2
        from core.events.event_bus import reset_event_bus
        reset_risk_engine_v2()
        reset_event_bus()

    def teardown_method(self):
        from backend.services.trading.risk_engine_v2 import reset_risk_engine_v2
        from core.events.event_bus import reset_event_bus
        reset_risk_engine_v2()
        reset_event_bus()

    def test_allow_within_limits(self):
        from backend.services.trading.risk_engine_v2 import get_risk_engine_v2

        rv2 = get_risk_engine_v2()
        rv2.update_capital(10000)

        verdict = rv2.evaluate("BTCUSDT", "long", 400, 50000.0)
        assert verdict.allowed is True
        assert verdict.action == "ALLOW"

    def test_block_global_exposure(self):
        from backend.services.trading.risk_engine_v2 import RiskEngineV2, RiskV2Config

        config = RiskV2Config()
        config.max_global_exposure_pct = 10.0  # 10% = $1000 of $10k
        rv2 = RiskEngineV2(config=config)
        rv2.update_capital(10000)

        # Already have $1000 exposure
        positions = {"ETHUSDT": {"notional": 1000, "side": "long"}}
        verdict = rv2.evaluate("BTCUSDT", "long", 500, 50000.0, positions)
        # Should block since 1000 + 500 > 1000 max
        assert verdict.allowed is False or verdict.size_multiplier < 1.0

    def test_single_position_cap(self):
        from backend.services.trading.risk_engine_v2 import RiskEngineV2, RiskV2Config

        config = RiskV2Config()
        config.max_position_pct = 2.0  # 2% of $10k = $200
        rv2 = RiskEngineV2(config=config)
        rv2.update_capital(10000)

        verdict = rv2.evaluate("BTCUSDT", "long", 500, 50000.0)
        assert verdict.size_multiplier < 1.0
        assert verdict.action == "REDUCE"

    def test_daily_loss_block(self):
        from backend.services.trading.risk_engine_v2 import RiskEngineV2, RiskV2Config

        config = RiskV2Config()
        config.max_daily_loss_per_symbol_pct = 1.0  # 1% of $10k = $100
        rv2 = RiskEngineV2(config=config)
        rv2.update_capital(10000)

        # Record a loss > $100
        rv2.record_trade_result("BTCUSDT", pnl=-150, is_win=False)

        verdict = rv2.evaluate("BTCUSDT", "long", 200, 50000.0)
        assert verdict.allowed is False
        assert "daily_loss_limit" in verdict.reasons[0]

    def test_cooldown_after_loss(self):
        from backend.services.trading.risk_engine_v2 import RiskEngineV2, RiskV2Config

        config = RiskV2Config()
        config.cooldown_after_loss_sec = 300
        rv2 = RiskEngineV2(config=config)
        rv2.update_capital(10000)

        rv2.record_trade_result("BTCUSDT", pnl=-50, is_win=False)
        verdict = rv2.evaluate("BTCUSDT", "long", 200, 50000.0)
        assert verdict.allowed is False
        assert "cooldown" in verdict.reasons[0]

    def test_correlation_limit(self):
        from backend.services.trading.risk_engine_v2 import RiskEngineV2, RiskV2Config

        config = RiskV2Config()
        config.max_correlated_positions = 1
        rv2 = RiskEngineV2(config=config)
        rv2.update_capital(100000)

        # ETHUSDT is in "eth" group, BTCUSDT in "btc" — no correlation
        positions = {"ETHUSDT": {"notional": 500, "side": "long"}}
        verdict = rv2.evaluate("BTCUSDT", "long", 500, 50000.0, positions)
        assert verdict.allowed is True

        # PF_XBTUSD is in "btc" group — correlated with BTCUSDT
        positions = {"PF_XBTUSD": {"notional": 500, "side": "long"}}
        verdict = rv2.evaluate("BTCUSDT", "long", 500, 50000.0, positions)
        assert verdict.allowed is False
        assert "correlation_limit" in verdict.reasons[0]

    def test_auto_derisk(self):
        from backend.services.trading.risk_engine_v2 import RiskEngineV2, RiskV2Config

        config = RiskV2Config()
        config.max_global_exposure_pct = 25.0  # Max $2500 of $10k
        config.auto_derisk_threshold = 0.8     # Start at 80% = $2000
        rv2 = RiskEngineV2(config=config)
        rv2.update_capital(10000)

        # Already at $2100 exposure — above 80% threshold
        positions = {"ETHUSDT": {"notional": 2100, "side": "long"}}
        verdict = rv2.evaluate("BTCUSDT", "long", 200, 50000.0, positions)
        # Should be reduced, not blocked
        assert verdict.size_multiplier < 1.0

    def test_status_report(self):
        from backend.services.trading.risk_engine_v2 import get_risk_engine_v2

        rv2 = get_risk_engine_v2()
        rv2.update_capital(10000)
        rv2.record_trade_result("BTCUSDT", pnl=50, is_win=True)

        status = rv2.get_status()
        assert status["total_capital"] == 10000
        assert "BTCUSDT" in status["symbols"]


# ═══════════════════════════════════════════════════════════════
#  4. FAILURE ISOLATION TESTS
# ═══════════════════════════════════════════════════════════════

class TestServiceWrapper:
    """Service wrapper — crash recovery, restart, circuit breaker."""

    @pytest.mark.asyncio
    async def test_normal_service_lifecycle(self):
        from core.isolation import ServiceWrapper, ServiceState

        ran = False

        async def service_fn():
            nonlocal ran
            ran = True

        wrapper = ServiceWrapper("test_svc", service_fn)
        await wrapper.start()
        await asyncio.sleep(0.5)
        await wrapper.stop()

        assert ran is True
        assert wrapper.state == ServiceState.STOPPED

    @pytest.mark.asyncio
    async def test_crash_and_restart(self):
        from core.isolation import ServiceWrapper, ServiceState

        call_count = 0

        async def crashing_service():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError(f"crash #{call_count}")
            # Success on 3rd attempt — run briefly then exit
            await asyncio.sleep(0.1)

        wrapper = ServiceWrapper(
            "crash_svc", crashing_service,
            max_restarts=5, restart_delay_sec=0.1,
        )
        await wrapper.start()
        await asyncio.sleep(3)
        await wrapper.stop()

        assert call_count >= 3
        assert wrapper.stats.crash_count >= 2

    @pytest.mark.asyncio
    async def test_max_restarts_exceeded(self):
        from core.isolation import ServiceWrapper, ServiceState

        async def always_crash():
            raise RuntimeError("permanent failure")

        wrapper = ServiceWrapper(
            "perm_crash", always_crash,
            max_restarts=2, restart_delay_sec=0.1,
        )
        await wrapper.start()
        await asyncio.sleep(3)

        assert wrapper.stats.crash_count >= 2
        assert wrapper.state == ServiceState.CRASHED

    @pytest.mark.asyncio
    async def test_crash_callback(self):
        from core.isolation import ServiceWrapper

        crashes = []

        async def on_crash(name, exc):
            crashes.append((name, str(exc)))

        async def crashing():
            raise ValueError("boom")

        wrapper = ServiceWrapper(
            "cb_test", crashing,
            max_restarts=1, restart_delay_sec=0.1, on_crash=on_crash,
        )
        await wrapper.start()
        await asyncio.sleep(2)
        await wrapper.stop()

        assert len(crashes) >= 1
        assert crashes[0][0] == "cb_test"
        assert "boom" in crashes[0][1]


class TestServiceSupervisor:
    """Supervisor — ordered start/stop, status."""

    @pytest.mark.asyncio
    async def test_supervisor_start_stop(self):
        from core.isolation import ServiceSupervisor, ServiceState

        order = []

        async def svc_a():
            order.append("a_start")
            await asyncio.sleep(10)

        async def svc_b():
            order.append("b_start")
            await asyncio.sleep(10)

        sup = ServiceSupervisor()
        sup.register("svc_a", svc_a)
        sup.register("svc_b", svc_b)

        await sup.start_all()
        await asyncio.sleep(0.5)

        status = sup.get_status()
        assert "svc_a" in status.get("services", status)
        assert "svc_b" in status.get("services", status)

        await sup.stop_all(timeout=2)

    @pytest.mark.asyncio
    async def test_supervisor_health_check(self):
        from core.isolation import ServiceSupervisor

        async def healthy_svc():
            await asyncio.sleep(10)

        sup = ServiceSupervisor()
        sup.register("healthy", healthy_svc)
        await sup.start_all()
        await asyncio.sleep(0.3)

        assert sup.is_healthy() is True
        await sup.stop_all(timeout=2)


# ═══════════════════════════════════════════════════════════════
#  5. OBSERVABILITY TESTS
# ═══════════════════════════════════════════════════════════════

class TestObservability:
    """Trace IDs, structured logging, health aggregator."""

    def test_trace_id_generation(self):
        from core.observability import new_trace_id, get_trace_id, set_trace_id

        tid = new_trace_id()
        assert len(tid) == 16
        assert get_trace_id() == tid

        set_trace_id("custom123")
        assert get_trace_id() == "custom123"

    def test_structured_formatter(self):
        import json
        from core.observability import StructuredFormatter, set_trace_id

        formatter = StructuredFormatter()
        set_trace_id("test_trace_42")

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=None,
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)

        assert data["msg"] == "test message"
        assert data["level"] == "INFO"
        assert data["trace_id"] == "test_trace_42"

    @pytest.mark.asyncio
    async def test_health_aggregator(self):
        from core.observability import HealthAggregator, SubsystemHealth

        health = HealthAggregator()

        def check_ok():
            return SubsystemHealth(name="db", status="healthy")

        def check_bad():
            return SubsystemHealth(name="broker", status="unhealthy", error="timeout")

        health.register("db", check_ok)
        health.register("broker", check_bad)

        result = await health.check_all()
        assert result["status"] == "unhealthy"  # worst status
        assert result["subsystems"]["db"]["status"] == "healthy"
        assert result["subsystems"]["broker"]["status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_health_aggregator_timeout(self):
        from core.observability import HealthAggregator

        health = HealthAggregator()

        async def slow_check():
            await asyncio.sleep(30)

        health.register("slow", slow_check)
        result = await health.check_all()
        assert result["subsystems"]["slow"]["status"] == "unhealthy"
        assert result["subsystems"]["slow"]["error"] == "timeout"


# ═══════════════════════════════════════════════════════════════
#  6. RECONCILIATION V2 TESTS
# ═══════════════════════════════════════════════════════════════

class TestReconciliationV2:
    """Reconciliation V2 — divergence detection, severity, safe mode trigger."""

    def setup_method(self):
        from core.events.event_bus import reset_event_bus
        reset_event_bus()

    def teardown_method(self):
        from core.events.event_bus import reset_event_bus
        reset_event_bus()

    def test_config_defaults(self):
        from backend.services.reconciliation_v2 import ReconciliationConfig

        config = ReconciliationConfig()
        assert config.interval_sec == 30
        assert config.position_threshold_pct == 5.0
        assert config.block_on_critical is True

    def test_position_divergence_creation(self):
        from backend.services.reconciliation_v2 import PositionDivergence

        div = PositionDivergence(
            symbol="BTCUSDT",
            broker="kraken",
            divergence_type="phantom_db",
            db_value=0.01,
            exchange_value=0,
            severity="critical",
        )
        assert div.severity == "critical"
        assert div.divergence_type == "phantom_db"

    def test_result_critical_detection(self):
        from backend.services.reconciliation_v2 import ReconciliationResult, PositionDivergence

        result = ReconciliationResult(broker="kraken")
        result.divergences.append(
            PositionDivergence(
                symbol="BTCUSDT", broker="kraken",
                divergence_type="phantom_db", severity="warning",
            )
        )
        assert result.has_critical is False
        assert result.has_warnings is True

        result.divergences.append(
            PositionDivergence(
                symbol="ETHUSDT", broker="kraken",
                divergence_type="size_mismatch", severity="critical",
            )
        )
        assert result.has_critical is True

    def test_status_report(self):
        from backend.services.reconciliation_v2 import ReconciliationV2Service

        svc = ReconciliationV2Service()
        status = svc.get_status()
        assert status["running"] is False
        assert "config" in status


# ═══════════════════════════════════════════════════════════════
#  7. POSTGRESQL MIGRATION LAYER TESTS
# ═══════════════════════════════════════════════════════════════

class TestPgMigration:
    """PostgreSQL migration — config, DDL generation, health check."""

    def test_sqlite_config_detection(self):
        from backend.db.pg_migration import DatabaseConfig

        config = DatabaseConfig("sqlite:///./data/test.db")
        assert config.is_sqlite is True
        assert config.is_postgres is False

    def test_postgres_config_detection(self):
        from backend.db.pg_migration import DatabaseConfig

        config = DatabaseConfig("postgresql://user:pass@localhost/trading")
        assert config.is_postgres is True
        assert config.is_sqlite is False

    def test_ddl_generation(self):
        from backend.db.pg_migration import create_pg_migration_sql

        sql = create_pg_migration_sql()
        assert "live_trades" in sql
        assert "reconciliation_log" in sql
        assert "risk_events" in sql
        assert "alert_history" in sql
        assert "JSONB" in sql

    def test_sqlite_engine_creation(self):
        from backend.db.pg_migration import DatabaseConfig  

        config = DatabaseConfig("sqlite:///./data/test_phase6.db")
        engine = config.create_engine()
        assert engine is not None
        # Cleanup
        engine.dispose()

    def test_health_check_sqlite(self):
        from backend.db.pg_migration import DatabaseConfig, check_db_health

        config = DatabaseConfig("sqlite:///./data/test_phase6_hc.db")
        engine = config.create_engine()
        health = check_db_health(engine)
        assert health["healthy"] is True
        assert health["engine"] == "sqlite"
        assert health["latency_ms"] > 0
        engine.dispose()


# ═══════════════════════════════════════════════════════════════
#  8. ALERT MANAGER EVENT BUS INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════

class TestAlertManagerEventBus:
    """Alert manager — event bus integration."""

    def setup_method(self):
        from core.events.event_bus import reset_event_bus
        reset_event_bus()

    def teardown_method(self):
        from core.events.event_bus import reset_event_bus
        from notifications.alert_manager import AlertManager
        AlertManager.reset()
        reset_event_bus()

    def test_event_bus_connection(self):
        from notifications.alert_manager import AlertManager

        am = AlertManager.get()
        am.connect_event_bus()
        assert am._event_bus_connected is True

    def test_status_includes_bus_flag(self):
        from notifications.alert_manager import AlertManager

        am = AlertManager.get()
        status = am.get_status()
        assert "event_bus_connected" in status


# ═══════════════════════════════════════════════════════════════
#  9. INTEGRATION: EVENT BUS → RISK V2 → ALERTING CHAIN
# ═══════════════════════════════════════════════════════════════

class TestPhase6Integration:
    """End-to-end: risk decision → event bus → alert dispatch."""

    def setup_method(self):
        from core.events.event_bus import reset_event_bus
        from backend.services.trading.risk_engine_v2 import reset_risk_engine_v2
        reset_event_bus()
        reset_risk_engine_v2()

    def teardown_method(self):
        from core.events.event_bus import reset_event_bus
        from backend.services.trading.risk_engine_v2 import reset_risk_engine_v2
        reset_event_bus()
        reset_risk_engine_v2()

    @pytest.mark.asyncio
    async def test_risk_block_emits_event(self):
        from core.events.event_bus import get_event_bus
        from core.events.event_types import EventType
        from backend.services.trading.risk_engine_v2 import RiskEngineV2, RiskV2Config

        bus = get_event_bus()
        events = []

        async def collector(evt):
            events.append(evt)

        bus.subscribe(collector, name="collector")
        await bus.start()

        config = RiskV2Config()
        config.max_daily_loss_per_symbol_pct = 0.5
        rv2 = RiskEngineV2(config=config)
        rv2.update_capital(10000)
        rv2.record_trade_result("BTCUSDT", pnl=-100, is_win=False)

        # This should emit a RISK_DAILY_LIMIT event
        rv2.evaluate("BTCUSDT", "long", 200, 50000.0)

        await bus.wait_idle(timeout=2.0)
        await bus.stop()

        risk_events = [
            e for e in events
            if e.event_type in (EventType.RISK_DAILY_LIMIT, EventType.RISK_VERDICT)
        ]
        assert len(risk_events) >= 1

    @pytest.mark.asyncio
    async def test_full_chain_signal_to_alert(self):
        """Complete chain: publish trade event → event bus → subscriber receives."""
        from core.events.event_bus import get_event_bus
        from core.events.event_types import TradeEvent, EventType

        bus = get_event_bus()
        alerts = []

        async def alert_handler(event):
            alerts.append(event)

        bus.subscribe(
            alert_handler,
            {EventType.TRADE_OPENED, EventType.TRADE_CLOSED},
            name="alert_sim",
        )
        await bus.start()

        # Simulate trade opened
        bus.publish(TradeEvent(
            EventType.TRADE_OPENED,
            symbol="BTCUSDT",
            broker="kraken",
            side="long",
            qty=0.01,
            price=50000.0,
        ))

        # Simulate trade closed
        bus.publish(TradeEvent(
            EventType.TRADE_CLOSED,
            symbol="BTCUSDT",
            broker="kraken",
            side="long",
            pnl=25.0,
        ))

        await bus.wait_idle(timeout=2.0)
        await bus.stop()

        assert len(alerts) == 2
        assert alerts[0].event_type == EventType.TRADE_OPENED
        assert alerts[1].payload["pnl"] == 25.0


# ═══════════════════════════════════════════════════════════════
#  10. SQLITE PRAGMA UPGRADE TEST
# ═══════════════════════════════════════════════════════════════

class TestSQLitePragma:
    """Verify Phase 6 PRAGMA upgrade: synchronous=FULL."""

    def test_synchronous_full(self):
        """DB session uses PRAGMA synchronous=FULL (not NORMAL)."""
        from backend.db.session import engine

        url = str(engine.url)
        if not url.startswith("sqlite"):
            pytest.skip("Not SQLite")

        with engine.connect() as conn:
            result = conn.execute(__import__("sqlalchemy").text("PRAGMA synchronous"))
            sync_val = result.fetchone()[0]
            # FULL = 2, NORMAL = 1
            assert sync_val == 2, f"Expected synchronous=FULL(2), got {sync_val}"
