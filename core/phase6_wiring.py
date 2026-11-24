"""
Phase 7 Wiring — Institutional Runtime (V2-only, no flags, no V1 fallback)
══════════════════════════════════════════════════════════════════

Central module responsible for:
  1. Initializing ALL institutional services (no flags — always ON)
  2. Wiring event bus subscriptions (Risk V2, Reconciliation, Alerts)
  3. Activating isolation supervisor for crash resilience
  4. Graceful shutdown in reverse order

ALL modules are ALWAYS active. No feature flags. V2 = sole path.

Call order at startup:
  1. init_observability()
  2. start_event_bus()
  3. init_alert_manager()
  4. init_risk_engine_v2(capital)
  5. wire_risk_v2_subscriptions()
  6. start_reconciliation_v2(bitget_exec, kraken_exec)
  7. wire_reconciliation_subscriptions()
  8. start_isolation()

Call at shutdown:
  shutdown_phase6()
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("smarttrade.phase6_wiring")


# ═══════════════════════════════════════════════════════════════
#  FLAGS — Legacy compat shim (all True, always)
# ═══════════════════════════════════════════════════════════════

class Phase6Flags:
    """All modules always active. No env-var gating."""

    event_bus: bool = True
    risk_v2: bool = True
    reconciliation_v2: bool = True
    alert_manager: bool = True
    isolation: bool = True
    observability: bool = True

    def summary(self) -> Dict[str, bool]:
        return {
            "event_bus": True,
            "risk_v2": True,
            "reconciliation_v2": True,
            "alert_manager": True,
            "isolation": True,
            "observability": True,
        }


# Global flags instance (compat — always all-True)
FLAGS = Phase6Flags()


def init_phase6_flags() -> Phase6Flags:
    """Legacy compat — flags are no longer read from env."""
    global FLAGS
    FLAGS = Phase6Flags()
    logger.info("[PHASE7] All institutional modules ACTIVE (no flags, V2-only)")
    return FLAGS


# ═══════════════════════════════════════════════════════════════
#  OBSERVABILITY
# ═══════════════════════════════════════════════════════════════

_health_aggregator = None


def init_observability() -> None:
    """Configure structured logging + health aggregator."""
    global _health_aggregator
    try:
        from core.observability import (
            get_health_aggregator,
            register_event_bus_health,
            register_risk_v2_health,
            register_reconciliation_health,
        )
        _health_aggregator = get_health_aggregator()
        register_event_bus_health(_health_aggregator)
        register_risk_v2_health(_health_aggregator)
        register_reconciliation_health(_health_aggregator)
        logger.info("[PHASE7] Observability initialized + all health checks registered")
    except Exception as e:
        logger.error("[PHASE7] Observability init failed (non-fatal): %s", e)


def get_phase6_health_aggregator():
    return _health_aggregator


# ═══════════════════════════════════════════════════════════════
#  EVENT BUS
# ═══════════════════════════════════════════════════════════════

_event_bus = None


async def start_event_bus() -> None:
    """Start the event bus (always active)."""
    global _event_bus
    try:
        from core.events.event_bus import get_event_bus
        _event_bus = get_event_bus()
        await _event_bus.start()
        logger.info("[PHASE7] Event bus started — %d subscribers", len(_event_bus._subscriptions))
    except Exception as e:
        logger.error("[PHASE7] Event bus start FAILED: %s", e)
        _event_bus = None


def publish_event(event) -> int:
    """
    Publish an event to the bus if available. Returns 0 if bus is down.
    NEVER raises — safe to call unconditionally.
    """
    if _event_bus is None:
        return 0
    try:
        return _event_bus.publish(event)
    except Exception as e:
        logger.warning("[PHASE6] Event publish failed: %s", e)
        return 0


def get_bus():
    """Get event bus instance (or None if disabled)."""
    return _event_bus


# ═══════════════════════════════════════════════════════════════
#  ALERT MANAGER
# ═══════════════════════════════════════════════════════════════

_alert_manager = None


def init_alert_manager() -> None:
    """Initialize alert manager and connect to event bus (always active)."""
    global _alert_manager
    try:
        from notifications.alert_manager import AlertManager
        _alert_manager = AlertManager.get()

        if _event_bus is not None:
            _alert_manager.connect_event_bus()
            logger.info("[PHASE7] Alert manager connected to event bus")
        else:
            logger.info("[PHASE7] Alert manager initialized (event bus not yet available)")
    except Exception as e:
        logger.error("[PHASE7] Alert manager init failed (non-fatal): %s", e)


async def send_alert(level: str, title: str, **details) -> bool:
    """Send alert if manager available. Never raises."""
    if _alert_manager is None:
        return False
    try:
        from notifications.alert_manager import AlertLevel
        _levels = {"info": AlertLevel.INFO, "warning": AlertLevel.WARNING, "critical": AlertLevel.CRITICAL}
        return await _alert_manager.send(_levels.get(level, AlertLevel.INFO), title, details or None)
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
#  RISK ENGINE V2
# ═══════════════════════════════════════════════════════════════

_risk_v2 = None


def init_risk_engine_v2(initial_capital: float = 0.0) -> None:
    """Initialize Risk V2 engine (always active — sole risk authority)."""
    global _risk_v2
    try:
        from backend.services.trading.risk_engine_v2 import get_risk_engine_v2
        _risk_v2 = get_risk_engine_v2()
        if initial_capital > 0:
            _risk_v2.update_capital(initial_capital)
        logger.info(
            "[PHASE7] Risk Engine V2 initialized (SOLE AUTHORITY) — capital=%.2f max_pos=%.1f%% max_exp=%.1f%%",
            initial_capital,
            _risk_v2.config.max_position_pct,
            _risk_v2.config.max_global_exposure_pct,
        )
    except Exception as e:
        logger.error("[PHASE7] Risk V2 init FAILED: %s", e)
        _risk_v2 = None


def get_risk_v2():
    """Get Risk V2 instance (or None if disabled)."""
    return _risk_v2


def evaluate_risk_v2(
    symbol: str,
    side: str,
    proposed_size_usd: float,
    current_price: float,
    open_positions: Optional[Dict] = None,
):
    """
    Evaluate trade through Risk V2.
    Returns (allowed: bool, size_multiplier: float, reasons: list).

    Phase 9 SAFETY:
      - If Risk V2 is unavailable → DENY trade (never allow without risk check)
      - If trading gate is blocked → DENY trade
    """
    # ── TRADING GATE CHECK (Phase 10: MANDATORY — deny if supervisor absent) ──
    if _supervisor is None:
        logger.error("[PHASE7] TRADE DENIED — supervisor unavailable (no trading gate)")
        return False, 0.0, ["trading_gate_unavailable"]
    allowed, reason = _supervisor.is_trading_allowed()
    if not allowed:
        logger.warning("[PHASE7] TRADE DENIED by trading gate: %s", reason)
        return False, 0.0, [f"trading_gate_blocked:{reason}"]

    # ── RISK ENGINE CHECK ──
    if _risk_v2 is None:
        logger.error("[PHASE7] TRADE DENIED — Risk V2 engine unavailable (SAFETY: no trading without risk check)")
        return False, 0.0, ["risk_engine_unavailable"]

    try:
        verdict = _risk_v2.evaluate(
            symbol=symbol,
            side=side,
            proposed_size_usd=proposed_size_usd,
            current_price=current_price,
            open_positions=open_positions,
        )
        return verdict.allowed, verdict.size_multiplier, verdict.reasons
    except Exception as e:
        logger.error("[PHASE7] Risk V2 evaluate FAILED — DENYING trade: %s", e)
        return False, 0.0, [f"risk_v2_error:{e}"]


def record_risk_v2_trade_result(symbol: str, pnl: float, is_win: bool) -> None:
    """Record trade result in Risk V2 if enabled."""
    if _risk_v2 is None:
        return
    try:
        _risk_v2.record_trade_result(symbol, pnl, is_win)
    except Exception as e:
        logger.warning("[PHASE6] Risk V2 record_trade_result error: %s", e)


def update_risk_v2_capital(capital: float) -> None:
    """Update Risk V2 capital. Safe to call always."""
    if _risk_v2 is not None:
        try:
            _risk_v2.update_capital(capital)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  RECONCILIATION V2
# ═══════════════════════════════════════════════════════════════

_reconciliation_v2 = None


def start_reconciliation_v2(bitget_executor=None, kraken_executor=None) -> None:
    """Start reconciliation V2 service (always active)."""
    global _reconciliation_v2
    try:
        from backend.services.reconciliation_v2 import ReconciliationV2Service
        _reconciliation_v2 = ReconciliationV2Service(
            bitget_executor=bitget_executor,
            kraken_executor=kraken_executor,
        )
        _reconciliation_v2.start()
        logger.info(
            "[PHASE7] Reconciliation V2 started — interval=%ds",
            _reconciliation_v2.config.interval_sec,
        )
    except Exception as e:
        logger.error("[PHASE7] Reconciliation V2 start FAILED: %s", e)
        _reconciliation_v2 = None


def get_reconciliation_v2():
    return _reconciliation_v2


# ═══════════════════════════════════════════════════════════════
#  ISOLATION / SUPERVISOR
# ═══════════════════════════════════════════════════════════════

_supervisor = None


async def start_isolation(services: Dict[str, Callable] = None) -> None:
    """
    Wrap key services in failure isolation (always active).
    services: dict of name → async callable (the service loop).
    """
    global _supervisor
    if not services:
        logger.info("[PHASE7] Isolation active but no services to wrap yet")
        return

    try:
        from core.isolation import get_supervisor
        _supervisor = get_supervisor()

        for name, run_fn in services.items():
            _supervisor.register(name, run_fn, max_restarts=10)

        await _supervisor.start_all()
        logger.info("[PHASE7] Supervisor started — %d services wrapped", len(services))
    except Exception as e:
        logger.error("[PHASE7] Isolation start FAILED: %s", e)
        _supervisor = None


def get_supervisor_status() -> Dict[str, Any]:
    """Get supervisor status if available."""
    if _supervisor is None:
        return {"enabled": False}
    try:
        return {"enabled": True, **_supervisor.get_status()}
    except Exception:
        return {"enabled": True, "error": "status_unavailable"}


# ═══════════════════════════════════════════════════════════════
#  LIFECYCLE  — Main entry points
# ═══════════════════════════════════════════════════════════════

async def boot_phase6(
    initial_capital: float = 0.0,
    bitget_executor=None,
    kraken_executor=None,
) -> Dict[str, bool]:
    """
    Boot all institutional modules in correct order.
    No flags — everything always starts. V2 = sole path.
    Returns dict of module → actually_started.
    """
    init_phase6_flags()
    results = {}

    # 1. Observability
    init_observability()
    results["observability"] = _health_aggregator is not None

    # 2. Event Bus (must start before subscribers)
    await start_event_bus()
    results["event_bus"] = _event_bus is not None

    # 3. Alert Manager (needs event bus)
    init_alert_manager()
    results["alert_manager"] = _alert_manager is not None

    # 4. Risk V2 (sole risk authority)
    init_risk_engine_v2(initial_capital)
    results["risk_v2"] = _risk_v2 is not None

    # 5. Wire Risk V2 event subscriptions
    _wire_risk_v2_subscriptions()
    results["risk_v2_subscriptions"] = _risk_v2 is not None

    # 6. Reconciliation V2
    start_reconciliation_v2(bitget_executor, kraken_executor)
    results["reconciliation_v2"] = _reconciliation_v2 is not None

    # 7. Wire Reconciliation event subscriptions
    _wire_reconciliation_subscriptions()
    results["reconciliation_subscriptions"] = _reconciliation_v2 is not None

    # 8. Isolation — register services under supervision for crash resilience
    isolation_services = {}
    if _reconciliation_v2 is not None and hasattr(_reconciliation_v2, 'run_loop'):
        isolation_services["reconciliation"] = _reconciliation_v2.run_loop
    # Event bus dispatch is self-supervised but we register a health probe
    if _event_bus is not None:
        async def _event_bus_health_loop():
            """Probe: checks bus health, purges old dead letters, detects anomalies."""
            import asyncio as _aio
            from core.events.event_types import SystemEvent, EventType
            while True:
                stats = _event_bus.get_stats()
                if stats.get("total_errors", 0) > 100:
                    logger.warning("[ISOLATION] Event bus error count high: %d", stats["total_errors"])
                if stats.get("max_backlog", 0) > 5000:
                    logger.warning("[ISOLATION] Event bus backlog high: %d", stats["max_backlog"])
                # Auto-purge dead letters older than 1 hour
                _event_bus.purge_dead_letters(max_age_sec=3600.0)
                await _aio.sleep(30)
        isolation_services["event_bus_health"] = _event_bus_health_loop

    # Risk engine health probe (Phase 9 — critical service)
    if _risk_v2 is not None:
        async def _risk_engine_health_loop():
            """Probe: verifies risk engine responds, checks for anomalies."""
            import asyncio as _aio
            while True:
                try:
                    # Simple health check — ensure risk engine is responsive
                    if hasattr(_risk_v2, 'get_status'):
                        _risk_v2.get_status()
                except Exception as e:
                    logger.error("[ISOLATION] Risk engine health check FAILED: %s", e)
                    raise  # Let supervisor handle restart
                await _aio.sleep(30)
        isolation_services["risk_engine"] = _risk_engine_health_loop

    # Phase 10: Long-run monitor (production-grade health surveillance)
    try:
        from core.long_run_monitor import get_monitor
        _long_run_monitor = get_monitor()
        isolation_services["long_run_monitor"] = _long_run_monitor.run
        logger.info("[PHASE7] Long-run monitor registered as supervised service")
    except Exception as e:
        logger.error("[PHASE7] Long-run monitor init failed: %s", e)

    if isolation_services:
        await start_isolation(isolation_services)
        results["isolation"] = _supervisor is not None
    else:
        results["isolation"] = False

    logger.info("[PHASE7] Boot complete — %s", results)

    # Publish startup event
    if _event_bus:
        try:
            from core.events.event_types import SystemEvent, EventType
            publish_event(SystemEvent(
                EventType.SYSTEM_STARTUP,
                message="Institutional modules booted (V2-only, no flags)",
                details={"modules": results},
                source="phase7_wiring",
            ))
        except Exception:
            pass

    return results


# ═══════════════════════════════════════════════════════════════
#  EVENT SUBSCRIPTIONS — Risk V2 reacts to trade events
# ═══════════════════════════════════════════════════════════════

def _wire_risk_v2_subscriptions() -> None:
    """Subscribe Risk V2 to trade lifecycle events for auto-update."""
    if _risk_v2 is None or _event_bus is None:
        return

    async def _on_trade_closed(event) -> None:
        """Update Risk V2 after trade close."""
        try:
            payload = event.payload if hasattr(event, 'payload') else {}
            symbol = getattr(event, 'symbol', None) or payload.get("symbol", "")
            pnl = payload.get("pnl", 0.0)
            is_win = pnl > 0
            _risk_v2.record_trade_result(symbol, pnl, is_win)
            logger.debug("[PHASE7] Risk V2 updated after TRADE_CLOSED: %s pnl=%.2f", symbol, pnl)
        except Exception as e:
            logger.warning("[PHASE7] Risk V2 on_trade_closed error: %s", e)

    async def _on_order_filled(event) -> None:
        """Update Risk V2 position tracking after fill."""
        try:
            payload = event.payload if hasattr(event, 'payload') else {}
            symbol = getattr(event, 'symbol', None) or payload.get("symbol", "")
            price = getattr(event, 'price', 0) or payload.get("price", 0)
            qty = payload.get("quantity", 0) or payload.get("qty", 0)
            notional = abs(float(price) * float(qty)) if price and qty else 0
            _risk_v2.update_position(symbol, float(qty), notional)
            logger.debug("[PHASE7] Risk V2 position updated after ORDER_FILLED: %s", symbol)
        except Exception as e:
            logger.warning("[PHASE7] Risk V2 on_order_filled error: %s", e)

    async def _on_pnl_update(event) -> None:
        """Update Risk V2 capital from PnL events."""
        try:
            payload = event.payload if hasattr(event, 'payload') else {}
            new_capital = payload.get("equity", 0) or payload.get("balance", 0)
            if new_capital > 0:
                _risk_v2.update_capital(float(new_capital))
        except Exception as e:
            logger.warning("[PHASE7] Risk V2 on_pnl_update error: %s", e)

    from core.events.event_types import EventType
    _event_bus.subscribe(
        _on_trade_closed,
        {EventType.TRADE_CLOSED},
        name="risk_v2_trade_closed",
    )
    _event_bus.subscribe(
        _on_order_filled,
        {EventType.ORDER_FILLED},
        name="risk_v2_order_filled",
    )
    _event_bus.subscribe(
        _on_pnl_update,
        {EventType.PNL_UPDATE, EventType.EQUITY_UPDATE},
        name="risk_v2_capital_update",
    )
    logger.info("[PHASE7] Risk V2 subscribed to TRADE_CLOSED, ORDER_FILLED, PNL_UPDATE, EQUITY_UPDATE")


def _wire_reconciliation_subscriptions() -> None:
    """Subscribe Reconciliation V2 to trade events for immediate verification."""
    if _reconciliation_v2 is None or _event_bus is None:
        return

    async def _on_trade_event(event) -> None:
        """Trigger immediate reconciliation check after trade lifecycle events."""
        try:
            payload = event.payload if hasattr(event, 'payload') else {}
            symbol = getattr(event, 'symbol', None) or payload.get("symbol", "")
            logger.debug(
                "[PHASE7] Reconciliation triggered by %s for %s",
                event.event_type.value, symbol,
            )
            # Request an immediate reconciliation pass (non-blocking)
            if hasattr(_reconciliation_v2, 'request_check'):
                _reconciliation_v2.request_check(symbol)
        except Exception as e:
            logger.warning("[PHASE7] Reconciliation on_trade_event error: %s", e)

    from core.events.event_types import EventType
    _event_bus.subscribe(
        _on_trade_event,
        {EventType.TRADE_OPENED, EventType.TRADE_CLOSED, EventType.ORDER_FILLED},
        name="reconciliation_trade_events",
    )
    logger.info("[PHASE7] Reconciliation V2 subscribed to TRADE_OPENED, TRADE_CLOSED, ORDER_FILLED")


async def shutdown_phase6() -> None:
    """Graceful shutdown of all institutional modules in reverse order."""
    logger.info("[PHASE7] Shutting down...")

    # Publish shutdown event before stopping bus
    if _event_bus:
        try:
            from core.events.event_types import SystemEvent, EventType
            publish_event(SystemEvent(
                EventType.SYSTEM_SHUTDOWN,
                message="Institutional modules shutting down",
                source="phase7_wiring",
            ))
        except Exception:
            pass

    # 1. Stop supervisor (reverse order)
    if _supervisor:
        try:
            await _supervisor.stop_all()
        except Exception as e:
            logger.error("[PHASE7] Supervisor stop error: %s", e)

    # 2. Stop reconciliation
    if _reconciliation_v2:
        try:
            _reconciliation_v2.stop()
        except Exception as e:
            logger.error("[PHASE7] Reconciliation V2 stop error: %s", e)

    # 3. Stop event bus (last, so others can emit final events)
    if _event_bus:
        try:
            await _event_bus.stop()
        except Exception as e:
            logger.error("[PHASE7] Event bus stop error: %s", e)

    logger.info("[PHASE7] Shutdown complete")
