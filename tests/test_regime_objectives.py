"""
Tests for Phase 11A — Regime-Conditioned Objectives

Covers:
  1. Objective tables: per-regime targets existence & sanity
  2. Promotion gate: regime-specific PF/WR/DD gates
  3. Scaling gate: regime-specific scale-up blocking
  4. Edge score: actual vs target comparison (0-100, A-F)
  5. Edge report: multi-regime aggregation
  6. Integration: adaptive weight + regime gate interaction
  7. Integration: capital scaling + regime gate interaction
  8. Rendering: markdown edge report section
"""

import pytest
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.reporting.regime_objectives import (
    REGIME_OBJECTIVES,
    get_objectives,
    check_regime_promotion_gate,
    check_regime_scaling_gate,
    compute_edge_score,
    compute_edge_report,
    get_regime_risk_factor,
    render_edge_report_section,
)
from scripts.reporting.adaptive_weight_engine import (
    evaluate_adaptive_weight,
    WEIGHT_STEPS,
)
from scripts.reporting.capital_scaling_engine import (
    evaluate_scaling,
    DEFAULTS,
)


# ══════════════════════════════════════════════════════════════════════
# 1. OBJECTIVE TABLES
# ══════════════════════════════════════════════════════════════════════

class TestObjectiveTables:

    def test_all_regimes_defined(self):
        for r in ("TREND", "RANGE", "CHOP", "RECOVERY"):
            assert r in REGIME_OBJECTIVES

    def test_trend_highest_targets(self):
        t = REGIME_OBJECTIVES["TREND"]
        r = REGIME_OBJECTIVES["RANGE"]
        c = REGIME_OBJECTIVES["CHOP"]
        assert t["target_pf"] >= r["target_pf"] >= c["target_pf"]
        assert t["max_dd_pct"] >= r["max_dd_pct"] >= c["max_dd_pct"]

    def test_recovery_blocks_promo_and_scale(self):
        rec = REGIME_OBJECTIVES["RECOVERY"]
        assert rec["promo_pf_min"] >= 100  # unreachable
        assert rec["scale_pf_min"] >= 100  # unreachable

    def test_chop_strictest_scale_bar(self):
        c = REGIME_OBJECTIVES["CHOP"]
        t = REGIME_OBJECTIVES["TREND"]
        assert c["scale_pf_min"] >= t["scale_pf_min"]

    def test_risk_factors_ordered(self):
        assert REGIME_OBJECTIVES["TREND"]["risk_factor"] >= REGIME_OBJECTIVES["RANGE"]["risk_factor"]
        assert REGIME_OBJECTIVES["RANGE"]["risk_factor"] >= REGIME_OBJECTIVES["CHOP"]["risk_factor"]
        assert REGIME_OBJECTIVES["CHOP"]["risk_factor"] >= REGIME_OBJECTIVES["RECOVERY"]["risk_factor"]

    def test_get_objectives_known(self):
        obj = get_objectives("TREND")
        assert obj["target_pf"] == 1.5

    def test_get_objectives_unknown_fallback(self):
        obj = get_objectives("ALIEN_REGIME")
        assert obj == REGIME_OBJECTIVES["RANGE"]

    def test_max_trades_per_day(self):
        assert REGIME_OBJECTIVES["TREND"]["max_trades_day"] > REGIME_OBJECTIVES["CHOP"]["max_trades_day"]

    def test_all_keys_present(self):
        required_keys = {"target_pf", "max_dd_pct", "min_winrate", "max_trades_day",
                         "promo_pf_min", "scale_pf_min", "risk_factor"}
        for regime, obj in REGIME_OBJECTIVES.items():
            assert required_keys.issubset(obj.keys()), f"{regime} missing keys"


# ══════════════════════════════════════════════════════════════════════
# 2. PROMOTION GATE
# ══════════════════════════════════════════════════════════════════════

