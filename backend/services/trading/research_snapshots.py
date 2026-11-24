"""
Research Snapshot Service — Periodic Full-State Persistence
══════════════════════════════════════════════════════════

Captures periodic snapshots of every setup's robustness state:
  - survival score
  - confidence score
  - fragility score
  - edge trend
  - robustness score
  - allocation
  - regime fit
  - lifecycle state
  - performance stats

Stored as JSONL, ready for later export to SQLite / Postgres.

Config via .env:
  SNAPSHOT_DATA_DIR=data/robustness
  SNAPSHOT_MAX_PER_FILE=5000  (compact after this many lines)
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.services.trading.robustness_models import ResearchSnapshot

logger = logging.getLogger("smarttrade.robustness.snapshots")


class ResearchSnapshotService:
    """
    Periodic snapshot persistence for the robustness layer.
    Thread-safety: single-writer assumption.

    Usage:
        svc = ResearchSnapshotService()
        svc.save_snapshot(snapshot)
        recent = svc.load_recent(limit=50)
        svc.compact()
    """

    def __init__(self, data_dir: Optional[str] = None) -> None:
        base = data_dir or os.getenv("SNAPSHOT_DATA_DIR", "data/robustness")
        self._base = Path(base)
        self._base.mkdir(parents=True, exist_ok=True)
        self._path = self._base / "snapshots.jsonl"
        self._max_lines = int(os.getenv("SNAPSHOT_MAX_PER_FILE", "5000"))

    # ──────────────────────────────────────────────────
    #  WRITE
    # ──────────────────────────────────────────────────

    def save_snapshot(self, snapshot: ResearchSnapshot) -> None:
        """Append one snapshot."""
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot.to_dict(), default=str) + "\n")
        except OSError as e:
            logger.error("[SNAPSHOT] Write failed: %s", e)

    def save_batch(self, snapshots: List[ResearchSnapshot]) -> None:
        """Append a batch of snapshots (one per setup)."""
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                for snap in snapshots:
                    f.write(json.dumps(snap.to_dict(), default=str) + "\n")
        except OSError as e:
            logger.error("[SNAPSHOT] Batch write failed: %s", e)

    # ──────────────────────────────────────────────────
    #  READ
    # ──────────────────────────────────────────────────

    def load_recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Load the most recent N snapshots (newest first)."""
        all_records = self._load_all()
        all_records.reverse()
        return all_records[:limit]

    def load_for_setup(self, setup_key: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Load snapshots for a specific setup."""
        all_records = self._load_all()
        filtered = [r for r in all_records if r.get("setup_key") == setup_key]
        filtered.reverse()
        return filtered[:limit]

    def load_latest_per_setup(self) -> Dict[str, Dict[str, Any]]:
        """Most recent snapshot for each setup (for dashboard)."""
        latest: Dict[str, Dict[str, Any]] = {}
        for record in self._load_all():
            sk = record.get("setup_key", "")
            latest[sk] = record
        return latest

    # ──────────────────────────────────────────────────
    #  COMPACTION
    # ──────────────────────────────────────────────────

    def compact(self) -> int:
        """
        Keep only the latest snapshot per setup + last N entries.
        Returns number of records after compaction.
        """
        all_records = self._load_all()
        if len(all_records) <= self._max_lines:
            return len(all_records)

        # Strategy: keep latest per setup + last max_lines/2 entries
        keep_count = self._max_lines // 2
        latest_per_setup: Dict[str, Dict[str, Any]] = {}
        for r in all_records:
            latest_per_setup[r.get("setup_key", "")] = r

        tail = all_records[-keep_count:]

        # Merge: latest_per_setup values + tail (deduplicated)
        seen = set()
        merged: List[Dict[str, Any]] = []
        for r in list(latest_per_setup.values()) + tail:
            key = (r.get("setup_key", ""), r.get("timestamp", 0))
            if key not in seen:
                seen.add(key)
                merged.append(r)

        # Sort by timestamp
        merged.sort(key=lambda r: r.get("timestamp", 0))

        self._write_all(merged)
        logger.info("[SNAPSHOT] Compacted %d → %d records", len(all_records), len(merged))
        return len(merged)

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    def _load_all(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        records: List[Dict[str, Any]] = []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("[SNAPSHOT] Read failed: %s", e)
        return records

    def _write_all(self, records: List[Dict[str, Any]]) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, default=str) + "\n")
            tmp.replace(self._path)
        except OSError as e:
            logger.error("[SNAPSHOT] Flush failed: %s", e)
            if tmp.exists():
                tmp.unlink()
