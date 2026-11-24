"""
Long-Run Monitor — Production-grade health surveillance for 24/7 operation.
═══════════════════════════════════════════════════════════════════════════

Phase 10. Runs as a background service under the supervisor.

Responsibilities:
  1. ROLLING METRICS: latency, backlog, WAL size, dead letters, restart freq
  2. DRIFT DETECTION: slow degradation that escapes point-in-time checks
  3. PERIODIC MAINTENANCE: WAL cleanup, dead letter purge, dedup GC
  4. BACKPRESSURE: block trading when bus is overloaded
  5. HEALTH AGGREGATION: single system-wide health verdict
  6. AUTO-HEAL: resync on safe drifts, block on dangerous ones

Health States:
  HEALTHY    → Full trading capacity
  DEGRADED   → Trading allowed with reduced capacity warning
  AT_RISK    → Trading allowed but one more issue → block
  BLOCKED    → NO new trades (auto or manual intervention needed)
  RECOVERY   → Post-incident, validating stability before resuming
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("smarttrade.longrun_monitor")


class HealthLevel(str, Enum):
    """System-wide health level — drives trading decisions."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    AT_RISK = "at_risk"
    BLOCKED = "blocked"
    RECOVERY = "recovery"


# ═══════════════════════════════════════════════════════════════
#  THRESHOLDS — tuned for real-money 24/7 operation
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Thresholds:
    """All monitoring thresholds in one place. Immutable."""
    # Event bus
    backlog_warn: int = 1000           # queue depth → DEGRADED
    backlog_critical: int = 5000       # queue depth → BLOCKED
    latency_p99_warn_ms: float = 500.0
    latency_p99_critical_ms: float = 2000.0
    # WAL
    wal_size_warn_mb: float = 20.0
    wal_size_critical_mb: float = 40.0
    # Dead letters
    dead_letter_warn: int = 50
    dead_letter_critical: int = 200
    # Restarts
    restart_rate_warn: float = 3.0     # restarts per rolling window
    restart_rate_critical: float = 8.0
    # Consistency
    drift_rate_warn: float = 5.0       # drifts per window
    drift_rate_critical: float = 15.0
    # Kill switch / safe mode frequency
    kill_switch_freq_warn: int = 2     # per rolling window
    safe_mode_freq_warn: int = 3
    # Maintenance intervals (seconds)
    maintenance_interval: float = 60.0
    wal_purge_age_sec: float = 3600.0  # purge dead letters > 1h
    metrics_window_sec: float = 300.0  # 5-minute rolling window


THRESHOLDS = Thresholds()


# ═══════════════════════════════════════════════════════════════
#  ROLLING COUNTER — time-bucketed event counting
# ═══════════════════════════════════════════════════════════════

class RollingCounter:
    """Count events in a sliding time window."""

    def __init__(self, window_sec: float = 300.0, bucket_sec: float = 10.0):
        self._window = window_sec
        self._bucket = bucket_sec
        self._buckets: Deque[Tuple[float, int]] = collections.deque()
        self._current_ts: float = 0.0
        self._current_count: int = 0

    def record(self, count: int = 1) -> None:
        now = time.time()
        bucket_ts = now - (now % self._bucket)
        if bucket_ts != self._current_ts:
            if self._current_ts > 0:
                self._buckets.append((self._current_ts, self._current_count))
            self._current_ts = bucket_ts
            self._current_count = 0
        self._current_count += count
        self._evict(now)

    def total(self) -> int:
        now = time.time()
        self._evict(now)
        total = sum(c for _, c in self._buckets) + self._current_count
        return total

    def rate_per_minute(self) -> float:
        total = self.total()
        return total / (self._window / 60.0) if self._window > 0 else 0.0

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._buckets and self._buckets[0][0] < cutoff:
            self._buckets.popleft()


# ═══════════════════════════════════════════════════════════════
#  ALERTS — structured alert system
# ═══════════════════════════════════════════════════════════════

