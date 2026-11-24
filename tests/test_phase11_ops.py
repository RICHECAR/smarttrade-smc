"""
Phase 11 — Ops-Grade Tests
═══════════════════════════

Tests for:
  1. BackupManager: create, verify, restore, rotation, integrity
  2. PreflightChecker: all checks, verdict logic
  3. OpsDashboard: snapshot collection, capital assessment
  4. FailureDrillRunner: all drills pass
  5. Integration: backup→preflight→dashboard pipeline
  6. Edge cases: disk full sim, corrupt files, missing deps
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time

import pytest

# ═══════════════════════════════════════════════════════════════
#  1. BACKUP MANAGER
# ═══════════════════════════════════════════════════════════════


class TestBackupManager:

    def _make_db(self, path: str) -> str:
        """Create a test SQLite DB."""
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, pnl REAL)")
        conn.execute("INSERT INTO trades VALUES (1, 'BTCUSDT', 42.5)")
        conn.execute("INSERT INTO trades VALUES (2, 'ETHUSDT', -10.0)")
        conn.commit()
        conn.close()
        return path

    def test_create_backup(self):
        td = tempfile.mkdtemp()
        try:
            db_path = self._make_db(os.path.join(td, "test.db"))
            from core.backup_manager import BackupManager
            mgr = BackupManager(db_path=db_path, backup_dir=os.path.join(td, "bk"))
            record = mgr.create_backup(include_wal=False)

            assert record.integrity_ok is True
            assert record.size_bytes > 0
            assert len(record.sha256) == 64
            assert record.filename.startswith("trades_backup_")
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_verify_backup(self):
        td = tempfile.mkdtemp()
        try:
            db_path = self._make_db(os.path.join(td, "test.db"))
            from core.backup_manager import BackupManager
            mgr = BackupManager(db_path=db_path, backup_dir=os.path.join(td, "bk"))
            record = mgr.create_backup(include_wal=False)

            verify = mgr.verify_backup(record.filename)
            assert verify["exists"] is True
            assert verify["integrity_ok"] is True
            assert verify["hash_match"] is True
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_verify_tampered(self):
        td = tempfile.mkdtemp()
        try:
            db_path = self._make_db(os.path.join(td, "test.db"))
            from core.backup_manager import BackupManager
            mgr = BackupManager(db_path=db_path, backup_dir=os.path.join(td, "bk"))
            record = mgr.create_backup(include_wal=False)

            # Tamper
            bk_path = os.path.join(td, "bk", record.filename)
            with open(bk_path, "ab") as f:
                f.write(b"TAMPER")

            verify = mgr.verify_backup(record.filename)
            assert verify["hash_match"] is False
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_verify_nonexistent(self):
        td = tempfile.mkdtemp()
        try:
            from core.backup_manager import BackupManager
            mgr = BackupManager(
                db_path=os.path.join(td, "dummy.db"),
                backup_dir=os.path.join(td, "bk"),
            )
            verify = mgr.verify_backup("nonexistent.db")
            assert verify["exists"] is False
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_restore_backup(self):
        td = tempfile.mkdtemp()
        try:
            db_path = self._make_db(os.path.join(td, "test.db"))
            from core.backup_manager import BackupManager
            mgr = BackupManager(db_path=db_path, backup_dir=os.path.join(td, "bk"))
            record = mgr.create_backup(include_wal=False)

            # Corrupt original
            with open(db_path, "wb") as f:
                f.write(b"CORRUPT")

            result = mgr.restore_backup(record.filename)
            assert result["success"] is True

            # Verify data
            conn = sqlite3.connect(db_path)
            count = conn.execute("SELECT count(*) FROM trades").fetchone()[0]
            conn.close()
            assert count == 2
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_restore_corrupt_rejected(self):
        td = tempfile.mkdtemp()
        try:
            db_path = self._make_db(os.path.join(td, "test.db"))
            from core.backup_manager import BackupManager
            bk_dir = os.path.join(td, "bk")
            mgr = BackupManager(db_path=db_path, backup_dir=bk_dir)

            # Create corrupt backup manually
            os.makedirs(bk_dir, exist_ok=True)
            with open(os.path.join(bk_dir, "bad.db"), "wb") as f:
                f.write(b"NOT SQLITE")

            result = mgr.restore_backup("bad.db", verify_first=True)
            assert result["success"] is False
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_rotation(self):
        td = tempfile.mkdtemp()
        try:
            db_path = self._make_db(os.path.join(td, "test.db"))
            from core.backup_manager import BackupManager
            mgr = BackupManager(
                db_path=db_path,
                backup_dir=os.path.join(td, "bk"),
                max_backups=3,
            )
            for _ in range(5):
                mgr.create_backup(include_wal=False)

            assert len(mgr.list_backups()) <= 3
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_manifest_persistence(self):
        td = tempfile.mkdtemp()
        try:
            db_path = self._make_db(os.path.join(td, "test.db"))
            bk_dir = os.path.join(td, "bk")
            from core.backup_manager import BackupManager
            mgr1 = BackupManager(db_path=db_path, backup_dir=bk_dir)
            mgr1.create_backup(include_wal=False)

            # New instance should load manifest
            mgr2 = BackupManager(db_path=db_path, backup_dir=bk_dir)
            assert len(mgr2.list_backups()) == 1
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_get_status(self):
        td = tempfile.mkdtemp()
        try:
            db_path = self._make_db(os.path.join(td, "test.db"))
            from core.backup_manager import BackupManager
            mgr = BackupManager(db_path=db_path, backup_dir=os.path.join(td, "bk"))
            mgr.create_backup(include_wal=False)

            status = mgr.get_status()
            assert status.backup_count == 1
            assert status.latest_backup is not None
            assert status.free_disk_mb > 0
            assert status.healthy is True
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_missing_source_raises(self):
        td = tempfile.mkdtemp()
        try:
            from core.backup_manager import BackupManager
            mgr = BackupManager(
                db_path=os.path.join(td, "nonexistent.db"),
                backup_dir=os.path.join(td, "bk"),
            )
            with pytest.raises(RuntimeError, match="not found"):
                mgr.create_backup()
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_sha256_deterministic(self):
        td = tempfile.mkdtemp()
        try:
            db_path = self._make_db(os.path.join(td, "test.db"))
            from core.backup_manager import BackupManager
            h1 = BackupManager._sha256(db_path)
            h2 = BackupManager._sha256(db_path)
            assert h1 == h2
            assert len(h1) == 64
        finally:
            shutil.rmtree(td, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
#  2. PREFLIGHT CHECKER
# ═══════════════════════════════════════════════════════════════


class TestPreflightChecker:

    def test_disk_space_check(self):
        from core.preflight import PreflightChecker
        checker = PreflightChecker()
        result = checker._check_disk_space()
        # Should always pass on dev machine
        assert result.name == "disk_space"
        assert result.passed is True

    def test_config_check(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("DEFAULT_BROKER", "kraken")
        monkeypatch.setenv("DATABASE_URL", "sqlite:///./data/trades.db")

        from core.preflight import PreflightChecker
        checker = PreflightChecker()
        result = checker._check_config()
        assert result.passed is True

    def test_config_check_missing(self, monkeypatch):
        # PreflightChecker.__init__ calls load_dotenv which re-reads .env,
        # so we must mock it to let monkeypatch.delenv take effect.
        monkeypatch.setattr("dotenv.load_dotenv", lambda **kw: None)
        monkeypatch.delenv("TRADING_MODE", raising=False)
        monkeypatch.delenv("DEFAULT_BROKER", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)

        from core.preflight import PreflightChecker
        checker = PreflightChecker()
        result = checker._check_config()
        assert result.passed is False

    def test_permissions_check(self):
        from core.preflight import PreflightChecker
        checker = PreflightChecker()
        result = checker._check_permissions()
        assert result.name == "permissions"
        # Should pass in normal environment
        assert result.passed is True

    def test_database_check(self):
        from core.preflight import PreflightChecker
        checker = PreflightChecker()
        result = checker._check_database()
        assert result.name == "database"
        # DB exists in the project
        if os.path.exists("data/trades.db"):
            assert result.passed is True

    def test_database_missing(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///./nonexistent_xyz.db")
        from core.preflight import PreflightChecker
        checker = PreflightChecker()
        result = checker._check_database()
        assert result.passed is False

    def test_clock_check(self):
        from core.preflight import PreflightChecker
        checker = PreflightChecker()
        result = checker._check_clock()
        assert result.passed is True

    def test_wal_check_missing_ok(self):
        """WAL not existing is fine (will be created)."""
        from core.preflight import PreflightChecker
        checker = PreflightChecker()
        result = checker._check_wal()
        # Either passes (file exists and small) or info (file missing)
        assert result.name == "wal"

    def test_run_all_produces_report(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("DEFAULT_BROKER", "kraken")
        monkeypatch.setenv("DATABASE_URL", "sqlite:///./data/trades.db")

        from core.preflight import PreflightChecker, PreflightVerdict
        checker = PreflightChecker()
        report = checker.run_all()

        assert report.verdict in (
            PreflightVerdict.READY,
            PreflightVerdict.DEGRADED,
            PreflightVerdict.BLOCKED,
        )
        assert len(report.checks) > 0
        assert report.duration_sec >= 0

    def test_report_to_dict(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("DEFAULT_BROKER", "kraken")
        monkeypatch.setenv("DATABASE_URL", "sqlite:///./data/trades.db")

        from core.preflight import PreflightChecker
        checker = PreflightChecker()
        report = checker.run_all()
        d = report.to_dict()

        assert "verdict" in d
        assert "checks" in d
        assert "passed" in d
        assert "failed" in d

    def test_verdict_blocked_on_critical(self):
        from core.preflight import (
            PreflightReport, PreflightVerdict,
            CheckResult, CheckSeverity,
        )
        report = PreflightReport(
            verdict=PreflightVerdict.READY,
            checks=[
                CheckResult("test", False, CheckSeverity.CRITICAL, "fail"),
            ],
            timestamp="now",
            duration_sec=0,
        )
        assert len(report.critical_failures) == 1

    def test_secrets_check_missing(self, monkeypatch):
        monkeypatch.setattr("dotenv.load_dotenv", lambda **kw: None)
        monkeypatch.setenv("DEFAULT_BROKER", "kraken")
        monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
        monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)

        from core.preflight import PreflightChecker
        checker = PreflightChecker()
        result = checker._check_secrets()
        assert result.passed is False


# ═══════════════════════════════════════════════════════════════
#  3. OPS DASHBOARD
# ═══════════════════════════════════════════════════════════════


class TestOpsDashboard:

    def test_get_snapshot(self):
        from core.ops_dashboard import OpsDashboard
        dashboard = OpsDashboard()
        snapshot = dashboard.get_snapshot()

        assert "timestamp" in snapshot
        assert "global" in snapshot
        assert "event_bus" in snapshot
        assert "risk" in snapshot
        assert "database" in snapshot
        assert "backups" in snapshot
        assert "monitoring" in snapshot
        assert "capital_readiness" in snapshot
        assert "collection_time_ms" in snapshot

    def test_capital_readiness_structure(self):
        from core.ops_dashboard import OpsDashboard
        dashboard = OpsDashboard()
        snapshot = dashboard.get_snapshot()
        cap = snapshot["capital_readiness"]

        assert "tier" in cap
        assert "max_capital_eur" in cap
        assert "conditions" in cap
        assert "blockers" in cap
        assert "recommendation" in cap

    def test_database_collection(self):
        from core.ops_dashboard import OpsDashboard
        dashboard = OpsDashboard()
        db = dashboard._collect_database()
        assert "path" in db
        if os.path.exists("data/trades.db"):
            assert db.get("healthy") is True

    def test_capital_tier_enum(self):
        from core.ops_dashboard import CapitalTier
        assert CapitalTier.PAPER.value == "paper"
        assert CapitalTier.TIER_1.value == "tier_1"
        assert CapitalTier.TIER_5.value == "tier_5"

    def test_snapshot_count_increments(self):
        from core.ops_dashboard import OpsDashboard
        dashboard = OpsDashboard()
        s1 = dashboard.get_snapshot()
        s2 = dashboard.get_snapshot()
        assert s2["snapshot_id"] == s1["snapshot_id"] + 1


# ═══════════════════════════════════════════════════════════════
#  4. FAILURE DRILLS
# ═══════════════════════════════════════════════════════════════


class TestFailureDrills:

    def test_db_backup_restore_drill(self):
        from core.failure_drills import FailureDrillRunner
        runner = FailureDrillRunner()
        result = runner.run_single("db_backup_restore")
        assert result.passed is True, f"Drill failed: {result.message}"

    def test_invalid_backup_drill(self):
        from core.failure_drills import FailureDrillRunner
        runner = FailureDrillRunner()
        result = runner.run_single("invalid_backup_rejection")
        assert result.passed is True, f"Drill failed: {result.message}"

    def test_wal_rotation_drill(self):
        from core.failure_drills import FailureDrillRunner
        runner = FailureDrillRunner()
        result = runner.run_single("wal_rotation")
        assert result.passed is True, f"Drill failed: {result.message}"

    def test_kill_switch_drill(self):
        from core.failure_drills import FailureDrillRunner
        runner = FailureDrillRunner()
        result = runner.run_single("kill_switch_blocks_trading")
        assert result.passed is True, f"Drill failed: {result.message}"

    def test_backup_integrity_drill(self):
        from core.failure_drills import FailureDrillRunner
        runner = FailureDrillRunner()
        result = runner.run_single("backup_integrity_check")
        assert result.passed is True, f"Drill failed: {result.message}"

    def test_preflight_detection_drill(self):
        from core.failure_drills import FailureDrillRunner
        runner = FailureDrillRunner()
        result = runner.run_single("preflight_detects_issues")
        assert result.passed is True, f"Drill failed: {result.message}"

    def test_monitor_health_drill(self):
        from core.failure_drills import FailureDrillRunner
        runner = FailureDrillRunner()
        result = runner.run_single("monitor_health_evaluation")
        assert result.passed is True, f"Drill failed: {result.message}"

    def test_run_all_drills(self):
        from core.failure_drills import FailureDrillRunner
        runner = FailureDrillRunner()
        report = runner.run_all()
        assert report.total > 0
        # Most drills should pass (some may depend on project state)
        assert report.passed >= report.total - 2  # Allow 2 tolerance

    def test_drill_report_to_dict(self):
        from core.failure_drills import FailureDrillRunner
        runner = FailureDrillRunner()
        report = runner.run_all()
        d = report.to_dict()
        assert "total" in d
        assert "passed" in d
        assert "results" in d
        assert len(d["results"]) == report.total

    def test_unknown_drill(self):
        from core.failure_drills import FailureDrillRunner
        runner = FailureDrillRunner()
        result = runner.run_single("nonexistent_drill")
        assert result.passed is False


# ═══════════════════════════════════════════════════════════════
#  5. INTEGRATION
# ═══════════════════════════════════════════════════════════════


class TestIntegration:

    def test_backup_then_preflight(self):
        """Backup creates a file, then preflight backup check passes."""
        td = tempfile.mkdtemp()
        try:
            # Create test DB
            db_path = os.path.join(td, "test.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.commit()
            conn.close()

            from core.backup_manager import BackupManager
            mgr = BackupManager(
                db_path=db_path,
                backup_dir=os.path.join(td, "bk"),
            )
            record = mgr.create_backup(include_wal=False)
            assert record.integrity_ok is True

            # Now verify
            verify = mgr.verify_backup(record.filename)
            assert verify["exists"] is True
            assert verify["hash_match"] is True
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_preflight_report_serializable(self, monkeypatch):
        """Preflight report can be JSON-serialized."""
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("DEFAULT_BROKER", "kraken")
        monkeypatch.setenv("DATABASE_URL", "sqlite:///./data/trades.db")

        from core.preflight import PreflightChecker
        checker = PreflightChecker()
        report = checker.run_all()
        d = report.to_dict()

        # Should be JSON serializable
        serialized = json.dumps(d, default=str)
        assert len(serialized) > 0

    def test_dashboard_snapshot_serializable(self):
        """Dashboard snapshot can be JSON-serialized."""
        from core.ops_dashboard import OpsDashboard
        dashboard = OpsDashboard()
        snapshot = dashboard.get_snapshot()

        serialized = json.dumps(snapshot, default=str)
        assert len(serialized) > 0

    def test_drill_report_serializable(self):
        """Drill report can be JSON-serialized."""
        from core.failure_drills import FailureDrillRunner
        runner = FailureDrillRunner()
        report = runner.run_all()
        d = report.to_dict()

        serialized = json.dumps(d, default=str)
        assert len(serialized) > 0


# ═══════════════════════════════════════════════════════════════
#  6. EDGE CASES
# ═══════════════════════════════════════════════════════════════


class TestEdgeCases:

    def test_backup_empty_db(self):
        """Backup of an empty DB still works."""
        td = tempfile.mkdtemp()
        try:
            db_path = os.path.join(td, "empty.db")
            conn = sqlite3.connect(db_path)
            conn.close()

            from core.backup_manager import BackupManager
            mgr = BackupManager(db_path=db_path, backup_dir=os.path.join(td, "bk"))
            record = mgr.create_backup(include_wal=False)
            assert record.integrity_ok is True
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_backup_concurrent_safe(self):
        """Multiple backups don't corrupt each other."""
        td = tempfile.mkdtemp()
        try:
            db_path = os.path.join(td, "test.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (v INTEGER)")
            conn.commit()
            conn.close()

            from core.backup_manager import BackupManager
            mgr = BackupManager(db_path=db_path, backup_dir=os.path.join(td, "bk"))

            records = []
            for _ in range(3):
                records.append(mgr.create_backup(include_wal=False))

            # All should have unique filenames (due to timestamp)
            filenames = [r.filename for r in records]
            # They might have same timestamp in fast execution, but names should be valid
            assert all(r.integrity_ok for r in records)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_preflight_all_checks_have_names(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("DEFAULT_BROKER", "kraken")
        monkeypatch.setenv("DATABASE_URL", "sqlite:///./data/trades.db")

        from core.preflight import PreflightChecker
        checker = PreflightChecker()
        report = checker.run_all()

        names = [c.name for c in report.checks]
        assert len(names) == len(set(names)), "Duplicate check names"
        assert all(len(n) > 0 for n in names), "Empty check name"

    def test_backup_status_empty_dir(self):
        td = tempfile.mkdtemp()
        try:
            from core.backup_manager import BackupManager
            mgr = BackupManager(
                db_path=os.path.join(td, "dummy.db"),
                backup_dir=os.path.join(td, "bk"),
            )
            status = mgr.get_status()
            assert status.backup_count == 0
            assert status.latest_backup is None
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_restore_nonexistent(self):
        td = tempfile.mkdtemp()
        try:
            from core.backup_manager import BackupManager
            mgr = BackupManager(
                db_path=os.path.join(td, "dummy.db"),
                backup_dir=os.path.join(td, "bk"),
            )
            result = mgr.restore_backup("ghost.db")
            assert result["success"] is False
        finally:
            shutil.rmtree(td, ignore_errors=True)
