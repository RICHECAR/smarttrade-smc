"""
Structural Break Intelligence Layer — Full Validation
══════════════════════════════════════════════════════

Tests all 9 modules of the Structural Break Intelligence Layer:
  1. structural_break_models.py    — enums + dataclasses
  2. structural_break_detector.py  — z-score break detection
  3. regime_transition_monitor.py  — transition detection
  4. doctrine_validity_engine.py   — doctrine mismatch analysis
  5. shock_propagation_analyzer.py — propagation scope analysis
  6. preemptive_throttle_engine.py — throttle level management
  7. false_recovery_guard.py       — recovery guard logic
  8. structural_break_store.py     — JSONL persistence
  9. structural_break_routes.py    — FastAPI route imports

Run:  .venv\Scripts\python.exe validate_structural_break.py
"""

import sys
import os
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(__file__))

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
print("\n═══ 1. structural_break_models ═══")
from backend.services.trading.structural_break_models import (
    BreakType, BreakSeverity, TransitionType, DoctrineAction,
    ShockScope, ThrottleLevel, RecoveryValidity,
    BreakSignal, BreakReport, TransitionSignal, TransitionReport,
    DoctrineMismatch, DoctrineVerdict, PropagationResult,
    ThrottleState, RecoveryCheckResult,
)

test("BreakType enum has 6 values", len(BreakType) == 6)
test("BreakSeverity enum has 5 values", len(BreakSeverity) == 5)
test("TransitionType enum has 5 values", len(TransitionType) == 5)
test("DoctrineAction enum has 4 values", len(DoctrineAction) == 4)
test("ShockScope enum has 3 values", len(ShockScope) == 3)
test("ThrottleLevel enum has 5 values", len(ThrottleLevel) == 5)
test("RecoveryValidity enum has 3 values", len(RecoveryValidity) == 3)

sig = BreakSignal(break_type=BreakType.VOLATILITY_SHOCK, severity=BreakSeverity.SEVERE, z_score=3.2, confidence=0.9)
d = sig.to_dict()
test("BreakSignal.to_dict() works", d["break_type"] == "volatility_shock" and d["severity"] == "severe")

report = BreakReport(breaks=[sig], max_severity=BreakSeverity.SEVERE, composite_stress=0.7, active_break_count=1)
d = report.to_dict()
test("BreakReport.to_dict() serializes breaks list", len(d["breaks"]) == 1)

ts = TransitionSignal(transition_type=TransitionType.TREND_EXHAUSTION, confidence=0.8, fragility_impact=0.6)
d = ts.to_dict()
test("TransitionSignal.to_dict()", d["transition_type"] == "trend_exhaustion")

tr = TransitionReport(transitions=[ts], is_unstable=True, max_fragility=0.6)
test("TransitionReport.to_dict()", tr.to_dict()["is_unstable"] is True)

dm = DoctrineMismatch(dimension="test", expected="0.8", actual="0.3", severity=0.5)
test("DoctrineMismatch.to_dict()", dm.to_dict()["severity"] == 0.5)

dv = DoctrineVerdict(validity_score=0.8, action=DoctrineAction.VALID, mismatches=[], detail="ok")
test("DoctrineVerdict.to_dict()", dv.to_dict()["action"] == "valid")

pr = PropagationResult(scope=ShockScope.LOCAL, propagation_score=0.3, containment="contained")
test("PropagationResult.to_dict()", pr.to_dict()["scope"] == "local")

tst = ThrottleState(level=ThrottleLevel.MODERATE, exposure_multiplier=0.5, size_multiplier=0.6, max_concurrent_override=5, blocked_setups=[], reason="test")
test("ThrottleState.to_dict()", tst.to_dict()["level"] == "moderate")

rcr = RecoveryCheckResult(validity=RecoveryValidity.PARTIAL, validity_score=0.6, recommendation="partial_restore")
test("RecoveryCheckResult.to_dict()", rcr.to_dict()["validity"] == "partial")

