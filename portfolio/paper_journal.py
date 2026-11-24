"""
Paper Journal — Append-only execution log (JSONL).
═══════════════════════════════════════════════════
One line per rebalance event, immutable after write.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_JOURNAL_PATH = Path(__file__).parent / "data" / "portfolio_paper_journal.jsonl"


@dataclass
class JournalEntry:
    """One rebalance event."""
    timestamp: str
    rebal_date: str
    universe_valid_count: int
    top_rankings: Dict[str, float]       # ticker → score (top N)
    current_weights: Dict[str, float]
    target_weights: Dict[str, float]
    delta_by_ticker: Dict[str, float]
    cash_before: float
    cash_after: float
    portfolio_value_before: float
    portfolio_value_after: float
    cost_simulated: float
    turnover: float
    safeguard_status: str                # "PASSED" or reason codes
    constraint_status: str               # "ALL_CONSTRAINTS_PASSED" or blockers
    benchmark_value: float
    benchmark_ticker: str = "SPY"
    blocked: bool = False
    block_reasons: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


class PaperJournal:
    """Append-only JSONL journal for paper trading."""

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else DEFAULT_JOURNAL_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, entry: JournalEntry) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(entry.to_json() + "\n")

    def read_all(self) -> List[JournalEntry]:
        if not self._path.exists():
            return []
        entries = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                entries.append(JournalEntry(**{
                    k: v for k, v in d.items()
                    if k in JournalEntry.__dataclass_fields__
                }))
        return entries

    def count(self) -> int:
        if not self._path.exists():
            return 0
        with open(self._path, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    def clear(self) -> None:
        """Only for testing. Production journal is append-only."""
        if self._path.exists():
            self._path.unlink()
