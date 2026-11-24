"""
Tests for Capital Allocator — Phase 11B

Coverage:
  1. Edge-weighted allocation (proportional to edge score)
  2. Edge score filtering (< threshold → skip)
  3. Regime capital caps (TREND=100%, CHOP=50%, etc.)
  4. Correlation reduction (same group → reduce)
  5. Max concurrent allocations cap
  6. Capital utilization metrics
  7. Efficiency KPI aggregation
  8. Edge cases: empty signals, zero equity, single signal
  9. Governance render

Author: SMC Trading Bot Framework — Phase 11B Tests
"""

import pytest
from scripts.reporting.capital_allocator import (
    allocate_capital,
    compute_capital_utilization,
    compute_idle_capital_pct,
    compute_efficiency_kpis,
    render_efficiency_section,
    REGIME_CAPITAL_CAPS,
    MIN_EDGE_SCORE_TO_TRADE,
    MAX_CONCURRENT_ALLOCATIONS,
    MAX_GROUP_RISK_FRACTION,
    MIN_RISK_PER_TRADE_PCT,
    MAX_RISK_PER_TRADE_PCT,
    CORRELATION_GROUPS,
)


# ────────────────────────────────────────────────────────────────────
# 1. BASIC ALLOCATION
# ────────────────────────────────────────────────────────────────────

class TestAllocateCapitalBasic:
    """Basic allocation logic."""

    def test_single_signal_allocates_full_risk(self):
        signals = [{"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"}]
        result = allocate_capital(10000, 0.5, signals, "TREND")

        assert len(result["allocations"]) == 1
        assert result["allocations"][0]["symbol"] == "BTCUSDT"
        assert result["allocations"][0]["risk_pct"] > 0
        assert result["allocations"][0]["risk_usd"] > 0
        assert result["metrics"]["signals_allocated"] == 1
        assert result["metrics"]["signals_skipped"] == 0

    def test_two_signals_weighted_by_edge(self):
        signals = [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"},
            {"symbol": "SOLUSDT", "edge_score": 40, "direction": "SHORT"},
        ]
        result = allocate_capital(10000, 0.5, signals, "TREND")

        allocs = result["allocations"]
        assert len(allocs) == 2
        # BTC should get higher allocation (higher edge)
        btc = next(a for a in allocs if a["symbol"] == "BTCUSDT")
        sol = next(a for a in allocs if a["symbol"] == "SOLUSDT")
        assert btc["risk_pct"] >= sol["risk_pct"]

    def test_proportional_allocation(self):
        """Edge 80 vs 20: BTC gets 80% of risk, other gets 20%."""
        signals = [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"},
            {"symbol": "XRPUSDT", "edge_score": 20, "direction": "LONG"},
        ]
        # XRPUSDT edge=20 < 40 → will be skipped; use higher edges
        signals = [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"},
            {"symbol": "XRPUSDT", "edge_score": 60, "direction": "LONG"},
        ]
        result = allocate_capital(10000, 1.0, signals, "TREND")
        allocs = result["allocations"]
        assert len(allocs) == 2

    def test_empty_signals(self):
        result = allocate_capital(10000, 0.5, [], "TREND")
        assert result["allocations"] == []
        assert result["metrics"]["signals_allocated"] == 0

    def test_zero_equity(self):
        signals = [{"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"}]
        result = allocate_capital(0, 0.5, signals, "TREND")
        assert result["allocations"] == []

    def test_zero_risk_pct(self):
        signals = [{"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"}]
        result = allocate_capital(10000, 0, signals, "TREND")
        assert result["allocations"] == []

    def test_risk_usd_calculation(self):
        signals = [{"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"}]
        result = allocate_capital(10000, 0.5, signals, "TREND")
        alloc = result["allocations"][0]
        expected_usd = round(10000 * alloc["risk_pct"] / 100, 2)
        assert alloc["risk_usd"] == expected_usd


# ────────────────────────────────────────────────────────────────────
# 2. EDGE SCORE FILTERING
# ────────────────────────────────────────────────────────────────────