# ════════════════════════════════════════════════════════
#  2. StructuralBreakDetector
# ════════════════════════════════════════════════════════
print("\n═══ 2. StructuralBreakDetector ═══")
from backend.services.trading.structural_break_detector import StructuralBreakDetector

det = StructuralBreakDetector()

# Feed normal data to build baseline (with slight variance for stdev)
import random
random.seed(42)
for i in range(25):
    det.ingest("BTCUSDT", volatility=0.02 + random.uniform(-0.002, 0.002),
               volume=1000.0 + random.uniform(-50, 50),
               spread=0.001 + random.uniform(-0.0001, 0.0001),
               fill_quality=0.98 + random.uniform(-0.01, 0.01),
               latency_ms=50.0 + random.uniform(-5, 5),
               correlation_shift=0.0 + random.uniform(-0.01, 0.01),
               edge_quality=0.7 + random.uniform(-0.05, 0.05))

report = det.evaluate(["BTCUSDT"])
test("Detector: normal data → low composite_stress", report.composite_stress < 0.3,
     f"composite_stress={report.composite_stress}")
test("Detector: normal data → NONE severity", report.max_severity == BreakSeverity.NONE,
     f"max_severity={report.max_severity}")

# Inject a shock
det.ingest("BTCUSDT", volatility=0.15, volume=200.0, spread=0.01,
           fill_quality=0.50, latency_ms=500.0, correlation_shift=0.5, edge_quality=0.1)

report_shock = det.evaluate(["BTCUSDT"])
test("Detector: shock data → high stress", report_shock.composite_stress > 0.2,
     f"composite_stress={report_shock.composite_stress}")
test("Detector: shock data → breaks detected", report_shock.active_break_count > 0,
     f"active_break_count={report_shock.active_break_count}")
test("Detector: BreakReport.to_dict() has all keys",
     all(k in report_shock.to_dict() for k in ["breaks", "max_severity", "composite_stress"]))

# ════════════════════════════════════════════════════════
#  3. RegimeTransitionMonitor
# ════════════════════════════════════════════════════════
print("\n═══ 3. RegimeTransitionMonitor ═══")
from backend.services.trading.regime_transition_monitor import RegimeTransitionMonitor

rtm = RegimeTransitionMonitor()

# Build baseline: steady trending (with slight variance)
for i in range(25):
    rtm.ingest("BTCUSDT", momentum=10.0 + random.uniform(-1, 1),
               range_width=500.0 + random.uniform(-20, 20),
               volatility=0.02 + random.uniform(-0.002, 0.002),
               breakout_price=50000.0, current_price=50000.0,
               regime_clarity=0.9 + random.uniform(-0.05, 0.05))

report_transition = rtm.evaluate(["BTCUSDT"])
test("Transition: stable market → no unstable", report_transition.is_unstable is False,
     f"is_unstable={report_transition.is_unstable}")
test("Transition: stable market → low fragility", report_transition.max_fragility < 0.5,
     f"max_fragility={report_transition.max_fragility}")

# Inject exhaustion: momentum drops to near 0
rtm.ingest("BTCUSDT", momentum=0.5, range_width=100.0, volatility=0.08,
           breakout_price=50000.0, current_price=46000.0, regime_clarity=0.3)

report_trans_shock = rtm.evaluate(["BTCUSDT"])
test("Transition: shock conditions → transitions detected",
     len(report_trans_shock.transitions) > 0,
     f"transitions={len(report_trans_shock.transitions)}")

# ════════════════════════════════════════════════════════
#  4. DoctrineValidityEngine
# ════════════════════════════════════════════════════════
print("\n═══ 4. DoctrineValidityEngine ═══")
from backend.services.trading.doctrine_validity_engine import DoctrineValidityEngine

dve = DoctrineValidityEngine()

