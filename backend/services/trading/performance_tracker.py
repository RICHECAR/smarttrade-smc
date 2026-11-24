"""
Performance Tracker — Win Rate, Sharpe, Drawdown, Profit Factor
═══════════════════════════════════════════════════════════════

Records every trade outcome and computes real-time performance metrics.
No database dependency — operates in-memory with optional JSON export.

Feeds back to:
  - PositionSizingEngine (streak data)
  - RiskEngine (drawdown, loss counts)
  - TradingLoop (performance snapshot for monitoring)
"""

import json
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from backend.services.trading.models import PerformanceSnapshot

logger = logging.getLogger("smarttrade.trading.performance")


@dataclass
class TradeRecord:
    """A completed trade for performance tracking."""
    signal_id: str
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    entry_time: float
    exit_time: float
    exit_reason: str = ""

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def hold_seconds(self) -> float:
        return self.exit_time - self.entry_time


class PerformanceTracker:
    """
    Tracks trade outcomes and computes rolling performance metrics.

    Usage:
        tracker = PerformanceTracker()
        tracker.record_trade(signal_id="abc", symbol="BTC", ..., pnl=150.0)
        snapshot = tracker.snapshot()
        print(snapshot.win_rate, snapshot.sharpe_ratio)
    """

    def __init__(self, max_history: int = 500) -> None:
        self._trades: Deque[TradeRecord] = deque(maxlen=max_history)
        self._equity_curve: Deque[float] = deque(maxlen=5000)
        self._returns: Deque[float] = deque(maxlen=5000)
        self._peak_equity: float = 0.0
        self._starting_equity: float = 0.0
        self._current_equity: float = 0.0

    # ──────────────────────────────────────────────────
    #  RECORDING
    # ──────────────────────────────────────────────────

    def set_starting_equity(self, equity: float) -> None:
        """Set baseline equity for return calculations."""
        self._starting_equity = equity
        self._current_equity = equity
        self._peak_equity = equity
        self._equity_curve.append(equity)

    def update_equity(self, equity: float) -> None:
        """Update equity on each cycle (for drawdown tracking)."""
        if equity > 0:
            if self._current_equity > 0:
                ret = (equity - self._current_equity) / self._current_equity
                self._returns.append(ret)
            self._current_equity = equity
            self._equity_curve.append(equity)
            if equity > self._peak_equity:
                self._peak_equity = equity

    def record_trade(
        self,
        signal_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        quantity: float,
        pnl: float,
        entry_time: float,
        exit_time: float,
        exit_reason: str = "",
    ) -> None:
        """Record a completed trade."""
        pnl_pct = 0.0
        cost = entry_price * quantity
        if cost > 0:
            pnl_pct = (pnl / cost) * 100

        record = TradeRecord(
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            pnl=pnl,
            pnl_pct=pnl_pct,
            entry_time=entry_time,
            exit_time=exit_time,
            exit_reason=exit_reason,
        )
        self._trades.append(record)

        logger.info(
            "[PERF] Trade recorded: %s %s | pnl=%.2f (%.2f%%) | reason=%s",
            symbol, side, pnl, pnl_pct, exit_reason,
        )

    # ──────────────────────────────────────────────────
    #  METRICS
    # ──────────────────────────────────────────────────

    def snapshot(self) -> PerformanceSnapshot:
        """Compute a point-in-time performance snapshot."""
        trades = list(self._trades)
        if not trades:
            return PerformanceSnapshot()

        wins = [t for t in trades if t.is_win]
        losses = [t for t in trades if not t.is_win]

        total = len(trades)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / total if total > 0 else 0.0

        avg_win = sum(t.pnl for t in wins) / win_count if win_count > 0 else 0.0
        avg_loss = sum(t.pnl for t in losses) / loss_count if loss_count > 0 else 0.0

        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
            gross_profit if gross_profit > 0 else 0.0
        )

        net_pnl = sum(t.pnl for t in trades)

        # Max drawdown from equity curve
        max_dd = self._max_drawdown_pct()
        current_dd = self._current_drawdown_pct()

        # Sharpe (annualised, assuming ~252 trading days × 24 cycles per day)
        sharpe = self._sharpe_ratio()

        # Streaks
        consec_wins, consec_losses = self._current_streaks()

        # Best / worst trades
        best = max((t.pnl for t in trades), default=0.0)
        worst = min((t.pnl for t in trades), default=0.0)

        # Avg hold time
        avg_hold = sum(t.hold_seconds for t in trades) / total if total > 0 else 0.0

        return PerformanceSnapshot(
            total_trades=total,
            wins=win_count,
            losses=loss_count,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            max_drawdown_pct=max_dd,
            current_drawdown_pct=current_dd,
            sharpe_ratio=sharpe,
            net_pnl=net_pnl,
            consecutive_wins=consec_wins,
            consecutive_losses=consec_losses,
            best_trade=best,
            worst_trade=worst,
            avg_hold_seconds=avg_hold,
        )

    # ──────────────────────────────────────────────────
    #  INTERNAL CALCULATIONS
    # ──────────────────────────────────────────────────

    def _max_drawdown_pct(self) -> float:
        """Maximum drawdown from peak, as percentage."""
        curve = list(self._equity_curve)
        if len(curve) < 2:
            return 0.0

        peak = curve[0]
        max_dd = 0.0
        for eq in curve:
            if eq > peak:
                peak = eq
            dd = ((peak - eq) / peak) * 100 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def _current_drawdown_pct(self) -> float:
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, ((self._peak_equity - self._current_equity) / self._peak_equity) * 100)

    def _sharpe_ratio(self) -> float:
        """Annualised Sharpe ratio from per-cycle returns."""
        returns = list(self._returns)
        if len(returns) < 10:
            return 0.0

        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        std_ret = math.sqrt(variance) if variance > 0 else 0.0

        if std_ret == 0:
            return 0.0

        # Annualise: assume ~6000 cycles/year (24h × 250 days, cycle=1min)
        # Adjust if cycle frequency differs
        periods_per_year = 6000
        return (mean_ret / std_ret) * math.sqrt(periods_per_year)

    def _current_streaks(self) -> tuple:
        """Return (consecutive_wins, consecutive_losses) from most recent trades."""
        trades = list(self._trades)
        if not trades:
            return 0, 0

        consec_wins = 0
        consec_losses = 0

        for t in reversed(trades):
            if t.is_win:
                if consec_losses > 0:
                    break
                consec_wins += 1
            else:
                if consec_wins > 0:
                    break
                consec_losses += 1

        return consec_wins, consec_losses

    # ──────────────────────────────────────────────────
    #  EXPORT
    # ──────────────────────────────────────────────────

    def export_trades_json(self, filepath: str = "data/performance_trades.json") -> None:
        """Export trade history to JSON."""
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        data = []
        for t in self._trades:
            data.append({
                "signal_id": t.signal_id,
                "symbol": t.symbol,
                "side": t.side,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                "pnl": round(t.pnl, 2),
                "pnl_pct": round(t.pnl_pct, 2),
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "hold_seconds": round(t.hold_seconds, 1),
                "exit_reason": t.exit_reason,
            })
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("[PERF] Exported %d trades to %s", len(data), filepath)

    @property
    def trade_count(self) -> int:
        return len(self._trades)

    @property
    def last_trade(self) -> Optional[TradeRecord]:
        return self._trades[-1] if self._trades else None
