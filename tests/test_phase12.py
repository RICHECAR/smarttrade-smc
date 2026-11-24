"""
Phase 12 Tests — Live validation, state machine, capital scaling,
                 incident memory, risk adaptation, temporal monitoring.
═══════════════════════════════════════════════════════════════════════
"""

import json
import os
import tempfile
import time
import pytest


# ═══════════════════════════════════════════════════════════════
#  1. EXPLOITATION MODE
# ═══════════════════════════════════════════════════════════════


class TestExploitationMode:

    def test_default_mode_is_paper(self, monkeypatch):
        monkeypatch.delenv("TRADING_MODE", raising=False)
        from core.exploitation_mode import ExploitationModeController, ExploitationMode
        ctrl = ExploitationModeController()
        assert ctrl.mode == ExploitationMode.PAPER

    def test_mode_from_env_live(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "live")
        from core.exploitation_mode import ExploitationModeController, ExploitationMode
        ctrl = ExploitationModeController()
        assert ctrl.mode == ExploitationMode.LIVE

    def test_mode_from_env_micro(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "micro_capital")
        from core.exploitation_mode import ExploitationModeController, ExploitationMode
        ctrl = ExploitationModeController()
        assert ctrl.mode == ExploitationMode.MICRO_CAPITAL

    def test_set_mode(self, monkeypatch):
        monkeypatch.delenv("TRADING_MODE", raising=False)
        from core.exploitation_mode import ExploitationModeController, ExploitationMode
        ctrl = ExploitationModeController()
        ctrl.set_mode(ExploitationMode.LIVE)
        assert ctrl.mode == ExploitationMode.LIVE

    def test_profile_paper(self, monkeypatch):
        monkeypatch.delenv("TRADING_MODE", raising=False)
        from core.exploitation_mode import ExploitationModeController, ExploitationMode
        ctrl = ExploitationModeController()
        p = ctrl.profile
        assert p.mode == ExploitationMode.PAPER
        assert p.max_position_size_eur == 10_000

    def test_profile_micro(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "micro_capital")
        from core.exploitation_mode import ExploitationModeController
        ctrl = ExploitationModeController()
        p = ctrl.profile
        assert p.max_position_size_eur == 100
        assert p.kill_switch_drawdown_pct == 5.0
        assert p.alert_on_every_trade is True

    def test_check_position_allowed(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "micro_capital")
        from core.exploitation_mode import ExploitationModeController
        ctrl = ExploitationModeController()
        ok, reason = ctrl.check_position_allowed(50, 100, 1)
        assert ok is True
        ok, reason = ctrl.check_position_allowed(200, 100, 1)
        assert ok is False
        assert "mode limit" in reason

    def test_check_exposure_limit(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "micro_capital")
        from core.exploitation_mode import ExploitationModeController
        ctrl = ExploitationModeController()
        ok, reason = ctrl.check_position_allowed(50, 500, 1)
        assert ok is False
        assert "exposure" in reason.lower()

    def test_check_max_positions(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "micro_capital")
        from core.exploitation_mode import ExploitationModeController
        ctrl = ExploitationModeController()
        ok, reason = ctrl.check_position_allowed(10, 0, 2)
        assert ok is False
        assert "positions" in reason.lower()

    def test_to_dict(self, monkeypatch):
        monkeypatch.delenv("TRADING_MODE", raising=False)
        from core.exploitation_mode import ExploitationModeController
        ctrl = ExploitationModeController()
        d = ctrl.to_dict()
        assert d["mode"] == "paper"
        assert "max_position_size_eur" in d


# ═══════════════════════════════════════════════════════════════
#  2. STATE MACHINE
# ═══════════════════════════════════════════════════════════════


