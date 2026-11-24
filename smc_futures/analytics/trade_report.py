"""
Trade Report — Attribution & performance analytics from trade journal.
═════════════════════════════════════════════════════════════════════

Reads TradeRecord list and produces:
  - PnL by symbol / broker / strategy / regime
  - Win rate & payoff ratio by setup
  - Expectancy by setup
  - Slippage by broker/symbol
  - Exit reason distribution
  - Outlier loss rate
  - Holding time distribution
  - Top winners / losers

No DB dependency.  Pure computation on TradeRecord list.

Author: Chantier 5 — Trade Journal Institutionnel
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from smc_futures.analytics.trade_journal import TradeRecord

logger = logging.getLogger("smarttrade.analytics.report")

_MIN_TRADES_FOR_STATS = 1


# ═════════════════════════════════════════════════════════════════════════════
# GROUPING RESULT
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class GroupStats:
    """Aggregated statistics for a group of trades."""

    key: str = ""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_fees: float = 0.0
    win_rate: Optional[float] = None
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None
    payoff_ratio: Optional[float] = None   # avg_win / |avg_loss|
    expectancy: Optional[float] = None     # WR * avg_win - (1-WR) * |avg_loss|
    avg_slippage_entry_bps: Optional[float] = None
    avg_slippage_exit_bps: Optional[float] = None
    avg_holding_sec: Optional[float] = None
    avg_mae: Optional[float] = None
    avg_mfe: Optional[float] = None
    outlier_loss_rate: Optional[float] = None  # % of trades with loss > 2σ
    best_trade_pnl: Optional[float] = None
    worst_trade_pnl: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "breakeven": self.breakeven,
            "gross_pnl": round(self.gross_pnl, 4),
            "net_pnl": round(self.net_pnl, 4),
            "total_fees": round(self.total_fees, 4),
            "win_rate": _r(self.win_rate),
            "avg_win": _r(self.avg_win),
            "avg_loss": _r(self.avg_loss),
            "payoff_ratio": _r(self.payoff_ratio),
            "expectancy": _r(self.expectancy),
            "avg_slippage_entry_bps": _r(self.avg_slippage_entry_bps),
            "avg_slippage_exit_bps": _r(self.avg_slippage_exit_bps),
            "avg_holding_sec": _r(self.avg_holding_sec),
            "avg_mae": _r(self.avg_mae),
            "avg_mfe": _r(self.avg_mfe),
            "outlier_loss_rate": _r(self.outlier_loss_rate),
            "best_trade_pnl": _r(self.best_trade_pnl),
            "worst_trade_pnl": _r(self.worst_trade_pnl),
        }


# ═════════════════════════════════════════════════════════════════════════════
# REPORT GENERATOR
# ═════════════════════════════════════════════════════════════════════════════

class TradeReport:
    """
    Generate attribution reports from a list of TradeRecords.
    """

    def __init__(self, records: Sequence[TradeRecord]):
        self._records = list(records)

    @property
    def total(self) -> int:
        return len(self._records)

    # ── Group-by reports ─────────────────────────────────────────────────────

    def by_strategy(self) -> Dict[str, GroupStats]:
        return self._group_by(lambda r: r.strategy_id or "unknown")

    def by_symbol(self) -> Dict[str, GroupStats]:
        return self._group_by(lambda r: r.symbol or "unknown")

    def by_broker(self) -> Dict[str, GroupStats]:
        return self._group_by(lambda r: r.broker or "unknown")

    def by_regime(self) -> Dict[str, GroupStats]:
        return self._group_by(lambda r: r.regime_at_entry or "unknown")

    def by_exit_reason(self) -> Dict[str, GroupStats]:
        return self._group_by(lambda r: r.exit_reason or "UNKNOWN")

    def by_setup(self) -> Dict[str, GroupStats]:
        return self._group_by(lambda r: r.setup_id or "unknown")

    # ── Top / bottom trades ──────────────────────────────────────────────────

    def top_winners(self, n: int = 5) -> List[Dict[str, Any]]:
        """Top N trades by gross PnL."""
        valid = [r for r in self._records if r.gross_pnl is not None]
        valid.sort(key=lambda r: r.gross_pnl, reverse=True)  # type: ignore[arg-type]
        return [_trade_summary(r) for r in valid[:n]]

    def top_losers(self, n: int = 5) -> List[Dict[str, Any]]:
        """Bottom N trades by gross PnL."""
        valid = [r for r in self._records if r.gross_pnl is not None]
        valid.sort(key=lambda r: r.gross_pnl)  # type: ignore[arg-type]
        return [_trade_summary(r) for r in valid[:n]]

    # ── Holding time distribution ────────────────────────────────────────────

    def holding_time_distribution(self) -> Dict[str, int]:
        """Bucket trades by holding time."""
        buckets: Dict[str, int] = {
            "<1m": 0, "1m-5m": 0, "5m-15m": 0, "15m-1h": 0,
            "1h-4h": 0, "4h-1d": 0, ">1d": 0, "unknown": 0,
        }
        for r in self._records:
            ht = r.holding_time_sec
            if ht is None:
                buckets["unknown"] += 1
            elif ht < 60:
                buckets["<1m"] += 1
            elif ht < 300:
                buckets["1m-5m"] += 1
            elif ht < 900:
                buckets["5m-15m"] += 1
            elif ht < 3600:
                buckets["15m-1h"] += 1
            elif ht < 14400:
                buckets["1h-4h"] += 1
            elif ht < 86400:
                buckets["4h-1d"] += 1
            else:
                buckets[">1d"] += 1
        return buckets

    # ── Full report ──────────────────────────────────────────────────────────

    def full_report(self) -> Dict[str, Any]:
        """Generate complete attribution report."""
        return {
            "total_trades": self.total,
            "overall": _compute_stats("overall", self._records).to_dict(),
            "by_strategy": {k: v.to_dict() for k, v in self.by_strategy().items()},
            "by_symbol": {k: v.to_dict() for k, v in self.by_symbol().items()},
            "by_broker": {k: v.to_dict() for k, v in self.by_broker().items()},
            "by_regime": {k: v.to_dict() for k, v in self.by_regime().items()},
            "by_exit_reason": {k: v.to_dict() for k, v in self.by_exit_reason().items()},
            "top_winners": self.top_winners(),
            "top_losers": self.top_losers(),
            "holding_time_distribution": self.holding_time_distribution(),
        }

    # ── Internal ─────────────────────────────────────────────────────────────

    def _group_by(self, key_fn) -> Dict[str, GroupStats]:
        groups: Dict[str, List[TradeRecord]] = defaultdict(list)
        for r in self._records:
            groups[key_fn(r)].append(r)
        return {k: _compute_stats(k, recs) for k, recs in groups.items()}


# ═════════════════════════════════════════════════════════════════════════════
# STATISTICS COMPUTATION
# ═════════════════════════════════════════════════════════════════════════════

def _compute_stats(key: str, records: List[TradeRecord]) -> GroupStats:
    """Compute aggregated stats for a group of trades."""
    gs = GroupStats(key=key, total_trades=len(records))

    if not records:
        return gs

    pnls: List[float] = []
    win_pnls: List[float] = []
    loss_pnls: List[float] = []
    entry_slips: List[float] = []
    exit_slips: List[float] = []
    holdings: List[float] = []
    maes: List[float] = []
    mfes: List[float] = []

    for r in records:
        gpnl = r.gross_pnl or 0.0
        pnls.append(gpnl)
        gs.gross_pnl += gpnl
        gs.net_pnl += (r.net_pnl if r.net_pnl is not None else gpnl)
        gs.total_fees += (r.fees or 0.0)

        if gpnl > 0:
            gs.wins += 1
            win_pnls.append(gpnl)
        elif gpnl < 0:
            gs.losses += 1
            loss_pnls.append(gpnl)
        else:
            gs.breakeven += 1

        if r.slippage_entry_bps is not None:
            entry_slips.append(r.slippage_entry_bps)
        if r.slippage_exit_bps is not None:
            exit_slips.append(r.slippage_exit_bps)
        if r.holding_time_sec is not None:
            holdings.append(r.holding_time_sec)
        if r.mae is not None:
            maes.append(r.mae)
        if r.mfe is not None:
            mfes.append(r.mfe)

    n = gs.total_trades
    gs.win_rate = gs.wins / n if n > 0 else None
    gs.avg_win = _mean(win_pnls)
    gs.avg_loss = _mean(loss_pnls)

    if gs.avg_win is not None and gs.avg_loss is not None and gs.avg_loss != 0:
        gs.payoff_ratio = abs(gs.avg_win / gs.avg_loss)

    if gs.win_rate is not None and gs.avg_win is not None and gs.avg_loss is not None:
        gs.expectancy = (
            gs.win_rate * gs.avg_win + (1 - gs.win_rate) * gs.avg_loss
        )

    gs.avg_slippage_entry_bps = _mean(entry_slips)
    gs.avg_slippage_exit_bps = _mean(exit_slips)
    gs.avg_holding_sec = _mean(holdings)
    gs.avg_mae = _mean(maes)
    gs.avg_mfe = _mean(mfes)

    # Best / worst
    if pnls:
        gs.best_trade_pnl = max(pnls)
        gs.worst_trade_pnl = min(pnls)

    # Outlier loss rate: trades losing > 2σ from mean
    gs.outlier_loss_rate = _outlier_loss_rate(pnls)

    return gs


def _outlier_loss_rate(pnls: List[float]) -> Optional[float]:
    """Fraction of trades with loss exceeding mean - 2*σ."""
    if len(pnls) < 3:
        return None
    mu = statistics.mean(pnls)
    sigma = statistics.stdev(pnls)
    if sigma == 0:
        return 0.0
    threshold = mu - 2 * sigma
    outliers = sum(1 for p in pnls if p < threshold)
    return round(outliers / len(pnls), 4)


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _r(val: Optional[float]) -> Optional[float]:
    """Round if not None."""
    return round(val, 4) if val is not None else None


def _trade_summary(r: TradeRecord) -> Dict[str, Any]:
    return {
        "trade_id": r.trade_id,
        "symbol": r.symbol,
        "side": r.side,
        "strategy_id": r.strategy_id,
        "gross_pnl": r.gross_pnl,
        "net_pnl": r.net_pnl,
        "exit_reason": r.exit_reason,
        "holding_time_sec": r.holding_time_sec,
        "rr_realized": r.rr_realized,
        "mae": r.mae,
        "mfe": r.mfe,
    }
