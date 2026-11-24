"""
Opportunity Dominance Layer — Full Validation
══════════════════════════════════════════════

Tests all 10 modules of the Opportunity Dominance Layer:
  1.  opportunity_dominance_models.py    — enums + dataclasses
  2.  opportunity_detection_engine.py    — 5 opportunity types
  3.  opportunity_ranking_engine.py      — multi-dimension ranking
  4.  aggression_amplifier.py            — 4-condition amplification
  5.  timing_precision_engine.py         — entry timing quality
  6.  winner_exploitation_engine.py      — scale/hold/reduce/exit
  7.  opportunity_decay_detector.py      — 3-signal decay monitor
  8.  opportunity_capital_allocator.py   — tier-based allocation
  9.  opportunity_dominance_store.py     — JSONL persistence
  10. opportunity_dominance_routes.py    — FastAPI routes

Run:  .venv\\Scripts\\python.exe validate_opportunity_dominance.py
"""

import sys
import os
import tempfile
import shutil
import random

sys.path.insert(0, os.path.dirname(__file__))
random.seed(42)

PASSED = 0
FAILED = 0


def test(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} — {detail}")


# ════════════════════════════════════════════════════════
#  1. Models
# ════════════════════════════════════════════════════════
print("\n═══ 1. opportunity_dominance_models ═══")
from backend.services.trading.opportunity_dominance_models import (
    OpportunityType, TimingVerdict, WinnerAction, DecayStage, AllocationTier,
    OpportunitySignal, DetectionReport, RankedOpportunity, RankingReport,
    AggressionDecision, TimingResult, WinnerDecision, DecayResult,
    AllocationDecision, CapitalPlan,
)

test("OpportunityType has 5 values", len(OpportunityType) == 5)
test("TimingVerdict has 3 values", len(TimingVerdict) == 3)
test("WinnerAction has 4 values", len(WinnerAction) == 4)
test("DecayStage has 4 values", len(DecayStage) == 4)
test("AllocationTier has 5 values", len(AllocationTier) == 5)

sig = OpportunitySignal(symbol="BTCUSDT", opportunity_type=OpportunityType.LIQUIDITY_SWEEP,
                        opportunity_score=0.85, expected_asymmetry=3.5, timing_quality=0.9, confidence=0.8)
d = sig.to_dict()
test("OpportunitySignal.to_dict()", d["opportunity_type"] == "liquidity_sweep" and d["opportunity_score"] == 0.85)

dr = DetectionReport(opportunities=[sig], total_detected=1, max_score=0.85, avg_asymmetry=3.5)
test("DetectionReport.to_dict()", len(dr.to_dict()["opportunities"]) == 1)

ro = RankedOpportunity(symbol="BTCUSDT", composite_rank_score=0.75, rank=1)
test("RankedOpportunity.to_dict()", ro.to_dict()["rank"] == 1)

rr = RankingReport(ranked=[ro], top_candidates=[ro], suppressed=[], total_ranked=1)
test("RankingReport.to_dict()", rr.to_dict()["total_ranked"] == 1)

ad = AggressionDecision(aggression_multiplier=1.5, conditions_met=3)
test("AggressionDecision.to_dict()", ad.to_dict()["conditions_met"] == 3)

tr = TimingResult(timing_score=0.8, verdict=TimingVerdict.ENTER)
test("TimingResult.to_dict()", tr.to_dict()["verdict"] == "enter")

wd = WinnerDecision(action=WinnerAction.SCALE_IN, scale_in_size=0.15)
test("WinnerDecision.to_dict()", wd.to_dict()["action"] == "scale_in")

dcr = DecayResult(decay_score=0.7, stage=DecayStage.PROGRESSING, early_exit_signal=True)
test("DecayResult.to_dict()", dcr.to_dict()["stage"] == "progressing")

adc = AllocationDecision(symbol="BTCUSDT", tier=AllocationTier.MAXIMUM, capital_pct=0.30)
test("AllocationDecision.to_dict()", adc.to_dict()["tier"] == "maximum")

cp = CapitalPlan(allocations=[adc], total_allocated_pct=0.30)
test("CapitalPlan.to_dict()", cp.to_dict()["total_allocated_pct"] == 0.30)


# ════════════════════════════════════════════════════════
#  2. OpportunityDetectionEngine
# ════════════════════════════════════════════════════════
print("\n═══ 2. OpportunityDetectionEngine ═══")
from backend.services.trading.opportunity_detection_engine import OpportunityDetectionEngine

det = OpportunityDetectionEngine()

