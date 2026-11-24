"""
Capital Efficiency Scorer — Risk-adjusted return per unit of capital
══════════════════════════════════════════════════════════════════

Scores setups by:
  • Risk-adjusted return (edge / drawdown risk)
  • Capital turnover (trades per unit capital)
  • Sharpe proxy (mean / std of returns)

Produces a composite efficiency score 0–1.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import math
from typing import Dict, List

from backend.services.trading.portfolio_models import EfficiencyScore


class CapitalEfficiencyScorer:
    """Score setups by capital efficiency for portfolio optimization."""

    # Component weights
    W_RAR = 0.45        # risk-adjusted return
    W_SHARPE = 0.35     # sharpe proxy
    W_TURNOVER = 0.20   # capital turnover

    def __init__(self) -> None:
        pass

    # ────────────────────────────────────────────────────
    #  PUBLIC
    # ────────────────────────────────────────────────────

    def score(
        self,
        setup_key: str,
        pnls: List[float],
        capital_per_trade: float = 1.0,
        max_drawdown: float = 0.0,
    ) -> EfficiencyScore:
        """
        Score a setup's capital efficiency.

        Parameters
        ----------
        pnls : list of realized PnL per trade
        capital_per_trade : average capital consumed per trade
        max_drawdown : worst drawdown fraction (0–1)
        """
        if not pnls or capital_per_trade <= 0:
            return EfficiencyScore(setup_key=setup_key)

        n = len(pnls)

        # Risk-adjusted return: mean PnL / max(drawdown, small)
        mean_pnl = sum(pnls) / n
        dd_risk = max(max_drawdown, 0.01)
        rar = mean_pnl / (dd_risk * capital_per_trade)

        # Capital turnover: trades per unit of capital
        turnover = n / capital_per_trade if capital_per_trade > 0 else 0.0

        # Sharpe proxy: mean / std
        sharpe = 0.0
        if n >= 2:
            std = math.sqrt(sum((p - mean_pnl) ** 2 for p in pnls) / (n - 1))
            if std > 1e-12:
                sharpe = mean_pnl / std

        # Normalize to 0–1
        rar_norm = self._sigmoid(rar, 0.5)
        sharpe_norm = self._sigmoid(sharpe, 1.0)
        turn_norm = self._sigmoid(turnover, 10.0)

        composite = (
            self.W_RAR * rar_norm
            + self.W_SHARPE * sharpe_norm
            + self.W_TURNOVER * turn_norm
        )
        composite = max(0.0, min(1.0, composite))

        return EfficiencyScore(
            setup_key=setup_key,
            risk_adjusted_return=rar,
            capital_turnover=turnover,
            sharpe_proxy=sharpe,
            efficiency_score=composite,
        )

    def rank(
        self,
        scores: List[EfficiencyScore],
    ) -> List[EfficiencyScore]:
        """Sort and assign ranks to efficiency scores."""
        ranked = sorted(scores, key=lambda s: s.efficiency_score, reverse=True)
        for i, s in enumerate(ranked, 1):
            s.rank = i
        return ranked

    # ────────────────────────────────────────────────────
    #  INTERNAL
    # ────────────────────────────────────────────────────

    @staticmethod
    def _sigmoid(x: float, scale: float) -> float:
        """Normalize x to 0–1 using sigmoid. x=scale → 0.73."""
        if x <= 0:
            return 0.0
        return min(1.0, x / (x + scale))
