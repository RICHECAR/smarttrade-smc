"""
Incident Memory — Structured incident journal with pattern detection.
═══════════════════════════════════════════════════════════════════════

Phase 12: Every incident is recorded, typed, scored, and analyzed.

Objective: detect invisible patterns before they become critical.

Types: drift, kill_switch, safe_mode, recovery, exchange_error,
       latency_spike, backlog_overflow, dead_letter, reconciliation,
       position_orphan, slippage_anomaly, config_error.
"""

import json
import logging
import os
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List

logger = logging.getLogger("smarttrade.incident_memory")

_PERSISTENCE_PATH = os.path.join("data", "incident_memory.json")


class IncidentSeverity:
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Incident:
    """A single recorded incident."""
    timestamp: float
    incident_type: str
    severity: str  # info, warning, critical
    source: str  # module that reported
    message: str
    resolution_sec: float = 0.0  # time to resolve (0 = unresolved)
    auto_resolved: bool = False
    details: dict = field(default_factory=dict)


@dataclass
class IncidentPattern:
    """Detected pattern in incidents."""
    pattern_type: str  # "frequency", "escalation", "recurring", "burst"
    incident_type: str
    count: int
    window_hours: float
    severity: str
    message: str


class IncidentMemory:
    """
    Persistent incident journal with pattern analysis.

    Every incident is:
      1. Recorded with full metadata
      2. Persisted to disk
      3. Analyzed for patterns
      4. Available for risk adaptation

    Pattern detection:
      - Frequency: same type > N times in window
      - Escalation: severity increasing over time
      - Burst: many incidents in short period
      - Recurring: same type at regular intervals
    """

    MAX_INCIDENTS = 10_000  # Keep last N
    PATTERN_WINDOW_HOURS = 24.0
    FREQUENCY_THRESHOLD = 3  # Same type 3+ times in window = pattern

    def __init__(self, persistence_path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._path = persistence_path or _PERSISTENCE_PATH
        self._incidents: List[dict] = self._load()

    def _load(self) -> List[dict]:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data[-self.MAX_INCIDENTS:]
            except Exception as exc:
                logger.error("[INCIDENTS] Corrupt journal, starting fresh: %s", exc)
        return []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._incidents[-self.MAX_INCIDENTS:], f, indent=2)
        except Exception as exc:
            logger.error("[INCIDENTS] Persist failed: %s", exc)

    # ── Recording ──────────────────────────────────────────────

    def record(
        self,
        incident_type: str,
        severity: str,
        source: str,
        message: str,
        resolution_sec: float = 0.0,
        auto_resolved: bool = False,
        details: Optional[dict] = None,
    ) -> Incident:
        """Record an incident and persist."""
        incident = Incident(
            timestamp=time.time(),
            incident_type=incident_type,
            severity=severity,
            source=source,
            message=message,
            resolution_sec=resolution_sec,
            auto_resolved=auto_resolved,
            details=details or {},
        )

        with self._lock:
            self._incidents.append(asdict(incident))
            if len(self._incidents) > self.MAX_INCIDENTS:
                self._incidents = self._incidents[-self.MAX_INCIDENTS:]
            self._save()

        logger.info(
            "[INCIDENTS] Recorded: type=%s severity=%s source=%s",
            incident_type, severity, source,
        )
        return incident

    # ── Queries ────────────────────────────────────────────────

    def get_recent(self, hours: float = 24.0, severity: Optional[str] = None) -> List[dict]:
        """Get incidents from the last N hours."""
        cutoff = time.time() - (hours * 3600)
        with self._lock:
            results = [
                i for i in self._incidents
                if i["timestamp"] >= cutoff
            ]
        if severity:
            results = [i for i in results if i["severity"] == severity]
        return results

    def count_by_type(self, hours: float = 24.0) -> Dict[str, int]:
        """Count incidents by type in the last N hours."""
        recent = self.get_recent(hours)
        counter: Dict[str, int] = {}
        for i in recent:
            t = i["incident_type"]
            counter[t] = counter.get(t, 0) + 1
        return counter

    def count_by_severity(self, hours: float = 24.0) -> Dict[str, int]:
        """Count incidents by severity in the last N hours."""
        recent = self.get_recent(hours)
        counter: Dict[str, int] = {}
        for i in recent:
            s = i["severity"]
            counter[s] = counter.get(s, 0) + 1
        return counter

    @property
    def total_count(self) -> int:
        with self._lock:
            return len(self._incidents)

    def get_critical_count(self, hours: float = 24.0) -> int:
        return len(self.get_recent(hours, severity="critical"))

    def avg_resolution_time(self, hours: float = 24.0) -> float:
        """Average resolution time in seconds for resolved incidents."""
        recent = self.get_recent(hours)
        resolved = [i for i in recent if i["resolution_sec"] > 0]
        if not resolved:
            return 0.0
        return sum(i["resolution_sec"] for i in resolved) / len(resolved)

    # ── Pattern detection ──────────────────────────────────────

    def detect_patterns(self, hours: float = 0) -> List[IncidentPattern]:
        """
        Detect incident patterns.

        Returns list of identified patterns:
          - frequency: same type appears too often
          - burst: many incidents in short window
          - escalation: severity increasing
        """
        if hours <= 0:
            hours = self.PATTERN_WINDOW_HOURS

        recent = self.get_recent(hours)
        if not recent:
            return []

        patterns: List[IncidentPattern] = []

        # ── Frequency patterns ──
        type_counts = self.count_by_type(hours)
        for itype, count in type_counts.items():
            if count >= self.FREQUENCY_THRESHOLD:
                severities = [
                    i["severity"] for i in recent
                    if i["incident_type"] == itype
                ]
                worst = "critical" if "critical" in severities else (
                    "warning" if "warning" in severities else "info"
                )
                patterns.append(IncidentPattern(
                    pattern_type="frequency",
                    incident_type=itype,
                    count=count,
                    window_hours=hours,
                    severity=worst,
                    message=f"{itype} occurred {count}x in {hours}h",
                ))

        # ── Burst pattern: >5 incidents in 1 hour ──
        one_hour_ago = time.time() - 3600
        burst_count = sum(1 for i in recent if i["timestamp"] >= one_hour_ago)
        if burst_count > 5:
            patterns.append(IncidentPattern(
                pattern_type="burst",
                incident_type="mixed",
                count=burst_count,
                window_hours=1.0,
                severity="warning",
                message=f"{burst_count} incidents in last hour (burst)",
            ))

        # ── Escalation: severity increasing over time ──
        if len(recent) >= 3:
            sev_map = {"info": 0, "warning": 1, "critical": 2}
            last_three = recent[-3:]
            scores = [sev_map.get(i["severity"], 0) for i in last_three]
            if scores == sorted(scores) and scores[-1] > scores[0]:
                patterns.append(IncidentPattern(
                    pattern_type="escalation",
                    incident_type=last_three[-1]["incident_type"],
                    count=3,
                    window_hours=hours,
                    severity="critical",
                    message="Severity escalation detected (info→warning→critical)",
                ))

        return patterns

    def get_risk_score(self, hours: float = 24.0) -> float:
        """
        Compute a risk score from 0.0 (safe) to 1.0 (dangerous).

        Based on: incident count, severity distribution, patterns.
        """
        sev_counts = self.count_by_severity(hours)
        critical = sev_counts.get("critical", 0)
        warning = sev_counts.get("warning", 0)
        info = sev_counts.get("info", 0)

        patterns = self.detect_patterns(hours)
        pattern_penalty = len(patterns) * 0.1

        # Weighted score
        raw = (critical * 0.3) + (warning * 0.05) + (info * 0.01) + pattern_penalty
        return min(1.0, raw)

    # ── Status ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "total_incidents": self.total_count,
            "last_24h": {
                "by_type": self.count_by_type(24.0),
                "by_severity": self.count_by_severity(24.0),
                "critical_count": self.get_critical_count(24.0),
                "avg_resolution_sec": self.avg_resolution_time(24.0),
            },
            "patterns": [asdict(p) for p in self.detect_patterns()],
            "risk_score": self.get_risk_score(),
        }


# ─── Singleton ────────────────────────────────────────────────

_memory: Optional[IncidentMemory] = None
_singleton_lock = threading.Lock()


def get_incident_memory(persistence_path: Optional[str] = None) -> IncidentMemory:
    global _memory
    if _memory is None:
        with _singleton_lock:
            if _memory is None:
                _memory = IncidentMemory(persistence_path)
    return _memory


def reset_incident_memory() -> None:
    """Reset singleton (tests only)."""
    global _memory
    _memory = None
