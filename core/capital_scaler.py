"""
Capital Scaler — Progressive capital allocation with rollback.
═══════════════════════════════════════════════════════════════════

Phase 12: 1k → 5k → 10k → 25k → 50k → 100k

Scale-up requires:
  - Stability period met
  - 0 critical incidents in window
  - Metrics within thresholds

Scale-down (rollback) when:
  - Critical incident detected
  - Metrics breach thresholds
  - Manual operator intervention
"""

import enum
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger("smarttrade.capital_scaler")

_PERSISTENCE_PATH = os.path.join("data", "capital_scaler.json")


@dataclass(frozen=True)
class CapitalTierDef:
    """Definition of a capital tier."""
    name: str
    max_capital_eur: float
    min_stability_days: int  # days in this tier before upgrade
    max_drawdown_pct: float
    max_daily_loss_pct: float
    max_position_size_pct: float  # % of capital per trade


# ─── Tier ladder ──────────────────────────────────────────────

TIER_LADDER: List[CapitalTierDef] = [
    CapitalTierDef("T0_PAPER",   0,       0,   99.0,  99.0,  100.0),
    CapitalTierDef("T1_1K",      1_000,   7,   5.0,   2.0,   10.0),
    CapitalTierDef("T2_5K",      5_000,   14,  5.0,   2.0,   8.0),
    CapitalTierDef("T3_10K",     10_000,  21,  4.0,   1.5,   5.0),
    CapitalTierDef("T4_25K",     25_000,  30,  3.0,   1.0,   4.0),
    CapitalTierDef("T5_50K",     50_000,  60,  2.5,   0.8,   3.0),
    CapitalTierDef("T6_100K",    100_000, 90,  2.0,   0.5,   2.0),
]


@dataclass
class ScaleEvent:
    """Record of a scale change."""
    timestamp: float
    direction: str  # "up", "down", "emergency"
    from_tier: int
    to_tier: int
    reason: str
    capital_eur: float


@dataclass
class ScalerState:
    """Persisted scaler state."""
    current_tier_idx: int = 0
    entered_at: float = 0.0
    peak_capital_eur: float = 0.0
    last_scale_event: float = 0.0
    critical_incidents_in_window: int = 0
    window_start: float = 0.0
    scale_history: list = field(default_factory=list)


