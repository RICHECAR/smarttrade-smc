"""
Robustness Layer API Routes — FastAPI Endpoints
═══════════════════════════════════════════════

REST endpoints for the statistical robustness layer:
  - Confidence scoring
  - Multi-window edge validation
  - Fragility analysis
  - Promotion gate check
  - Allocation stability
  - Research snapshots
  - Full pipeline evaluation

Router prefix: /api/robustness
"""

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from backend.services.trading.allocation_stability import AllocationStabilityGuard
from backend.services.trading.edge_fragility import EdgeFragilityAnalyzer
from backend.services.trading.multi_window_validator import MultiWindowValidator
from backend.services.trading.promotion_gate import PromotionGate
from backend.services.trading.research_snapshots import ResearchSnapshotService
from backend.services.trading.robustness_models import ResearchSnapshot
from backend.services.trading.statistical_confidence import StatisticalConfidenceEngine

logger = logging.getLogger("smarttrade.robustness.routes")

router = APIRouter(prefix="/api/robustness", tags=["Statistical Robustness"])


# ═══════════════════════════════════════════════════════════════
#  SINGLETON INSTANCES
# ═══════════════════════════════════════════════════════════════

_confidence: Optional[StatisticalConfidenceEngine] = None
_windows: Optional[MultiWindowValidator] = None
_fragility: Optional[EdgeFragilityAnalyzer] = None
_gate: Optional[PromotionGate] = None
_stability: Optional[AllocationStabilityGuard] = None
_snapshots: Optional[ResearchSnapshotService] = None


def _get_confidence() -> StatisticalConfidenceEngine:
    global _confidence
    if _confidence is None:
        _confidence = StatisticalConfidenceEngine()
    return _confidence


def _get_windows() -> MultiWindowValidator:
    global _windows
    if _windows is None:
        _windows = MultiWindowValidator()
    return _windows


def _get_fragility() -> EdgeFragilityAnalyzer:
    global _fragility
    if _fragility is None:
        _fragility = EdgeFragilityAnalyzer()
    return _fragility


def _get_gate() -> PromotionGate:
    global _gate
    if _gate is None:
        _gate = PromotionGate()
    return _gate


def _get_stability() -> AllocationStabilityGuard:
    global _stability
    if _stability is None:
        _stability = AllocationStabilityGuard()
    return _stability


def _get_snapshots() -> ResearchSnapshotService:
    global _snapshots
    if _snapshots is None:
        _snapshots = ResearchSnapshotService()
    return _snapshots


# ═══════════════════════════════════════════════════════════════
#  1. RECORD — Feed trade outcomes into all robustness subsystems
# ═══════════════════════════════════════════════════════════════
@router.post("/record")
async def record_outcome(
    setup_key: str,
    pnl: float,
    regime: str = "unknown",
    drawdown_contribution: float = 0.0,
) -> Dict[str, str]:
    """Feed a trade outcome into confidence, window, and fragility engines."""
    _get_confidence().record(setup_key, pnl)
    _get_windows().record(setup_key, pnl, regime=regime)
    _get_fragility().record(setup_key, pnl, regime=regime, drawdown_contribution=drawdown_contribution)
    return {"status": "recorded", "setup_key": setup_key}


# ═══════════════════════════════════════════════════════════════
#  2. CONFIDENCE — Statistical confidence scoring
# ═══════════════════════════════════════════════════════════════
@router.get("/confidence")
async def confidence(
    setup_key: str,
    win_rate: float,
    total_trades: int,
    profit_factor: float = 1.0,
) -> Dict[str, Any]:
    """Get statistical confidence verdict for a setup."""
    verdict = _get_confidence().evaluate(
        setup_key, win_rate=win_rate, total_trades=total_trades, profit_factor=profit_factor,
    )
    return verdict.to_dict()


# ═══════════════════════════════════════════════════════════════
#  3. MULTI-WINDOW — Cross-timeframe edge validation
# ═══════════════════════════════════════════════════════════════
@router.get("/windows")
async def multi_window(setup_key: str) -> Dict[str, Any]:
    """Multi-window edge trend analysis."""
    verdict = _get_windows().evaluate(setup_key)
    return verdict.to_dict()


# ═══════════════════════════════════════════════════════════════
#  4. FRAGILITY — Edge fragility analysis
# ═══════════════════════════════════════════════════════════════
@router.get("/fragility")
async def fragility(setup_key: str) -> Dict[str, Any]:
    """Edge fragility report for a setup."""
    report = _get_fragility().evaluate(setup_key)
    return report.to_dict()


