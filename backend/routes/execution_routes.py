"""
Execution Reality Layer API Routes — FastAPI Endpoints
═════════════════════════════════════════════════════

REST endpoints for the execution reality layer:
  - Execution quality scoring
  - Slippage profiling
  - Capacity stress analysis
  - Execution-adjusted edge
  - Fill reliability monitoring
  - Full execution gate evaluation

Router prefix: /api/execution
"""

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from backend.services.trading.execution_models import (
    ExecutionRecord,
    FillStatus,
)
from backend.services.trading.execution_quality import ExecutionQualityAnalyzer
from backend.services.trading.slippage_profile import SlippageProfileEngine
from backend.services.trading.capacity_stress import CapacityStressAnalyzer
from backend.services.trading.execution_adjusted_edge import ExecutionAdjustedEdgeEngine
from backend.services.trading.fill_reliability import FillReliabilityMonitor
from backend.services.trading.execution_reality_gate import ExecutionRealityGate
from backend.services.trading.execution_store import ExecutionStore

logger = logging.getLogger("smarttrade.execution.routes")

router = APIRouter(prefix="/api/execution", tags=["Execution Reality"])


# ═══════════════════════════════════════════════════════════════
#  SINGLETON INSTANCES
# ═══════════════════════════════════════════════════════════════

_quality: Optional[ExecutionQualityAnalyzer] = None
_slippage: Optional[SlippageProfileEngine] = None
_capacity: Optional[CapacityStressAnalyzer] = None
_edge: Optional[ExecutionAdjustedEdgeEngine] = None
_reliability: Optional[FillReliabilityMonitor] = None
_gate: Optional[ExecutionRealityGate] = None
_store: Optional[ExecutionStore] = None


def _get_quality() -> ExecutionQualityAnalyzer:
    global _quality
    if _quality is None:
        _quality = ExecutionQualityAnalyzer()
    return _quality


def _get_slippage() -> SlippageProfileEngine:
    global _slippage
    if _slippage is None:
        _slippage = SlippageProfileEngine()
    return _slippage


def _get_capacity() -> CapacityStressAnalyzer:
    global _capacity
    if _capacity is None:
        _capacity = CapacityStressAnalyzer()
    return _capacity


def _get_edge() -> ExecutionAdjustedEdgeEngine:
    global _edge
    if _edge is None:
        _edge = ExecutionAdjustedEdgeEngine()
    return _edge


def _get_reliability() -> FillReliabilityMonitor:
    global _reliability
    if _reliability is None:
        _reliability = FillReliabilityMonitor()
    return _reliability


def _get_gate() -> ExecutionRealityGate:
    global _gate
    if _gate is None:
        _gate = ExecutionRealityGate()
    return _gate


def _get_store() -> ExecutionStore:
    global _store
    if _store is None:
        _store = ExecutionStore()
    return _store


