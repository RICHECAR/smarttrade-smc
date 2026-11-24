"""Validation script for the Meta-Control Layer (8 modules + routes)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

passed = 0
failed = 0

def test(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  PASS  {name}")
        passed += 1
    except Exception as e:
        print(f"  FAIL  {name}: {e}")
        failed += 1

# ═══════════════════════════════════════════════════════
# 1) Import all modules
# ═══════════════════════════════════════════════════════
def t_imports():
    from backend.services.trading.meta_control_models import SystemMode, RecoveryPhase, ModeInputs, ModeDecision, RiskBudget, UniverseState, FilterVerdict, RecoveryState, RotationCandidate, RotationDecision, DomainHealth, StackHealthReport, HealthDomain, HealthStatus, BlockReason
    from backend.services.trading.meta_control_engine import MetaControlEngine
    from backend.services.trading.global_risk_budget import GlobalRiskBudgetController
    from backend.services.trading.opportunity_filter import OpportunityUniverseFilter
    from backend.services.trading.recovery_protocol import RecoveryProtocolEngine
    from backend.services.trading.strategic_rotation import StrategicRotationEngine
    from backend.services.trading.meta_health_monitor import MetaHealthMonitor
    from backend.services.trading.meta_control_store import MetaControlStore
    from backend.routes.meta_control_routes import router
    # Verify enum values exist
    assert HealthStatus.HEALTHY
    assert HealthStatus.WARNING
    assert HealthStatus.DEGRADED
    assert HealthStatus.CRITICAL

test("1. All imports", t_imports)

# ═══════════════════════════════════════════════════════
# 2) MetaControlEngine — mode selection
# ═══════════════════════════════════════════════════════
def t_engine_expansion():
    from backend.services.trading.meta_control_engine import MetaControlEngine
    from backend.services.trading.meta_control_models import ModeInputs, SystemMode
    e = MetaControlEngine()
    inp = ModeInputs(drawdown_pct=0.01, drawdown_phase="normal", avg_edge_quality=0.90, avg_execution_quality=0.95, regime_quality=0.90, avg_robustness=0.85, stack_health=0.95)
    d = e.evaluate(inp)
    # First call won't promote instantly due to hysteresis — stays SELECTIVE default
    # Need PROMOTION_HOLD cycles for upgrade
    assert d.mode == SystemMode.SELECTIVE, f"Expected SELECTIVE (hysteresis), got {d.mode}"

test("2. Engine: hysteresis prevents instant promotion", t_engine_expansion)

def t_engine_survival():
    from backend.services.trading.meta_control_engine import MetaControlEngine
    from backend.services.trading.meta_control_models import ModeInputs, SystemMode
    e = MetaControlEngine()
    inp = ModeInputs(drawdown_pct=0.25, drawdown_phase="critical", avg_edge_quality=0.10, avg_execution_quality=0.20, regime_quality=0.10, avg_robustness=0.10, stack_health=0.10)
    d = e.evaluate(inp)
    # Degradation is instant
    assert d.mode == SystemMode.SURVIVAL, f"Expected SURVIVAL, got {d.mode}"
    assert d.max_concurrent == 2

test("3. Engine: instant degradation to SURVIVAL", t_engine_survival)

def t_engine_promotion_after_hold():
    from backend.services.trading.meta_control_engine import MetaControlEngine
    from backend.services.trading.meta_control_models import ModeInputs, SystemMode
    e = MetaControlEngine()
    good = ModeInputs(drawdown_pct=0.01, drawdown_phase="normal", avg_edge_quality=0.95, avg_execution_quality=0.95, regime_quality=0.95, avg_robustness=0.90, stack_health=0.95)
    # Run PROMOTION_HOLD cycles
    for _ in range(e.PROMOTION_HOLD + 1):
        d = e.evaluate(good)
    # Should have promoted
    assert d.mode in (SystemMode.EXPANSION, SystemMode.OFFENSIVE), f"Expected promotion, got {d.mode}"

test("4. Engine: promotion after hold cycles", t_engine_promotion_after_hold)

# ═══════════════════════════════════════════════════════
# 3) GlobalRiskBudgetController
# ═══════════════════════════════════════════════════════
def t_budget():
    from backend.services.trading.global_risk_budget import GlobalRiskBudgetController
    from backend.services.trading.meta_control_models import SystemMode
    ctrl = GlobalRiskBudgetController()
    b = ctrl.compute(SystemMode.EXPANSION)
    assert b.max_total_exposure == 0.95, f"Expected 0.95, got {b.max_total_exposure}"
    assert b.max_concurrent_positions == 12
    b2 = ctrl.compute(SystemMode.SURVIVAL)
    assert b2.max_total_exposure == 0.20
    assert b2.max_concurrent_positions == 2

test("5. Budget: EXPANSION vs SURVIVAL caps", t_budget)

def t_budget_degradation():
    from backend.services.trading.global_risk_budget import GlobalRiskBudgetController
    from backend.services.trading.meta_control_models import SystemMode
    ctrl = GlobalRiskBudgetController()
    b = ctrl.compute(SystemMode.OFFENSIVE, degradation_mult=0.5)
    assert b.max_total_exposure <= 0.40, f"Expected <=0.40, got {b.max_total_exposure}"

test("6. Budget: degradation multiplier tightens", t_budget_degradation)

# ═══════════════════════════════════════════════════════
# 4) OpportunityUniverseFilter
# ═══════════════════════════════════════════════════════
def t_filter_pass():
    from backend.services.trading.opportunity_filter import OpportunityUniverseFilter
    from backend.services.trading.meta_control_models import SystemMode
    f = OpportunityUniverseFilter()
    v = f.evaluate("BTCUSD", "breakout", "trending", ["trending", "ranging"], SystemMode.SELECTIVE, setup_key="BTC_breakout")
    assert v.allowed is True
    assert v.setup_key == "BTC_breakout"

test("7. Filter: clean pass", t_filter_pass)

def t_filter_consec_loss():
    from backend.services.trading.opportunity_filter import OpportunityUniverseFilter
    from backend.services.trading.meta_control_models import SystemMode
    f = OpportunityUniverseFilter()
    for _ in range(3):
        f.record_outcome("ETHUSD", "reversal", False)
    v = f.evaluate("ETHUSD", "reversal", "trending", ["trending"], SystemMode.OFFENSIVE)
    assert v.allowed is False, f"Expected block, got allowed"
    assert v.block_reason != ""

test("8. Filter: consecutive losses block", t_filter_consec_loss)

def t_filter_regime_mismatch():
    from backend.services.trading.opportunity_filter import OpportunityUniverseFilter
    from backend.services.trading.meta_control_models import SystemMode
    f = OpportunityUniverseFilter()
    v = f.evaluate("SOLUSD", "breakout", "volatile", ["trending"], SystemMode.SELECTIVE)
    assert v.allowed is False

test("9. Filter: regime mismatch block", t_filter_regime_mismatch)

# ═══════════════════════════════════════════════════════
# 5) RecoveryProtocolEngine
# ═══════════════════════════════════════════════════════
def t_recovery_trigger():
    from backend.services.trading.recovery_protocol import RecoveryProtocolEngine
    from backend.services.trading.meta_control_models import RecoveryPhase
    r = RecoveryProtocolEngine()
    s = r.update(10000, 0.15)
    assert s.phase == RecoveryPhase.STABILISATION
    assert s.allocation_cap == 0.0
    assert s.expansion_allowed is False

test("10. Recovery: trigger on 15% DD", t_recovery_trigger)

def t_recovery_progression():
    from backend.services.trading.recovery_protocol import RecoveryProtocolEngine
    from backend.services.trading.meta_control_models import RecoveryPhase
    r = RecoveryProtocolEngine()
    # Trigger
    r.update(10000, 0.15)
    # Go through STABILISATION cycles
    for _ in range(6):
        s = r.update(9900, 0.10)
    assert s.phase == RecoveryPhase.REBUILDING
    assert s.allocation_cap == 0.30
    # Win 3 trades
    for _ in range(3):
        s = r.update(9950, 0.08, trade_won=True)
    assert s.phase == RecoveryPhase.VERIFICATION

test("11. Recovery: STAB -> REBUILD -> VERIFY progression", t_recovery_progression)

# ═══════════════════════════════════════════════════════
# 6) StrategicRotationEngine
# ═══════════════════════════════════════════════════════
def t_rotation_ranking():
    from backend.services.trading.strategic_rotation import StrategicRotationEngine
    from backend.services.trading.meta_control_models import RotationCandidate, SystemMode
    rot = StrategicRotationEngine()
    candidates = [
        RotationCandidate(setup_key="BTC_breakout", symbol="BTCUSD", adjusted_edge=0.8, robustness_score=0.9, diversification_value=0.5, capital_efficiency=0.7, regime_fit=0.85),
        RotationCandidate(setup_key="ETH_reversal", symbol="ETHUSD", adjusted_edge=0.3, robustness_score=0.4, diversification_value=0.8, capital_efficiency=0.5, regime_fit=0.3),
        RotationCandidate(setup_key="SOL_momentum", symbol="SOLUSD", adjusted_edge=0.9, robustness_score=0.6, diversification_value=0.6, capital_efficiency=0.8, regime_fit=0.9),
    ]
    decisions = rot.rank(candidates, SystemMode.OFFENSIVE, max_slots=3)
    assert len(decisions) == 3
    # SOL should rank higher in OFFENSIVE (edge-heavy)
    assert decisions[0].setup_key == "SOL_momentum", f"Expected SOL first, got {decisions[0].setup_key}"
    assert decisions[0].rank == 1
    assert decisions[0].favored is True

test("12. Rotation: OFFENSIVE ranking (edge-heavy)", t_rotation_ranking)

def t_rotation_defensive():
    from backend.services.trading.strategic_rotation import StrategicRotationEngine
    from backend.services.trading.meta_control_models import RotationCandidate, SystemMode
    rot = StrategicRotationEngine()
    candidates = [
        RotationCandidate(setup_key="A", symbol="A", adjusted_edge=0.9, robustness_score=0.3, diversification_value=0.5, capital_efficiency=0.3, regime_fit=0.5),
        RotationCandidate(setup_key="B", symbol="B", adjusted_edge=0.3, robustness_score=0.95, diversification_value=0.5, capital_efficiency=0.9, regime_fit=0.5),
    ]
    decisions = rot.rank(candidates, SystemMode.DEFENSIVE)
    # In DEFENSIVE, robustness + cap_efficiency dominate → B should rank first
    assert decisions[0].setup_key == "B", f"Expected B first, got {decisions[0].setup_key}"
    assert decisions[0].favored is True

test("13. Rotation: DEFENSIVE favors robustness", t_rotation_defensive)

# ═══════════════════════════════════════════════════════
# 7) MetaHealthMonitor
# ═══════════════════════════════════════════════════════
def t_health_full():
    from backend.services.trading.meta_health_monitor import MetaHealthMonitor
    from backend.services.trading.meta_control_models import HealthStatus
    h = MetaHealthMonitor()
    h.update_execution(0.95, 100, 0)
    h.update_signal(0.80, 0.70, 0.85)
    h.update_portfolio(0.03, 0.40, 0.20)
    h.update_policy(0.5, 0.80, 0.75)
    h.update_data(10, 0.01, 0)
    r = h.get_report()
    assert r.overall_status == HealthStatus.HEALTHY
    assert r.overall_score > 0.6
    assert r.degradation_mult > 0.7

test("14. Health: all-green → HEALTHY", t_health_full)

def t_health_degraded():
    from backend.services.trading.meta_health_monitor import MetaHealthMonitor
    from backend.services.trading.meta_control_models import HealthStatus
    h = MetaHealthMonitor()
    h.update_execution(0.30, 1500, 4)
    h.update_signal(0.20, 0.30, 0.20)
    h.update_portfolio(0.20, 0.80, 0.45)
    h.update_policy(2.5, 0.30, 0.25)
    h.update_data(200, 0.30, 6)
    r = h.get_report()
    assert r.overall_status in (HealthStatus.DEGRADED, HealthStatus.CRITICAL, HealthStatus.WARNING)
    assert r.degradation_mult < 0.7

test("15. Health: degraded conditions", t_health_degraded)

# ═══════════════════════════════════════════════════════
# 8) MetaControlStore (persistence)
# ═══════════════════════════════════════════════════════
def t_store():
    import tempfile, shutil
    from backend.services.trading.meta_control_store import MetaControlStore
    from backend.services.trading.meta_control_models import SystemMode, RiskBudget
    tmp = tempfile.mkdtemp()
    try:
        s = MetaControlStore(tmp)
        budget = RiskBudget(mode="selective", max_total_exposure=0.60, max_concurrent_positions=7, max_per_symbol_cluster=0.30, max_per_setup_family=0.20)
        s.log_risk_budget(budget)
        s.log_risk_budget(budget)
        records = s.read_risk_budgets(10)
        assert len(records) == 2
        assert records[0]["mode"] == "selective"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

test("16. Store: write + read JSONL", t_store)

# ═══════════════════════════════════════════════════════
# 9) Full pipeline integration
# ═══════════════════════════════════════════════════════
def t_full_pipeline():
    from backend.services.trading.meta_control_engine import MetaControlEngine
    from backend.services.trading.global_risk_budget import GlobalRiskBudgetController
    from backend.services.trading.opportunity_filter import OpportunityUniverseFilter
    from backend.services.trading.recovery_protocol import RecoveryProtocolEngine
    from backend.services.trading.strategic_rotation import StrategicRotationEngine
    from backend.services.trading.meta_health_monitor import MetaHealthMonitor
    from backend.services.trading.meta_control_models import ModeInputs, RotationCandidate, SystemMode

    # 1. Health
    health = MetaHealthMonitor()
    health.update_execution(0.90, 150, 1)
    health.update_signal(0.70, 0.60, 0.75)
    health.update_portfolio(0.08, 0.50, 0.25)
    health.update_policy(1.0, 0.70, 0.65)
    health.update_data(30, 0.05, 1)
    report = health.get_report()

    # 2. Engine
    engine = MetaControlEngine()
    inputs = ModeInputs(
        drawdown_pct=0.08, drawdown_phase="caution",
        avg_edge_quality=0.70, avg_execution_quality=0.90,
        regime_quality=0.75, avg_robustness=0.65,
        stack_health=report.overall_score,
    )
    decision = engine.evaluate(inputs)

    # 3. Budget
    budget_ctrl = GlobalRiskBudgetController()
    budget = budget_ctrl.compute(decision.mode, degradation_mult=report.degradation_mult)

    # 4. Recovery
    recovery = RecoveryProtocolEngine()
    rec_state = recovery.update(9200, 0.08)

    # 5. Filter
    filt = OpportunityUniverseFilter()
    verdict = filt.evaluate("BTCUSD", "breakout", "trending", ["trending", "ranging"], decision.mode)

    # 6. Rotation
    rotation = StrategicRotationEngine()
    candidates = [
        RotationCandidate(setup_key="BTC_break", symbol="BTCUSD", adjusted_edge=0.75, robustness_score=0.80, diversification_value=0.60, capital_efficiency=0.70, regime_fit=0.80),
    ]
    rankings = rotation.rank(candidates, decision.mode, max_slots=budget.max_concurrent_positions)

    # Verify pipeline coherence
    assert decision.mode is not None
    assert budget.max_total_exposure > 0
    assert verdict.allowed is True
    assert len(rankings) == 1
    assert rankings[0].composite_score > 0
    # Cross-check budget limits
    assert budget.max_concurrent_positions <= 12

test("17. Full pipeline integration", t_full_pipeline)

# ═══════════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"  Meta-Control Layer Validation: {passed} passed, {failed} failed")
print(f"{'='*50}")
sys.exit(1 if failed > 0 else 0)