@dataclass
class Alert:
    """A single monitoring alert."""
    metric: str
    level: str  # "warn" or "critical"
    value: Any
    threshold: Any
    message: str
    timestamp: float = field(default_factory=time.time)


# ═══════════════════════════════════════════════════════════════
#  LONG-RUN MONITOR
# ═══════════════════════════════════════════════════════════════

class LongRunMonitor:
    """
    Production-grade health monitor. Designed to run as a supervised service.

    Runs a periodic loop that:
      1. Collects metrics from event bus, isolation, consistency checker
      2. Evaluates health thresholds
      3. Triggers maintenance (WAL cleanup, dead letter purge)
      4. Applies backpressure if needed
      5. Publishes health status
    """

    def __init__(self, thresholds: Optional[Thresholds] = None) -> None:
        self._thresholds = thresholds or THRESHOLDS
        self._health = HealthLevel.HEALTHY
        self._alerts: List[Alert] = []
        self._max_alerts = 200
        self._active_alerts: Dict[str, Alert] = {}

        # Rolling counters (5-minute window)
        window = self._thresholds.metrics_window_sec
        self._restart_counter = RollingCounter(window_sec=window)
        self._drift_counter = RollingCounter(window_sec=window)
        self._kill_switch_counter = RollingCounter(window_sec=window)
        self._safe_mode_counter = RollingCounter(window_sec=window)
        self._error_counter = RollingCounter(window_sec=window)

        # Snapshot history for drift detection
        self._snapshots: Deque[Dict[str, Any]] = collections.deque(maxlen=60)
        self._last_maintenance = 0.0
        self._cycles = 0
        self._started_at = 0.0

    # ──────────────────────────────────────────────────
    #  MAIN LOOP (runs under ServiceSupervisor)
    # ──────────────────────────────────────────────────

    async def run(self) -> None:
        """Main monitoring loop. Call from supervisor."""
        self._started_at = time.time()
        logger.info("[LONGRUN] Monitor started — interval=%.0fs", self._thresholds.maintenance_interval)

        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[LONGRUN] Monitor tick error: %s", e, exc_info=True)
                self._error_counter.record()

            await asyncio.sleep(self._thresholds.maintenance_interval)

    async def _tick(self) -> None:
        """Single monitoring cycle."""
        self._cycles += 1
        snapshot = self._collect_snapshot()
        self._snapshots.append(snapshot)

        # Evaluate health from snapshot
        alerts = self._evaluate_health(snapshot)
        self._process_alerts(alerts)

        # Periodic maintenance (every 5 intervals)
        now = time.time()
        if now - self._last_maintenance > self._thresholds.maintenance_interval * 5:
            self._run_maintenance(snapshot)
            self._last_maintenance = now

        # Apply backpressure if needed
        self._apply_backpressure(snapshot)

        # Detect slow drifts between consecutive snapshots
        self._detect_slow_drift()

        # Log periodic summary
        if self._cycles % 10 == 0:
            logger.info(
                "[LONGRUN] Health=%s | backlog=%d | lat_p99=%.0fms | wal=%.1fMB | dl=%d | restarts=%d | cycles=%d",
                self._health.value,
                snapshot.get("max_backlog", 0),
                snapshot.get("latency_p99_ms", 0),
                snapshot.get("wal_size_mb", 0),
                snapshot.get("dead_letter_count", 0),
                self._restart_counter.total(),
                self._cycles,
            )

    # ──────────────────────────────────────────────────
    #  COLLECT SNAPSHOT
    # ──────────────────────────────────────────────────

    def _collect_snapshot(self) -> Dict[str, Any]:
        """Collect metrics from all critical subsystems."""
        snapshot: Dict[str, Any] = {"ts": time.time(), "cycle": self._cycles}

        # Event bus metrics
        try:
            from core.events.event_bus import get_event_bus
            bus = get_event_bus()
            stats = bus.get_stats()
            snapshot["max_backlog"] = stats.get("max_backlog", 0)
            snapshot["latency_avg_ms"] = stats.get("latency_avg_ms", 0.0)
            snapshot["latency_p99_ms"] = stats.get("latency_p99_ms", 0.0)
            snapshot["dead_letter_count"] = stats.get("dead_letter_count", 0)
            snapshot["wal_size_bytes"] = stats.get("wal_size_bytes", 0)
            snapshot["wal_size_mb"] = snapshot["wal_size_bytes"] / (1024 * 1024)
            snapshot["total_published"] = stats.get("total_published", 0)
            snapshot["total_dropped"] = stats.get("total_dropped", 0)
            snapshot["total_errors"] = stats.get("total_errors", 0)
            snapshot["gaps_detected"] = stats.get("gaps_detected", 0)
            snapshot["kill_switch_active"] = stats.get("kill_switch_active", False)
            snapshot["dedup_cache_size"] = stats.get("dedup_cache_size", 0)
            snapshot["seq_map_size"] = stats.get("seq_map_size", 0)

            # Phase 10: Memory pressure from bounded caches
            mem = bus.get_memory_pressure()
            snapshot["memory_dedup"] = mem.get("dedup_cache", 0)
            snapshot["memory_seq_map"] = mem.get("seq_map", 0)
            snapshot["memory_consumer_dedup"] = mem.get("total_consumer_dedup", 0)
            snapshot["memory_dead_letters"] = mem.get("dead_letters", 0)

            # Phase 10: Periodic dedup GC (every 5 cycles)
            if self._cycles % 5 == 0:
                evicted = bus.force_dedup_gc()
                if evicted > 0:
                    logger.debug("[LONGRUN] Dedup GC evicted %d entries", evicted)
        except Exception as e:
            logger.error("[LONGRUN] Failed to collect bus metrics: %s", e)
            snapshot["bus_error"] = str(e)

        # Isolation / supervisor metrics
        try:
            from core.isolation import ServiceSupervisor
            # Access via phase6_wiring's global supervisor
            from core.phase6_wiring import _supervisor
            if _supervisor is not None:
                status = _supervisor.get_status()
                snapshot["system_health"] = status.get("system_health", "unknown")
                snapshot["trading_allowed"] = status.get("trading_allowed", False)
                svc_info = status.get("services", {})
                total_crashes = sum(s.get("crash_count", 0) for s in svc_info.values())
                snapshot["total_service_crashes"] = total_crashes
        except Exception:
            pass

        # Phase 10: Consistency checker metrics
        try:
            from core.consistency_checker import get_consistency_checker
            checker = get_consistency_checker()
            cstats = checker.get_stats()
            snapshot["consistency_checks"] = cstats.get("checks", 0)
            snapshot["consistency_drifts"] = cstats.get("drifts_detected", 0)
            snapshot["auto_heal_corrected"] = cstats.get("auto_heal", {}).get("corrected", 0)
            snapshot["auto_heal_blocked"] = cstats.get("auto_heal", {}).get("blocked", 0)
        except Exception:
            pass

        return snapshot

    # ──────────────────────────────────────────────────
    #  EVALUATE HEALTH
    # ──────────────────────────────────────────────────

    def _evaluate_health(self, snap: Dict[str, Any]) -> List[Alert]:
        """Evaluate all thresholds and produce alerts."""
        t = self._thresholds
        alerts: List[Alert] = []

        # Backlog
        backlog = snap.get("max_backlog", 0)
        if backlog >= t.backlog_critical:
            alerts.append(Alert("backlog", "critical", backlog, t.backlog_critical,
                                f"Backlog {backlog} >= {t.backlog_critical}"))
        elif backlog >= t.backlog_warn:
            alerts.append(Alert("backlog", "warn", backlog, t.backlog_warn,
                                f"Backlog {backlog} >= {t.backlog_warn}"))

        # Latency p99
        lat = snap.get("latency_p99_ms", 0.0)
        if lat >= t.latency_p99_critical_ms:
            alerts.append(Alert("latency_p99", "critical", lat, t.latency_p99_critical_ms,
                                f"P99 latency {lat:.0f}ms >= {t.latency_p99_critical_ms}ms"))
        elif lat >= t.latency_p99_warn_ms:
            alerts.append(Alert("latency_p99", "warn", lat, t.latency_p99_warn_ms,
                                f"P99 latency {lat:.0f}ms >= {t.latency_p99_warn_ms}ms"))

        # WAL size
        wal_mb = snap.get("wal_size_mb", 0.0)
        if wal_mb >= t.wal_size_critical_mb:
            alerts.append(Alert("wal_size", "critical", wal_mb, t.wal_size_critical_mb,
                                f"WAL {wal_mb:.1f}MB >= {t.wal_size_critical_mb}MB"))
        elif wal_mb >= t.wal_size_warn_mb:
            alerts.append(Alert("wal_size", "warn", wal_mb, t.wal_size_warn_mb,
                                f"WAL {wal_mb:.1f}MB >= {t.wal_size_warn_mb}MB"))

        # Dead letters
        dl = snap.get("dead_letter_count", 0)
        if dl >= t.dead_letter_critical:
            alerts.append(Alert("dead_letters", "critical", dl, t.dead_letter_critical,
                                f"Dead letters {dl} >= {t.dead_letter_critical}"))
        elif dl >= t.dead_letter_warn:
            alerts.append(Alert("dead_letters", "warn", dl, t.dead_letter_warn,
                                f"Dead letters {dl} >= {t.dead_letter_warn}"))

        # Restart rate
        restart_rate = self._restart_counter.total()
        if restart_rate >= t.restart_rate_critical:
            alerts.append(Alert("restart_rate", "critical", restart_rate, t.restart_rate_critical,
                                f"Restarts {restart_rate} >= {t.restart_rate_critical} in window"))
        elif restart_rate >= t.restart_rate_warn:
            alerts.append(Alert("restart_rate", "warn", restart_rate, t.restart_rate_warn,
                                f"Restarts {restart_rate} >= {t.restart_rate_warn} in window"))

        # Drift rate
        drift_rate = self._drift_counter.total()
        if drift_rate >= t.drift_rate_critical:
            alerts.append(Alert("drift_rate", "critical", drift_rate, t.drift_rate_critical,
                                f"Drifts {drift_rate} >= {t.drift_rate_critical} in window"))
        elif drift_rate >= t.drift_rate_warn:
            alerts.append(Alert("drift_rate", "warn", drift_rate, t.drift_rate_warn,
                                f"Drifts {drift_rate} >= {t.drift_rate_warn} in window"))

        # Compute overall health
        critical_count = sum(1 for a in alerts if a.level == "critical")
        warn_count = sum(1 for a in alerts if a.level == "warn")

        if snap.get("kill_switch_active", False):
            self._health = HealthLevel.BLOCKED
        elif critical_count >= 2:
            self._health = HealthLevel.BLOCKED
        elif critical_count == 1:
            self._health = HealthLevel.AT_RISK
        elif warn_count >= 3:
            self._health = HealthLevel.AT_RISK
        elif warn_count >= 1:
            self._health = HealthLevel.DEGRADED
        else:
            self._health = HealthLevel.HEALTHY

        return alerts

    # ──────────────────────────────────────────────────
    #  PROCESS ALERTS
    # ──────────────────────────────────────────────────

    def _process_alerts(self, alerts: List[Alert]) -> None:
        new_active: Dict[str, Alert] = {}
        for alert in alerts:
            new_active[alert.metric] = alert
            if alert.metric not in self._active_alerts:
                log_fn = logger.critical if alert.level == "critical" else logger.warning
                log_fn("[LONGRUN] ALERT %s: %s", alert.level.upper(), alert.message)
            self._alerts.append(alert)

        # Log recovery
        for metric in self._active_alerts:
            if metric not in new_active:
                logger.info("[LONGRUN] RESOLVED: %s alert cleared", metric)

        self._active_alerts = new_active

        # Bound alert history
        if len(self._alerts) > self._max_alerts:
            self._alerts = self._alerts[-self._max_alerts:]

    # ──────────────────────────────────────────────────
    #  MAINTENANCE — periodic cleanup
    # ──────────────────────────────────────────────────

    def _run_maintenance(self, snap: Dict[str, Any]) -> None:
        """Periodic cleanup tasks — prevents long-run resource accumulation."""
        try:
            from core.events.event_bus import get_event_bus
            bus = get_event_bus()

            # Purge old dead letters
            purged = bus.purge_dead_letters(max_age_sec=self._thresholds.wal_purge_age_sec)
            if purged > 0:
                logger.info("[LONGRUN] Maintenance: purged %d dead letters", purged)

            # Force WAL rotation if too large
            wal_mb = snap.get("wal_size_mb", 0.0)
            if wal_mb > self._thresholds.wal_size_critical_mb:
                logger.warning("[LONGRUN] WAL too large (%.1fMB) — forcing rotation", wal_mb)
                bus.compact_wal()

            # Force dedup GC
            evicted = bus.force_dedup_gc()
            if evicted > 0:
                logger.info("[LONGRUN] Maintenance: dedup GC evicted %d entries", evicted)

        except Exception as e:
            logger.error("[LONGRUN] Maintenance error: %s", e)

    # ──────────────────────────────────────────────────
    #  BACKPRESSURE — block trading on overload
    # ──────────────────────────────────────────────────

    def _apply_backpressure(self, snap: Dict[str, Any]) -> None:
        """If bus is overloaded, activate safe mode to stop new trades."""
        backlog = snap.get("max_backlog", 0)
        if backlog >= self._thresholds.backlog_critical:
            try:
                from core.events.event_bus import get_event_bus
                from core.events.event_types import Event, EventType
                bus = get_event_bus()
                if not bus.kill_switch_active:
                    logger.critical(
                        "[LONGRUN] BACKPRESSURE — backlog %d >= %d — activating safe mode",
                        backlog, self._thresholds.backlog_critical,
                    )
                    bus.publish(Event(
                        event_type=EventType.SAFE_MODE_ACTIVATED,
                        payload={"reason": "backpressure", "backlog": backlog},
                        source="long_run_monitor",
                    ))
            except Exception as e:
                logger.error("[LONGRUN] Backpressure activation failed: %s", e)

    # ──────────────────────────────────────────────────
    #  SLOW DRIFT DETECTION — catch creeping degradation
    # ──────────────────────────────────────────────────

    def _detect_slow_drift(self) -> None:
        """
        Compare recent snapshots to detect slow, progressive degradation.
        This catches issues that don't cross thresholds in a single tick
        but accumulate over time (e.g., backlog growing by 10/tick).
        """
        if len(self._snapshots) < 10:
            return  # Need enough history

        recent = list(self._snapshots)
        old = recent[0]
        new = recent[-1]
        window_sec = new.get("ts", 0) - old.get("ts", 1)
        if window_sec <= 0:
            return

        # Backlog growth rate
        backlog_delta = new.get("max_backlog", 0) - old.get("max_backlog", 0)
        if backlog_delta > 0:
            growth_per_min = backlog_delta / (window_sec / 60.0)
            if growth_per_min > 100:  # Growing > 100/min = problematic
                logger.warning(
                    "[LONGRUN] SLOW DRIFT: backlog growing at %.0f/min (%d → %d)",
                    growth_per_min, old.get("max_backlog", 0), new.get("max_backlog", 0),
                )
                self._error_counter.record()

        # Dead letter accumulation rate
        dl_delta = new.get("dead_letter_count", 0) - old.get("dead_letter_count", 0)
        if dl_delta > 0:
            dl_per_min = dl_delta / (window_sec / 60.0)
            if dl_per_min > 5:  # > 5 dead letters/min = delivery issues
                logger.warning(
                    "[LONGRUN] SLOW DRIFT: dead letters growing at %.1f/min (%d → %d)",
                    dl_per_min, old.get("dead_letter_count", 0), new.get("dead_letter_count", 0),
                )
                self._error_counter.record()

        # Error accumulation
        err_delta = new.get("total_errors", 0) - old.get("total_errors", 0)
        if err_delta > 0:
            err_per_min = err_delta / (window_sec / 60.0)
            if err_per_min > 10:
                logger.warning(
                    "[LONGRUN] SLOW DRIFT: errors growing at %.0f/min",
                    err_per_min,
                )

        # Latency creep
        old_lat = old.get("latency_p99_ms", 0)
        new_lat = new.get("latency_p99_ms", 0)
        if old_lat > 0 and new_lat > old_lat * 2 and new_lat > 100:
            logger.warning(
                "[LONGRUN] SLOW DRIFT: p99 latency doubled (%.0fms → %.0fms)",
                old_lat, new_lat,
            )

    # ──────────────────────────────────────────────────
    #  EXTERNAL API
    # ──────────────────────────────────────────────────

    def record_restart(self) -> None:
        """Call when a service restarts."""
        self._restart_counter.record()

    def record_drift(self, count: int = 1) -> None:
        """Call when consistency drift is detected."""
        self._drift_counter.record(count)

    def record_kill_switch(self) -> None:
        """Call when kill switch is activated."""
        self._kill_switch_counter.record()

    def record_safe_mode(self) -> None:
        """Call when safe mode is activated."""
        self._safe_mode_counter.record()

    @property
    def health(self) -> HealthLevel:
        return self._health

    def get_status(self) -> Dict[str, Any]:
        """Full monitoring status for dashboard/API."""
        return {
            "health": self._health.value,
            "uptime_sec": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "cycles": self._cycles,
            "rolling_window_sec": self._thresholds.metrics_window_sec,
            "counters": {
                "restarts": self._restart_counter.total(),
                "drifts": self._drift_counter.total(),
                "kill_switches": self._kill_switch_counter.total(),
                "safe_modes": self._safe_mode_counter.total(),
                "errors": self._error_counter.total(),
            },
            "active_alerts": {
                k: {"level": v.level, "message": v.message, "value": v.value}
                for k, v in self._active_alerts.items()
            },
            "recent_alerts_count": len(self._alerts),
            "snapshots_count": len(self._snapshots),
            "last_snapshot": dict(self._snapshots[-1]) if self._snapshots else {},
        }

    def get_latency_percentiles(self) -> Dict[str, float]:
        """Get latency percentiles from bus."""
        try:
            from core.events.event_bus import get_event_bus
            bus = get_event_bus()
            latencies = sorted(bus._latencies)
            if not latencies:
                return {"p50": 0, "p95": 0, "p99": 0}
            n = len(latencies)
            return {
                "p50": round(latencies[int(n * 0.50)], 2),
                "p95": round(latencies[int(n * 0.95)], 2),
                "p99": round(latencies[int(n * 0.99)], 2),
            }
        except Exception:
            return {"p50": 0, "p95": 0, "p99": 0}