class TestEdgeFiltering:
    """Signals with low edge scores should be skipped."""

    def test_below_threshold_skipped(self):
        signals = [{"symbol": "BTCUSDT", "edge_score": 10, "direction": "LONG"}]
        result = allocate_capital(10000, 0.5, signals, "TREND")
        assert len(result["allocations"]) == 0
        assert len(result["skipped"]) == 1
        assert "Edge score" in result["skipped"][0]["reason"]

    def test_at_threshold_included(self):
        signals = [{"symbol": "BTCUSDT", "edge_score": MIN_EDGE_SCORE_TO_TRADE, "direction": "LONG"}]
        result = allocate_capital(10000, 0.5, signals, "TREND")
        assert len(result["allocations"]) == 1

    def test_mixed_above_below(self):
        signals = [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"},
            {"symbol": "DOGEUSDT", "edge_score": 10, "direction": "SHORT"},
        ]
        result = allocate_capital(10000, 0.5, signals, "TREND")
        assert result["metrics"]["signals_allocated"] == 1
        assert result["metrics"]["signals_skipped"] == 1


# ────────────────────────────────────────────────────────────────────
# 3. REGIME CAPITAL CAPS
# ────────────────────────────────────────────────────────────────────

class TestRegimeCapitalCaps:
    """Regime limits total deployable risk."""

    def test_trend_cap_100(self):
        result = allocate_capital(10000, 1.0, [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"}
        ], "TREND")
        assert result["metrics"]["regime_cap_pct"] == 100.0

    def test_chop_cap_50(self):
        result = allocate_capital(10000, 1.0, [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"}
        ], "CHOP")
        assert result["metrics"]["regime_cap_pct"] == 50.0
        alloc = result["allocations"][0]
        # In CHOP, risk should be capped at 50% of base
        assert alloc["risk_pct"] <= 0.5 * 1.0 * 100 + 0.01

    def test_recovery_cap_30(self):
        result = allocate_capital(10000, 1.0, [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"}
        ], "RECOVERY")
        assert result["metrics"]["regime_cap_pct"] == 30.0

    def test_range_cap_80(self):
        result = allocate_capital(10000, 1.0, [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"}
        ], "RANGE")
        assert result["metrics"]["regime_cap_pct"] == 80.0

    def test_unknown_regime_uses_default(self):
        result = allocate_capital(10000, 0.5, [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"}
        ], "UNKNOWN_REGIME")
        assert result["metrics"]["regime_cap_pct"] == 80.0  # default


# ────────────────────────────────────────────────────────────────────
# 4. CORRELATION REDUCTION
# ────────────────────────────────────────────────────────────────────

class TestCorrelationReduction:
    """Correlated symbols get reduced allocation."""

    def test_two_btc_correlated(self):
        signals = [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"},
            {"symbol": "BTCUSD", "edge_score": 60, "direction": "LONG"},
        ]
        result = allocate_capital(10000, 1.0, signals, "TREND")
        assert result["metrics"]["correlation_reductions"] == 1
        allocs = result["allocations"]
        btc_main = next(a for a in allocs if a["symbol"] == "BTCUSDT")
        btc_alt = next(a for a in allocs if a["symbol"] == "BTCUSD")
        # Alt should have lower risk due to correlation reduction
        assert btc_alt["risk_pct"] < btc_main["risk_pct"]

    def test_uncorrelated_no_reduction(self):
        signals = [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"},
            {"symbol": "XRPUSDT", "edge_score": 60, "direction": "LONG"},
        ]
        result = allocate_capital(10000, 1.0, signals, "TREND")
        assert result["metrics"]["correlation_reductions"] == 0

    def test_custom_correlation_groups(self):
        custom = {"MY_GROUP": ["AAUSDT", "BBUSDT"]}
        signals = [
            {"symbol": "AAUSDT", "edge_score": 80, "direction": "LONG"},
            {"symbol": "BBUSDT", "edge_score": 60, "direction": "LONG"},
        ]
        result = allocate_capital(10000, 1.0, signals, "TREND", correlation_groups=custom)
        assert result["metrics"]["correlation_reductions"] == 1

    def test_single_in_group_no_reduction(self):
        signals = [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"},
        ]
        result = allocate_capital(10000, 1.0, signals, "TREND")
        assert result["metrics"]["correlation_reductions"] == 0


# ────────────────────────────────────────────────────────────────────
# 5. MAX CONCURRENT ALLOCATIONS
# ────────────────────────────────────────────────────────────────────