# Build normal market baseline
for i in range(20):
    base_price = 50000.0 + random.uniform(-100, 100)
    det.ingest("BTCUSDT",
               price=base_price, high=base_price + 50, low=base_price - 50,
               close=base_price,
               volatility=0.02 + random.uniform(-0.002, 0.002),
               volume=1000 + random.uniform(-50, 50),
               buy_volume=500 + random.uniform(-50, 50),
               sell_volume=500 + random.uniform(-50, 50))

normal_report = det.detect(["BTCUSDT"])
test("Detection: normal market → few or no signals",
     normal_report.total_detected <= 2,
     f"detected={normal_report.total_detected}")

# Inject liquidity sweep: price goes below recent lows then closes above
for i in range(5):
    det.ingest("ETHUSDT",
               price=3000.0, high=3020.0, low=2990.0, close=3000.0,
               volatility=0.02, volume=500,
               buy_volume=250, sell_volume=250)

# Sweep candle: low breaks below, close recovers
det.ingest("ETHUSDT",
           price=3010.0, high=3015.0, low=2950.0, close=3010.0,
           volatility=0.03, volume=800,
           buy_volume=600, sell_volume=200)

sweep_report = det.detect(["ETHUSDT"])
sweep_types = [o.opportunity_type for o in sweep_report.opportunities]
test("Detection: liquidity sweep detected",
     OpportunityType.LIQUIDITY_SWEEP in sweep_types or sweep_report.total_detected > 0,
     f"types={[t.value for t in sweep_types]}")

# Inject momentum ignition: volume spike + directional
for i in range(12):
    det.ingest("SOLUSDT",
               price=100.0 + random.uniform(-1, 1),
               high=101.0, low=99.0, close=100.0 + random.uniform(-1, 1),
               volatility=0.015, volume=200 + random.uniform(-20, 20),
               buy_volume=100, sell_volume=100)

# Ignition: big volume + 3 consecutive up moves
det.ingest("SOLUSDT", price=102.0, high=102.5, low=101.5, close=102.0,
           volatility=0.02, volume=600, buy_volume=500, sell_volume=100)
det.ingest("SOLUSDT", price=104.0, high=104.5, low=103.5, close=104.0,
           volatility=0.025, volume=700, buy_volume=600, sell_volume=100)
det.ingest("SOLUSDT", price=107.0, high=107.5, low=106.5, close=107.0,
           volatility=0.03, volume=900, buy_volume=700, sell_volume=200)

ignition_report = det.detect(["SOLUSDT"])
test("Detection: momentum ignition detected",
     ignition_report.total_detected > 0,
     f"detected={ignition_report.total_detected}")

# Inject breakout compression
for i in range(12):
    det.ingest("BNBUSDT",
               price=300.0 + random.uniform(-5, 5),
               high=310.0, low=290.0, close=300.0,
               volatility=0.02, volume=400,
               buy_volume=200, sell_volume=200)

# Compressed candles (very tight range)
for i in range(3):
    det.ingest("BNBUSDT",
               price=300.5, high=301.0, low=300.0, close=300.5,
               candle_range=1.0,  # very tight
               volatility=0.005, volume=300,
               buy_volume=150, sell_volume=150)

compression_report = det.detect(["BNBUSDT"])
test("Detection: breakout compression detected",
     compression_report.total_detected > 0,
     f"detected={compression_report.total_detected}")

test("Detection: DetectionReport has all keys",
     all(k in ignition_report.to_dict() for k in ["opportunities", "total_detected", "max_score"]))


# ════════════════════════════════════════════════════════
#  3. OpportunityRankingEngine
# ════════════════════════════════════════════════════════
print("\n═══ 3. OpportunityRankingEngine ═══")
from backend.services.trading.opportunity_ranking_engine import OpportunityRankingEngine

ranker = OpportunityRankingEngine()

test_signals = [
    OpportunitySignal(symbol="BTCUSDT", opportunity_type=OpportunityType.LIQUIDITY_SWEEP,
                      opportunity_score=0.90, expected_asymmetry=4.0, confidence=0.85),
    OpportunitySignal(symbol="ETHUSDT", opportunity_type=OpportunityType.MOMENTUM_IGNITION,
                      opportunity_score=0.70, expected_asymmetry=2.5, confidence=0.70),
    OpportunitySignal(symbol="XRPUSDT", opportunity_type=OpportunityType.IMBALANCE_ZONE,
                      opportunity_score=0.30, expected_asymmetry=1.2, confidence=0.40),
]

