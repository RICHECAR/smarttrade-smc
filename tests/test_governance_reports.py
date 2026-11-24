"""
Tests for Phase 10 — Governance Reports & Hands-Off Config

Covers:
  - Period KPI aggregation (empty, single day, multi-day)
  - Weekly report generation
  - Monthly report generation
  - Markdown rendering
  - Governance recommendation text
  - Alert checking
  - Pre-flight checklist
  - File I/O (save JSON + Markdown)
"""

import pytest
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.reporting.governance_report import (
    aggregate_period_kpis,
    build_governance_recommendation,
    generate_governance_report,
    render_governance_markdown,
    save_governance_report,
    REPORT_WEEKLY,
    REPORT_MONTHLY,
)
from scripts.reporting.handsoff_config import (
    GOVERNANCE_SCHEDULE,
    ALERT_THRESHOLDS,
    AUTO_ACTIONS,
    OPERATIONAL_LIMITS,
    run_preflight_checks,
    check_alerts,
    render_preflight_markdown,
    _GRADE_ORDER,
)


# ── Helper ────────────────────────────────────────────────────────────

def _make_snapshot(
    date_str: str,
    regime: str = "TREND",
    pnl: float = 10.0,
    trades: int = 3,
    winrate: float = 60.0,
    max_dd: float = 2.0,
    pf: float = 1.5,
    ks_block: int = 0,
    aw_decision: str = "HOLD",
    sc_decision: str = "HOLD",
    risk_pct: float = 0.5,
    eq_start: float = 1000.0,
    eq_end: float = 1010.0,
):
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
                "effective_regime": regime,
                "risk_factor": 1.0,
                "vol_pause_active": False,
                "recovery_active": False,
            },
            "kill_switch": {"block_count": ks_block},
            "capital_scaling": {
                "decision": sc_decision,
                "current_risk_pct": risk_pct,
            },
            "adaptive_weight": {"decision": aw_decision},
            "balance": {"equity_start": eq_start, "equity_end": eq_end},
        },
    }


# ── aggregate_period_kpis ─────────────────────────────────────────────

class TestAggregatePeriodKpis:

    def test_empty_snapshots(self):
        kpis = aggregate_period_kpis([])
        assert kpis["total_pnl"] == 0.0
        assert kpis["total_trades"] == 0
        assert kpis["trading_days"] == 0

    def test_single_day(self):
        kpis = aggregate_period_kpis([_make_snapshot("2025-01-01", pnl=25.0, trades=5)])
        assert kpis["total_pnl"] == 25.0
        assert kpis["total_trades"] == 5
        assert kpis["trading_days"] == 1
        assert kpis["no_trade_days"] == 0

    def test_multi_day_aggregation(self):
        snaps = [
            _make_snapshot("2025-01-01", pnl=10.0, trades=3),
            _make_snapshot("2025-01-02", pnl=-5.0, trades=2),
            _make_snapshot("2025-01-03", pnl=0.0, trades=0),
        ]
        kpis = aggregate_period_kpis(snaps)
        assert kpis["total_pnl"] == 5.0
        assert kpis["total_trades"] == 5
        assert kpis["trading_days"] == 2
        assert kpis["no_trade_days"] == 1

    def test_max_dd_across_days(self):
        snaps = [
            _make_snapshot("2025-01-01", max_dd=2.0),
            _make_snapshot("2025-01-02", max_dd=7.0),
            _make_snapshot("2025-01-03", max_dd=3.0),
        ]
        kpis = aggregate_period_kpis(snaps)
        assert kpis["max_dd"] == 7.0

    def test_equity_change(self):
        snaps = [
            _make_snapshot("2025-01-01", eq_start=1000.0, eq_end=1010.0),
            _make_snapshot("2025-01-02", eq_start=1010.0, eq_end=1050.0),
        ]
        kpis = aggregate_period_kpis(snaps)
        assert kpis["equity_start"] == 1000.0
        assert kpis["equity_end"] == 1050.0
        assert kpis["equity_change_pct"] == 5.0

    def test_adaptive_weight_changes(self):
        snaps = [
            _make_snapshot("2025-01-01", aw_decision="HOLD"),
            _make_snapshot("2025-01-02", aw_decision="PROMOTE"),
            _make_snapshot("2025-01-03", aw_decision="ROLLBACK"),
        ]
        kpis = aggregate_period_kpis(snaps)
        assert kpis["adaptive_weight_changes"] == 2

    def test_scaling_changes(self):
        snaps = [
            _make_snapshot("2025-01-01", sc_decision="HOLD"),
            _make_snapshot("2025-01-02", sc_decision="SCALE_UP"),
        ]
        kpis = aggregate_period_kpis(snaps)
        assert kpis["scaling_changes"] == 1

    def test_kill_switch_total(self):
        snaps = [
            _make_snapshot("2025-01-01", ks_block=1),
            _make_snapshot("2025-01-02", ks_block=2),
        ]
        kpis = aggregate_period_kpis(snaps)
        assert kpis["kill_switch_total_blocks"] == 3


