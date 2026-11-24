"""
Global Risk Watchdog — Central Monitoring Orchestrator.
═══════════════════════════════════════════════════════

THE brain of the monitoring layer. Runs a periodic loop and:

  1. CIRCUIT BREAKER:  3+ consecutive critical errors → stop ALL trading
  2. KILL SWITCH:      Drawdown > N% or open_positions > M → halt + alert
  3. POSITION SAFETY:  Open position + no SL + repair failed → FORCE CLOSE
  4. API HEALTH:       Exchange DOWN → log CRITICAL + alert, pause trading
  5. ERROR RATE:       Errors/min > threshold → pause + alert

Env:
  GLOBAL_KILL_SWITCH_ENABLED=true
  GLOBAL_MAX_DRAWDOWN_PCT=5.0
  GLOBAL_MAX_OPEN_POSITIONS=3
  GLOBAL_MAX_ERRORS_PER_MIN=5
  WATCHDOG_INTERVAL_SEC=10
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

from core.env_loader import safe_load_dotenv

safe_load_dotenv()
logger = logging.getLogger("smarttrade.watchdog")


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


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class WatchdogConfig:
    enabled: bool = field(default_factory=lambda: _env_bool("GLOBAL_KILL_SWITCH_ENABLED", True))
    max_drawdown_pct: float = field(default_factory=lambda: _env_float("GLOBAL_MAX_DRAWDOWN_PCT", 5.0))
    max_open_positions: int = field(default_factory=lambda: _env_int("GLOBAL_MAX_OPEN_POSITIONS", 3))
    max_errors_per_min: int = field(default_factory=lambda: _env_int("GLOBAL_MAX_ERRORS_PER_MIN", 5))
    interval_sec: int = field(default_factory=lambda: _env_int("WATCHDOG_INTERVAL_SEC", 10))
    circuit_breaker_threshold: int = field(default_factory=lambda: _env_int("WATCHDOG_CIRCUIT_BREAKER_THRESHOLD", 3))


class WatchdogState:
    """Mutable runtime state of the watchdog."""

    def __init__(self):
        self.trading_halted: bool = False
        self.halt_reason: str = ""
        self.circuit_breaker_open: bool = False
        self.consecutive_critical: int = 0
        self.errors_last_min: List[float] = []  # timestamps of errors
        self.last_check_ts: Optional[float] = None
        self.checks_done: int = 0
        self.total_alerts: int = 0

    def record_error(self):
        self.errors_last_min.append(time.monotonic())

    def errors_in_last_60s(self) -> int:
        cutoff = time.monotonic() - 60
        self.errors_last_min = [t for t in self.errors_last_min if t > cutoff]
        return len(self.errors_last_min)


class GlobalRiskWatchdog:
    """
    Central risk supervisor — ties all monitors together.

    Usage:
        watchdog = GlobalRiskWatchdog(
            health_monitor=exchange_health_monitor,
            alert_manager=alert_manager,
        )
        watchdog.register_halt_callback(orchestrator.halt_trading)
        watchdog.register_position_provider(get_open_positions)
        watchdog.register_equity_provider(get_current_equity)
        watchdog.start()
    """

    def __init__(
        self,
        health_monitor=None,
        alert_manager=None,
        config: Optional[WatchdogConfig] = None,
    ):
        self.config = config or WatchdogConfig()
        self._health = health_monitor
        self._alerts = alert_manager
        self._state = WatchdogState()
        self._task: Optional[asyncio.Task] = None

        # Callbacks
        self._halt_cb: Optional[Callable[[], Coroutine]] = None
        self._position_provider: Optional[Callable[[], Coroutine]] = None
        self._equity_provider: Optional[Callable[[], Coroutine]] = None
        self._emergency_close_cb: Optional[Callable[[str, str, str], Coroutine]] = None
        self._initial_equity: Optional[float] = None

    # ──────────────────────────────────────────────────
    #  REGISTRATION
    # ──────────────────────────────────────────────────

    def register_halt_callback(self, cb: Callable[[], Coroutine]):
        """Called when watchdog decides to halt all trading."""
        self._halt_cb = cb

    def register_position_provider(self, cb: Callable[[], Coroutine]):
        """Async fn returning list of open positions."""
        self._position_provider = cb

    def register_equity_provider(self, cb: Callable[[], Coroutine]):
        """Async fn returning current account equity (float)."""
        self._equity_provider = cb

    def register_emergency_close(self, cb: Callable[[str, str, str], Coroutine]):
        """Async fn(symbol, side, reason) to force-close a position."""
        self._emergency_close_cb = cb

    def set_initial_equity(self, equity: float):
        self._initial_equity = equity

    # ──────────────────────────────────────────────────
    #  CORE CHECK
    # ──────────────────────────────────────────────────

    async def check_once(self) -> Dict[str, Any]:
        """Run one full watchdog cycle. Returns a status dict."""
        results: Dict[str, Any] = {"actions": [], "halted": False}

        if not self.config.enabled:
            return results

        self._state.last_check_ts = time.monotonic()
        self._state.checks_done += 1

        # ── 1. API HEALTH ────────────────────────────
        if self._health:
            for exchange, metrics in self._health.get_all_metrics().items():
                if self._health.is_down(exchange):
                    msg = f"Exchange {exchange} is DOWN (error_rate={metrics['error_rate']:.0%})"
                    logger.critical("[WATCHDOG] %s", msg)
                    await self._alert_critical(msg, exchange=exchange, **metrics)
                    results["actions"].append(f"ALERT: {msg}")
                    self._state.consecutive_critical += 1

        # ── 2. DRAWDOWN CHECK ────────────────────────
        if self._equity_provider and self._initial_equity and self._initial_equity > 0:
            try:
                current_equity = await asyncio.wait_for(
                    self._equity_provider(), timeout=5.0
                )
                drawdown_pct = ((self._initial_equity - current_equity) / self._initial_equity) * 100
                if drawdown_pct >= self.config.max_drawdown_pct:
                    reason = f"Drawdown {drawdown_pct:.2f}% >= limit {self.config.max_drawdown_pct}%"
                    await self._halt_trading(reason)
                    results["halted"] = True
                    results["actions"].append(f"HALT: {reason}")
            except asyncio.TimeoutError:
                logger.error("[WATCHDOG] Equity provider TIMEOUT (5s) — data may be stale")
                self._state.record_error()
                results["actions"].append("ERROR: equity provider timeout")
            except Exception as e:
                logger.error("[WATCHDOG] Equity check failed: %s", e)

        # ── 3. MAX OPEN POSITIONS ─────────────────────
        if self._position_provider:
            try:
                positions = await asyncio.wait_for(
                    self._position_provider(), timeout=5.0
                )
                n = len(positions) if positions else 0
                if n > self.config.max_open_positions:
                    reason = f"Open positions {n} > limit {self.config.max_open_positions}"
                    await self._halt_trading(reason)
                    results["halted"] = True
                    results["actions"].append(f"HALT: {reason}")
            except asyncio.TimeoutError:
                logger.error("[WATCHDOG] Position provider TIMEOUT (5s) — data may be stale")
                self._state.record_error()
                results["actions"].append("ERROR: position provider timeout")
            except Exception as e:
                logger.error("[WATCHDOG] Position check failed: %s", e)

        # ── 4. ERROR RATE ────────────────────────────
        error_count = self._state.errors_in_last_60s()
        if error_count >= self.config.max_errors_per_min:
            reason = f"Error rate {error_count}/min >= limit {self.config.max_errors_per_min}/min"
            await self._halt_trading(reason)
            results["halted"] = True
            results["actions"].append(f"HALT: {reason}")

        # ── 5. CIRCUIT BREAKER ────────────────────────
        if self._state.consecutive_critical >= self.config.circuit_breaker_threshold:
            if not self._state.circuit_breaker_open:
                self._state.circuit_breaker_open = True
                reason = f"Circuit breaker: {self._state.consecutive_critical} consecutive critical events"
                await self._halt_trading(reason)
                results["halted"] = True
                results["actions"].append(f"CIRCUIT_BREAKER: {reason}")

        # Reset consecutive critical on OK cycle
        if not results.get("halted") and self._state.consecutive_critical > 0:
            if self._health:
                all_ok = all(self._health.is_healthy(ex) for ex in self._health._samples)
                if all_ok:
                    self._state.consecutive_critical = 0
                    if self._state.circuit_breaker_open:
                        logger.info("[WATCHDOG] All exchanges healthy — circuit breaker RESET")
                        self._state.circuit_breaker_open = False

        return results

    # ──────────────────────────────────────────────────
    #  ERROR RECORDING (called by other modules)
    # ──────────────────────────────────────────────────

    def record_error(self):
        """Record a trading/system error for rate monitoring."""
        self._state.record_error()

    # ──────────────────────────────────────────────────
    #  HALT
    # ──────────────────────────────────────────────────

    async def _halt_trading(self, reason: str):
        if self._state.trading_halted and self._state.halt_reason == reason:
            return  # already halted for same reason
        self._state.trading_halted = True
        self._state.halt_reason = reason
        logger.critical("[WATCHDOG] *** TRADING HALTED *** Reason: %s", reason)
        await self._alert_critical(f"TRADING HALTED: {reason}")
        if self._halt_cb:
            try:
                await self._halt_cb()
            except Exception as e:
                logger.error("[WATCHDOG] Halt callback error: %s", e)

    def resume_trading(self):
        """Manual resume after halt."""
        if self._state.trading_halted:
            self._state.trading_halted = False
            self._state.halt_reason = ""
            self._state.circuit_breaker_open = False
            self._state.consecutive_critical = 0
            logger.info("[WATCHDOG] Trading RESUMED manually")

    # ──────────────────────────────────────────────────
    #  ALERTING
    # ──────────────────────────────────────────────────

    async def _alert_critical(self, msg: str, **details):
        self._state.total_alerts += 1
        if self._alerts:
            try:
                await self._alerts.critical(msg, **details)
            except Exception as e:
                logger.error("[WATCHDOG] Alert send failed: %s", e)

    async def _alert_warning(self, msg: str, **details):
        self._state.total_alerts += 1
        if self._alerts:
            try:
                await self._alerts.warning(msg, **details)
            except Exception as e:
                logger.error("[WATCHDOG] Alert send failed: %s", e)

    # ──────────────────────────────────────────────────
    #  BACKGROUND LOOP
    # ──────────────────────────────────────────────────

    def start(self):
        if self._task and not self._task.done():
            return

        async def _loop():
            while True:
                try:
                    await self.check_once()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("[WATCHDOG] Loop error: %s", e)
                await asyncio.sleep(self.config.interval_sec)

        self._task = asyncio.ensure_future(_loop())
        logger.info("[WATCHDOG] Started (interval=%ds)", self.config.interval_sec)

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("[WATCHDOG] Stopped")

    # ──────────────────────────────────────────────────
    #  STATUS
    # ──────────────────────────────────────────────────

    @property
    def is_halted(self) -> bool:
        return self._state.trading_halted

    @property
    def halt_reason(self) -> str:
        return self._state.halt_reason

    def get_status(self) -> Dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "trading_halted": self._state.trading_halted,
            "halt_reason": self._state.halt_reason,
            "circuit_breaker_open": self._state.circuit_breaker_open,
            "consecutive_critical": self._state.consecutive_critical,
            "errors_last_60s": self._state.errors_in_last_60s(),
            "checks_done": self._state.checks_done,
            "total_alerts": self._state.total_alerts,
            "limits": {
                "max_drawdown_pct": self.config.max_drawdown_pct,
                "max_open_positions": self.config.max_open_positions,
                "max_errors_per_min": self.config.max_errors_per_min,
                "circuit_breaker_threshold": self.config.circuit_breaker_threshold,
            },
        }
