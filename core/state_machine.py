"""
System State Machine — Deterministic lifecycle progression.
═══════════════════════════════════════════════════════════════

Phase 12: INIT → PAPER → MICRO_CAPITAL → LIVE → SCALE_UP

Transitions are GATED:
  - preflight must pass
  - metrics must be stable
  - no recent critical incident
  - minimum time in current state

Fail-safe: if state cannot be determined → STOP trading.
"""

import enum
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Callable

logger = logging.getLogger("smarttrade.state_machine")

_PERSISTENCE_PATH = os.path.join("data", "system_state.json")


class SystemState(str, enum.Enum):
    INIT = "init"
    PAPER = "paper"
    MICRO_CAPITAL = "micro_capital"
    LIVE = "live"
    SCALE_UP = "scale_up"
    STOPPED = "stopped"  # fail-safe


# ─── Allowed transitions ──────────────────────────────────────

_TRANSITIONS = {
    SystemState.INIT: {SystemState.PAPER, SystemState.STOPPED},
    SystemState.PAPER: {SystemState.MICRO_CAPITAL, SystemState.STOPPED, SystemState.INIT},
    SystemState.MICRO_CAPITAL: {SystemState.LIVE, SystemState.PAPER, SystemState.STOPPED},
    SystemState.LIVE: {SystemState.SCALE_UP, SystemState.MICRO_CAPITAL, SystemState.STOPPED},
    SystemState.SCALE_UP: {SystemState.LIVE, SystemState.STOPPED},
    SystemState.STOPPED: {SystemState.INIT},
}

# Minimum seconds in a state before advancing forward
_MIN_DURATION = {
    SystemState.INIT: 0,
    SystemState.PAPER: 24 * 3600,          # 24h
    SystemState.MICRO_CAPITAL: 7 * 86400,  # 7 days
    SystemState.LIVE: 30 * 86400,          # 30 days
    SystemState.SCALE_UP: 0,
    SystemState.STOPPED: 0,
}


@dataclass
class TransitionCondition:
    """Result of a pre-transition check."""
    name: str
    passed: bool
    message: str


@dataclass
class StateRecord:
    """Persisted state with metadata."""
    state: str
    entered_at: float
    previous_state: str
    transition_reason: str
    transition_count: int = 0


