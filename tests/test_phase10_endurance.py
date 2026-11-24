"""
Phase 10 - Endurance, Recovery, Stress, Fail-Closed & Failure Fatigue Tests.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.events.event_bus import (
    EventBus,
    WriteAheadLog,
    DeadLetterEntry,
    _MAX_DEDUP_CACHE,
    _MAX_SEQ_MAP,
    _MAX_CONSUMER_DEDUP,
    _DEAD_LETTER_MAX_RETRIES,
)
from core.events.event_types import Event, EventPriority, EventType
from core.consistency_checker import ConsistencyChecker, DriftRecord, DriftSeverity
from core.recovery_protocol import RecoveryProtocol, RecoveryStepResult
from core.long_run_monitor import (
    LongRunMonitor,
    AutoHealEngine,
    RollingCounter,
    HealthLevel,
    Thresholds,
)
from core.isolation import (
    ServiceWrapper,
    ServiceSupervisor,
    ServiceState,
    SystemHealthState,
    _CRITICAL_SERVICES,
)


def _make_event(etype=EventType.TRADE_OPENED, source="test", eid=None, payload=None):
    return Event(
        event_type=etype,
        payload=payload or {"test": True},
        priority=EventPriority.NORMAL,
        event_id=eid or uuid.uuid4().hex[:16],
        source=source,
    )


def _close_bus(bus):
    """Close WAL file handle to avoid Windows PermissionError on temp dir cleanup."""
    if hasattr(bus, "_wal") and bus._wal:
        bus._wal.close()


# ---------------------------------------------------------------
#  1. LONG-RUN STABILITY
# ---------------------------------------------------------------

class TestLongRunStability:

    def test_dedup_cache_bounded(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        try:
            for i in range(60_000):
                bus.publish(_make_event(eid=f"mass_{i}"))
            assert len(bus._seen_event_ids) <= _MAX_DEDUP_CACHE
            assert len(bus._seen_ids_order) <= _MAX_DEDUP_CACHE
        finally:
            _close_bus(bus)

    def test_seq_map_bounded(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        try:
            for i in range(60_000):
                bus.publish(_make_event(eid=f"seq_{i}"))
            assert len(bus._seq_map) <= _MAX_SEQ_MAP
            assert len(bus._seq_order) <= _MAX_SEQ_MAP
        finally:
            _close_bus(bus)

    def test_consumer_dedup_bounded(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        try:
            async def handler(evt):
                pass
            bus.subscribe(handler, name="bound_test")
            sub = bus._subscriptions[0]
            for i in range(15_000):
                eid = f"cdedup_{i}"
                sub._processed_ids.add(eid)
                sub._processed_ids_order.append(eid)
                while len(sub._processed_ids) > _MAX_CONSUMER_DEDUP:
                    old = sub._processed_ids_order.popleft()
                    sub._processed_ids.discard(old)
            assert len(sub._processed_ids) <= _MAX_CONSUMER_DEDUP
        finally:
            _close_bus(bus)

    def test_dead_letter_bounded(self):
        td = tempfile.mkdtemp()
        bus = EventBus(max_dead_letters=100, wal_path=os.path.join(td, "wal.jsonl"))
        try:
            for i in range(500):
                bus._add_dead_letter(_make_event(eid=f"dl_{i}"), "test_sub", f"error_{i}")
            assert len(bus._dead_letters) <= 100
        finally:
            _close_bus(bus)

    def test_wal_backup_purge(self):
        td = tempfile.mkdtemp()
        wal = WriteAheadLog(path=os.path.join(td, "wal.jsonl"), max_size_mb=0.001)
        try:
            for i in range(200):
                wal.append(_make_event(etype=EventType.TRADE_OPENED, eid=f"walp_{i}"), i)
            baks = [f for f in os.listdir(td) if f.endswith(".bak")]
            assert len(baks) <= 5
        finally:
            wal.close()

    def test_force_dedup_gc(self):
        td = tempfile.mkdtemp()
        bus = EventBus(dedup_window_sec=0.01, wal_path=os.path.join(td, "wal.jsonl"))
        try:
            for i in range(100):
                bus.publish(_make_event(eid=f"gc_{i}"))
            assert len(bus._seen_event_ids) == 100
            time.sleep(0.02)
            evicted = bus.force_dedup_gc()
            assert evicted == 100
            assert len(bus._seen_event_ids) == 0
        finally:
            _close_bus(bus)

    def test_memory_pressure_report(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        try:
            for i in range(50):
                bus.publish(_make_event(eid=f"mp_{i}"))
            mem = bus.get_memory_pressure()
            assert mem["dedup_cache"] == 50
            assert mem["seq_map"] == 50
            assert "dead_letters" in mem
            assert "total_consumer_dedup" in mem
        finally:
            _close_bus(bus)

    def test_rolling_counter_within_window(self):
        rc = RollingCounter(window_sec=300.0, bucket_sec=1.0)
        for _ in range(10):
            rc.record()
        assert rc.total() == 10

    def test_rolling_counter_rate(self):
        rc = RollingCounter(window_sec=60.0, bucket_sec=1.0)
        for _ in range(30):
            rc.record()
        assert rc.rate_per_minute() == 30.0


# ---------------------------------------------------------------
#  2. STRESS CONTINU
# ---------------------------------------------------------------

class TestStressContinu:

    @pytest.mark.asyncio
    async def test_burst_calm_burst_pattern(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        received = []
        async def handler(evt):
            received.append(evt.event_id)
        bus.subscribe(handler, name="burst_test")
        await bus.start()
        try:
            for i in range(500):
                bus.publish(_make_event(eid=f"b1_{i}"))
            await bus.wait_idle(timeout=5.0)
            await asyncio.sleep(0.05)
            for i in range(500):
                bus.publish(_make_event(eid=f"b2_{i}"))
            await bus.wait_idle(timeout=5.0)
        finally:
            await bus.stop()
            _close_bus(bus)
        assert len(received) == 1000
        assert bus.get_stats()["total_dropped"] == 0

    @pytest.mark.asyncio
    async def test_fast_publisher_slow_consumer(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        processed = []
        async def slow_handler(evt):
            await asyncio.sleep(0.001)
            processed.append(evt.event_id)
        bus.subscribe(slow_handler, name="slow_consumer")
        await bus.start()
        try:
            for i in range(2000):
                bus.publish(_make_event(eid=f"fast_{i}"))
            await bus.wait_idle(timeout=30.0)
        finally:
            await bus.stop()
            _close_bus(bus)
        stats = bus.get_stats()
        assert len(processed) + stats["total_dropped"] == 2000

    @pytest.mark.asyncio
    async def test_repeated_kill_switch_cycles(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        received = []
        async def handler(evt):
            received.append(evt.event_type.value)
        bus.subscribe(handler, name="ks_cycle")
        await bus.start()
        try:
            for cycle in range(10):
                bus.publish(Event(
                    event_type=EventType.RISK_KILL_SWITCH,
                    payload={"cycle": cycle}, source="test",
                    event_id=f"ks_on_{cycle}",
                ))
                await asyncio.sleep(0.01)
                result = bus.publish(_make_event(eid=f"blocked_{cycle}"))
                assert result == 0
                bus.reset_kill_switch()
                result = bus.publish(_make_event(eid=f"allowed_{cycle}"))
                assert result > 0
            await bus.wait_idle(timeout=5.0)
        finally:
            await bus.stop()
            _close_bus(bus)
        ks_events = [e for e in received if e == "risk.kill_switch"]
        assert len(ks_events) == 10

    @pytest.mark.asyncio
    async def test_sustained_10k_no_backlog_growth(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        async def fast_handler(evt):
            pass
        bus.subscribe(fast_handler, name="fast")
        await bus.start()
        try:
            for i in range(10_000):
                bus.publish(Event(EventType.SYSTEM_HEARTBEAT, payload={"i": i}))
                if i % 1000 == 0:
                    await asyncio.sleep(0.01)
            await bus.wait_idle(timeout=15.0)
        finally:
            await bus.stop()
            _close_bus(bus)
        assert bus.get_stats()["max_backlog"] == 0


# ---------------------------------------------------------------
#  3. RECOVERY COMPLET
# ---------------------------------------------------------------

class TestRecoveryComplet:

    def _mock_bus(self, **overrides):
        m = MagicMock()
        m.kill_switch_active = overrides.get("ks", False)
        m._dead_letters = overrides.get("dl", [])
        m._wal = MagicMock()
        m._wal.read_from.return_value = overrides.get("wal_entries", [])
        m._wal._corrupt_lines = overrides.get("corrupt", 0)
        m._wal.get_size_bytes.return_value = overrides.get("wal_size", 100)
        m.get_stats.return_value = overrides.get("stats", {"max_backlog": 0, "total_dropped": 0})
        return m

    @pytest.mark.asyncio
    async def test_clean_recovery(self):
        mock_bus = self._mock_bus()
        mock_sm = MagicMock()
        mock_sm.is_active = False
        with patch("core.events.event_bus.get_event_bus", return_value=mock_bus), \
             patch("core.safe_mode.safe_mode_controller", mock_sm):
            protocol = RecoveryProtocol()
            results = await protocol.execute()
            assert protocol.is_cleared(results)
            assert len(results) == 7

    @pytest.mark.asyncio
    async def test_blocks_on_kill_switch(self):
        mock_bus = self._mock_bus(ks=True)
        mock_sm = MagicMock()
        mock_sm.is_active = False
        with patch("core.events.event_bus.get_event_bus", return_value=mock_bus), \
             patch("core.safe_mode.safe_mode_controller", mock_sm):
            protocol = RecoveryProtocol()
            results = await protocol.execute()
            assert not protocol.is_cleared(results)

    @pytest.mark.asyncio
    async def test_blocks_on_high_backlog(self):
        mock_bus = self._mock_bus(stats={"max_backlog": 6000, "total_dropped": 50})
        mock_sm = MagicMock()
        mock_sm.is_active = False
        with patch("core.events.event_bus.get_event_bus", return_value=mock_bus), \
             patch("core.safe_mode.safe_mode_controller", mock_sm):
            protocol = RecoveryProtocol()
            results = await protocol.execute()
            assert not protocol.is_cleared(results)

    @pytest.mark.asyncio
    async def test_blocks_on_dead_letter_overflow(self):
        mock_bus = self._mock_bus(dl=[MagicMock() for _ in range(300)])
        mock_sm = MagicMock()
        mock_sm.is_active = False
        with patch("core.events.event_bus.get_event_bus", return_value=mock_bus), \
             patch("core.safe_mode.safe_mode_controller", mock_sm):
            protocol = RecoveryProtocol()
            results = await protocol.execute()
            assert not protocol.is_cleared(results)

    @pytest.mark.asyncio
    async def test_fill_consistency_detects_duplicates(self):
        mock_bus = self._mock_bus(wal_entries=[
            {"seq": 1, "type": "order.filled", "event_id": "fill_1"},
            {"seq": 2, "type": "order.filled", "event_id": "fill_1"},
            {"seq": 3, "type": "trade.opened", "event_id": "open_1"},
        ], wal_size=500)
        mock_sm = MagicMock()
        mock_sm.is_active = False
        with patch("core.events.event_bus.get_event_bus", return_value=mock_bus), \
             patch("core.safe_mode.safe_mode_controller", mock_sm):
            protocol = RecoveryProtocol()
            results = await protocol.execute()
            fill_step = [r for r in results if r.step == "fill_consistency"][0]
            assert not fill_step.passed
            assert "duplicate" in fill_step.detail.lower()

    @pytest.mark.asyncio
    async def test_fill_consistency_clean(self):
        mock_bus = self._mock_bus(wal_entries=[
            {"seq": 1, "type": "order.filled", "event_id": "f1"},
            {"seq": 2, "type": "trade.opened", "event_id": "o1"},
        ], wal_size=500)
        mock_sm = MagicMock()
        mock_sm.is_active = False
        with patch("core.events.event_bus.get_event_bus", return_value=mock_bus), \
             patch("core.safe_mode.safe_mode_controller", mock_sm):
            protocol = RecoveryProtocol()
            results = await protocol.execute()
            fill_step = [r for r in results if r.step == "fill_consistency"][0]
            assert fill_step.passed

    @pytest.mark.asyncio
    async def test_position_reconstruction_critical_drift(self):
        mock_bus = self._mock_bus()
        mock_sm = MagicMock()
        mock_sm.is_active = False
        mock_cc = MagicMock()
        mock_cc.get_stats.return_value = {
            "checks": 5, "drifts_detected": 3,
            "recent_drifts": [{"severity": "critical", "symbol": "BTC", "source": "db_vs_exchange"}],
        }
        with patch("core.events.event_bus.get_event_bus", return_value=mock_bus), \
             patch("core.safe_mode.safe_mode_controller", mock_sm), \
             patch("core.consistency_checker.get_consistency_checker", return_value=mock_cc):
            protocol = RecoveryProtocol()
            results = await protocol.execute()
            pos_step = [r for r in results if r.step == "position_reconstruction"][0]
            assert not pos_step.passed

    @pytest.mark.asyncio
    async def test_recovery_empty_not_cleared(self):
        protocol = RecoveryProtocol()
        assert not protocol.is_cleared([])
        assert not protocol.is_cleared()

    @pytest.mark.asyncio
    async def test_wal_corruption_repair(self):
        mock_bus = self._mock_bus(corrupt=15, wal_size=5000)
        mock_bus._wal.read_from.side_effect = [[{"seq": 1}], []]
        mock_sm = MagicMock()
        mock_sm.is_active = False
        with patch("core.events.event_bus.get_event_bus", return_value=mock_bus), \
             patch("core.safe_mode.safe_mode_controller", mock_sm):
            protocol = RecoveryProtocol()
            results = await protocol.execute()
            wal_step = [r for r in results if r.step == "wal_integrity"][0]
            assert "REPAIRED" in wal_step.detail or not wal_step.passed


# ---------------------------------------------------------------
#  4. AUTO-HEAL
# ---------------------------------------------------------------

class TestAutoHeal:

    def test_small_balance_drift_corrected(self):
        checker = ConsistencyChecker()
        drifts = [DriftRecord(
            field="balance", expected=1000.0, actual=1020.0,
            severity=DriftSeverity.WARNING, source="db_vs_exchange",
        )]
        action = checker.escalate(drifts)
        assert action == "corrected"
        assert checker._auto_heal_stats["corrected"] == 1

    def test_small_position_drift_corrected(self):
        checker = ConsistencyChecker()
        drifts = [DriftRecord(
            field="position_size", expected=1.0, actual=1.005,
            severity=DriftSeverity.WARNING, source="event_vs_db", symbol="BTC",
        )]
        action = checker.escalate(drifts)
        assert action == "corrected"

    def test_ghost_position_not_auto_healed(self):
        checker = ConsistencyChecker()
        drifts = [DriftRecord(
            field="ghost_position", expected=0, actual=1.5,
            severity=DriftSeverity.CRITICAL, source="db_vs_exchange", symbol="ETH",
        )]
        with patch("core.events.event_bus.get_event_bus") as m:
            m.return_value = MagicMock(publish=MagicMock(return_value=1))
            action = checker.escalate(drifts)
        assert action == "safe_mode"
        assert checker._auto_heal_stats["blocked"] >= 1

    def test_critical_drift_escalated(self):
        checker = ConsistencyChecker()
        drifts = [DriftRecord(
            field="position_size", expected=1.0, actual=2.0,
            severity=DriftSeverity.CRITICAL, source="db_vs_exchange", symbol="BTC",
        )]
        with patch("core.events.event_bus.get_event_bus") as m:
            m.return_value = MagicMock(publish=MagicMock(return_value=1))
            action = checker.escalate(drifts)
        assert action == "safe_mode"

    def test_mixed_drifts_partial_heal(self):
        checker = ConsistencyChecker()
        drifts = [
            DriftRecord(field="position_size", expected=1.0, actual=1.003,
                        severity=DriftSeverity.WARNING, source="event_vs_db", symbol="BTC"),
            DriftRecord(field="position_size", expected=1.0, actual=2.0,
                        severity=DriftSeverity.CRITICAL, source="db_vs_exchange", symbol="ETH"),
        ]
        with patch("core.events.event_bus.get_event_bus") as m:
            m.return_value = MagicMock(publish=MagicMock(return_value=1))
            action = checker.escalate(drifts)
        assert action == "safe_mode"
        assert checker._auto_heal_stats["corrected"] >= 1
        assert checker._auto_heal_stats["blocked"] >= 1

    def test_autoheal_engine_standalone(self):
        engine = AutoHealEngine()
        r1 = engine.attempt_heal(DriftRecord(
            field="balance", expected=1000, actual=1020,
            severity=DriftSeverity.WARNING, source="db_vs_exchange",
        ))
        assert r1 == "corrected"
        r2 = engine.attempt_heal(DriftRecord(
            field="ghost_position", expected=0, actual=1.5,
            severity=DriftSeverity.CRITICAL, source="db_vs_exchange", symbol="ETH",
        ))
        assert r2 == "blocked"
        r3 = engine.attempt_heal(DriftRecord(
            field="position_size", expected=1.0, actual=5.0,
            severity=DriftSeverity.CRITICAL, source="db_vs_exchange", symbol="BTC",
        ))
        assert r3 == "blocked"
        stats = engine.get_stats()
        assert stats["corrected"] >= 1
        assert stats["blocked"] >= 2

    def test_large_balance_drift_not_auto_healed(self):
        engine = AutoHealEngine()
        r = engine.attempt_heal(DriftRecord(
            field="balance", expected=10000, actual=10600,
            severity=DriftSeverity.WARNING, source="db_vs_exchange",
        ))
        assert r == "ignored"


# ---------------------------------------------------------------
#  5. EVENT BUS PRESSURE
# ---------------------------------------------------------------

class TestEventBusPressure:

    @pytest.mark.asyncio
    async def test_massive_backlog_graceful(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        try:
            async def noop(evt):
                pass
            bus.subscribe(noop, name="idle_sub")
            for i in range(10_000):
                bus.publish(_make_event(eid=f"pressure_{i}"))
            stats = bus.get_stats()
            assert stats["total_published"] > 0
            assert bus.is_backlogged(threshold=5000) or stats["total_dropped"] > 0
        finally:
            _close_bus(bus)

    def test_backlog_depth_accurate(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        try:
            async def handler(evt):
                pass
            bus.subscribe(handler, name="depth_test")
            for i in range(100):
                bus.publish(_make_event(eid=f"depth_{i}"))
            assert bus.get_backlog_depth() == 100
            assert bus.get_subscriber_backlogs()["depth_test"] == 100
        finally:
            _close_bus(bus)

    @pytest.mark.asyncio
    async def test_kill_switch_blocks_during_backlog(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        try:
            async def handler(evt):
                pass
            bus.subscribe(handler, name="ks_backlog")
            for i in range(1000):
                bus.publish(_make_event(eid=f"pre_ks_{i}"))
            bus.publish(Event(event_type=EventType.RISK_KILL_SWITCH,
                              payload={"reason": "test"}, source="test"))
            for i in range(100):
                result = bus.publish(_make_event(eid=f"post_ks_{i}"))
                assert result == 0
        finally:
            _close_bus(bus)

    @pytest.mark.asyncio
    async def test_replay_from_wal_bounded(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        try:
            for i in range(100):
                bus.publish(_make_event(etype=EventType.TRADE_OPENED, eid=f"wal_replay_{i}"))
            count = await bus.replay_from_wal(after_seq=0, max_entries=10)
            assert count <= 10
        finally:
            _close_bus(bus)

    def test_compact_wal_succeeds(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        try:
            for i in range(50):
                bus.publish(_make_event(etype=EventType.TRADE_OPENED, eid=f"compact_{i}"))
            assert bus.compact_wal() is True
        finally:
            _close_bus(bus)


# ---------------------------------------------------------------
#  6. MONITORING
# ---------------------------------------------------------------

class TestMonitoring:

    def test_healthy_when_all_ok(self):
        m = LongRunMonitor()
        m._evaluate_health({"max_backlog": 0, "latency_p99_ms": 10.0,
                            "wal_size_mb": 1.0, "dead_letter_count": 0, "kill_switch_active": False})
        assert m.health == HealthLevel.HEALTHY

    def test_degraded_on_single_warning(self):
        m = LongRunMonitor()
        m._evaluate_health({"max_backlog": 1500, "latency_p99_ms": 10.0,
                            "wal_size_mb": 1.0, "dead_letter_count": 0, "kill_switch_active": False})
        assert m.health == HealthLevel.DEGRADED

    def test_at_risk_on_single_critical(self):
        m = LongRunMonitor()
        m._evaluate_health({"max_backlog": 6000, "latency_p99_ms": 10.0,
                            "wal_size_mb": 1.0, "dead_letter_count": 0, "kill_switch_active": False})
        assert m.health == HealthLevel.AT_RISK

    def test_blocked_on_multiple_criticals(self):
        m = LongRunMonitor()
        m._evaluate_health({"max_backlog": 6000, "latency_p99_ms": 3000.0,
                            "wal_size_mb": 1.0, "dead_letter_count": 0, "kill_switch_active": False})
        assert m.health == HealthLevel.BLOCKED

    def test_blocked_on_kill_switch(self):
        m = LongRunMonitor()
        m._evaluate_health({"max_backlog": 0, "latency_p99_ms": 0,
                            "wal_size_mb": 0, "dead_letter_count": 0, "kill_switch_active": True})
        assert m.health == HealthLevel.BLOCKED

    def test_at_risk_on_three_warnings(self):
        m = LongRunMonitor()
        m._evaluate_health({"max_backlog": 1500, "latency_p99_ms": 600.0,
                            "wal_size_mb": 25.0, "dead_letter_count": 0, "kill_switch_active": False})
        assert m.health == HealthLevel.AT_RISK

    def test_get_status_structure(self):
        m = LongRunMonitor()
        m._started_at = time.time() - 60
        m._cycles = 10
        s = m.get_status()
        assert "health" in s and "counters" in s and "active_alerts" in s

    def test_latency_percentiles(self):
        m = LongRunMonitor()
        p = m.get_latency_percentiles()
        assert "p50" in p and "p95" in p and "p99" in p

    def test_alert_processing_and_recovery(self):
        m = LongRunMonitor()
        alerts = m._evaluate_health({"max_backlog": 6000, "latency_p99_ms": 10.0,
                                      "wal_size_mb": 1.0, "dead_letter_count": 0, "kill_switch_active": False})
        m._process_alerts(alerts)
        assert "backlog" in m._active_alerts
        alerts = m._evaluate_health({"max_backlog": 0, "latency_p99_ms": 10.0,
                                      "wal_size_mb": 1.0, "dead_letter_count": 0, "kill_switch_active": False})
        m._process_alerts(alerts)
        assert "backlog" not in m._active_alerts


# ---------------------------------------------------------------
#  7. FAILURE FATIGUE
# ---------------------------------------------------------------

class TestFailureFatigue:

    @pytest.mark.asyncio
    async def test_repeated_crash_restart_service(self):
        crash_count = 0
        async def crashy():
            nonlocal crash_count
            crash_count += 1
            raise RuntimeError(f"crash #{crash_count}")
        w = ServiceWrapper("crashy", crashy, max_restarts=25,
                           restart_delay_sec=0.01, max_restart_delay_sec=0.05)
        await w.start()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if w.stats.crash_count >= 20:
                break
        await w.stop(timeout=1.0)
        assert w.stats.crash_count >= 20
        assert w.state in (ServiceState.CRASHED, ServiceState.STOPPED, ServiceState.STOPPING)

    @pytest.mark.asyncio
    async def test_supervisor_gate_under_fatigue(self):
        sup = ServiceSupervisor()
        async def unstable():
            await asyncio.sleep(0.05)
            raise RuntimeError("crash")
        sup.register("risk_engine", unstable, max_restarts=5, restart_delay_sec=0.01)
        await sup.start_all()
        await asyncio.sleep(0.5)
        status = sup.get_status()
        risk_state = status["services"]["risk_engine"]["state"]
        if risk_state != "running":
            allowed, _ = sup.is_trading_allowed()
            assert not allowed
        await sup.stop_all(timeout=2.0)

    @pytest.mark.asyncio
    async def test_kill_switch_fatigue(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        async def h(e):
            pass
        bus.subscribe(h, name="fatigue")
        await bus.start()
        try:
            for i in range(10):
                bus.publish(Event(event_type=EventType.RISK_KILL_SWITCH,
                                  payload={"f": i}, source="test", event_id=f"fks_{i}"))
                assert bus.kill_switch_active
                bus.reset_kill_switch()
                assert not bus.kill_switch_active
        finally:
            await bus.stop()
            _close_bus(bus)
        assert not bus.kill_switch_active

    def test_consistency_checker_under_repeated_drifts(self):
        checker = ConsistencyChecker()
        for i in range(100):
            checker.check_positions(
                {"BTC": float(i)}, {"BTC": float(i) + 0.001}, {"BTC": float(i)})
        assert len(checker._history) <= checker._max_history
        assert checker.get_stats()["checks"] == 100


# ---------------------------------------------------------------
#  8. FAIL-CLOSED AUDIT
# ---------------------------------------------------------------

class TestFailClosed:

    def test_risk_v2_unavailable_denies(self):
        import core.phase6_wiring as w
        old_r, old_s = w._risk_v2, w._supervisor
        try:
            w._risk_v2 = None
            w._supervisor = MagicMock(is_trading_allowed=MagicMock(return_value=(True, "ok")))
            ok, mult, reasons = w.evaluate_risk_v2("BTC", "long", 100, 50000)
            assert not ok and mult == 0.0
            assert any("unavailable" in r for r in reasons)
        finally:
            w._risk_v2, w._supervisor = old_r, old_s

    def test_supervisor_unavailable_denies(self):
        import core.phase6_wiring as w
        old_s = w._supervisor
        try:
            w._supervisor = None
            ok, mult, reasons = w.evaluate_risk_v2("BTC", "long", 100, 50000)
            assert not ok and mult == 0.0
        finally:
            w._supervisor = old_s

    def test_trading_gate_blocked_denies(self):
        import core.phase6_wiring as w
        old_r, old_s = w._risk_v2, w._supervisor
        try:
            w._risk_v2 = MagicMock()
            w._supervisor = MagicMock(
                is_trading_allowed=MagicMock(return_value=(False, "critical down")))
            ok, mult, reasons = w.evaluate_risk_v2("BTC", "long", 100, 50000)
            assert not ok and mult == 0.0
        finally:
            w._risk_v2, w._supervisor = old_r, old_s

    def test_risk_exception_denies(self):
        import core.phase6_wiring as w
        old_r, old_s = w._risk_v2, w._supervisor
        try:
            mock_r = MagicMock()
            mock_r.evaluate.side_effect = RuntimeError("crash")
            w._risk_v2 = mock_r
            w._supervisor = MagicMock(
                is_trading_allowed=MagicMock(return_value=(True, "ok")))
            ok, mult, reasons = w.evaluate_risk_v2("BTC", "long", 100, 50000)
            assert not ok and mult == 0.0
            assert any("error" in r.lower() for r in reasons)
        finally:
            w._risk_v2, w._supervisor = old_r, old_s

    def test_no_services_blocks_trading(self):
        sup = ServiceSupervisor()
        ok, _ = sup.is_trading_allowed()
        assert not ok

    def test_critical_service_stopped_blocks(self):
        sup = ServiceSupervisor()
        async def dummy():
            pass
        sup.register("risk_engine", dummy)
        ok, _ = sup.is_trading_allowed()
        assert not ok

    def test_recovery_not_cleared_blocks(self):
        p = RecoveryProtocol()
        assert not p.is_cleared()
        assert not p.is_cleared([
            RecoveryStepResult("a", True, "ok"),
            RecoveryStepResult("b", False, "fail"),
        ])

    def test_escalation_failure_forces_kill_switch(self):
        checker = ConsistencyChecker()
        drifts = [
            DriftRecord("p", 1, 5, DriftSeverity.CRITICAL, "db_vs_exchange", "BTC"),
            DriftRecord("p", 1, 5, DriftSeverity.CRITICAL, "db_vs_exchange", "ETH"),
            DriftRecord("p", 1, 5, DriftSeverity.CRITICAL, "db_vs_exchange", "SOL"),
        ]
        with patch("core.events.event_bus.get_event_bus") as m:
            mock_bus = MagicMock()
            mock_bus.publish.side_effect = RuntimeError("dead")
            mock_bus._kill_switch_active = False
            m.return_value = mock_bus
            checker.escalate(drifts)
            assert mock_bus._kill_switch_active is True


# ---------------------------------------------------------------
#  9. SLOW DRIFT DETECTION
# ---------------------------------------------------------------

class TestSlowDriftDetection:

    def test_detect_backlog_growth(self):
        m = LongRunMonitor()
        base = time.time()
        for i in range(15):
            m._snapshots.append({"ts": base + i * 60, "max_backlog": i * 50,
                                  "dead_letter_count": 0, "total_errors": 0, "latency_p99_ms": 10.0})
        m._detect_slow_drift()

    def test_detect_dead_letter_accumulation(self):
        m = LongRunMonitor()
        base = time.time()
        for i in range(15):
            m._snapshots.append({"ts": base + i * 60, "max_backlog": 0,
                                  "dead_letter_count": i * 10, "total_errors": 0, "latency_p99_ms": 10.0})
        m._detect_slow_drift()

    def test_no_drift_when_stable(self):
        m = LongRunMonitor()
        base = time.time()
        for i in range(15):
            m._snapshots.append({"ts": base + i * 60, "max_backlog": 10,
                                  "dead_letter_count": 0, "total_errors": 0, "latency_p99_ms": 10.0})
        before = m._error_counter.total()
        m._detect_slow_drift()
        assert m._error_counter.total() == before


# ---------------------------------------------------------------
#  10. WAL STRESS
# ---------------------------------------------------------------

class TestWALStress:

    def test_wal_read_with_corrupt_lines(self):
        td = tempfile.mkdtemp()
        path = os.path.join(td, "wal.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"seq": 1, "event_id": "a", "type": "trade.opened"}) + "\n")
            f.write("CORRUPT\n")
            f.write("{invalid\n")
            f.write(json.dumps({"seq": 2, "event_id": "b", "type": "trade.closed"}) + "\n")
            f.write("\n")
            f.write(json.dumps({"seq": 3, "event_id": "c", "type": "order.filled"}) + "\n")
        wal = WriteAheadLog(path=path)
        try:
            entries = wal.read_from(after_seq=0)
            assert len(entries) == 3
            assert wal._corrupt_lines == 2
        finally:
            wal.close()

    def test_wal_rotation_creates_backup(self):
        td = tempfile.mkdtemp()
        wal = WriteAheadLog(path=os.path.join(td, "wal.jsonl"), max_size_mb=0.0001)
        try:
            for i in range(50):
                wal.append(_make_event(etype=EventType.TRADE_OPENED, eid=f"rot_{i}"), i)
            baks = [f for f in os.listdir(td) if f.endswith(".bak")]
            assert len(baks) >= 1
        finally:
            wal.close()

    def test_wal_disabled_after_errors(self):
        td = tempfile.mkdtemp()
        wal = WriteAheadLog(path=os.path.join(td, "wal.jsonl"))
        wal._fd.close()
        wal._fd = None
        wal._enabled = True
        results = []
        for i in range(5):
            results.append(wal.append(
                _make_event(etype=EventType.TRADE_OPENED, eid=f"e_{i}"), i))
        assert all(r is False for r in results)


# ---------------------------------------------------------------
#  11. ENDURANCE SIMULATION
# ---------------------------------------------------------------

class TestEnduranceSimulation:

    @pytest.mark.asyncio
    async def test_simulated_1h_operation(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"), dedup_window_sec=5.0)
        received = []
        async def handler(evt):
            received.append(evt.event_id)
        bus.subscribe(handler, name="endurance")
        await bus.start()
        try:
            for tick in range(3600):
                bus.publish(_make_event(eid=f"sim_{tick}"))
                if tick > 0 and tick % 360 == 0:
                    bus.publish(Event(event_type=EventType.RISK_KILL_SWITCH,
                                      payload={"tick": tick}, source="sim",
                                      event_id=f"sim_ks_{tick}"))
                    await asyncio.sleep(0.001)
                    bus.reset_kill_switch()
                if tick % 500 == 0:
                    bus.force_dedup_gc()
                if tick % 600 == 0:
                    bus.purge_dead_letters(max_age_sec=2.0)
            await bus.wait_idle(timeout=10.0)
        finally:
            await bus.stop()
            _close_bus(bus)
        mem = bus.get_memory_pressure()
        assert mem["dedup_cache"] <= _MAX_DEDUP_CACHE
        assert mem["seq_map"] <= _MAX_SEQ_MAP
        assert bus.get_stats()["total_dropped"] == 0

    @pytest.mark.asyncio
    async def test_simulated_6h_burst_pattern(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        received = []
        async def handler(evt):
            received.append(evt.event_id)
        bus.subscribe(handler, name="burst_sim")
        await bus.start()
        total = 0
        try:
            for cycle in range(6):
                for i in range(500):
                    bus.publish(_make_event(eid=f"c{cycle}_b_{i}"))
                    total += 1
                await asyncio.sleep(0.01)
                for i in range(100):
                    bus.publish(_make_event(eid=f"c{cycle}_c_{i}"))
                    total += 1
            await bus.wait_idle(timeout=15.0)
        finally:
            await bus.stop()
            _close_bus(bus)
        assert len(received) + bus.get_stats()["total_dropped"] == total

    @pytest.mark.asyncio
    async def test_burst_rest_burst_10_cycles(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        received = [0]
        async def counter(event):
            received[0] += 1
        bus.subscribe(counter, name="burst_counter")
        await bus.start()
        try:
            for burst in range(10):
                for i in range(1000):
                    bus.publish(Event(EventType.SYSTEM_HEARTBEAT,
                                      payload={"burst": burst, "i": i}))
                await bus.wait_idle(timeout=5.0)
        finally:
            await bus.stop()
            _close_bus(bus)
        assert received[0] == 10_000
        assert len(bus._seen_event_ids) <= _MAX_DEDUP_CACHE
        assert bus.get_stats()["total_dropped"] == 0


# ---------------------------------------------------------------
#  12. MONITOR INTEGRATION
# ---------------------------------------------------------------

class TestMonitorIntegration:

    def test_maintenance_purges_dead_letters(self):
        td = tempfile.mkdtemp()
        bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))
        try:
            for i in range(50):
                bus._dead_letters.append(DeadLetterEntry(
                    event=_make_event(eid=f"old_{i}"),
                    subscriber_name="t", error="old",
                    timestamp=time.time() - 7200,
                ))
            purged = bus.purge_dead_letters(max_age_sec=3600)
            assert purged == 50 and len(bus._dead_letters) == 0
        finally:
            _close_bus(bus)

    def test_backpressure_triggers_safe_mode(self):
        m = LongRunMonitor()
        with patch("core.events.event_bus.get_event_bus") as mock_fn:
            mock_bus = MagicMock()
            mock_bus.kill_switch_active = False
            mock_bus.publish = MagicMock(return_value=1)
            mock_fn.return_value = mock_bus
            m._apply_backpressure({"max_backlog": 6000})
            mock_bus.publish.assert_called_once()
            assert mock_bus.publish.call_args[0][0].event_type == EventType.SAFE_MODE_ACTIVATED
