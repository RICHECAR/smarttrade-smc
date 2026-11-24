"""
Live Dual Tracker — Parallel paper / live portfolio comparison.
══════════════════════════════════════════════════════════════
Maintains two portfolio views:
  - paper: what WOULD have executed (reference, from PaperEngine)
  - live:  what DID execute (real fills)

Computes:
  - PnL divergence, weight divergence, trade-by-trade slippage
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TRACKER_PATH = Path(__file__).parent / "data" / "dual_tracker.json"


@dataclass
class DualSnapshot:
    """One point-in-time comparison."""
    date: str
    paper_value: float
    live_value: float
    paper_return: float        # cumulative since inception
    live_return: float
    return_divergence: float   # abs(paper - live)
    paper_weights: Dict[str, float] = field(default_factory=dict)
    live_weights: Dict[str, float] = field(default_factory=dict)
    max_weight_diff: float = 0.0
    avg_slippage_bps: float = 0.0
    total_live_cost: float = 0.0
    total_paper_cost: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TradeComparison:
    """Compare a single trade paper vs live."""
    ticker: str
    side: str
    paper_shares: float
    paper_price: float
    live_shares: float
    live_price: float
    slippage_bps: float
    cost_diff: float


class DualTracker:
    """Tracks paper and live portfolios side by side."""

    def __init__(
        self,
        initial_value: float = 100_000.0,
        path: Optional[Path] = None,
    ):
        self._path = Path(path) if path else DEFAULT_TRACKER_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

        self.initial_value = initial_value
        self.paper_value: float = initial_value
        self.live_value: float = initial_value
        self.paper_weights: Dict[str, float] = {}
        self.live_weights: Dict[str, float] = {}
        self.peak_live_value: float = initial_value
        self.snapshots: List[DualSnapshot] = []
        self.trade_comparisons: List[TradeComparison] = []

    # ── Updates ──────────────────────────────────────────────────

    def update_paper(
        self, value: float, weights: Dict[str, float], cost: float = 0.0,
    ) -> None:
        self.paper_value = value
        self.paper_weights = dict(weights)

    def update_live(
        self, value: float, weights: Dict[str, float], cost: float = 0.0,
    ) -> None:
        self.live_value = value
        self.live_weights = dict(weights)
        if value > self.peak_live_value:
            self.peak_live_value = value

    def record_trade_comparison(
        self,
        ticker: str,
        side: str,
        paper_shares: float,
        paper_price: float,
        live_shares: float,
        live_price: float,
        cost_diff: float,
    ) -> None:
        if paper_price > 0:
            if side == "BUY":
                slip = (live_price - paper_price) / paper_price * 10_000
            else:
                slip = (paper_price - live_price) / paper_price * 10_000
        else:
            slip = 0.0

        self.trade_comparisons.append(TradeComparison(
            ticker=ticker, side=side,
            paper_shares=round(paper_shares, 6),
            paper_price=round(paper_price, 4),
            live_shares=round(live_shares, 6),
            live_price=round(live_price, 4),
            slippage_bps=round(slip, 2),
            cost_diff=round(cost_diff, 4),
        ))

    # ── Snapshot ─────────────────────────────────────────────────

    def take_snapshot(self, date: str) -> DualSnapshot:
        """Record current state for history."""
        paper_ret = (self.paper_value / self.initial_value) - 1 if self.initial_value > 0 else 0
        live_ret = (self.live_value / self.initial_value) - 1 if self.initial_value > 0 else 0

        all_t = set(self.paper_weights) | set(self.live_weights)
        max_diff = 0.0
        for t in all_t:
            diff = abs(self.paper_weights.get(t, 0.0) - self.live_weights.get(t, 0.0))
            if diff > max_diff:
                max_diff = diff

        avg_slip = 0.0
        if self.trade_comparisons:
            avg_slip = sum(tc.slippage_bps for tc in self.trade_comparisons) / len(self.trade_comparisons)

        snap = DualSnapshot(
            date=date,
            paper_value=round(self.paper_value, 2),
            live_value=round(self.live_value, 2),
            paper_return=round(paper_ret, 6),
            live_return=round(live_ret, 6),
            return_divergence=round(abs(paper_ret - live_ret), 6),
            paper_weights={k: round(v, 4) for k, v in self.paper_weights.items()},
            live_weights={k: round(v, 4) for k, v in self.live_weights.items()},
            max_weight_diff=round(max_diff, 4),
            avg_slippage_bps=round(avg_slip, 2),
        )
        self.snapshots.append(snap)
        return snap

    # ── Queries ──────────────────────────────────────────────────

    @property
    def paper_return(self) -> float:
        return (self.paper_value / self.initial_value) - 1 if self.initial_value > 0 else 0

    @property
    def live_return(self) -> float:
        return (self.live_value / self.initial_value) - 1 if self.initial_value > 0 else 0

    @property
    def return_divergence(self) -> float:
        return abs(self.paper_return - self.live_return)

    @property
    def live_drawdown(self) -> float:
        if self.peak_live_value <= 0:
            return 0.0
        return (self.peak_live_value - self.live_value) / self.peak_live_value

    # ── Persistence ──────────────────────────────────────────────

    def save(self) -> None:
        data = {
            "initial_value": self.initial_value,
            "paper_value": self.paper_value,
            "live_value": self.live_value,
            "peak_live_value": self.peak_live_value,
            "paper_weights": self.paper_weights,
            "live_weights": self.live_weights,
            "snapshots": [s.to_dict() for s in self.snapshots[-500:]],
        }
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        tmp.replace(self._path)

    def load(self) -> bool:
        if not self._path.exists():
            return False
        with open(self._path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.initial_value = data.get("initial_value", self.initial_value)
        self.paper_value = data.get("paper_value", self.initial_value)
        self.live_value = data.get("live_value", self.initial_value)
        self.peak_live_value = data.get("peak_live_value", self.initial_value)
        self.paper_weights = data.get("paper_weights", {})
        self.live_weights = data.get("live_weights", {})
        self.snapshots = [
            DualSnapshot(**s) for s in data.get("snapshots", [])
        ]
        return True

    # ── Reporting ────────────────────────────────────────────────

    def generate_comparison_report(self) -> str:
        """Human-readable paper vs live comparison."""
        lines = []
        lines.append("=" * 60)
        lines.append("  DUAL TRACKING REPORT — PAPER vs LIVE")
        lines.append("=" * 60)
        lines.append("")
        lines.append(f"  Paper Value:    ${self.paper_value:>12,.2f}")
        lines.append(f"  Live Value:     ${self.live_value:>12,.2f}")
        lines.append(f"  Paper Return:   {self.paper_return:>+10.2%}")
        lines.append(f"  Live Return:    {self.live_return:>+10.2%}")
        lines.append(f"  Divergence:     {self.return_divergence:>10.2%}")
        lines.append(f"  Live Drawdown:  {self.live_drawdown:>10.2%}")
        lines.append(f"  Peak Live:      ${self.peak_live_value:>12,.2f}")
        lines.append("")

        # Weight comparison
        all_t = sorted(set(self.paper_weights) | set(self.live_weights))
        if all_t:
            lines.append("  WEIGHTS COMPARISON")
            lines.append(f"  {'Ticker':<8} {'Paper':>8} {'Live':>8} {'Diff':>8}")
            lines.append("  " + "-" * 34)
            for t in all_t:
                pw = self.paper_weights.get(t, 0.0)
                lw = self.live_weights.get(t, 0.0)
                diff = lw - pw
                lines.append(f"  {t:<8} {pw:>7.1%} {lw:>7.1%} {diff:>+7.1%}")

        lines.append("")

        # Trade slippage summary
        if self.trade_comparisons:
            slips = [tc.slippage_bps for tc in self.trade_comparisons]
            lines.append("  SLIPPAGE SUMMARY")
            lines.append(f"    Trades:      {len(slips)}")
            lines.append(f"    Avg slip:    {sum(slips)/len(slips):.1f} bps")
            lines.append(f"    Max slip:    {max(slips):.1f} bps")
            lines.append(f"    Min slip:    {min(slips):.1f} bps")

        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)
