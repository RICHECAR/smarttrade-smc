"""
Alert Manager — Telegram / Discord notification service.
═════════════════════════════════════════════════════════

Sends structured alerts for every significant bot event.
Designed for async usage from any module.

Phase 6: Event Bus integration — subscribes to risk/reconciliation/system
events and dispatches notifications automatically.

Levels:
  CRITICAL — requires immediate human attention
  WARNING  — degraded but recoverable
  INFO     — trade lifecycle events

Env:
  ALERT_TELEGRAM_ENABLED=true
  ALERT_TELEGRAM_TOKEN=<bot_token>
  ALERT_TELEGRAM_CHAT_ID=<chat_id>
  ALERT_DISCORD_ENABLED=false
  ALERT_DISCORD_WEBHOOK_URL=<url>
  ALERT_MIN_LEVEL=INFO
  ALERT_RATE_LIMIT_SEC=5
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Dict, Optional

import httpx
from core.env_loader import safe_load_dotenv

safe_load_dotenv()
logger = logging.getLogger("smarttrade.alerts")


# ═══════════════════════════════════════════════════
#  EVENT BUS → ALERT BRIDGE (Phase 6)
# ═══════════════════════════════════════════════════
# Maps EventType → AlertLevel for auto-dispatch
_EVENT_ALERT_MAP: Dict[str, int] = {}  # populated after AlertLevel defined


class AlertLevel(IntEnum):
    INFO = 0
    WARNING = 1
    CRITICAL = 2


_LEVEL_EMOJI = {
    AlertLevel.INFO: "ℹ️",
    AlertLevel.WARNING: "⚠️",
    AlertLevel.CRITICAL: "🚨",
}

_LEVEL_NAMES = {
    AlertLevel.INFO: "INFO",
    AlertLevel.WARNING: "WARNING",
    AlertLevel.CRITICAL: "CRITICAL",
}


def _parse_level(val: str) -> AlertLevel:
    return {"info": AlertLevel.INFO, "warning": AlertLevel.WARNING, "critical": AlertLevel.CRITICAL}.get(
        val.strip().lower(), AlertLevel.INFO
    )


class AlertManager:
    """Singleton alert dispatcher — Telegram + Discord."""

    _instance: Optional["AlertManager"] = None

    def __init__(self):
        self._tg_enabled = os.getenv("ALERT_TELEGRAM_ENABLED", "false").lower() == "true"
        self._tg_token = os.getenv("ALERT_TELEGRAM_TOKEN", "")
        self._tg_chat_id = os.getenv("ALERT_TELEGRAM_CHAT_ID", "")
        self._dc_enabled = os.getenv("ALERT_DISCORD_ENABLED", "false").lower() == "true"
        self._dc_webhook = os.getenv("ALERT_DISCORD_WEBHOOK_URL", "")
        self._min_level = _parse_level(os.getenv("ALERT_MIN_LEVEL", "INFO"))
        self._rate_limit_sec = float(os.getenv("ALERT_RATE_LIMIT_SEC", "5"))
        self._last_send: Dict[str, float] = {}  # dedup key -> timestamp
        self._send_count = 0
        self._error_count = 0

    @classmethod
    def get(cls) -> "AlertManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        """Reset singleton (for testing)."""
        cls._instance = None

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    async def send(
        self,
        level: AlertLevel,
        title: str,
        details: Optional[Dict[str, Any]] = None,
        dedup_key: Optional[str] = None,
    ) -> bool:
        """
        Send an alert if level >= min_level and rate limit not hit.

        Args:
            level: AlertLevel (INFO, WARNING, CRITICAL)
            title: Short alert title
            details: Optional k/v context (symbol, pnl, reason, etc.)
            dedup_key: Optional key for rate-limiting identical alerts

        Returns:
            True if sent, False if filtered/skipped
        """
        if level < self._min_level:
            return False

        # Rate limit per dedup_key
        now = time.monotonic()
        if dedup_key:
            last = self._last_send.get(dedup_key, 0)
            if now - last < self._rate_limit_sec:
                return False
            self._last_send[dedup_key] = now

        message = self._format_message(level, title, details)

        # Always log
        log_fn = {AlertLevel.INFO: logger.info, AlertLevel.WARNING: logger.warning, AlertLevel.CRITICAL: logger.critical}
        log_fn.get(level, logger.info)("[ALERT] %s | %s", _LEVEL_NAMES[level], title)

        # Dispatch
        sent = False
        if self._tg_enabled and self._tg_token and self._tg_chat_id:
            sent = await self._send_telegram(message) or sent
        if self._dc_enabled and self._dc_webhook:
            sent = await self._send_discord(message) or sent

        if sent:
            self._send_count += 1
        return sent

    async def critical(self, title: str, **details):
        return await self.send(AlertLevel.CRITICAL, title, details or None, dedup_key=title)

    async def warning(self, title: str, **details):
        return await self.send(AlertLevel.WARNING, title, details or None, dedup_key=title)

    async def info(self, title: str, **details):
        return await self.send(AlertLevel.INFO, title, details or None)

    # ──────────────────────────────────────────────────
    #  MESSAGE FORMATTING
    # ──────────────────────────────────────────────────

    def _format_message(self, level: AlertLevel, title: str, details: Optional[Dict[str, Any]]) -> str:
        emoji = _LEVEL_EMOJI.get(level, "")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [f"{emoji} [{_LEVEL_NAMES[level]}] {title}", f"Time: {ts}"]
        if details:
            for k, v in details.items():
                lines.append(f"{k}: {v}")
        return "\n".join(lines)

    # ──────────────────────────────────────────────────
    #  TELEGRAM
    # ──────────────────────────────────────────────────

    async def _send_telegram(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        payload = {"chat_id": self._tg_chat_id, "text": text, "parse_mode": "HTML"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    return True
                logger.error("[ALERT_TG] HTTP %d: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            self._error_count += 1
            logger.error("[ALERT_TG] Send failed: %s", e)
        return False

    # ──────────────────────────────────────────────────
    #  DISCORD
    # ──────────────────────────────────────────────────

    async def _send_discord(self, text: str) -> bool:
        payload = {"content": text}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._dc_webhook, json=payload)
                if resp.status_code in (200, 204):
                    return True
                logger.error("[ALERT_DC] HTTP %d: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            self._error_count += 1
            logger.error("[ALERT_DC] Send failed: %s", e)
        return False

    # ──────────────────────────────────────────────────
    #  STATUS
    # ──────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        return {
            "telegram_enabled": self._tg_enabled,
            "discord_enabled": self._dc_enabled,
            "min_level": _LEVEL_NAMES[self._min_level],
            "send_count": self._send_count,
            "error_count": self._error_count,
            "event_bus_connected": self._event_bus_connected,
        }

    # ──────────────────────────────────────────────────
    #  EVENT BUS INTEGRATION (Phase 6)
    # ──────────────────────────────────────────────────

    _event_bus_connected: bool = False

    def connect_event_bus(self) -> None:
        """
        Subscribe to EventBus for automatic alert dispatch.
        Call once at startup after event bus is running.
        """
        try:
            from core.events.event_bus import get_event_bus
            from core.events.event_types import EventType, EventPriority

            bus = get_event_bus()

            # Subscribe to all alert-worthy events
            alert_events = {
                EventType.RISK_KILL_SWITCH,
                EventType.RISK_EXPOSURE_BREACH,
                EventType.RISK_DRAWDOWN_ALERT,
                EventType.RISK_DAILY_LIMIT,
                EventType.RECONCILIATION_CRITICAL,
                EventType.RECONCILIATION_DIVERGENCE,
                EventType.SAFE_MODE_ACTIVATED,
                EventType.CIRCUIT_BREAKER_OPEN,
                EventType.SYSTEM_ERROR,
                EventType.TRADE_OPENED,
                EventType.TRADE_CLOSED,
                EventType.ALERT_INFO,
                EventType.ALERT_WARNING,
                EventType.ALERT_CRITICAL,
            }

            bus.subscribe(
                self._on_bus_event,
                alert_events,
                name="alert_manager_bus",
                min_priority=EventPriority.LOW,
            )
            self._event_bus_connected = True
            logger.info("[ALERT] Connected to EventBus — %d event types subscribed", len(alert_events))
        except Exception as e:
            logger.warning("[ALERT] Failed to connect to EventBus: %s", e)

    async def _on_bus_event(self, event) -> None:
        """Handle event from bus → dispatch alert."""
        from core.events.event_types import EventPriority

        # Map event priority to AlertLevel
        if event.priority == EventPriority.CRITICAL:
            level = AlertLevel.CRITICAL
        elif event.priority.value >= EventPriority.HIGH.value:
            level = AlertLevel.WARNING
        else:
            level = AlertLevel.INFO

        # Extract readable title from event
        title = event.event_type.value.replace(".", " ").upper()
        payload = event.payload
        details = {}

        if "symbol" in payload:
            details["symbol"] = payload["symbol"]
        if "broker" in payload:
            details["broker"] = payload["broker"]
        if "reason" in payload:
            details["reason"] = payload["reason"]
        if "message" in payload:
            details["message"] = payload["message"]
        if "pnl" in payload:
            details["pnl"] = f"{payload['pnl']:.2f}"
        if "title" in payload:
            title = payload["title"]
        details["trace_id"] = event.trace_id

        await self.send(
            level=level,
            title=title,
            details=details if details else None,
            dedup_key=f"{event.event_type.value}:{payload.get('symbol', '')}",
        )
