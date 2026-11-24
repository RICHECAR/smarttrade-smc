"""
Failure Isolation — Service wrappers ensuring crash resilience.
═══════════════════════════════════════════════════════════════

Phase 9 hardening. Provides:
  1. SERVICE WRAPPER: Async retry + crash isolation + monitoring
  2. SUPERVISOR: Monitor background tasks, restart on crash
  3. GRACEFUL SHUTDOWN: Ordered shutdown with timeout
  4. RESTART COUNTER RESET: after sustained uptime, counter resets
  5. TRADING GATE: block all new trades when critical services unhealthy
  6. SYSTEM HEALTH STATE: HEALTHY/DEGRADED/BLOCKED with event publishing

The trading gate is the SINGLE enforcement point for service health → trading.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("smarttrade.isolation")


class ServiceState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    CRASHED = "crashed"
    STOPPING = "stopping"


class SystemHealthState(str, Enum):
    """System-wide health state — controls trading gate."""
    HEALTHY = "healthy"       # All critical services running → trading allowed
    DEGRADED = "degraded"     # Non-critical service down → trading allowed with warning
    BLOCKED = "blocked"       # Critical service down → NO new trades
    RECOVERY = "recovery"     # System recovering after crash → NO new trades


# Services that MUST be healthy for trading to proceed
_CRITICAL_SERVICES = {"risk_engine", "event_bus_health", "reconciliation"}

# Sustained uptime (seconds) after which restart counter resets
_UPTIME_RESET_THRESHOLD = 300.0  # 5 minutes of healthy uptime → reset crash counter


@dataclass
class ServiceStats:
    """Runtime statistics for a wrapped service."""
    name: str
    state: ServiceState = ServiceState.STOPPED
    start_count: int = 0
    crash_count: int = 0
    last_crash_time: float = 0.0
    last_crash_error: str = ""
    uptime_sec: float = 0.0
    _start_time: float = 0.0


class ServiceWrapper:
    """
    Wraps an async service with crash isolation, retry, and monitoring.

    Phase 9 additions:
      - Restart counter resets after sustained uptime
      - Emits events on crash and recovery
    """

    def __init__(
        self,
        name: str,
        run_fn: Callable[[], Awaitable[None]],
        *,
        max_restarts: int = 10,
        restart_delay_sec: float = 5.0,
        max_restart_delay_sec: float = 60.0,
        on_crash: Optional[Callable[[str, Exception], Awaitable[None]]] = None,
    ):
        self.name = name
        self._run_fn = run_fn
        self._max_restarts = max_restarts
        self._base_delay = restart_delay_sec
        self._max_delay = max_restart_delay_sec
        self._on_crash = on_crash
        self._task: Optional[asyncio.Task] = None
        self._stats = ServiceStats(name=name)
        self._stop_event = asyncio.Event()

    @property
    def state(self) -> ServiceState:
        return self._stats.state

    @property
    def stats(self) -> ServiceStats:
        if self._stats.state == ServiceState.RUNNING and self._stats._start_time > 0:
            self._stats.uptime_sec = time.time() - self._stats._start_time
        return self._stats

    async def start(self) -> None:
        if self._stats.state in (ServiceState.RUNNING, ServiceState.STARTING):
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._supervised_loop(), name=f"svc_{self.name}")
        logger.info("[ISOLATION] Service '%s' starting", self.name)

    async def stop(self, timeout: float = 10.0) -> None:
        if self._stats.state == ServiceState.STOPPED:
            return
        self._stats.state = ServiceState.STOPPING
        self._stop_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        self._stats.state = ServiceState.STOPPED
        logger.info("[ISOLATION] Service '%s' stopped", self.name)

    async def _supervised_loop(self) -> None:
        """Restart service on crash with backoff. Reset counter after sustained uptime."""
        restart_count = 0

        while not self._stop_event.is_set() and restart_count <= self._max_restarts:
            self._stats.state = ServiceState.STARTING
            self._stats.start_count += 1
            self._stats._start_time = time.time()

            try:
                self._stats.state = ServiceState.RUNNING
                logger.info("[ISOLATION] Service '%s' running (start #%d)", self.name, self._stats.start_count)
                await self._run_fn()
                break  # Clean exit
            except asyncio.CancelledError:
                break
            except Exception as exc:
                run_duration = time.time() - self._stats._start_time

                # Phase 9: Reset restart counter if service ran for a sustained period
                if run_duration >= _UPTIME_RESET_THRESHOLD and restart_count > 0:
                    logger.info(
                        "[ISOLATION] Service '%s' ran %.0fs before crash — resetting restart counter (was %d)",
                        self.name, run_duration, restart_count,
                    )
                    restart_count = 0

                restart_count += 1
                self._stats.crash_count += 1
                self._stats.last_crash_time = time.time()
                self._stats.last_crash_error = str(exc)
                self._stats.state = ServiceState.CRASHED

                logger.error(
                    "[ISOLATION] Service '%s' CRASHED (#%d/%d after %.0fs): %s",
                    self.name, restart_count, self._max_restarts,
                    run_duration, exc, exc_info=True,
                )

                if self._on_crash:
                    try:
                        await self._on_crash(self.name, exc)
                    except Exception:
                        pass

                try:
                    from core.events.event_bus import get_event_bus
                    from core.events.event_types import SystemEvent, EventType
                    get_event_bus().publish(SystemEvent(
                        EventType.SYSTEM_ERROR,
                        message=f"Service '{self.name}' crashed: {exc}",
                        details={"restart_count": restart_count, "max_restarts": self._max_restarts, "run_duration": run_duration},
                        source="isolation",
                    ))
                except Exception:
                    pass

                if restart_count > self._max_restarts:
                    logger.critical(
                        "[ISOLATION] Service '%s' exceeded max restarts (%d) — giving up",
                        self.name, self._max_restarts,
                    )
                    self._stats.state = ServiceState.CRASHED
                    return

                delay = min(self._base_delay * (2 ** (restart_count - 1)), self._max_delay)
                self._stats.state = ServiceState.DEGRADED
                logger.info("[ISOLATION] Restarting '%s' in %.1fs...", self.name, delay)

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    continue

        if not self._stop_event.is_set():
            self._stats.state = ServiceState.STOPPED


class ServiceSupervisor:
    """
    Manages multiple ServiceWrappers with ordered startup/shutdown.
    Phase 9: includes trading gate and system health state.
    """

    def __init__(self) -> None:
        self._services: Dict[str, ServiceWrapper] = {}
        self._start_order: List[str] = []
        self._system_health = SystemHealthState.HEALTHY
        self._trading_blocked_since: float = 0.0
        self._trading_blocked_reason: str = ""

    def register(
        self,
        name: str,
        run_fn: Callable[[], Awaitable[None]],
        *,
        max_restarts: int = 10,
        restart_delay_sec: float = 5.0,
        on_crash: Optional[Callable[[str, Exception], Awaitable[None]]] = None,
    ) -> ServiceWrapper:
        wrapper = ServiceWrapper(
            name=name,
            run_fn=run_fn,
            max_restarts=max_restarts,
            restart_delay_sec=restart_delay_sec,
            on_crash=on_crash,
        )
        self._services[name] = wrapper
        self._start_order.append(name)
        logger.info("[SUPERVISOR] Registered service '%s' (max_restarts=%d)", name, max_restarts)
        return wrapper

    async def start_all(self) -> None:
        logger.info("[SUPERVISOR] Starting %d services...", len(self._services))
        for name in self._start_order:
            await self._services[name].start()
        logger.info("[SUPERVISOR] All services started")

    async def stop_all(self, timeout: float = 15.0) -> None:
        logger.info("[SUPERVISOR] Stopping %d services...", len(self._services))
        for name in reversed(self._start_order):
            try:
                await self._services[name].stop(timeout=timeout / max(len(self._services), 1))
            except Exception as e:
                logger.error("[SUPERVISOR] Error stopping '%s': %s", name, e)
        logger.info("[SUPERVISOR] All services stopped")

    # ──────────────────────────────────────────────────
    #  TRADING GATE — Phase 9 critical safety feature
    # ──────────────────────────────────────────────────

    def is_trading_allowed(self) -> tuple:
        """
        Check if trading is allowed based on service health.
        Returns (allowed: bool, reason: str).

        Trading is BLOCKED if any critical service is not RUNNING.
        """
        self._update_system_health()

        if self._system_health in (SystemHealthState.BLOCKED, SystemHealthState.RECOVERY):
            return False, f"System state: {self._system_health.value} — {self._trading_blocked_reason}"

        return True, "healthy"

    def _update_system_health(self) -> None:
        """Recompute system health from service states."""
        if not self._services:
            # Phase 10: no services registered = no protection = BLOCKED
            self._system_health = SystemHealthState.BLOCKED
            self._trading_blocked_reason = "No services registered — trading gate has no protection"
            return

        critical_down = []
        non_critical_down = []

        for name, svc in self._services.items():
            # Phase 10: STOPPED is NOT healthy for critical services
            # A critical service must be RUNNING, not just "not crashed"
            is_healthy = svc.state == ServiceState.RUNNING
            if not is_healthy:
                if name in _CRITICAL_SERVICES:
                    critical_down.append(name)
                elif svc.state not in (ServiceState.RUNNING, ServiceState.STOPPED):
                    non_critical_down.append(name)

        if critical_down:
            prev = self._system_health
            self._system_health = SystemHealthState.BLOCKED
            self._trading_blocked_reason = f"Critical services down: {', '.join(critical_down)}"
            if prev != SystemHealthState.BLOCKED:
                self._trading_blocked_since = time.time()
                logger.critical(
                    "[SUPERVISOR] TRADING BLOCKED — %s", self._trading_blocked_reason,
                )
        elif non_critical_down:
            self._system_health = SystemHealthState.DEGRADED
            if self._trading_blocked_reason:
                self._trading_blocked_reason = ""
        else:
            if self._system_health == SystemHealthState.BLOCKED:
                logger.info("[SUPERVISOR] All critical services recovered — trading UNBLOCKED")
            self._system_health = SystemHealthState.HEALTHY
            self._trading_blocked_reason = ""

    @property
    def system_health(self) -> SystemHealthState:
        self._update_system_health()
        return self._system_health

    def get_status(self) -> Dict[str, Any]:
        self._update_system_health()
        return {
            "system_health": self._system_health.value,
            "trading_allowed": self._system_health in (SystemHealthState.HEALTHY, SystemHealthState.DEGRADED),
            "trading_blocked_reason": self._trading_blocked_reason,
            "services": {
                name: {
                    "state": svc.stats.state.value,
                    "start_count": svc.stats.start_count,
                    "crash_count": svc.stats.crash_count,
                    "uptime_sec": round(svc.stats.uptime_sec, 1),
                    "last_crash": svc.stats.last_crash_error if svc.stats.last_crash_error else None,
                }
                for name, svc in self._services.items()
            },
        }

    def is_healthy(self) -> bool:
        return all(
            svc.state == ServiceState.RUNNING for svc in self._services.values()
        )


# ═══════════════════════════════════════════════════════════════
#  SINGLETON
# ═══════════════════════════════════════════════════════════════

_supervisor_instance: Optional[ServiceSupervisor] = None


def get_supervisor() -> ServiceSupervisor:
    global _supervisor_instance
    if _supervisor_instance is None:
        _supervisor_instance = ServiceSupervisor()
    return _supervisor_instance