# ═══════════════════════════════════════════════════════════════
#  5. PROMOTION GATE — Should this setup be promoted?
# ═══════════════════════════════════════════════════════════════
@router.get("/promotion_check")
async def promotion_check(
    setup_key: str,
    total_trades: int,
    win_rate: float,
    profit_factor: float = 1.0,
) -> Dict[str, Any]:
    """Full promotion gate check combining all robustness signals."""
    # Gather sub-verdicts
    conf = _get_confidence().evaluate(
        setup_key, win_rate=win_rate, total_trades=total_trades, profit_factor=profit_factor,
    )
    frag = _get_fragility().evaluate(setup_key)
    win_v = _get_windows().evaluate(setup_key)

    # Get recent PnLs for spike detection
    pnls = list(_get_confidence()._pnls.get(setup_key, []))

    verdict = _get_gate().evaluate(
        total_trades=total_trades,
        recent_pnls=pnls,
        confidence=conf,
        fragility=frag,
        windows=win_v,
    )
    return {
        "promotion": verdict.to_dict(),
        "confidence": conf.to_dict(),
        "fragility": frag.to_dict(),
        "windows": win_v.to_dict(),
    }


# ═══════════════════════════════════════════════════════════════
#  6. ALLOCATION STABILITY
# ═══════════════════════════════════════════════════════════════
@router.post("/stabilize_allocation")
async def stabilize_allocation(
    setup_key: str,
    raw_allocation: float,
) -> Dict[str, Any]:
    """Stabilize a raw allocation with hysteresis/step/cooldown."""
    result = _get_stability().stabilize(setup_key, raw_allocation)
    return result.to_dict()


@router.post("/stabilize_batch")
async def stabilize_batch(
    allocations: Dict[str, float],
) -> Dict[str, Any]:
    """Stabilize a batch of allocations."""
    results = _get_stability().stabilize_batch(allocations)
    return {k: v.to_dict() for k, v in results.items()}


@router.get("/stability_state")
async def stability_state() -> Dict[str, Any]:
    """Current allocation stability guard state."""
    return _get_stability().get_state()


# ═══════════════════════════════════════════════════════════════
#  7. FULL PIPELINE EVALUATE
# ═══════════════════════════════════════════════════════════════
@router.get("/evaluate")
async def full_evaluate(
    setup_key: str,
    win_rate: float,
    total_trades: int,
    profit_factor: float = 1.0,
    survival_score: float = 0.5,
    lifecycle_state: str = "emerging",
    regime_fit: float = 0.5,
    raw_allocation: float = 1.0,
) -> Dict[str, Any]:
    """
    Full robustness evaluation: confidence + windows + fragility + gate + stability.
    """
    conf = _get_confidence().evaluate(
        setup_key, win_rate=win_rate, total_trades=total_trades, profit_factor=profit_factor,
    )
    win_v = _get_windows().evaluate(setup_key)
    frag = _get_fragility().evaluate(setup_key)

    pnls = list(_get_confidence()._pnls.get(setup_key, []))
    promo = _get_gate().evaluate(
        total_trades=total_trades,
        recent_pnls=pnls,
        confidence=conf,
        fragility=frag,
        windows=win_v,
    )

    stab = _get_stability().stabilize(setup_key, raw_allocation)

    # Persist snapshot
    snapshot = ResearchSnapshot(
        setup_key=setup_key,
        timestamp=time.time(),
        lifecycle_state=lifecycle_state,
        survival_score=survival_score,
        confidence_score=conf.confidence_score,
        fragility_score=frag.fragility_score,
        edge_trend=win_v.edge_trend.value,
        robustness_score=promo.robustness_score,
        allocation_pct=stab.stabilized_allocation,
        regime_fit=regime_fit,
        total_trades=total_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy=0.0,
    )
    _get_snapshots().save_snapshot(snapshot)

    return {
        "confidence": conf.to_dict(),
        "windows": win_v.to_dict(),
        "fragility": frag.to_dict(),
        "promotion": promo.to_dict(),
        "allocation": stab.to_dict(),
    }


# ═══════════════════════════════════════════════════════════════
#  8. RESEARCH SNAPSHOTS
# ═══════════════════════════════════════════════════════════════
@router.get("/snapshots/recent")
async def snapshots_recent(limit: int = 50) -> Any:
    """Recent research snapshots."""
    return _get_snapshots().load_recent(limit=limit)


@router.get("/snapshots/setup")
async def snapshots_for_setup(setup_key: str, limit: int = 50) -> Any:
    """Snapshot history for one setup."""
    return _get_snapshots().load_for_setup(setup_key, limit=limit)


@router.get("/snapshots/latest")
async def snapshots_latest() -> Dict[str, Any]:
    """Latest snapshot per setup (dashboard view)."""
    return _get_snapshots().load_latest_per_setup()


@router.post("/snapshots/compact")
async def snapshots_compact() -> Dict[str, Any]:
    """Compact snapshot storage."""
    n = _get_snapshots().compact()
    return {"status": "compacted", "records": n}