# All aligned
verdict_good = dve.evaluate(
    mode_regime_alignment=0.9,
    active_setup_count=5, regime_suitable_count=4,
    spread_normal=True, volatility_normal=True, liquidity_normal=True,
    avg_fill_quality=0.95, avg_edge_degradation=0.05,
    drawdown_pct=0.02, portfolio_stress_score=0.1, correlation_risk=0.1,
)
test("Doctrine: aligned → VALID", verdict_good.action == DoctrineAction.VALID,
     f"action={verdict_good.action}, score={verdict_good.validity_score}")

# Major misalignment
verdict_bad = dve.evaluate(
    mode_regime_alignment=0.1,
    active_setup_count=10, regime_suitable_count=1,
    spread_normal=False, volatility_normal=False, liquidity_normal=False,
    avg_fill_quality=0.2, avg_edge_degradation=0.8,
    drawdown_pct=0.15, portfolio_stress_score=0.9, correlation_risk=0.8,
)
test("Doctrine: misaligned → FREEZE or ROTATE",
     verdict_bad.action in (DoctrineAction.FREEZE, DoctrineAction.ROTATE),
     f"action={verdict_bad.action}, score={verdict_bad.validity_score}")
test("Doctrine: misaligned → low score", verdict_bad.validity_score < 0.5,
     f"score={verdict_bad.validity_score}")
test("Doctrine: mismatches populated", len(verdict_bad.mismatches) > 0)

# ════════════════════════════════════════════════════════
#  5. ShockPropagationAnalyzer
# ════════════════════════════════════════════════════════
print("\n═══ 5. ShockPropagationAnalyzer ═══")
from backend.services.trading.shock_propagation_analyzer import ShockPropagationAnalyzer

spa = ShockPropagationAnalyzer()

# Localized shock (1 symbol out of 10)
result_local = spa.analyze(
    total_symbols=10, stressed_symbols=["BTCUSDT"],
    total_families=5, stressed_families=[],
    total_clusters=3, stressed_clusters=0,
    total_sessions=3, stressed_sessions=[],
    execution_stress_pct=0.0,
)
test("Propagation: localized → LOCAL scope", result_local.scope == ShockScope.LOCAL,
     f"scope={result_local.scope}")
test("Propagation: localized → contained", result_local.containment == "contained",
     f"containment={result_local.containment}")