class TestStateMachine:

    def _make_machine(self):
        from core.state_machine import SystemStateMachine
        td = tempfile.mkdtemp(prefix="sm_test_")
        path = os.path.join(td, "state.json")
        return SystemStateMachine(persistence_path=path), td

    def test_initial_state(self):
        sm, td = self._make_machine()
        from core.state_machine import SystemState
        assert sm.state == SystemState.INIT

    def test_init_to_paper(self):
        sm, td = self._make_machine()
        from core.state_machine import SystemState
        ok, msg = sm.transition(SystemState.PAPER, "test")
        assert ok is True
        assert sm.state == SystemState.PAPER

    def test_invalid_transition(self):
        sm, td = self._make_machine()
        from core.state_machine import SystemState
        ok, msg = sm.transition(SystemState.LIVE, "shortcut")
        assert ok is False
        assert "not allowed" in msg.lower()

    def test_emergency_stop(self):
        sm, td = self._make_machine()
        from core.state_machine import SystemState
        sm.transition(SystemState.PAPER, "test")
        sm.emergency_stop("drill")
        assert sm.state == SystemState.STOPPED

    def test_min_duration_blocks(self):
        sm, td = self._make_machine()
        from core.state_machine import SystemState
        sm.transition(SystemState.PAPER, "test")
        # Paper requires 24h before micro_capital
        ok, msg = sm.transition(SystemState.MICRO_CAPITAL, "too_early")
        assert ok is False
        assert "remaining" in msg.lower()

    def test_backward_skips_duration(self):
        sm, td = self._make_machine()
        from core.state_machine import SystemState
        sm.transition(SystemState.PAPER, "test")
        ok, msg = sm.transition(SystemState.INIT, "rollback")
        assert ok is True

    def test_stopped_to_init(self):
        sm, td = self._make_machine()
        from core.state_machine import SystemState
        sm.emergency_stop("test")
        ok, msg = sm.transition(SystemState.INIT, "recovery")
        assert ok is True
        assert sm.state == SystemState.INIT

    def test_persistence(self):
        from core.state_machine import SystemStateMachine, SystemState
        td = tempfile.mkdtemp(prefix="sm_persist_")
        path = os.path.join(td, "state.json")
        sm1 = SystemStateMachine(persistence_path=path)
        sm1.transition(SystemState.PAPER, "test")
        # Reload
        sm2 = SystemStateMachine(persistence_path=path)
        assert sm2.state == SystemState.PAPER

    def test_is_trading_allowed_init(self):
        sm, td = self._make_machine()
        ok, reason = sm.is_trading_allowed()
        assert ok is False
        assert "INIT" in reason

    def test_is_trading_allowed_paper(self):
        sm, td = self._make_machine()
        from core.state_machine import SystemState
        sm.transition(SystemState.PAPER, "test")
        ok, reason = sm.is_trading_allowed()
        assert ok is True

    def test_is_trading_allowed_stopped(self):
        sm, td = self._make_machine()
        sm.emergency_stop("test")
        ok, reason = sm.is_trading_allowed()
        assert ok is False

    def test_condition_callback(self):
        sm, td = self._make_machine()
        from core.state_machine import SystemState, TransitionCondition

        # First transition before registering the blocker
        sm.transition(SystemState.PAPER, "test")

        def block_fn():
            return TransitionCondition(
                name="test_block",
                passed=False,
                message="blocked by test"
            )

        sm.register_condition(block_fn)

        # Hack: override entered_at to bypass min duration
        sm._record.entered_at = time.time() - 200_000

        ok, msg = sm.transition(SystemState.MICRO_CAPITAL, "test")
        assert ok is False
        assert "test_block" in msg

    def test_to_dict(self):
        sm, td = self._make_machine()
        d = sm.to_dict()
        assert "state" in d
        assert "time_in_state_sec" in d
        assert "trading_allowed" in d

    def test_transition_count(self):
        sm, td = self._make_machine()
        from core.state_machine import SystemState
        sm.transition(SystemState.PAPER, "1")
        sm.transition(SystemState.INIT, "2")
        sm.transition(SystemState.PAPER, "3")
        assert sm.record.transition_count == 3


# ═══════════════════════════════════════════════════════════════
#  3. VALIDATION PROTOCOL
# ═══════════════════════════════════════════════════════════════


