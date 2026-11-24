"""
Portfolio Store — JSONL persistence for Portfolio Intelligence Layer
═══════════════════════════════════════════════════════════════════

Persists:
  • Portfolio allocation snapshots
  • Correlation risk reports
  • Drawdown state history
  • Conflict decisions
  • Stress test results
  • Efficiency scores

Same JSONL pattern as execution_store.py and darwinian_store.py.
Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List


class PortfolioStore:
    """JSONL persistence for all portfolio intelligence data."""

    def __init__(self, base_dir: str = "data/portfolio") -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    # ────────────────────────────────────────────────────
    #  WRITE
    # ────────────────────────────────────────────────────

    def save_allocation(self, snapshot_dict: Dict[str, Any]) -> None:
        self._append("allocations.jsonl", snapshot_dict)

    def save_correlation(self, report_dict: Dict[str, Any]) -> None:
        self._append("correlations.jsonl", report_dict)

    def save_drawdown(self, state_dict: Dict[str, Any]) -> None:
        self._append("drawdowns.jsonl", state_dict)

    def save_conflict(self, report_dict: Dict[str, Any]) -> None:
        self._append("conflicts.jsonl", report_dict)

    def save_stress(self, report_dict: Dict[str, Any]) -> None:
        self._append("stress_tests.jsonl", report_dict)

    def save_efficiency(self, score_dict: Dict[str, Any]) -> None:
        self._append("efficiency.jsonl", score_dict)

    # ────────────────────────────────────────────────────
    #  READ
    # ────────────────────────────────────────────────────

    def load_allocations(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self._load("allocations.jsonl", limit=limit)

    def load_correlations(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._load("correlations.jsonl", limit=limit)

    def load_drawdowns(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self._load("drawdowns.jsonl", limit=limit)

    def load_conflicts(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._load("conflicts.jsonl", limit=limit)

    def load_stress_tests(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._load("stress_tests.jsonl", limit=limit)

    def load_efficiency(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self._load("efficiency.jsonl", limit=limit)

    # ────────────────────────────────────────────────────
    #  INTERNAL
    # ────────────────────────────────────────────────────

    def _append(self, filename: str, data: Dict[str, Any]) -> None:
        data["_ts"] = time.time()
        filepath = self._base / filename
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, default=str) + "\n")

    def _load(
        self,
        filename: str,
        limit: int = 100,
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
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        # Return most recent entries
        return results[-limit:]