class TestPromotionGate:

    def test_trend_passes_with_strong_kpis(self):
        kpis = {"profit_factor_B": 1.5, "winrate_B": 0.60, "drawdown_B": 3.0}
        ok, reason = check_regime_promotion_gate("TREND", kpis)
        assert ok is True

    def test_trend_fails_low_pf(self):
        kpis = {"profit_factor_B": 1.1, "winrate_B": 0.60, "drawdown_B": 3.0}
        ok, reason = check_regime_promotion_gate("TREND", kpis)
        assert ok is False
        assert "promo_pf_min" in reason

    def test_range_passes_with_adequate_kpis(self):
        kpis = {"profit_factor_B": 1.3, "winrate_B": 0.55, "drawdown_B": 2.0}
        ok, reason = check_regime_promotion_gate("RANGE", kpis)
        assert ok is True

    def test_chop_blocks_moderate_pf(self):
        kpis = {"profit_factor_B": 1.05, "winrate_B": 0.50, "drawdown_B": 2.0}
        ok, reason = check_regime_promotion_gate("CHOP", kpis)
        assert ok is False

    def test_chop_passes_acceptable_pf(self):
        kpis = {"profit_factor_B": 1.15, "winrate_B": 0.50, "drawdown_B": 2.0}
        ok, reason = check_regime_promotion_gate("CHOP", kpis)
        assert ok is True

    def test_recovery_always_blocks(self):
        kpis = {"profit_factor_B": 5.0, "winrate_B": 0.90, "drawdown_B": 0.1}
        ok, reason = check_regime_promotion_gate("RECOVERY", kpis)
        assert ok is False

    def test_dd_breach_blocks(self):
        # RANGE max_dd is 4.0%
        kpis = {"profit_factor_B": 1.5, "winrate_B": 0.55, "drawdown_B": 5.0}
        ok, reason = check_regime_promotion_gate("RANGE", kpis)
        assert ok is False
        assert "max_dd" in reason

    def test_winrate_breach_blocks(self):
        # TREND min_winrate is 55%
        kpis = {"profit_factor_B": 1.5, "winrate_B": 0.50, "drawdown_B": 2.0}
        ok, reason = check_regime_promotion_gate("TREND", kpis)
        assert ok is False
        assert "min_winrate" in reason

    def test_unknown_regime_uses_range(self):
        kpis = {"profit_factor_B": 1.3, "winrate_B": 0.55, "drawdown_B": 2.0}
        ok, reason = check_regime_promotion_gate("UNKNOWN", kpis)
        assert ok is True  # RANGE gate passed


# ══════════════════════════════════════════════════════════════════════
# 3. SCALING GATE
# ══════════════════════════════════════════════════════════════════════

class TestScalingGate:

    def test_trend_allows_scale_up(self):
        kpis = {"profit_factor": 1.5, "max_dd_pct": 3.0}
        ok, reason = check_regime_scaling_gate("TREND", kpis)
        assert ok is True

    def test_trend_blocks_low_pf(self):
        kpis = {"profit_factor": 1.2, "max_dd_pct": 3.0}
        ok, reason = check_regime_scaling_gate("TREND", kpis)
        assert ok is False

    def test_chop_very_high_bar(self):
        # CHOP scale_pf_min = 1.5
        kpis = {"profit_factor": 1.4, "max_dd_pct": 2.0}
        ok, reason = check_regime_scaling_gate("CHOP", kpis)
        assert ok is False

    def test_chop_passes_high_pf(self):
        kpis = {"profit_factor": 1.6, "max_dd_pct": 2.0}
        ok, reason = check_regime_scaling_gate("CHOP", kpis)
        assert ok is True

    def test_recovery_always_blocks(self):
        kpis = {"profit_factor": 5.0, "max_dd_pct": 0.1}
        ok, reason = check_regime_scaling_gate("RECOVERY", kpis)
        assert ok is False

    def test_dd_breach_blocks(self):
        # RANGE max_dd is 4.0%
        kpis = {"profit_factor": 1.5, "max_dd_pct": 5.0}
        ok, reason = check_regime_scaling_gate("RANGE", kpis)
        assert ok is False