class TestValidationProtocol:

    def _make_proto(self):
        from core.validation_protocol import ValidationProtocol
        td = tempfile.mkdtemp(prefix="vp_test_")
        path = os.path.join(td, "validation.json")
        return ValidationProtocol(persistence_path=path), td

    def test_start_protocol(self):
        proto, td = self._make_proto()
        proto.start()
        assert proto.is_active is True
        assert proto.consecutive_passed == 0

    def test_record_and_close_clean_day(self):
        proto, td = self._make_proto()
        proto.start()
        proto.record_uptime(86000, 86400)
        day = proto.close_day()
        assert day is not None
        assert day.passed is True
        assert proto.consecutive_passed == 1

    def test_critical_incident_fails_day(self):
        proto, td = self._make_proto()
        proto.start()
        proto.record_incident("critical")
        proto.record_uptime(86000, 86400)
        day = proto.close_day()
        assert day.passed is False
        assert proto.consecutive_passed == 0

    def test_kill_switch_fails_day(self):
        proto, td = self._make_proto()
        proto.start()
        proto.record_kill_switch()
        day = proto.close_day()
        assert day.passed is False

    def test_drift_unresolved_fails(self):
        proto, td = self._make_proto()
        proto.start()
        proto.record_drift(auto_healed=False)
        day = proto.close_day()
        assert day.passed is False

    def test_drift_auto_healed_passes(self):
        proto, td = self._make_proto()
        proto.start()
        proto.record_drift(auto_healed=True)
        proto.record_uptime(86000, 86400)
        day = proto.close_day()
        assert day.passed is True

    def test_7_days_validates(self):
        proto, td = self._make_proto()
        proto.start()
        for i in range(7):
            proto.record_uptime(86000, 86400)
            proto.close_day()
        assert proto.is_validated is True
        assert proto.consecutive_passed == 7

    def test_failure_resets_count(self):
        proto, td = self._make_proto()
        proto.start()
        for _ in range(3):
            proto.record_uptime(86000, 86400)
            proto.close_day()
        assert proto.consecutive_passed == 3
        proto.record_incident("critical")
        proto.close_day()
        assert proto.consecutive_passed == 0

    def test_record_trade(self):
        proto, td = self._make_proto()
        proto.start()
        proto.record_trade(pnl=10.5, slippage_bps=5.0, latency_ms=100)
        proto.record_trade(pnl=-3.0, slippage_bps=10.0, latency_ms=200)
        report = proto.get_daily_report()
        assert report["trades_executed"] == 2
        assert abs(report["pnl_eur"] - 7.5) < 0.01

    def test_reset_protocol(self):
        proto, td = self._make_proto()
        proto.start()
        proto.reset("test_reason")
        assert proto.is_active is False
        assert proto.total_resets >= 1

    def test_to_dict(self):
        proto, td = self._make_proto()
        proto.start()
        d = proto.to_dict()
        assert d["active"] is True
        assert d["required_days"] == 7

    def test_persistence(self):
        from core.validation_protocol import ValidationProtocol
        td = tempfile.mkdtemp(prefix="vp_persist_")
        path = os.path.join(td, "validation.json")
        p1 = ValidationProtocol(persistence_path=path)
        p1.start()
        p1.record_uptime(86000, 86400)
        p1.close_day()

        p2 = ValidationProtocol(persistence_path=path)
        assert p2.consecutive_passed == 1


# ═══════════════════════════════════════════════════════════════
#  4. CAPITAL SCALER
# ═══════════════════════════════════════════════════════════════


class TestCapitalScaler:

    def _make_scaler(self):
        from core.capital_scaler import CapitalScaler
        td = tempfile.mkdtemp(prefix="cs_test_")
        path = os.path.join(td, "scaler.json")
        return CapitalScaler(persistence_path=path), td

    def test_initial_tier(self):
        scaler, td = self._make_scaler()
        assert scaler.tier_index == 0
        assert scaler.current_tier.name == "T0_PAPER"

    def test_cannot_scale_up_immediately(self):
        scaler, td = self._make_scaler()
        # Scale to T1 first (T0 requires 0 days)
        scaler.scale_up("boot")
        assert scaler.tier_index == 1
        # Now T1 requires 7 days → immediate scale-up should be blocked
        ok, blockers = scaler.can_scale_up()
        assert ok is False
        assert any("stability" in b for b in blockers)

    def test_scale_up_after_stability(self):
        scaler, td = self._make_scaler()
        # T0 requires 0 days, so should be able to scale up
        # But need 0 critical incidents
        ok, msg = scaler.scale_up("test")
        assert ok is True
        assert scaler.tier_index == 1
        assert scaler.current_tier.name == "T1_1K"

    def test_scale_up_blocked_by_incidents(self):
        scaler, td = self._make_scaler()
        scaler.scale_up("t0→t1")
        scaler.record_incident()
        ok, blockers = scaler.can_scale_up()
        assert ok is False
        assert any("critical incidents" in b.lower() for b in blockers)

    def test_rollback(self):
        scaler, td = self._make_scaler()
        scaler.scale_up("up")
        ok, msg = scaler.rollback("incident")
        assert ok is True
        assert scaler.tier_index == 0

    def test_rollback_at_floor(self):
        scaler, td = self._make_scaler()
        ok, msg = scaler.rollback("test")
        assert ok is False

    def test_emergency_floor(self):
        scaler, td = self._make_scaler()
        scaler.scale_up("1")
        scaler._state.entered_at = time.time() - 100 * 86400
        scaler.scale_up("2")
        scaler.emergency_floor("panic")
        assert scaler.tier_index == 0

    def test_check_position_size(self):
        scaler, td = self._make_scaler()
        scaler.scale_up("test")  # T1: 1000€, 10% max size = 100€
        ok, reason = scaler.check_position_size(50)
        assert ok is True
        ok, reason = scaler.check_position_size(200)
        assert ok is False

    def test_persistence(self):
        from core.capital_scaler import CapitalScaler
        td = tempfile.mkdtemp(prefix="cs_persist_")
        path = os.path.join(td, "scaler.json")
        s1 = CapitalScaler(persistence_path=path)
        s1.scale_up("test")
        s2 = CapitalScaler(persistence_path=path)
        assert s2.tier_index == 1

    def test_scale_history_recorded(self):
        scaler, td = self._make_scaler()
        scaler.scale_up("up1")
        d = scaler.to_dict()
        assert d["scale_history_count"] == 1

    def test_to_dict(self):
        scaler, td = self._make_scaler()
        d = scaler.to_dict()
        assert "tier_name" in d
        assert "max_capital_eur" in d
        assert "incidents_in_window" in d

    def test_reset_incident_window(self):
        scaler, td = self._make_scaler()
        scaler.record_incident()
        scaler.record_incident()
        scaler.reset_incident_window()
        assert scaler._state.critical_incidents_in_window == 0


