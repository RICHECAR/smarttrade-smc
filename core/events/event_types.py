"""
Event Types — Typed event payloads for the institutional event bus.
═══════════════════════════════════════════════════════════════════

Every cross-service communication MUST go through a typed event.
No raw dicts. No untyped callbacks. Full traceability.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, Optional


class EventType(str, Enum):
    """All event types flowing through the bus."""

    # ── Trade lifecycle ──
    SIGNAL_GENERATED = "signal.generated"
    SIGNAL_BLOCKED = "signal.blocked"
    ORDER_SUBMITTED = "order.submitted"
    ORDER_FILLED = "order.filled"
    ORDER_REJECTED = "order.rejected"
    ORDER_CANCELLED = "order.cancelled"
    TRADE_OPENED = "trade.opened"
    TRADE_CLOSED = "trade.closed"
    TRADE_UPDATED = "trade.updated"
    POSITION_CHANGED = "position.changed"

    # ── Risk ──
    RISK_VERDICT = "risk.verdict"
    RISK_KILL_SWITCH = "risk.kill_switch"
    RISK_DRAWDOWN_ALERT = "risk.drawdown_alert"
    RISK_EXPOSURE_BREACH = "risk.exposure_breach"
    RISK_DAILY_LIMIT = "risk.daily_limit"

    # ── Reconciliation ──
    RECONCILIATION_STARTED = "reconciliation.started"
    RECONCILIATION_COMPLETED = "reconciliation.completed"
    RECONCILIATION_DIVERGENCE = "reconciliation.divergence"
    RECONCILIATION_CRITICAL = "reconciliation.critical"

    # ── System ──
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"
    SYSTEM_HEARTBEAT = "system.heartbeat"
    SYSTEM_ERROR = "system.error"
    SAFE_MODE_ACTIVATED = "system.safe_mode_activated"
    SAFE_MODE_DEACTIVATED = "system.safe_mode_deactivated"
    CIRCUIT_BREAKER_OPEN = "system.circuit_breaker_open"
    CIRCUIT_BREAKER_CLOSED = "system.circuit_breaker_closed"

    # ── Alert ──
    ALERT_INFO = "alert.info"
    ALERT_WARNING = "alert.warning"
    ALERT_CRITICAL = "alert.critical"

    # ── Performance ──
    PNL_UPDATE = "performance.pnl_update"
    EQUITY_UPDATE = "performance.equity_update"
    BALANCE_UPDATE = "performance.balance_update"


class EventPriority(int, Enum):
    """Priority levels — higher = processed first."""
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass(frozen=True)
class Event:
    """
    Base event — immutable, timestamped, traceable.

    Every event gets a unique ID and monotonic timestamp.
    trace_id links correlated events (e.g. signal → order → fill → trade).
    """
    event_type: EventType
    payload: Dict[str, Any]
    priority: EventPriority = EventPriority.NORMAL
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    timestamp: float = field(default_factory=time.time)
    source: str = ""  # e.g. "risk_engine", "broker_router", "reconciler"

    def with_trace(self, trace_id: str) -> Event:
        """Create a copy with a shared trace_id for correlation."""
        return Event(
            event_type=self.event_type,
            payload=self.payload,
            priority=self.priority,
            event_id=uuid.uuid4().hex[:16],
            trace_id=trace_id,
            timestamp=time.time(),
            source=self.source,
        )


# ═══════════════════════════════════════════════════════════════
#  CONVENIENCE FACTORIES — Type-safe event creation
# ═══════════════════════════════════════════════════════════════

def TradeEvent(
    event_type: EventType,
    *,
    symbol: str,
    broker: str,
    side: str = "",
    qty: float = 0.0,
    price: float = 0.0,
    pnl: Optional[float] = None,
    order_id: str = "",
    trade_id: str = "",
    trace_id: str = "",
    extra: Optional[Dict[str, Any]] = None,
    source: str = "trade_engine",
) -> Event:
    payload = {
        "symbol": symbol,
        "broker": broker,
        "side": side,
        "qty": qty,
        "price": price,
        "order_id": order_id,
        "trade_id": trade_id,
    }
    if pnl is not None:
        payload["pnl"] = pnl
    if extra:
        payload.update(extra)
    return Event(
        event_type=event_type,
        payload=payload,
        priority=EventPriority.HIGH,
        trace_id=trace_id or uuid.uuid4().hex[:16],
        source=source,
    )


def RiskEvent(
    event_type: EventType,
    *,
    reason: str,
    symbol: str = "",
    broker: str = "",
    current_value: float = 0.0,
    threshold: float = 0.0,
    action: str = "",
    trace_id: str = "",
    source: str = "risk_engine",
) -> Event:
    return Event(
        event_type=event_type,
        payload={
            "reason": reason,
            "symbol": symbol,
            "broker": broker,
            "current_value": current_value,
            "threshold": threshold,
            "action": action,
        },
        priority=EventPriority.CRITICAL,
        trace_id=trace_id or uuid.uuid4().hex[:16],
        source=source,
    )


def ReconciliationEvent(
    event_type: EventType,
    *,
    broker: str,
    details: Dict[str, Any],
    divergence_type: str = "",
    severity: str = "info",
    trace_id: str = "",
    source: str = "reconciler",
) -> Event:
    prio = EventPriority.NORMAL
    if severity == "warning":
        prio = EventPriority.HIGH
    elif severity == "critical":
        prio = EventPriority.CRITICAL

    return Event(
        event_type=event_type,
        payload={
            "broker": broker,
            "divergence_type": divergence_type,
            "severity": severity,
            "details": details,
        },
        priority=prio,
        trace_id=trace_id or uuid.uuid4().hex[:16],
        source=source,
    )


def SystemEvent(
    event_type: EventType,
    *,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    source: str = "system",
) -> Event:
    return Event(
        event_type=event_type,
        payload={"message": message, "details": details or {}},
        priority=EventPriority.NORMAL if "heartbeat" in event_type.value else EventPriority.HIGH,
        source=source,
    )


def AlertEvent(
    event_type: EventType,
    *,
    title: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    source: str = "alerting",
) -> Event:
    prio_map = {
        EventType.ALERT_INFO: EventPriority.LOW,
        EventType.ALERT_WARNING: EventPriority.HIGH,
        EventType.ALERT_CRITICAL: EventPriority.CRITICAL,
    }
    return Event(
        event_type=event_type,
        payload={"title": title, "message": message, "details": details or {}},
        priority=prio_map.get(event_type, EventPriority.NORMAL),
        source=source,
    )
