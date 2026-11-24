"""
Exchange Health Monitor — Latency, error rate, and API status tracking.
═══════════════════════════════════════════════════════════════════════

Measures:
  - Per-request latency (moving average + P99)
  - Error rate over sliding window
  - Timeout tracking
  - Rate-limit detection

Thresholds (via .env):
  HEALTH_LATENCY_WARN_MS=2000
  HEALTH_LATENCY_CRITICAL_MS=5000
  HEALTH_ERROR_RATE_WARN=0.1
  HEALTH_ERROR_RATE_CRITICAL=0.3
  HEALTH_WINDOW_SEC=60
"""

import asyncio
import collections
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Deque, Optional

from core.env_loader import safe_load_dotenv

safe_load_dotenv()
logger = logging.getLogger("smarttrade.health")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


@dataclass
class HealthConfig:
    latency_warn_ms: float = field(default_factory=lambda: _env_float("HEALTH_LATENCY_WARN_MS", 2000))
    latency_critical_ms: float = field(default_factory=lambda: _env_float("HEALTH_LATENCY_CRITICAL_MS", 5000))
    error_rate_warn: float = field(default_factory=lambda: _env_float("HEALTH_ERROR_RATE_WARN", 0.10))
    error_rate_critical: float = field(default_factory=lambda: _env_float("HEALTH_ERROR_RATE_CRITICAL", 0.30))
    window_sec: int = field(default_factory=lambda: _env_int("HEALTH_WINDOW_SEC", 60))


@dataclass
class RequestSample:
    timestamp: float
    latency_ms: float
    success: bool
    error: Optional[str] = None


class ExchangeHealthMonitor:
    """Track API health for one or more exchanges."""

    def __init__(self, config: Optional[HealthConfig] = None):
        self.config = config or HealthConfig()
        # Per-exchange sample ring buffer
        self._samples: Dict[str, Deque[RequestSample]] = {}
        self._status: Dict[str, str] = {}  # exchange -> "OK" | "DEGRADED" | "DOWN"
        self._consecutive_errors: Dict[str, int] = {}

    def _get_buffer(self, exchange: str) -> Deque[RequestSample]:
        if exchange not in self._samples:
            self._samples[exchange] = collections.deque(maxlen=500)
            self._status[exchange] = "OK"
            self._consecutive_errors[exchange] = 0
        return self._samples[exchange]

    # ──────────────────────────────────────────────────
    #  RECORDING
    # ──────────────────────────────────────────────────

    def record_request(
        self,
        exchange: str,
        latency_ms: float,
        success: bool,
        error: Optional[str] = None,
    ):
        """Record a single API request result."""
        buf = self._get_buffer(exchange)
        buf.append(RequestSample(
            timestamp=time.monotonic(),
            latency_ms=latency_ms,
            success=success,
            error=error,
        ))
        if success:
            self._consecutive_errors[exchange] = 0
        else:
            self._consecutive_errors[exchange] = self._consecutive_errors.get(exchange, 0) + 1

        self._update_status(exchange)

    def record_success(self, exchange: str, latency_ms: float):
        self.record_request(exchange, latency_ms, True)

    def record_error(self, exchange: str, latency_ms: float, error: str):
        self.record_request(exchange, latency_ms, False, error)

    # ──────────────────────────────────────────────────
    #  STATUS
    # ──────────────────────────────────────────────────

    def _update_status(self, exchange: str):
        metrics = self.get_metrics(exchange)
        if metrics["error_rate"] >= self.config.error_rate_critical or self._consecutive_errors.get(exchange, 0) >= 5:
            self._status[exchange] = "DOWN"
        elif metrics["error_rate"] >= self.config.error_rate_warn or metrics["avg_latency_ms"] >= self.config.latency_critical_ms:
            self._status[exchange] = "DEGRADED"
        else:
            self._status[exchange] = "OK"

    def get_status(self, exchange: str) -> str:
        return self._status.get(exchange, "UNKNOWN")

    def is_healthy(self, exchange: str) -> bool:
        return self.get_status(exchange) == "OK"

    def is_down(self, exchange: str) -> bool:
        return self.get_status(exchange) == "DOWN"

    # ──────────────────────────────────────────────────
    #  METRICS
    # ──────────────────────────────────────────────────

    def get_metrics(self, exchange: str) -> Dict[str, Any]:
        """Return health metrics for the given exchange over the sliding window."""
        buf = self._get_buffer(exchange)
        cutoff = time.monotonic() - self.config.window_sec
        recent = [s for s in buf if s.timestamp >= cutoff]

        if not recent:
            return {
                "total_requests": 0,
                "error_rate": 0.0,
                "avg_latency_ms": 0.0,
                "p99_latency_ms": 0.0,
                "consecutive_errors": self._consecutive_errors.get(exchange, 0),
                "status": self._status.get(exchange, "UNKNOWN"),
            }

        total = len(recent)
        errors = sum(1 for s in recent if not s.success)
        latencies = sorted(s.latency_ms for s in recent)
        avg_lat = sum(latencies) / total
        p99_idx = max(0, int(total * 0.99) - 1)
        p99_lat = latencies[p99_idx] if latencies else 0

        return {
            "total_requests": total,
            "error_rate": errors / total,
            "avg_latency_ms": round(avg_lat, 1),
            "p99_latency_ms": round(p99_lat, 1),
            "consecutive_errors": self._consecutive_errors.get(exchange, 0),
            "status": self._status.get(exchange, "UNKNOWN"),
        }

    def get_all_metrics(self) -> Dict[str, Dict[str, Any]]:
        return {ex: self.get_metrics(ex) for ex in self._samples}
