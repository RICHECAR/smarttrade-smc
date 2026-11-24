"""
Live Journal — Append-only execution log for live orders (JSONL).
═════════════════════════════════════════════════════════════════
One line per live order event. Immutable after write.
Captures real fills, slippage, and broker responses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional


DEFAULT_LIVE_JOURNAL_PATH = (
    Path(__file__).parent / "data" / "live_journal.jsonl"
)


@dataclass
class LiveOrderEntry:
    """Single live order record."""
    timestamp: str                     # ISO wall-clock time
    rebal_date: str                    # Which rebalance triggered this
    ticker: str
    side: str                          # BUY / SELL
    intended_shares: float             # What we planned to trade
    intended_price: float              # Price at decision time
    filled_shares: float = 0.0        # Actual fill
    filled_price: float = 0.0         # Actual fill price
    slippage_bps: float = 0.0         # (fill - intended) / intended * 10_000
    cost_dollar: float = 0.0          # Broker fees
    broker_order_id: str = ""
    broker_status: str = ""            # FILLED / PARTIAL / REJECTED / TIMEOUT
    error: str = ""
    latency_ms: float = 0.0           # Order→fill latency

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


@dataclass
class LiveRebalanceEntry:
    """Aggregate record for one rebalance cycle."""
    timestamp: str
    rebal_date: str
    mode: str = "live"                 # live / dry_run
    paper_weights: Dict[str, float] = field(default_factory=dict)
    live_weights_before: Dict[str, float] = field(default_factory=dict)
    live_weights_after: Dict[str, float] = field(default_factory=dict)
    target_weights: Dict[str, float] = field(default_factory=dict)
    orders_count: int = 0
    orders_filled: int = 0
    orders_failed: int = 0
    total_cost: float = 0.0
    total_slippage_bps: float = 0.0
    turnover: float = 0.0
    portfolio_value_before: float = 0.0
    portfolio_value_after: float = 0.0
    risk_guard_status: str = "PASSED"
    alerts: List[str] = field(default_factory=list)
    blocked: bool = False
    block_reasons: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


class LiveJournal:
    """Append-only JSONL journal for live trading."""

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else DEFAULT_LIVE_JOURNAL_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append_order(self, entry: LiveOrderEntry) -> None:
        line = json.dumps({"type": "order", **entry.to_dict()}, default=str)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def append_rebalance(self, entry: LiveRebalanceEntry) -> None:
        line = json.dumps({"type": "rebalance", **entry.to_dict()}, default=str)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read_all(self) -> List[dict]:
        if not self._path.exists():
            return []
        entries = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    def read_orders(self) -> List[LiveOrderEntry]:
        return [
            LiveOrderEntry(**{k: v for k, v in e.items()
                              if k in LiveOrderEntry.__dataclass_fields__})
            for e in self.read_all() if e.get("type") == "order"
        ]

    def read_rebalances(self) -> List[LiveRebalanceEntry]:
        return [
            LiveRebalanceEntry(**{k: v for k, v in e.items()
                                  if k in LiveRebalanceEntry.__dataclass_fields__})
            for e in self.read_all() if e.get("type") == "rebalance"
        ]

    def count(self) -> int:
        if not self._path.exists():
            return 0
        with open(self._path, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    def clear(self) -> None:
        """Only for testing."""
        if self._path.exists():
            self._path.unlink()