# ══════════════════════════════════════════════════════════════════════
# 4. EDGE SCORE
# ══════════════════════════════════════════════════════════════════════

class TestEdgeScore:

    def test_perfect_trend_score(self):
        edge = compute_edge_score("TREND", actual_pf=3.0, actual_dd=0.0, actual_wr=70.0)
        assert edge["score"] >= 85
        assert edge["grade"] == "A"

    def test_failing_trend_score(self):
        edge = compute_edge_score("TREND", actual_pf=0.5, actual_dd=10.0, actual_wr=30.0)
        assert edge["score"] < 40
        assert edge["grade"] == "F"

    def test_borderline_range(self):
        # Exactly at targets
        edge = compute_edge_score("RANGE", actual_pf=1.2, actual_dd=4.0, actual_wr=50.0)
        assert 30 <= edge["score"] <= 60

    def test_has_all_fields(self):
        edge = compute_edge_score("TREND", actual_pf=1.5, actual_dd=3.0, actual_wr=55.0)
        assert "regime" in edge
        assert "score" in edge
        assert "grade" in edge
        assert "pf_ratio" in edge
        assert "dd_ratio" in edge
        assert "wr_ratio" in edge
        assert "verdict" in edge

    def test_score_bounded_0_100(self):
        # Extreme values
        edge = compute_edge_score("CHOP", actual_pf=0.0, actual_dd=100.0, actual_wr=0.0)
        assert 0 <= edge["score"] <= 100

        edge = compute_edge_score("CHOP", actual_pf=100.0, actual_dd=0.0, actual_wr=100.0)
        assert 0 <= edge["score"] <= 100

    def test_grade_a_threshold(self):
        edge = compute_edge_score("CHOP", actual_pf=2.0, actual_dd=0.0, actual_wr=80.0)
        assert edge["grade"] == "A"
        assert edge["score"] >= 85

    def test_verdict_contains_regime(self):
        edge = compute_edge_score("TREND", actual_pf=1.5, actual_dd=3.0, actual_wr=55.0)
        assert "TREND" in edge["verdict"]


# ══════════════════════════════════════════════════════════════════════
# 5. EDGE REPORT (multi-regime)
# ══════════════════════════════════════════════════════════════════════

