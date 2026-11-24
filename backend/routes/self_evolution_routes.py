"""
Self-Evolving Trading Intelligence — FastAPI Routes
════════════════════════════════════════════════════

Endpoints under /api/self-evolution:

  GET  /status           — Global evolution state
  GET  /strategies       — Strategy health & weights
  GET  /patterns         — Discovered meta-learning patterns
  GET  /parameters       — Parameter states & adaptations
  GET  /failures         — Failure patterns & suppressions
  GET  /discoveries      — Discovered edges
  GET  /cycles           — Improvement loop history
  GET  /trend            — Net improvement trend over cycles
  POST /run-cycle        — Force an improvement cycle
  POST /rollback-params  — Rollback all parameters to defaults

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.services.trading.strategy_evolution_engine import StrategyEvolutionEngine
from backend.services.trading.meta_learning_engine import MetaLearningEngine
from backend.services.trading.parameter_adaptation_engine import ParameterAdaptationEngine
from backend.services.trading.failure_pattern_detector import FailurePatternDetector
from backend.services.trading.discovery_engine import DiscoveryEngine
from backend.services.trading.continuous_improvement_loop import ContinuousImprovementLoop
from backend.services.trading.self_evolution_store import SelfEvolutionStore

router = APIRouter(prefix="/api/self-evolution", tags=["self-evolution"])

# ── Singleton instances ──────────────────────────────────────────────
_evolution = StrategyEvolutionEngine()
_meta = MetaLearningEngine()
_params = ParameterAdaptationEngine()
_failures = FailurePatternDetector()
_discovery = DiscoveryEngine()
_loop = ContinuousImprovementLoop(
    evolution_engine=_evolution,
    meta_learning=_meta,
    param_adaptation=_params,
    failure_detector=_failures,
    discovery_engine=_discovery,
)
_store = SelfEvolutionStore()


# ── GET endpoints ────────────────────────────────────────────────────

@router.get("/status")
async def get_status():
    """Global evolution state snapshot."""
    state = _loop.get_state()
    return state.to_dict()


@router.get("/strategies")
async def get_strategies():
    """Strategy health and current weights."""
    report = _evolution.get_last_report()
    weights = _evolution.get_all_weights()
    return {
        "report": report.to_dict() if report else None,
        "weights": weights,
    }


@router.get("/patterns")
async def get_patterns():
    """Meta-learning discovered patterns."""
    report = _meta.get_last_report()
    actionable = _meta.get_actionable_patterns()
    return {
        "report": report.to_dict() if report else None,
        "actionable": [p.to_dict() for p in actionable],
    }


@router.get("/parameters")
async def get_parameters():
    """Parameter states and adaptation plan."""
    plan = _params.get_last_plan()
    all_params = _params.get_all_parameters()
    return {
        "plan": plan.to_dict() if plan else None,
        "parameters": [p.to_dict() for p in all_params],
    }


@router.get("/failures")
async def get_failures():
    """Failure patterns and suppression state."""
    report = _failures.get_last_report()
    suppressed = [ft.value for ft in _failures.get_suppressed_types()]
    return {
        "report": report.to_dict() if report else None,
        "suppressed_types": suppressed,
    }


@router.get("/discoveries")
async def get_discoveries():
    """Discovered edges."""
    report = _discovery.get_last_report()
    validated = _discovery.get_validated_edges()
    return {
        "report": report.to_dict() if report else None,
        "validated_edges": [e.to_dict() for e in validated],
    }


@router.get("/cycles")
async def get_cycles(last_n: int = 20):
    """Improvement loop history."""
    cycles = _loop.get_cycle_history(last_n)
    return {"cycles": [c.to_dict() for c in cycles]}


@router.get("/trend")
async def get_trend():
    """Net improvement trend over cycles."""
    trend = _loop.get_improvement_trend()
    return {"trend": trend, "total_cycles": len(trend)}


# ── POST endpoints ──────────────────────────────────────────────────

@router.post("/run-cycle")
async def run_cycle():
    """Force an improvement cycle to run immediately."""
    cycle = _loop.run_cycle(force=True)
    if cycle is None:
        return {"status": "no_data", "detail": "No trades ingested yet"}

    # Persist
    _store.log_cycle(cycle)
    _store.log_state(_loop.get_state())
    evo_report = _evolution.get_last_report()
    if evo_report:
        _store.log_evolution(evo_report)
    meta_report = _meta.get_last_report()
    if meta_report:
        _store.log_learning(meta_report)
    param_plan = _params.get_last_plan()
    if param_plan:
        _store.log_adaptation(param_plan)
    fail_report = _failures.get_last_report()
    if fail_report:
        _store.log_failures(fail_report)
    disc_report = _discovery.get_last_report()
    if disc_report:
        _store.log_discovery(disc_report)

    return {"status": "ok", "cycle": cycle.to_dict()}


@router.post("/rollback-params")
async def rollback_params():
    """Rollback all parameters to defaults."""
    count = _params.rollback_all()
    return {"status": "ok", "reverted": count}
