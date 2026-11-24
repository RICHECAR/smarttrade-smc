"""
Rollback Governor
═════════════════

Instantly reverts unsafe adaptations when:

  • Live performance degradation appears
  • Execution fragility rises above threshold
  • Structural mismatch emerges (regime shift)
  • Portfolio damage exceeds acceptable level

Works with CandidateDeploymentManager to move candidates to ROLLED_BACK.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Dict, List, Optional

from backend.services.trading.governance_models import (
    DeploymentStage,
    RollbackEvent,
    RollbackTrigger,
)

logger = logging.getLogger(__name__)


class RollbackGovernor:
    """
    Monitors live candidate performance and triggers instant rollback
    when safety boundaries are breached.
    """

    # ── Configurable thresholds ──────────────────────────────────
    MAX_LIVE_DRAWDOWN: float = 0.12
    MAX_EXECUTION_FRAGILITY: float = 0.60
    MAX_REGIME_MISMATCH: float = 0.70
    MAX_PORTFOLIO_DAMAGE: float = 0.10
    # Consecutive checks before rollback (debounce)
    DEGRADATION_STREAK_THRESHOLD: int = 3
    # Cooldown after rollback before same adaptation can retry
    ROLLBACK_COOLDOWN_SECONDS: float = 86400.0  # 24h

    def __init__(self, **overrides: Any) -> None:
        for key, value in overrides.items():
            if hasattr(self, key.upper()):
                setattr(self, key.upper(), value)
        self._degradation_streaks: Dict[str, int] = {}
        self._rollback_history: List[RollbackEvent] = []
        self._cooldowns: Dict[str, float] = {}  # candidate_id → cooldown_until

    # ── Public API ───────────────────────────────────────────────

    def evaluate(
        self,
        candidate_id: str,
        current_stage: DeploymentStage,
        *,
        live_drawdown: float = 0.0,
        execution_fragility: float = 0.0,
        regime_mismatch: float = 0.0,
        portfolio_damage: float = 0.0,
    ) -> Optional[RollbackEvent]:
        """
        Evaluate a candidate's live metrics and return a RollbackEvent
        if a rollback is triggered, or None if safe.
        """
        if current_stage in (DeploymentStage.CANDIDATE, DeploymentStage.ROLLED_BACK):
            return None

        triggers: List[RollbackTrigger] = []
        metrics: Dict[str, float] = {
            "live_drawdown": live_drawdown,
            "execution_fragility": execution_fragility,
            "regime_mismatch": regime_mismatch,
            "portfolio_damage": portfolio_damage,
        }

        if live_drawdown > self.MAX_LIVE_DRAWDOWN:
            triggers.append(RollbackTrigger.LIVE_DEGRADATION)
        if execution_fragility > self.MAX_EXECUTION_FRAGILITY:
            triggers.append(RollbackTrigger.EXECUTION_FRAGILITY)
        if regime_mismatch > self.MAX_REGIME_MISMATCH:
            triggers.append(RollbackTrigger.STRUCTURAL_MISMATCH)
        if portfolio_damage > self.MAX_PORTFOLIO_DAMAGE:
            triggers.append(RollbackTrigger.PORTFOLIO_DAMAGE)

        if not triggers:
            # Reset streak if clean
            self._degradation_streaks.pop(candidate_id, None)
            return None

        # Increment streak
        streak = self._degradation_streaks.get(candidate_id, 0) + 1
        self._degradation_streaks[candidate_id] = streak

        # Immediate rollback for severe breaches (2x threshold)
        severe = (
            live_drawdown > self.MAX_LIVE_DRAWDOWN * 2
            or portfolio_damage > self.MAX_PORTFOLIO_DAMAGE * 2
        )

        if not severe and streak < self.DEGRADATION_STREAK_THRESHOLD:
            logger.debug(
                "Candidate %s: degradation streak %d/%d",
                candidate_id, streak, self.DEGRADATION_STREAK_THRESHOLD,
            )
            return None

        # Trigger rollback
        severity = self._compute_severity(metrics)
        primary_trigger = triggers[0]  # Most critical

        event = RollbackEvent(
            rollback_id=self._make_id(candidate_id),
            candidate_id=candidate_id,
            trigger=primary_trigger,
            previous_stage=current_stage,
            severity=severity,
            metrics_at_rollback=metrics,
            reason=f"triggers={[t.value for t in triggers]}, streak={streak}, severe={severe}",
            automatic=True,
        )

        self._rollback_history.append(event)
        self._degradation_streaks.pop(candidate_id, None)
        self._cooldowns[candidate_id] = time.time() + self.ROLLBACK_COOLDOWN_SECONDS

        logger.warning(
            "ROLLBACK %s: %s (severity=%.2f, stage=%s)",
            candidate_id, primary_trigger.value, severity, current_stage.value,
        )
        return event

    def manual_rollback(
        self, candidate_id: str, current_stage: DeploymentStage, reason: str = "",
    ) -> RollbackEvent:
        """Force a manual rollback."""
        event = RollbackEvent(
            rollback_id=self._make_id(candidate_id),
            candidate_id=candidate_id,
            trigger=RollbackTrigger.MANUAL,
            previous_stage=current_stage,
            severity=1.0,
            reason=reason or "manual_rollback",
            automatic=False,
        )
        self._rollback_history.append(event)
        self._cooldowns[candidate_id] = time.time() + self.ROLLBACK_COOLDOWN_SECONDS
        logger.warning("Manual rollback %s: %s", candidate_id, reason)
        return event

    def is_in_cooldown(self, candidate_id: str) -> bool:
        """Check if a rolled-back candidate is still in cooldown."""
        until = self._cooldowns.get(candidate_id, 0.0)
        return time.time() < until

    def get_history(self, last_n: int = 50) -> List[RollbackEvent]:
        return self._rollback_history[-last_n:]

    def get_recent_rollback_count(self, window_seconds: float = 3600.0) -> int:
        cutoff = time.time() - window_seconds
        return sum(1 for e in self._rollback_history if e.timestamp >= cutoff)

    # ── Private helpers ──────────────────────────────────────────

    def _compute_severity(self, metrics: Dict[str, float]) -> float:
        """0-1 severity score from breach magnitude."""
        scores = []
        if metrics.get("live_drawdown", 0) > 0:
            scores.append(min(metrics["live_drawdown"] / max(self.MAX_LIVE_DRAWDOWN, 1e-9), 2.0))
        if metrics.get("execution_fragility", 0) > 0:
            scores.append(min(metrics["execution_fragility"] / max(self.MAX_EXECUTION_FRAGILITY, 1e-9), 2.0))
        if metrics.get("regime_mismatch", 0) > 0:
            scores.append(min(metrics["regime_mismatch"] / max(self.MAX_REGIME_MISMATCH, 1e-9), 2.0))
        if metrics.get("portfolio_damage", 0) > 0:
            scores.append(min(metrics["portfolio_damage"] / max(self.MAX_PORTFOLIO_DAMAGE, 1e-9), 2.0))
        if not scores:
            return 0.0
        return min(sum(scores) / len(scores), 1.0)

    @staticmethod
    def _make_id(candidate_id: str) -> str:
        h = hashlib.sha256(f"rb:{candidate_id}:{time.time()}".encode()).hexdigest()[:12]
        return f"rb_{h}"
