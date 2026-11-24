"""
Darwinian Store — Persistence Abstraction for Setup State
════════════════════════════════════════════════════════

JSONL-backed storage with a clean interface ready for migration
to SQLite / Postgres when needed.

Stores:
  - SetupProfile snapshots  (profiles.jsonl)
  - State transitions log   (transitions.jsonl)
  - Regime×Setup cells       (matrix.jsonl)

All writes are append-only. Reads load from last snapshot.

Config via .env:
  DARWINIAN_DATA_DIR=data/darwinian
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.services.trading.darwinian_models import StorageRecord

logger = logging.getLogger("smarttrade.darwinian.store")


class DarwinianStore:
    """
    JSONL-based persistence for the Darwinian layer.
    Thread-safety: single-writer assumption (one trading loop).
    """

    def __init__(self, data_dir: Optional[str] = None) -> None:
        base = data_dir or os.getenv("DARWINIAN_DATA_DIR", "data/darwinian")
        self._base = Path(base)
        self._base.mkdir(parents=True, exist_ok=True)

        self._profiles_path = self._base / "profiles.jsonl"
        self._transitions_path = self._base / "transitions.jsonl"
        self._matrix_path = self._base / "matrix.jsonl"

    # ──────────────────────────────────────────────────
    #  WRITE
    # ──────────────────────────────────────────────────

    def save_profile(self, key: str, data: Dict[str, Any]) -> None:
        """Append a profile snapshot."""
        record = StorageRecord(
            record_type="profile",
            key=key,
            data=data,
            timestamp=time.time(),
        )
        self._append(self._profiles_path, record.to_dict())

    def save_transition(self, key: str, data: Dict[str, Any]) -> None:
        """Append a state transition event."""
        record = StorageRecord(
            record_type="transition",
            key=key,
            data=data,
            timestamp=time.time(),
        )
        self._append(self._transitions_path, record.to_dict())

    def save_matrix_cell(self, regime: str, setup_key: str, data: Dict[str, Any]) -> None:
        """Append a matrix cell snapshot."""
        record = StorageRecord(
            record_type="matrix_cell",
            key=f"{regime}::{setup_key}",
            data=data,
            timestamp=time.time(),
        )
        self._append(self._matrix_path, record.to_dict())

    # ──────────────────────────────────────────────────
    #  READ
    # ──────────────────────────────────────────────────

    def load_latest_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Load the most recent snapshot for each setup key."""
        return self._load_latest(self._profiles_path)

    def load_transitions(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Load recent transitions (newest first)."""
        all_records = self._load_all(self._transitions_path)
        all_records.reverse()
        return all_records[:limit]

    def load_latest_matrix(self) -> Dict[str, Dict[str, Any]]:
        """Load the most recent cell for each (regime::setup_key)."""
        return self._load_latest(self._matrix_path)

    # ──────────────────────────────────────────────────
    #  FLUSH / COMPACTION
    # ──────────────────────────────────────────────────

    def flush_profiles(self, profiles: Dict[str, Dict[str, Any]]) -> None:
        """
        Full rewrite of profiles — keeps file compact.
        Used for periodic compaction.
        """
        records = []
        for key, data in profiles.items():
            records.append(StorageRecord(
                record_type="profile",
                key=key,
                data=data,
                timestamp=time.time(),
            ).to_dict())
        self._write_all(self._profiles_path, records)

    def flush_matrix(self, matrix: Dict[str, Dict[str, Any]]) -> None:
        """Full rewrite of matrix cells."""
        records = []
        for key, data in matrix.items():
            records.append(StorageRecord(
                record_type="matrix_cell",
                key=key,
                data=data,
                timestamp=time.time(),
            ).to_dict())
        self._write_all(self._matrix_path, records)

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    def _append(self, path: Path, record: Dict[str, Any]) -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError as e:
            logger.error("[STORE] Write failed for %s: %s", path, e)

    def _load_all(self, path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        records = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("[STORE] Read failed for %s: %s", path, e)
        return records

    def _load_latest(self, path: Path) -> Dict[str, Dict[str, Any]]:
        """Deduplicate by key, keeping last occurrence."""
        latest: Dict[str, Dict[str, Any]] = {}
        for record in self._load_all(path):
            key = record.get("key", "")
            latest[key] = record.get("data", {})
        return latest

    def _write_all(self, path: Path, records: List[Dict[str, Any]]) -> None:
        """Overwrite file completely (for compaction)."""
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, default=str) + "\n")
            tmp.replace(path)
        except OSError as e:
            logger.error("[STORE] Flush failed for %s: %s", path, e)
            if tmp.exists():
                tmp.unlink()
