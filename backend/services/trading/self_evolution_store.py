"""
Self-Evolution Store — JSONL Persistence
════════════════════════════════════════

Append-only JSONL storage for all self-evolution events:

  • evolution_reports.jsonl     — strategy evolution snapshots
  • learning_patterns.jsonl    — meta-learning discoveries
  • parameter_plans.jsonl      — parameter adaptation plans
  • failure_reports.jsonl      — failure pattern analyses
  • discovery_reports.jsonl    — new edge discoveries
  • improvement_cycles.jsonl   — improvement loop cycles
  • evolution_state.jsonl      — global evolution state snapshots

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from backend.services.trading.self_evolution_models import (
    AdaptationPlan,
    DiscoveryReport,
    EvolutionReport,
    EvolutionState,
    FailureReport,
    ImprovementCycle,
    MetaLearningReport,
)

_BASE_DIR = Path("data/self_evolution")


class SelfEvolutionStore:
    """Append-only JSONL persistence for self-evolution layer."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._dir = Path(base_dir) if base_dir else _BASE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────
    #  Write methods
    # ──────────────────────────────────────────

    def log_evolution(self, report: EvolutionReport) -> None:
        self._append("evolution_reports.jsonl", report.to_dict())

    def log_learning(self, report: MetaLearningReport) -> None:
        self._append("learning_patterns.jsonl", report.to_dict())

    def log_adaptation(self, plan: AdaptationPlan) -> None:
        self._append("parameter_plans.jsonl", plan.to_dict())

    def log_failures(self, report: FailureReport) -> None:
        self._append("failure_reports.jsonl", report.to_dict())

    def log_discovery(self, report: DiscoveryReport) -> None:
        self._append("discovery_reports.jsonl", report.to_dict())

    def log_cycle(self, cycle: ImprovementCycle) -> None:
        self._append("improvement_cycles.jsonl", cycle.to_dict())

    def log_state(self, state: EvolutionState) -> None:
        self._append("evolution_state.jsonl", state.to_dict())

    # ──────────────────────────────────────────
    #  Read methods
    # ──────────────────────────────────────────

    def read_evolutions(self, last_n: int = 50) -> list[dict]:
        return self._read_last("evolution_reports.jsonl", last_n)

    def read_learnings(self, last_n: int = 50) -> list[dict]:
        return self._read_last("learning_patterns.jsonl", last_n)

    def read_adaptations(self, last_n: int = 50) -> list[dict]:
        return self._read_last("parameter_plans.jsonl", last_n)

    def read_failures(self, last_n: int = 50) -> list[dict]:
        return self._read_last("failure_reports.jsonl", last_n)

    def read_discoveries(self, last_n: int = 50) -> list[dict]:
        return self._read_last("discovery_reports.jsonl", last_n)

    def read_cycles(self, last_n: int = 50) -> list[dict]:
        return self._read_last("improvement_cycles.jsonl", last_n)

    def read_states(self, last_n: int = 50) -> list[dict]:
        return self._read_last("evolution_state.jsonl", last_n)

    # ──────────────────────────────────────────
    #  Internal
    # ──────────────────────────────────────────

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
