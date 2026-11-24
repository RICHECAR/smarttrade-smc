"""
Darwinian Layer API Routes — FastAPI Endpoints
══════════════════════════════════════════════

REST endpoints for the Darwinian setup selection layer:
  - Lifecycle state per setup
  - Survival scoring
  - Edge allocation map
  - Regime × setup matrix
  - Decay guard report
  - Full pipeline: evaluate a setup
  - Persistence / restore

Router prefix: /api/darwinian
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from backend.services.trading.darwinian_models import (
    SetupKey,
    SetupState,
    SurvivalRecommendation,
)
from backend.services.trading.darwinian_store import DarwinianStore
from backend.services.trading.edge_allocator import EdgeAllocator
from backend.services.trading.policy_decay_guard import PolicyDecayGuard
from backend.services.trading.regime_setup_matrix import RegimeSetupMatrix
from backend.services.trading.setup_lifecycle import SetupLifecycleEngine
from backend.services.trading.setup_survival import SetupSurvivalScorer

logger = logging.getLogger("smarttrade.darwinian.routes")

router = APIRouter(prefix="/api/darwinian", tags=["Darwinian Selection"])


# ═══════════════════════════════════════════════════════════════
#  SINGLETON INSTANCES
# ═══════════════════════════════════════════════════════════════
_lifecycle: Optional[SetupLifecycleEngine] = None
_scorer: Optional[SetupSurvivalScorer] = None
_allocator: Optional[EdgeAllocator] = None
_matrix: Optional[RegimeSetupMatrix] = None
_guard: Optional[PolicyDecayGuard] = None
_store: Optional[DarwinianStore] = None


def _get_lifecycle() -> SetupLifecycleEngine:
    global _lifecycle
    if _lifecycle is None:
        _lifecycle = SetupLifecycleEngine()
    return _lifecycle


def _get_scorer() -> SetupSurvivalScorer:
    global _scorer
    if _scorer is None:
        _scorer = SetupSurvivalScorer()
    return _scorer


def _get_allocator() -> EdgeAllocator:
    global _allocator
    if _allocator is None:
        _allocator = EdgeAllocator()
    return _allocator


def _get_matrix() -> RegimeSetupMatrix:
    global _matrix
    if _matrix is None:
        _matrix = RegimeSetupMatrix()
    return _matrix


def _get_guard() -> PolicyDecayGuard:
    global _guard
    if _guard is None:
        _guard = PolicyDecayGuard()
    return _guard


def _get_store() -> DarwinianStore:
    global _store
    if _store is None:
        _store = DarwinianStore()
    return _store


def _make_key(direction: str, strength: str, symbol: str) -> SetupKey:
    return SetupKey(direction=direction, strength=strength, symbol=symbol)


# ═══════════════════════════════════════════════════════════════
#  1. RECORD OUTCOME — feed trade results into the whole pipeline
# ═══════════════════════════════════════════════════════════════
@router.post("/record_outcome")
async def record_outcome(
    direction: str,
    strength: str,
    symbol: str,
    pnl: float,
    regime: str = "unknown",
) -> Dict[str, Any]:
    """Record a trade outcome and update all Darwinian subsystems."""
    key = _make_key(direction, strength, symbol)
    lifecycle = _get_lifecycle()
    matrix = _get_matrix()

    # 1. Lifecycle update
    profile = lifecycle.record_outcome(key, pnl=pnl, regime=regime)

    # 2. Matrix update
    matrix.record(regime=regime, setup_key=str(key), pnl=pnl)

    # 3. Survival score
    regime_fit = matrix.get_fit(regime, str(key))
    verdict = _get_scorer().score(profile, regime_fit=regime_fit)

    # 4. Decay check
    decay = _get_guard().evaluate(profile, verdict)

    # 5. Persist
    store = _get_store()
    store.save_profile(str(key), profile.to_dict())
    if profile.state_history:
        store.save_transition(str(key), profile.state_history[-1])

    return {
        "setup_key": str(key),
        "state": profile.state.value,
        "survival_score": verdict.score,
        "recommendation": verdict.recommendation.value,
        "decay_severity": decay.decay_severity,
        "hard_block": decay.hard_block,
        "allowed": lifecycle.is_allowed(key),
    }


# ═══════════════════════════════════════════════════════════════
#  2. EVALUATE — dry-run: check if a setup should trade now
# ═══════════════════════════════════════════════════════════════
@router.get("/evaluate")
async def evaluate_setup(
    direction: str,
    strength: str,
    symbol: str,
    regime: str = "unknown",
) -> Dict[str, Any]:
    """Dry-run evaluation: is this setup allowed to trade?"""
    key = _make_key(direction, strength, symbol)
    lifecycle = _get_lifecycle()
    profile = lifecycle.get_profile(key)

    if profile is None:
        return {
            "setup_key": str(key),
            "state": "unseen",
            "allowed": True,
            "survival_score": None,
            "recommendation": "no_data",
            "allocation_pct": 1.0,
            "decay_severity": 0,
        }

    # Regime fit
    regime_fit = _get_matrix().get_fit(regime, str(key))
    verdict = _get_scorer().score(profile, regime_fit=regime_fit)
    decay = _get_guard().evaluate(profile, verdict)
    allocation = _get_allocator().single_allocation(verdict)

    return {
        "setup_key": str(key),
        "state": profile.state.value,
        "allowed": lifecycle.is_allowed(key) and not decay.hard_block,
        "survival_score": verdict.score,
        "recommendation": verdict.recommendation.value,
        "allocation_pct": allocation.allocation_pct,
        "max_concurrent": allocation.max_concurrent,
        "decay_severity": decay.decay_severity,
        "decay_overrides": {
            "size_mult": decay.override_size_mult,
            "conf_threshold_add": decay.override_conf_threshold,
            "concurrent_delta": decay.override_max_concurrent,
        },
    }


# ═══════════════════════════════════════════════════════════════
#  3. LIFECYCLE SUMMARY
# ═══════════════════════════════════════════════════════════════
@router.get("/lifecycle/summary")
async def lifecycle_summary() -> Dict[str, Any]:
    """Overview of all setups and their lifecycle states."""
    return _get_lifecycle().summary()


@router.get("/lifecycle/profile")
async def lifecycle_profile(
    direction: str, strength: str, symbol: str,
) -> Dict[str, Any]:
    """Detail for one setup."""
    key = _make_key(direction, strength, symbol)
    profile = _get_lifecycle().get_profile(key)
    if profile is None:
        raise HTTPException(status_code=404, detail="Setup not found")
    return profile.to_dict()


@router.post("/lifecycle/reset")
async def lifecycle_reset(
    direction: str, strength: str, symbol: str,
) -> Dict[str, str]:
    """Manually reset a blocked setup back to EMERGING."""
    key = _make_key(direction, strength, symbol)
    _get_lifecycle().reset_setup(key)
    return {"status": "reset", "setup_key": str(key)}


# ═══════════════════════════════════════════════════════════════
#  4. ALLOCATION MAP
# ═══════════════════════════════════════════════════════════════
@router.get("/allocations")
async def allocations(regime: str = "unknown") -> Dict[str, Any]:
    """Compute allocations for all known setups."""
    lifecycle = _get_lifecycle()
    scorer = _get_scorer()
    matrix = _get_matrix()

    verdicts = {}
    for sk, profile in lifecycle.get_all_profiles().items():
        regime_fit = matrix.get_fit(regime, sk)
        verdicts[sk] = scorer.score(profile, regime_fit=regime_fit)

    allocs = _get_allocator().allocate(verdicts)
    return {
        sk: {
            "allocation_pct": a.allocation_pct,
            "max_concurrent": a.max_concurrent,
            "reason": a.reason,
        }
        for sk, a in allocs.items()
    }


# ═══════════════════════════════════════════════════════════════
#  5. REGIME × SETUP MATRIX
# ═══════════════════════════════════════════════════════════════
@router.get("/matrix")
async def full_matrix() -> Dict[str, Any]:
    """Full regime × setup compatibility matrix."""
    return _get_matrix().full_matrix()


@router.get("/matrix/regime")
async def matrix_regime(regime: str) -> Dict[str, Any]:
    """Cells for one regime."""
    return _get_matrix().get_regime_report(regime)


@router.get("/matrix/best")
async def matrix_best(regime: str, top_n: int = 5) -> Any:
    """Top N setups for a specific regime."""
    return _get_matrix().get_best_setups(regime, top_n=top_n)


# ═══════════════════════════════════════════════════════════════
#  6. PERSISTENCE
# ═══════════════════════════════════════════════════════════════
@router.get("/store/transitions")
async def store_transitions(limit: int = 50) -> Any:
    """Recent state transition events."""
    return _get_store().load_transitions(limit=limit)


@router.post("/store/compact")
async def store_compact() -> Dict[str, str]:
    """Compact persisted profiles (rewrite, deduplicate)."""
    lifecycle = _get_lifecycle()
    matrix_obj = _get_matrix()
    store = _get_store()

    profiles_data = {k: v.to_dict() for k, v in lifecycle.get_all_profiles().items()}
    store.flush_profiles(profiles_data)

    matrix_data = {}
    for (r, sk), cell in matrix_obj._grid.items():
        matrix_data[f"{r}::{sk}"] = cell.to_dict()
    store.flush_matrix(matrix_data)

    return {"status": "compacted", "profiles": str(len(profiles_data)), "matrix_cells": str(len(matrix_data))}
