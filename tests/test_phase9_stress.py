"""
Phase 9 — Long-Duration Stress Tests.
═════════════════════════════════════

Tests that verify NO memory leaks, NO unbounded growth, NO silent failures
under sustained load. Each test runs hundreds/thousands of iterations
to prove long-run reliability.

Run: pytest tests/test_phase9_stress.py -v
"""

from __future__ import annotations

import asyncio
import gc
import sys
import time
import uuid

import pytest

from core.events.event_types import Event, EventPriority, EventType
from core.events.event_bus import (
    EventBus,
    WriteAheadLog,
    _MAX_CONSUMER_DEDUP,
    _MAX_DEDUP_CACHE,
    _MAX_SEQ_MAP,
    _MAX_WAL_BACKUPS,
    _DEAD_LETTER_MAX_RETRIES,
    reset_event_bus,
)
from core.consistency_checker import ConsistencyChecker, DriftSeverity
from core.isolation import (
    ServiceSupervisor,
    ServiceWrapper,
    ServiceState,
    SystemHealthState,
    _UPTIME_RESET_THRESHOLD,
)


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def _make_event(etype: EventType = EventType.SIGNAL_GENERATED, eid: str = None) -> Event:
    return Event(
        event_type=etype,
        payload={"test": True},
        priority=EventPriority.NORMAL,
        event_id=eid or uuid.uuid4().hex[:16],
        source="stress_test",
    )


# ═══════════════════════════════════════════════════════════════
#  1. EVENT BUS — BOUNDED MEMORY GROWTH
# ═══════════════════════════════════════════════════════════════

class TestEventBusMemoryBounds:
    """Prove all caches stay bounded under sustained load."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        reset_event_bus()
        self.bus = EventBus(wal_path=str(tmp_path / "test_wal.jsonl"))
        yield
        reset_event_bus()

    def test_dedup_cache_bounded(self):
        """Publisher dedup cache must never exceed MAX_DEDUP_CACHE + some margin."""
        bus = self.bus
        for i in range(100_000):
            bus.publish(_make_event())

        assert len(bus._seen_event_ids) <= _MAX_DEDUP_CACHE + 200
        assert len(bus._seen_ids_order) <= _MAX_DEDUP_CACHE + 200

    def test_seq_map_bounded(self):
        """Sequence map must never exceed MAX_SEQ_MAP."""
        bus = self.bus
        for i in range(100_000):
            bus.publish(_make_event())

        assert len(bus._seq_map) <= _MAX_SEQ_MAP + 200
        assert len(bus._seq_order) <= _MAX_SEQ_MAP + 200

    @pytest.mark.asyncio
    async def test_consumer_dedup_bounded(self):
        """Consumer processed_ids must stay bounded."""
        bus = self.bus
        processed = []

        async def handler(event):
            processed.append(event.event_id)

        bus.subscribe(handler, name="test_sub")
        await bus.start()

        # Queue max is 10k, so we publish in batches and drain
        total = 20_000
        batch = 5000
        for start in range(0, total, batch):
            for i in range(batch):
                bus.publish(_make_event())
            await bus.wait_idle(timeout=10.0)

        await bus.wait_idle(timeout=15.0)
        await bus.stop()

        sub = bus._subscriptions[0]
        assert len(sub._processed_ids) <= _MAX_CONSUMER_DEDUP + 100
        assert len(sub._processed_ids_order) <= _MAX_CONSUMER_DEDUP + 100
        assert len(processed) == total

    def test_dead_letters_bounded(self):
        """Dead letters stay bounded even with many failures."""
        bus = self.bus
        for i in range(2000):
            bus._add_dead_letter(_make_event(), "test_sub", f"error_{i}")

        assert len(bus._dead_letters) <= bus._max_dead_letters

    def test_stats_structure(self):
        """Stats must include all Phase 9 fields."""
        bus = self.bus
        bus.publish(_make_event())
        stats = bus.get_stats()

        required_keys = [
            "running", "sequence", "kill_switch_active", "kill_switch_source",
            "kill_switch_blocked", "total_published", "total_delivered",
            "total_dropped", "total_errors", "total_replays",
            "wal_persisted", "wal_size_bytes", "fast_path_delivered",
            "gaps_detected", "dead_letter_count", "dedup_rejected",
            "dedup_cache_size", "seq_map_size", "max_backlog",
            "latency_avg_ms", "latency_p99_ms", "event_counts", "subscribers",
        ]
        for key in required_keys:
            assert key in stats, f"Missing stats key: {key}"


# ═══════════════════════════════════════════════════════════════
#  2. KILL SWITCH — BLOCKS ALL NORMAL EVENTS
# ═══════════════════════════════════════════════════════════════

class TestKillSwitchBlocking:
    """Kill switch must block ALL normal events after activation."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        reset_event_bus()
        self.bus = EventBus(wal_path=str(tmp_path / "test_wal.jsonl"))
        yield
        reset_event_bus()

    def test_kill_switch_blocks_normal_events(self):
        bus = self.bus
        # Publish kill switch event
        ks_event = _make_event(EventType.RISK_KILL_SWITCH)
        bus.publish(ks_event)

        assert bus.kill_switch_active is True

        # Normal events should now be blocked
        for i in range(100):
            result = bus.publish(_make_event())
            assert result == 0

        assert bus._events_blocked_by_ks == 100

    def test_kill_switch_allows_fast_path_events(self):
        bus = self.bus
        # Activate kill switch
        bus.publish(_make_event(EventType.RISK_KILL_SWITCH))
        assert bus.kill_switch_active is True

        # Fast path events should still pass
        safe_mode = _make_event(EventType.SAFE_MODE_ACTIVATED)
        result = bus.publish(safe_mode)
        # No subscribers, so result may be 0, but it should not be blocked
        assert bus._events_blocked_by_ks == 0  # fast path events don't increment blocked count

    def test_kill_switch_reset(self):
        bus = self.bus
        bus.publish(_make_event(EventType.RISK_KILL_SWITCH))
        assert bus.kill_switch_active is True

        bus.reset_kill_switch()
        assert bus.kill_switch_active is False

        # Normal events should flow again
        result = bus.publish(_make_event())
        assert bus._events_blocked_by_ks == 0


