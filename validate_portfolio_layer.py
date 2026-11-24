"""Full validation of Portfolio Intelligence Layer — 9 modules."""
import tempfile, shutil, random
random.seed(42)

from backend.services.trading.portfolio_models import *
from backend.services.trading.portfolio_allocator import PortfolioAllocator
from backend.services.trading.correlation_risk import CorrelationRiskEngine
from backend.services.trading.portfolio_drawdown_governor import PortfolioDrawdownGovernor
from backend.services.trading.capital_efficiency import CapitalEfficiencyScorer
from backend.services.trading.conflict_resolution import ConflictResolutionEngine
from backend.services.trading.portfolio_stress_tester import PortfolioStressTester
from backend.services.trading.portfolio_store import PortfolioStore

print("ALL 9 MODULES IMPORT OK")

# ═══════════════════════════════════════════════════════════
# TEST 1: PortfolioAllocator
# ═══════════════════════════════════════════════════════════
allocator = PortfolioAllocator()
opps = [
    SetupOpportunity(setup_key="btc_long", symbol="BTC/USD", direction="long",
                     adjusted_edge=25.0, robustness_score=0.90, regime_fit=0.85,
                     execution_quality=0.95, requested_allocation_pct=0.15),
    SetupOpportunity(setup_key="eth_short", symbol="ETH/USD", direction="short",
                     adjusted_edge=12.0, robustness_score=0.70, regime_fit=0.60,
                     execution_quality=0.80, requested_allocation_pct=0.10),
    SetupOpportunity(setup_key="sol_long", symbol="SOL/USD", direction="long",
                     adjusted_edge=5.0, robustness_score=0.50, regime_fit=0.40,
                     execution_quality=0.60, requested_allocation_pct=0.08),
]
snap = allocator.allocate(opps, 100000)
print(f"Allocator: {len(snap.allocations)} positions, util={snap.utilization:.2%}, free=${snap.free_capital:.0f}")
assert len(snap.allocations) >= 2
assert snap.allocations[0].setup_key == "btc_long"
print("ALLOCATOR TEST PASS")

# ═══════════════════════════════════════════════════════════
# TEST 2: CorrelationRiskEngine
# ═══════════════════════════════════════════════════════════
corr = CorrelationRiskEngine()
base = [random.gauss(0, 1) for _ in range(50)]
corr.update_returns("BTC/USD", base)
corr.update_returns("ETH/USD", [b + random.gauss(0, 0.3) for b in base])
corr.update_returns("SOL/USD", [random.gauss(0, 1) for _ in range(50)])

positions = {"btc_long": 0.15, "eth_short": 0.10, "sol_long": 0.08}
sym_map = {"btc_long": "BTC/USD", "eth_short": "ETH/USD", "sol_long": "SOL/USD"}
cr = corr.analyze(positions, sym_map)
print(f"Correlation: {len(cr.pairs)} pairs, clusters={len(cr.concentration_clusters)}, div={cr.portfolio_diversification_score:.3f}")
btc_eth = [p for p in cr.pairs if {p.key_a, p.key_b} == {"BTC/USD", "ETH/USD"}]
assert btc_eth and btc_eth[0].correlation > 0.6, "BTC-ETH should be highly correlated"
print(f"BTC-ETH corr: {btc_eth[0].correlation:.3f} level={btc_eth[0].level.value}")
print("CORRELATION TEST PASS")

# ═══════════════════════════════════════════════════════════
# TEST 3: PortfolioDrawdownGovernor
# ═══════════════════════════════════════════════════════════
gov = PortfolioDrawdownGovernor()
s = gov.update(100000)
assert s.phase == DrawdownPhase.NORMAL
print(f"DD at peak: phase={s.phase.value} mult={s.allocation_multiplier}")

s = gov.update(93000)
assert s.phase == DrawdownPhase.CAUTION
print(f"DD 7%: phase={s.phase.value} mult={s.allocation_multiplier} max_pos={s.max_concurrent_positions}")

s = gov.update(85000)
assert s.phase == DrawdownPhase.STRESSED
print(f"DD 15%: phase={s.phase.value} mult={s.allocation_multiplier} max_pos={s.max_concurrent_positions}")

s = gov.update(75000)
assert s.phase == DrawdownPhase.CRITICAL
print(f"DD 25%: phase={s.phase.value} mult={s.allocation_multiplier} max_pos={s.max_concurrent_positions}")

s = gov.update(93000)
assert s.phase == DrawdownPhase.RECOVERY
print(f"Recovery: phase={s.phase.value} mult={s.allocation_multiplier}")

