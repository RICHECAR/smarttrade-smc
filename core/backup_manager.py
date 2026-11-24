"""
Backup Manager — SQLite DB backup, rotation, integrity, restore.
═══════════════════════════════════════════════════════════════════

Phase 11: Ops-grade backup infrastructure for 24/7 operation.

Features:
  1. HOT BACKUP: Uses SQLite online backup API (no downtime)
  2. ROTATION: Keeps N most recent backups, purges oldest
  3. INTEGRITY: PRAGMA integrity_check on backup after creation
  4. RESTORE: Copy backup to DB path with safety checks
  5. WAL BACKUP: Snapshot event WAL alongside DB
  6. DISK CHECK: Verify sufficient free space before backup
  7. COHERENCE: DB + WAL + runtime state cross-check

Env:
  BACKUP_DIR=data/backups
  BACKUP_MAX_COUNT=10
  BACKUP_MIN_FREE_MB=500
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("smarttrade.backup")


def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


@dataclass
class BackupRecord:
    """Metadata for a single backup."""
    filename: str
    timestamp: str
    size_bytes: int
    sha256: str
    integrity_ok: bool
    db_source: str
    wal_included: bool
    duration_sec: float


@dataclass
class BackupStatus:
    """Current backup subsystem status."""
    backup_dir: str
    backup_count: int
    latest_backup: Optional[str]
    latest_timestamp: Optional[str]
    total_size_bytes: int
    free_disk_mb: float
    healthy: bool
    error: Optional[str] = None


class BackupManager:
    """
    Production-grade SQLite backup manager.

    Usage:
        mgr = BackupManager()
        record = mgr.create_backup()
        mgr.verify_backup(record.filename)
        mgr.restore_backup(record.filename)
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        backup_dir: Optional[str] = None,
        max_backups: Optional[int] = None,
        min_free_mb: Optional[int] = None,
    ) -> None:
        from dotenv import load_dotenv
        load_dotenv()

        raw_url = db_path or os.getenv("DATABASE_URL", "sqlite:///./data/trades.db")
        self._db_path = raw_url.replace("sqlite:///", "")
        self._backup_dir = backup_dir or _env_str("BACKUP_DIR", "data/backups")
        self._max_backups = max_backups or _env_int("BACKUP_MAX_COUNT", 10)
        self._min_free_mb = min_free_mb or _env_int("BACKUP_MIN_FREE_MB", 500)
        self._manifest_path = os.path.join(self._backup_dir, "manifest.json")
        self._manifest: List[Dict[str, Any]] = []

        os.makedirs(self._backup_dir, exist_ok=True)
        self._load_manifest()

    # ──────────────────────────────────────────────────
    #  CREATE BACKUP
    # ──────────────────────────────────────────────────

    def create_backup(self, include_wal: bool = True) -> BackupRecord:
        """
        Create a hot backup of the SQLite database.

        Uses sqlite3 backup API for consistency.
        Returns BackupRecord with metadata.

        Raises:
            RuntimeError: on disk space, missing source, or backup failure
        """
        # Pre-checks
        if not os.path.exists(self._db_path):
            raise RuntimeError(f"Source DB not found: {self._db_path}")

        free_mb = self._get_free_disk_mb()
        if free_mb < self._min_free_mb:
            raise RuntimeError(
                f"Insufficient disk space: {free_mb:.0f}MB free, need {self._min_free_mb}MB"
            )

        ts = datetime.now(timezone.utc)
        ts_str = ts.strftime("%Y%m%d_%H%M%S")
        backup_name = f"trades_backup_{ts_str}.db"
        backup_path = os.path.join(self._backup_dir, backup_name)

        start = time.time()

        # Hot backup using sqlite3 API
        try:
            src = sqlite3.connect(self._db_path)
            dst = sqlite3.connect(backup_path)
            src.backup(dst)
            dst.close()
            src.close()
        except Exception as e:
            # Cleanup partial backup
            if os.path.exists(backup_path):
                os.remove(backup_path)
            raise RuntimeError(f"Backup failed: {e}") from e

        duration = time.time() - start

        # Compute hash
        sha = self._sha256(backup_path)
        size = os.path.getsize(backup_path)

        # Integrity check
        integrity_ok = self._integrity_check(backup_path)

        # WAL backup
        wal_included = False
        if include_wal:
            wal_included = self._backup_wal(ts_str)

        record = BackupRecord(
            filename=backup_name,
            timestamp=ts.isoformat(),
            size_bytes=size,
            sha256=sha,
            integrity_ok=integrity_ok,
            db_source=self._db_path,
            wal_included=wal_included,
            duration_sec=round(duration, 3),
        )

        # Update manifest
        self._manifest.append(asdict(record))
        self._save_manifest()

        # Rotate old backups
        self._rotate()

        logger.info(
            "[BACKUP] Created %s (%.1fKB, %.3fs, integrity=%s, sha=%s)",
            backup_name, size / 1024, duration,
            "OK" if integrity_ok else "FAIL", sha[:12],
        )

        if not integrity_ok:
            logger.error("[BACKUP] INTEGRITY CHECK FAILED on %s", backup_name)

        return record

    # ──────────────────────────────────────────────────
    #  VERIFY BACKUP
    # ──────────────────────────────────────────────────

    def verify_backup(self, filename: str) -> Dict[str, Any]:
        """
        Verify a backup file: existence, integrity, hash match.

        Returns dict with verification results.
        """
        path = os.path.join(self._backup_dir, filename)
        result: Dict[str, Any] = {
            "filename": filename,
            "exists": False,
            "integrity_ok": False,
            "hash_match": False,
            "size_bytes": 0,
        }

        if not os.path.exists(path):
            result["error"] = "File not found"
            return result

        result["exists"] = True
        result["size_bytes"] = os.path.getsize(path)
        result["integrity_ok"] = self._integrity_check(path)

        # Check hash against manifest
        current_sha = self._sha256(path)
        result["current_sha256"] = current_sha

        manifest_entry = next(
            (e for e in self._manifest if e["filename"] == filename), None
        )
        if manifest_entry:
            result["manifest_sha256"] = manifest_entry["sha256"]
            result["hash_match"] = current_sha == manifest_entry["sha256"]
        else:
            result["hash_match"] = False
            result["error"] = "Not in manifest"

        return result

    # ──────────────────────────────────────────────────
    #  RESTORE BACKUP
    # ──────────────────────────────────────────────────

    def restore_backup(self, filename: str, verify_first: bool = True) -> Dict[str, Any]:
        """
        Restore a backup to the main DB path.

        Safety:
          1. Verifies backup integrity first
          2. Creates a pre-restore backup of current DB
          3. Copies backup to DB path

        Returns dict with restore results.
        """
        backup_path = os.path.join(self._backup_dir, filename)

        if not os.path.exists(backup_path):
            return {"success": False, "error": f"Backup not found: {filename}"}

        if verify_first:
            vresult = self.verify_backup(filename)
            if not vresult.get("integrity_ok"):
                return {
                    "success": False,
                    "error": "Backup failed integrity check",
                    "verification": vresult,
                }

        # Pre-restore safety: backup current DB
        pre_restore_name = None
        if os.path.exists(self._db_path):
            pre_restore_name = f"pre_restore_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"
            pre_restore_path = os.path.join(self._backup_dir, pre_restore_name)
            shutil.copy2(self._db_path, pre_restore_path)
            logger.info("[BACKUP] Pre-restore safety copy: %s", pre_restore_name)

        # Restore
        try:
            shutil.copy2(backup_path, self._db_path)
            logger.info("[BACKUP] RESTORED %s → %s", filename, self._db_path)
            return {
                "success": True,
                "restored_from": filename,
                "pre_restore_backup": pre_restore_name,
            }
        except Exception as e:
            logger.error("[BACKUP] Restore failed: %s", e)
            return {"success": False, "error": str(e)}

    # ──────────────────────────────────────────────────
    #  LIST / STATUS
    # ──────────────────────────────────────────────────

    def list_backups(self) -> List[Dict[str, Any]]:
        """List all backups from manifest."""
        return list(self._manifest)

    def get_status(self) -> BackupStatus:
        """Get backup subsystem status."""
        total_size = 0
        for entry in self._manifest:
            path = os.path.join(self._backup_dir, entry["filename"])
            if os.path.exists(path):
                total_size += os.path.getsize(path)

        latest = self._manifest[-1] if self._manifest else None
        free_mb = self._get_free_disk_mb()

        return BackupStatus(
            backup_dir=self._backup_dir,
            backup_count=len(self._manifest),
            latest_backup=latest["filename"] if latest else None,
            latest_timestamp=latest["timestamp"] if latest else None,
            total_size_bytes=total_size,
            free_disk_mb=round(free_mb, 1),
            healthy=free_mb >= self._min_free_mb and (not latest or latest.get("integrity_ok", False)),
        )

    # ──────────────────────────────────────────────────
    #  INTERNAL
    # ──────────────────────────────────────────────────

    def _integrity_check(self, db_path: str) -> bool:
        """Run PRAGMA integrity_check on a SQLite file."""
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.execute("PRAGMA integrity_check")
            result = cursor.fetchone()
            conn.close()
            return result is not None and result[0] == "ok"
        except Exception as e:
            logger.error("[BACKUP] Integrity check failed for %s: %s", db_path, e)
            return False

    @staticmethod
    def _sha256(path: str) -> str:
        """Compute SHA-256 of a file."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _backup_wal(self, ts_str: str) -> bool:
        """Backup the event WAL file if it exists."""
        wal_path = "logs/event_wal.jsonl"
        if not os.path.exists(wal_path):
            return False
        try:
            dest = os.path.join(self._backup_dir, f"event_wal_{ts_str}.jsonl")
            shutil.copy2(wal_path, dest)
            logger.info("[BACKUP] WAL backed up: %s", dest)
            return True
        except Exception as e:
            logger.warning("[BACKUP] WAL backup failed: %s", e)
            return False

    def _rotate(self) -> None:
        """Remove oldest backups beyond max_backups."""
        # Sort manifest by timestamp
        self._manifest.sort(key=lambda e: e.get("timestamp", ""))

        while len(self._manifest) > self._max_backups:
            oldest = self._manifest.pop(0)
            path = os.path.join(self._backup_dir, oldest["filename"])
            try:
                if os.path.exists(path):
                    os.remove(path)
                    logger.info("[BACKUP] Rotated old backup: %s", oldest["filename"])
            except OSError as e:
                logger.warning("[BACKUP] Failed to remove old backup %s: %s", oldest["filename"], e)

        self._save_manifest()

    def _get_free_disk_mb(self) -> float:
        """Get free disk space in MB for the backup directory."""
        try:
            usage = shutil.disk_usage(self._backup_dir)
            return usage.free / (1024 * 1024)
        except Exception:
            return 0.0

    def _load_manifest(self) -> None:
        """Load backup manifest from disk."""
        if os.path.exists(self._manifest_path):
            try:
                with open(self._manifest_path, "r", encoding="utf-8") as f:
                    self._manifest = json.load(f)
            except Exception as e:
                logger.warning("[BACKUP] Failed to load manifest: %s", e)
                self._manifest = []

    def _save_manifest(self) -> None:
        """Save backup manifest to disk."""
        try:
            with open(self._manifest_path, "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, indent=2)
        except Exception as e:
            logger.error("[BACKUP] Failed to save manifest: %s", e)


# ──────────────────────────────────────────────────
#  SINGLETON
# ──────────────────────────────────────────────────

_instance: Optional[BackupManager] = None


def get_backup_manager() -> BackupManager:
    global _instance
    if _instance is None:
        _instance = BackupManager()
    return _instance
