"""
Opportunity Dominance Store — JSONL Persistence
════════════════════════════════════════════════

Append-only JSONL storage for all opportunity dominance events:

  • opportunity_detections.jsonl   — detected opportunities
  • ranking_history.jsonl          — ranking snapshots
  • aggression_decisions.jsonl     — aggression amplifier outputs
  • timing_results.jsonl           — timing precision outputs
  • winner_decisions.jsonl         — winner exploitation outputs
  • scaling_events.jsonl           — scale-in / reduce events
  • decay_signals.jsonl            — decay detection outputs
  • allocation_plans.jsonl         — capital allocation plans

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from backend.services.trading.opportunity_dominance_models import (
    AggressionDecision,
    AllocationDecision,
    CapitalPlan,
    DecayResult,
    DetectionReport,
    RankingReport,
    TimingResult,
    WinnerDecision,
)

_BASE_DIR = Path("data/opportunity_dominance")


class OpportunityDominanceStore:
    """Append-only JSONL persistence for opportunity dominance layer."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._dir = Path(base_dir) if base_dir else _BASE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────
    #  Write methods
    # ──────────────────────────────────────────

    def log_detection(self, report: DetectionReport) -> None:
        self._append("opportunity_detections.jsonl", report.to_dict())

    def log_ranking(self, report: RankingReport) -> None:
        self._append("ranking_history.jsonl", report.to_dict())

    def log_aggression(self, decision: AggressionDecision) -> None:
        self._append("aggression_decisions.jsonl", decision.to_dict())

    def log_timing(self, result: TimingResult) -> None:
        self._append("timing_results.jsonl", result.to_dict())

    def log_winner_decision(self, decision: WinnerDecision) -> None:
        self._append("winner_decisions.jsonl", decision.to_dict())

    def log_scaling_event(self, event: dict) -> None:
        self._append("scaling_events.jsonl", event)

    def log_decay(self, result: DecayResult) -> None:
        self._append("decay_signals.jsonl", result.to_dict())

    def log_allocation(self, plan: CapitalPlan) -> None:
        self._append("allocation_plans.jsonl", plan.to_dict())

    # ──────────────────────────────────────────
    #  Read methods
    # ──────────────────────────────────────────

    def read_detections(self, last_n: int = 50) -> list[dict]:
        return self._read_last("opportunity_detections.jsonl", last_n)

    def read_rankings(self, last_n: int = 50) -> list[dict]:
        return self._read_last("ranking_history.jsonl", last_n)

    def read_aggressions(self, last_n: int = 50) -> list[dict]:
        return self._read_last("aggression_decisions.jsonl", last_n)

    def read_timings(self, last_n: int = 50) -> list[dict]:
        return self._read_last("timing_results.jsonl", last_n)

    def read_winner_decisions(self, last_n: int = 50) -> list[dict]:
        return self._read_last("winner_decisions.jsonl", last_n)

    def read_scaling_events(self, last_n: int = 50) -> list[dict]:
        return self._read_last("scaling_events.jsonl", last_n)

    def read_decay_signals(self, last_n: int = 50) -> list[dict]:
        return self._read_last("decay_signals.jsonl", last_n)

    def read_allocations(self, last_n: int = 50) -> list[dict]:
        return self._read_last("allocation_plans.jsonl", last_n)

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
