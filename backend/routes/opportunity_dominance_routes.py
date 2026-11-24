"""
Opportunity Dominance Layer — FastAPI Routes
═════════════════════════════════════════════

Endpoints under /api/opportunity-dominance:

  GET  /status          — Full dominance layer status snapshot
  GET  /detections      — Recent opportunity detections
  GET  /rankings        — Recent ranking snapshots
  GET  /aggression      — Recent aggression decisions
  GET  /timing          — Recent timing results
  GET  /winners         — Recent winner exploitation decisions
  GET  /decay           — Recent decay signals
  GET  /allocations     — Recent capital allocation plans
  POST /force-aggression — Force aggression multiplier (emergency)

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.trading.aggression_amplifier import AggressionAmplifier
from backend.services.trading.opportunity_capital_allocator import OpportunityCapitalAllocator
from backend.services.trading.opportunity_decay_detector import OpportunityDecayDetector
from backend.services.trading.opportunity_detection_engine import OpportunityDetectionEngine
from backend.services.trading.opportunity_dominance_store import OpportunityDominanceStore
from backend.services.trading.opportunity_ranking_engine import OpportunityRankingEngine
from backend.services.trading.timing_precision_engine import TimingPrecisionEngine
from backend.services.trading.winner_exploitation_engine import WinnerExploitationEngine

router = APIRouter(prefix="/api/opportunity-dominance", tags=["opportunity-dominance"])

# ── Singleton instances ──────────────────────────────────────────────
_detector = OpportunityDetectionEngine()
_ranker = OpportunityRankingEngine()
_aggression = AggressionAmplifier()
_timing = TimingPrecisionEngine()
_winner = WinnerExploitationEngine()
_decay = OpportunityDecayDetector()
_allocator = OpportunityCapitalAllocator()
_store = OpportunityDominanceStore()


# ── Request models ───────────────────────────────────────────────────

class ForceAggressionRequest(BaseModel):
    multiplier: float  # 0.5 to 2.0


# ── GET endpoints ────────────────────────────────────────────────────

@router.get("/status")
async def get_status():
    """Full opportunity dominance layer status snapshot."""
    detection = _detector.get_last_report()
    ranking = _ranker.get_last_report()
    aggression = _aggression.get_last_decision()
    timing = _timing.get_last_result()
    plan = _allocator.get_last_plan()

    return {
        "detection": detection.to_dict() if detection else None,
        "ranking": {
            "total_ranked": ranking.total_ranked,
            "top_candidates": len(ranking.top_candidates),
            "suppressed": len(ranking.suppressed),
        } if ranking else None,
        "aggression": aggression.to_dict() if aggression else None,
        "timing": timing.to_dict() if timing else None,
        "allocation": plan.to_dict() if plan else None,
    }


@router.get("/detections")
async def get_detections(last_n: int = 50):
    """Recent opportunity detection reports."""
    return {"history": _store.read_detections(last_n)}


@router.get("/rankings")
async def get_rankings(last_n: int = 50):
    """Recent ranking snapshots."""
    return {"history": _store.read_rankings(last_n)}


@router.get("/aggression")
async def get_aggression(last_n: int = 50):
    """Recent aggression decisions."""
    return {"history": _store.read_aggressions(last_n)}


@router.get("/timing")
async def get_timing(last_n: int = 50):
    """Recent timing precision results."""
    return {"history": _store.read_timings(last_n)}


@router.get("/winners")
async def get_winners(last_n: int = 50):
    """Recent winner exploitation decisions."""
    return {"history": _store.read_winner_decisions(last_n)}


@router.get("/decay")
async def get_decay(last_n: int = 50):
    """Recent decay detection signals."""
    return {"history": _store.read_decay_signals(last_n)}


@router.get("/allocations")
async def get_allocations(last_n: int = 50):
    """Recent capital allocation plans."""
    return {"history": _store.read_allocations(last_n)}


# ── POST endpoints ──────────────────────────────────────────────────

@router.post("/force-aggression")
async def force_aggression(req: ForceAggressionRequest):
    """Force an aggression multiplier (emergency override)."""
    if req.multiplier < 0.0 or req.multiplier > 3.0:
        raise HTTPException(
            status_code=400,
            detail=f"multiplier must be 0.0–3.0, got {req.multiplier}",
        )
    # Create a forced decision
    from backend.services.trading.opportunity_dominance_models import AggressionDecision
    forced = AggressionDecision(
        aggression_multiplier=req.multiplier,
        size_multiplier=req.multiplier,
        scaling_speed=1.0,
        tp_delay_factor=1.0,
        trailing_looseness=1.0,
        risk_adjusted_intensity=min(1.0, max(0.0, (req.multiplier - 1.0) / 0.75)),
        conditions_met=0,
        detail=f"FORCED override mult={req.multiplier}",
    )
    _store.log_aggression(forced)
    return {"status": "ok", "aggression": forced.to_dict()}