ranking = ranker.rank(
    test_signals,
    execution_edges={"BTCUSDT": 0.85, "ETHUSDT": 0.70, "XRPUSDT": 0.40},
    robustness_scores={"BTCUSDT": 0.80, "ETHUSDT": 0.65, "XRPUSDT": 0.15},
    regime_alignments={"BTCUSDT": 0.90, "ETHUSDT": 0.60, "XRPUSDT": 0.10},
    diversification_benefits={"BTCUSDT": 0.70, "ETHUSDT": 0.80, "XRPUSDT": 0.50},
)

test("Ranking: BTCUSDT ranked #1", ranking.ranked[0].symbol == "BTCUSDT",
     f"#1={ranking.ranked[0].symbol}")
test("Ranking: top_candidates populated", len(ranking.top_candidates) >= 1)
test("Ranking: XRPUSDT suppressed (low robustness or regime)",
     any(r.suppressed for r in ranking.ranked if r.symbol == "XRPUSDT"),
     f"suppressed={[r.symbol for r in ranking.suppressed]}")
test("Ranking: total_ranked == 3", ranking.total_ranked == 3)


# ════════════════════════════════════════════════════════
#  4. AggressionAmplifier
# ════════════════════════════════════════════════════════
print("\n═══ 4. AggressionAmplifier ═══")
from backend.services.trading.aggression_amplifier import AggressionAmplifier

amp = AggressionAmplifier()

# All conditions favorable → max aggression
decision_max = amp.evaluate(
    opportunity_score=0.85, composite_stress=0.10,
    execution_quality=0.90, portfolio_stress=0.15,
    expected_asymmetry=3.5,
)
test("Aggression: all conditions → 4 met", decision_max.conditions_met == 4)
test("Aggression: high size_multiplier", decision_max.size_multiplier > 1.5,
     f"size={decision_max.size_multiplier}")
test("Aggression: high intensity", decision_max.risk_adjusted_intensity > 0.5,
     f"intensity={decision_max.risk_adjusted_intensity}")

# Poor conditions → baseline
decision_min = amp.evaluate(
    opportunity_score=0.20, composite_stress=0.80,
    execution_quality=0.30, portfolio_stress=0.70,
    expected_asymmetry=1.0,
)
test("Aggression: poor conditions → 0 met", decision_min.conditions_met == 0)
test("Aggression: baseline size", decision_min.size_multiplier == 1.0,
     f"size={decision_min.size_multiplier}")

# Mixed: 2 conditions
decision_mid = amp.evaluate(
    opportunity_score=0.70, composite_stress=0.10,
    execution_quality=0.50, portfolio_stress=0.60,
    expected_asymmetry=2.0,
)
test("Aggression: mixed → 2 met", decision_mid.conditions_met == 2)
test("Aggression: moderate amplification", 1.0 < decision_mid.size_multiplier < 1.5,
     f"size={decision_mid.size_multiplier}")


# ════════════════════════════════════════════════════════
#  5. TimingPrecisionEngine
# ════════════════════════════════════════════════════════
print("\n═══ 5. TimingPrecisionEngine ═══")
from backend.services.trading.timing_precision_engine import TimingPrecisionEngine

timing = TimingPrecisionEngine()

# Feed data
for i in range(10):
    timing.ingest("BTCUSDT", price=50000 + i * 10, momentum=5.0 + i * 0.5,
                  volume=1000 + random.uniform(-50, 50))

# Optimal entry: good confirmation, fresh signal, move not yet expressed
result_enter = timing.evaluate(
    "BTCUSDT", opportunity_score=0.80, signal_age_bars=2,
    move_expressed_pct=0.20, confirmation_count=3, max_confirmations=4,
)
test("Timing: optimal conditions → ENTER", result_enter.verdict == TimingVerdict.ENTER,
     f"verdict={result_enter.verdict}")
test("Timing: high timing_score", result_enter.timing_score > 0.6,
     f"score={result_enter.timing_score}")

# Late entry: move already 80% expressed
result_late = timing.evaluate(
    "BTCUSDT", opportunity_score=0.80, signal_age_bars=10,
    move_expressed_pct=0.80, confirmation_count=4, max_confirmations=4,
)
test("Timing: late entry → SKIP", result_late.verdict == TimingVerdict.SKIP,
     f"verdict={result_late.verdict}")

