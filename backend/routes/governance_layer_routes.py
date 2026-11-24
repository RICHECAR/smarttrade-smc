"""
Validation & Governance Layer — FastAPI Routes
═══════════════════════════════════════════════

Endpoints under /api/governance-layer:

  GET  /status               — Aggregate governance status
  GET  /candidates           — All tracked candidates
  GET  /candidates/{id}      — Single candidate detail
  GET  /validations          — Validation history
  GET  /comparisons          — A/B comparison history
  GET  /rollbacks            — Rollback event history
  GET  /scaling              — Capital scaling allocations
  GET  /learning-pace        — Current learning pace
  GET  /audit                — Full governance audit trail
  GET  /audit/{candidate_id} — Audit trail for one candidate

  POST /validate             — Validate a proposed adaptation
  POST /register-candidate   — Register new candidate
  POST /promote              — Attempt stage promotion
  POST /compare              — Run A/B comparison
  POST /rollback             — Force manual rollback
  POST /evaluate-scaling     — Evaluate capital scaling
  POST /assess-pace          — Assess learning pace

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from backend.services.trading.evolution_validation_engine import EvolutionValidationEngine
from backend.services.trading.candidate_deployment_manager import CandidateDeploymentManager
from backend.services.trading.ab_comparison_engine import ABComparisonEngine
from backend.services.trading.rollback_governor import RollbackGovernor
from backend.services.trading.capital_scaling_governor import CapitalScalingGovernor
from backend.services.trading.learning_pace_controller import LearningPaceController
from backend.services.trading.governance_store import GovernanceStore
from backend.services.trading.governance_models import (
    ABMetrics,
    DeploymentStage,
    GovernanceStatus,
    LearningPace,
)

router = APIRouter(prefix="/api/governance-layer", tags=["governance-layer"])

# ── Singleton instances ──────────────────────────────────────────────
_validator = EvolutionValidationEngine()
_deployer = CandidateDeploymentManager()
_comparator = ABComparisonEngine()
_rollback = RollbackGovernor()
_scaler = CapitalScalingGovernor()
_pacer = LearningPaceController()
_store = GovernanceStore()


# ═══════════════════════════════════════════════════════════════
#  Pydantic request models
# ═══════════════════════════════════════════════════════════════

class ValidateRequest(BaseModel):
    adaptation_id: str
    sample_size: int = 0
    win_rate_ci_lower: float = 0.0
    expectancy_stability: float = 0.0
    pf_stability: float = 0.0
    windows_tested: int = 0
    windows_positive: int = 0
    consistency_score: float = 0.0
    regime_dependency: float = 0.0
    worst_window_expectancy: float = 0.0
    raw_improvement: float = 0.0
    execution_adjusted_improvement: float = 0.0
    fragility_before: float = 0.0
    fragility_after: float = 0.0
    max_dd_before: float = 0.0
    max_dd_after: float = 0.0


class RegisterCandidateRequest(BaseModel):
    adaptation_id: str
    description: str = ""


class PromoteRequest(BaseModel):
    candidate_id: str
    trades: int = 0
    win_rate: float = 0.0
    expectancy: float = 0.0
    sharpe: float = 0.0
    fragility: float = 0.0
    drawdown: float = 0.0
    ab_confidence: float = 0.0


class CompareRequest(BaseModel):
    candidate_id: str
    baseline_trade_count: int = 0
    baseline_win_rate: float = 0.0
    baseline_expectancy: float = 0.0
    baseline_max_drawdown: float = 0.0
    baseline_sharpe: float = 0.0
    baseline_execution_quality: float = 0.0
    baseline_avg_recovery_time: float = 0.0
    baseline_portfolio_impact: float = 0.0
    candidate_trade_count: int = 0
    candidate_win_rate: float = 0.0
    candidate_expectancy: float = 0.0
    candidate_max_drawdown: float = 0.0
    candidate_sharpe: float = 0.0
    candidate_execution_quality: float = 0.0
    candidate_avg_recovery_time: float = 0.0
    candidate_portfolio_impact: float = 0.0


class RollbackRequest(BaseModel):
    candidate_id: str
    reason: str = ""


class ScalingRequest(BaseModel):
    candidate_id: str
    trades: int = 0
    win_rate: float = 0.0
    expectancy: float = 0.0
    sharpe: float = 0.0
    drawdown: float = 0.0
    fragility: float = 0.0
    total_capital: float = 0.0


class PaceRequest(BaseModel):
    regime_stability: float = 1.0
    evidence_strength: float = 1.0
    execution_noise: float = 0.0
    portfolio_stress: float = 0.0


# ═══════════════════════════════════════════════════════════════
#  GET endpoints
# ═══════════════════════════════════════════════════════════════

@router.get("/status")
async def get_status():
    """Aggregate governance layer status."""
    counts = _deployer.get_stage_counts()
    pace_assessment = _pacer.get_last_assessment()
    status = GovernanceStatus(
        active_candidates=sum(counts.values()),
        shadow_count=counts.get("shadow", 0),
        paper_count=counts.get("paper", 0),
        limited_live_count=counts.get("limited_live", 0),
        active_count=counts.get("active", 0),
        rolled_back_count=counts.get("rolled_back", 0),
        pending_validations=counts.get("candidate", 0),
        active_ab_tests=0,
        learning_pace=pace_assessment.pace if pace_assessment else LearningPace.NORMAL,
        capital_utilisation=_scaler.get_total_utilisation(),
        recent_rollbacks=_rollback.get_recent_rollback_count(),
    )
    return status.to_dict()


@router.get("/candidates")
async def get_candidates():
    """All tracked candidates."""
    candidates = _deployer.get_all_candidates()
    return {"candidates": [c.to_dict() for c in candidates]}


@router.get("/candidates/{candidate_id}")
async def get_candidate(candidate_id: str):
    """Single candidate detail."""
    record = _deployer.get_candidate(candidate_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return record.to_dict()


@router.get("/validations")
async def get_validations(last_n: int = 50):
    """Validation history."""
    return {"validations": _store.read_validations(last_n)}


@router.get("/comparisons")
async def get_comparisons(last_n: int = 50):
    """A/B comparison history."""
    return {"comparisons": _store.read_ab_comparisons(last_n)}


@router.get("/rollbacks")
async def get_rollbacks(last_n: int = 50):
    """Rollback event history."""
    return {"rollbacks": _store.read_rollbacks(last_n)}


@router.get("/scaling")
async def get_scaling():
    """Capital scaling allocations."""
    allocations = _scaler.get_all_allocations()
    return {
        "allocations": [a.to_dict() for a in allocations],
        "total_utilisation": round(_scaler.get_total_utilisation(), 4),
    }


@router.get("/learning-pace")
async def get_learning_pace():
    """Current learning pace."""
    assessment = _pacer.get_last_assessment()
    return assessment.to_dict() if assessment else {"pace": "normal", "adaptation_multiplier": 1.0}


@router.get("/audit")
async def get_audit(last_n: int = 100):
    """Full governance audit trail."""
    return {"events": _store.read_all_events(last_n)}


@router.get("/audit/{candidate_id}")
async def get_audit_by_candidate(candidate_id: str, last_n: int = 50):
    """Audit trail for one candidate."""
    events = _store.read_events_by_candidate(candidate_id, last_n)
    return {"candidate_id": candidate_id, "events": events}


# ═══════════════════════════════════════════════════════════════
#  POST endpoints
# ═══════════════════════════════════════════════════════════════

@router.post("/validate")
async def validate_adaptation(req: ValidateRequest):
    """Validate a proposed adaptation."""
    result = _validator.validate(
        req.adaptation_id,
        sample_size=req.sample_size,
        win_rate_ci_lower=req.win_rate_ci_lower,
        expectancy_stability=req.expectancy_stability,
        pf_stability=req.pf_stability,
        windows_tested=req.windows_tested,
        windows_positive=req.windows_positive,
        consistency_score=req.consistency_score,
        regime_dependency=req.regime_dependency,
        worst_window_expectancy=req.worst_window_expectancy,
        raw_improvement=req.raw_improvement,
        execution_adjusted_improvement=req.execution_adjusted_improvement,
        fragility_before=req.fragility_before,
        fragility_after=req.fragility_after,
        max_dd_before=req.max_dd_before,
        max_dd_after=req.max_dd_after,
    )
    _store.log_validation(result)
    return result.to_dict()


@router.post("/register-candidate")
async def register_candidate(req: RegisterCandidateRequest):
    """Register a new adaptation candidate."""
    # Optionally validate first
    record = _deployer.register_candidate(req.adaptation_id, req.description)
    _scaler.register(record.candidate_id)
    _store.log_deployment_transition(record, "registered")
    return record.to_dict()


@router.post("/promote")
async def promote_candidate(req: PromoteRequest):
    """Attempt to promote a candidate to the next deployment stage."""
    # Check learning pace — refuse promotion if frozen
    if _pacer.is_frozen():
        raise HTTPException(
            status_code=409,
            detail="Learning pace is FROZEN — promotion blocked until conditions improve",
        )

    # Check rollback cooldown
    if _rollback.is_in_cooldown(req.candidate_id):
        raise HTTPException(
            status_code=409,
            detail="Candidate is in rollback cooldown period",
        )

    try:
        record = _deployer.try_promote(
            req.candidate_id,
            trades=req.trades,
            win_rate=req.win_rate,
            expectancy=req.expectancy,
            sharpe=req.sharpe,
            fragility=req.fragility,
            drawdown=req.drawdown,
            ab_confidence=req.ab_confidence,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidate not found")

    _store.log_deployment_transition(record, "promotion_attempt")
    return record.to_dict()


@router.post("/compare")
async def run_comparison(req: CompareRequest):
    """Run A/B comparison between baseline and candidate."""
    baseline = ABMetrics(
        trade_count=req.baseline_trade_count,
        win_rate=req.baseline_win_rate,
        expectancy=req.baseline_expectancy,
        max_drawdown=req.baseline_max_drawdown,
        sharpe=req.baseline_sharpe,
        execution_quality=req.baseline_execution_quality,
        avg_recovery_time=req.baseline_avg_recovery_time,
        portfolio_impact=req.baseline_portfolio_impact,
    )
    candidate = ABMetrics(
        trade_count=req.candidate_trade_count,
        win_rate=req.candidate_win_rate,
        expectancy=req.candidate_expectancy,
        max_drawdown=req.candidate_max_drawdown,
        sharpe=req.candidate_sharpe,
        execution_quality=req.candidate_execution_quality,
        avg_recovery_time=req.candidate_avg_recovery_time,
        portfolio_impact=req.candidate_portfolio_impact,
    )
    result = _comparator.compare(req.candidate_id, baseline, candidate)

    # Attach to deployment manager
    _deployer.attach_ab_result(req.candidate_id, result)
    _store.log_ab_comparison(result)
    return result.to_dict()


@router.post("/rollback")
async def rollback_candidate(req: RollbackRequest):
    """Force manual rollback of a candidate."""
    record = _deployer.get_candidate(req.candidate_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Candidate not found")

    event = _rollback.manual_rollback(req.candidate_id, record.stage, req.reason)
    _deployer.rollback(req.candidate_id, req.reason)
    _scaler.force_sandbox(req.candidate_id)
    _store.log_rollback(event)
    return event.to_dict()


@router.post("/evaluate-scaling")
async def evaluate_scaling(req: ScalingRequest):
    """Evaluate capital scaling for a candidate."""
    decision = _scaler.evaluate_scaling(
        req.candidate_id,
        trades=req.trades,
        win_rate=req.win_rate,
        expectancy=req.expectancy,
        sharpe=req.sharpe,
        drawdown=req.drawdown,
        fragility=req.fragility,
        total_capital=req.total_capital,
    )
    _store.log_capital_scaling(decision)
    return decision.to_dict()


@router.post("/assess-pace")
async def assess_pace(req: PaceRequest):
    """Assess and update learning pace."""
    assessment = _pacer.assess(
        regime_stability=req.regime_stability,
        evidence_strength=req.evidence_strength,
        execution_noise=req.execution_noise,
        portfolio_stress=req.portfolio_stress,
    )
    _store.log_learning_pace(assessment)
    return assessment.to_dict()