class TestEdgeReport:

    def _make_regime_analytics(self, regimes_data):
        """Build a regime_analytics dict from per-regime data."""
        per_regime = {}
        for r, data in regimes_data.items():
            per_regime[r] = {
                "days": data.get("days", 5),
                "avg_pf": data.get("avg_pf", 1.0),
                "max_dd": data.get("max_dd", 3.0),
                "avg_winrate": data.get("avg_winrate", 50.0),
            }
        return {"per_regime": per_regime}

    def test_empty_analytics(self):
        report = compute_edge_report({"per_regime": {}})
        assert report["best_edge_regime"] == "N/A"
        assert report["worst_edge_regime"] == "N/A"
        assert report["actionable_regimes"] == []

    def test_single_regime(self):
        analytics = self._make_regime_analytics({
            "TREND": {"avg_pf": 2.0, "max_dd": 2.0, "avg_winrate": 60.0}
        })
        report = compute_edge_report(analytics)
        assert report["best_edge_regime"] == "TREND"
        assert report["worst_edge_regime"] == "TREND"
        assert "TREND" in report["actionable_regimes"]

    def test_best_worst_identification(self):
        analytics = self._make_regime_analytics({
            "TREND": {"avg_pf": 2.0, "max_dd": 1.0, "avg_winrate": 65.0},
            "CHOP": {"avg_pf": 0.5, "max_dd": 5.0, "avg_winrate": 30.0},
        })
        report = compute_edge_report(analytics)
        assert report["best_edge_regime"] == "TREND"
        assert report["worst_edge_regime"] == "CHOP"

    def test_actionable_vs_avoid(self):
        analytics = self._make_regime_analytics({
            "TREND": {"avg_pf": 2.0, "max_dd": 1.0, "avg_winrate": 65.0},
            "CHOP": {"avg_pf": 0.3, "max_dd": 8.0, "avg_winrate": 25.0},
        })
        report = compute_edge_report(analytics)
        assert "TREND" in report["actionable_regimes"]
        assert "CHOP" in report["avoid_regimes"]

    def test_skips_zero_day_regimes(self):
        analytics = {"per_regime": {
            "TREND": {"days": 5, "avg_pf": 1.5, "max_dd": 2.0, "avg_winrate": 55.0},
            "CHOP": {"days": 0, "avg_pf": 0.0, "max_dd": 0.0, "avg_winrate": 0.0},
        }}
        report = compute_edge_report(analytics)
        assert "CHOP" not in report["per_regime"]

    def test_has_avg_edge_score(self):
        analytics = self._make_regime_analytics({
            "TREND": {"avg_pf": 1.5, "max_dd": 3.0, "avg_winrate": 55.0},
        })
        report = compute_edge_report(analytics)
        assert "avg_edge_score" in report
        assert isinstance(report["avg_edge_score"], float)

    def test_fitness_summary_present(self):
        analytics = self._make_regime_analytics({
            "TREND": {"avg_pf": 1.5, "max_dd": 3.0, "avg_winrate": 55.0},
        })
        report = compute_edge_report(analytics)
        assert len(report["regime_fitness_summary"]) > 10


# ══════════════════════════════════════════════════════════════════════
# 6. RISK FACTOR
# ══════════════════════════════════════════════════════════════════════

class TestRegimeRiskFactor:

    def test_trend_full_risk(self):
        assert get_regime_risk_factor("TREND") == 1.0

    def test_range_reduced(self):
        assert get_regime_risk_factor("RANGE") == 0.85

    def test_chop_heavily_reduced(self):
        assert get_regime_risk_factor("CHOP") == 0.7

    def test_recovery_minimal(self):
        assert get_regime_risk_factor("RECOVERY") == 0.5

    def test_unknown_uses_range(self):
        assert get_regime_risk_factor("UNKNOWN") == 0.85


# ══════════════════════════════════════════════════════════════════════
# 7. INTEGRATION: adaptive weight + regime gate
# ══════════════════════════════════════════════════════════════════════

class TestAdaptiveWeightRegimeGate:

    def _good_kpis(self):
        return {
            "decisions": 50,
            "expectancy_B": 1.5, "expectancy_A": 1.0,
            "profit_factor_B": 1.6, "winrate_B": 0.65, "winrate_A": 0.55,
            "drawdown_B": 2.0, "drawdown_A": 2.5,
            "losing_streak_B": 2,
        }

    def _state(self):
        return {"cooldown_ok": True, "last_decision": "HOLD", "current_weight": 0.0}

    def test_trend_allows_promotion(self):
        kpis = self._good_kpis()
        regime = {"effective_regime": "TREND", "freeze_adaptive": False, "adaptive_weight_cap": 0.5}
        new_w, decision = evaluate_adaptive_weight(0.0, kpis, kpis, self._state(), regime_info=regime)
        assert decision == "PROMOTE"
        assert new_w == 0.1

    def test_chop_blocks_low_pf_promotion(self):
        """In CHOP, promo_pf_min=1.1 — PF(B)=1.05 should block."""
        kpis = self._good_kpis()
        kpis["profit_factor_B"] = 1.05  # below CHOP promo_pf_min=1.1
        regime = {"effective_regime": "CHOP", "freeze_adaptive": False, "adaptive_weight_cap": 0.5}
        new_w, decision = evaluate_adaptive_weight(0.0, kpis, kpis, self._state(), regime_info=regime)
        assert decision == "HOLD"

    def test_recovery_blocks_all_promotions(self):
        kpis = self._good_kpis()
        kpis["profit_factor_B"] = 5.0
        regime = {"effective_regime": "RECOVERY", "freeze_adaptive": False, "adaptive_weight_cap": 0.5}
        new_w, decision = evaluate_adaptive_weight(0.0, kpis, kpis, self._state(), regime_info=regime)
        assert decision == "HOLD"

    def test_no_regime_info_still_promotes(self):
        """Without regime_info, promotion gate is skipped — backward compatible."""
        kpis = self._good_kpis()
        new_w, decision = evaluate_adaptive_weight(0.0, kpis, kpis, self._state(), regime_info=None)
        assert decision == "PROMOTE"


