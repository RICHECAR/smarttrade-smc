"""
Structural Break Store — JSONL Persistence
═══════════════════════════════════════════

Append-only JSONL storage for all structural break intelligence events:

  • break_events.jsonl          — structural break detections
  • transition_events.jsonl     — regime transition detections
  • doctrine_events.jsonl       — doctrine mismatch verdicts
  • propagation_events.jsonl    — shock propagation analyses
  • throttle_actions.jsonl      — throttle state changes
  • recovery_blocks.jsonl       — false recovery guard results

All entries timestamped and auditable.
Ready for SQLite/Postgres migration later.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from backend.services.trading.structural_break_models import (
    BreakReport,
    DoctrineVerdict,
    PropagationResult,
    RecoveryCheckResult,
    ThrottleState,
    TransitionReport,
)

_BASE_DIR = Path("data/structural_breaks")


class StructuralBreakStore:
    """Append-only JSONL persistence for structural break intelligence."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._dir = Path(base_dir) if base_dir else _BASE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    # ────────────────────────────────────────────────────
    #  Write methods
    # ────────────────────────────────────────────────────

    def log_break_report(self, report: BreakReport) -> None:
        self._append("break_events.jsonl", report.to_dict())

    def log_transition_report(self, report: TransitionReport) -> None:
        self._append("transition_events.jsonl", report.to_dict())

    def log_doctrine_verdict(self, verdict: DoctrineVerdict) -> None:
        self._append("doctrine_events.jsonl", verdict.to_dict())

    def log_propagation_result(self, result: PropagationResult) -> None:
        self._append("propagation_events.jsonl", result.to_dict())

    def log_throttle_state(self, state: ThrottleState) -> None:
        self._append("throttle_actions.jsonl", state.to_dict())

    def log_recovery_check(self, result: RecoveryCheckResult) -> None:
        self._append("recovery_blocks.jsonl", result.to_dict())

    # ────────────────────────────────────────────────────
    #  Read methods
    # ────────────────────────────────────────────────────

    def read_break_history(self, last_n: int = 50) -> list[dict]:
        return self._read_last("break_events.jsonl", last_n)

    def read_transition_history(self, last_n: int = 50) -> list[dict]:
        return self._read_last("transition_events.jsonl", last_n)

    def read_doctrine_history(self, last_n: int = 50) -> list[dict]:
        return self._read_last("doctrine_events.jsonl", last_n)

    def read_propagation_history(self, last_n: int = 50) -> list[dict]:
        return self._read_last("propagation_events.jsonl", last_n)

    def read_throttle_history(self, last_n: int = 50) -> list[dict]:
        return self._read_last("throttle_actions.jsonl", last_n)

    def read_recovery_history(self, last_n: int = 50) -> list[dict]:
        return self._read_last("recovery_blocks.jsonl", last_n)

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
