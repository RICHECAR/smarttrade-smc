"""
7-Day Validation Protocol — Continuous system proof.
═══════════════════════════════════════════════════════

Phase 12: Automated tracking of system health over 7+ consecutive days.

Tracks: uptime, incidents, safe mode, kill switch, drift, backlog,
        recovery events, daily PnL, slippage.

Generates daily reports with pass/fail verdict.

Passing all 7 days = eligible for next exploitation tier.
Any critical failure = counter resets.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List

logger = logging.getLogger("smarttrade.validation_protocol")

_PERSISTENCE_PATH = os.path.join("data", "validation_protocol.json")


@dataclass
class DayMetrics:
    """Metrics for one validation day."""
    day: int
    date: str  # YYYY-MM-DD
    start_ts: float = 0.0
    end_ts: float = 0.0

    # Uptime
    uptime_sec: float = 0.0
    total_sec: float = 0.0

    # Incidents
    critical_incidents: int = 0
    warnings: int = 0
    safe_mode_activations: int = 0
    kill_switch_activations: int = 0

    # Drift / consistency
    drift_events: int = 0
    drift_auto_healed: int = 0
    drift_unresolved: int = 0

    # Event bus
    backlog_max: int = 0
    backlog_avg: float = 0.0
    dead_letters: int = 0

    # Recovery
    recovery_events: int = 0

    # Trading (micro/live only)
    trades_executed: int = 0
    pnl_eur: float = 0.0
    slippage_avg_bps: float = 0.0
    latency_avg_ms: float = 0.0

    # Verdict
    passed: bool = False
    blockers: list = field(default_factory=list)


@dataclass
class ValidationState:
    """Persisted validation protocol state."""
    active: bool = False
    started_at: float = 0.0
    current_day: int = 0
    consecutive_passed: int = 0
    total_resets: int = 0
    days: list = field(default_factory=list)  # list of DayMetrics dicts


class ValidationProtocol:
    """
    7-day validation protocol.

    Usage:
        proto = ValidationProtocol()
        proto.start()

        # Each day, record events:
        proto.record_incident("critical")
        proto.record_drift(auto_healed=True)
        proto.record_trade(pnl=12.5, slippage_bps=5.0, latency_ms=120)

        # End of day:
        report = proto.close_day()

        # After 7 passed days:
        assert proto.is_validated
    """

    REQUIRED_DAYS = 7

    # ── Thresholds for a day to PASS ──
    MAX_CRITICAL_INCIDENTS = 0
    MAX_KILL_SWITCH = 0
    MAX_DRIFT_UNRESOLVED = 0
    MAX_BACKLOG = 5000
    MAX_DEAD_LETTERS = 10
    MIN_UPTIME_PCT = 99.0

    def __init__(self, persistence_path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._path = persistence_path or _PERSISTENCE_PATH
        self._state = self._load()
        self._today: Optional[DayMetrics] = None
        if self._state.active and self._state.current_day > 0:
            self._today = DayMetrics(
                day=self._state.current_day,
                date=time.strftime("%Y-%m-%d"),
                start_ts=time.time(),
            )

    def _load(self) -> ValidationState:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    data = json.load(f)
                return ValidationState(**data)
            except Exception as exc:
                logger.error("[VALIDATION] Corrupt state, resetting: %s", exc)
        return ValidationState()

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(asdict(self._state), f, indent=2)
        except Exception as exc:
            logger.error("[VALIDATION] Persist failed: %s", exc)

    # ── Lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        """Start or restart the 7-day protocol."""
        with self._lock:
            self._state = ValidationState(
                active=True,
                started_at=time.time(),
                current_day=1,
                consecutive_passed=0,
                total_resets=self._state.total_resets,
                days=[],
            )
            self._today = DayMetrics(
                day=1,
                date=time.strftime("%Y-%m-%d"),
                start_ts=time.time(),
            )
            self._save()
        logger.info("[VALIDATION] Protocol started (attempt #%d)", self._state.total_resets + 1)

    def reset(self, reason: str) -> None:
        """Reset protocol — a critical failure occurred."""
        with self._lock:
            self._state.active = False
            self._state.consecutive_passed = 0
            self._state.total_resets += 1
            self._save()
        logger.warning("[VALIDATION] Protocol RESET: %s (total resets: %d)", reason, self._state.total_resets)

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._state.active

    @property
    def is_validated(self) -> bool:
        with self._lock:
            return self._state.consecutive_passed >= self.REQUIRED_DAYS

    @property
    def consecutive_passed(self) -> int:
        with self._lock:
            return self._state.consecutive_passed

    @property
    def total_resets(self) -> int:
        with self._lock:
            return self._state.total_resets

    # ── Recording events ───────────────────────────────────────

    def record_incident(self, severity: str) -> None:
        with self._lock:
            if self._today is None:
                return
            if severity == "critical":
                self._today.critical_incidents += 1
            else:
                self._today.warnings += 1

    def record_safe_mode(self) -> None:
        with self._lock:
            if self._today is None:
                return
            self._today.safe_mode_activations += 1

    def record_kill_switch(self) -> None:
        with self._lock:
            if self._today is None:
                return
            self._today.kill_switch_activations += 1

    def record_drift(self, auto_healed: bool = False) -> None:
        with self._lock:
            if self._today is None:
                return
            self._today.drift_events += 1
            if auto_healed:
                self._today.drift_auto_healed += 1
            else:
                self._today.drift_unresolved += 1

    def record_recovery(self) -> None:
        with self._lock:
            if self._today is None:
                return
            self._today.recovery_events += 1

    def record_backlog(self, size: int) -> None:
        with self._lock:
            if self._today is None:
                return
            self._today.backlog_max = max(self._today.backlog_max, size)

    def record_dead_letter(self) -> None:
        with self._lock:
            if self._today is None:
                return
            self._today.dead_letters += 1

    def record_trade(
        self,
        pnl: float = 0.0,
        slippage_bps: float = 0.0,
        latency_ms: float = 0.0,
    ) -> None:
        with self._lock:
            if self._today is None:
                return
            n = self._today.trades_executed
            self._today.pnl_eur += pnl
            # Running average for slippage and latency
            self._today.slippage_avg_bps = (
                (self._today.slippage_avg_bps * n + slippage_bps) / (n + 1)
            )
            self._today.latency_avg_ms = (
                (self._today.latency_avg_ms * n + latency_ms) / (n + 1)
            )
            self._today.trades_executed = n + 1

    def record_uptime(self, uptime_sec: float, total_sec: float) -> None:
        with self._lock:
            if self._today is None:
                return
            self._today.uptime_sec = uptime_sec
            self._today.total_sec = total_sec

    # ── Day evaluation ─────────────────────────────────────────

    def evaluate_day(self, day: DayMetrics) -> DayMetrics:
        """Evaluate a day's metrics against thresholds."""
        blockers = []

        if day.critical_incidents > self.MAX_CRITICAL_INCIDENTS:
            blockers.append(
                f"Critical incidents: {day.critical_incidents} > {self.MAX_CRITICAL_INCIDENTS}"
            )
        if day.kill_switch_activations > self.MAX_KILL_SWITCH:
            blockers.append(
                f"Kill switch activations: {day.kill_switch_activations} > {self.MAX_KILL_SWITCH}"
            )
        if day.drift_unresolved > self.MAX_DRIFT_UNRESOLVED:
            blockers.append(
                f"Unresolved drift: {day.drift_unresolved} > {self.MAX_DRIFT_UNRESOLVED}"
            )
        if day.backlog_max > self.MAX_BACKLOG:
            blockers.append(
                f"Max backlog: {day.backlog_max} > {self.MAX_BACKLOG}"
            )
        if day.dead_letters > self.MAX_DEAD_LETTERS:
            blockers.append(
                f"Dead letters: {day.dead_letters} > {self.MAX_DEAD_LETTERS}"
            )
        if day.total_sec > 0:
            uptime_pct = (day.uptime_sec / day.total_sec) * 100
            if uptime_pct < self.MIN_UPTIME_PCT:
                blockers.append(
                    f"Uptime: {uptime_pct:.1f}% < {self.MIN_UPTIME_PCT}%"
                )

        day.passed = len(blockers) == 0
        day.blockers = blockers
        return day

    def close_day(self) -> Optional[DayMetrics]:
        """
        Close the current day, evaluate, and advance.
        Returns the day report.
        """
        with self._lock:
            if self._today is None:
                return None

            self._today.end_ts = time.time()
            if self._today.total_sec == 0:
                self._today.total_sec = self._today.end_ts - self._today.start_ts
                self._today.uptime_sec = self._today.total_sec

            day = self.evaluate_day(self._today)

            self._state.days.append(asdict(day))

            if day.passed:
                self._state.consecutive_passed += 1
                logger.info(
                    "[VALIDATION] Day %d PASSED (%d/%d)",
                    day.day, self._state.consecutive_passed, self.REQUIRED_DAYS,
                )
            else:
                self._state.consecutive_passed = 0
                self._state.total_resets += 1
                logger.warning(
                    "[VALIDATION] Day %d FAILED: %s (reset #%d)",
                    day.day, "; ".join(day.blockers), self._state.total_resets,
                )

            if self._state.consecutive_passed >= self.REQUIRED_DAYS:
                logger.warning("[VALIDATION] *** 7 DAYS VALIDATED — eligible for next tier ***")
                self._state.active = False

            # Advance to next day
            next_day = day.day + 1
            self._state.current_day = next_day
            self._today = DayMetrics(
                day=next_day,
                date=time.strftime("%Y-%m-%d"),
                start_ts=time.time(),
            )

            self._save()
            return day

    # ── Status ─────────────────────────────────────────────────

    def get_daily_report(self) -> Optional[dict]:
        """Get current day's in-progress metrics."""
        with self._lock:
            if self._today is None:
                return None
            return asdict(self._today)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "active": self._state.active,
                "started_at": self._state.started_at,
                "current_day": self._state.current_day,
                "consecutive_passed": self._state.consecutive_passed,
                "required_days": self.REQUIRED_DAYS,
                "total_resets": self._state.total_resets,
                "is_validated": self._state.consecutive_passed >= self.REQUIRED_DAYS,
                "days_completed": len(self._state.days),
            }


# ─── Singleton ────────────────────────────────────────────────

_protocol: Optional[ValidationProtocol] = None
_singleton_lock = threading.Lock()


def get_validation_protocol(persistence_path: Optional[str] = None) -> ValidationProtocol:
    global _protocol
    if _protocol is None:
        with _singleton_lock:
            if _protocol is None:
                _protocol = ValidationProtocol(persistence_path)
    return _protocol


def reset_validation_protocol() -> None:
    """Reset singleton (tests only)."""
    global _protocol
    _protocol = None