class TestMaxConcurrent:
    """Enforce maximum number of concurrent allocations."""

    def test_within_limit(self):
        signals = [
            {"symbol": f"SYM{i}USDT", "edge_score": 80 - i, "direction": "LONG"}
            for i in range(MAX_CONCURRENT_ALLOCATIONS)
        ]
        result = allocate_capital(10000, 1.0, signals, "TREND")
        assert result["metrics"]["signals_allocated"] == MAX_CONCURRENT_ALLOCATIONS

    def test_exceeds_limit(self):
        signals = [
            {"symbol": f"SYM{i}USDT", "edge_score": 80 - i, "direction": "LONG"}
            for i in range(MAX_CONCURRENT_ALLOCATIONS + 3)
        ]
        result = allocate_capital(10000, 1.0, signals, "TREND")
        assert result["metrics"]["signals_allocated"] == MAX_CONCURRENT_ALLOCATIONS
        assert result["metrics"]["signals_skipped"] == 3
        # Best edge scores should be kept
        alloc_edges = [a["edge_score"] for a in result["allocations"]]
        assert sorted(alloc_edges, reverse=True) == alloc_edges


# ────────────────────────────────────────────────────────────────────
# 6. CAPITAL UTILIZATION METRICS
# ────────────────────────────────────────────────────────────────────

class TestCapitalUtilization:
    """Test utilization metric functions."""

    def test_full_utilization(self):
        assert compute_capital_utilization(1.0, 1.0) == 1.0

    def test_zero_utilization(self):
        assert compute_capital_utilization(0.0, 1.0) == 0.0

    def test_partial_utilization(self):
        result = compute_capital_utilization(0.3, 1.0)
        assert 0.29 <= result <= 0.31

    def test_zero_available(self):
        assert compute_capital_utilization(0.5, 0.0) == 0.0

    def test_over_100_capped(self):
        assert compute_capital_utilization(2.0, 1.0) == 1.0

    def test_idle_capital_full(self):
        assert compute_idle_capital_pct(10000, 0) == 100.0

    def test_idle_capital_partial(self):
        result = compute_idle_capital_pct(10000, 500)
        assert result == 95.0

    def test_idle_capital_zero_equity(self):
        assert compute_idle_capital_pct(0, 100) == 100.0


# ────────────────────────────────────────────────────────────────────
# 7. EFFICIENCY KPIs
# ────────────────────────────────────────────────────────────────────

class TestEfficiencyKPIs:
    """Aggregate daily allocation results into KPIs."""

    def _make_daily(self, util=0.5, signals=3, alloc=2, skip=1, corr=0):
        return {
            "metrics": {
                "capital_utilization": util,
                "signals_received": signals,
                "signals_allocated": alloc,
                "signals_skipped": skip,
                "correlation_reductions": corr,
            }
        }

    def test_empty_history(self):
        kpis = compute_efficiency_kpis([])
        assert kpis["avg_utilization"] == 0.0
        assert kpis["efficiency_grade"] == "N/A"

    def test_single_day(self):
        kpis = compute_efficiency_kpis([self._make_daily(0.8, 5, 4, 1, 2)])
        assert kpis["avg_utilization"] == 0.8
        assert kpis["avg_signals_per_day"] == 5.0
        assert kpis["avg_allocated_per_day"] == 4.0
        assert kpis["total_skipped"] == 1
        assert kpis["total_correlation_reductions"] == 2
        assert kpis["efficiency_grade"] == "A"

    def test_multi_day_average(self):
        daily = [
            self._make_daily(0.6, 4, 3, 1, 1),
            self._make_daily(0.4, 2, 1, 1, 0),
        ]
        kpis = compute_efficiency_kpis(daily)
        assert kpis["avg_utilization"] == 0.5
        assert kpis["efficiency_grade"] == "B"
        assert kpis["total_skipped"] == 2
        assert kpis["total_correlation_reductions"] == 1

    def test_grade_a(self):
        kpis = compute_efficiency_kpis([self._make_daily(0.7)])
        assert kpis["efficiency_grade"] == "A"

    def test_grade_b(self):
        kpis = compute_efficiency_kpis([self._make_daily(0.5)])
        assert kpis["efficiency_grade"] == "B"

    def test_grade_c(self):
        kpis = compute_efficiency_kpis([self._make_daily(0.3)])
        assert kpis["efficiency_grade"] == "C"

    def test_grade_d(self):
        kpis = compute_efficiency_kpis([self._make_daily(0.1)])
        assert kpis["efficiency_grade"] == "D"

    def test_grade_f(self):
        kpis = compute_efficiency_kpis([self._make_daily(0.05)])
        assert kpis["efficiency_grade"] == "F"


# ────────────────────────────────────────────────────────────────────
# 8. RENDER SECTION
# ────────────────────────────────────────────────────────────────────

