"""
Tests for Health Gates + SLO/SLI — Phase 11C

Coverage:
  1. Individual gate checks (pass / fail)
  2. Aggregate health check (all pass, partial fail, critical fail)
  3. SLO evaluation (met / missed)
  4. Render functions
  5. Gate threshold overrides
  6. Edge cases

Author: SMC Trading Bot Framework — Phase 11C Tests
"""

import pytest
from scripts.reporting.health_gates import (
    check_api_connectivity,
    check_data_freshness,
    check_state_integrity,
    check_clock_sync,
    check_risk_budget,
    check_kill_switch,
    check_disk_space,
    check_memory_usage,
    run_health_check,
    evaluate_slo,
    evaluate_all_slos,
    render_health_section,
    render_slo_section,
    GATE_THRESHOLDS,
    SLO_DEFINITIONS,
    GateResult,
    HealthCheckResult,
)


# ────────────────────────────────────────────────────────────────────
# 1. INDIVIDUAL GATES — PASS
# ────────────────────────────────────────────────────────────────────

class TestGatesPass:

    def test_api_connectivity_ok(self):
        r = check_api_connectivity(api_latency_ms=100, api_reachable=True)
        assert r.passed is True
        assert r.gate_id == "api_connectivity"

    def test_data_freshness_ok(self):
        r = check_data_freshness(data_age_seconds=10)
        assert r.passed is True

    def test_state_integrity_ok(self):
        r = check_state_integrity(state_readable=True, state_valid=True)
        assert r.passed is True

    def test_clock_sync_ok(self):
        r = check_clock_sync(skew_seconds=2)
        assert r.passed is True

    def test_risk_budget_ok(self):
        r = check_risk_budget(remaining_risk_pct=0.5)
        assert r.passed is True

    def test_kill_switch_clear(self):
        r = check_kill_switch(kill_switch_active=False)
        assert r.passed is True

    def test_disk_space_ok(self):
        r = check_disk_space(free_disk_mb=500)
        assert r.passed is True

    def test_memory_ok(self):
        r = check_memory_usage(process_memory_mb=256)
        assert r.passed is True


# ────────────────────────────────────────────────────────────────────
# 2. INDIVIDUAL GATES — FAIL
# ────────────────────────────────────────────────────────────────────

class TestGatesFail:

    def test_api_unreachable(self):
        r = check_api_connectivity(api_reachable=False)
        assert r.passed is False
        assert r.severity == "critical"

    def test_api_high_latency(self):
        r = check_api_connectivity(api_latency_ms=5000)
        assert r.passed is False

    def test_data_stale(self):
        r = check_data_freshness(data_age_seconds=120)
        assert r.passed is False
        assert r.severity == "critical"

    def test_state_unreadable(self):
        r = check_state_integrity(state_readable=False)
        assert r.passed is False

    def test_state_invalid(self):
        r = check_state_integrity(state_readable=True, state_valid=False)
        assert r.passed is False

    def test_clock_skew_large(self):
        r = check_clock_sync(skew_seconds=30)
        assert r.passed is False

    def test_negative_clock_skew(self):
        r = check_clock_sync(skew_seconds=-20)
        assert r.passed is False

    def test_risk_budget_exhausted(self):
        r = check_risk_budget(remaining_risk_pct=0.001)
        assert r.passed is False

    def test_kill_switch_active(self):
        r = check_kill_switch(kill_switch_active=True)
        assert r.passed is False
        assert r.severity == "critical"

    def test_disk_low(self):
        r = check_disk_space(free_disk_mb=50)
        assert r.passed is False
        assert r.severity == "warning"

    def test_memory_high(self):
        r = check_memory_usage(process_memory_mb=2000)
        assert r.passed is False
        assert r.severity == "warning"


# ────────────────────────────────────────────────────────────────────
# 3. AGGREGATE HEALTH CHECK
# ────────────────────────────────────────────────────────────────────

class TestAggregateHealth:

    def test_all_pass(self):
        result = run_health_check(
            api_latency_ms=100, api_reachable=True,
            data_age_seconds=5, state_readable=True, state_valid=True,
            skew_seconds=1, remaining_risk_pct=0.5,
            kill_switch_active=False, free_disk_mb=1000,
            process_memory_mb=256,
        )
        assert result.all_passed is True
        assert result.critical_passed is True
        assert result.summary["trade_allowed"] is True
        assert result.summary["failed"] == 0

    def test_warning_only_still_trades(self):
        result = run_health_check(
            api_latency_ms=100, api_reachable=True,
            data_age_seconds=5, state_readable=True, state_valid=True,
            skew_seconds=1, remaining_risk_pct=0.5,
            kill_switch_active=False,
            free_disk_mb=50,         # warning: low disk
            process_memory_mb=256,
        )
        assert result.all_passed is False
        assert result.critical_passed is True
        assert result.summary["trade_allowed"] is True
        assert "disk_space" in result.summary["warning_failures"]

    def test_critical_failure_blocks_trading(self):
        result = run_health_check(
            api_latency_ms=100, api_reachable=False,  # critical!
            data_age_seconds=5, state_readable=True, state_valid=True,
            skew_seconds=1, remaining_risk_pct=0.5,
            kill_switch_active=False, free_disk_mb=1000,
            process_memory_mb=256,
        )
        assert result.critical_passed is False
        assert result.summary["trade_allowed"] is False
        assert "api_connectivity" in result.summary["critical_failures"]

    def test_multiple_failures(self):
        result = run_health_check(
            api_latency_ms=100, api_reachable=False,
            data_age_seconds=120,
            state_readable=False,
            skew_seconds=30,
            remaining_risk_pct=0.001,
            kill_switch_active=True,
            free_disk_mb=10,
            process_memory_mb=3000,
        )
        assert result.all_passed is False
        assert result.summary["failed"] == 8
        assert result.summary["trade_allowed"] is False

    def test_gate_count(self):
        result = run_health_check()
        assert result.summary["total_gates"] == 8