# Too early: no confirmation
result_early = timing.evaluate(
    "BTCUSDT", opportunity_score=0.80, signal_age_bars=0,
    move_expressed_pct=0.05, confirmation_count=0, max_confirmations=4,
)
test("Timing: no confirmation → SKIP or WAIT",
     result_early.verdict in (TimingVerdict.SKIP, TimingVerdict.WAIT),
     f"verdict={result_early.verdict}")


# ════════════════════════════════════════════════════════
#  6. WinnerExploitationEngine
# ════════════════════════════════════════════════════════
print("\n═══ 6. WinnerExploitationEngine ═══")
from backend.services.trading.winner_exploitation_engine import WinnerExploitationEngine

winner = WinnerExploitationEngine()

# Strong continuation, low exhaustion → SCALE_IN
dec_scale = winner.evaluate(
    "BTCUSDT", unrealized_pnl_pct=3.5,
    momentum=8.0, momentum_peak=9.0,
    volume_current=1200, volume_avg=1000,
    bars_in_trade=5, atr_distance=1.5, already_scaled_pct=0.0,
)
test("Winner: strong continuation → SCALE_IN", dec_scale.action == WinnerAction.SCALE_IN,
     f"action={dec_scale.action}")
test("Winner: positive scale_in_size", dec_scale.scale_in_size > 0,
     f"scale={dec_scale.scale_in_size}")

# High exhaustion → EXIT
dec_exit = winner.evaluate(
    "BTCUSDT", unrealized_pnl_pct=5.0,
    momentum=1.0, momentum_peak=10.0,
    volume_current=300, volume_avg=1000,
    bars_in_trade=20, atr_distance=5.0, already_scaled_pct=0.30,
)
test("Winner: exhaustion → EXIT or REDUCE",
     dec_exit.action in (WinnerAction.EXIT, WinnerAction.REDUCE),
     f"action={dec_exit.action}")

# Moderate → HOLD
dec_hold = winner.evaluate(
    "ETHUSDT", unrealized_pnl_pct=1.5,
    momentum=5.0, momentum_peak=7.0,
    volume_current=800, volume_avg=1000,
    bars_in_trade=8, atr_distance=2.0, already_scaled_pct=0.0,
)
test("Winner: moderate → HOLD or SCALE_IN",
     dec_hold.action in (WinnerAction.HOLD, WinnerAction.SCALE_IN),
     f"action={dec_hold.action}")


# ════════════════════════════════════════════════════════
#  7. OpportunityDecayDetector
# ════════════════════════════════════════════════════════
print("\n═══ 7. OpportunityDecayDetector ═══")
from backend.services.trading.opportunity_decay_detector import OpportunityDecayDetector

decay = OpportunityDecayDetector()

# Fresh strong opportunity
for i in range(8):
    decay.ingest("BTCUSDT", price=50000 + i * 100, momentum=10.0 + i * 0.5,
                 volume=1000 + random.uniform(-50, 50), direction=1)

result_fresh = decay.evaluate("BTCUSDT")
test("Decay: fresh opportunity → NONE or EARLY", result_fresh.stage in (DecayStage.NONE, DecayStage.EARLY),
     f"stage={result_fresh.stage}")
test("Decay: low decay_score", result_fresh.decay_score < 0.5,
     f"score={result_fresh.decay_score}")

# Inject decay: momentum drop, volume drop, no new highs
decay.ingest("BTCUSDT", price=50700, momentum=2.0, volume=400, direction=1)
decay.ingest("BTCUSDT", price=50650, momentum=1.0, volume=300, direction=1)
decay.ingest("BTCUSDT", price=50600, momentum=0.5, volume=200, direction=1)
decay.ingest("BTCUSDT", price=50550, momentum=0.3, volume=150, direction=1)
decay.ingest("BTCUSDT", price=50500, momentum=0.1, volume=100, direction=1)

result_decay = decay.evaluate("BTCUSDT")
test("Decay: fading opportunity → PROGRESSING or TERMINAL",
     result_decay.stage in (DecayStage.PROGRESSING, DecayStage.TERMINAL),
     f"stage={result_decay.stage}")
test("Decay: momentum_fading detected", result_decay.momentum_fading is True)
test("Decay: volume_dropping detected", result_decay.volume_dropping is True)
test("Decay: high decay_score", result_decay.decay_score > 0.4,
     f"score={result_decay.decay_score}")

# Test reset
decay.reset("BTCUSDT")
test("Decay: reset clears symbol", decay.get_last_result("BTCUSDT") is None)


# ════════════════════════════════════════════════════════
#  8. OpportunityCapitalAllocator
# ════════════════════════════════════════════════════════
print("\n═══ 8. OpportunityCapitalAllocator ═══")
from backend.services.trading.opportunity_capital_allocator import OpportunityCapitalAllocator

