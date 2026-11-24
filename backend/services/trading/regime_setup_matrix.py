"""
Regime × Setup Matrix — Compatibility Learning Grid
═══════════════════════════════════════════════════

Maintains an NxM matrix of (MarketRegime, SetupType) cells.
Each cell tracks rolling performance stats and makes an allow/block decision.

The matrix is filled as trades arrive. Cells with insufficient data
use a neutral prior. Cells with strong negative evidence are blocked
to prevent regime-incompatible trades.

Grid structure:
  rows    = MarketRegime values (from adaptive_models.py)
  columns = SetupKey strings (discovered dynamically)
  cell    = RegimeSetupCell (win_rate, profit_factor, trade_count, allowed)

Usage:
    matrix = RegimeSetupMatrix()
    matrix.record(regime="trending_up", setup_key="BOS_long_strong", pnl=42.0)
    fit = matrix.get_fit("trending_up", "BOS_long_strong")  # 0.0–1.0
    allowed = matrix.is_allowed("trending_up", "BOS_long_strong")  # True/False
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from backend.services.trading.darwinian_models import RegimeSetupCell

logger = logging.getLogger("smarttrade.darwinian.regime_matrix")


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class RegimeSetupMatrix:
    """
    Self-learning regime-setup compatibility matrix.
    """

    def __init__(self) -> None:
        self._min_trades = _envi("MATRIX_MIN_TRADES", 5)
        self._block_wr = _envf("MATRIX_BLOCK_WR", 0.30)
        self._block_pf = _envf("MATRIX_BLOCK_PF", 0.7)
        self._neutral_fit = _envf("MATRIX_NEUTRAL_FIT", 0.5)

        # grid[(regime_str, setup_key_str)] → RegimeSetupCell
        self._grid: Dict[Tuple[str, str], RegimeSetupCell] = {}

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def record(self, regime: str, setup_key: str, pnl: float) -> RegimeSetupCell:
        """Record a trade outcome for a (regime, setup) pair."""
        cell = self._ensure_cell(regime, setup_key)
        cell.trade_count += 1

        if pnl > 0:
            cell.wins += 1
            cell.gross_profit += pnl
        else:
            cell.losses += 1
            cell.gross_loss += abs(pnl)

        # Recompute
        total = cell.wins + cell.losses
        cell.win_rate = cell.wins / total if total > 0 else 0.0
        cell.profit_factor = (
            cell.gross_profit / cell.gross_loss
            if cell.gross_loss > 0
            else (cell.gross_profit if cell.gross_profit > 0 else 0.0)
        )

        # Auto-block decision
        self._evaluate_allowed(cell)

        return cell

    def get_fit(self, regime: str, setup_key: str) -> float:
        """
        Returns regime-fit score 0.0–1.0 for the given (regime, setup) pair.
        Use this as the `regime_fit` input for SetupSurvivalScorer.
        """
        key = (regime, setup_key)
        cell = self._grid.get(key)

        if cell is None or cell.trade_count < self._min_trades:
            return self._neutral_fit  # not enough data → neutral

        if not cell.allowed:
            return 0.05  # nearly zero but not exactly (leaves door open after cooldown)

        # Composite: 50% win-rate + 50% normalised profit_factor
        wr_part = cell.win_rate
        pf_part = min(1.0, cell.profit_factor / 2.5)
        fit = 0.5 * wr_part + 0.5 * pf_part
        return round(max(0.0, min(1.0, fit)), 4)

    def is_allowed(self, regime: str, setup_key: str) -> bool:
        """Quick check: is this (regime, setup) combo allowed?"""
        cell = self._grid.get((regime, setup_key))
        if cell is None:
            return True  # unknown → allowed by default
        return cell.allowed

    def get_cell(self, regime: str, setup_key: str) -> Optional[RegimeSetupCell]:
        return self._grid.get((regime, setup_key))

    def get_regime_report(self, regime: str) -> Dict[str, Any]:
        """All cells for a given regime."""
        return {
            sk: cell.to_dict()
            for (r, sk), cell in self._grid.items()
            if r == regime
        }

    def full_matrix(self) -> Dict[str, Any]:
        """Full grid dump."""
        result: Dict[str, Dict[str, Any]] = {}
        for (regime, setup_key), cell in self._grid.items():
            result.setdefault(regime, {})[setup_key] = cell.to_dict()
        return result

    def get_best_setups(self, regime: str, top_n: int = 5) -> List[Tuple[str, float]]:
        """Top N setups for a regime, sorted by fit score."""
        fits: List[Tuple[str, float]] = []
        for (r, sk), cell in self._grid.items():
            if r == regime and cell.allowed and cell.trade_count >= self._min_trades:
                fits.append((sk, self.get_fit(r, sk)))
        fits.sort(key=lambda x: x[1], reverse=True)
        return fits[:top_n]

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    def _ensure_cell(self, regime: str, setup_key: str) -> RegimeSetupCell:
        key = (regime, setup_key)
        if key not in self._grid:
            self._grid[key] = RegimeSetupCell(
                regime=regime,
                setup_key=setup_key,
            )
        return self._grid[key]

    def _evaluate_allowed(self, cell: RegimeSetupCell) -> None:
        """Block cell if evidence is strong enough for negative outcome."""
        if cell.trade_count < self._min_trades:
            cell.allowed = True
            return

        if cell.win_rate < self._block_wr and cell.profit_factor < self._block_pf:
            cell.allowed = False
            logger.info(
                "[MATRIX] BLOCKED %s × %s (wr=%.2f, pf=%.2f, n=%d)",
                cell.regime, cell.setup_key,
                cell.win_rate, cell.profit_factor, cell.trade_count,
            )
        else:
            cell.allowed = True
