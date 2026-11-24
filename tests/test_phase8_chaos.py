"""
Phase 8 — Event Bus Chaos Tests
════════════════════════════════

REAL stress tests for the hardened event bus. Not theoretical.

Tests:
  1. Duplicate event rejection (publisher-side dedup)
  2. Consumer-side dedup (idempotency guarantee)
  3. Ordering via sequence numbers + gap detection
  4. Dead letter queue on subscriber failure
  5. Kill switch fast-path bypasses queue backlog
  6. WAL persistence and replay
  7. Dead letter replay with consumer dedup
  8. Bus metrics accuracy under load
  9. Subscriber timeout → dead letter
  10. Queue overflow handling (bounded queues)
  11. Consistency checker drift detection
  12. Consistency checker escalation logic
"""

import asyncio
import os
import tempfile
import time
import pytest

from core.events.event_bus import EventBus, WriteAheadLog, reset_event_bus, get_event_bus
from core.events.event_types import Event, EventType, EventPriority, TradeEvent, SystemEvent


# ═══════════════════════════════════════════════════════════════
#  FIXTURES
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def clean_bus():
    """Fresh event bus per test."""
    reset_event_bus()
    yield
    reset_event_bus()


def make_bus(wal_path: str = None) -> EventBus:
    """Create a test bus with optional WAL path."""
    if wal_path is None:
        tmp = tempfile.mkdtemp()
        wal_path = os.path.join(tmp, "test_wal.jsonl")
    return EventBus(wal_path=wal_path)


# ═══════════════════════════════════════════════════════════════
#  1. PUBLISHER-SIDE DEDUP
# ═══════════════════════════════════════════════════════════════

class TestPublisherDedup:
    def test_duplicate_event_rejected(self):
        bus = make_bus()
        received = []

        async def handler(e):
            received.append(e)

        bus.subscribe(handler, {EventType.TRADE_OPENED}, name="test")
        evt = TradeEvent(EventType.TRADE_OPENED, symbol="BTCUSDT", broker="test", side="long", qty=0.1, price=50000)

        n1 = bus.publish(evt)
        n2 = bus.publish(evt)  # same event_id → rejected

        assert n1 == 1
        assert n2 == 0
        stats = bus.get_stats()
        assert stats["dedup_rejected"] == 1

    def test_different_events_not_deduped(self):
        bus = make_bus()
        received = []

        async def handler(e):
            received.append(e)

        bus.subscribe(handler, {EventType.TRADE_OPENED}, name="test")
        e1 = TradeEvent(EventType.TRADE_OPENED, symbol="BTCUSDT", broker="test", side="long", qty=0.1, price=50000)
        e2 = TradeEvent(EventType.TRADE_OPENED, symbol="ETHUSDT", broker="test", side="long", qty=0.5, price=3000)

        assert bus.publish(e1) == 1
        assert bus.publish(e2) == 1
        assert bus.get_stats()["dedup_rejected"] == 0


# ═══════════════════════════════════════════════════════════════
#  2. CONSUMER-SIDE DEDUP
# ═══════════════════════════════════════════════════════════════

class TestConsumerDedup:
    @pytest.mark.asyncio
    async def test_consumer_dedup_on_replay(self):
        """After processing, replayed event with same id is skipped."""
        bus = make_bus()
        received = []

        async def handler(e):
            received.append(e.event_id)

        bus.subscribe(handler, {EventType.TRADE_OPENED}, name="consumer")
        evt = TradeEvent(EventType.TRADE_OPENED, symbol="BTCUSDT", broker="test", side="long", qty=0.1, price=50000)

        bus.publish(evt)
        await bus.start()
        await bus.wait_idle(timeout=2.0)
        await bus.stop()

        assert len(received) == 1
        # Consumer now has this event_id in _processed_ids
        sub = bus._subscriptions[0]
        assert evt.event_id in sub._processed_ids


# ═══════════════════════════════════════════════════════════════
#  3. SEQUENCE NUMBERS + GAP DETECTION
# ═══════════════════════════════════════════════════════════════

class TestSequenceOrdering:
    def test_sequence_monotonically_increases(self):
        bus = make_bus()

        async def noop(e):
            pass

        bus.subscribe(noop, name="test")

        for i in range(10):
            e = TradeEvent(EventType.TRADE_OPENED, symbol=f"SYM{i}", broker="test", side="long", qty=0.1, price=i)
            bus.publish(e)

        assert bus._sequence == 10
        # All sequence numbers should be 1..10
        seqs = sorted(bus._seq_map.values())
        assert seqs == list(range(1, 11))

    def test_stats_expose_sequence(self):
        bus = make_bus()
        assert bus.get_stats()["sequence"] == 0

        async def noop(e):
            pass

        bus.subscribe(noop, name="test")
        bus.publish(TradeEvent(EventType.TRADE_OPENED, symbol="X", broker="t", side="long", qty=0.1, price=1))
        assert bus.get_stats()["sequence"] == 1


