"""
Meta-Control Layer — FastAPI Routes
════════════════════════════════════

Endpoints under /api/meta-control:

  GET  /status          — Current mode, budget, recovery, health
  GET  /mode-history    — Recent mode transitions
  GET  /health          — Detailed stack health report
  GET  /universe        — Current universe filter state
  GET  /rotation        — Latest opportunity rankings
  GET  /recovery        — Current recovery protocol state
  POST /force-mode      — Force system mode (emergency)
  POST /block-symbol    — Manually block a symbol
  POST /unblock-symbol  — Remove manual symbol block

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.trading.global_risk_budget import GlobalRiskBudgetController
from backend.services.trading.meta_control_engine import MetaControlEngine
from backend.services.trading.meta_control_models import (
    ModeInputs,
    RotationCandidate,
    SystemMode,
)
from backend.services.trading.meta_control_store import MetaControlStore
from backend.services.trading.meta_health_monitor import MetaHealthMonitor
from backend.services.trading.opportunity_filter import OpportunityUniverseFilter
from backend.services.trading.recovery_protocol import RecoveryProtocolEngine
from backend.services.trading.strategic_rotation import StrategicRotationEngine

router = APIRouter(prefix="/api/meta-control", tags=["meta-control"])

# ── Singleton instances ──────────────────────────────────────────────
_engine = MetaControlEngine()
_budget_ctrl = GlobalRiskBudgetController()
_filter = OpportunityUniverseFilter()
_recovery = RecoveryProtocolEngine()
_rotation = StrategicRotationEngine()
_health = MetaHealthMonitor()
_store = MetaControlStore()


# ── Request models ───────────────────────────────────────────────────

class ForceModeRequest(BaseModel):
    mode: str  # EXPANSION / OFFENSIVE / SELECTIVE / DEFENSIVE / SURVIVAL


class SymbolRequest(BaseModel):
    symbol: str


# ── GET endpoints ────────────────────────────────────────────────────

@router.get("/status")
async def get_status():
    """Full meta-control status snapshot."""
    mode = _engine.get_mode()
    budget = _budget_ctrl.get_last_budget()
    recovery_phase = _recovery.get_phase()
    health_report = _health.get_report()
    universe = _filter.get_universe_state(mode)

    return {
        "mode": mode.value,
        "budget": budget.to_dict() if budget else None,
        "recovery_phase": recovery_phase.value,
        "recovery_capacity": _recovery.get_capacity_multiplier(),
        "health": health_report.to_dict(),
        "universe": universe.to_dict(),
    }


@router.get("/mode-history")
async def get_mode_history(last_n: int = 50):
    """Recent mode transitions."""
    return {"history": _store.read_mode_history(last_n)}


@router.get("/health")
async def get_health():
    """Detailed stack health report."""
    report = _health.get_report()
    return report.to_dict()


@router.get("/universe")
async def get_universe():
    """Current universe filter state."""
    mode = _engine.get_mode()
    state = _filter.get_universe_state(mode)
    return state.to_dict()


@router.get("/rotation")
async def get_rotation():
    """Latest opportunity rankings."""
    rankings = _rotation.get_last_rankings()
    return {
        "count": len(rankings),
        "rankings": [r.to_dict() for r in rankings],
    }


@router.get("/recovery")
async def get_recovery():
    """Current recovery protocol state."""
    phase = _recovery.get_phase()
    return {
        "phase": phase.value,
        "capacity_multiplier": _recovery.get_capacity_multiplier(),
        "max_new_positions": _recovery.get_max_new_positions(),
    }


# ── POST endpoints ──────────────────────────────────────────────────

@router.post("/force-mode")
async def force_mode(req: ForceModeRequest):
    """Force the system into a specific mode (emergency)."""
    try:
        mode = SystemMode(req.mode)
    except ValueError:
        valid = [m.value for m in SystemMode]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode '{req.mode}'. Valid: {valid}",
        )
    _engine.force_mode(mode)
    # Recompute budget
    budget = _budget_ctrl.compute(mode, degradation_mult=_health.get_degradation_multiplier())
    _store.log_risk_budget(budget)
    return {"status": "ok", "mode": mode.value, "budget": budget.to_dict()}


@router.post("/block-symbol")
async def block_symbol(req: SymbolRequest):
    """Manually block a symbol from the opportunity universe."""
    _filter.add_manual_block(req.symbol)
    return {"status": "ok", "blocked": req.symbol}


@router.post("/unblock-symbol")
async def unblock_symbol(req: SymbolRequest):
    """Remove a manual symbol block."""
    _filter.remove_manual_block(req.symbol)
    return {"status": "ok", "unblocked": req.symbol}