# ═══════════════════════════════════════════════════════════════
#  5. INCIDENT MEMORY
# ═══════════════════════════════════════════════════════════════


class TestIncidentMemory:

    def _make_memory(self):
        from core.incident_memory import IncidentMemory
        td = tempfile.mkdtemp(prefix="im_test_")
        path = os.path.join(td, "incidents.json")
        return IncidentMemory(persistence_path=path), td

    def test_record_incident(self):
        mem, td = self._make_memory()
        inc = mem.record(
            incident_type="drift",
            severity="warning",
            source="test",
            message="Test drift",
        )
        assert inc.incident_type == "drift"
        assert mem.total_count == 1

    def test_get_recent(self):
        mem, td = self._make_memory()
        mem.record("drift", "warning", "test", "msg1")
        mem.record("kill_switch", "critical", "test", "msg2")
        recent = mem.get_recent(hours=1.0)
        assert len(recent) == 2

    def test_count_by_type(self):
        mem, td = self._make_memory()
        mem.record("drift", "warning", "test", "1")
        mem.record("drift", "warning", "test", "2")
        mem.record("kill_switch", "critical", "test", "3")
        counts = mem.count_by_type(24.0)
        assert counts["drift"] == 2
        assert counts["kill_switch"] == 1

    def test_count_by_severity(self):
        mem, td = self._make_memory()
        mem.record("a", "warning", "test", "1")
        mem.record("b", "critical", "test", "2")
        counts = mem.count_by_severity(24.0)
        assert counts["warning"] == 1
        assert counts["critical"] == 1

    def test_get_critical_count(self):
        mem, td = self._make_memory()
        mem.record("a", "critical", "test", "1")
        mem.record("b", "warning", "test", "2")
        assert mem.get_critical_count(24.0) == 1

    def test_frequency_pattern(self):
        mem, td = self._make_memory()
        for _ in range(5):
            mem.record("drift", "warning", "test", "repeated")
        patterns = mem.detect_patterns(hours=1.0)
        freq = [p for p in patterns if p.pattern_type == "frequency"]
        assert len(freq) >= 1
        assert freq[0].incident_type == "drift"

    def test_burst_pattern(self):
        mem, td = self._make_memory()
        for _ in range(7):
            mem.record("mixed", "info", "test", "burst")
        patterns = mem.detect_patterns(hours=1.0)
        burst = [p for p in patterns if p.pattern_type == "burst"]
        assert len(burst) >= 1

    def test_escalation_pattern(self):
        mem, td = self._make_memory()
        mem.record("a", "info", "test", "1")
        mem.record("a", "warning", "test", "2")
        mem.record("a", "critical", "test", "3")
        patterns = mem.detect_patterns(hours=1.0)
        esc = [p for p in patterns if p.pattern_type == "escalation"]
        assert len(esc) >= 1

    def test_risk_score_zero_on_empty(self):
        mem, td = self._make_memory()
        assert mem.get_risk_score(24.0) == 0.0

    def test_risk_score_increases(self):
        mem, td = self._make_memory()
        mem.record("a", "critical", "test", "1")
        mem.record("b", "critical", "test", "2")
        score = mem.get_risk_score(24.0)
        assert score > 0.3

    def test_persistence(self):
        from core.incident_memory import IncidentMemory
        td = tempfile.mkdtemp(prefix="im_persist_")
        path = os.path.join(td, "incidents.json")
        m1 = IncidentMemory(persistence_path=path)
        m1.record("drift", "warning", "test", "msg")
        m2 = IncidentMemory(persistence_path=path)
        assert m2.total_count == 1

    def test_avg_resolution_time(self):
        mem, td = self._make_memory()
        mem.record("a", "warning", "test", "1", resolution_sec=10.0)
        mem.record("b", "warning", "test", "2", resolution_sec=20.0)
        avg = mem.avg_resolution_time(24.0)
        assert abs(avg - 15.0) < 0.01

    def test_to_dict(self):
        mem, td = self._make_memory()
        mem.record("a", "info", "test", "x")
        d = mem.to_dict()
        assert "total_incidents" in d
        assert "risk_score" in d
        assert "patterns" in d