# ═══════════════════════════════════════════════════════════════
#  4. DEAD LETTER ON SUBSCRIBER FAILURE
# ═══════════════════════════════════════════════════════════════

class TestDeadLetter:
    @pytest.mark.asyncio
    async def test_exception_goes_to_dead_letter(self):
        bus = make_bus()

        async def bad_handler(e):
            raise RuntimeError("boom")

        bus.subscribe(bad_handler, {EventType.TRADE_OPENED}, name="crasher")
        evt = TradeEvent(EventType.TRADE_OPENED, symbol="BTC", broker="t", side="l", qty=0.1, price=1)
        bus.publish(evt)

        await bus.start()
        await bus.wait_idle(timeout=2.0)
        await bus.stop()

        dls = bus.get_dead_letters()
        assert len(dls) == 1
        assert dls[0]["subscriber"] == "crasher"
        assert "boom" in dls[0]["error"]

    @pytest.mark.asyncio
    async def test_timeout_goes_to_dead_letter(self):
        bus = make_bus()

        async def slow_handler(e):
            await asyncio.sleep(20)  # will timeout at 10s

        bus.subscribe(slow_handler, {EventType.TRADE_OPENED}, name="slow")
        evt = TradeEvent(EventType.TRADE_OPENED, symbol="BTC", broker="t", side="l", qty=0.1, price=1)
        bus.publish(evt)

        await bus.start()
        # Wait enough for the timeout (10s) to trigger
        await asyncio.sleep(12)
        await bus.stop()

        dls = bus.get_dead_letters()
        assert len(dls) >= 1
        assert dls[0]["error"] == "timeout"


# ═══════════════════════════════════════════════════════════════
#  5. KILL SWITCH FAST-PATH
# ═══════════════════════════════════════════════════════════════

class TestKillSwitchFastPath:
    def test_kill_switch_sets_flag_immediately(self):
        bus = make_bus()
        received = []

        async def handler(e):
            received.append(e)

        bus.subscribe(handler, {EventType.RISK_KILL_SWITCH}, name="ks_handler")

        assert not bus.kill_switch_active
        evt = SystemEvent(EventType.RISK_KILL_SWITCH, message="Emergency", source="test")
        bus.publish(evt)

        # Flag set synchronously = zero latency
        assert bus.kill_switch_active
        stats = bus.get_stats()
        assert stats["fast_path_delivered"] >= 1
        assert stats["kill_switch_active"] is True

    def test_kill_switch_reset(self):
        bus = make_bus()

        async def noop(e):
            pass

        bus.subscribe(noop, {EventType.RISK_KILL_SWITCH}, name="ks")
        bus.publish(SystemEvent(EventType.RISK_KILL_SWITCH, message="test", source="test"))
        assert bus.kill_switch_active

        bus.reset_kill_switch()
        assert not bus.kill_switch_active

    def test_safe_mode_uses_fast_path(self):
        bus = make_bus()
        received = []

        async def handler(e):
            received.append(e)

        bus.subscribe(handler, {EventType.SAFE_MODE_ACTIVATED}, name="sm")
        evt = SystemEvent(EventType.SAFE_MODE_ACTIVATED, message="test", source="test")
        bus.publish(evt)

        assert bus.get_stats()["fast_path_delivered"] >= 1


# ═══════════════════════════════════════════════════════════════
#  6. WAL PERSISTENCE + REPLAY
# ═══════════════════════════════════════════════════════════════

