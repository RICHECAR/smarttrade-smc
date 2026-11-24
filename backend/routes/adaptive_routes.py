"""
Adaptive Layer API Routes — FastAPI Endpoints
══════════════════════════════════════════════

REST endpoints for the adaptive alpha layer:
  - Regime detection state
  - Setup scoring (dry-run)
  - Strategy adaptation params
  - Trade journal queries + export
  - Meta-performance analysis
  - Adaptive policy status

Router prefix: /api/adaptive
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from backend.services.trading.adaptive_models import MarketRegime
from backend.services.trading.adaptive_policy import AdaptivePolicy
from backend.services.trading.meta_analyzer import MetaPerformanceAnalyzer
from backend.services.trading.models import (
    PerformanceSnapshot,
    SignalDirection,
    SignalStrength,
    TradeSignal,
)
from backend.services.trading.regime_detection import RegimeDetectionEngine
from backend.services.trading.setup_scoring import SetupScoringEngine
from backend.services.trading.strategy_adaptation import StrategyAdaptationEngine
from backend.services.trading.trade_journal import TradeJournalService

logger = logging.getLogger("smarttrade.adaptive.routes")

router = APIRouter(prefix="/api/adaptive", tags=["Adaptive Alpha"])


# ═══════════════════════════════════════════════════════════════
#  SINGLETON INSTANCES
# ═══════════════════════════════════════════════════════════════
_regime_engine: Optional[RegimeDetectionEngine] = None
_scorer: Optional[SetupScoringEngine] = None
_adapter: Optional[StrategyAdaptationEngine] = None
_journal: Optional[TradeJournalService] = None
_meta: Optional[MetaPerformanceAnalyzer] = None
_policy: Optional[AdaptivePolicy] = None


def get_regime_engine() -> RegimeDetectionEngine:
    global _regime_engine
    if _regime_engine is None:
        _regime_engine = RegimeDetectionEngine()
    return _regime_engine


def get_scorer() -> SetupScoringEngine:
    global _scorer
    if _scorer is None:
        _scorer = SetupScoringEngine()
    return _scorer


def get_adapter() -> StrategyAdaptationEngine:
    global _adapter
    if _adapter is None:
        _adapter = StrategyAdaptationEngine()
    return _adapter


def get_journal() -> TradeJournalService:
    global _journal
    if _journal is None:
        _journal = TradeJournalService()
        _journal.load_from_file()
    return _journal


def get_meta() -> MetaPerformanceAnalyzer:
    global _meta
    if _meta is None:
        _meta = MetaPerformanceAnalyzer()
    return _meta


def get_policy() -> AdaptivePolicy:
    global _policy
    if _policy is None:
        _policy = AdaptivePolicy(
            regime_engine=get_regime_engine(),
            scorer=get_scorer(),
            adapter=get_adapter(),
            meta_analyzer=get_meta(),
        )
    return _policy


# ═══════════════════════════════════════════════════════════════
#  REGIME ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.get("/regime/{symbol}")
async def get_regime(symbol: str):
    """Get current market regime classification for a symbol."""
    engine = get_regime_engine()
    snap = engine.detect(symbol)
    return {
        "success": True,
        "data": snap.to_dict(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════
#  SCORING ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.post("/score")
async def score_signal(
    symbol: str = Query(..., description="Trading symbol"),
    direction: str = Query(..., description="long or short"),
    confidence: float = Query(0.6, ge=0, le=1),
    risk_reward: float = Query(2.0, ge=0),
    strength: str = Query("moderate"),
):
    """Dry-run: score a hypothetical signal without executing."""
    signal = TradeSignal(
        symbol=symbol,
        direction=SignalDirection(direction),
        strength=SignalStrength(strength),
        confidence=confidence,
        risk_reward=risk_reward,
        entry_price=0,
        stop_loss=0,
        take_profit_1=0,
        take_profit_2=0,
        reasons=["manual_score_test"],
    )

    engine = get_regime_engine()
    regime = engine.detect(symbol)
    scorer = get_scorer()
    result = scorer.score(signal, regime)

    return {
        "success": True,
        "data": {
            "score": result.to_dict(),
            "regime": regime.to_dict(),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════
#  ADAPTATION ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.get("/adaptation")
async def get_current_adaptation(
    win_rate: float = Query(0.55, ge=0, le=1),
    drawdown: float = Query(0.0, ge=0),
    total_trades: int = Query(20, ge=0),
    consecutive_losses: int = Query(0, ge=0),
    consecutive_wins: int = Query(0, ge=0),
):
    """Compute adapted parameters for given performance state."""
    perf = PerformanceSnapshot(
        total_trades=total_trades,
        win_rate=win_rate,
        current_drawdown_pct=drawdown,
        consecutive_losses=consecutive_losses,
        consecutive_wins=consecutive_wins,
        profit_factor=1.5,
    )

    adapter = get_adapter()
    params = adapter.adapt(perf)

    return {
        "success": True,
        "data": params.to_dict(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════
#  JOURNAL ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.get("/journal")
async def get_journal_entries(
    symbol: Optional[str] = Query(None),
    closed_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
):
    """Query trade journal entries."""
    journal = get_journal()
    entries = journal.get_entries(symbol=symbol, closed_only=closed_only, limit=limit)
    return {
        "success": True,
        "data": entries,
        "count": len(entries),
        "total_entries": journal.entry_count,
        "open_entries": journal.open_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/journal/export/json")
async def export_journal_json():
    """Export full journal as JSON."""
    journal = get_journal()
    content = journal.export_json()
    return {
        "success": True,
        "entries": journal.entry_count,
        "data": content,
    }


@router.post("/journal/export/csv")
async def export_journal_csv(
    filepath: str = Query("data/trade_journal_export.csv"),
):
    """Export closed journal entries to CSV file."""
    journal = get_journal()
    journal.export_csv(filepath=filepath)
    return {
        "success": True,
        "message": f"Exported to {filepath}",
        "entries": journal.entry_count,
    }


# ═══════════════════════════════════════════════════════════════
#  META-ANALYSIS ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.get("/meta/report")
async def get_meta_report():
    """Full multi-dimensional performance analysis."""
    journal = get_journal()
    entries = journal.get_closed_entries(limit=500)

    if not entries:
        return {
            "success": True,
            "data": {"message": "No closed trades to analyze"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    meta = get_meta()
    report = meta.full_report(entries)

    return {
        "success": True,
        "data": report,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/meta/strongest")
async def get_strongest_conditions(top_n: int = Query(5, ge=1, le=20)):
    """Identify top-N conditions where the system has the most edge."""
    journal = get_journal()
    entries = journal.get_closed_entries()
    meta = get_meta()
    strongest = meta.strongest_conditions(entries, top_n=top_n)

    return {
        "success": True,
        "data": strongest,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/meta/weakest")
async def get_weakest_conditions(top_n: int = Query(5, ge=1, le=20)):
    """Identify top-N conditions where the system bleeds."""
    journal = get_journal()
    entries = journal.get_closed_entries()
    meta = get_meta()
    weakest = meta.weakest_conditions(entries, top_n=top_n)

    return {
        "success": True,
        "data": weakest,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════
#  POLICY ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.get("/policy/status")
async def get_policy_status():
    """Current adaptive policy state."""
    policy = get_policy()
    return {
        "success": True,
        "data": policy.status(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/policy/evaluate")
async def evaluate_policy(
    symbol: str = Query(...),
    direction: str = Query(...),
    confidence: float = Query(0.6, ge=0, le=1),
    risk_reward: float = Query(2.0, ge=0),
    strength: str = Query("moderate"),
    win_rate: float = Query(0.55, ge=0, le=1),
    drawdown: float = Query(0.0, ge=0),
    total_trades: int = Query(20, ge=0),
    consecutive_losses: int = Query(0, ge=0),
):
    """
    Dry-run: full adaptive policy evaluation for a hypothetical signal.
    Returns policy action + adapted parameters + regime + score.
    """
    signal = TradeSignal(
        symbol=symbol,
        direction=SignalDirection(direction),
        strength=SignalStrength(strength),
        confidence=confidence,
        risk_reward=risk_reward,
        entry_price=0,
        stop_loss=0,
        take_profit_1=0,
        take_profit_2=0,
        reasons=["policy_eval_test"],
    )

    perf = PerformanceSnapshot(
        total_trades=total_trades,
        win_rate=win_rate,
        current_drawdown_pct=drawdown,
        consecutive_losses=consecutive_losses,
        profit_factor=1.5,
    )

    policy = get_policy()
    journal = get_journal()
    journal_entries = journal.get_closed_entries()

    decision = policy.evaluate(signal, perf, journal_entries)

    return {
        "success": True,
        "data": decision.to_dict(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