# ═══════════════════════════════════════════════════════════════
#  6. RISK ADAPTIVE
# ═══════════════════════════════════════════════════════════════


class TestRiskAdaptive:

    def test_nominal(self):
        from core.risk_adaptive import RiskAdaptive
        ra = RiskAdaptive()
        adj = ra.evaluate(risk_score=0.05, hours_since_last_incident=1.0)
        assert adj.position_size_multiplier == 1.0
        assert adj.reason == "nominal"

    def test_caution(self):
        from core.risk_adaptive import RiskAdaptive
        ra = RiskAdaptive()
        adj = ra.evaluate(risk_score=0.4)
        assert adj.position_size_multiplier < 1.0
        assert adj.position_size_multiplier > 0.3
        assert "caution" in adj.reason.lower()

    def test_danger(self):
        from core.risk_adaptive import RiskAdaptive
        ra = RiskAdaptive()
        adj = ra.evaluate(risk_score=0.7)
        assert adj.position_size_multiplier <= 0.3
        assert adj.kill_switch_sensitivity == 2.0

    def test_critical(self):
        from core.risk_adaptive import RiskAdaptive
        ra = RiskAdaptive()
        adj = ra.evaluate(risk_score=0.9)
        assert adj.position_size_multiplier == 0.1
        assert "critical" in adj.reason.lower()

    def test_stability_bonus(self):
        from core.risk_adaptive import RiskAdaptive
        ra = RiskAdaptive()
        adj = ra.evaluate(
            risk_score=0.05,
            hours_since_last_incident=100.0,
            consecutive_stable_days=10,
        )
        assert adj.position_size_multiplier == 1.0
        assert adj.kill_switch_sensitivity == 0.5  # relaxed

    def test_to_dict(self):
        from core.risk_adaptive import RiskAdaptive
        ra = RiskAdaptive()
        ra.evaluate(risk_score=0.1)
        d = ra.to_dict()
        assert "position_size_multiplier" in d
        assert "reason" in d

    def test_multiplier_bounds(self):
        from core.risk_adaptive import RiskAdaptive
        ra = RiskAdaptive()
        adj = ra.evaluate(risk_score=1.0)
        assert adj.position_size_multiplier >= 0.1
        adj = ra.evaluate(risk_score=0.0)
        assert adj.position_size_multiplier <= 1.0


# ═══════════════════════════════════════════════════════════════
#  7. TEMPORAL MONITOR
# ═══════════════════════════════════════════════════════════════


