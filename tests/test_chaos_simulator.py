"""
Tests for Chaos Simulator — Phase 11C

Coverage:
  1. Each scenario runs and returns handled=True
  2. Unknown scenario returns graceful failure
  3. Run all scenarios returns summary
  4. Fault injector edge cases (context overrides)
  5. Render chaos report
  6. Result structure validation

Author: SMC Trading Bot Framework — Phase 11C Tests
"""

import pytest
from scripts.reporting.chaos_simulator import (
    run_scenario,
    run_all_scenarios,
    render_chaos_report,
    SCENARIOS,
    INJECTORS,
    ChaosScenario,
    ChaosResult,
    inject_exchange_outage,
    inject_latency_spike,
    inject_data_stale,
    inject_partial_fill,
    inject_kill_switch_trigger,
    inject_clock_skew,
    inject_db_unavailable,
    inject_api_rate_limit,
)


# ────────────────────────────────────────────────────────────────────
# 1. INDIVIDUAL SCENARIOS
# ────────────────────────────────────────────────────────────────────

class TestIndividualScenarios:
    """Each scenario should handle the fault gracefully."""

    def test_exchange_outage(self):
        result = run_scenario("exchange_outage")
        assert result.passed is True
        assert result.recovery_action == "pause_trading"

    def test_latency_spike(self):
        result = run_scenario("latency_spike", {"latency_ms": 5000, "timeout_ms": 3000})
        assert result.passed is True
        assert result.recovery_action == "use_cached_data"

    def test_latency_within_threshold(self):
        result = run_scenario("latency_spike", {"latency_ms": 1000, "timeout_ms": 3000})
        assert result.passed is True
        assert result.recovery_action == "normal"

    def test_data_stale(self):
        result = run_scenario("data_stale", {"data_age_seconds": 120, "max_age_seconds": 60})
        assert result.passed is True
        assert result.recovery_action == "skip_cycle"

    def test_data_fresh(self):
        result = run_scenario("data_stale", {"data_age_seconds": 10, "max_age_seconds": 60})
        assert result.passed is True
        assert result.recovery_action == "normal"

    def test_partial_fill(self):
        result = run_scenario("partial_fill", {"fill_pct": 0.3, "order_qty": 1.0})
        assert result.passed is True
        assert result.recovery_action == "adjust_position"
        assert result.details["filled_qty"] == 0.3
        assert result.details["remaining_qty"] == 0.7

    def test_kill_switch_trigger(self):
        result = run_scenario("kill_switch_trigger")
        assert result.passed is True
        assert result.recovery_action == "block_new_orders"
        assert result.details["positions_preserved"] is True

    def test_clock_skew_large(self):
        result = run_scenario("clock_skew", {"skew_seconds": 30, "max_skew_seconds": 10})
        assert result.passed is True
        assert result.recovery_action == "pause_until_synced"

    def test_clock_skew_small(self):
        result = run_scenario("clock_skew", {"skew_seconds": 5, "max_skew_seconds": 10})
        assert result.passed is True
        assert result.recovery_action == "normal"

    def test_db_unavailable(self):
        result = run_scenario("db_unavailable")
        assert result.passed is True
        assert result.recovery_action == "use_defaults"
        assert result.details["fallback_used"] is True

    def test_api_rate_limit(self):
        result = run_scenario("api_rate_limit", {"retry_after_seconds": 30})
        assert result.passed is True
        assert result.recovery_action == "exponential_backoff"


# ────────────────────────────────────────────────────────────────────
# 2. UNKNOWN SCENARIO
# ────────────────────────────────────────────────────────────────────

class TestUnknownScenario:

    def test_unknown_returns_failure(self):
        result = run_scenario("nonexistent_scenario")
        assert result.passed is False
        assert "Unknown scenario" in result.actual_behavior


# ────────────────────────────────────────────────────────────────────
# 3. RUN ALL SCENARIOS
# ────────────────────────────────────────────────────────────────────

class TestRunAll:

    def test_all_scenarios_pass(self):
        report = run_all_scenarios()
        s = report["summary"]
        assert s["total"] == len(SCENARIOS)
        assert s["passed"] == len(SCENARIOS)
        assert s["failed"] == 0
        assert s["pass_rate"] == 100.0
        assert s["grade"] == "A"

    def test_all_scenarios_have_results(self):
        report = run_all_scenarios()
        assert len(report["results"]) == len(SCENARIOS)
        for r in report["results"]:
            assert "scenario_id" in r
            assert "passed" in r
            assert "recovery_action" in r

    def test_custom_contexts(self):
        contexts = {
            "latency_spike": {"latency_ms": 1000, "timeout_ms": 3000},
        }
        report = run_all_scenarios(contexts)
        assert report["summary"]["passed"] == len(SCENARIOS)


# ────────────────────────────────────────────────────────────────────
# 4. SCENARIO REGISTRY CONSISTENCY
# ────────────────────────────────────────────────────────────────────

class TestRegistryConsistency:

    def test_all_scenarios_have_injectors(self):
        for sid in SCENARIOS:
            assert sid in INJECTORS, f"Scenario {sid} has no injector"

    def test_all_injectors_have_scenarios(self):
        for sid in INJECTORS:
            assert sid in SCENARIOS, f"Injector {sid} has no scenario"

    def test_scenario_fields(self):
        for sid, scenario in SCENARIOS.items():
            assert scenario.scenario_id == sid
            assert len(scenario.description) > 0
            assert scenario.severity in ("low", "medium", "high", "critical")
            assert scenario.fault_type in ("network", "data", "system", "exchange")


# ────────────────────────────────────────────────────────────────────
# 5. RENDER
# ────────────────────────────────────────────────────────────────────

class TestRenderChaos:

    def test_render_report(self):
        report = run_all_scenarios()
        md = render_chaos_report(report)
        assert "Chaos Simulation Report" in md
        assert "Grade:" in md
        for sid in SCENARIOS:
            assert sid in md

    def test_render_empty(self):
        md = render_chaos_report({"results": [], "summary": {
            "total": 0, "passed": 0, "failed": 0, "pass_rate": 0, "grade": "N/A"
        }})
        assert "Chaos Simulation Report" in md


# ────────────────────────────────────────────────────────────────────
# 6. RESULT DATACLASS
# ────────────────────────────────────────────────────────────────────

class TestChaosResult:

    def test_result_defaults(self):
        r = ChaosResult(
            scenario_id="test",
            passed=True,
            expected_behavior="ok",
            actual_behavior="ok",
            recovery_action="none",
        )
        assert r.duration_ms == 0.0
        assert r.details == {}

    def test_duration_populated(self):
        result = run_scenario("exchange_outage")
        assert result.duration_ms >= 0
