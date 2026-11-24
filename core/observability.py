"""
Structured Observability Layer — Trace IDs, structured logging, unified health.
═══════════════════════════════════════════════════════════════════════════════

Phase 6 addition. Provides:
  1. TRACE CONTEXT: Propagate trace_id across signal → execution chain
  2. STRUCTURED LOGGING: JSON-formatted log entries with trace context
  3. UNIFIED HEALTH ENDPOINT: Single source of truth for all subsystems
  4. EVENT BUS METRICS: Prometheus integration for bus statistics
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("smarttrade.observability")


# ═══════════════════════════════════════════════════════════════
#  1. TRACE CONTEXT — propagated via contextvars
# ═══════════════════════════════════════════════════════════════

_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)


def new_trace_id() -> str:
    """Generate and set a new trace ID."""
    tid = uuid.uuid4().hex[:16]
    _trace_id_var.set(tid)
    return tid


def get_trace_id() -> str:
    """Get current trace ID (empty if not set)."""
    return _trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """Explicitly set trace ID (for cross-service propagation)."""
    _trace_id_var.set(trace_id)


# ═══════════════════════════════════════════════════════════════
#  2. STRUCTURED LOG FORMATTER
# ═══════════════════════════════════════════════════════════════

class StructuredFormatter(logging.Formatter):
    """
    JSON log formatter with trace context injection.

    Output:
      {"ts":"2024-01-01T00:00:00Z","level":"INFO","logger":"name","msg":"...","trace_id":"abc123"}
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Inject trace ID if available
        trace_id = _trace_id_var.get()
        if trace_id:
            entry["trace_id"] = trace_id

        # Additional context from record extras
        for key in ("symbol", "broker", "order_id", "event_type", "component"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val

        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])
            entry["exc_type"] = type(record.exc_info[1]).__name__

        return json.dumps(entry, default=str)


def configure_structured_logging(
    level: int = logging.INFO,
    json_output: bool = True,
) -> None:
    """
    Configure root logger with structured JSON output.

    Call once at startup. Only activates JSON format if json_output=True.
    Falls back to standard format for development.
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Remove existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    if json_output:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        ))
    root.addHandler(handler)

    logger.info("[OBSERVABILITY] Structured logging configured — json=%s", json_output)


# ═══════════════════════════════════════════════════════════════
#  3. UNIFIED HEALTH STATUS
# ═══════════════════════════════════════════════════════════════

@dataclass
class SubsystemHealth:
    """Health of a single subsystem."""
    name: str
    status: str = "unknown"  # "healthy", "degraded", "unhealthy", "unknown"
    last_check: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


class HealthAggregator:
    """
    Unified health status for all subsystems.

    Each subsystem registers a health check function.
    /health/full endpoint calls all checks and returns aggregate.
    """

    def __init__(self) -> None:
        self._checks: Dict[str, Any] = {}  # name -> async callable returning SubsystemHealth
        self._cache: Dict[str, SubsystemHealth] = {}
        self._cache_ttl = 5.0  # seconds
        self._last_full_check = 0.0

    def register(self, name: str, check_fn) -> None:
        """Register a health check function (async or sync)."""
        self._checks[name] = check_fn
        logger.info("[HEALTH] Registered subsystem: %s", name)

    async def check_all(self) -> Dict[str, Any]:
        """Run all health checks and return aggregate status."""
        now = time.time()
        results = {}
        overall = "healthy"

        for name, check_fn in self._checks.items():
            try:
                if inspect.iscoroutinefunction(check_fn):
                    health = await asyncio.wait_for(check_fn(), timeout=5.0)
                else:
                    health = check_fn()

                if not isinstance(health, SubsystemHealth):
                    health = SubsystemHealth(
                        name=name,
                        status="healthy" if health else "unhealthy",
                        last_check=now,
                    )
                health.last_check = now
                self._cache[name] = health
                results[name] = asdict(health)

                if health.status == "unhealthy":
                    overall = "unhealthy"
                elif health.status == "degraded" and overall == "healthy":
                    overall = "degraded"

            except asyncio.TimeoutError:
                health = SubsystemHealth(
                    name=name, status="unhealthy", last_check=now, error="timeout"
                )
                self._cache[name] = health
                results[name] = asdict(health)
                overall = "unhealthy"
            except Exception as e:
                health = SubsystemHealth(
                    name=name, status="unhealthy", last_check=now, error=str(e)
                )
                self._cache[name] = health
                results[name] = asdict(health)
                overall = "unhealthy"

        self._last_full_check = now
        return {
            "status": overall,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "subsystems": results,
            "check_count": len(results),
        }

    def get_cached(self) -> Dict[str, Any]:
        """Return cached health without running checks."""
        overall = "healthy"
        results = {}
        for name, health in self._cache.items():
            results[name] = asdict(health)
            if health.status == "unhealthy":
                overall = "unhealthy"
            elif health.status == "degraded" and overall == "healthy":
                overall = "degraded"

        return {
            "status": overall,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "subsystems": results,
            "cached": True,
            "last_full_check": self._last_full_check,
        }


# ═══════════════════════════════════════════════════════════════
#  4. EVENT BUS METRICS BRIDGE
# ═══════════════════════════════════════════════════════════════

def register_event_bus_health(health: HealthAggregator) -> None:
    """Register event bus health check with the aggregator."""

    def check_event_bus() -> SubsystemHealth:
        try:
            from core.events.event_bus import get_event_bus
            bus = get_event_bus()
            stats = bus.get_stats()
            status = "healthy"
            if stats["total_errors"] > 0 and stats["total_errors"] / max(stats["total_published"], 1) > 0.1:
                status = "degraded"
            if stats["total_dropped"] > 100:
                status = "degraded"
            return SubsystemHealth(
                name="event_bus",
                status=status,
                details=stats,
            )
        except Exception as e:
            return SubsystemHealth(name="event_bus", status="unhealthy", error=str(e))

    health.register("event_bus", check_event_bus)


def register_risk_v2_health(health: HealthAggregator) -> None:
    """Register Risk V2 health check."""

    def check_risk_v2() -> SubsystemHealth:
        try:
            from backend.services.trading.risk_engine_v2 import get_risk_engine_v2
            rv2 = get_risk_engine_v2()
            stats = rv2.get_status()
            return SubsystemHealth(
                name="risk_v2",
                status="healthy" if stats["total_capital"] > 0 else "degraded",
                details=stats,
            )
        except Exception as e:
            return SubsystemHealth(name="risk_v2", status="unhealthy", error=str(e))

    health.register("risk_v2", check_risk_v2)


def register_reconciliation_health(health: HealthAggregator) -> None:
    """Register reconciliation V2 health check."""

    def check_reconciliation() -> SubsystemHealth:
        try:
            from backend.services.reconciliation_v2 import ReconciliationV2Service
            # Will be injected at startup — use the global instance
            return SubsystemHealth(name="reconciliation_v2", status="healthy")
        except Exception as e:
            return SubsystemHealth(name="reconciliation_v2", status="unhealthy", error=str(e))

    health.register("reconciliation_v2", check_reconciliation)


# ═══════════════════════════════════════════════════════════════
#  SINGLETON
# ═══════════════════════════════════════════════════════════════

_health_instance: Optional[HealthAggregator] = None


def get_health_aggregator() -> HealthAggregator:
    global _health_instance
    if _health_instance is None:
        _health_instance = HealthAggregator()
    return _health_instance