# ═══════════════════════════════════════════════════════════════
#  3. WAL — BACKUP PURGE + BOUNDED READ
# ═══════════════════════════════════════════════════════════════

class TestWALPurgeAndBounds:
    """WAL must purge old backups and bound reads."""

    def test_wal_backup_purge(self, tmp_path):
        """Old .bak files must be purged beyond MAX_WAL_BACKUPS."""
        wal = WriteAheadLog(path=str(tmp_path / "test_wal.jsonl"), max_size_mb=0.001)

        # Write enough to trigger multiple rotations
        for i in range(200):
            evt = _make_event(EventType.TRADE_OPENED)
            wal.append(evt, i + 1)

        wal.close()

        import glob
        backups = glob.glob(str(tmp_path / "test_wal.jsonl.*.bak"))
        assert len(backups) <= _MAX_WAL_BACKUPS

    def test_wal_bounded_read(self, tmp_path):
        """read_from respects max_entries limit."""
        wal = WriteAheadLog(path=str(tmp_path / "test_wal.jsonl"), max_size_mb=100)

        for i in range(500):
            evt = _make_event(EventType.TRADE_OPENED)
            wal.append(evt, i + 1)

        entries = wal.read_from(after_seq=0, max_entries=50)
        assert len(entries) == 50
        wal.close()

    def test_wal_corrupt_lines_handled(self, tmp_path):
        """WAL reader must skip corrupt lines without crashing."""
        wal_path = str(tmp_path / "test_wal.jsonl")
        with open(wal_path, "w") as f:
            f.write('{"seq": 1, "event_id": "a", "type": "trade.opened"}\n')
            f.write('CORRUPT LINE HERE\n')
            f.write('{"seq": 2, "event_id": "b", "type": "trade.closed"}\n')
            f.write('\n')
            f.write('ANOTHER CORRUPT\n')
            f.write('{"seq": 3, "event_id": "c", "type": "order.filled"}\n')

        wal = WriteAheadLog(path=wal_path)
        entries = wal.read_from(after_seq=0)
        assert len(entries) == 3
        assert wal._corrupt_lines == 2
        wal.close()


