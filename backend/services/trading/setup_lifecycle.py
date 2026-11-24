"""
Setup Lifecycle Engine — State Machine for Setup Evolution
══════════════════════════════════════════════════════════

Tracks each setup type through a lifecycle:

    EMERGING → ACTIVE → DEGRADED → BLOCKED
                  ↑         │
                  └─── rehabilitation ─┘

Transitions are driven by rolling-window statistics from trade outcomes.

Promotion rules:
  - EMERGING → ACTIVE : win_rate ≥ threshold AND trades ≥ min_sample
  - ACTIVE  → DEGRADED: rolling win_rate drops below threshold OR
                         profit_factor < 1.0
  - DEGRADED → BLOCKED: stays degraded for N consecutive evaluation cycles
  - DEGRADED → ACTIVE : recovery detected (win_rate rebounds)
  - BLOCKED  → EMERGING: manual reset OR cooling period elapsed

Config via .env:
  LIFECYCLE_ROLLING_WINDOW=30
  LIFECYCLE_MIN_SAMPLE=5
  LIFECYCLE_PROMOTE_WR=0.50
  LIFECYCLE_PROMOTE_PF=1.2
  LIFECYCLE_DEGRADE_WR=0.40
  LIFECYCLE_DEGRADE_PF=0.9
  LIFECYCLE_BLOCK_AFTER_BARS=10
  LIFECYCLE_COOLDOWN_HOURS=24
"""

import logging
import os
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from backend.services.trading.darwinian_models import (
    SetupKey,
    SetupProfile,
    SetupState,
)