class SystemStateMachine:
    """
    Deterministic state machine with persistence and transition gating.

    Every transition is:
      1. Validated against allowed graph
      2. Checked for minimum duration
      3. Validated via condition callbacks
      4. Persisted to disk
      5. Logged

    Fail-safe: transition_or_stop() — if uncertain, go to STOPPED.
    """

    def __init__(self, persistence_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._path = persistence_path or _PERSISTENCE_PATH
        self._conditions: List[Callable[[], TransitionCondition]] = []
        self._record = self._load()
        logger.info(
            "[STATE] Loaded: %s (since %s)",
            self._record.state,
            time.strftime("%Y-%m-%d %H:%M", time.localtime(self._record.entered_at)),
        )

    def _load(self) -> StateRecord:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    data = json.load(f)
                return StateRecord(**data)
            except Exception as exc:
                logger.error("[STATE] Corrupt state file, resetting: %s", exc)
        return StateRecord(
            state=SystemState.INIT.value,
            entered_at=time.time(),
            previous_state="",
            transition_reason="initial",
        )

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(asdict(self._record), f, indent=2)
        except Exception as exc:
            logger.error("[STATE] Failed to persist state: %s", exc)

    # ── Public API ─────────────────────────────────────────────

    @property
    def state(self) -> SystemState:
        with self._lock:
            return SystemState(self._record.state)

    @property
    def time_in_state(self) -> float:
        """Seconds since entering current state."""
        with self._lock:
            return time.time() - self._record.entered_at

    @property
    def record(self) -> StateRecord:
        with self._lock:
            return StateRecord(**asdict(self._record))

    def register_condition(self, fn: Callable[[], TransitionCondition]) -> None:
        """Register a pre-transition check."""
        self._conditions.append(fn)

    def can_transition(self, target: SystemState) -> tuple:
        """
        Check if transition is allowed.
        Returns (allowed: bool, blockers: list[str]).
        """
        with self._lock:
            current = SystemState(self._record.state)

        blockers = []

        # Graph check
        allowed_targets = _TRANSITIONS.get(current, set())
        if target not in allowed_targets:
            blockers.append(
                f"Transition {current.value} → {target.value} not allowed"
            )
            return False, blockers

        # STOPPED is always allowed (emergency)
        if target == SystemState.STOPPED:
            return True, []

        # Backward transitions always allowed (safety)
        state_order = [
            SystemState.INIT, SystemState.PAPER, SystemState.MICRO_CAPITAL,
            SystemState.LIVE, SystemState.SCALE_UP,
        ]
        current_idx = state_order.index(current) if current in state_order else 0
        target_idx = state_order.index(target) if target in state_order else 0
        is_forward = target_idx > current_idx

        # Minimum duration for forward transitions only
        if is_forward:
            min_dur = _MIN_DURATION.get(current, 0)
            elapsed = time.time() - self._record.entered_at
            if elapsed < min_dur:
                remaining_h = (min_dur - elapsed) / 3600
                blockers.append(
                    f"Minimum {min_dur / 3600:.0f}h in {current.value}, "
                    f"{remaining_h:.1f}h remaining"
                )

        # Custom conditions for forward transitions
        if is_forward:
            for fn in self._conditions:
                try:
                    cond = fn()
                    if not cond.passed:
                        blockers.append(f"[{cond.name}] {cond.message}")
                except Exception as exc:
                    blockers.append(f"Condition check failed: {exc}")

        return len(blockers) == 0, blockers

    def transition(self, target: SystemState, reason: str) -> tuple:
        """
        Attempt transition.
        Returns (success: bool, message: str).
        """
        allowed, blockers = self.can_transition(target)
        if not allowed:
            msg = f"Transition BLOCKED: {'; '.join(blockers)}"
            logger.warning("[STATE] %s", msg)
            return False, msg

        with self._lock:
            old = self._record.state
            self._record = StateRecord(
                state=target.value,
                entered_at=time.time(),
                previous_state=old,
                transition_reason=reason,
                transition_count=self._record.transition_count + 1,
            )
            self._save()

        logger.warning("[STATE] Transition: %s → %s (reason: %s)", old, target.value, reason)
        return True, f"{old} → {target.value}"

    def emergency_stop(self, reason: str) -> None:
        """Unconditional transition to STOPPED. Always succeeds."""
        with self._lock:
            old = self._record.state
            self._record = StateRecord(
                state=SystemState.STOPPED.value,
                entered_at=time.time(),
                previous_state=old,
                transition_reason=f"EMERGENCY: {reason}",
                transition_count=self._record.transition_count + 1,
            )
            self._save()
        logger.critical("[STATE] EMERGENCY STOP: %s → STOPPED (reason: %s)", old, reason)

    def is_trading_allowed(self) -> tuple:
        """
        Fail-safe gate: should the system trade?
        Returns (allowed: bool, reason: str | None).

        Rule: if state cannot be determined with certainty → STOP.
        """
        with self._lock:
            try:
                current = SystemState(self._record.state)
            except (ValueError, KeyError):
                return False, "State unknown — fail-safe STOP"

        if current == SystemState.STOPPED:
            return False, "System is STOPPED"
        if current == SystemState.INIT:
            return False, "System in INIT — not ready"

        return True, None

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "state": self._record.state,
                "entered_at": self._record.entered_at,
                "time_in_state_sec": time.time() - self._record.entered_at,
                "previous_state": self._record.previous_state,
                "transition_reason": self._record.transition_reason,
                "transition_count": self._record.transition_count,
                "trading_allowed": self.is_trading_allowed()[0],
            }


# ─── Singleton ────────────────────────────────────────────────

_machine: Optional[SystemStateMachine] = None
_singleton_lock = threading.Lock()


def get_state_machine(persistence_path: Optional[str] = None) -> SystemStateMachine:
    global _machine
    if _machine is None:
        with _singleton_lock:
            if _machine is None:
                _machine = SystemStateMachine(persistence_path)
    return _machine


def reset_state_machine() -> None:
    """Reset singleton (tests only)."""
    global _machine
    _machine = None