class TestTemporalMonitor:

    def test_initial_snapshot(self):
        from core.temporal_monitor import TemporalMonitor
        tm = TemporalMonitor()
        snap = tm.get_snapshot()
        assert snap.alert_level == "nominal"

    def test_incident_tracking(self):
        from core.temporal_monitor import TemporalMonitor
        tm = TemporalMonitor()
        tm.record_incident("warning")
        snap = tm.get_snapshot()
        assert snap.time_since_last_incident_sec < 1.0

    def test_recovery_counting(self):
        from core.temporal_monitor import TemporalMonitor
        tm = TemporalMonitor()
        for _ in range(5):
            tm.record_recovery()
        snap = tm.get_snapshot()
        assert snap.recovery_count_1h == 5
        assert snap.alert_level == "elevated"

    def test_safe_mode_tracking(self):
        from core.temporal_monitor import TemporalMonitor
        tm = TemporalMonitor()
        tm.record_safe_mode(True)
        time.sleep(0.01)
        tm.record_safe_mode(False)
        snap = tm.get_snapshot()
        assert snap.time_in_safe_mode_sec > 0
        assert snap.safe_mode_currently_active is False

    def test_backlog_trend(self):
        from core.temporal_monitor import TemporalMonitor
        tm = TemporalMonitor()
        # Flat backlog
        for i in range(10):
            tm.record_backlog(100)
        snap = tm.get_snapshot()
        assert abs(snap.backlog_trend) < 1.0  # flat
        assert snap.backlog_samples == 10

    def test_silence_detection(self):
        from core.temporal_monitor import TemporalMonitor
        tm = TemporalMonitor()
        # Simulate old last alert
        tm._last_alert_ts = time.time() - 7 * 3600  # 7h ago
        snap = tm.get_snapshot()
        assert snap.alert_level == "watch"
        assert any("silence" in r.lower() for r in snap.alert_reasons)

    def test_burst_incidents(self):
        from core.temporal_monitor import TemporalMonitor
        tm = TemporalMonitor()
        for _ in range(6):
            tm.record_incident("warning")
        snap = tm.get_snapshot()
        assert snap.alert_level in ("elevated", "critical")

    def test_to_dict(self):
        from core.temporal_monitor import TemporalMonitor
        tm = TemporalMonitor()
        d = tm.to_dict()
        assert "alert_level" in d
        assert "uptime_streak_sec" in d
        assert "backlog_trend" in d

    def test_restart_resets_uptime(self):
        from core.temporal_monitor import TemporalMonitor
        tm = TemporalMonitor()
        tm.record_restart()
        snap = tm.get_snapshot()
        assert snap.uptime_streak_sec < 1.0

    def test_critical_incident_timestamp(self):
        from core.temporal_monitor import TemporalMonitor
        tm = TemporalMonitor()
        tm.record_incident("critical")
        snap = tm.get_snapshot()
        assert snap.time_since_last_critical_sec < 1.0


# ═══════════════════════════════════════════════════════════════
#  8. INTEGRATION
# ═══════════════════════════════════════════════════════════════


class TestPhase12Integration:

    def test_incident_drives_risk_adaptation(self):
        """Incidents → risk score → adaptive adjustment → reduced sizing."""
        from core.incident_memory import IncidentMemory
        from core.risk_adaptive import RiskAdaptive
        td = tempfile.mkdtemp(prefix="int_test_")
        mem = IncidentMemory(os.path.join(td, "inc.json"))
        ra = RiskAdaptive()

        # Record critical incidents
        for _ in range(3):
            mem.record("drift", "critical", "test", "repeated drift")

        score = mem.get_risk_score(24.0)
        assert score > 0.5

        adj = ra.evaluate(risk_score=score)
        assert adj.position_size_multiplier < 0.5

    def test_validation_to_scale_up(self):
        """7-day validation → eligible for capital scale-up."""
        from core.validation_protocol import ValidationProtocol
        from core.capital_scaler import CapitalScaler
        td = tempfile.mkdtemp(prefix="int_test2_")

        proto = ValidationProtocol(os.path.join(td, "val.json"))
        scaler = CapitalScaler(os.path.join(td, "scaler.json"))

        proto.start()
        for _ in range(7):
            proto.record_uptime(86000, 86400)
            proto.close_day()

        assert proto.is_validated is True

        # Now scale up
        ok, msg = scaler.scale_up("7_day_validated")
        assert ok is True
        assert scaler.tier_index == 1

    def test_incident_triggers_rollback(self):
        """Critical incident → capital rollback."""
        from core.capital_scaler import CapitalScaler
        td = tempfile.mkdtemp(prefix="int_test3_")
        scaler = CapitalScaler(os.path.join(td, "scaler.json"))

        scaler.scale_up("initial")
        assert scaler.tier_index == 1

        scaler.record_incident()
        ok, msg = scaler.rollback("critical_incident")
        assert ok is True
        assert scaler.tier_index == 0

    def test_state_machine_gates_mode(self):
        """State machine state determines exploitation mode."""
        from core.state_machine import SystemStateMachine, SystemState
        from core.exploitation_mode import ExploitationModeController, ExploitationMode
        td = tempfile.mkdtemp(prefix="int_test4_")

        sm = SystemStateMachine(os.path.join(td, "state.json"))
        ctrl = ExploitationModeController()

        # Init → not trading
        ok, reason = sm.is_trading_allowed()
        assert ok is False

        sm.transition(SystemState.PAPER, "boot")
        ok, _ = sm.is_trading_allowed()
        assert ok is True

    def test_all_modules_serializable(self):
        """Every module's to_dict() produces JSON-serializable output."""
        from core.exploitation_mode import ExploitationModeController
        from core.state_machine import SystemStateMachine
        from core.validation_protocol import ValidationProtocol
        from core.capital_scaler import CapitalScaler
        from core.incident_memory import IncidentMemory
        from core.risk_adaptive import RiskAdaptive
        from core.temporal_monitor import TemporalMonitor

        td = tempfile.mkdtemp(prefix="int_serial_")

        objects = [
            ExploitationModeController(),
            SystemStateMachine(os.path.join(td, "sm.json")),
            ValidationProtocol(os.path.join(td, "vp.json")),
            CapitalScaler(os.path.join(td, "cs.json")),
            IncidentMemory(os.path.join(td, "im.json")),
            RiskAdaptive(),
            TemporalMonitor(),
        ]

        for obj in objects:
            d = obj.to_dict()
            serialized = json.dumps(d)
            assert len(serialized) > 0