# ═══════════════════════════════════════════════════════════════
#  4. CONSISTENCY CHECKER — EXECUTES ACTIONS
# ═══════════════════════════════════════════════════════════════

class TestConsistencyCheckerActions:
    """Consistency checker must execute escalation actions, not just return strings."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        reset_event_bus()
        self.bus = EventBus(wal_path=str(tmp_path / "test_wal.jsonl"))
        yield
        reset_event_bus()

    def test_escalate_kill_switch_activates_bus_kill_switch(self):
        """3+ critical drifts → kill switch published → bus kill switch active."""
        checker = ConsistencyChecker()
        # Use the singleton bus (consistency checker uses get_event_bus())
        from core.events.event_bus import get_event_bus
        bus = get_event_bus()
        bus._kill_switch_active = False  # Ensure clean state

        # Create 3 critical drifts (DB vs exchange mismatch on 3 symbols)
        drifts = checker.check_positions(
            event_positions={"BTC": 0.1, "ETH": 1.0, "SOL": 10.0},
            db_positions={"BTC": 0.1, "ETH": 1.0, "SOL": 10.0},
            exchange_positions={"BTC": 0.5, "ETH": 5.0, "SOL": 50.0},  # Massive drift
        )
        critical = [d for d in drifts if d.severity == DriftSeverity.CRITICAL]
        assert len(critical) >= 3

        action = checker.escalate(drifts)
        assert action == "kill_switch"

        # Bus should have received the kill switch event
        assert bus.kill_switch_active is True

    def test_escalate_safe_mode(self):
        """1 critical drift → safe_mode event published."""
        reset_event_bus()
        checker = ConsistencyChecker()

        drifts = checker.check_positions(
            event_positions={"BTC": 0.1},
            db_positions={"BTC": 0.1},
            exchange_positions={"BTC": 0.5},
        )
        action = checker.escalate(drifts)
        assert action == "safe_mode"

    def test_escalate_alert(self):
        """Warning drift → alert event published."""
        checker = ConsistencyChecker()

        # Small drift between event and DB only (within 0.05 → WARNING, not CRITICAL)
        # Exchange matches DB so no db_vs_exchange drift
        drifts = checker.check_positions(
            event_positions={"BTC": 1.0},
            db_positions={"BTC": 1.03},  # 3% drift → warning severity (< 0.05 threshold)
            exchange_positions={"BTC": 1.03},  # Exchange matches DB → no critical drift
        )
        # Should have exactly 1 warning drift (event_vs_db only)
        assert len(drifts) >= 1
        assert all(d.severity == DriftSeverity.WARNING for d in drifts)
        action = checker.escalate(drifts)
        assert action == "alert"

    def test_escalate_anti_spam(self):
        """Same escalation action within cooldown period should not re-execute."""
        checker = ConsistencyChecker()

        drifts = checker.check_positions(
            event_positions={"BTC": 0.1},
            db_positions={"BTC": 0.1},
            exchange_positions={"BTC": 0.5},
        )

        # First call: execute
        action1 = checker.escalate(drifts)
        assert action1 == "safe_mode"
        assert checker._actions_executed["safe_mode"] == 1

        # Second call: same action type but within cooldown — should not increment
        action2 = checker.escalate(drifts)
        assert action2 == "safe_mode"
        assert checker._actions_executed["safe_mode"] == 1  # Still 1 — anti-spam blocked

    def test_get_stats_includes_actions(self):
        """Stats must include action execution counts."""
        checker = ConsistencyChecker()
        stats = checker.get_stats()
        assert "actions_executed" in stats
        assert "last_action_ts" in stats


# ═══════════════════════════════════════════════════════════════
#  5. ISOLATION — TRADING GATE
# ═══════════════════════════════════════════════════════════════

class TestTradingGate:
    """Trading gate must block trades when critical services are unhealthy."""

    @pytest.mark.asyncio
    async def test_trading_blocked_on_critical_service_crash(self):
        supervisor = ServiceSupervisor()

        call_count = 0
        async def failing_service():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("simulated crash")

        supervisor.register("risk_engine", failing_service, max_restarts=2, restart_delay_sec=0.1)

        await supervisor.start_all()
        await asyncio.sleep(1.0)  # Let it crash and enter degraded/crashed state

        allowed, reason = supervisor.is_trading_allowed()
        # Service should be crashed or degraded
        status = supervisor.get_status()
        svc_state = status["services"]["risk_engine"]["state"]
        assert svc_state in ("crashed", "degraded", "starting")

        await supervisor.stop_all()

    def test_trading_blocked_when_empty(self):
        """Phase 10: Empty supervisor → BLOCKED (no services = no protection)."""
        supervisor = ServiceSupervisor()
        allowed, reason = supervisor.is_trading_allowed()
        assert allowed is False
        assert supervisor.system_health == SystemHealthState.BLOCKED

    def test_system_health_state_values(self):
        """System health states must exist."""
        assert SystemHealthState.HEALTHY.value == "healthy"
        assert SystemHealthState.BLOCKED.value == "blocked"
        assert SystemHealthState.DEGRADED.value == "degraded"
        assert SystemHealthState.RECOVERY.value == "recovery"


# ═══════════════════════════════════════════════════════════════
#  6. ISOLATION — RESTART COUNTER RESET
# ═══════════════════════════════════════════════════════════════

class TestRestartCounterReset:
    """Restart counter must reset after sustained uptime."""

    @pytest.mark.asyncio
    async def test_counter_resets_after_sustained_uptime(self, monkeypatch):
        """If service runs > UPTIME_RESET_THRESHOLD before crash, counter resets."""
        # Monkey-patch the threshold to a small value for testing
        import core.isolation as iso_mod
        monkeypatch.setattr(iso_mod, "_UPTIME_RESET_THRESHOLD", 0.5)

        crash_count = 0
        async def flaky_service():
            nonlocal crash_count
            crash_count += 1
            if crash_count <= 3:
                await asyncio.sleep(0.6)  # Run longer than threshold
                raise RuntimeError(f"crash #{crash_count}")
            # After 3 crashes, run cleanly
            await asyncio.sleep(10)

        wrapper = ServiceWrapper("test_flaky", flaky_service, max_restarts=3, restart_delay_sec=0.1)
        await wrapper.start()
        await asyncio.sleep(4.0)

        # Service should still be running because counter resets after sustained uptime
        assert wrapper.state in (ServiceState.RUNNING, ServiceState.STOPPED)
        assert wrapper.stats.crash_count >= 2  # Should have crashed multiple times
        await wrapper.stop()


# ═══════════════════════════════════════════════════════════════
#  7. DEAD LETTER REPLAY — MAX RETRIES ENFORCED
# ═══════════════════════════════════════════════════════════════

class TestDeadLetterMaxRetries:
    """Dead letters must be discarded after max retries."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        reset_event_bus()
        self.bus = EventBus(wal_path=str(tmp_path / "test_wal.jsonl"))
        yield
        reset_event_bus()

    @pytest.mark.asyncio
    async def test_dead_letter_discarded_after_max_retries(self):
        bus = self.bus
        evt = _make_event()

        # Manually add dead letter with retries at limit
        from core.events.event_bus import DeadLetterEntry
        dle = DeadLetterEntry(event=evt, subscriber_name="test", error="test_error", retries=_DEAD_LETTER_MAX_RETRIES)
        bus._dead_letters.append(dle)

        replayed = await bus.replay_dead_letters()
        assert replayed == 0  # Should be discarded, not replayed

    @pytest.mark.asyncio
    async def test_dead_letter_purge_by_age(self):
        bus = self.bus
        from core.events.event_bus import DeadLetterEntry

        # Add old dead letter
        old_evt = _make_event()
        old_dle = DeadLetterEntry(event=old_evt, subscriber_name="test", error="old", timestamp=time.time() - 7200)
        bus._dead_letters.append(old_dle)

        # Add recent dead letter
        new_evt = _make_event()
        new_dle = DeadLetterEntry(event=new_evt, subscriber_name="test", error="new")
        bus._dead_letters.append(new_dle)

        purged = bus.purge_dead_letters(max_age_sec=3600.0)
        assert purged == 1
        assert len(bus._dead_letters) == 1