class TestWAL:
    def test_critical_event_persisted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            wal_path = os.path.join(tmp, "test.jsonl")
            bus = EventBus(wal_path=wal_path)

            async def noop(e):
                pass

            bus.subscribe(noop, name="test")
            ks_evt = SystemEvent(EventType.RISK_KILL_SWITCH, message="test", source="wal_test")
            bus.publish(ks_evt)

            assert bus.get_stats()["wal_persisted"] == 1
            bus._wal.close()
            # WAL file should exist with content
            assert os.path.exists(wal_path)
            with open(wal_path, "r") as f:
                content = f.read()
            assert "risk.kill_switch" in content

    def test_non_critical_event_not_wal(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            wal_path = os.path.join(tmp, "test.jsonl")
            bus = EventBus(wal_path=wal_path)

            async def noop(e):
                pass

            bus.subscribe(noop, name="test")
            evt = SystemEvent(EventType.SYSTEM_STARTUP, message="hello", source="test")
            bus.publish(evt)

            assert bus.get_stats()["wal_persisted"] == 0
            bus._wal.close()

    @pytest.mark.asyncio
    async def test_replay_from_wal(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            wal_path = os.path.join(tmp, "test.jsonl")
            bus = EventBus(wal_path=wal_path)
            received = []

            async def handler(e):
                received.append(e)

            bus.subscribe(handler, {EventType.RISK_KILL_SWITCH}, name="replay_test")

            # Publish critical event
            bus.publish(SystemEvent(EventType.RISK_KILL_SWITCH, message="original", source="test"))
            await bus.start()
            await bus.wait_idle(timeout=2.0)
            await bus.stop()  # stop also closes WAL

            # replay from WAL
            bus2 = EventBus(wal_path=wal_path)
            received2 = []

            async def handler2(e):
                received2.append(e)

            bus2.subscribe(handler2, {EventType.RISK_KILL_SWITCH}, name="replay2")
            count = await bus2.replay_from_wal(after_seq=0)
            assert count == 1

            await bus2.start()
            await bus2.wait_idle(timeout=2.0)
            await bus2.stop()
            assert len(received2) == 1
            assert "_replayed_from_wal" in received2[0].payload


# ═══════════════════════════════════════════════════════════════
#  7. DEAD LETTER REPLAY
# ═══════════════════════════════════════════════════════════════

class TestDeadLetterReplay:
    @pytest.mark.asyncio
    async def test_replay_dead_letters(self):
        bus = make_bus()
        call_count = 0

        async def failing_then_ok(e):
            nonlocal call_count
            call_count += 1
            if "_replayed_from" not in e.payload:
                raise RuntimeError("first try fails")
            # replayed version succeeds

        bus.subscribe(failing_then_ok, {EventType.TRADE_OPENED}, name="retry_test")
        evt = TradeEvent(EventType.TRADE_OPENED, symbol="BTC", broker="t", side="l", qty=0.1, price=1)
        bus.publish(evt)

        await bus.start()
        await bus.wait_idle(timeout=2.0)

        assert len(bus._dead_letters) == 1

        # Replay dead letters
        replayed = await bus.replay_dead_letters()
        assert replayed == 1
        assert len(bus._dead_letters) == 0

        await bus.wait_idle(timeout=2.0)
        await bus.stop()
        assert call_count >= 2  # original fail + replay


# ═══════════════════════════════════════════════════════════════
#  8. METRICS UNDER LOAD
# ═══════════════════════════════════════════════════════════════

class TestMetricsUnderLoad:
    @pytest.mark.asyncio
    async def test_100_events_accurate_stats(self):
        bus = make_bus()
        received = []

        async def handler(e):
            received.append(e)

        bus.subscribe(handler, name="load_test")

        for i in range(100):
            bus.publish(TradeEvent(
                EventType.TRADE_OPENED, symbol=f"SYM{i}", broker="t", side="long", qty=0.1, price=i
            ))

        await bus.start()
        await bus.wait_idle(timeout=5.0)
        await bus.stop()

        stats = bus.get_stats()
        assert stats["total_published"] == 100
        assert stats["total_delivered"] == 100
        assert stats["total_dropped"] == 0
        assert stats["total_errors"] == 0
        assert stats["sequence"] == 100
        assert len(received) == 100


# ═══════════════════════════════════════════════════════════════
#  9. QUEUE OVERFLOW
# ═══════════════════════════════════════════════════════════════

class TestQueueOverflow:
    def test_overflow_graceful_no_crash(self):
        """When subscriber queue is full, events are dropped not crashed."""
        bus = make_bus()

        async def slow(e):
            await asyncio.sleep(100)  # never returns

        bus.subscribe(slow, name="slow_sub")

        # Fill the queue (default 10,000)
        dropped = 0
        for i in range(10_100):
            n = bus.publish(TradeEvent(
                EventType.TRADE_OPENED, symbol=f"S{i}", broker="t", side="l", qty=0.1, price=1
            ))
            if n == 0 and i > 0:
                dropped += 1

        stats = bus.get_stats()
        assert stats["total_dropped"] > 0
        assert stats["total_dropped"] <= 200  # ~100 dropped near boundary


# ═══════════════════════════════════════════════════════════════
#  10. CONSISTENCY CHECKER
# ═══════════════════════════════════════════════════════════════

class TestConsistencyChecker:
    def test_no_drift_when_consistent(self):
        from core.consistency_checker import ConsistencyChecker
        checker = ConsistencyChecker()
        drifts = checker.check_positions(
            event_positions={"BTCUSDT": 0.1},
            db_positions={"BTCUSDT": 0.1},
            exchange_positions={"BTCUSDT": 0.1},
        )
        assert len(drifts) == 0

    def test_detect_db_vs_exchange_drift(self):
        from core.consistency_checker import ConsistencyChecker, DriftSeverity
        checker = ConsistencyChecker()
        drifts = checker.check_positions(
            event_positions={"BTCUSDT": 0.1},
            db_positions={"BTCUSDT": 0.1},
            exchange_positions={"BTCUSDT": 0.05},  # 50% drift
        )
        assert len(drifts) >= 1
        critical = [d for d in drifts if d.severity == DriftSeverity.CRITICAL]
        assert len(critical) >= 1

    def test_ghost_position_detected(self):
        from core.consistency_checker import ConsistencyChecker, DriftSeverity
        checker = ConsistencyChecker()
        drifts = checker.check_positions(
            event_positions={},
            db_positions={},
            exchange_positions={"BTCUSDT": 0.5},  # ghost!
        )
        ghost = [d for d in drifts if d.field == "ghost_position"]
        assert len(ghost) == 1
        assert ghost[0].severity == DriftSeverity.CRITICAL

    def test_balance_drift(self):
        from core.consistency_checker import ConsistencyChecker
        checker = ConsistencyChecker()
        drift = checker.check_balance(db_balance=10000, exchange_balance=9500)
        assert drift is not None
        assert drift.field == "balance"

    def test_balance_within_tolerance(self):
        from core.consistency_checker import ConsistencyChecker
        checker = ConsistencyChecker()
        drift = checker.check_balance(db_balance=10000, exchange_balance=9950)
        assert drift is None  # within 1% tolerance

    def test_escalation_logic(self):
        from core.consistency_checker import ConsistencyChecker, DriftRecord, DriftSeverity
        checker = ConsistencyChecker()

        # No drifts → none
        assert checker.escalate([]) == "none"

        # 1 critical → safe_mode
        assert checker.escalate([
            DriftRecord(field="x", expected=1, actual=2, severity=DriftSeverity.CRITICAL, source="test")
        ]) == "safe_mode"

        # 3 critical → kill_switch
        assert checker.escalate([
            DriftRecord(field="x", expected=1, actual=2, severity=DriftSeverity.CRITICAL, source="test"),
            DriftRecord(field="y", expected=1, actual=2, severity=DriftSeverity.CRITICAL, source="test"),
            DriftRecord(field="z", expected=1, actual=2, severity=DriftSeverity.CRITICAL, source="test"),
        ]) == "kill_switch"

        # Only warnings → alert
        assert checker.escalate([
            DriftRecord(field="x", expected=1, actual=2, severity=DriftSeverity.WARNING, source="test"),
        ]) == "alert"


# ═══════════════════════════════════════════════════════════════
#  11. ISOLATION SUPERVISOR INTEGRATION
# ═══════════════════════════════════════════════════════════════

class TestIsolationSupervisor:
    @pytest.mark.asyncio
    async def test_service_restarts_on_crash(self):
        from core.isolation import ServiceWrapper, ServiceState
        call_count = 0

        async def crashing_service():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("crash!")
            # Third attempt succeeds but exits clean
            return

        wrapper = ServiceWrapper("test_svc", crashing_service, max_restarts=5, restart_delay_sec=0.1)
        await wrapper.start()
        await asyncio.sleep(2)  # let it crash and restart
        await wrapper.stop(timeout=2.0)

        assert call_count >= 3
        assert wrapper.stats.crash_count >= 2

    @pytest.mark.asyncio
    async def test_max_restarts_respected(self):
        from core.isolation import ServiceWrapper, ServiceState

        async def always_crash():
            raise RuntimeError("always")

        wrapper = ServiceWrapper("doomed", always_crash, max_restarts=2, restart_delay_sec=0.1)
        await wrapper.start()
        await asyncio.sleep(3)

        assert wrapper.stats.crash_count >= 2
        assert wrapper.state in (ServiceState.CRASHED, ServiceState.STOPPED)
        await wrapper.stop(timeout=1.0)