# ═══════════════════════════════════════════════════════════════
#  9. FAIL-SAFE / EDGE CASES
# ═══════════════════════════════════════════════════════════════


class TestFailSafe:

    def test_corrupt_state_file(self):
        """Corrupt state file → graceful reset to INIT."""
        from core.state_machine import SystemStateMachine, SystemState
        td = tempfile.mkdtemp(prefix="failsafe_")
        path = os.path.join(td, "state.json")
        with open(path, "w") as f:
            f.write("NOT VALID JSON {{")
        sm = SystemStateMachine(persistence_path=path)
        assert sm.state == SystemState.INIT

    def test_corrupt_incident_file(self):
        """Corrupt incident file → empty journal."""
        from core.incident_memory import IncidentMemory
        td = tempfile.mkdtemp(prefix="failsafe2_")
        path = os.path.join(td, "inc.json")
        with open(path, "w") as f:
            f.write("CORRUPT")
        mem = IncidentMemory(persistence_path=path)
        assert mem.total_count == 0

    def test_state_machine_always_allows_stop(self):
        """STOPPED is reachable from any state."""
        from core.state_machine import SystemStateMachine, SystemState
        for start_state in [SystemState.INIT, SystemState.PAPER, SystemState.MICRO_CAPITAL, SystemState.LIVE]:
            td = tempfile.mkdtemp(prefix="stop_test_")
            sm = SystemStateMachine(os.path.join(td, "s.json"))
            # Force state
            sm._record.state = start_state.value
            sm.emergency_stop("test")
            assert sm.state == SystemState.STOPPED

    def test_risk_adaptive_clamps_bounds(self):
        """Risk multipliers never exceed bounds."""
        from core.risk_adaptive import RiskAdaptive
        ra = RiskAdaptive()
        for score in [0.0, 0.3, 0.5, 0.8, 1.0]:
            adj = ra.evaluate(risk_score=score)
            assert 0.1 <= adj.position_size_multiplier <= 1.0
            assert 0.5 <= adj.kill_switch_sensitivity <= 2.0

    def test_capital_scaler_max_tier(self):
        """Cannot scale beyond max tier."""
        from core.capital_scaler import CapitalScaler, TIER_LADDER
        td = tempfile.mkdtemp(prefix="max_tier_")
        scaler = CapitalScaler(os.path.join(td, "s.json"))
        # Force to max
        scaler._state.current_tier_idx = len(TIER_LADDER) - 1
        ok, blockers = scaler.can_scale_up()
        assert ok is False
        assert "maximum" in blockers[0].lower()

    def test_temporal_monitor_no_samples(self):
        """Backlog trend with no samples → 0."""
        from core.temporal_monitor import TemporalMonitor
        tm = TemporalMonitor()
        snap = tm.get_snapshot()
        assert snap.backlog_trend == 0.0
