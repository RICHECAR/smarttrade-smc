"""
Tests for Phase 10 — Regime Analytics Tracker

Covers:
  - Daily snapshot loading (with/without data)
  - Per-regime KPI aggregation
  - Stability score computation (A-F grading)
  - Edge cases: empty snapshots, single regime, no trades
  - Warning generation
  - Transition counting
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.reporting.regime_analytics import (
    load_daily_snapshots,
    compute_regime_analytics,
    compute_regime_stability_score,
    REGIMES,
    _empty_analytics,
)


# ── Helpers ───────────────────────────────────────────────────────────

def _make_snapshot(
    date_str: str,
    regime: str = "TREND",
    pnl: float = 10.0,
    trades: int = 3,
    winrate: float = 60.0,
    max_dd: float = 2.0,
    pf: float = 1.5,
    risk_factor: float = 1.0,
    ks_block: int = 0,
    vol_pause: bool = False,
    recovery: bool = False,
):
    """Create a minimal daily snapshot dict."""
    return {
        "report_meta": {"date": date_str},
        "data": {
            "performance": {
                "pnl_abs": pnl,
                "n_trades": trades,
                "winrate": winrate,
                "max_dd": max_dd,
                "profit_factor": pf,
            },
            "regime": {
                "regime": regime,
                "effective_regime": "RECOVERY" if recovery else regime,
                "risk_factor": risk_factor,
                "vol_pause_active": vol_pause,
                "recovery_active": recovery,
            },
            "kill_switch": {
                "block_count": ks_block,
            },
            "capital_scaling": {
                "current_risk_pct": 0.5,
            },
        },
    }


# ── compute_regime_analytics ─────────────────────────────────────────

class TestComputeRegimeAnalytics:

    def test_empty_snapshots(self):
        result = compute_regime_analytics([])
        assert result["total_days"] == 0
        assert result["best_regime"] == "N/A"
        assert result["worst_regime"] == "N/A"
        assert result["regime_transitions"] == 0

    def test_single_day_trend(self):
        snaps = [_make_snapshot("2025-01-01", regime="TREND", pnl=20.0, trades=5)]
        result = compute_regime_analytics(snaps)
        assert result["total_days"] == 1
        t = result["per_regime"]["TREND"]
        assert t["days"] == 1
        assert t["pct_time"] == 100.0
        assert t["total_pnl"] == 20.0
        assert t["avg_pnl"] == 20.0
        assert t["total_trades"] == 5

    def test_multi_regime_distribution(self):
        snaps = [
            _make_snapshot("2025-01-01", regime="TREND", pnl=10.0),
            _make_snapshot("2025-01-02", regime="TREND", pnl=15.0),
            _make_snapshot("2025-01-03", regime="RANGE", pnl=-5.0),
            _make_snapshot("2025-01-04", regime="CHOP", pnl=-2.0),
        ]
        result = compute_regime_analytics(snaps)
        assert result["total_days"] == 4
        assert result["per_regime"]["TREND"]["days"] == 2
        assert result["per_regime"]["TREND"]["pct_time"] == 50.0
        assert result["per_regime"]["RANGE"]["days"] == 1
        assert result["per_regime"]["CHOP"]["days"] == 1

    def test_best_worst_regime(self):
        snaps = [
            _make_snapshot("2025-01-01", regime="TREND", pnl=30.0),
            _make_snapshot("2025-01-02", regime="RANGE", pnl=-20.0),
        ]
        result = compute_regime_analytics(snaps)
        assert result["best_regime"] == "TREND"
        assert result["worst_regime"] == "RANGE"

    def test_regime_transitions(self):
        snaps = [
            _make_snapshot("2025-01-01", regime="TREND"),
            _make_snapshot("2025-01-02", regime="TREND"),
            _make_snapshot("2025-01-03", regime="RANGE"),
            _make_snapshot("2025-01-04", regime="CHOP"),
            _make_snapshot("2025-01-05", regime="TREND"),
        ]
        result = compute_regime_analytics(snaps)
        assert result["regime_transitions"] == 3  # TREND→RANGE, RANGE→CHOP, CHOP→TREND

    def test_no_transitions_same_regime(self):
        snaps = [
            _make_snapshot("2025-01-01", regime="TREND"),
            _make_snapshot("2025-01-02", regime="TREND"),
        ]
        result = compute_regime_analytics(snaps)
        assert result["regime_transitions"] == 0

    def test_pnl_aggregation(self):
        snaps = [
            _make_snapshot("2025-01-01", regime="TREND", pnl=10.0),
            _make_snapshot("2025-01-02", regime="TREND", pnl=20.0),
            _make_snapshot("2025-01-03", regime="TREND", pnl=-5.0),
        ]
        result = compute_regime_analytics(snaps)
        t = result["per_regime"]["TREND"]
        assert abs(t["total_pnl"] - 25.0) < 0.01
        assert abs(t["avg_pnl"] - 25.0 / 3) < 0.1

    def test_max_dd_takes_maximum(self):
        snaps = [
            _make_snapshot("2025-01-01", regime="TREND", max_dd=2.0),
            _make_snapshot("2025-01-02", regime="TREND", max_dd=5.0),
            _make_snapshot("2025-01-03", regime="TREND", max_dd=3.0),
        ]
        result = compute_regime_analytics(snaps)
        assert result["per_regime"]["TREND"]["max_dd"] == 5.0

    def test_kill_switch_blocks_counted(self):
        snaps = [
            _make_snapshot("2025-01-01", regime="TREND", ks_block=2),
            _make_snapshot("2025-01-02", regime="TREND", ks_block=1),
        ]
        result = compute_regime_analytics(snaps)
        assert result["per_regime"]["TREND"]["kill_switch_blocks"] == 3

    def test_vol_pause_days_counted(self):
        snaps = [
            _make_snapshot("2025-01-01", regime="TREND", vol_pause=True),
            _make_snapshot("2025-01-02", regime="TREND", vol_pause=False),
            _make_snapshot("2025-01-03", regime="TREND", vol_pause=True),
        ]
        result = compute_regime_analytics(snaps)
        assert result["per_regime"]["TREND"]["vol_pause_days"] == 2

    def test_recovery_days_counted(self):
        snaps = [
            _make_snapshot("2025-01-01", regime="RANGE", recovery=True),
            _make_snapshot("2025-01-02", regime="RANGE", recovery=False),
        ]
        result = compute_regime_analytics(snaps)
        assert result["per_regime"]["RECOVERY"]["recovery_days"] >= 1

    def test_warnings_generated_for_excessive_ks(self):
        snaps = [
            _make_snapshot("2025-01-01", regime="TREND", ks_block=5),
        ]
        result = compute_regime_analytics(snaps)
        found = any("kill-switch" in w.lower() for w in result.get("warnings", []))
        assert found

    def test_warnings_generated_for_chop_dominance(self):
        snaps = [_make_snapshot(f"2025-01-{i+1:02d}", regime="CHOP", pnl=-3.0) for i in range(5)]
        result = compute_regime_analytics(snaps)
        found = any("chop" in w.lower() for w in result.get("warnings", []))
        assert found


class TestStabilityScore:

    def test_perfect_score(self):
        """No transitions, no recovery, no kill-switch, profitable dominant."""
        snaps = [_make_snapshot(f"2025-01-{i+1:02d}", regime="TREND", pnl=10.0) for i in range(10)]
        analytics = compute_regime_analytics(snaps)
        stability = compute_regime_stability_score(analytics)
        assert stability["grade"] in ("A", "B")
        assert stability["score"] >= 70

    def test_poor_score_many_transitions(self):
        regimes = ["TREND", "RANGE", "CHOP", "TREND", "RANGE", "CHOP", "TREND", "RANGE"]
        snaps = [_make_snapshot(f"2025-01-{i+1:02d}", regime=regimes[i], pnl=-5.0) for i in range(8)]
        analytics = compute_regime_analytics(snaps)
        stability = compute_regime_stability_score(analytics)
        assert stability["score"] < 70

    def test_empty_analytics_gives_zero(self):
        analytics = _empty_analytics()
        stability = compute_regime_stability_score(analytics)
        assert stability["score"] == 0
        assert stability["grade"] == "N/A"

    def test_has_breakdown(self):
        snaps = [_make_snapshot("2025-01-01", regime="TREND")]
        analytics = compute_regime_analytics(snaps)
        stability = compute_regime_stability_score(analytics)
        assert "breakdown" in stability
        assert "transition_stability" in stability["breakdown"]
        assert "recovery_burden" in stability["breakdown"]
        assert "kill_switch_safety" in stability["breakdown"]
        assert "dominant_profitability" in stability["breakdown"]

    def test_has_assessment(self):
        snaps = [_make_snapshot("2025-01-01", regime="TREND")]
        analytics = compute_regime_analytics(snaps)
        stability = compute_regime_stability_score(analytics)
        assert "assessment" in stability
        assert len(stability["assessment"]) > 10

    def test_grade_a_is_90_plus(self):
        snaps = [_make_snapshot(f"2025-01-{i+1:02d}", regime="TREND", pnl=20.0) for i in range(20)]
        analytics = compute_regime_analytics(snaps)
        stability = compute_regime_stability_score(analytics)
        if stability["score"] >= 90:
            assert stability["grade"] == "A"

    def test_grade_f_is_below_40(self):
        # Many transitions, recovery, ks blocks, losses
        regimes = ["TREND", "CHOP", "RANGE", "TREND", "CHOP"]
        snaps = [
            _make_snapshot(f"2025-01-{i+1:02d}", regime=regimes[i], pnl=-20.0, ks_block=3, recovery=True)
            for i in range(5)
        ]
        analytics = compute_regime_analytics(snaps)
        stability = compute_regime_stability_score(analytics)
        if stability["score"] < 40:
            assert stability["grade"] == "F"


class TestLoadDailySnapshots:

    def test_empty_dir(self, tmp_path):
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        from datetime import date
        result = load_daily_snapshots(tmp_path, date(2025, 1, 1), date(2025, 1, 7))
        assert result == []

    def test_loads_existing_files(self, tmp_path):
        import json
        from datetime import date

        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        snap = _make_snapshot("2025-01-01")
        with open(daily_dir / "daily_report_20250101.json", "w") as f:
            json.dump(snap, f)

        result = load_daily_snapshots(tmp_path, date(2025, 1, 1), date(2025, 1, 1))
        assert len(result) == 1

    def test_skips_out_of_range(self, tmp_path):
        import json
        from datetime import date

        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        snap = _make_snapshot("2025-01-15")
        with open(daily_dir / "daily_report_20250115.json", "w") as f:
            json.dump(snap, f)

        result = load_daily_snapshots(tmp_path, date(2025, 1, 1), date(2025, 1, 7))
        assert len(result) == 0

    def test_handles_corrupt_json(self, tmp_path):
        from datetime import date

        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        with open(daily_dir / "daily_report_20250101.json", "w") as f:
            f.write("NOT JSON {{{{")

        result = load_daily_snapshots(tmp_path, date(2025, 1, 1), date(2025, 1, 1))
        assert len(result) == 0
