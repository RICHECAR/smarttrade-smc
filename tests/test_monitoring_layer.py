"""
Tests for the Phase 4 Monitoring & Alerting Layer.
═══════════════════════════════════════════════════

Covers:
  - AlertManager: formatting, level filtering, rate limiting
  - ExchangeHealthMonitor: metrics, status transitions, thresholds
  - TradeJournal: JSONL write, fields, thread safety
  - PositionMonitor: mismatch detection (ghost, qty, missing SL)
  - GlobalRiskWatchdog: drawdown halt, error rate halt, circuit breaker, resume
  - Heartbeat: beat record structure, file write
  - StartupSafetyCheck: pass/fail scenarios
"""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ═══════════════════════════════════════════════════════════════
#  FIXTURES
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset all singletons between tests."""
    from notifications.alert_manager import AlertManager
    from core.trade_journal import TradeJournal
    AlertManager.reset()
    TradeJournal.reset()
    yield
    AlertManager.reset()
    TradeJournal.reset()


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


# ═══════════════════════════════════════════════════════════════
#  ALERT MANAGER
# ═══════════════════════════════════════════════════════════════

class TestAlertManager:

    def _make(self):
        from notifications.alert_manager import AlertManager
        am = AlertManager()
        am._tg_enabled = False
        am._dc_enabled = False
        am._min_level = 0  # INFO
        am._rate_limit_sec = 0
        return am

    @pytest.mark.asyncio
    async def test_send_info_logged(self):
        from notifications.alert_manager import AlertLevel
        am = self._make()
        result = await am.send(AlertLevel.INFO, "Test info")
        # Not sent (no channel enabled), but not filtered
        assert result is False  # no channels → not sent

    @pytest.mark.asyncio
    async def test_level_filtering(self):
        from notifications.alert_manager import AlertLevel
        am = self._make()
        am._min_level = AlertLevel.WARNING
        result = await am.send(AlertLevel.INFO, "Should be filtered")
        assert result is False

    @pytest.mark.asyncio
    async def test_rate_limiting(self):
        from notifications.alert_manager import AlertLevel
        am = self._make()
        am._rate_limit_sec = 1000  # very long
        await am.send(AlertLevel.CRITICAL, "First", dedup_key="test_key")
        result = await am.send(AlertLevel.CRITICAL, "Second", dedup_key="test_key")
        assert result is False  # rate limited

    def test_format_message(self):
        from notifications.alert_manager import AlertLevel
        am = self._make()
        msg = am._format_message(AlertLevel.CRITICAL, "SL missing", {"symbol": "BTCUSDT"})
        assert "CRITICAL" in msg
        assert "SL missing" in msg
        assert "BTCUSDT" in msg

    def test_get_status(self):
        am = self._make()
        status = am.get_status()
        assert "send_count" in status
        assert "error_count" in status
        assert status["send_count"] == 0


# ═══════════════════════════════════════════════════════════════
#  EXCHANGE HEALTH MONITOR
# ═══════════════════════════════════════════════════════════════

class TestExchangeHealthMonitor:

    def _make(self):
        from core.exchange_health_monitor import ExchangeHealthMonitor, HealthConfig
        cfg = HealthConfig(
            latency_warn_ms=100,
            latency_critical_ms=500,
            error_rate_warn=0.10,
            error_rate_critical=0.30,
            window_sec=3600,
        )
        return ExchangeHealthMonitor(config=cfg)

    def test_record_success(self):
        mon = self._make()
        mon.record_success("kraken", 50.0)
        metrics = mon.get_metrics("kraken")
        assert metrics["total_requests"] == 1
        assert metrics["error_rate"] == 0.0
        assert metrics["status"] == "OK"

    def test_error_rate_triggers_degraded(self):
        mon = self._make()
        # 2 success + 1 error = 33% error rate > 30% critical
        mon.record_success("kraken", 50.0)
        mon.record_success("kraken", 50.0)
        mon.record_error("kraken", 50.0, "timeout")
        status = mon.get_status("kraken")
        # 33% > 30% critical → DOWN
        assert status == "DOWN"

    def test_high_latency_triggers_degraded(self):
        mon = self._make()
        # 10 requests all with high latency
        for _ in range(10):
            mon.record_success("bitget", 600.0)  # > 500ms critical
        status = mon.get_status("bitget")
        assert status == "DEGRADED"

    def test_consecutive_errors_triggers_down(self):
        mon = self._make()
        for i in range(5):
            mon.record_error("kraken", 50.0, f"error_{i}")
        assert mon.is_down("kraken")

    def test_unknown_exchange_returns_unknown(self):
        mon = self._make()
        assert mon.get_status("nonexistent") == "UNKNOWN"

    def test_p99_latency(self):
        mon = self._make()
        for i in range(100):
            mon.record_success("bitget", float(i))
        metrics = mon.get_metrics("bitget")
        assert metrics["p99_latency_ms"] >= 95

    def test_is_healthy_after_success(self):
        mon = self._make()
        mon.record_success("kraken", 20.0)
        assert mon.is_healthy("kraken")


# ═══════════════════════════════════════════════════════════════
#  TRADE JOURNAL
# ═══════════════════════════════════════════════════════════════

class TestTradeJournal:

    def test_log_trade_creates_file(self, tmp_dir):
        from core.trade_journal import TradeJournal
        path = os.path.join(tmp_dir, "journal.jsonl")
        tj = TradeJournal(path=path)
        tj.log_open("BTCUSDT", "long", 50000.0, 0.01)
        assert Path(path).exists()
        with open(path, "r") as f:
            record = json.loads(f.readline())
        assert record["symbol"] == "BTCUSDT"
        assert record["side"] == "long"
        assert record["action"] == "OPEN"

    def test_log_close_writes_pnl(self, tmp_dir):
        from core.trade_journal import TradeJournal
        path = os.path.join(tmp_dir, "journal.jsonl")
        tj = TradeJournal(path=path)
        tj.log_close("ETHUSDT", "short", entry=3000.0, exit_price=2900.0, pnl=100.0, reason="TP")
        with open(path, "r") as f:
            record = json.loads(f.readline())
        assert record["pnl"] == 100.0
        assert record["reason"] == "TP"

    def test_count_increments(self, tmp_dir):
        from core.trade_journal import TradeJournal
        path = os.path.join(tmp_dir, "journal.jsonl")
        tj = TradeJournal(path=path)
        assert tj.count == 0
        tj.log_event("XRPUSDT", "EMERGENCY_CLOSE", reason="no SL")
        assert tj.count == 1
        tj.log_event("XRPUSDT", "EMERGENCY_CLOSE", reason="no SL")
        assert tj.count == 2

    def test_extra_fields(self, tmp_dir):
        from core.trade_journal import TradeJournal
        path = os.path.join(tmp_dir, "journal.jsonl")
        tj = TradeJournal(path=path)
        tj.log_trade("BTCUSDT", "long", "OPEN", custom_field="hello")
        with open(path, "r") as f:
            record = json.loads(f.readline())
        assert record["extra"]["custom_field"] == "hello"


# ═══════════════════════════════════════════════════════════════
#  POSITION MONITOR
# ═══════════════════════════════════════════════════════════════

class TestPositionMonitor:

    def _make(self):
        from core.position_monitor import PositionMonitor
        return PositionMonitor()

    @pytest.mark.asyncio
    async def test_no_positions_no_mismatches(self):
        mon = self._make()
        result = await mon.check_once([])
        assert result == []

    @pytest.mark.asyncio
    async def test_ghost_local_detected(self):
        from core.position_monitor import PositionSnapshot
        mon = self._make()
        # Fetcher returns empty (no exchange positions)
        mon.register_fetcher("bitget", AsyncMock(return_value=[]))
        local = [PositionSnapshot("BTCUSDT", "long", 0.01, 50000.0, True, True, "bitget")]
        mismatches = await mon.check_once(local)
        assert len(mismatches) == 1
        assert mismatches[0].kind == "ghost_local"

    @pytest.mark.asyncio
    async def test_ghost_exchange_detected(self):
        from core.position_monitor import PositionSnapshot
        mon = self._make()
        exchange_pos = [PositionSnapshot("ETHUSDT", "short", 0.1, 3000.0, True, True, "kraken")]
        mon.register_fetcher("kraken", AsyncMock(return_value=exchange_pos))
        mismatches = await mon.check_once([])  # empty local
        assert len(mismatches) == 1
        assert mismatches[0].kind == "ghost_exchange"

    @pytest.mark.asyncio
    async def test_qty_mismatch_detected(self):
        from core.position_monitor import PositionSnapshot
        mon = self._make()
        local = [PositionSnapshot("BTCUSDT", "long", 0.05, 50000.0, True, True, "bitget")]
        exchange = [PositionSnapshot("BTCUSDT", "long", 0.10, 50000.0, True, True, "bitget")]
        mon.register_fetcher("bitget", AsyncMock(return_value=exchange))
        mismatches = await mon.check_once(local)
        kinds = [m.kind for m in mismatches]
        assert "qty_mismatch" in kinds

    @pytest.mark.asyncio
    async def test_missing_sl_detected(self):
        from core.position_monitor import PositionSnapshot
        mon = self._make()
        local = [PositionSnapshot("BTCUSDT", "long", 0.01, 50000.0, True, True, "bitget")]
        exchange = [PositionSnapshot("BTCUSDT", "long", 0.01, 50000.0, False, True, "bitget")]
        mon.register_fetcher("bitget", AsyncMock(return_value=exchange))
        mismatches = await mon.check_once(local)
        kinds = [m.kind for m in mismatches]
        assert "missing_sl" in kinds

    @pytest.mark.asyncio
    async def test_missing_tp_detected(self):
        from core.position_monitor import PositionSnapshot
        mon = self._make()
        local = [PositionSnapshot("BTCUSDT", "long", 0.01, 50000.0, True, True, "bitget")]
        exchange = [PositionSnapshot("BTCUSDT", "long", 0.01, 50000.0, True, False, "bitget")]
        mon.register_fetcher("bitget", AsyncMock(return_value=exchange))
        mismatches = await mon.check_once(local)
        kinds = [m.kind for m in mismatches]
        assert "missing_tp" in kinds

    @pytest.mark.asyncio
    async def test_fetcher_error_recorded(self):
        mon = self._make()
        mon.register_fetcher("kraken", AsyncMock(side_effect=Exception("network")))
        mismatches = await mon.check_once([])
        assert len(mismatches) == 1
        assert mismatches[0].kind == "fetch_error"

    @pytest.mark.asyncio
    async def test_status_tracks_checks(self):
        mon = self._make()
        await mon.check_once([])
        await mon.check_once([])
        assert mon.get_status()["checks_done"] == 2


# ═══════════════════════════════════════════════════════════════
#  GLOBAL RISK WATCHDOG
# ═══════════════════════════════════════════════════════════════

class TestGlobalRiskWatchdog:

    def _make(self, **overrides):
        from core.global_risk_watchdog import GlobalRiskWatchdog, WatchdogConfig
        cfg = WatchdogConfig(
            enabled=True,
            max_drawdown_pct=5.0,
            max_open_positions=3,
            max_errors_per_min=5,
            interval_sec=1,
            circuit_breaker_threshold=3,
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return GlobalRiskWatchdog(config=cfg)

    @pytest.mark.asyncio
    async def test_no_halt_on_healthy_state(self):
        wd = self._make()
        result = await wd.check_once()
        assert result["halted"] is False
        assert wd.is_halted is False

    @pytest.mark.asyncio
    async def test_drawdown_triggers_halt(self):
        wd = self._make(max_drawdown_pct=5.0)
        wd.set_initial_equity(10000.0)
        wd.register_equity_provider(AsyncMock(return_value=9400.0))  # 6% drawdown
        wd.register_halt_callback(AsyncMock())
        result = await wd.check_once()
        assert result["halted"] is True
        assert wd.is_halted is True
        assert "Drawdown" in wd.halt_reason

    @pytest.mark.asyncio
    async def test_max_positions_triggers_halt(self):
        wd = self._make(max_open_positions=2)
        # 3 positions > limit of 2
        wd.register_position_provider(AsyncMock(return_value=["p1", "p2", "p3"]))
        wd.register_halt_callback(AsyncMock())
        result = await wd.check_once()
        assert result["halted"] is True
        assert "positions" in wd.halt_reason.lower()

    @pytest.mark.asyncio
    async def test_error_rate_triggers_halt(self):
        wd = self._make(max_errors_per_min=3)
        for _ in range(4):
            wd.record_error()
        wd.register_halt_callback(AsyncMock())
        result = await wd.check_once()
        assert result["halted"] is True
        assert "Error rate" in wd.halt_reason

    @pytest.mark.asyncio
    async def test_circuit_breaker_after_consecutive_criticals(self):
        from core.exchange_health_monitor import ExchangeHealthMonitor, HealthConfig
        health = ExchangeHealthMonitor(config=HealthConfig(
            error_rate_critical=0.01, window_sec=3600,
        ))
        # Make kraken DOWN
        for _ in range(10):
            health.record_error("kraken", 100.0, "fail")

        wd = self._make(circuit_breaker_threshold=2)
        wd._health = health
        wd.register_halt_callback(AsyncMock())

        # 1st check → consecutive_critical = 1
        await wd.check_once()
        assert not wd._state.circuit_breaker_open

        # 2nd check → consecutive_critical = 2 → circuit breaker
        await wd.check_once()
        assert wd._state.circuit_breaker_open
        assert wd.is_halted

    @pytest.mark.asyncio
    async def test_resume_trading(self):
        wd = self._make(max_errors_per_min=1)
        wd.record_error()
        wd.record_error()
        wd.register_halt_callback(AsyncMock())
        await wd.check_once()
        assert wd.is_halted
        wd.resume_trading()
        assert not wd.is_halted
        assert wd.halt_reason == ""

    @pytest.mark.asyncio
    async def test_disabled_watchdog_does_nothing(self):
        wd = self._make(enabled=False)
        wd.set_initial_equity(10000.0)
        wd.register_equity_provider(AsyncMock(return_value=1.0))  # massive drawdown
        result = await wd.check_once()
        assert result["halted"] is False

    def test_get_status(self):
        wd = self._make()
        status = wd.get_status()
        assert "trading_halted" in status
        assert "limits" in status
        assert status["limits"]["max_drawdown_pct"] == 5.0

    @pytest.mark.asyncio
    async def test_alert_sent_on_halt(self):
        from notifications.alert_manager import AlertManager
        am = AlertManager()
        am._tg_enabled = False
        am._dc_enabled = False
        am._min_level = 0
        am.critical = AsyncMock()

        wd = self._make(max_errors_per_min=1)
        wd._alerts = am
        wd.record_error()
        wd.record_error()
        wd.register_halt_callback(AsyncMock())
        await wd.check_once()
        am.critical.assert_called()


# ═══════════════════════════════════════════════════════════════
#  HEARTBEAT
# ═══════════════════════════════════════════════════════════════

class TestHeartbeat:

    @pytest.mark.asyncio
    async def test_beat_writes_record(self, tmp_dir):
        from core.heartbeat import Heartbeat
        hb = Heartbeat()
        hb._path = Path(tmp_dir) / "heartbeat.log"
        record = await hb.beat()
        assert "timestamp" in record
        assert "status" in record
        assert record["status"] == "OK"
        assert hb.beat_count == 1
        # Verify file written
        with open(hb._path, "r") as f:
            line = json.loads(f.readline())
        assert line["status"] == "OK"

    @pytest.mark.asyncio
    async def test_beat_reflects_halted_watchdog(self, tmp_dir):
        from core.heartbeat import Heartbeat
        from core.global_risk_watchdog import GlobalRiskWatchdog, WatchdogConfig

        wd = GlobalRiskWatchdog(config=WatchdogConfig(enabled=True, max_errors_per_min=1))
        wd._state.trading_halted = True
        wd._state.halt_reason = "test halt"

        hb = Heartbeat()
        hb._path = Path(tmp_dir) / "heartbeat.log"
        hb.set_watchdog(wd)
        record = await hb.beat()
        assert record["status"] == "HALTED"

    @pytest.mark.asyncio
    async def test_beat_includes_latency(self, tmp_dir):
        from core.heartbeat import Heartbeat
        from core.exchange_health_monitor import ExchangeHealthMonitor, HealthConfig

        health = ExchangeHealthMonitor(config=HealthConfig(window_sec=3600))
        health.record_success("bitget", 42.5)

        hb = Heartbeat()
        hb._path = Path(tmp_dir) / "heartbeat.log"
        hb.set_health_monitor(health)
        record = await hb.beat()
        assert "avg_latency_ms" in record
        assert record["avg_latency_ms"] == 42.5


# ═══════════════════════════════════════════════════════════════
#  STARTUP SAFETY CHECK
# ═══════════════════════════════════════════════════════════════

class TestStartupSafetyCheck:

    def _make(self):
        from core.startup_safety_check import StartupSafetyCheck
        return StartupSafetyCheck()

    @pytest.mark.asyncio
    async def test_all_pass_no_positions(self):
        sc = self._make()
        sc.register_connectivity_check("bitget", AsyncMock())
        sc.register_position_fetcher("bitget", AsyncMock(return_value=[]))
        results = await sc.run_all()
        assert sc.all_passed(results)

    @pytest.mark.asyncio
    async def test_connectivity_failure(self):
        sc = self._make()
        sc.register_connectivity_check("kraken", AsyncMock(side_effect=Exception("timeout")))
        results = await sc.run_all()
        assert not sc.all_passed(results)
        failures = sc.get_failures(results)
        assert any("connectivity" in f.name for f in failures)

    @pytest.mark.asyncio
    async def test_unprotected_position_fails(self):
        sc = self._make()
        sc.register_connectivity_check("bitget", AsyncMock())
        positions = [{"symbol": "BTCUSDT", "has_sl": False, "has_tp": True}]
        sc.register_position_fetcher("bitget", AsyncMock(return_value=positions))
        results = await sc.run_all()
        assert not sc.all_passed(results)

    @pytest.mark.asyncio
    async def test_protected_position_passes(self):
        sc = self._make()
        sc.register_connectivity_check("bitget", AsyncMock())
        positions = [{"symbol": "BTCUSDT", "has_sl": True, "has_tp": True}]
        sc.register_position_fetcher("bitget", AsyncMock(return_value=positions))
        results = await sc.run_all()
        assert sc.all_passed(results)

    @pytest.mark.asyncio
    async def test_kill_switch_active_fails(self):
        sc = self._make()
        sc.register_kill_switch_provider(lambda: True)
        results = await sc.run_all()
        assert not sc.all_passed(results)

    @pytest.mark.asyncio
    async def test_kill_switch_inactive_passes(self):
        sc = self._make()
        sc.register_kill_switch_provider(lambda: False)
        results = await sc.run_all()
        assert sc.all_passed(results)

    @pytest.mark.asyncio
    async def test_disabled_returns_pass(self):
        sc = self._make()
        sc._enabled = False
        results = await sc.run_all()
        assert sc.all_passed(results)
