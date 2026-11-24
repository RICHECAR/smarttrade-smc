"""
Execution Store — JSONL persistence for Execution Reality Layer
══════════════════════════════════════════════════════════════

Persists:
  • ExecutionRecord (per trade)
  • SlippageProfile snapshots
  • CapacityReport snapshots
  • AdjustedEdgeReport snapshots
  • ExecutionGateVerdict history

Same JSONL pattern as darwinian_store.py and research_snapshots.py.
Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List


class ExecutionStore:
    """JSONL persistence for all execution reality data."""

    def __init__(self, base_dir: str = "data/execution") -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────────────
    #  PUBLIC — write
    # ──────────────────────────────────────────────────────

    def save_record(self, record_dict: Dict[str, Any]) -> None:
        """Append one execution record."""
        self._append("execution_records.jsonl", record_dict)

    def save_slippage_profile(self, profile_dict: Dict[str, Any]) -> None:
        """Snapshot a slippage profile."""
        self._append("slippage_profiles.jsonl", profile_dict)

    def save_capacity_report(self, report_dict: Dict[str, Any]) -> None:
        """Snapshot a capacity report."""
        self._append("capacity_reports.jsonl", report_dict)

    def save_adjusted_edge(self, report_dict: Dict[str, Any]) -> None:
        """Snapshot an adjusted edge report."""
        self._append("adjusted_edge.jsonl", report_dict)

    def save_gate_verdict(self, verdict_dict: Dict[str, Any]) -> None:
        """Snapshot a gate verdict."""
        self._append("gate_verdicts.jsonl", verdict_dict)

    # ──────────────────────────────────────────────────────
    #  PUBLIC — read
    # ──────────────────────────────────────────────────────

    def load_records(self, setup_key: str | None = None) -> List[Dict[str, Any]]:
        """Load execution records, optionally filtered by setup_key."""
        return self._load("execution_records.jsonl", setup_key)

    def load_slippage_profiles(self, setup_key: str | None = None) -> List[Dict[str, Any]]:
        return self._load("slippage_profiles.jsonl", setup_key)

    def load_capacity_reports(self, setup_key: str | None = None) -> List[Dict[str, Any]]:
        return self._load("capacity_reports.jsonl", setup_key)

    def load_adjusted_edges(self, setup_key: str | None = None) -> List[Dict[str, Any]]:
        return self._load("adjusted_edge.jsonl", setup_key)

    def load_gate_verdicts(self, setup_key: str | None = None) -> List[Dict[str, Any]]:
        return self._load("gate_verdicts.jsonl", setup_key)

    # ──────────────────────────────────────────────────────
    #  INTERNAL
    # ──────────────────────────────────────────────────────

    def _append(self, filename: str, data: Dict[str, Any]) -> None:
        data["_ts"] = time.time()
        filepath = self._base / filename
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, default=str) + "\n")

    def _load(
        self,
        filename: str,
        setup_key: str | None = None,
    ) -> List[Dict[str, Any]]:
        filepath = self._base / filename
        if not filepath.exists():
            return []
        results: List[Dict[str, Any]] = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if setup_key and obj.get("setup_key") != setup_key:
                    continue
                results.append(obj)
        return results