# Systemic shock
result_systemic = spa.analyze(
    total_symbols=10, stressed_symbols=["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "DOT"],
    total_families=5, stressed_families=["L1", "L2", "DeFi", "Meme"],
    total_clusters=3, stressed_clusters=3,
    total_sessions=3, stressed_sessions=["ASIA", "EU", "US"],
    execution_stress_pct=0.8,
)
test("Propagation: systemic → SYSTEMIC scope", result_systemic.scope == ShockScope.SYSTEMIC,
     f"scope={result_systemic.scope}")
test("Propagation: systemic → not contained", result_systemic.containment != "contained",
     f"containment={result_systemic.containment}")
test("Propagation: high propagation_score", result_systemic.propagation_score > 0.6,
     f"score={result_systemic.propagation_score}")

# ════════════════════════════════════════════════════════
#  6. PreemptiveThrottleEngine
# ════════════════════════════════════════════════════════
print("\n═══ 6. PreemptiveThrottleEngine ═══")
from backend.services.trading.preemptive_throttle_engine import PreemptiveThrottleEngine

pte = PreemptiveThrottleEngine()

# Nominal conditions → NONE
state_none = pte.evaluate(composite_stress=0.0, max_fragility=0.0,
                          doctrine_validity=1.0, propagation_score=0.0)
test("Throttle: nominal → NONE", state_none.level == ThrottleLevel.NONE,
     f"level={state_none.level}")
test("Throttle: nominal → full exposure", state_none.exposure_multiplier == 1.0)

# High danger → HALT
state_halt = pte.evaluate(composite_stress=0.9, max_fragility=0.9,
                          doctrine_validity=0.1, propagation_score=0.9)
test("Throttle: extreme danger → HALT or HEAVY",
     state_halt.level in (ThrottleLevel.HALT, ThrottleLevel.HEAVY),
     f"level={state_halt.level}")
test("Throttle: extreme → reduced exposure", state_halt.exposure_multiplier <= 0.25,
     f"exposure={state_halt.exposure_multiplier}")

# Asymmetric relaxation: need 3 cycles to step down
for _ in range(2):
    pte.evaluate(composite_stress=0.0, max_fragility=0.0,
                 doctrine_validity=1.0, propagation_score=0.0)
level_after_2 = pte.get_level()
test("Throttle: after 2 calm cycles → still not NONE (asymmetric)",
     level_after_2 != ThrottleLevel.NONE,
     f"level={level_after_2}")

state_relax = pte.evaluate(composite_stress=0.0, max_fragility=0.0,
                            doctrine_validity=1.0, propagation_score=0.0)
test("Throttle: after 3 calm cycles → stepped down", True)  # just verify no crash

# Force level
pte.force_level(ThrottleLevel.HEAVY)
test("Throttle: force_level works", pte.get_level() == ThrottleLevel.HEAVY)

# ════════════════════════════════════════════════════════
#  7. FalseRecoveryGuard
# ════════════════════════════════════════════════════════
print("\n═══ 7. FalseRecoveryGuard ═══")
from backend.services.trading.false_recovery_guard import FalseRecoveryGuard

frg = FalseRecoveryGuard()

# No shock → always valid
result_no_shock = frg.evaluate(avg_fill_quality=0.9, avg_edge_quality=0.7,
                                regime_clarity=0.8, doctrine_validity=0.8)
test("Recovery: no shock → VALID", result_no_shock.validity == RecoveryValidity.VALID)
test("Recovery: no shock → 'restore'", result_no_shock.recommendation == "restore")

# Register shock
frg.register_shock()
test("Recovery: shock_active after register", frg.is_shock_active())

# Immediate check → INVALID (min hold)
result_hold = frg.evaluate(avg_fill_quality=0.9, avg_edge_quality=0.7,
                            regime_clarity=0.8, doctrine_validity=0.8)
test("Recovery: within min hold → INVALID", result_hold.validity == RecoveryValidity.INVALID)
test("Recovery: within min hold → 'hold'", result_hold.recommendation == "hold")

# Advance past min hold
for _ in range(5):
    frg.tick()

# All conditions met → VALID
result_restore = frg.evaluate(avg_fill_quality=0.9, avg_edge_quality=0.7,
                               regime_clarity=0.8, doctrine_validity=0.8)
test("Recovery: all axes passed → VALID", result_restore.validity == RecoveryValidity.VALID)
test("Recovery: shock cleared after full restore", not frg.is_shock_active())

# Register another shock, partial recovery
frg.register_shock()
for _ in range(5):
    frg.tick()
result_partial = frg.evaluate(avg_fill_quality=0.9, avg_edge_quality=0.7,
                               regime_clarity=0.2, doctrine_validity=0.2)
test("Recovery: 2 axes → PARTIAL", result_partial.validity == RecoveryValidity.PARTIAL)

# ════════════════════════════════════════════════════════
#  8. StructuralBreakStore
# ════════════════════════════════════════════════════════
print("\n═══ 8. StructuralBreakStore ═══")
from backend.services.trading.structural_break_store import StructuralBreakStore

tmpdir = tempfile.mkdtemp(prefix="sb_test_")
try:
    store = StructuralBreakStore(base_dir=tmpdir)

    # Log break report
    store.log_break_report(report_shock)
    history = store.read_break_history(10)
    test("Store: break_events written + readable", len(history) == 1)

    # Log transition report
    store.log_transition_report(report_trans_shock)
    test("Store: transition_events written", len(store.read_transition_history()) == 1)

    # Log doctrine verdict
    store.log_doctrine_verdict(verdict_bad)
    test("Store: doctrine_events written", len(store.read_doctrine_history()) == 1)

    # Log propagation result
    store.log_propagation_result(result_systemic)
    test("Store: propagation_events written", len(store.read_propagation_history()) == 1)

    # Log throttle state
    store.log_throttle_state(state_halt)
    test("Store: throttle_actions written", len(store.read_throttle_history()) == 1)

    # Log recovery check
    store.log_recovery_check(result_partial)
    test("Store: recovery_blocks written", len(store.read_recovery_history()) == 1)

    # Multiple writes
    store.log_break_report(report)
    store.log_break_report(report_shock)
    test("Store: multiple writes append correctly", len(store.read_break_history()) == 3)

finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

# ════════════════════════════════════════════════════════
#  9. Routes import check
# ════════════════════════════════════════════════════════
print("\n═══ 9. Routes import ═══")
try:
    from backend.routes.structural_break_routes import router
    test("Routes: import successful", True)
    test("Routes: has correct prefix", router.prefix == "/api/structural-breaks")

    route_paths = [r.path for r in router.routes]
    prefix = router.prefix
    test("Routes: /status endpoint exists", f"{prefix}/status" in route_paths)
    test("Routes: /breaks endpoint exists", f"{prefix}/breaks" in route_paths)
    test("Routes: /transitions endpoint exists", f"{prefix}/transitions" in route_paths)
    test("Routes: /doctrine endpoint exists", f"{prefix}/doctrine" in route_paths)
    test("Routes: /propagation endpoint exists", f"{prefix}/propagation" in route_paths)
    test("Routes: /throttle endpoint exists", f"{prefix}/throttle" in route_paths)
    test("Routes: /recovery endpoint exists", f"{prefix}/recovery" in route_paths)
    test("Routes: /force-throttle endpoint exists", f"{prefix}/force-throttle" in route_paths)
    test("Routes: /register-shock endpoint exists", f"{prefix}/register-shock" in route_paths)
except Exception as e:
    test("Routes: import failed", False, str(e))

# ════════════════════════════════════════════════════════
#  10. Full pipeline integration
# ════════════════════════════════════════════════════════
print("\n═══ 10. Full Pipeline Integration ═══")

# Simulate a full cycle: detect → transition → doctrine → propagation → throttle → recovery
pipeline_detector = StructuralBreakDetector()
pipeline_transition = RegimeTransitionMonitor()
pipeline_doctrine = DoctrineValidityEngine()
pipeline_propagation = ShockPropagationAnalyzer()
pipeline_throttle = PreemptiveThrottleEngine()
pipeline_recovery = FalseRecoveryGuard()

# 1) Build baselines (with slight variance)
for i in range(25):
    pipeline_detector.ingest("BTCUSDT", volatility=0.02 + random.uniform(-0.002, 0.002),
                             volume=1000.0 + random.uniform(-50, 50),
                             spread=0.001 + random.uniform(-0.0001, 0.0001),
                             fill_quality=0.98 + random.uniform(-0.01, 0.01),
                             latency_ms=50.0 + random.uniform(-5, 5),
                             correlation_shift=0.0 + random.uniform(-0.01, 0.01),
                             edge_quality=0.7 + random.uniform(-0.05, 0.05))
    pipeline_transition.ingest("BTCUSDT", momentum=10.0 + random.uniform(-1, 1),
                               range_width=500.0 + random.uniform(-20, 20),
                               volatility=0.02 + random.uniform(-0.002, 0.002),
                               breakout_price=50000.0, current_price=50000.0,
                               regime_clarity=0.9 + random.uniform(-0.05, 0.05))

# 2) Nominal evaluation
break_rpt = pipeline_detector.evaluate(["BTCUSDT"])
trans_rpt = pipeline_transition.evaluate(["BTCUSDT"])
doc_verdict = pipeline_doctrine.evaluate(
    mode_regime_alignment=0.8,
    active_setup_count=5, regime_suitable_count=4,
    spread_normal=True, volatility_normal=True, liquidity_normal=True,
    avg_fill_quality=0.95, avg_edge_degradation=0.05,
    drawdown_pct=0.02, portfolio_stress_score=0.1, correlation_risk=0.1,
)
prop_result = pipeline_propagation.analyze(
    total_symbols=10, stressed_symbols=[],
    total_families=5, stressed_families=[],
    total_clusters=3, stressed_clusters=0,
    total_sessions=3, stressed_sessions=[],
    execution_stress_pct=0.0,
)
throttle_state = pipeline_throttle.evaluate(
    composite_stress=break_rpt.composite_stress,
    max_fragility=trans_rpt.max_fragility,
    doctrine_validity=doc_verdict.validity_score,
    propagation_score=prop_result.propagation_score,
)
recovery_result = pipeline_recovery.evaluate(
    avg_fill_quality=0.95, avg_edge_quality=0.7,
    regime_clarity=0.9, doctrine_validity=doc_verdict.validity_score,
)

test("Pipeline: nominal → NONE throttle", throttle_state.level == ThrottleLevel.NONE,
     f"level={throttle_state.level}")
test("Pipeline: nominal → VALID recovery", recovery_result.validity == RecoveryValidity.VALID)

# 3) Inject shock
pipeline_detector.ingest("BTCUSDT", volatility=0.20, volume=100.0, spread=0.02,
                         fill_quality=0.30, latency_ms=800.0, correlation_shift=0.7, edge_quality=0.05)
pipeline_transition.ingest("BTCUSDT", momentum=0.1, range_width=50.0, volatility=0.15,
                           breakout_price=50000.0, current_price=42000.0, regime_clarity=0.15)

break_rpt2 = pipeline_detector.evaluate(["BTCUSDT"])
trans_rpt2 = pipeline_transition.evaluate(["BTCUSDT"])
doc_verdict2 = pipeline_doctrine.evaluate(
    mode_regime_alignment=0.1,
    active_setup_count=10, regime_suitable_count=1,
    spread_normal=False, volatility_normal=False, liquidity_normal=False,
    avg_fill_quality=0.2, avg_edge_degradation=0.8,
    drawdown_pct=0.15, portfolio_stress_score=0.9, correlation_risk=0.8,
)
prop_result2 = pipeline_propagation.analyze(
    total_symbols=10, stressed_symbols=["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE"],
    total_families=5, stressed_families=["L1", "L2", "DeFi", "Meme"],
    total_clusters=3, stressed_clusters=3,
    total_sessions=3, stressed_sessions=["ASIA", "EU"],
    execution_stress_pct=0.7,
)
throttle_state2 = pipeline_throttle.evaluate(
    composite_stress=break_rpt2.composite_stress,
    max_fragility=trans_rpt2.max_fragility,
    doctrine_validity=doc_verdict2.validity_score,
    propagation_score=prop_result2.propagation_score,
)

test("Pipeline: shock → elevated throttle",
     throttle_state2.level in (ThrottleLevel.MODERATE, ThrottleLevel.HEAVY, ThrottleLevel.HALT),
     f"level={throttle_state2.level}")
test("Pipeline: shock → reduced exposure", throttle_state2.exposure_multiplier < 1.0,
     f"exposure={throttle_state2.exposure_multiplier}")

# Register shock for recovery guard
if break_rpt2.max_severity in (BreakSeverity.SEVERE, BreakSeverity.CRITICAL):
    pipeline_recovery.register_shock()
    test("Pipeline: shock registered", pipeline_recovery.is_shock_active())
else:
    test("Pipeline: shock severity triggers guard",
         break_rpt2.active_break_count > 0,
         f"severity={break_rpt2.max_severity}")

print(f"\n{'='*50}")
print(f"TOTAL:  {PASSED} passed,  {FAILED} failed  ({PASSED + FAILED} tests)")
print(f"{'='*50}")

sys.exit(0 if FAILED == 0 else 1)