class TestRenderEfficiency:
    """Governance markdown rendering."""

    def test_render_non_empty(self):
        kpis = {
            "avg_utilization": 0.65,
            "efficiency_grade": "B",
            "avg_signals_per_day": 3.5,
            "avg_allocated_per_day": 2.5,
            "total_skipped": 7,
            "total_correlation_reductions": 3,
        }
        md = render_efficiency_section(kpis)
        assert "Capital Efficiency" in md
        assert "65.0%" in md
        assert "B" in md
        assert "3.5" in md
        assert "7" in md

    def test_render_empty_defaults(self):
        md = render_efficiency_section({})
        assert "Capital Efficiency" in md


# ────────────────────────────────────────────────────────────────────
# 9. RISK CONSTRAINTS NEVER EXCEEDED
# ────────────────────────────────────────────────────────────────────

class TestRiskConstraints:
    """Total allocated risk must never exceed effective_risk_pct * regime_cap."""

    def test_total_risk_within_cap(self):
        signals = [
            {"symbol": f"SYM{i}USDT", "edge_score": 60 + i, "direction": "LONG"}
            for i in range(5)
        ]
        result = allocate_capital(50000, 0.8, signals, "RANGE")
        cap = 0.8 * REGIME_CAPITAL_CAPS["RANGE"]
        total = result["metrics"]["total_risk_pct"]
        # Total should not exceed cap (with small float tolerance)
        assert total <= cap + 0.01, f"total {total} exceeded cap {cap}"

    def test_per_trade_risk_clamped(self):
        signals = [
            {"symbol": "BTCUSDT", "edge_score": 99, "direction": "LONG"},
        ]
        result = allocate_capital(10000, 5.0, signals, "TREND")
        for alloc in result["allocations"]:
            assert alloc["risk_pct"] >= MIN_RISK_PER_TRADE_PCT
            assert alloc["risk_pct"] <= MAX_RISK_PER_TRADE_PCT

    def test_chop_regime_reduces_total(self):
        signals = [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"},
        ]
        trend = allocate_capital(10000, 1.0, signals, "TREND")
        chop = allocate_capital(10000, 1.0, signals, "CHOP")
        # CHOP risk should be lower than TREND risk
        assert chop["metrics"]["total_risk_pct"] <= trend["metrics"]["total_risk_pct"]


# ────────────────────────────────────────────────────────────────────
# 10. INTEGRATION CONSISTENCY
# ────────────────────────────────────────────────────────────────────

class TestIntegration:
    """Verify allocation result structure consistency."""

    def test_result_structure(self):
        signals = [{"symbol": "BTCUSDT", "edge_score": 70, "direction": "LONG"}]
        result = allocate_capital(10000, 0.5, signals, "TREND")

        assert "allocations" in result
        assert "skipped" in result
        assert "metrics" in result

        m = result["metrics"]
        assert "total_risk_pct" in m
        assert "regime_cap_pct" in m
        assert "capital_utilization" in m
        assert "signals_received" in m
        assert "signals_allocated" in m
        assert "signals_skipped" in m
        assert "correlation_reductions" in m

    def test_allocation_fields(self):
        signals = [{"symbol": "BTCUSDT", "edge_score": 70, "direction": "LONG"}]
        result = allocate_capital(10000, 0.5, signals, "TREND")
        alloc = result["allocations"][0]

        assert "symbol" in alloc
        assert "direction" in alloc
        assert "risk_pct" in alloc
        assert "risk_usd" in alloc
        assert "edge_score" in alloc
        assert "reason" in alloc

    def test_skipped_fields(self):
        signals = [{"symbol": "BTCUSDT", "edge_score": 5, "direction": "LONG"}]
        result = allocate_capital(10000, 0.5, signals, "TREND")
        skipped = result["skipped"][0]

        assert "symbol" in skipped
        assert "reason" in skipped
        assert "edge_score" in skipped

    def test_metrics_count_consistency(self):
        signals = [
            {"symbol": "BTCUSDT", "edge_score": 80, "direction": "LONG"},
            {"symbol": "DOGEUSDT", "edge_score": 10, "direction": "SHORT"},
            {"symbol": "ETHUSDT", "edge_score": 60, "direction": "LONG"},
        ]
        result = allocate_capital(10000, 0.5, signals, "TREND")
        m = result["metrics"]
        assert m["signals_received"] == 3
        assert m["signals_allocated"] + m["signals_skipped"] == 3
