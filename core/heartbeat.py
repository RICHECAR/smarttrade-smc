"""
Heartbeat System — Periodic Health Pulse Logger.
════════════════════════════════════════════════

Writes a heartbeat JSON line every HEARTBEAT_INTERVAL_SEC to logs/heartbeat.log
Each line: {timestamp, status, open_positions, latency_ms, errors_last_min, uptime_sec}

Env:
  HEARTBEAT_INTERVAL_SEC=10
  HEARTBEAT_LOG_PATH=logs/heartbeat.log
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional

from core.env_loader import safe_load_dotenv

safe_load_dotenv()
logger = logging.getLogger("smarttrade.heartbeat")


class Heartbeat:
    """Emits periodic heartbeat records to a log file."""

    def __init__(self):
        self._interval = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "10"))
        self._path = Path(os.getenv("HEARTBEAT_LOG_PATH", "logs/heartbeat.log"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._start_time = time.monotonic()
        self._task: Optional[asyncio.Task] = None
        self._beat_count = 0

        # Providers (injected)
        self._watchdog = None
        self._health_monitor = None
        self._position_provider: Optional[Callable[[], Coroutine[Any, Any, int]]] = None

    def set_watchdog(self, watchdog):
        self._watchdog = watchdog

    def set_health_monitor(self, monitor):
        self._health_monitor = monitor

    def set_position_count_provider(self, fn: Callable[[], Coroutine[Any, Any, int]]):
        """Async fn returning number of open positions."""
        self._position_provider = fn

    async def beat(self) -> Dict[str, Any]:
        """Generate and write one heartbeat record."""
        uptime = time.monotonic() - self._start_time

        record: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "OK",
            "uptime_sec": round(uptime, 1),
            "open_positions": 0,
            "errors_last_min": 0,
        }

        # Watchdog state
        if self._watchdog:
            ws = self._watchdog.get_status()
            if ws.get("trading_halted"):
                record["status"] = "HALTED"
            elif ws.get("circuit_breaker_open"):
                record["status"] = "CIRCUIT_BREAKER"
            record["errors_last_min"] = ws.get("errors_last_60s", 0)

        # Position count
        if self._position_provider:
            try:
                record["open_positions"] = await self._position_provider()
            except Exception:
                pass

        # Health metrics (avg latency across exchanges)
        if self._health_monitor:
            all_metrics = self._health_monitor.get_all_metrics()
            if all_metrics:
                avg_lat = sum(m["avg_latency_ms"] for m in all_metrics.values()) / len(all_metrics)
                record["avg_latency_ms"] = round(avg_lat, 1)

        # Write
        line = json.dumps(record, default=str)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.error("[HEARTBEAT] Write failed: %s", e)

        self._beat_count += 1
        return record

    def start(self):
        if self._task and not self._task.done():
            return

        async def _loop():
            while True:
                try:
                    await self.beat()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("[HEARTBEAT] Loop error: %s", e)
                await asyncio.sleep(self._interval)

        self._task = asyncio.ensure_future(_loop())
        logger.info("[HEARTBEAT] Started (interval=%ds, path=%s)", self._interval, self._path)

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("[HEARTBEAT] Stopped")

    @property
    def beat_count(self) -> int:
        return self._beat_count

    @property
    def path(self) -> Path:
        return self._path