logger = logging.getLogger("smarttrade.darwinian.lifecycle")


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class SetupLifecycleEngine:
    """
    State machine tracking each setup through its lifecycle.

    Usage:
        engine = SetupLifecycleEngine()
        engine.record_outcome(key, pnl=50.0, regime="trending_up")
        profile = engine.get_profile(key)
        # profile.state → SetupState.ACTIVE / DEGRADED / etc.
    """

    def __init__(self) -> None:
        self._rolling_window = _envi("LIFECYCLE_ROLLING_WINDOW", 30)
        self._min_sample = _envi("LIFECYCLE_MIN_SAMPLE", 5)
        self._promote_wr = _envf("LIFECYCLE_PROMOTE_WR", 0.50)
        self._promote_pf = _envf("LIFECYCLE_PROMOTE_PF", 1.2)
        self._degrade_wr = _envf("LIFECYCLE_DEGRADE_WR", 0.40)
        self._degrade_pf = _envf("LIFECYCLE_DEGRADE_PF", 0.9)
        self._block_after_bars = _envi("LIFECYCLE_BLOCK_AFTER_BARS", 10)
        self._cooldown_hours = _envf("LIFECYCLE_COOLDOWN_HOURS", 24.0)

        # Per-setup state
        self._profiles: Dict[str, SetupProfile] = {}
        self._outcomes: Dict[str, Deque[float]] = {}  # rolling pnl window

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def record_outcome(
        self,
        key: SetupKey,
        pnl: float,
        regime: str = "unknown",
    ) -> SetupProfile:
        """
        Record a trade outcome for a setup and re-evaluate its lifecycle state.
        Returns the updated SetupProfile.
        """
        sk = str(key)
        profile = self._ensure_profile(key)
        outcomes = self._outcomes.setdefault(sk, deque(maxlen=self._rolling_window))

        # Update stats
        outcomes.append(pnl)
        profile.total_trades += 1
        profile.last_trade_at = time.time()

        if pnl > 0:
            profile.wins += 1
            profile.gross_profit += pnl
        else:
            profile.losses += 1
            profile.gross_loss += abs(pnl)

        profile.net_pnl += pnl
        self._recompute_stats(profile, outcomes)

        # Evaluate state transition
        self._evaluate_transition(profile, outcomes)

        return profile

    def get_profile(self, key: SetupKey) -> Optional[SetupProfile]:
        """Get the profile for a setup, or None if never seen."""
        return self._profiles.get(str(key))

    def get_all_profiles(self) -> Dict[str, SetupProfile]:
        return dict(self._profiles)

    def get_state(self, key: SetupKey) -> SetupState:
        """Quick state lookup."""
        profile = self._profiles.get(str(key))
        return profile.state if profile else SetupState.EMERGING

    def is_allowed(self, key: SetupKey) -> bool:
        """Can this setup trade right now?"""
        state = self.get_state(key)
        return state in (SetupState.EMERGING, SetupState.ACTIVE)

    def reset_setup(self, key: SetupKey) -> None:
        """Manual reset — move a blocked setup back to EMERGING."""
        sk = str(key)
        profile = self._profiles.get(sk)
        if profile:
            self._transition(profile, SetupState.EMERGING, "manual_reset")

    def summary(self) -> Dict[str, Any]:
        """Overview of all setups and their states."""
        by_state: Dict[str, int] = {}
        for p in self._profiles.values():
            by_state[p.state.value] = by_state.get(p.state.value, 0) + 1

        return {
            "total_setups": len(self._profiles),
            "by_state": by_state,
            "profiles": {k: v.to_dict() for k, v in self._profiles.items()},
        }

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    def _ensure_profile(self, key: SetupKey) -> SetupProfile:
        sk = str(key)
        if sk not in self._profiles:
            self._profiles[sk] = SetupProfile(key=key, state=SetupState.EMERGING)
        return self._profiles[sk]

    def _recompute_stats(self, profile: SetupProfile, outcomes: Deque[float]) -> None:
        """Recompute rolling stats from the window."""
        window = list(outcomes)
        n = len(window)
        if n == 0:
            return

        wins = sum(1 for p in window if p > 0)
        losses = n - wins
        profile.win_rate = wins / n

        gp = sum(p for p in window if p > 0)
        gl = abs(sum(p for p in window if p <= 0))
        profile.profit_factor = gp / gl if gl > 0 else (gp if gp > 0 else 0.0)
        profile.expectancy = sum(window) / n

        # Drawdown contribution: ratio of losing trades' loss to total portfolio loss
        if gl > 0:
            profile.drawdown_contribution = gl / max(gl, gp)
        else:
            profile.drawdown_contribution = 0.0

    def _evaluate_transition(self, profile: SetupProfile, outcomes: Deque[float]) -> None:
        """Check if a state transition is warranted."""
        n = len(outcomes)
        state = profile.state

        if state == SetupState.EMERGING:
            # Need enough data to promote
            if n >= self._min_sample:
                if (
                    profile.win_rate >= self._promote_wr
                    and profile.profit_factor >= self._promote_pf
                ):
                    self._transition(profile, SetupState.ACTIVE, "promoted: stats qualify")
                elif (
                    profile.win_rate < self._degrade_wr
                    or profile.profit_factor < self._degrade_pf
                ):
                    self._transition(profile, SetupState.DEGRADED, "poor initial performance")

        elif state == SetupState.ACTIVE:
            if n >= self._min_sample:
                if (
                    profile.win_rate < self._degrade_wr
                    or profile.profit_factor < self._degrade_pf
                ):
                    self._transition(profile, SetupState.DEGRADED, "performance degradation")

        elif state == SetupState.DEGRADED:
            profile.consecutive_state_bars += 1

            # Recovery check
            if n >= self._min_sample and profile.win_rate >= self._promote_wr and profile.profit_factor >= self._promote_pf:
                self._transition(profile, SetupState.ACTIVE, "recovered")
            elif profile.consecutive_state_bars >= self._block_after_bars:
                self._transition(profile, SetupState.BLOCKED, f"degraded for {profile.consecutive_state_bars} cycles")

        elif state == SetupState.BLOCKED:
            # Auto-unblock after cooldown
            elapsed_h = (time.time() - profile.last_transition_at) / 3600
            if elapsed_h >= self._cooldown_hours:
                self._transition(profile, SetupState.EMERGING, f"cooldown elapsed ({elapsed_h:.0f}h)")

    def _transition(self, profile: SetupProfile, new_state: SetupState, reason: str) -> None:
        """Execute a state transition with audit trail."""
        old_state = profile.state
        profile.state_history.append({
            "from": old_state.value,
            "to": new_state.value,
            "reason": reason,
            "timestamp": time.time(),
        })
        # Keep history bounded
        if len(profile.state_history) > 50:
            profile.state_history = profile.state_history[-50:]

        profile.state = new_state
        profile.last_transition_at = time.time()
        profile.consecutive_state_bars = 0

        logger.info(
            "[LIFECYCLE] %s: %s → %s (%s)",
            str(profile.key), old_state.value, new_state.value, reason,
        )