# ── generate_governance_report ────────────────────────────────────────

class TestGenerateGovernanceReport:

    def _setup_daily_files(self, tmp_path, n_days=7, pnl=10.0):
        """Create n daily JSON files."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        today = date(2025, 6, 15)
        for i in range(n_days):
            d = today - timedelta(days=n_days - 1 - i)
            snap = _make_snapshot(d.isoformat(), pnl=pnl)
            with open(daily_dir / f"daily_report_{d.strftime('%Y%m%d')}.json", "w") as f:
                json.dump(snap, f)
        return today

    def test_weekly_report_structure(self, tmp_path):
        today = self._setup_daily_files(tmp_path, n_days=7)
        report = generate_governance_report(tmp_path, today, REPORT_WEEKLY)
        assert report["meta"]["report_type"] == "weekly"
        assert report["meta"]["days_requested"] == 7
        assert "period_kpis" in report
        assert "regime_analytics" in report
        assert "stability" in report
        assert "recommendation" in report

    def test_monthly_report_structure(self, tmp_path):
        today = self._setup_daily_files(tmp_path, n_days=30)
        report = generate_governance_report(tmp_path, today, REPORT_MONTHLY)
        assert report["meta"]["report_type"] == "monthly"
        assert report["meta"]["days_requested"] == 30

    def test_empty_period(self, tmp_path):
        (tmp_path / "daily").mkdir(parents=True, exist_ok=True)
        report = generate_governance_report(tmp_path, date(2025, 1, 1), REPORT_WEEKLY)
        assert report["meta"]["days_with_data"] == 0
        assert report["period_kpis"]["total_trades"] == 0

    def test_pnl_aggregated_correctly(self, tmp_path):
        today = self._setup_daily_files(tmp_path, n_days=3, pnl=20.0)
        report = generate_governance_report(tmp_path, today, REPORT_WEEKLY)
        # 3 days of data (not 7, because only 3 files)
        assert report["period_kpis"]["total_pnl"] == 60.0


# ── render_governance_markdown ────────────────────────────────────────

class TestRenderMarkdown:

    def test_weekly_title(self, tmp_path):
        (tmp_path / "daily").mkdir(parents=True, exist_ok=True)
        report = generate_governance_report(tmp_path, date(2025, 1, 7), REPORT_WEEKLY)
        md = render_governance_markdown(report)
        assert "Weekly Governance Report" in md

    def test_monthly_title(self, tmp_path):
        (tmp_path / "daily").mkdir(parents=True, exist_ok=True)
        report = generate_governance_report(tmp_path, date(2025, 1, 31), REPORT_MONTHLY)
        md = render_governance_markdown(report)
        assert "Monthly Governance Report" in md

    def test_contains_performance_table(self, tmp_path):
        (tmp_path / "daily").mkdir(parents=True, exist_ok=True)
        report = generate_governance_report(tmp_path, date(2025, 1, 7), REPORT_WEEKLY)
        md = render_governance_markdown(report)
        assert "Performance Summary" in md
        assert "Total PnL" in md
        assert "Max Drawdown" in md

    def test_contains_regime_table(self, tmp_path):
        (tmp_path / "daily").mkdir(parents=True, exist_ok=True)
        report = generate_governance_report(tmp_path, date(2025, 1, 7), REPORT_WEEKLY)
        md = render_governance_markdown(report)
        assert "Regime Distribution" in md

    def test_contains_stability_section(self, tmp_path):
        (tmp_path / "daily").mkdir(parents=True, exist_ok=True)
        report = generate_governance_report(tmp_path, date(2025, 1, 7), REPORT_WEEKLY)
        md = render_governance_markdown(report)
        assert "Stability Score" in md

    def test_contains_recommendation(self, tmp_path):
        (tmp_path / "daily").mkdir(parents=True, exist_ok=True)
        report = generate_governance_report(tmp_path, date(2025, 1, 7), REPORT_WEEKLY)
        md = render_governance_markdown(report)
        assert "Governance Recommendation" in md


# ── save_governance_report ────────────────────────────────────────────

class TestSaveGovernanceReport:

    def test_creates_json_and_md(self, tmp_path):
        (tmp_path / "daily").mkdir(parents=True, exist_ok=True)
        report = generate_governance_report(tmp_path, date(2025, 1, 7), REPORT_WEEKLY)
        md_path = save_governance_report(report, tmp_path, REPORT_WEEKLY)
        assert md_path.exists()
        json_path = md_path.with_suffix(".json")
        assert json_path.exists()

    def test_governance_dir_created(self, tmp_path):
        (tmp_path / "daily").mkdir(parents=True, exist_ok=True)
        report = generate_governance_report(tmp_path, date(2025, 1, 7), REPORT_WEEKLY)
        save_governance_report(report, tmp_path, REPORT_WEEKLY)
        assert (tmp_path / "governance").exists()

    def test_json_valid(self, tmp_path):
        (tmp_path / "daily").mkdir(parents=True, exist_ok=True)
        report = generate_governance_report(tmp_path, date(2025, 1, 7), REPORT_WEEKLY)
        save_governance_report(report, tmp_path, REPORT_WEEKLY)
        json_path = tmp_path / "governance" / "weekly_report_20250107.json"
        data = json.loads(json_path.read_text())
        assert data["meta"]["report_type"] == "weekly"


# ── build_governance_recommendation ───────────────────────────────────

class TestGovernanceRecommendation:

    def _default_inputs(self, pnl=50.0, ks=0, grade="A", score=95):
        kpis = {"total_pnl": pnl, "kill_switch_total_blocks": ks}
        stability = {"grade": grade, "score": score, "assessment": "Test assessment"}
        regime = {
            "worst_regime": "CHOP",
            "per_regime": {"CHOP": {"days": 2, "avg_pnl": -5.0}},
        }
        return kpis, stability, regime

    def test_positive_pnl_message(self):
        kpis, stab, reg = self._default_inputs(pnl=50.0)
        reco = build_governance_recommendation(kpis, reg, stab, REPORT_WEEKLY)
        assert "alpha" in reco.lower()

    def test_negative_pnl_message(self):
        kpis, stab, reg = self._default_inputs(pnl=-50.0)
        reco = build_governance_recommendation(kpis, reg, stab, REPORT_WEEKLY)
        assert "negative" in reco.lower() or "loss" in reco.lower()

    def test_high_ks_warning(self):
        kpis, stab, reg = self._default_inputs(ks=5)
        reco = build_governance_recommendation(kpis, reg, stab, REPORT_WEEKLY)
        assert "review sizing" in reco.lower() or "activations" in reco.lower()

    def test_grade_a_no_intervention(self):
        kpis, stab, reg = self._default_inputs(grade="A")
        reco = build_governance_recommendation(kpis, reg, stab, REPORT_WEEKLY)
        assert "no intervention" in reco.lower() or "hands-off" in reco.lower()

    def test_grade_d_manual_review(self):
        kpis, stab, reg = self._default_inputs(grade="D", score=30)
        reco = build_governance_recommendation(kpis, reg, stab, REPORT_WEEKLY)
        assert "manual review" in reco.lower() or "pausing" in reco.lower()

    def test_monthly_validated(self):
        kpis, stab, reg = self._default_inputs(grade="A")
        reco = build_governance_recommendation(kpis, reg, stab, REPORT_MONTHLY)
        assert "autonomous" in reco.lower() or "validated" in reco.lower()


# ── check_alerts ──────────────────────────────────────────────────────

class TestCheckAlerts:

    def test_no_alerts_healthy_system(self):
        kpis = {"total_pnl": 100.0, "avg_winrate": 60.0, "avg_pf": 1.5,
                "max_dd": 2.0, "kill_switch_total_blocks": 0, "no_trade_days": 0}
        stability = {"grade": "A"}
        regime = {"regime_transitions": 2}
        alerts = check_alerts(kpis, stability, regime)
        assert len(alerts) == 0

    def test_critical_weekly_loss(self):
        kpis = {"total_pnl": -200.0, "avg_winrate": 60.0, "avg_pf": 1.5,
                "max_dd": 2.0, "kill_switch_total_blocks": 0, "no_trade_days": 0}
        stability = {"grade": "A"}
        regime = {"regime_transitions": 2}
        alerts = check_alerts(kpis, stability, regime)
        critical = [a for a in alerts if a["level"] == "CRITICAL"]
        assert len(critical) >= 1

    def test_warning_low_winrate(self):
        kpis = {"total_pnl": 10.0, "avg_winrate": 25.0, "avg_pf": 1.5,
                "max_dd": 2.0, "kill_switch_total_blocks": 0, "no_trade_days": 0}
        stability = {"grade": "A"}
        regime = {"regime_transitions": 2}
        alerts = check_alerts(kpis, stability, regime)
        assert any("win-rate" in a["message"].lower() for a in alerts)

    def test_critical_drawdown(self):
        kpis = {"total_pnl": 10.0, "avg_winrate": 60.0, "avg_pf": 1.5,
                "max_dd": 15.0, "kill_switch_total_blocks": 0, "no_trade_days": 0}
        stability = {"grade": "A"}
        regime = {"regime_transitions": 2}
        alerts = check_alerts(kpis, stability, regime)
        critical = [a for a in alerts if a["level"] == "CRITICAL"]
        assert len(critical) >= 1

    def test_kill_switch_alert(self):
        kpis = {"total_pnl": 10.0, "avg_winrate": 60.0, "avg_pf": 1.5,
                "max_dd": 2.0, "kill_switch_total_blocks": 5, "no_trade_days": 0}
        stability = {"grade": "A"}
        regime = {"regime_transitions": 2}
        alerts = check_alerts(kpis, stability, regime)
        assert any("kill-switch" in a["message"].lower() for a in alerts)

    def test_grade_warning(self):
        kpis = {"total_pnl": 10.0, "avg_winrate": 60.0, "avg_pf": 1.5,
                "max_dd": 2.0, "kill_switch_total_blocks": 0, "no_trade_days": 0}
        stability = {"grade": "D"}
        regime = {"regime_transitions": 2}
        alerts = check_alerts(kpis, stability, regime)
        assert any("stability grade" in a["message"].lower() for a in alerts)

    def test_no_trade_days_alert(self):
        kpis = {"total_pnl": 10.0, "avg_winrate": 60.0, "avg_pf": 1.5,
                "max_dd": 2.0, "kill_switch_total_blocks": 0, "no_trade_days": 5}
        stability = {"grade": "A"}
        regime = {"regime_transitions": 2}
        alerts = check_alerts(kpis, stability, regime)
        assert any("no-trade" in a["message"].lower() for a in alerts)


# ── run_preflight_checks ──────────────────────────────────────────────

class TestPreflightChecks:

    def test_basic_pass(self, tmp_path):
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        # Create a recent daily report
        from datetime import date as dt
        today = dt.today()
        fname = f"daily_report_{today.strftime('%Y%m%d')}.json"
        (daily_dir / fname).write_text('{}')

        result = run_preflight_checks(tmp_path)
        assert "checks" in result
        assert "timestamp" in result
        assert isinstance(result["pass"], bool)

    def test_no_reports_fails_check(self, tmp_path):
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        result = run_preflight_checks(tmp_path)
        daily_check = next(c for c in result["checks"] if c["name"] == "daily_reports_recent")
        assert daily_check["pass"] is False

    def test_kill_switch_disabled_fails(self, tmp_path):
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        result = run_preflight_checks(tmp_path, config={"kill_switch_enabled": False})
        ks_check = next(c for c in result["checks"] if c["name"] == "kill_switch_enabled")
        assert ks_check["pass"] is False

    def test_governance_dir_created(self, tmp_path):
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        run_preflight_checks(tmp_path)
        assert (tmp_path / "governance").exists()


# ── render_preflight_markdown ─────────────────────────────────────────

class TestPreflightMarkdown:

    def test_pass_result(self, tmp_path):
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        from datetime import date as dt
        today = dt.today()
        fname = f"daily_report_{today.strftime('%Y%m%d')}.json"
        (daily_dir / fname).write_text('{}')

        result = run_preflight_checks(tmp_path)
        md = render_preflight_markdown(result)
        assert "Pre-Flight Checklist" in md


# ── Config constants ──────────────────────────────────────────────────

class TestConfigConstants:

    def test_governance_schedule_has_weekly(self):
        assert GOVERNANCE_SCHEDULE["weekly_report"] is True
        assert 0 <= GOVERNANCE_SCHEDULE["weekly_day"] <= 6

    def test_governance_schedule_has_monthly(self):
        assert GOVERNANCE_SCHEDULE["monthly_report"] is True
        assert 1 <= GOVERNANCE_SCHEDULE["monthly_day"] <= 31

    def test_alert_thresholds_sane(self):
        assert ALERT_THRESHOLDS["max_daily_loss_usd"] < 0
        assert ALERT_THRESHOLDS["max_drawdown_pct"] > 0
        assert ALERT_THRESHOLDS["min_weekly_winrate"] > 0

    def test_auto_actions_safety(self):
        """Critical actions must NOT be auto-enabled."""
        assert AUTO_ACTIONS["increase_risk_base"] is False
        assert AUTO_ACTIONS["disable_kill_switch"] is False
        assert AUTO_ACTIONS["switch_exchange"] is False

    def test_operational_limits_sane(self):
        assert OPERATIONAL_LIMITS["max_equity_at_risk_pct"] > 0
        assert OPERATIONAL_LIMITS["max_equity_at_risk_pct"] <= 5.0
        assert OPERATIONAL_LIMITS["min_balance_usd"] > 0

    def test_grade_ordering(self):
        assert _GRADE_ORDER["A"] > _GRADE_ORDER["B"] > _GRADE_ORDER["C"]
        assert _GRADE_ORDER["C"] > _GRADE_ORDER["D"] > _GRADE_ORDER["F"]
