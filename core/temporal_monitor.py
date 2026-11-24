"""
Temporal Monitor — Duration-oriented metrics for slow drift detection.
═══════════════════════════════════════════════════════════════════════

Phase 12: Detect invisible threats by measuring TIME.

Metrics:
  - time_since_last_incident: hours since any incident
  - time_in_safe_mode: cumulative seconds in safe mode
  - recovery_frequency: events per hour
  - backlog_trend: slope of backlog over time
  - uptime_streak: current consecutive uptime
  - silence_duration: time with 0 alerts (suspicious if too long)

Alerting:
  - Repeated incidents → escalation
  - Prolonged silence → suspicious
  - Cumulative anomalies → strong alert
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional, List, Deque, Tuple

logger = logging.getLogger("smarttrade.temporal_monitor")


@dataclass
class TemporalSnapshot:
    """Snapshot of temporal metrics."""
    timestamp: float

    time_since_last_incident_sec: float
    time_since_last_critical_sec: float
    time_in_safe_mode_sec: float
    safe_mode_currently_active: bool

    recovery_count_1h: int
    recovery_count_24h: int

    backlog_trend: float  # positive = growing, negative = shrinking
    backlog_samples: int

    uptime_streak_sec: float
    silence_duration_sec: float  # time since last alert of any kind

    alert_level: str  # "nominal", "watch", "elevated", "critical"
    alert_reasons: list


class TemporalMonitor:
    """
    Duration-oriented monitoring.

    Call record_*() as events happen, then get_snapshot() for assessment.

    Alert logic:
      - silence > 6h while trading → "watch" (sensor may be dead)
      - 3+ recoveries in 1h → "elevated"
      - incident every < 2h → "elevated"
      - 5+ incidents in 6h → "critical"
      - safe mode > 30min cumulative in 24h → "elevated"
    """

    SILENCE_WATCH_SEC = 6 * 3600       # 6h silence = suspicious
    RECOVERY_ELEVATED = 3              # per hour
    INCIDENT_ELEVATED_GAP = 2 * 3600   # incident every 2h = bad
    SAFE_MODE_ELEVATED_SEC = 30 * 60   # 30min cumulative
    BURST_CRITICAL = 5                 # 5 incidents in 6h

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Timestamps
        self._last_incident_ts: float = 0.0
        self._last_critical_ts: float = 0.0
        self._last_alert_ts: float = time.time()  # Start w/ now to avoid false alarm
        self._start_ts: float = time.time()

        # Safe mode tracking
        self._safe_mode_active: bool = False
        self._safe_mode_entered_ts: float = 0.0
        self._safe_mode_total_sec: float = 0.0

        # Recovery tracking (deque of timestamps)
        self._recovery_times: Deque[float] = deque(maxlen=1000)

        # Incident timestamps
        self._incident_times: Deque[float] = deque(maxlen=1000)

        # Backlog samples (timestamp, size)
        self._backlog_samples: Deque[Tuple[float, int]] = deque(maxlen=500)

        # Uptime
        self._last_restart_ts: float = time.time()

    # ── Recording ──────────────────────────────────────────────

    def record_incident(self, severity: str = "warning") -> None:
        now = time.time()
        with self._lock:
            self._last_incident_ts = now
            self._incident_times.append(now)
            if severity == "critical":
                self._last_critical_ts = now

    def record_alert(self) -> None:
        with self._lock:
            self._last_alert_ts = time.time()

    def record_recovery(self) -> None:
        with self._lock:
            self._recovery_times.append(time.time())

    def record_safe_mode(self, active: bool) -> None:
        now = time.time()
        with self._lock:
            if active and not self._safe_mode_active:
                self._safe_mode_active = True
                self._safe_mode_entered_ts = now
            elif not active and self._safe_mode_active:
                self._safe_mode_active = False
                if self._safe_mode_entered_ts > 0:
                    self._safe_mode_total_sec += now - self._safe_mode_entered_ts

    def record_backlog(self, size: int) -> None:
        with self._lock:
            self._backlog_samples.append((time.time(), size))

    def record_restart(self) -> None:
        with self._lock:
            self._last_restart_ts = time.time()

    # ── Analysis ───────────────────────────────────────────────

    def _compute_backlog_trend(self) -> float:
        """Simple linear regression slope on backlog samples."""
        with self._lock:
            samples = list(self._backlog_samples)

        if len(samples) < 3:
            return 0.0

        # Normalize timestamps
        t0 = samples[0][0]
        xs = [(s[0] - t0) for s in samples]
        ys = [float(s[1]) for s in samples]

        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n

        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        den = sum((x - mean_x) ** 2 for x in xs)

        if den == 0:
            return 0.0
        return num / den  # positive = growing

    def _count_in_window(self, times: Deque[float], hours: float) -> int:
        cutoff = time.time() - (hours * 3600)
        return sum(1 for t in times if t >= cutoff)

    def get_snapshot(self) -> TemporalSnapshot:
        """Compute full temporal snapshot with alert assessment."""
        now = time.time()

        with self._lock:
            since_incident = (now - self._last_incident_ts) if self._last_incident_ts > 0 else now - self._start_ts
            since_critical = (now - self._last_critical_ts) if self._last_critical_ts > 0 else now - self._start_ts
            since_alert = now - self._last_alert_ts

            safe_mode_sec = self._safe_mode_total_sec
            if self._safe_mode_active and self._safe_mode_entered_ts > 0:
                safe_mode_sec += now - self._safe_mode_entered_ts

            recovery_1h = self._count_in_window(self._recovery_times, 1.0)
            recovery_24h = self._count_in_window(self._recovery_times, 24.0)

            uptime = now - self._last_restart_ts

            incident_count_6h = self._count_in_window(self._incident_times, 6.0)

        trend = self._compute_backlog_trend()

        # ── Alert assessment ──
        alert_reasons = []
        alert_level = "nominal"

        if since_alert > self.SILENCE_WATCH_SEC:
            alert_reasons.append(
                f"Silence: {since_alert / 3600:.1f}h with no alerts (sensor dead?)"
            )
            alert_level = "watch"

        if recovery_1h >= self.RECOVERY_ELEVATED:
            alert_reasons.append(
                f"Recovery storm: {recovery_1h} recoveries in 1h"
            )
            alert_level = max(alert_level, "elevated", key=_level_order)

        if self._last_incident_ts > 0 and since_incident < self.INCIDENT_ELEVATED_GAP:
            if incident_count_6h >= 2:
                alert_reasons.append(
                    f"Frequent incidents: {incident_count_6h} in 6h, last {since_incident / 60:.0f}min ago"
                )
                alert_level = max(alert_level, "elevated", key=_level_order)

        if safe_mode_sec > self.SAFE_MODE_ELEVATED_SEC:
            alert_reasons.append(
                f"Safe mode cumulative: {safe_mode_sec / 60:.1f}min > threshold"
            )
            alert_level = max(alert_level, "elevated", key=_level_order)

        if incident_count_6h >= self.BURST_CRITICAL:
            alert_reasons.append(
                f"Incident burst: {incident_count_6h} incidents in 6h"
            )
            alert_level = "critical"

        if trend > 10.0:
            alert_reasons.append(
                f"Backlog growing: slope={trend:.1f}/sec"
            )
            alert_level = max(alert_level, "watch", key=_level_order)

        with self._lock:
            backlog_n = len(self._backlog_samples)

        return TemporalSnapshot(
            timestamp=now,
            time_since_last_incident_sec=since_incident,
            time_since_last_critical_sec=since_critical,
            time_in_safe_mode_sec=safe_mode_sec,
            safe_mode_currently_active=self._safe_mode_active,
            recovery_count_1h=recovery_1h,
            recovery_count_24h=recovery_24h,
            backlog_trend=round(trend, 3),
            backlog_samples=backlog_n,
            uptime_streak_sec=uptime,
            silence_duration_sec=since_alert,
            alert_level=alert_level,
            alert_reasons=alert_reasons,
        )

    def to_dict(self) -> dict:
        snap = self.get_snapshot()
        return asdict(snap)


def _level_order(level: str) -> int:
    """Order function for alert levels."""
    return {"nominal": 0, "watch": 1, "elevated": 2, "critical": 3}.get(level, 0)


# ─── Singleton ────────────────────────────────────────────────

_monitor: Optional[TemporalMonitor] = None
_singleton_lock = threading.Lock()


def get_temporal_monitor() -> TemporalMonitor:
    global _monitor
    if _monitor is None:
        with _singleton_lock:
            if _monitor is None:
                _monitor = TemporalMonitor()
    return _monitor


def reset_temporal_monitor() -> None:
    """Reset singleton (tests only)."""
    global _monitor
    _monitor = None