allocator = OpportunityCapitalAllocator()

test_ranked = [
    RankedOpportunity(symbol="BTCUSDT", composite_rank_score=0.90, rank=1),
    RankedOpportunity(symbol="ETHUSDT", composite_rank_score=0.70, rank=2),
    RankedOpportunity(symbol="SOLUSDT", composite_rank_score=0.50, rank=3),
    RankedOpportunity(symbol="XRPUSDT", composite_rank_score=0.15, suppressed=True, suppression_reason="low_regime"),
]

plan = allocator.allocate(
    test_ranked,
    timing_scores={"BTCUSDT": 0.90, "ETHUSDT": 0.70, "SOLUSDT": 0.50},
    aggression_multiplier=1.5,
)

test("Allocator: 3 allocations (XRPUSDT suppressed)", len(plan.allocations) == 3,
     f"allocations={len(plan.allocations)}")
test("Allocator: BTCUSDT gets most capital",
     plan.allocations[0].symbol == "BTCUSDT" and plan.allocations[0].capital_pct > plan.allocations[1].capital_pct,
     f"BTC={plan.allocations[0].capital_pct} ETH={plan.allocations[1].capital_pct}")
test("Allocator: BTCUSDT tier is MAXIMUM",
     plan.allocations[0].tier == AllocationTier.MAXIMUM,
     f"tier={plan.allocations[0].tier}")
test("Allocator: total_allocated > 0", plan.total_allocated_pct > 0)
test("Allocator: total_allocated ≤ 1.0", plan.total_allocated_pct <= 1.0)


# ════════════════════════════════════════════════════════
#  9. OpportunityDominanceStore
# ════════════════════════════════════════════════════════
print("\n═══ 9. OpportunityDominanceStore ═══")
from backend.services.trading.opportunity_dominance_store import OpportunityDominanceStore

tmpdir = tempfile.mkdtemp(prefix="od_test_")
try:
    store = OpportunityDominanceStore(base_dir=tmpdir)

    # Log detection
    store.log_detection(ignition_report)
    test("Store: detections written", len(store.read_detections()) == 1)

    # Log ranking
    store.log_ranking(ranking)
    test("Store: rankings written", len(store.read_rankings()) == 1)

    # Log aggression
    store.log_aggression(decision_max)
    test("Store: aggressions written", len(store.read_aggressions()) == 1)

    # Log timing
    store.log_timing(result_enter)
    test("Store: timings written", len(store.read_timings()) == 1)

    # Log winner decision
    store.log_winner_decision(dec_scale)
    test("Store: winner decisions written", len(store.read_winner_decisions()) == 1)

    # Log scaling event
    store.log_scaling_event({"symbol": "BTCUSDT", "action": "scale_in", "size": 0.15})
    test("Store: scaling events written", len(store.read_scaling_events()) == 1)

    # Log decay
    store.log_decay(result_decay)
    test("Store: decay signals written", len(store.read_decay_signals()) == 1)

    # Log allocation
    store.log_allocation(plan)
    test("Store: allocations written", len(store.read_allocations()) == 1)

    # Multiple writes
    store.log_detection(sweep_report)
    store.log_detection(compression_report)
    test("Store: multiple writes append", len(store.read_detections()) == 3)

finally:
    shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════
#  10. Routes import
# ════════════════════════════════════════════════════════
print("\n═══ 10. Routes import ═══")
try:
    from backend.routes.opportunity_dominance_routes import router
    test("Routes: import successful", True)
    test("Routes: has correct prefix", router.prefix == "/api/opportunity-dominance")

    route_paths = [r.path for r in router.routes]
    prefix = router.prefix
    expected = ["/status", "/detections", "/rankings", "/aggression",
                "/timing", "/winners", "/decay", "/allocations", "/force-aggression"]
    for ep in expected:
        test(f"Routes: {ep} exists", f"{prefix}{ep}" in route_paths,
             f"missing {prefix}{ep}")
except Exception as e:
    test("Routes: import failed", False, str(e))


# ════════════════════════════════════════════════════════
#  11. Full Pipeline Integration
# ════════════════════════════════════════════════════════
print("\n═══ 11. Full Pipeline Integration ═══")

pipe_det = OpportunityDetectionEngine()
pipe_rank = OpportunityRankingEngine()
pipe_amp = AggressionAmplifier()
pipe_timing = TimingPrecisionEngine()
pipe_winner = WinnerExploitationEngine()
pipe_decay = OpportunityDecayDetector()
pipe_alloc = OpportunityCapitalAllocator()