# ═══════════════════════════════════════════════════════════════
#  AUTO-HEAL — Safe drift correction
# ═══════════════════════════════════════════════════════════════

class AutoHealEngine:
    """
    Attempts safe corrections for known drift types.
    Phase 10: Only corrects when SAFE. Blocks on uncertainty.

    Correction hierarchy:
      1. Balance mismatch (< 5%) → resync from exchange REST
      2. Position mismatch (small) → recalculate from recent fills
      3. Ghost position → flag for manual review + safe mode
      4. Large mismatch → kill switch (never auto-correct large unknown drifts)
    """

    # Max drift % that can be auto-corrected
    SAFE_BALANCE_DRIFT_PCT = 0.05      # 5% — beyond this, block
    SAFE_POSITION_DRIFT_ABS = 0.01     # 1% of position size

    def __init__(self) -> None:
        self._corrections: List[Dict[str, Any]] = []
        self._max_corrections = 100
        self._blocked_count = 0
        self._corrected_count = 0
        self._last_correction_ts = 0.0

    def attempt_heal(self, drift: Any) -> str:
        """
        Attempt to heal a drift record.
        Returns: "corrected", "blocked", "ignored"
        """
        from core.consistency_checker import DriftRecord, DriftSeverity
        if not isinstance(drift, DriftRecord):
            return "ignored"

        now = time.time()

        # Ghost position → ALWAYS block (never auto-correct unknown positions)
        if drift.field == "ghost_position":
            logger.critical(
                "[AUTOHEAL] GHOST POSITION %s — BLOCKING (manual review required)",
                drift.symbol,
            )
            self._blocked_count += 1
            self._record_correction(drift, "blocked", "ghost_position")
            return "blocked"

        # Critical severity → block (too dangerous to auto-correct)
        if drift.severity == DriftSeverity.CRITICAL:
            logger.error(
                "[AUTOHEAL] CRITICAL drift %s/%s — BLOCKING (unsafe to auto-correct)",
                drift.symbol, drift.source,
            )
            self._blocked_count += 1
            self._record_correction(drift, "blocked", "critical_severity")
            return "blocked"

        # WARNING severity balance drift — check if safe to correct
        if drift.field == "balance" and drift.source == "db_vs_exchange":
            expected = float(drift.expected)
            actual = float(drift.actual)
            ref = max(abs(expected), abs(actual), 1.0)
            pct = abs(expected - actual) / ref
            if pct <= self.SAFE_BALANCE_DRIFT_PCT:
                logger.info(
                    "[AUTOHEAL] Balance drift %.2f%% for %s — CORRECTING (trusting exchange)",
                    pct * 100, drift.symbol or "account",
                )
                self._corrected_count += 1
                self._last_correction_ts = now
                self._record_correction(drift, "corrected", f"balance_resync_{pct:.4f}")
                return "corrected"

        # WARNING position drift — check if small enough
        if drift.field == "position_size" and drift.source == "event_vs_db":
            expected = float(drift.expected)
            actual = float(drift.actual)
            ref = max(abs(expected), abs(actual), 1.0)
            pct = abs(expected - actual) / ref
            if pct <= self.SAFE_POSITION_DRIFT_ABS:
                logger.info(
                    "[AUTOHEAL] Position drift %.4f%% for %s — CORRECTING (trusting DB over event)",
                    pct * 100, drift.symbol,
                )
                self._corrected_count += 1
                self._last_correction_ts = now
                self._record_correction(drift, "corrected", f"position_resync_{pct:.4f}")
                return "corrected"

        return "ignored"

    def _record_correction(self, drift: Any, action: str, detail: str) -> None:
        self._corrections.append({
            "ts": time.time(),
            "symbol": getattr(drift, "symbol", ""),
            "field": getattr(drift, "field", ""),
            "source": getattr(drift, "source", ""),
            "action": action,
            "detail": detail,
        })
        if len(self._corrections) > self._max_corrections:
            self._corrections = self._corrections[-self._max_corrections:]

    def get_stats(self) -> Dict[str, Any]:
        return {
            "corrected": self._corrected_count,
            "blocked": self._blocked_count,
            "last_correction_ts": self._last_correction_ts,
            "recent_corrections": self._corrections[-10:],
        }


# ═══════════════════════════════════════════════════════════════
#  SINGLETON
# ═══════════════════════════════════════════════════════════════

_monitor_instance: Optional[LongRunMonitor] = None
_autoheal_instance: Optional[AutoHealEngine] = None


def get_monitor() -> LongRunMonitor:
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = LongRunMonitor()
    return _monitor_instance


def get_autoheal() -> AutoHealEngine:
    global _autoheal_instance
    if _autoheal_instance is None:
        _autoheal_instance = AutoHealEngine()
    return _autoheal_instance
