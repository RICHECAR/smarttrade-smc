"""
Meta-Control Store — JSONL persistence
═══════════════════════════════════════

Persists all meta-control decisions to append-only JSONL files:

  • mode_transitions.jsonl    — every mode change (with reason)
  • risk_budgets.jsonl        — budget snapshots each cycle
  • recovery_phases.jsonl     — recovery state changes
  • universe_blocks.jsonl     — block/unblock events
  • health_reports.jsonl      — stack health snapshots
  • rotation_decisions.jsonl  — opportunity rankings

All entries are timestamped and auditable.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from backend.services.trading.meta_control_models import (
    ModeDecision,
    RecoveryState,
    RiskBudget,
    RotationDecision,
    StackHealthReport,
    UniverseState,
)

_BASE_DIR = Path("data/meta_control")


class MetaControlStore:
    """Append-only JSONL persistence for meta-control layer."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._dir = Path(base_dir) if base_dir else _BASE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    # ────────────────────────────────────────────────────
    #  Write methods
    # ────────────────────────────────────────────────────

    def log_mode_transition(self, decision: ModeDecision) -> None:
        self._append("mode_transitions.jsonl", decision.to_dict())

    def log_risk_budget(self, budget: RiskBudget) -> None:
        self._append("risk_budgets.jsonl", budget.to_dict())

    def log_recovery_state(self, state: RecoveryState) -> None:
        self._append("recovery_phases.jsonl", state.to_dict())

    def log_universe_state(self, state: UniverseState) -> None:
        self._append("universe_blocks.jsonl", state.to_dict())

    def log_health_report(self, report: StackHealthReport) -> None:
        self._append("health_reports.jsonl", report.to_dict())

    def log_rotation_decisions(self, decisions: list[RotationDecision]) -> None:
        batch = {
            "ts": time.time(),
            "count": len(decisions),
            "rankings": [d.to_dict() for d in decisions],
        }
        self._append("rotation_decisions.jsonl", batch)

    # ────────────────────────────────────────────────────
    #  Read methods
    # ────────────────────────────────────────────────────

    def read_mode_history(self, last_n: int = 50) -> list[dict]:
        return self._read_last("mode_transitions.jsonl", last_n)

    def read_risk_budgets(self, last_n: int = 50) -> list[dict]:
        return self._read_last("risk_budgets.jsonl", last_n)

    def read_recovery_history(self, last_n: int = 50) -> list[dict]:
        return self._read_last("recovery_phases.jsonl", last_n)

    def read_health_history(self, last_n: int = 50) -> list[dict]:
        return self._read_last("health_reports.jsonl", last_n)

    def read_rotation_history(self, last_n: int = 20) -> list[dict]:
        return self._read_last("rotation_decisions.jsonl", last_n)

    # ────────────────────────────────────────────────────
    #  Internal
    # ────────────────────────────────────────────────────

    def _append(self, filename: str, data: dict) -> None:
        if "ts" not in data:
            data["ts"] = time.time()
        path = self._dir / filename
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, default=str) + "\n")

    def _read_last(self, filename: str, n: int) -> list[dict]:
        path = self._dir / filename
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        result = []
        for line in lines[-n:]:
            if line.strip():
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return result