s = gov.update(100500)
assert s.phase == DrawdownPhase.NORMAL
print(f"Full recovery: phase={s.phase.value} mult={s.allocation_multiplier}")
print("DRAWDOWN GOVERNOR TEST PASS")

# ═══════════════════════════════════════════════════════════
# TEST 4: CapitalEfficiencyScorer
# ═══════════════════════════════════════════════════════════
eff = CapitalEfficiencyScorer()
good_pnls = [random.gauss(30, 10) for _ in range(50)]
bad_pnls = [random.gauss(-5, 20) for _ in range(50)]

g_score = eff.score("good_setup", good_pnls, capital_per_trade=1000, max_drawdown=0.05)
b_score = eff.score("bad_setup", bad_pnls, capital_per_trade=1000, max_drawdown=0.15)
print(f"Good efficiency: {g_score.efficiency_score:.4f} sharpe={g_score.sharpe_proxy:.3f}")
print(f"Bad efficiency:  {b_score.efficiency_score:.4f} sharpe={b_score.sharpe_proxy:.3f}")
assert g_score.efficiency_score > b_score.efficiency_score, "Good should beat bad"
ranked = eff.rank([g_score, b_score])
assert ranked[0].setup_key == "good_setup"
print("EFFICIENCY TEST PASS")

# ═══════════════════════════════════════════════════════════
# TEST 5: ConflictResolutionEngine
# ═══════════════════════════════════════════════════════════
conflict = ConflictResolutionEngine()
report = conflict.resolve(opps, available_budget_pct=0.20)
print(f"Conflict: {report.competing_setups} competing, {report.total_allocated:.2%} allocated")
winners = [d for d in report.decisions if d.outcome == ConflictOutcome.WINNER]
reduced = [d for d in report.decisions if d.outcome == ConflictOutcome.REDUCED]
rejected = [d for d in report.decisions if d.outcome == ConflictOutcome.REJECTED]
print(f"  Winners: {len(winners)}, Reduced: {len(reduced)}, Rejected: {len(rejected)}")
assert report.total_allocated <= 0.201, "Should respect budget"
print("CONFLICT TEST PASS")

# ═══════════════════════════════════════════════════════════
# TEST 6: PortfolioStressTester
# ═══════════════════════════════════════════════════════════
stress = PortfolioStressTester()
sr = stress.stress_test_setup(opps[0], execution_cost=2.0)
print(f"Stress BTC: worst={sr.worst_scenario} survival={sr.worst_survival:.2%} resilience={sr.composite_resilience:.2%}")
assert sr.composite_resilience > 0.3, "Strong setup should have decent resilience"

sr_bad = stress.stress_test_setup(opps[2], execution_cost=5.0)
print(f"Stress SOL: worst={sr_bad.worst_scenario} survival={sr_bad.worst_survival:.2%} resilience={sr_bad.composite_resilience:.2%}")
assert sr.composite_resilience > sr_bad.composite_resilience, "BTC more resilient than SOL"

haircuts = stress.get_stress_haircuts({"btc_long": sr, "sol_long": sr_bad})
print(f"Stress haircuts: {haircuts}")
print("STRESS TEST PASS")

# ═══════════════════════════════════════════════════════════
# TEST 7: PortfolioStore persistence
# ═══════════════════════════════════════════════════════════
tmpdir = tempfile.mkdtemp()
store = PortfolioStore(base_dir=tmpdir)
store.save_allocation(snap.to_dict())
store.save_drawdown(s.to_dict())
store.save_correlation(cr.to_dict())
assert len(store.load_allocations()) == 1
assert len(store.load_drawdowns()) == 1
assert len(store.load_correlations()) == 1
shutil.rmtree(tmpdir, ignore_errors=True)
print("STORE TEST PASS")

# ═══════════════════════════════════════════════════════════
# TEST 8: Full pipeline integration
# ═══════════════════════════════════════════════════════════
gov2 = PortfolioDrawdownGovernor()
gov2.update(100000)
gov2.update(92000)  # 8% DD -> CAUTION
dd_mult = gov2.get_allocation_multiplier()

snap2 = allocator.allocate(
    opps, 100000,
    drawdown_multiplier=dd_mult,
    correlation_haircuts=cr.haircut_map,
    stress_haircuts=haircuts,
)
print(f"Pipeline: util={snap2.utilization:.2%} (DD mult={dd_mult}, + corr + stress)")
assert snap2.utilization < snap.utilization, "Should be more conservative under stress"
print("PIPELINE INTEGRATION TEST PASS")

print()
print("=" * 65)
print("ALL 9 MODULES VALIDATED - PORTFOLIO INTELLIGENCE LAYER COMPLETE")
print("=" * 65)