# ═══════════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.post("/record", summary="Record a trade execution")
async def record_execution(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Ingest a single trade execution record and return quality score."""
    try:
        rec = ExecutionRecord(
            setup_key=str(payload.get("setup_key", "")),
            symbol=str(payload.get("symbol", "")),
            regime=str(payload.get("regime", "unknown")),
            session_hour=int(payload.get("session_hour", 0)),
            expected_entry=float(payload.get("expected_entry", 0)),
            realized_entry=float(payload.get("realized_entry", 0)),
            expected_exit=float(payload.get("expected_exit", 0)),
            realized_exit=float(payload.get("realized_exit", 0)),
            spread_paid=float(payload.get("spread_paid", 0)),
            intended_size=float(payload.get("intended_size", 0)),
            filled_size=float(payload.get("filled_size", 0)),
            fill_status=FillStatus(payload.get("fill_status", "full")),
            order_latency_ms=float(payload.get("order_latency_ms", 0)),
        )

        analyzer = _get_quality()
        rec = analyzer.record(rec)
        score = analyzer.score_trade(rec)

        # Persist
        _get_store().save_record(rec.to_dict())

        return {
            "status": "ok",
            "record": rec.to_dict(),
            "quality_score": score.to_dict(),
        }
    except Exception as e:
        logger.error("record_execution error: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/quality/{setup_key}", summary="Get execution quality for a setup")
async def get_quality(setup_key: str) -> Dict[str, Any]:
    """Return aggregate execution quality score for a setup."""
    score = _get_quality().score_setup(setup_key)
    return score.to_dict()


@router.get("/slippage/{setup_key}", summary="Get slippage profile for a setup")
async def get_slippage_profile(
    setup_key: str,
    raw_expectancy: float = Query(0.0, description="Raw expectancy for edge-kill detection"),
) -> Dict[str, Any]:
    """Build and return slippage profile across all dimensions."""
    analyzer = _get_quality()
    slippage = _get_slippage()

    records = analyzer.get_records(setup_key)
    if not records:
        raise HTTPException(status_code=404, detail="No execution records found")

    slippage.ingest(records)
    profile = slippage.build_profile(setup_key, raw_expectancy)

    # Persist snapshot
    _get_store().save_slippage_profile(profile.to_dict())

    return profile.to_dict()


@router.get("/capacity/{setup_key}", summary="Capacity stress analysis")
async def get_capacity(setup_key: str) -> Dict[str, Any]:
    """Analyze how execution degrades with position size."""
    records = _get_quality().get_records(setup_key)
    if not records:
        raise HTTPException(status_code=404, detail="No execution records found")

    report = _get_capacity().analyze(setup_key, records)

    _get_store().save_capacity_report(report.to_dict())

    return report.to_dict()


@router.get("/adjusted-edge/{setup_key}", summary="Execution-adjusted edge")
async def get_adjusted_edge(setup_key: str) -> Dict[str, Any]:
    """Compute edge after execution costs."""
    records = _get_quality().get_records(setup_key)
    if not records:
        raise HTTPException(status_code=404, detail="No execution records found")

    report = _get_edge().evaluate(setup_key, records)

    _get_store().save_adjusted_edge(report.to_dict())

    return report.to_dict()


@router.get("/reliability/{setup_key}", summary="Fill reliability report")
async def get_reliability(setup_key: str) -> Dict[str, Any]:
    """Monitor fill reliability for a setup."""
    records = _get_quality().get_records(setup_key)
    if not records:
        raise HTTPException(status_code=404, detail="No execution records found")

    report = _get_reliability().evaluate(setup_key, records)

    return report.to_dict()


@router.get("/gate/{setup_key}", summary="Full execution reality gate")
async def evaluate_gate(setup_key: str) -> Dict[str, Any]:
    """Run full execution reality gate evaluation."""
    analyzer = _get_quality()
    records = analyzer.get_records(setup_key)
    if not records:
        raise HTTPException(status_code=404, detail="No execution records found")

    quality = analyzer.score_setup(setup_key)
    edge = _get_edge().evaluate(setup_key, records)
    reliability = _get_reliability().evaluate(setup_key, records)
    capacity = _get_capacity().analyze(setup_key, records)

    verdict = _get_gate().evaluate(
        setup_key=setup_key,
        quality=quality,
        edge=edge,
        reliability=reliability,
        capacity=capacity,
    )

    _get_store().save_gate_verdict(verdict.to_dict())

    return {
        "verdict": verdict.to_dict(),
        "quality": quality.to_dict(),
        "edge": edge.to_dict(),
        "reliability": reliability.to_dict(),
        "capacity": capacity.to_dict(),
    }


@router.get("/history/{setup_key}", summary="Execution history for a setup")
async def get_history(
    setup_key: str,
    data_type: str = Query("records", description="records|slippage|capacity|edge|verdicts"),
) -> Dict[str, Any]:
    """Retrieve persisted execution data."""
    store = _get_store()
    loaders = {
        "records": store.load_records,
        "slippage": store.load_slippage_profiles,
        "capacity": store.load_capacity_reports,
        "edge": store.load_adjusted_edges,
        "verdicts": store.load_gate_verdicts,
    }
    loader = loaders.get(data_type)
    if not loader:
        raise HTTPException(status_code=400, detail=f"Unknown data_type: {data_type}")

    data = loader(setup_key)
    return {"setup_key": setup_key, "data_type": data_type, "count": len(data), "items": data}


@router.get("/dashboard", summary="Execution reality dashboard")
async def dashboard() -> Dict[str, Any]:
    """Overview of execution quality across all setups."""
    analyzer = _get_quality()
    all_records = analyzer.all_records()

    # Group by setup
    setups: Dict[str, list] = {}
    for r in all_records:
        setups.setdefault(r.setup_key, []).append(r)

    summary = []
    for sk, recs in setups.items():
        quality = analyzer.score_setup(sk)
        edge = _get_edge().evaluate(sk, recs)
        reliability = _get_reliability().evaluate(sk, recs)
        summary.append({
            "setup_key": sk,
            "trade_count": len(recs),
            "quality_score": quality.quality_score,
            "adjusted_expectancy": edge.adjusted_expectancy,
            "edge_degradation": edge.edge_degradation,
            "reliability_level": reliability.level.value,
        })

    summary.sort(key=lambda x: x["quality_score"])

    return {
        "total_setups": len(summary),
        "total_records": len(all_records),
        "setups": summary,
    }
