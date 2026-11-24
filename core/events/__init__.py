"""
Core Event Bus — Foundation for decoupled, institutional-grade architecture.

In-process async pub/sub with structured events.
Designed for seamless swap to Redis Streams / NATS when scaling beyond single-process.
"""

from core.events.event_bus import EventBus, get_event_bus
from core.events.event_types import (
    Event,
    EventType,
    TradeEvent,
    RiskEvent,
    ReconciliationEvent,
    SystemEvent,
    AlertEvent,
)

__all__ = [
    "EventBus",
    "get_event_bus",
    "Event",
    "EventType",
    "TradeEvent",
    "RiskEvent",
    "ReconciliationEvent",
    "SystemEvent",
    "AlertEvent",
]
