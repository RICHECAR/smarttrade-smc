"""
Structural Break Intelligence Layer — FastAPI Routes
═════════════════════════════════════════════════════

Endpoints under /api/structural-breaks:

  GET  /status          — Full structural break status snapshot
  GET  /breaks          — Recent break events history
  GET  /transitions     — Recent regime transition events
  GET  /doctrine        — Latest doctrine validity state
  GET  /propagation     — Recent shock propagation analyses
  GET  /throttle        — Current throttle state + history
  GET  /recovery        — Current recovery guard state
  POST /force-throttle  — Force a throttle level (emergency)
  POST /register-shock  — Manually register a shock event

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.trading.false_recovery_guard import FalseRecoveryGuard
from backend.services.trading.preemptive_throttle_engine import PreemptiveThrottleEngine
from backend.services.trading.structural_break_detector import StructuralBreakDetector
from backend.services.trading.structural_break_models import ThrottleLevel
from backend.services.trading.structural_break_store import StructuralBreakStore
from backend.services.trading.regime_transition_monitor import RegimeTransitionMonitor
from backend.services.trading.doctrine_validity_engine import DoctrineValidityEngine
from backend.services.trading.shock_propagation_analyzer import ShockPropagationAnalyzer

router = APIRouter(prefix="/api/structural-breaks", tags=["structural-breaks"])

# ── Singleton instances ──────────────────────────────────────────────
_detector = StructuralBreakDetector()
_transition = RegimeTransitionMonitor()
_doctrine = DoctrineValidityEngine()
_propagation = ShockPropagationAnalyzer()
_throttle = PreemptiveThrottleEngine()
_recovery = FalseRecoveryGuard()
_store = StructuralBreakStore()


# ── Request models ───────────────────────────────────────────────────

class ForceThrottleRequest(BaseModel):
    level: str  # NONE / LIGHT / MODERATE / HEAVY / HALT


class RegisterShockRequest(BaseModel):
    reason: str = "manual"


# ── GET endpoints ────────────────────────────────────────────────────

@router.get("/status")
async def get_status():
    """Full structural break status snapshot."""
    throttle_state = _throttle.get_last_state()
    return {
        "throttle": throttle_state.to_dict() if throttle_state else None,
        "recovery": {
            "shock_active": _recovery._shock_active,
            "cycles_since_shock": _recovery._cycles_since_shock,
        },
        "detector_symbols": list({k[0] for k in _detector._histories}),
        "transition_symbols": list(_transition._momentum_hist.keys()),
    }


@router.get("/breaks")
async def get_breaks(last_n: int = 50):
    """Recent break events history."""
    return {"history": _store.read_break_history(last_n)}


@router.get("/transitions")
async def get_transitions(last_n: int = 50):
    """Recent regime transition events."""
    return {"history": _store.read_transition_history(last_n)}


@router.get("/doctrine")
async def get_doctrine(last_n: int = 50):
    """Doctrine mismatch verdicts history."""
    return {"history": _store.read_doctrine_history(last_n)}


@router.get("/propagation")
async def get_propagation(last_n: int = 50):
    """Shock propagation analyses history."""
    return {"history": _store.read_propagation_history(last_n)}


@router.get("/throttle")
async def get_throttle(last_n: int = 50):
    """Current throttle state + history."""
    state = _throttle.get_last_state()
    return {
        "current": state.to_dict() if state else None,
        "history": _store.read_throttle_history(last_n),
    }


@router.get("/recovery")
async def get_recovery(last_n: int = 50):
    """Recovery guard state + history."""
    return {
        "shock_active": _recovery._shock_active,
        "cycles_since_shock": _recovery._cycles_since_shock,
        "history": _store.read_recovery_history(last_n),
    }


# ── POST endpoints ──────────────────────────────────────────────────

@router.post("/force-throttle")
async def force_throttle(req: ForceThrottleRequest):
    """Force a specific throttle level (emergency)."""
    try:
        level = ThrottleLevel(req.level)
    except ValueError:
        valid = [lv.value for lv in ThrottleLevel]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid level '{req.level}'. Valid: {valid}",
        )
    _throttle.force_level(level)
    state = _throttle.get_last_state()
    if state:
        _store.log_throttle_state(state)
    return {"status": "ok", "level": level.value}


@router.post("/register-shock")
async def register_shock(req: RegisterShockRequest):
    """Manually register a shock event to activate recovery guard."""
    _recovery.register_shock()
    return {
        "status": "ok",
        "reason": req.reason,
        "shock_active": _recovery._shock_active,
    }
