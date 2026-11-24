"""
Audit Trail Governance Store — JSONL Persistence
═════════════════════════════════════════════════

Append-only JSONL storage for all governance events:

  • validations.jsonl              — proposed changes + approvals/rejections
  • deployment_transitions.jsonl   — stage promotions & demotions
  • ab_comparisons.jsonl           — A/B test results
  • rollbacks.jsonl                — rollback events
  • capital_scaling.jsonl          — capital tier changes
  • learning_pace.jsonl            — pace assessments
  • governance_events.jsonl        — unified audit log

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.services.trading.governance_models import (
    ABComparisonResult,
    CapitalAllocation,
    CandidateRecord,
    GovernanceEvent,
    GovernanceEventType,
    PaceAssessment,
    RollbackEvent,
    ScalingDecision,
    ValidationResult,
)

_BASE_DIR = Path("data/governance")


class GovernanceStore:
    """Append-only JSONL persistence for the governance audit trail."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._dir = Path(base_dir) if base_dir else _BASE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────
    #  Write methods (typed)
    # ──────────────────────────────────────────

    def log_validation(self, result: ValidationResult) -> None:
        self._append("validations.jsonl", result.to_dict())
        self._log_event(GovernanceEventType.VALIDATION, result.adaptation_id, result.to_dict())

    def log_deployment_transition(self, record: CandidateRecord, reason: str = "") -> None:
        payload = record.to_dict()
        payload["transition_reason"] = reason
        self._append("deployment_transitions.jsonl", payload)
        self._log_event(GovernanceEventType.DEPLOYMENT_TRANSITION, record.candidate_id, payload)

    def log_ab_comparison(self, result: ABComparisonResult) -> None:
        self._append("ab_comparisons.jsonl", result.to_dict())
        self._log_event(GovernanceEventType.AB_COMPARISON, result.candidate_id, result.to_dict())

    def log_rollback(self, event: RollbackEvent) -> None:
        self._append("rollbacks.jsonl", event.to_dict())
        self._log_event(GovernanceEventType.ROLLBACK, event.candidate_id, event.to_dict())

    def log_capital_scaling(self, decision: ScalingDecision) -> None:
        self._append("capital_scaling.jsonl", decision.to_dict())
        self._log_event(GovernanceEventType.CAPITAL_SCALING, decision.candidate_id, decision.to_dict())

    def log_learning_pace(self, assessment: PaceAssessment) -> None:
        self._append("learning_pace.jsonl", assessment.to_dict())
        self._log_event(GovernanceEventType.LEARNING_PACE_CHANGE, "", assessment.to_dict())

    # ──────────────────────────────────────────
    #  Read methods
    # ──────────────────────────────────────────

    def read_validations(self, last_n: int = 50) -> List[Dict[str, Any]]:
        return self._read_last("validations.jsonl", last_n)

    def read_deployment_transitions(self, last_n: int = 50) -> List[Dict[str, Any]]:
        return self._read_last("deployment_transitions.jsonl", last_n)

    def read_ab_comparisons(self, last_n: int = 50) -> List[Dict[str, Any]]:
        return self._read_last("ab_comparisons.jsonl", last_n)

    def read_rollbacks(self, last_n: int = 50) -> List[Dict[str, Any]]:
        return self._read_last("rollbacks.jsonl", last_n)

    def read_capital_scaling(self, last_n: int = 50) -> List[Dict[str, Any]]:
        return self._read_last("capital_scaling.jsonl", last_n)

    def read_learning_pace(self, last_n: int = 50) -> List[Dict[str, Any]]:
        return self._read_last("learning_pace.jsonl", last_n)

    def read_all_events(self, last_n: int = 100) -> List[Dict[str, Any]]:
        return self._read_last("governance_events.jsonl", last_n)

    def read_events_by_candidate(self, candidate_id: str, last_n: int = 50) -> List[Dict[str, Any]]:
        events = self._read_last("governance_events.jsonl", last_n * 5)
        return [e for e in events if e.get("candidate_id") == candidate_id][-last_n:]

    # ──────────────────────────────────────────
    #  Internal
    # ──────────────────────────────────────────

    def _log_event(self, event_type: GovernanceEventType, candidate_id: str, payload: Dict[str, Any]) -> None:
        import hashlib
        eid = hashlib.sha256(f"{event_type.value}:{candidate_id}:{time.time()}".encode()).hexdigest()[:12]
        event = GovernanceEvent(
            event_id=f"gov_{eid}",
            event_type=event_type,
            candidate_id=candidate_id,
            payload=payload,
        )
        self._append("governance_events.jsonl", event.to_dict())

    def _append(self, filename: str, data: Dict[str, Any]) -> None:
        if "ts" not in data:
            data["ts"] = time.time()
        path = self._dir / filename
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, default=str) + "\n")

    def _read_last(self, filename: str, last_n: int) -> List[Dict[str, Any]]:
        path = self._dir / filename
        if not path.exists():
            return []
        lines: List[str] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return []
        result: List[Dict[str, Any]] = []
        for line in lines[-last_n:]:
            line = line.strip()
            if not line:
                continue
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result
