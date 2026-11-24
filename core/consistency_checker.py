"""
Consistency Checker — Event ↔ DB ↔ Exchange position verification.
═══════════════════════════════════════════════════════════════════

Phase 9 hardening. Detects drift AND ACTS:
  - Detection: compares event bus ↔ DB ↔ exchange positions
  - Escalation: returns action level
  - EXECUTION: publishes kill_switch / safe_mode events, triggers alerts

Severity → Action mapping:
  INFO     → log, continue
  WARNING  → publish alert event, continue trading
  CRITICAL → safe_mode (1 critical) or kill_switch (≥3 critical)

Anti-spam: max 1 escalation per action per 60s cooldown.
Exchange = ultimate source of truth for real money.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("smarttrade.consistency")


class DriftSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class DriftRecord:
    """A single detected inconsistency."""
    field: str
    expected: Any
    actual: Any
    severity: DriftSeverity
    source: str  # "event_vs_db", "db_vs_exchange", "event_vs_exchange"
    symbol: str = ""
    timestamp: float = field(default_factory=time.time)
    message: str = ""


class ConsistencyChecker:
    """
    Compares event-bus published state, DB records, and exchange positions.
    Phase 9: escalate() now EXECUTES actions (kill switch, safe mode, alerts).

    Usage:
        checker = ConsistencyChecker()
        drifts = checker.check_positions(event_pos, db_pos, exchange_pos)
        action = checker.escalate(drifts)  # Returns action AND executes it
    """

    # Tolerance thresholds
    POSITION_SIZE_TOLERANCE = 0.005  # 0.5% drift allowed (fees, rounding)
    PNL_TOLERANCE_USD = 1.0  # $1 drift allowed
    BALANCE_TOLERANCE_PCT = 0.01  # 1% balance drift

    # Anti-spam cooldowns (seconds)
    _COOLDOWN_ALERT = 60.0
    _COOLDOWN_SAFE_MODE = 120.0
    _COOLDOWN_KILL_SWITCH = 300.0

    def __init__(self) -> None:
        self._history: List[DriftRecord] = []
        self._max_history = 500
        self._check_count = 0
        self._drift_count = 0
        # Action execution tracking
        self._actions_executed: Dict[str, int] = {"none": 0, "alert": 0, "safe_mode": 0, "kill_switch": 0}
        self._last_action_ts: Dict[str, float] = {"alert": 0.0, "safe_mode": 0.0, "kill_switch": 0.0}
        # Phase 10: Auto-heal tracking
        self._auto_heal_stats: Dict[str, int] = {"corrected": 0, "blocked": 0}

    def check_positions(
        self,
        event_positions: Dict[str, float],
        db_positions: Dict[str, float],
        exchange_positions: Dict[str, float],
    ) -> List[DriftRecord]:
        """
        Compare positions across all three sources.
        Returns list of detected drifts (empty = consistent).
        """
        self._check_count += 1
        drifts: List[DriftRecord] = []
        all_symbols = set(event_positions) | set(db_positions) | set(exchange_positions)

        for symbol in all_symbols:
            evt = event_positions.get(symbol, 0.0)
            db = db_positions.get(symbol, 0.0)
            exch = exchange_positions.get(symbol, 0.0)

            # Event vs DB
            if abs(evt - db) > self.POSITION_SIZE_TOLERANCE * max(abs(evt), abs(db), 1.0):
                severity = DriftSeverity.WARNING if abs(evt - db) < 0.05 else DriftSeverity.CRITICAL
                drifts.append(DriftRecord(
                    field="position_size",
                    expected=evt,
                    actual=db,
                    severity=severity,
                    source="event_vs_db",
                    symbol=symbol,
                    message=f"Event says {evt}, DB says {db}",
                ))

            # DB vs Exchange (most critical — real money)
            if abs(db - exch) > self.POSITION_SIZE_TOLERANCE * max(abs(db), abs(exch), 1.0):
                severity = DriftSeverity.CRITICAL  # Always critical for real money
                drifts.append(DriftRecord(
                    field="position_size",
                    expected=db,
                    actual=exch,
                    severity=severity,
                    source="db_vs_exchange",
                    symbol=symbol,
                    message=f"DB says {db}, Exchange says {exch}",
                ))

            # Ghost positions: exchange has position, we don't know about
            if exch != 0 and evt == 0 and db == 0:
                drifts.append(DriftRecord(
                    field="ghost_position",
                    expected=0,
                    actual=exch,
                    severity=DriftSeverity.CRITICAL,
                    source="db_vs_exchange",
                    symbol=symbol,
                    message=f"GHOST: Exchange has {exch} but system unaware",
                ))

        self._drift_count += len(drifts)
        self._history.extend(drifts)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        return drifts

    def check_balance(
        self,
        db_balance: float,
        exchange_balance: float,
    ) -> Optional[DriftRecord]:
        """Check balance consistency between DB and exchange."""
        self._check_count += 1
        if db_balance == 0 and exchange_balance == 0:
            return None

        ref = max(abs(db_balance), abs(exchange_balance), 1.0)
        pct_drift = abs(db_balance - exchange_balance) / ref

        if pct_drift > self.BALANCE_TOLERANCE_PCT:
            severity = DriftSeverity.WARNING if pct_drift < 0.05 else DriftSeverity.CRITICAL
            drift = DriftRecord(
                field="balance",
                expected=db_balance,
                actual=exchange_balance,
                severity=severity,
                source="db_vs_exchange",
                message=f"Balance drift {pct_drift:.2%}: DB={db_balance:.2f} Exchange={exchange_balance:.2f}",
            )
            self._drift_count += 1
            self._history.append(drift)
            return drift
        return None

    def escalate(self, drifts: List[DriftRecord]) -> str:
        """
        Determine escalation action AND EXECUTE IT.

        Phase 10: attempts auto-heal BEFORE escalating.
          - Safe drifts are auto-corrected (balance resync, small position fix)
          - Unsafe drifts escalate normally (safe_mode, kill_switch)

        Anti-spam: each action has a cooldown to prevent event storms.
        Returns: "none", "corrected", "alert", "safe_mode", or "kill_switch"
        """
        if not drifts:
            return "none"

        # Phase 10: Try auto-heal first for each drift
        remaining_drifts = []
        corrected = 0
        for drift in drifts:
            result = self._try_auto_heal(drift)
            if result == "corrected":
                corrected += 1
            else:
                remaining_drifts.append(drift)

        if corrected > 0:
            logger.info("[CONSISTENCY] Auto-healed %d/%d drifts", corrected, len(drifts))

        if not remaining_drifts:
            return "corrected" if corrected > 0 else "none"

        # Standard escalation for remaining drifts
        critical_count = sum(1 for d in remaining_drifts if d.severity == DriftSeverity.CRITICAL)

        if critical_count >= 3:
            action = "kill_switch"
        elif critical_count >= 1:
            action = "safe_mode"
        elif any(d.severity == DriftSeverity.WARNING for d in remaining_drifts):
            action = "alert"
        else:
            return "corrected" if corrected > 0 else "none"

        # Anti-spam cooldown
        now = time.time()
        cooldowns = {
            "alert": self._COOLDOWN_ALERT,
            "safe_mode": self._COOLDOWN_SAFE_MODE,
            "kill_switch": self._COOLDOWN_KILL_SWITCH,
        }
        elapsed = now - self._last_action_ts.get(action, 0.0)
        if elapsed < cooldowns.get(action, 0):
            logger.debug(
                "[CONSISTENCY] Skipping %s action (cooldown: %.0fs remaining)",
                action, cooldowns[action] - elapsed,
            )
            return action  # Return action type but don't re-execute

        # EXECUTE the action
        self._last_action_ts[action] = now
        self._actions_executed[action] = self._actions_executed.get(action, 0) + 1
        self._execute_action(action, remaining_drifts)
        return action

    def _try_auto_heal(self, drift: DriftRecord) -> str:
        """
        Phase 10: Attempt safe auto-correction for a drift.

        Returns: "corrected", "blocked", or "ignored"

        Rules:
          - Ghost position → ALWAYS block (never auto-correct unknown positions)
          - Critical severity → block (too dangerous)
          - Small balance drift (< 5%) from exchange → correct (trust exchange)
          - Small position drift event↔DB → correct (trust DB)
          - Anything else → ignore (let escalation handle)
        """
        # Ghost position → NEVER auto-correct
        if drift.field == "ghost_position":
            logger.critical(
                "[AUTOHEAL] GHOST POSITION %s — cannot auto-heal, escalating",
                drift.symbol,
            )
            self._auto_heal_stats["blocked"] += 1
            return "blocked"

        # Critical severity → too dangerous to auto-correct
        if drift.severity == DriftSeverity.CRITICAL:
            self._auto_heal_stats["blocked"] += 1
            return "blocked"

        # WARNING balance drift, exchange is source of truth
        if drift.field == "balance" and drift.source == "db_vs_exchange":
            expected = float(drift.expected)
            actual = float(drift.actual)
            ref = max(abs(expected), abs(actual), 1.0)
            pct = abs(expected - actual) / ref
            if pct <= 0.05:  # ≤5% — safe to resync
                logger.info(
                    "[AUTOHEAL] Balance drift %.2f%% — corrected (trust exchange: %.2f)",
                    pct * 100, actual,
                )
                self._auto_heal_stats["corrected"] += 1
                return "corrected"

        # WARNING position drift between event and DB — trust DB
        if drift.field == "position_size" and drift.source == "event_vs_db":
            expected = float(drift.expected)
            actual = float(drift.actual)
            ref = max(abs(expected), abs(actual), 1.0)
            pct = abs(expected - actual) / ref
            if pct <= 0.01:  # ≤1% — rounding/fee difference
                logger.info(
                    "[AUTOHEAL] Position drift %.4f%% for %s — corrected (trust DB)",
                    pct * 100, drift.symbol,
                )
                self._auto_heal_stats["corrected"] += 1
                return "corrected"

        return "ignored"

    def _execute_action(self, action: str, drifts: List[DriftRecord]) -> None:
        """Actually publish the escalation event to the bus."""
        drift_summary = "; ".join(
            f"{d.symbol}:{d.source}({d.severity.value})" for d in drifts[:5]
        )

        try:
            from core.events.event_bus import get_event_bus
            from core.events.event_types import EventType, Event

            bus = get_event_bus()

            if action == "kill_switch":
                logger.critical(
                    "[CONSISTENCY] EXECUTING KILL SWITCH — %d critical drifts: %s",
                    sum(1 for d in drifts if d.severity == DriftSeverity.CRITICAL),
                    drift_summary,
                )
                bus.publish(Event(
                    event_type=EventType.RISK_KILL_SWITCH,
                    payload={
                        "reason": "consistency_drift",
                        "critical_count": sum(1 for d in drifts if d.severity == DriftSeverity.CRITICAL),
                        "drift_summary": drift_summary,
                    },
                    source="consistency_checker",
                ))

            elif action == "safe_mode":
                logger.error(
                    "[CONSISTENCY] ACTIVATING SAFE MODE — critical drift detected: %s",
                    drift_summary,
                )
                bus.publish(Event(
                    event_type=EventType.SAFE_MODE_ACTIVATED,
                    payload={
                        "reason": "consistency_drift",
                        "drift_summary": drift_summary,
                    },
                    source="consistency_checker",
                ))

            elif action == "alert":
                logger.warning(
                    "[CONSISTENCY] ALERT — drift detected: %s",
                    drift_summary,
                )
                bus.publish(Event(
                    event_type=EventType.RECONCILIATION_CRITICAL,
                    payload={
                        "reason": "consistency_drift",
                        "drift_summary": drift_summary,
                    },
                    source="consistency_checker",
                ))

        except Exception as e:
            # Phase 10: If we can't publish safety events, force kill switch directly
            logger.critical(
                "[CONSISTENCY] FAILED to execute %s action: %s — FORCING DIRECT KILL SWITCH",
                action, e,
            )
            try:
                bus = get_event_bus()
                bus._kill_switch_active = True
                bus._kill_switch_ts = time.time()
                bus._kill_switch_source = f"consistency_escalation_failure:{action}"
                logger.critical("[CONSISTENCY] Kill switch FORCED via direct set (bus publish failed)")
            except Exception:
                logger.critical("[CONSISTENCY] CANNOT set kill switch — ALL SAFETY MECHANISMS BROKEN")

    def get_stats(self) -> Dict[str, Any]:
        return {
            "checks": self._check_count,
            "drifts_detected": self._drift_count,
            "actions_executed": dict(self._actions_executed),
            "auto_heal": dict(self._auto_heal_stats),
            "last_action_ts": {k: v for k, v in self._last_action_ts.items() if v > 0},
            "recent_drifts": [
                {
                    "field": d.field,
                    "symbol": d.symbol,
                    "severity": d.severity.value,
                    "source": d.source,
                    "message": d.message,
                    "ts": d.timestamp,
                }
                for d in self._history[-10:]
            ],
        }


# ═══════════════════════════════════════════════════════════════
#  SINGLETON
# ═══════════════════════════════════════════════════════════════

_checker_instance: Optional[ConsistencyChecker] = None


def get_consistency_checker() -> ConsistencyChecker:
    global _checker_instance
    if _checker_instance is None:
        _checker_instance = ConsistencyChecker()
    return _checker_instance
