"""
Policy Decay Guard — Gradual Tightening of Degrading Setups
═══════════════════════════════════════════════════════════

Monitors setups in DEGRADED or near-degraded states and progressively
tightens their trading parameters:

  Level 0 (healthy):  no overrides
  Level 1 (warning):  size_mult ×0.7, confidence_threshold +0.05
  Level 2 (critical): size_mult ×0.4, confidence_threshold +0.10, max_concurrent −1
  Level 3 (blocked):  hard block — zero allocation

Decay severity is computed from:
  - Lifecycle state (DEGRADED adds +1 level)
  - Survival score (below 0.40 adds +1 level)
  - Consecutive losing trades (>5 adds +1 level)

The guard produces a DecayReport that downstream systems use
to override AdaptationParams or veto trade entry.

Config via .env:
  DECAY_L1_SIZE_MULT=0.7
  DECAY_L1_CONF_ADD=0.05
  DECAY_L2_SIZE_MULT=0.4
  DECAY_L2_CONF_ADD=0.10
  DECAY_L2_CONC_REDUCE=1
  DECAY_SCORE_THRESHOLD=0.40
  DECAY_LOSING_STREAK=5
"""

import logging
import os
from typing import Any, Dict

from backend.services.trading.darwinian_models import (
    DecayReport,
    SetupProfile,
    SetupState,
    SurvivalVerdict,
)

logger = logging.getLogger("smarttrade.darwinian.decay_guard")


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


class PolicyDecayGuard:
    """
    Computes decay-driven overrides for setups showing weakness.
    """

    def __init__(self) -> None:
        self._l1_size = _envf("DECAY_L1_SIZE_MULT", 0.7)
        self._l1_conf = _envf("DECAY_L1_CONF_ADD", 0.05)
        self._l2_size = _envf("DECAY_L2_SIZE_MULT", 0.4)
        self._l2_conf = _envf("DECAY_L2_CONF_ADD", 0.10)
        self._l2_conc = _envi("DECAY_L2_CONC_REDUCE", 1)
        self._score_thr = _envf("DECAY_SCORE_THRESHOLD", 0.40)
        self._losing_streak = _envi("DECAY_LOSING_STREAK", 5)

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def evaluate(
        self,
        profile: SetupProfile,
        verdict: SurvivalVerdict,
    ) -> DecayReport:
        """
        Assess decay severity and produce override recommendations.

        Args:
            profile: Current lifecycle profile
            verdict: Latest survival scoring verdict
        """
        severity = self._compute_severity(profile, verdict)

        if severity >= 3:
            return DecayReport(
                decay_severity=severity,
                override_size_mult=0.0,
                override_conf_threshold=1.0,  # impossible threshold → no trade
                override_max_concurrent=0,
                hard_block=True,
                reason=self._build_reason(profile, verdict, severity),
            )

        if severity == 2:
            return DecayReport(
                decay_severity=severity,
                override_size_mult=self._l2_size,
                override_conf_threshold=self._l2_conf,
                override_max_concurrent=-self._l2_conc,  # delta, not absolute
                hard_block=False,
                reason=self._build_reason(profile, verdict, severity),
            )

        if severity == 1:
            return DecayReport(
                decay_severity=severity,
                override_size_mult=self._l1_size,
                override_conf_threshold=self._l1_conf,
                override_max_concurrent=0,
                hard_block=False,
                reason=self._build_reason(profile, verdict, severity),
            )

        # severity 0 — no decay
        return DecayReport(
            decay_severity=0,
            override_size_mult=1.0,
            override_conf_threshold=0.0,
            override_max_concurrent=0,
            hard_block=False,
            reason="healthy",
        )

    def should_block(self, profile: SetupProfile, verdict: SurvivalVerdict) -> bool:
        """Quick check: should this setup be hard-blocked?"""
        return self.evaluate(profile, verdict).hard_block

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    def _compute_severity(self, profile: SetupProfile, verdict: SurvivalVerdict) -> int:
        """Sum up decay signals into a severity level 0–3."""
        severity = 0

        # Signal 1: lifecycle state
        if profile.state == SetupState.BLOCKED:
            severity += 2
        elif profile.state == SetupState.DEGRADED:
            severity += 1

        # Signal 2: survival score
        if verdict.score < self._score_thr:
            severity += 1

        # Signal 3: consecutive losses (approximate from profile)
        if profile.losses > 0 and profile.total_trades > 0:
            recent_loss_rate = profile.losses / profile.total_trades
            if recent_loss_rate > 0.7 and profile.total_trades >= self._losing_streak:
                severity += 1

        return min(3, severity)

    def _build_reason(
        self, profile: SetupProfile, verdict: SurvivalVerdict, severity: int
    ) -> str:
        parts = [f"severity={severity}"]
        parts.append(f"state={profile.state.value}")
        parts.append(f"score={verdict.score:.3f}")
        parts.append(f"wr={profile.win_rate:.2f}")
        parts.append(f"trades={profile.total_trades}")
        return ", ".join(parts)
