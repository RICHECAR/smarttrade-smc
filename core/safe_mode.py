"""
Safe Mode Controller — Centralized Degraded Operation Mode
══════════════════════════════════════════════════════════

When activated, Safe Mode:
  1. BLOCKS all new trade entries (existing positions managed normally)
  2. ALLOWS position closes and reduce-only orders
  3. ALLOWS dashboard read operations
  4. LOGS the trigger reason persistently

Activation triggers:
  - Kill switch activated
  - Startup safety checks failed
  - Ledger directory unwritable
  - Exchange API unreachable for > SAFE_MODE_API_TIMEOUT_SEC
  - Risk engine exception
  - Manual activation via API

Deactivation:
  - MANUAL ONLY (no auto-recovery) — requires explicit operator action

Persistence:
  - State saved to data/safe_mode.json (survives restarts)

Usage:
    from core.safe_mode import safe_mode_controller

    # Check before any trade
    if safe_mode_controller.is_active:
        logger.warning("Safe mode active: %s", safe_mode_controller.reason)
        return  # block trade

    # Activate on incident
    safe_mode_controller.activate("API_UNREACHABLE", "Kraken API down for 120s")

    # Deactivate (manual only)
    safe_mode_controller.deactivate(operator="admin")
"""

import json
import logging
import os
import time
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("smarttrade.safe_mode")

_DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
_SAFE_MODE_FILE = _DATA_DIR / "safe_mode.json"


@dataclass
class SafeModeState:
    active: bool = False
    trigger: str = ""
    reason: str = ""
    activated_at: Optional[str] = None
    deactivated_at: Optional[str] = None
    deactivated_by: Optional[str] = None


class SafeModeController:
    """
    Centralized safe/degraded mode controller.

    Thread-safe via threading.Lock for concurrent access.
    """

    def __init__(self, state_file: Optional[Path] = None):
        self._state_file = state_file or _SAFE_MODE_FILE
        self._state = SafeModeState()
        self._lock = threading.Lock()
        self._restore()

    # ── Public API ────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._state.active

    @property
    def reason(self) -> str:
        return self._state.reason

    @property
    def trigger(self) -> str:
        return self._state.trigger

    @property
    def status(self) -> dict:
        return asdict(self._state)

    def activate(self, trigger: str, reason: str) -> None:
        """Activate safe mode. Idempotent — if already active, just logs."""
        with self._lock:
            if self._state.active:
                logger.warning(
                    "[SAFE_MODE] Already active (trigger=%s). New trigger ignored: %s / %s",
                    self._state.trigger, trigger, reason,
                )
                return

            self._state.active = True
            self._state.trigger = trigger
            self._state.reason = reason
            self._state.activated_at = datetime.now(timezone.utc).isoformat()
            self._state.deactivated_at = None
            self._state.deactivated_by = None
            self._persist()

        logger.critical(
            "[SAFE_MODE] *** ACTIVATED *** trigger=%s reason=%s",
            trigger, reason,
        )

    def deactivate(self, operator: str = "unknown") -> None:
        """Deactivate safe mode. MANUAL ONLY."""
        with self._lock:
            if not self._state.active:
                logger.info("[SAFE_MODE] Already inactive, nothing to deactivate")
                return

            old_trigger = self._state.trigger
            self._state.active = False
            self._state.deactivated_at = datetime.now(timezone.utc).isoformat()
            self._state.deactivated_by = operator
            self._persist()

        logger.warning(
            "[SAFE_MODE] Deactivated by %s (was: %s)", operator, old_trigger,
        )

    def check_allow_trade(self) -> tuple:
        """
        Pre-trade gate.
        Returns: (allowed: bool, block_reason: str or None)
        """
        if self._state.active:
            return False, f"SAFE_MODE: {self._state.trigger} — {self._state.reason}"
        return True, None

    # ── Persistence ───────────────────────────────────

    def _persist(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps(asdict(self._state), indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("[SAFE_MODE] Failed to persist state: %s", e)

    def _restore(self) -> None:
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                self._state = SafeModeState(**{
                    k: v for k, v in data.items()
                    if k in SafeModeState.__dataclass_fields__
                })
                if self._state.active:
                    logger.critical(
                        "[SAFE_MODE] RESTORED from disk — active since %s: %s",
                        self._state.activated_at, self._state.reason,
                    )
        except Exception as e:
            logger.error("[SAFE_MODE] Failed to restore state: %s", e)


# ── Singleton ─────────────────────────────────────────
safe_mode_controller = SafeModeController()