class CapitalScaler:
    """
    Progressive capital scaler with automatic rollback.

    Scale-up: when stability is proven over required period.
    Rollback: immediate, to the previous tier, on any breach.
    """

    def __init__(self, persistence_path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._path = persistence_path or _PERSISTENCE_PATH
        self._state = self._load()

    def _load(self) -> ScalerState:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    data = json.load(f)
                return ScalerState(**data)
            except Exception as exc:
                logger.error("[SCALER] Corrupt state, resetting: %s", exc)
        return ScalerState(entered_at=time.time(), window_start=time.time())

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(asdict(self._state), f, indent=2)
        except Exception as exc:
            logger.error("[SCALER] Persist failed: %s", exc)

    # ── Public API ─────────────────────────────────────────────

    @property
    def current_tier(self) -> CapitalTierDef:
        with self._lock:
            idx = min(self._state.current_tier_idx, len(TIER_LADDER) - 1)
            return TIER_LADDER[idx]

    @property
    def tier_index(self) -> int:
        with self._lock:
            return self._state.current_tier_idx

    @property
    def max_capital(self) -> float:
        return self.current_tier.max_capital_eur

    @property
    def time_in_tier(self) -> float:
        with self._lock:
            return time.time() - self._state.entered_at

    @property
    def days_in_tier(self) -> float:
        return self.time_in_tier / 86400

    def record_incident(self) -> None:
        """Record a critical incident — may trigger rollback."""
        with self._lock:
            self._state.critical_incidents_in_window += 1
            logger.warning(
                "[SCALER] Critical incident #%d in window",
                self._state.critical_incidents_in_window,
            )

    def reset_incident_window(self) -> None:
        """Reset the incident counter (e.g., on new window)."""
        with self._lock:
            self._state.critical_incidents_in_window = 0
            self._state.window_start = time.time()
            self._save()

    def can_scale_up(self) -> Tuple[bool, List[str]]:
        """Check if conditions for scale-up are met."""
        with self._lock:
            idx = self._state.current_tier_idx
            if idx >= len(TIER_LADDER) - 1:
                return False, ["Already at maximum tier"]

            tier = TIER_LADDER[idx]
            blockers = []

            days = (time.time() - self._state.entered_at) / 86400
            if days < tier.min_stability_days:
                blockers.append(
                    f"Need {tier.min_stability_days}d stability, only {days:.1f}d"
                )

            if self._state.critical_incidents_in_window > 0:
                blockers.append(
                    f"{self._state.critical_incidents_in_window} critical incidents in window"
                )

            return len(blockers) == 0, blockers

    def scale_up(self, reason: str = "stability_proven") -> Tuple[bool, str]:
        """Attempt to scale up to next tier."""
        allowed, blockers = self.can_scale_up()
        if not allowed:
            return False, f"Scale-up blocked: {'; '.join(blockers)}"

        with self._lock:
            old_idx = self._state.current_tier_idx
            new_idx = old_idx + 1
            new_tier = TIER_LADDER[new_idx]

            event = ScaleEvent(
                timestamp=time.time(),
                direction="up",
                from_tier=old_idx,
                to_tier=new_idx,
                reason=reason,
                capital_eur=new_tier.max_capital_eur,
            )
            self._state.scale_history.append(asdict(event))
            self._state.current_tier_idx = new_idx
            self._state.entered_at = time.time()
            self._state.critical_incidents_in_window = 0
            self._state.window_start = time.time()
            self._save()

        logger.warning(
            "[SCALER] SCALE UP: %s → %s (max %s€)",
            TIER_LADDER[old_idx].name, new_tier.name, new_tier.max_capital_eur,
        )
        return True, f"Scaled to {new_tier.name} ({new_tier.max_capital_eur}€)"

    def rollback(self, reason: str = "incident") -> Tuple[bool, str]:
        """
        Rollback to previous tier.
        Always succeeds unless already at tier 0.
        """
        with self._lock:
            old_idx = self._state.current_tier_idx
            if old_idx <= 0:
                return False, "Already at minimum tier"

            new_idx = old_idx - 1
            new_tier = TIER_LADDER[new_idx]

            event = ScaleEvent(
                timestamp=time.time(),
                direction="down",
                from_tier=old_idx,
                to_tier=new_idx,
                reason=reason,
                capital_eur=new_tier.max_capital_eur,
            )
            self._state.scale_history.append(asdict(event))
            self._state.current_tier_idx = new_idx
            self._state.entered_at = time.time()
            self._state.critical_incidents_in_window = 0
            self._state.window_start = time.time()
            self._save()

        logger.warning(
            "[SCALER] ROLLBACK: %s → %s (reason: %s)",
            TIER_LADDER[old_idx].name, new_tier.name, reason,
        )
        return True, f"Rolled back to {new_tier.name} ({new_tier.max_capital_eur}€)"

    def emergency_floor(self, reason: str) -> None:
        """Emergency: drop to tier 0 (paper)."""
        with self._lock:
            old_idx = self._state.current_tier_idx
            event = ScaleEvent(
                timestamp=time.time(),
                direction="emergency",
                from_tier=old_idx,
                to_tier=0,
                reason=reason,
                capital_eur=0,
            )
            self._state.scale_history.append(asdict(event))
            self._state.current_tier_idx = 0
            self._state.entered_at = time.time()
            self._state.critical_incidents_in_window = 0
            self._save()
        logger.critical("[SCALER] EMERGENCY FLOOR: tier %d → 0 (reason: %s)", old_idx, reason)

    def check_position_size(self, size_eur: float) -> Tuple[bool, Optional[str]]:
        """Check if a position size is within tier limits."""
        tier = self.current_tier
        max_size = tier.max_capital_eur * (tier.max_position_size_pct / 100)
        if max_size > 0 and size_eur > max_size:
            return False, f"Size {size_eur:.0f}€ > tier limit {max_size:.0f}€"
        return True, None

    def to_dict(self) -> dict:
        tier = self.current_tier
        return {
            "tier_index": self._state.current_tier_idx,
            "tier_name": tier.name,
            "max_capital_eur": tier.max_capital_eur,
            "max_drawdown_pct": tier.max_drawdown_pct,
            "max_daily_loss_pct": tier.max_daily_loss_pct,
            "days_in_tier": self.days_in_tier,
            "required_days": tier.min_stability_days,
            "incidents_in_window": self._state.critical_incidents_in_window,
            "scale_history_count": len(self._state.scale_history),
        }


# ─── Singleton ────────────────────────────────────────────────

_scaler: Optional[CapitalScaler] = None
_singleton_lock = threading.Lock()


def get_capital_scaler(persistence_path: Optional[str] = None) -> CapitalScaler:
    global _scaler
    if _scaler is None:
        with _singleton_lock:
            if _scaler is None:
                _scaler = CapitalScaler(persistence_path)
    return _scaler


def reset_capital_scaler() -> None:
    """Reset singleton (tests only)."""
    global _scaler
    _scaler = None
