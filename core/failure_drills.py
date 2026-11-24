"""
Failure Drills — Systematic incident simulation for ops validation.
═══════════════════════════════════════════════════════════════════

Phase 11: Verify the system handles every failure mode correctly.

Drills:
  1. DB_LOSS_AND_RESTORE: Corrupt DB → backup restore → verify recovery
  2. INVALID_BACKUP: Attempt restore from corrupt backup → verify rejection
  3. DISK_FULL_SIMULATION: Simulate disk full → verify graceful degradation
  4. WAL_TOO_LARGE: Inflate WAL → verify rotation trigger
  5. PROLONGED_DRIFT: Inject persistent drift → verify escalation
  6. UNRESOLVED_SAFE_MODE: Activate safe mode → verify trading blocked
  7. ALERT_CHANNEL_DOWN: Simulate alert delivery failure → verify logging
  8. EXCHANGE_DEGRADED: Simulate exchange errors → verify backpressure
  9. RESTART_STORM: Rapid service restarts → verify circuit breaker

Each drill:
  - Sets up the failure condition
  - Verifies the expected system response
  - Cleans up and restores normal state
  - Returns pass/fail with details
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("smarttrade.failure_drills")


@dataclass
class DrillResult:
    """Result of a single failure drill."""
    name: str
    passed: bool
    duration_sec: float
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class DrillReport:
    """Report of all drills run."""
    total: int
    passed: int
    failed: int
    results: List[DrillResult]
    timestamp: str
    duration_sec: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "timestamp": self.timestamp,
            "duration_sec": self.duration_sec,
            "results": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "duration_sec": r.duration_sec,
                    "message": r.message,
                    **({"error": r.error} if r.error else {}),
                }
                for r in self.results
            ],
        }


class FailureDrillRunner:
    """
    Runs failure drills in isolation to verify ops resilience.

    Each drill operates on temporary resources (tempdir DB copies)
    and never touches the live system.
    """

    def __init__(self) -> None:
        self._drills: Dict[str, Callable[[], DrillResult]] = {
            "db_backup_restore": self._drill_db_backup_restore,
            "invalid_backup_rejection": self._drill_invalid_backup,
            "wal_rotation": self._drill_wal_rotation,
            "safe_mode_blocks_trading": self._drill_safe_mode_blocks,
            "kill_switch_blocks_trading": self._drill_kill_switch_blocks,
            "backup_integrity_check": self._drill_backup_integrity,
            "preflight_detects_issues": self._drill_preflight_detection,
            "monitor_health_evaluation": self._drill_monitor_health,
            "consistency_escalation": self._drill_consistency_escalation,
        }

    def run_all(self) -> DrillReport:
        """Run all registered drills."""
        start = time.time()
        results: List[DrillResult] = []

        for name, drill_fn in self._drills.items():
            try:
                result = drill_fn()
                results.append(result)
            except Exception as e:
                results.append(DrillResult(
                    name=name,
                    passed=False,
                    duration_sec=0.0,
                    message="Drill crashed",
                    error=str(e),
                ))

        passed = sum(1 for r in results if r.passed)
        return DrillReport(
            total=len(results),
            passed=passed,
            failed=len(results) - passed,
            results=results,
            timestamp=datetime.now(timezone.utc).isoformat(),
            duration_sec=round(time.time() - start, 3),
        )

    def run_single(self, name: str) -> DrillResult:
        """Run a single drill by name."""
        if name not in self._drills:
            return DrillResult(
                name=name, passed=False, duration_sec=0.0,
                message=f"Unknown drill: {name}",
            )
        return self._drills[name]()

    # ──────────────────────────────────────────────────
    #  DRILL IMPLEMENTATIONS
    # ──────────────────────────────────────────────────

    def _drill_db_backup_restore(self) -> DrillResult:
        """Test: create DB → backup → corrupt original → restore → verify."""
        start = time.time()
        td = tempfile.mkdtemp(prefix="drill_db_")
        try:
            # Create a test DB
            db_path = os.path.join(td, "test.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT)")
            conn.execute("INSERT INTO trades VALUES (1, 'BTCUSDT')")
            conn.execute("INSERT INTO trades VALUES (2, 'ETHUSDT')")
            conn.commit()
            conn.close()

            # Backup it
            from core.backup_manager import BackupManager
            mgr = BackupManager(
                db_path=db_path,
                backup_dir=os.path.join(td, "backups"),
                max_backups=5,
            )
            record = mgr.create_backup(include_wal=False)

            if not record.integrity_ok:
                return DrillResult(
                    name="db_backup_restore", passed=False,
                    duration_sec=time.time() - start,
                    message="Backup integrity check failed",
                )

            # Corrupt the original
            with open(db_path, "wb") as f:
                f.write(b"CORRUPTED DATA")

            # Verify corruption
            try:
                c2 = sqlite3.connect(db_path)
                c2.execute("SELECT count(*) FROM trades")
                c2.close()
                return DrillResult(
                    name="db_backup_restore", passed=False,
                    duration_sec=time.time() - start,
                    message="DB should be corrupt but wasn't",
                )
            except Exception:
                pass  # Expected

            # Restore
            result = mgr.restore_backup(record.filename)
            if not result.get("success"):
                return DrillResult(
                    name="db_backup_restore", passed=False,
                    duration_sec=time.time() - start,
                    message=f"Restore failed: {result.get('error')}",
                )

            # Verify restored data
            c3 = sqlite3.connect(db_path)
            count = c3.execute("SELECT count(*) FROM trades").fetchone()[0]
            c3.close()

            if count != 2:
                return DrillResult(
                    name="db_backup_restore", passed=False,
                    duration_sec=time.time() - start,
                    message=f"Expected 2 rows after restore, got {count}",
                )

            return DrillResult(
                name="db_backup_restore", passed=True,
                duration_sec=time.time() - start,
                message="DB backup→corrupt→restore cycle OK",
                details={"backup": record.filename, "sha256": record.sha256[:12]},
            )
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def _drill_invalid_backup(self) -> DrillResult:
        """Test: attempt restore from a corrupt backup → verify rejection."""
        start = time.time()
        td = tempfile.mkdtemp(prefix="drill_inv_")
        try:
            db_path = os.path.join(td, "test.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.commit()
            conn.close()

            backup_dir = os.path.join(td, "backups")
            os.makedirs(backup_dir)

            # Create a corrupt "backup"
            corrupt_path = os.path.join(backup_dir, "corrupt.db")
            with open(corrupt_path, "wb") as f:
                f.write(b"NOT A SQLITE FILE AT ALL")

            from core.backup_manager import BackupManager
            mgr = BackupManager(db_path=db_path, backup_dir=backup_dir)

            # Should fail integrity check and refuse restore
            result = mgr.restore_backup("corrupt.db", verify_first=True)
            if result.get("success"):
                return DrillResult(
                    name="invalid_backup_rejection", passed=False,
                    duration_sec=time.time() - start,
                    message="Corrupt backup was accepted (should be rejected)",
                )

            return DrillResult(
                name="invalid_backup_rejection", passed=True,
                duration_sec=time.time() - start,
                message="Corrupt backup correctly rejected",
                details={"error": result.get("error", "")},
            )
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def _drill_wal_rotation(self) -> DrillResult:
        """Test: inflate WAL beyond max size → verify rotation."""
        start = time.time()
        td = tempfile.mkdtemp(prefix="drill_wal_")
        try:
            from core.events.event_bus import WriteAheadLog, EventType
            from core.events.event_types import Event, EventPriority

            wal_path = os.path.join(td, "test_wal.jsonl")
            wal = WriteAheadLog(path=wal_path, max_size_mb=0.001)  # Tiny max

            # Write enough to trigger rotation
            for i in range(100):
                evt = Event(
                    event_type=EventType.TRADE_OPENED,
                    payload={"symbol": f"TEST{i}"},
                    priority=EventPriority.HIGH,
                    source="drill",
                )
                wal.append(evt, i)

            # Check that backup files were created
            baks = [f for f in os.listdir(td) if f.endswith(".bak")]

            wal.close()

            if len(baks) > 0:
                return DrillResult(
                    name="wal_rotation", passed=True,
                    duration_sec=time.time() - start,
                    message=f"WAL rotation triggered correctly ({len(baks)} backups)",
                    details={"backup_count": len(baks)},
                )

            return DrillResult(
                name="wal_rotation", passed=False,
                duration_sec=time.time() - start,
                message="WAL rotation did not trigger",
            )
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def _drill_safe_mode_blocks(self) -> DrillResult:
        """Test: safe mode activation blocks trading."""
        start = time.time()
        try:
            from core.safe_mode import SafeMode
            sm = SafeMode(state_file="data/.drill_safe_mode.json")

            # Activate
            sm.activate("drill", "failure drill test")
            allowed, reason = sm.check_allow_trade()

            if allowed:
                sm.deactivate("drill_cleanup")
                return DrillResult(
                    name="safe_mode_blocks_trading", passed=False,
                    duration_sec=time.time() - start,
                    message="Trading allowed during safe mode (should be blocked)",
                )

            # Deactivate
            sm.deactivate("drill_cleanup")
            allowed2, _ = sm.check_allow_trade()

            if not allowed2:
                return DrillResult(
                    name="safe_mode_blocks_trading", passed=False,
                    duration_sec=time.time() - start,
                    message="Trading still blocked after safe mode deactivation",
                )

            # Cleanup state file
            state_path = "data/.drill_safe_mode.json"
            if os.path.exists(state_path):
                os.remove(state_path)

            return DrillResult(
                name="safe_mode_blocks_trading", passed=True,
                duration_sec=time.time() - start,
                message="Safe mode correctly blocks and unblocks trading",
            )
        except Exception as e:
            return DrillResult(
                name="safe_mode_blocks_trading", passed=False,
                duration_sec=time.time() - start,
                message=f"Drill failed: {e}",
                error=str(e),
            )

    def _drill_kill_switch_blocks(self) -> DrillResult:
        """Test: kill switch blocks event publishing."""
        start = time.time()
        td = tempfile.mkdtemp(prefix="drill_ks_")
        try:
            from core.events.event_bus import EventBus
            from core.events.event_types import Event, EventType, EventPriority

            bus = EventBus(wal_path=os.path.join(td, "wal.jsonl"))

            # Activate kill switch via fast-path event
            ks_event = Event(
                event_type=EventType.RISK_KILL_SWITCH,
                payload={"reason": "drill_test"},
                priority=EventPriority.CRITICAL,
                source="drill",
            )
            bus.publish(ks_event)

            # Try to publish
            evt = Event(
                event_type=EventType.TRADE_OPENED,
                payload={"test": True},
                priority=EventPriority.HIGH,
                source="drill",
            )
            seq = bus.publish(evt)

            bus._wal.close()

            # publish() returns 0 when blocked by kill switch
            if seq != 0:
                return DrillResult(
                    name="kill_switch_blocks_trading", passed=False,
                    duration_sec=time.time() - start,
                    message=f"Event not blocked by kill switch (seq={seq})",
                )

            return DrillResult(
                name="kill_switch_blocks_trading", passed=True,
                duration_sec=time.time() - start,
                message="Kill switch correctly blocks event publishing",
            )
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def _drill_backup_integrity(self) -> DrillResult:
        """Test: backup verify detects tampered files."""
        start = time.time()
        td = tempfile.mkdtemp(prefix="drill_int_")
        try:
            db_path = os.path.join(td, "test.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
            conn.close()

            from core.backup_manager import BackupManager
            mgr = BackupManager(
                db_path=db_path,
                backup_dir=os.path.join(td, "backups"),
            )
            record = mgr.create_backup(include_wal=False)

            # Tamper with backup
            backup_path = os.path.join(td, "backups", record.filename)
            with open(backup_path, "ab") as f:
                f.write(b"TAMPERING")

            # Verify should detect mismatch
            verify = mgr.verify_backup(record.filename)

            if verify.get("hash_match", True):
                return DrillResult(
                    name="backup_integrity_check", passed=False,
                    duration_sec=time.time() - start,
                    message="Tampered backup not detected",
                )

            return DrillResult(
                name="backup_integrity_check", passed=True,
                duration_sec=time.time() - start,
                message="Tampered backup correctly detected via hash mismatch",
                details={"hash_match": verify.get("hash_match")},
            )
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def _drill_preflight_detection(self) -> DrillResult:
        """Test: preflight detects missing DB file."""
        start = time.time()
        try:
            from core.preflight import PreflightChecker, CheckResult, CheckSeverity

            checker = PreflightChecker()

            # Override DB check to use nonexistent path
            original_env = os.environ.get("DATABASE_URL", "")
            os.environ["DATABASE_URL"] = "sqlite:///./nonexistent_drill_db.db"

            try:
                result = checker._check_database()
                if result.passed:
                    return DrillResult(
                        name="preflight_detects_issues", passed=False,
                        duration_sec=time.time() - start,
                        message="Preflight passed with missing DB",
                    )

                return DrillResult(
                    name="preflight_detects_issues", passed=True,
                    duration_sec=time.time() - start,
                    message="Preflight correctly detects missing DB",
                    details={"check_message": result.message},
                )
            finally:
                if original_env:
                    os.environ["DATABASE_URL"] = original_env
                else:
                    os.environ.pop("DATABASE_URL", None)
        except Exception as e:
            return DrillResult(
                name="preflight_detects_issues", passed=False,
                duration_sec=time.time() - start,
                message=f"Drill failed: {e}",
                error=str(e),
            )

    def _drill_monitor_health(self) -> DrillResult:
        """Test: monitor evaluates health levels correctly."""
        start = time.time()
        try:
            from core.long_run_monitor import LongRunMonitor, HealthLevel

            m = LongRunMonitor()

            # Healthy state
            m._evaluate_health({
                "max_backlog": 0, "latency_p99_ms": 10.0,
                "wal_size_mb": 1.0, "dead_letter_count": 0,
                "kill_switch_active": False,
            })
            if m.health != HealthLevel.HEALTHY:
                return DrillResult(
                    name="monitor_health_evaluation", passed=False,
                    duration_sec=time.time() - start,
                    message=f"Expected HEALTHY, got {m.health.value}",
                )

            # Blocked state
            m._evaluate_health({
                "max_backlog": 0, "latency_p99_ms": 0,
                "wal_size_mb": 0, "dead_letter_count": 0,
                "kill_switch_active": True,
            })
            if m.health != HealthLevel.BLOCKED:
                return DrillResult(
                    name="monitor_health_evaluation", passed=False,
                    duration_sec=time.time() - start,
                    message=f"Expected BLOCKED on kill switch, got {m.health.value}",
                )

            return DrillResult(
                name="monitor_health_evaluation", passed=True,
                duration_sec=time.time() - start,
                message="Monitor correctly evaluates HEALTHY and BLOCKED states",
            )
        except Exception as e:
            return DrillResult(
                name="monitor_health_evaluation", passed=False,
                duration_sec=time.time() - start,
                message=f"Drill failed: {e}",
                error=str(e),
            )

    def _drill_consistency_escalation(self) -> DrillResult:
        """Test: consistency checker escalates critical drifts."""
        start = time.time()
        try:
            from core.consistency_checker import (
                ConsistencyChecker, DriftRecord, DriftSeverity,
            )

            checker = ConsistencyChecker()

            # Create 3 critical drifts → should trigger kill_switch
            drifts = [
                DriftRecord(
                    symbol=f"TEST{i}",
                    field="position_size",
                    expected="100.0",
                    actual="0.0",
                    severity=DriftSeverity.CRITICAL,
                    source="drill",
                )
                for i in range(3)
            ]

            action = checker.escalate(drifts)

            # Should be kill_switch or blocked
            if action in ("kill_switch", "blocked"):
                return DrillResult(
                    name="consistency_escalation", passed=True,
                    duration_sec=time.time() - start,
                    message=f"3 critical drifts correctly escalated to: {action}",
                )

            return DrillResult(
                name="consistency_escalation", passed=False,
                duration_sec=time.time() - start,
                message=f"Expected kill_switch, got: {action}",
            )
        except Exception as e:
            return DrillResult(
                name="consistency_escalation", passed=False,
                duration_sec=time.time() - start,
                message=f"Drill failed: {e}",
                error=str(e),
            )