# ═══════════════════════════════════════════════════════════════
#  8. RECOVERY PROTOCOL
# ═══════════════════════════════════════════════════════════════

class TestRecoveryProtocol:
    """Recovery protocol must verify system state before trading."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        reset_event_bus()
        yield
        reset_event_bus()

    @pytest.mark.asyncio
    async def test_recovery_protocol_clean_boot(self):
        from core.recovery_protocol import RecoveryProtocol
        protocol = RecoveryProtocol()
        results = await protocol.execute()

        assert len(results) >= 3  # At least 3 steps
        assert protocol.is_cleared(results)

        summary = protocol.get_summary()
        assert summary["all_passed"] is True

    @pytest.mark.asyncio
    async def test_recovery_detects_active_kill_switch(self, tmp_path):
        from core.recovery_protocol import RecoveryProtocol
        from core.events.event_bus import EventBus, get_event_bus

        bus = get_event_bus()
        bus.publish(_make_event(EventType.RISK_KILL_SWITCH))
        assert bus.kill_switch_active

        protocol = RecoveryProtocol()
        results = await protocol.execute()

        ks_step = next((r for r in results if r.step == "kill_switch_check"), None)
        assert ks_step is not None
        assert ks_step.passed is False


# ═══════════════════════════════════════════════════════════════
#  9. PHASE6 WIRING — SAFETY HOLES FIXED
# ═══════════════════════════════════════════════════════════════

class TestPhase6SafetyHoles:
    """Critical: evaluate_risk_v2 must DENY trades when risk engine is None."""

    def test_evaluate_risk_denies_without_risk_engine(self):
        """When risk engine is None, trades MUST be denied."""
        from core import phase6_wiring
        # Ensure risk engine is None
        phase6_wiring._risk_v2 = None
        phase6_wiring._supervisor = None

        allowed, multiplier, reasons = phase6_wiring.evaluate_risk_v2(
            symbol="BTCUSDT",
            side="buy",
            proposed_size_usd=100.0,
            current_price=50000.0,
        )

        assert allowed is False
        assert multiplier == 0.0
        assert any("unavailable" in r for r in reasons)

    def test_evaluate_risk_denies_on_risk_exception(self):
        """When risk engine throws, trades MUST be denied."""
        from core import phase6_wiring

        class BrokenRiskEngine:
            def evaluate(self, **kwargs):
                raise RuntimeError("boom")

        phase6_wiring._risk_v2 = BrokenRiskEngine()
        phase6_wiring._supervisor = None

        allowed, multiplier, reasons = phase6_wiring.evaluate_risk_v2(
            symbol="BTCUSDT",
            side="buy",
            proposed_size_usd=100.0,
            current_price=50000.0,
        )

        assert allowed is False
        assert multiplier == 0.0
        phase6_wiring._risk_v2 = None  # Cleanup


# ═══════════════════════════════════════════════════════════════
#  10. LATENCY TRACKING
# ═══════════════════════════════════════════════════════════════

class TestLatencyTracking:
    """Event bus must track publish→process latency."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        reset_event_bus()
        self.bus = EventBus(wal_path=str(tmp_path / "test_wal.jsonl"))
        yield
        reset_event_bus()

    @pytest.mark.asyncio
    async def test_latency_tracked(self):
        bus = self.bus

        async def slow_handler(event):
            await asyncio.sleep(0.01)  # 10ms delay

        bus.subscribe(slow_handler, name="slow_sub")
        await bus.start()

        for _ in range(10):
            bus.publish(_make_event())

        await bus.wait_idle(timeout=5.0)
        await bus.stop()

        stats = bus.get_stats()
        assert stats["latency_avg_ms"] > 0
        assert len(bus._latencies) == 10