# ────────────────────────────────────────────────────────────────────
# 4. SLO EVALUATION
# ────────────────────────────────────────────────────────────────────

class TestSLOEvaluation:

    def test_availability_met(self):
        r = evaluate_slo("availability", 99.9)
        assert r["met"] is True
        assert r["margin"] > 0

    def test_availability_missed(self):
        r = evaluate_slo("availability", 98.0)
        assert r["met"] is False
        assert r["margin"] < 0

    def test_latency_met(self):
        r = evaluate_slo("latency_p99", 3000)
        assert r["met"] is True

    def test_latency_missed(self):
        r = evaluate_slo("latency_p99", 8000)
        assert r["met"] is False

    def test_data_freshness_met(self):
        r = evaluate_slo("data_freshness", 30)
        assert r["met"] is True

    def test_error_rate_met(self):
        r = evaluate_slo("error_rate", 0.1)
        assert r["met"] is True

    def test_error_rate_missed(self):
        r = evaluate_slo("error_rate", 2.0)
        assert r["met"] is False

    def test_risk_compliance_perfect(self):
        r = evaluate_slo("risk_compliance", 100.0)
        assert r["met"] is True

    def test_unknown_slo(self):
        r = evaluate_slo("nonexistent", 50)
        assert r["met"] is False
        assert r["target"] is None

    def test_evaluate_all(self):
        actuals = {
            "availability": 99.9,
            "latency_p99": 3000,
            "data_freshness": 30,
            "error_rate": 0.1,
            "risk_compliance": 100.0,
        }
        report = evaluate_all_slos(actuals)
        s = report["summary"]
        assert s["total"] == 5
        assert s["met"] == 5
        assert s["compliance_pct"] == 100.0

    def test_evaluate_all_partial(self):
        actuals = {
            "availability": 99.9,
            "latency_p99": 8000,  # missed
        }
        report = evaluate_all_slos(actuals)
        assert report["summary"]["met"] == 1
        assert report["summary"]["missed"] == 1

    def test_evaluate_all_empty(self):
        report = evaluate_all_slos({})
        assert report["summary"]["total"] == 0


# ────────────────────────────────────────────────────────────────────
# 5. RENDER
# ────────────────────────────────────────────────────────────────────

class TestRenderHealth:

    def test_render_all_pass(self):
        result = run_health_check()
        md = render_health_section(result)
        assert "Health Gates" in md
        assert "ALL CLEAR" in md

    def test_render_with_failures(self):
        result = run_health_check(api_reachable=False)
        md = render_health_section(result)
        assert "BLOCKED" in md
        assert "api_connectivity" in md

    def test_render_slo_report(self):
        report = evaluate_all_slos({
            "availability": 99.9,
            "latency_p99": 3000,
        })
        md = render_slo_section(report)
        assert "SLO Compliance" in md
        assert "availability" in md
        assert "latency_p99" in md


# ────────────────────────────────────────────────────────────────────
# 6. THRESHOLD OVERRIDES
# ────────────────────────────────────────────────────────────────────

class TestThresholdOverrides:

    def test_custom_api_timeout(self):
        custom = {"api_timeout_ms": 100}
        r = check_api_connectivity(api_latency_ms=200, thresholds=custom)
        assert r.passed is False

    def test_custom_data_age(self):
        custom = {"data_max_age_seconds": 10}
        r = check_data_freshness(data_age_seconds=15, thresholds=custom)
        assert r.passed is False

    def test_custom_clock_skew(self):
        custom = {"clock_max_skew_seconds": 1}
        r = check_clock_sync(skew_seconds=5, thresholds=custom)
        assert r.passed is False

    def test_custom_thresholds_in_aggregate(self):
        custom = {"api_timeout_ms": 50, "data_max_age_seconds": 60,
                  "clock_max_skew_seconds": 10, "min_disk_mb": 100,
                  "max_memory_mb": 1024, "min_risk_budget_pct": 0.01}
        result = run_health_check(api_latency_ms=100, thresholds=custom)
        # API should fail with 50ms threshold
        assert result.critical_passed is False


# ────────────────────────────────────────────────────────────────────
# 7. SLO DEFINITIONS REGISTRY
# ────────────────────────────────────────────────────────────────────

class TestSLORegistry:

    def test_all_slos_have_required_fields(self):
        for slo_id, slo in SLO_DEFINITIONS.items():
            assert "description" in slo
            assert "target" in slo
            assert "unit" in slo
            assert "measurement" in slo
            assert isinstance(slo["target"], (int, float))

    def test_five_slos_defined(self):
        assert len(SLO_DEFINITIONS) == 5