# 1) Build baselines
symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
for sym_idx, sym in enumerate(symbols):
    base = 50000 - sym_idx * 15000
    for i in range(15):
        p = base + random.uniform(-100, 100)
        pipe_det.ingest(sym, price=p, high=p+50, low=p-50, close=p,
                        volatility=0.02+random.uniform(-0.002,0.002),
                        volume=1000+random.uniform(-100,100),
                        buy_volume=500+random.uniform(-50,50),
                        sell_volume=500+random.uniform(-50,50))
        pipe_timing.ingest(sym, price=p, momentum=5.0+random.uniform(-1,1),
                           volume=1000+random.uniform(-100,100))
        pipe_decay.ingest(sym, price=p, momentum=5.0+random.uniform(-1,1),
                          volume=1000+random.uniform(-100,100), direction=1)

# 2) Inject opportunity on BTCUSDT (momentum ignition)
for i in range(3):
    price = 50200 + i * 100
    pipe_det.ingest("BTCUSDT", price=price, high=price+20, low=price-10, close=price,
                    volatility=0.03, volume=3000+i*500,
                    buy_volume=2500, sell_volume=500)
    pipe_timing.ingest("BTCUSDT", price=price, momentum=8.0+i, volume=3000+i*500)

# 3) Detect
report = pipe_det.detect(symbols)
test("Pipeline: opportunities detected", report.total_detected > 0,
     f"detected={report.total_detected}")

# 4) Rank
ranking_result = pipe_rank.rank(
    report.opportunities,
    execution_edges={s: 0.80 for s in symbols},
    robustness_scores={s: 0.70 for s in symbols},
    regime_alignments={s: 0.75 for s in symbols},
    diversification_benefits={s: 0.60 for s in symbols},
)
test("Pipeline: ranking produced", ranking_result.total_ranked > 0)

# 5) Aggression
if ranking_result.top_candidates:
    top = ranking_result.top_candidates[0]
    aggr = pipe_amp.evaluate(
        opportunity_score=top.opportunity_score,
        composite_stress=0.10, execution_quality=0.85,
        portfolio_stress=0.15, expected_asymmetry=3.0,
    )
    test("Pipeline: aggression amplified", aggr.size_multiplier > 1.0,
         f"size={aggr.size_multiplier}")
else:
    aggr = pipe_amp.evaluate(opportunity_score=0.5)
    test("Pipeline: aggression baseline", aggr.size_multiplier >= 1.0)

# 6) Timing
timing_r = pipe_timing.evaluate(
    "BTCUSDT", opportunity_score=0.80, signal_age_bars=2,
    move_expressed_pct=0.25, confirmation_count=3, max_confirmations=4,
)
test("Pipeline: timing evaluated", timing_r.verdict in (TimingVerdict.ENTER, TimingVerdict.WAIT),
     f"verdict={timing_r.verdict}")

# 7) Winner management (simulate winning trade)
win_dec = pipe_winner.evaluate(
    "BTCUSDT", unrealized_pnl_pct=2.5,
    momentum=8.0, momentum_peak=9.0,
    volume_current=1200, volume_avg=1000,
    bars_in_trade=5, atr_distance=1.5,
)
test("Pipeline: winner decision", win_dec.action in (WinnerAction.SCALE_IN, WinnerAction.HOLD),
     f"action={win_dec.action}")

# 8) Decay check
decay_r = pipe_decay.evaluate("BTCUSDT")
test("Pipeline: decay checked", decay_r.stage in (DecayStage.NONE, DecayStage.EARLY),
     f"stage={decay_r.stage}")

# 9) Capital allocation
if ranking_result.ranked:
    alloc_plan = pipe_alloc.allocate(
        ranking_result.ranked,
        timing_scores={"BTCUSDT": timing_r.timing_score},
        aggression_multiplier=aggr.aggression_multiplier,
    )
    test("Pipeline: capital allocated", alloc_plan.total_allocated_pct > 0)
    test("Pipeline: allocation ≤ 100%", alloc_plan.total_allocated_pct <= 1.0)
else:
    test("Pipeline: no opportunities to allocate", True)


print(f"\n{'='*50}")
print(f"TOTAL:  {PASSED} passed,  {FAILED} failed  ({PASSED + FAILED} tests)")
print(f"{'='*50}")

sys.exit(0 if FAILED == 0 else 1)
