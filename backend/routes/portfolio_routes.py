"""
Portfolio Intelligence Layer API Routes — FastAPI Endpoints
══════════════════════════════════════════════════════════

REST endpoints for the portfolio intelligence layer:
  - Portfolio allocation
  - Correlation risk analysis
  - Drawdown governance
  - Capital efficiency scoring
  - Conflict resolution
  - Stress testing
  - Full pipeline evaluation

Router prefix: /api/portfolio
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from backend.services.trading.portfolio_models import SetupOpportunity
from backend.services.trading.portfolio_allocator import PortfolioAllocator
from backend.services.trading.correlation_risk import CorrelationRiskEngine
from backend.services.trading.portfolio_drawdown_governor import PortfolioDrawdownGovernor
from backend.services.trading.capital_efficiency import CapitalEfficiencyScorer
from backend.services.trading.conflict_resolution import ConflictResolutionEngine
from backend.services.trading.portfolio_stress_tester import PortfolioStressTester
from backend.services.trading.portfolio_store import PortfolioStore

logger = logging.getLogger("smarttrade.portfolio.routes")

router = APIRouter(prefix="/api/portfolio", tags=["Portfolio Intelligence"])


# ═══════════════════════════════════════════════════════════════
#  SINGLETON INSTANCES
# ═══════════════════════════════════════════════════════════════

_allocator: Optional[PortfolioAllocator] = None
_correlation: Optional[CorrelationRiskEngine] = None
_drawdown: Optional[PortfolioDrawdownGovernor] = None
_efficiency: Optional[CapitalEfficiencyScorer] = None
_conflict: Optional[ConflictResolutionEngine] = None
_stress: Optional[PortfolioStressTester] = None
_store: Optional[PortfolioStore] = None


def _get_allocator() -> PortfolioAllocator:
    global _allocator
    if _allocator is None:
        _allocator = PortfolioAllocator()
    return _allocator


def _get_correlation() -> CorrelationRiskEngine:
    global _correlation
    if _correlation is None:
        _correlation = CorrelationRiskEngine()
    return _correlation


def _get_drawdown() -> PortfolioDrawdownGovernor:
    global _drawdown
    if _drawdown is None:
        _drawdown = PortfolioDrawdownGovernor()
    return _drawdown


def _get_efficiency() -> CapitalEfficiencyScorer:
    global _efficiency
    if _efficiency is None:
        _efficiency = CapitalEfficiencyScorer()
    return _efficiency


def _get_conflict() -> ConflictResolutionEngine:
    global _conflict
    if _conflict is None:
        _conflict = ConflictResolutionEngine()
    return _conflict


def _get_stress() -> PortfolioStressTester:
    global _stress
    if _stress is None:
        _stress = PortfolioStressTester()
    return _stress


def _get_store() -> PortfolioStore:
    global _store
    if _store is None:
        _store = PortfolioStore()
    return _store


def _parse_opportunity(d: Dict[str, Any]) -> SetupOpportunity:
    return SetupOpportunity(
        setup_key=str(d.get("setup_key", "")),
        symbol=str(d.get("symbol", "")),
        direction=str(d.get("direction", "")),
        adjusted_edge=float(d.get("adjusted_edge", 0)),
        robustness_score=float(d.get("robustness_score", 0)),
        regime_fit=float(d.get("regime_fit", 0)),
        execution_quality=float(d.get("execution_quality", 1)),
        requested_allocation_pct=float(d.get("requested_allocation_pct", 0)),
        capital_required=float(d.get("capital_required", 0)),
    )


# ═══════════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.post("/allocate", summary="Allocate capital across setups")
async def allocate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Full portfolio allocation from a list of opportunities."""
    try:
        opps = [_parse_opportunity(o) for o in payload.get("opportunities", [])]
        total_capital = float(payload.get("total_capital", 10000))
        dd_mult = float(payload.get("drawdown_multiplier", 1.0))
        corr_cuts = payload.get("correlation_haircuts", {})
        stress_cuts = payload.get("stress_haircuts", {})

        snapshot = _get_allocator().allocate(
            opps, total_capital, dd_mult, corr_cuts, stress_cuts,
        )

        _get_store().save_allocation(snapshot.to_dict())
        return snapshot.to_dict()
    except Exception as e:
        logger.error("allocate error: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/correlation", summary="Analyze correlation risk")