# ══════════════════════════════════════════════════════════════════════
# 8. INTEGRATION: capital scaling + regime gate
# ══════════════════════════════════════════════════════════════════════

class TestCapitalScalingRegimeGate:

    def test_trend_allows_scaleup(self):
        kpis = {"profit_factor": 1.5, "max_dd_pct": 3.0, "trades": 60, "losing_streak": 2}
        regime = {"effective_regime": "TREND", "block_scale_up": False,
                  "scaling_cap_factor": 1.0, "risk_reduction_factor": 1.0}
        new_pct, decision, reason = evaluate_scaling(
            0.5, kpis, adaptive_weight=0.3, regime_info=regime
        )
        assert decision == "SCALE_UP"

    def test_chop_blocks_moderate_pf_scaleup(self):
        """CHOP scale_pf_min=1.5, PF=1.3 → blocked."""
        kpis = {"profit_factor": 1.3, "max_dd_pct": 2.0, "trades": 60, "losing_streak": 1}
        regime = {"effective_regime": "CHOP", "block_scale_up": False,
                  "scaling_cap_factor": 1.0, "risk_reduction_factor": 1.0}
        new_pct, decision, reason = evaluate_scaling(
            0.5, kpis, adaptive_weight=0.3, regime_info=regime
        )
        assert decision == "HOLD"
        assert "regime objectives" in reason.lower()

    def test_recovery_blocks_scaleup(self):
        kpis = {"profit_factor": 5.0, "max_dd_pct": 0.5, "trades": 60, "losing_streak": 0}
        regime = {"effective_regime": "RECOVERY", "block_scale_up": False,
                  "scaling_cap_factor": 1.0, "risk_reduction_factor": 1.0}
        new_pct, decision, reason = evaluate_scaling(
            0.5, kpis, adaptive_weight=0.3, regime_info=regime
        )
        assert decision == "HOLD"

    def test_no_regime_info_backward_compatible(self):
        kpis = {"profit_factor": 1.5, "max_dd_pct": 3.0, "trades": 60, "losing_streak": 2}
        new_pct, decision, reason = evaluate_scaling(
            0.5, kpis, adaptive_weight=0.3, regime_info=None
        )
        assert decision == "SCALE_UP"


# ══════════════════════════════════════════════════════════════════════
# 9. MARKDOWN RENDERING
# ══════════════════════════════════════════════════════════════════════

class TestRenderEdgeReport:

    def test_renders_table(self):
        analytics = {"per_regime": {
            "TREND": {"days": 5, "avg_pf": 2.0, "max_dd": 2.0, "avg_winrate": 60.0},
        }}
        report = compute_edge_report(analytics)
        md = render_edge_report_section(report)
        assert "Edge per Regime" in md
        assert "TREND" in md
        assert "Score" in md

    def test_empty_report(self):
        report = compute_edge_report({"per_regime": {}})
        md = render_edge_report_section(report)
        assert "Edge per Regime" in md
        assert "N/A" in md
