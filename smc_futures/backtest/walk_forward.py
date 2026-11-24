"""
Walk-Forward Validation — Anti-Overfitting Shield
══════════════════════════════════════════════════
Splits data into rolling train/validate/test windows.
Detects overfitting by comparing in-sample vs out-of-sample performance.

Architecture:
  [===TRAIN===][==VALIDATE==][==TEST==]
                     ↓ shift →
            [===TRAIN===][==VALIDATE==][==TEST==]
                              ↓ shift →
                     [===TRAIN===][==VALIDATE==][==TEST==]

No data leakage: test data is NEVER seen during signal generation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable, Tuple

from smc_futures.backtest.engine import (
    RealisticBacktestEngine,
    RealisticBacktestConfig,
    BTCandle,
    BacktestSignal,
    EngineState,
    RealisticTrade,
)

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────
#  Walk-Forward Config
# ───────────────────────────────────────────

@dataclass
class WalkForwardConfig:
    """Walk-forward validation parameters."""
    # Window sizes (in bars)
    train_bars: int = 2016        # ~7 days of 5m bars
    validate_bars: int = 576      # ~2 days
    test_bars: int = 576          # ~2 days

    # Step size (how much to shift each fold)
    step_bars: int = 576          # ~2 days

    # Minimum trades required
    min_trades_train: int = 10
    min_trades_validate: int = 3
    min_trades_test: int = 3

    # Overfitting detection thresholds
    max_is_oos_pf_ratio: float = 3.0   # In-sample PF / OOS PF > 3 → overfit
    max_is_oos_wr_diff: float = 20.0   # |IS winrate - OOS winrate| > 20% → overfit
    min_oos_profit_factor: float = 1.0  # OOS PF must be > 1.0

    # Number of folds
    max_folds: int = 50


# ───────────────────────────────────────────
#  Fold Result
# ───────────────────────────────────────────

@dataclass
class FoldResult:
    """Result of a single walk-forward fold."""
    fold_index: int
    train_start_idx: int
    train_end_idx: int
    validate_start_idx: int
    validate_end_idx: int
    test_start_idx: int
    test_end_idx: int

    # Train (in-sample) metrics
    train_trades: int = 0
    train_win_rate: float = 0.0
    train_profit_factor: float = 0.0
    train_net_pnl: float = 0.0
    train_max_dd_pct: float = 0.0

    # Validate metrics
    validate_trades: int = 0
    validate_win_rate: float = 0.0
    validate_profit_factor: float = 0.0
    validate_net_pnl: float = 0.0

    # Test (out-of-sample) metrics
    test_trades: int = 0
    test_win_rate: float = 0.0
    test_profit_factor: float = 0.0
    test_net_pnl: float = 0.0
    test_max_dd_pct: float = 0.0

    # Overfitting signals
    is_overfit: bool = False
    overfit_reasons: List[str] = field(default_factory=list)


# ───────────────────────────────────────────
#  Walk-Forward Result
# ───────────────────────────────────────────

@dataclass
class WalkForwardResult:
    """Aggregated walk-forward validation result."""
    symbol: str
    total_folds: int = 0
    valid_folds: int = 0        # Folds with enough trades
    overfit_folds: int = 0

    # Aggregated OOS (test) metrics
    oos_total_trades: int = 0
    oos_win_rate: float = 0.0
    oos_profit_factor: float = 0.0
    oos_total_pnl: float = 0.0
    oos_avg_pnl_per_trade: float = 0.0
    oos_max_dd_pct: float = 0.0

    # Aggregated IS (train) metrics for comparison
    is_win_rate: float = 0.0
    is_profit_factor: float = 0.0

    # Consistency
    oos_positive_folds: int = 0
    oos_consistency_ratio: float = 0.0  # % of folds profitable OOS

    # Overfitting score (0 = no overfit, 1 = severe overfit)
    overfit_score: float = 0.0

    folds: List[FoldResult] = field(default_factory=list)

    @property
    def is_robust(self) -> bool:
        """Passes walk-forward validation."""
        return (
            self.valid_folds >= 3
            and self.oos_profit_factor > 1.0
            and self.oos_consistency_ratio >= 0.5
            and self.overfit_score < 0.5
        )


# ═══════════════════════════════════════════
#  WALK-FORWARD VALIDATOR
# ═══════════════════════════════════════════

class WalkForwardValidator:
    """
    Runs rolling walk-forward validation on a strategy.

    Each fold:
      1. Train: Generate signals on train window (warm-up only, not used for scoring)
      2. Validate: Run strategy on validation window, compare vs train
      3. Test: Run strategy on test window (true OOS performance)
    """

    def __init__(
        self,
        wf_config: WalkForwardConfig,
        bt_config: RealisticBacktestConfig,
    ):
        self.wf_config = wf_config
        self.bt_config = bt_config

    def validate(
        self,
        symbol: str,
        candles: List[BTCandle],
        signal_fn: Callable[[List[BTCandle]], Optional[BacktestSignal]],
    ) -> WalkForwardResult:
        """
        Run walk-forward validation.

        Args:
            symbol: Trading pair
            candles: Full historical data
            signal_fn: Strategy function (receives only closed bars)
        """
        wf = self.wf_config
        result = WalkForwardResult(symbol=symbol)

        total_window = wf.train_bars + wf.validate_bars + wf.test_bars
        if len(candles) < total_window:
            logger.warning(
                f"[WF] {symbol}: Not enough candles ({len(candles)} < {total_window})"
            )
            return result

        # Generate fold indices
        folds = self._generate_folds(len(candles))
        result.total_folds = len(folds)

        all_oos_trades: List[RealisticTrade] = []
        all_is_trades: List[RealisticTrade] = []

        for fold_idx, (train_s, train_e, val_s, val_e, test_s, test_e) in enumerate(folds):
            fold = FoldResult(
                fold_index=fold_idx,
                train_start_idx=train_s, train_end_idx=train_e,
                validate_start_idx=val_s, validate_end_idx=val_e,
                test_start_idx=test_s, test_end_idx=test_e,
            )

            # ── Run on train window ──
            train_candles = candles[train_s:train_e]
            train_state = self._run_window(symbol, train_candles, signal_fn)
            fold.train_trades = len(train_state.trades)
            if fold.train_trades > 0:
                fold.train_win_rate = self._win_rate(train_state.trades)
                fold.train_profit_factor = self._profit_factor(train_state.trades)
                fold.train_net_pnl = sum(t.net_pnl for t in train_state.trades)
                fold.train_max_dd_pct = self._max_dd_pct(train_state)
                all_is_trades.extend(train_state.trades)

            # ── Run on validate window (uses train for warm-up) ──
            val_candles = candles[train_s:val_e]  # Include train for warm-up
            val_state = self._run_window(symbol, val_candles, signal_fn)
            # Only count trades that occurred in validation period
            val_trades = [
                t for t in val_state.trades
                if t.entry_bar_idx >= (val_s - train_s)
            ]
            fold.validate_trades = len(val_trades)
            if fold.validate_trades > 0:
                fold.validate_win_rate = self._win_rate(val_trades)
                fold.validate_profit_factor = self._profit_factor(val_trades)
                fold.validate_net_pnl = sum(t.net_pnl for t in val_trades)

            # ── Run on test window (uses train+val for warm-up) ──
            test_candles = candles[train_s:test_e]  # Include all for warm-up
            test_state = self._run_window(symbol, test_candles, signal_fn)
            # Only count trades in test period
            test_trades = [
                t for t in test_state.trades
                if t.entry_bar_idx >= (test_s - train_s)
            ]
            fold.test_trades = len(test_trades)
            if fold.test_trades > 0:
                fold.test_win_rate = self._win_rate(test_trades)
                fold.test_profit_factor = self._profit_factor(test_trades)
                fold.test_net_pnl = sum(t.net_pnl for t in test_trades)
                fold.test_max_dd_pct = self._max_dd_pct(test_state)
                all_oos_trades.extend(test_trades)

            # ── Overfitting detection ──
            if fold.train_trades >= wf.min_trades_train and fold.test_trades >= wf.min_trades_test:
                result.valid_folds += 1

                # Check PF ratio
                if fold.test_profit_factor > 0 and fold.train_profit_factor > 0:
                    pf_ratio = fold.train_profit_factor / fold.test_profit_factor
                    if pf_ratio > wf.max_is_oos_pf_ratio:
                        fold.is_overfit = True
                        fold.overfit_reasons.append(
                            f"PF ratio IS/OOS = {pf_ratio:.1f} > {wf.max_is_oos_pf_ratio}"
                        )

                # Check win rate degradation
                wr_diff = abs(fold.train_win_rate - fold.test_win_rate)
                if wr_diff > wf.max_is_oos_wr_diff:
                    fold.is_overfit = True
                    fold.overfit_reasons.append(
                        f"WR diff IS/OOS = {wr_diff:.1f}% > {wf.max_is_oos_wr_diff}%"
                    )

                # OOS PF < 1
                if fold.test_profit_factor < wf.min_oos_profit_factor:
                    fold.is_overfit = True
                    fold.overfit_reasons.append(
                        f"OOS PF = {fold.test_profit_factor:.2f} < {wf.min_oos_profit_factor}"
                    )

                if fold.is_overfit:
                    result.overfit_folds += 1
                if fold.test_net_pnl > 0:
                    result.oos_positive_folds += 1

            result.folds.append(fold)

        # ── Aggregate results ──
        if all_oos_trades:
            result.oos_total_trades = len(all_oos_trades)
            result.oos_win_rate = self._win_rate(all_oos_trades)
            result.oos_profit_factor = self._profit_factor(all_oos_trades)
            result.oos_total_pnl = sum(t.net_pnl for t in all_oos_trades)
            result.oos_avg_pnl_per_trade = result.oos_total_pnl / len(all_oos_trades)

        if all_is_trades:
            result.is_win_rate = self._win_rate(all_is_trades)
            result.is_profit_factor = self._profit_factor(all_is_trades)

        if result.valid_folds > 0:
            result.oos_consistency_ratio = result.oos_positive_folds / result.valid_folds
            result.overfit_score = result.overfit_folds / result.valid_folds

        return result

    # ── Internal helpers ─────────────────────

    def _generate_folds(self, n_bars: int) -> List[Tuple[int, int, int, int, int, int]]:
        """Generate fold indices: (train_s, train_e, val_s, val_e, test_s, test_e)."""
        wf = self.wf_config
        folds = []
        start = 0
        while start + wf.train_bars + wf.validate_bars + wf.test_bars <= n_bars:
            train_s = start
            train_e = start + wf.train_bars
            val_s = train_e
            val_e = val_s + wf.validate_bars
            test_s = val_e
            test_e = test_s + wf.test_bars
            folds.append((train_s, train_e, val_s, val_e, test_s, test_e))
            start += wf.step_bars
            if len(folds) >= wf.max_folds:
                break
        return folds

    def _run_window(
        self,
        symbol: str,
        candles: List[BTCandle],
        signal_fn: Callable[[List[BTCandle]], Optional[BacktestSignal]],
    ) -> EngineState:
        """Run backtest engine on a window of candles."""
        engine = RealisticBacktestEngine(config=self.bt_config)
        return engine.run(symbol, candles, signal_fn)

    @staticmethod
    def _win_rate(trades: List[RealisticTrade]) -> float:
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.is_win)
        return wins / len(trades) * 100

    @staticmethod
    def _profit_factor(trades: List[RealisticTrade]) -> float:
        gross_profit = sum(t.net_pnl for t in trades if t.net_pnl > 0)
        gross_loss = abs(sum(t.net_pnl for t in trades if t.net_pnl < 0))
        if gross_loss <= 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @staticmethod
    def _max_dd_pct(state: EngineState) -> float:
        if not state.equity_curve:
            return 0.0
        peak = state.equity_curve[0]
        max_dd = 0.0
        for eq in state.equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
        return max_dd