async def correlation_analysis(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Detect correlated exposure and compute haircuts."""
    try:
        engine = _get_correlation()

        # Feed return series
        returns_data = payload.get("returns", {})
        for sym, rets in returns_data.items():
            engine.update_returns(sym, rets)

        positions = payload.get("positions", {})
        symbol_map = payload.get("symbol_map", {})

        report = engine.analyze(positions, symbol_map)
        _get_store().save_correlation(report.to_dict())
        return report.to_dict()
    except Exception as e:
        logger.error("correlation error: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/drawdown/update", summary="Update drawdown state")
async def update_drawdown(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Update portfolio equity and get drawdown governance state."""
    try:
        equity = float(payload.get("current_equity", 0))
        if equity <= 0:
            raise ValueError("current_equity must be > 0")

        state = _get_drawdown().update(equity)
        _get_store().save_drawdown(state.to_dict())
        return state.to_dict()
    except Exception as e:
        logger.error("drawdown error: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/drawdown", summary="Get current drawdown state")
async def get_drawdown() -> Dict[str, Any]:
    return _get_drawdown().get_state().to_dict()


@router.post("/efficiency", summary="Score capital efficiency")
async def score_efficiency(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Score one setup's capital efficiency."""
    try:
        setup_key = str(payload.get("setup_key", ""))
        pnls = [float(p) for p in payload.get("pnls", [])]
        cap = float(payload.get("capital_per_trade", 1.0))
        mdd = float(payload.get("max_drawdown", 0.05))

        score = _get_efficiency().score(setup_key, pnls, cap, mdd)
        _get_store().save_efficiency(score.to_dict())
        return score.to_dict()
    except Exception as e:
        logger.error("efficiency error: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/conflict", summary="Resolve capital conflicts")
async def resolve_conflict(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve conflicts among competing setups."""
    try:
        opps = [_parse_opportunity(o) for o in payload.get("opportunities", [])]
        budget = float(payload.get("available_budget_pct", 0.5))
        div_vals = payload.get("diversification_values", {})

        report = _get_conflict().resolve(opps, budget, div_vals)
        _get_store().save_conflict(report.to_dict())
        return report.to_dict()
    except Exception as e:
        logger.error("conflict error: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/stress", summary="Stress test a setup")
async def stress_test(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run stress scenarios on one setup."""
    try:
        opp = _parse_opportunity(payload.get("opportunity", {}))
        exec_cost = float(payload.get("execution_cost", 0))

        report = _get_stress().stress_test_setup(opp, exec_cost)
        _get_store().save_stress(report.to_dict())
        return report.to_dict()
    except Exception as e:
        logger.error("stress error: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/stress/portfolio", summary="Stress test full portfolio")
async def stress_portfolio(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run stress on all setups and get haircuts."""
    try:
        opps = [_parse_opportunity(o) for o in payload.get("opportunities", [])]
        exec_costs = payload.get("execution_costs", {})

        reports = _get_stress().stress_test_portfolio(opps, exec_costs)
        haircuts = _get_stress().get_stress_haircuts(reports)

        for sk, r in reports.items():
            _get_store().save_stress(r.to_dict())

        return {
            "reports": {k: v.to_dict() for k, v in reports.items()},
            "haircuts": haircuts,
        }
    except Exception as e:
        logger.error("stress portfolio error: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/pipeline", summary="Full portfolio intelligence pipeline")
async def full_pipeline(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run the complete portfolio intelligence pipeline:
    1. Update drawdown → get multiplier
    2. Analyze correlations → get haircuts
    3. Stress test → get haircuts
    4. Resolve conflicts
    5. Allocate capital
    """
    try:
        opps = [_parse_opportunity(o) for o in payload.get("opportunities", [])]
        total_capital = float(payload.get("total_capital", 10000))
        current_equity = float(payload.get("current_equity", total_capital))
        returns_data = payload.get("returns", {})
        exec_costs = payload.get("execution_costs", {})

        # 1. Drawdown
        dd_state = _get_drawdown().update(current_equity)

        # 2. Correlation
        corr_engine = _get_correlation()
        for sym, rets in returns_data.items():
            corr_engine.update_returns(sym, rets)
        positions = {o.setup_key: o.requested_allocation_pct for o in opps}
        symbol_map = {o.setup_key: o.symbol for o in opps}
        corr_report = corr_engine.analyze(positions, symbol_map)

        # 3. Stress
        stress_reports = _get_stress().stress_test_portfolio(opps, exec_costs)
        stress_haircuts = _get_stress().get_stress_haircuts(stress_reports)

        # 4. Conflict resolution (if budget constrained)
        budget = 0.90 * dd_state.allocation_multiplier
        conflict_report = _get_conflict().resolve(opps, budget)

        # 5. Final allocation
        snapshot = _get_allocator().allocate(
            opps, total_capital,
            drawdown_multiplier=dd_state.allocation_multiplier,
            correlation_haircuts=corr_report.haircut_map,
            stress_haircuts=stress_haircuts,
        )

        # Persist
        store = _get_store()
        store.save_drawdown(dd_state.to_dict())
        store.save_correlation(corr_report.to_dict())
        store.save_conflict(conflict_report.to_dict())
        store.save_allocation(snapshot.to_dict())

        return {
            "drawdown": dd_state.to_dict(),
            "correlation": corr_report.to_dict(),
            "stress_haircuts": stress_haircuts,
            "conflicts": conflict_report.to_dict(),
            "allocation": snapshot.to_dict(),
        }
    except Exception as e:
        logger.error("pipeline error: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/history/{data_type}", summary="Load historical portfolio data")
async def get_history(
    data_type: str,
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """Retrieve persisted portfolio data."""
    store = _get_store()
    loaders = {
        "allocations": store.load_allocations,
        "correlations": store.load_correlations,
        "drawdowns": store.load_drawdowns,
        "conflicts": store.load_conflicts,
        "stress": store.load_stress_tests,
        "efficiency": store.load_efficiency,
    }
    loader = loaders.get(data_type)
    if not loader:
        raise HTTPException(status_code=400, detail=f"Unknown data_type: {data_type}")

    data = loader(limit)
    return {"data_type": data_type, "count": len(data), "items": data}
