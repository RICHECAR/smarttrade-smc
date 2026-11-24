"""
Paper Broker — Simulated order execution with realistic costs.
═══════════════════════════════════════════════════════════════
No real orders. Translates a RebalancePlan into simulated fills
on PaperPortfolioState.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from portfolio.paper_state import PaperPortfolioState, PaperOrder

logger = logging.getLogger(__name__)


class PaperBroker:
    """Simulated broker that applies a rebalance plan to virtual state."""

    def __init__(self, cost_bps: float = 5.0):
        self.cost_bps = cost_bps

    def execute_rebalance(
        self,
        state: PaperPortfolioState,
        target_weights: Dict[str, float],
        prices: Dict[str, float],
    ) -> Tuple[List[PaperOrder], float]:
        """
        Execute rebalance from current state to target weights.
        
        SELLs first (to free cash), then BUYs.
        
        Returns:
            (orders, total_cost_dollar)
        """
        total_value = state.total_value
        current_weights = state.weights()

        all_tickers = sorted(set(current_weights) | set(target_weights))

        sells: List[Tuple[str, float]] = []
        buys: List[Tuple[str, float]] = []

        for t in all_tickers:
            cw = current_weights.get(t, 0.0)
            tw = target_weights.get(t, 0.0)
            delta_dollar = (tw - cw) * total_value

            if delta_dollar < -1.0:  # SELL
                sells.append((t, abs(delta_dollar)))
            elif delta_dollar > 1.0:  # BUY
                buys.append((t, delta_dollar))

        orders: List[PaperOrder] = []
        total_cost = 0.0

        # Execute sells first
        for ticker, dollar_amount in sells:
            price = prices.get(ticker, 0.0)
            if price <= 0:
                logger.warning("[PAPER_BROKER] No price for %s — skip sell", ticker)
                continue
            shares = dollar_amount / price
            order = state.execute_sell(ticker, shares, price, self.cost_bps, reason="REBAL_SELL")
            orders.append(order)
            total_cost += order.cost_dollar

        # Then buys
        for ticker, dollar_amount in buys:
            price = prices.get(ticker, 0.0)
            if price <= 0:
                logger.warning("[PAPER_BROKER] No price for %s — skip buy", ticker)
                continue
            shares = dollar_amount / price
            order = state.execute_buy(ticker, shares, price, self.cost_bps, reason="REBAL_BUY")
            orders.append(order)
            total_cost += order.cost_dollar

        logger.info(
            "[PAPER_BROKER] Rebalance: %d orders, cost=$%.2f",
            len(orders), total_cost,
        )
        return orders, total_cost
