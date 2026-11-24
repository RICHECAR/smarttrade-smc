"""
Candidate Deployment Manager
═════════════════════════════

Manages adaptation lifecycle through progressive stages:

  CANDIDATE → SHADOW → PAPER → LIMITED_LIVE → ACTIVE
                                           ↘ ROLLED_BACK

Promotion only after evidence thresholds are met at each stage.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Dict, List, Optional

from backend.services.trading.governance_models import (
    ABComparisonResult,
    CandidateRecord,
    DeploymentStage,
    StageRequirement,
    ValidationResult,
    ValidationVerdict,
)

logger = logging.getLogger(__name__)

# Ordered promotion path
_PROMOTION_ORDER: List[DeploymentStage] = [
    DeploymentStage.CANDIDATE,
    DeploymentStage.SHADOW,
    DeploymentStage.PAPER,
    DeploymentStage.LIMITED_LIVE,
    DeploymentStage.ACTIVE,
]

# Default evidence requirements per stage transition
_DEFAULT_REQUIREMENTS: Dict[DeploymentStage, StageRequirement] = {
    DeploymentStage.SHADOW: StageRequirement(
        min_trades=0, min_win_rate=0.0, min_expectancy=0.0,
        min_sharpe=0.0, max_fragility=1.0, max_drawdown=1.0,
        min_ab_confidence=0.0,
    ),
    DeploymentStage.PAPER: StageRequirement(
        min_trades=10, min_win_rate=0.40, min_expectancy=0.0,
        min_sharpe=0.0, max_fragility=0.70, max_drawdown=0.20,
        min_ab_confidence=0.0,
    ),
    DeploymentStage.LIMITED_LIVE: StageRequirement(
        min_trades=30, min_win_rate=0.45, min_expectancy=0.5,
        min_sharpe=0.3, max_fragility=0.50, max_drawdown=0.15,
        min_ab_confidence=0.60,
    ),
    DeploymentStage.ACTIVE: StageRequirement(
        min_trades=60, min_win_rate=0.48, min_expectancy=1.0,
        min_sharpe=0.5, max_fragility=0.40, max_drawdown=0.12,
        min_ab_confidence=0.75,
    ),
}


class CandidateDeploymentManager:
    """
    Tracks all adaptation candidates and controls their progressive
    promotion through deployment stages.
    """

    def __init__(
        self,
        requirements: Optional[Dict[DeploymentStage, StageRequirement]] = None,
    ) -> None:
        self._candidates: Dict[str, CandidateRecord] = {}
        self._requirements = requirements or dict(_DEFAULT_REQUIREMENTS)

    # ── Public API ───────────────────────────────────────────────

    def register_candidate(
        self,
        adaptation_id: str,
        description: str = "",
        validation: Optional[ValidationResult] = None,
    ) -> CandidateRecord:
        """Register a new adaptation candidate."""
        cid = self._make_id(adaptation_id)
        now = time.time()
        record = CandidateRecord(
            candidate_id=cid,
            adaptation_id=adaptation_id,
            description=description,
            stage=DeploymentStage.CANDIDATE,
            stage_history=[{"stage": DeploymentStage.CANDIDATE.value, "ts": now}],
            validation_result=validation,
            created_at=now,
            stage_entered_at=now,
        )
        self._candidates[cid] = record
        logger.info("Registered candidate %s (%s)", cid, adaptation_id)
        return record

    def try_promote(
        self,
        candidate_id: str,
        *,
        trades: int = 0,
        win_rate: float = 0.0,
        expectancy: float = 0.0,
        sharpe: float = 0.0,
        fragility: float = 0.0,
        drawdown: float = 0.0,
        ab_confidence: float = 0.0,
    ) -> CandidateRecord:
        """
        Attempt to promote a candidate to the next stage.
        Returns the (possibly updated) record.
        """
        record = self._candidates.get(candidate_id)
        if record is None:
            raise KeyError(f"Unknown candidate: {candidate_id}")

        if record.stage == DeploymentStage.ACTIVE:
            logger.debug("Candidate %s already ACTIVE", candidate_id)
            return record

        if record.stage == DeploymentStage.ROLLED_BACK:
            logger.warning("Cannot promote rolled-back candidate %s", candidate_id)
            return record

        # Validation must APPROVE before leaving CANDIDATE
        if record.stage == DeploymentStage.CANDIDATE:
            if record.validation_result is None or record.validation_result.verdict != ValidationVerdict.APPROVE:
                logger.info("Candidate %s not yet validated/approved", candidate_id)
                return record

        next_stage = self._next_stage(record.stage)
        if next_stage is None:
            return record

        requirement = self._requirements.get(next_stage)
        if requirement is None:
            return record

        # Check evidence against threshold
        if not self._meets_requirements(
            requirement, trades=trades, win_rate=win_rate,
            expectancy=expectancy, sharpe=sharpe,
            fragility=fragility, drawdown=drawdown,
            ab_confidence=ab_confidence,
        ):
            logger.info(
                "Candidate %s does not meet %s requirements",
                candidate_id, next_stage.value,
            )
            record.trades_in_stage = trades
            return record

        # Promote
        now = time.time()
        record.stage_history.append({
            "stage": next_stage.value,
            "ts": now,
            "evidence": {
                "trades": trades, "win_rate": round(win_rate, 4),
                "expectancy": round(expectancy, 4), "sharpe": round(sharpe, 4),
                "fragility": round(fragility, 4), "drawdown": round(drawdown, 4),
                "ab_confidence": round(ab_confidence, 4),
            },
        })
        record.stage = next_stage
        record.stage_entered_at = now
        record.trades_in_stage = 0
        logger.info("Promoted candidate %s → %s", candidate_id, next_stage.value)
        return record

    def rollback(self, candidate_id: str, reason: str = "") -> CandidateRecord:
        """Force a candidate to ROLLED_BACK state."""
        record = self._candidates.get(candidate_id)
        if record is None:
            raise KeyError(f"Unknown candidate: {candidate_id}")
        now = time.time()
        record.stage_history.append({
            "stage": DeploymentStage.ROLLED_BACK.value,
            "ts": now,
            "reason": reason,
        })
        record.stage = DeploymentStage.ROLLED_BACK
        record.stage_entered_at = now
        logger.warning("Rolled back candidate %s: %s", candidate_id, reason)
        return record

    def attach_ab_result(
        self, candidate_id: str, ab_result: ABComparisonResult,
    ) -> None:
        record = self._candidates.get(candidate_id)
        if record:
            record.ab_result = ab_result

    def get_candidate(self, candidate_id: str) -> Optional[CandidateRecord]:
        return self._candidates.get(candidate_id)

    def get_all_candidates(self) -> List[CandidateRecord]:
        return list(self._candidates.values())

    def get_candidates_by_stage(self, stage: DeploymentStage) -> List[CandidateRecord]:
        return [c for c in self._candidates.values() if c.stage == stage]

    def get_stage_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {s.value: 0 for s in DeploymentStage}
        for c in self._candidates.values():
            counts[c.stage.value] += 1
        return counts

    # ── Private helpers ──────────────────────────────────────────

    @staticmethod
    def _next_stage(current: DeploymentStage) -> Optional[DeploymentStage]:
        try:
            idx = _PROMOTION_ORDER.index(current)
            if idx + 1 < len(_PROMOTION_ORDER):
                return _PROMOTION_ORDER[idx + 1]
        except ValueError:
            pass
        return None

    @staticmethod
    def _meets_requirements(
        req: StageRequirement, *, trades: int, win_rate: float,
        expectancy: float, sharpe: float, fragility: float,
        drawdown: float, ab_confidence: float,
    ) -> bool:
        return (
            trades >= req.min_trades
            and win_rate >= req.min_win_rate
            and expectancy >= req.min_expectancy
            and sharpe >= req.min_sharpe
            and fragility <= req.max_fragility
            and drawdown <= req.max_drawdown
            and ab_confidence >= req.min_ab_confidence
        )

    @staticmethod
    def _make_id(adaptation_id: str) -> str:
        h = hashlib.sha256(f"{adaptation_id}:{time.time()}".encode()).hexdigest()[:12]
        return f"cand_{h}"
