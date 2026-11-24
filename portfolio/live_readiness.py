"""
Live Readiness — Portfolio state, rebalance plan, order generation
══════════════════════════════════════════════════════════════════
No automatic execution. Decision and planning layer only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime


# ══════════════════════════════════════════════════════════════════
# PORTFOLIO LIVE STATE
# ══════════════════════════════════════════════════════════════════

@dataclass
class Position:
    ticker: str
    weight: float
    value: float = 0.0


@dataclass
class PortfolioState:
    positions: dict[str, Position] = field(default_factory=dict)
    cash: float = 0.0
    total_value: float = 0.0
    timestamp: str = ""

    def to_weights(self) -> dict[str, float]:
        return {t: p.weight for t, p in self.positions.items() if p.weight > 0.001}

    @classmethod
    def from_weights(cls, weights: dict[str, float], total_value: float = 100_000.0):
        positions = {}
        for t, w in weights.items():
            if w > 0.001:
                positions[t] = Position(ticker=t, weight=w, value=w * total_value)
        cash = total_value * (1.0 - sum(weights.values()))
        return cls(
            positions=positions,
            cash=max(cash, 0.0),
            total_value=total_value,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )


# ══════════════════════════════════════════════════════════════════
# REBALANCE PLAN
# ══════════════════════════════════════════════════════════════════

@dataclass
class RebalanceAction:
    ticker: str
    current_weight: float
    target_weight: float
    delta: float
    action: str             # BUY / SELL / HOLD
    dollar_amount: float    # Estimated dollar trade
    urgency: str            # HIGH / MEDIUM / LOW


@dataclass
class RebalancePlan:
    actions: list[RebalanceAction] = field(default_factory=list)
    total_buys: float = 0.0
    total_sells: float = 0.0
    net_turnover: float = 0.0
    timestamp: str = ""
    safeguard_passed: bool = True
    safeguard_messages: list[str] = field(default_factory=list)


def compute_rebalance_plan(
    current_state: PortfolioState,
    target_weights: dict[str, float],
    total_value: float = None,
    min_trade_pct: float = 0.01,
) -> RebalancePlan:
    """
    Compute the rebalance plan from current to target weights.

    Parameters:
        current_state: Current portfolio state
        target_weights: Target weights dict
        total_value: Portfolio total value (defaults to current_state.total_value)
        min_trade_pct: Minimum delta to trigger a trade (1% default)
    """
    if total_value is None:
        total_value = current_state.total_value

    current_weights = current_state.to_weights()
    all_tickers = sorted(set(current_weights.keys()) | set(target_weights.keys()))

    actions = []
    total_buys = 0.0
    total_sells = 0.0

    for t in all_tickers:
        cw = current_weights.get(t, 0.0)
        tw = target_weights.get(t, 0.0)
        delta = tw - cw
        dollar = delta * total_value

        if abs(delta) < min_trade_pct:
            action = "HOLD"
            urgency = "LOW"
        elif delta > 0:
            action = "BUY"
            total_buys += dollar
            urgency = "HIGH" if delta > 0.05 else "MEDIUM"
        else:
            action = "SELL"
            total_sells += abs(dollar)
            urgency = "HIGH" if abs(delta) > 0.05 else "MEDIUM"

        actions.append(RebalanceAction(
            ticker=t,
            current_weight=cw,
            target_weight=tw,
            delta=delta,
            action=action,
            dollar_amount=dollar,
            urgency=urgency,
        ))

    # Sort: SELL first (to free cash), then BUY
    actions.sort(key=lambda a: (0 if a.action == "SELL" else 1 if a.action == "BUY" else 2, -abs(a.delta)))

    turnover = sum(abs(a.delta) for a in actions) / 2.0

    return RebalancePlan(
        actions=actions,
        total_buys=total_buys,
        total_sells=total_sells,
        net_turnover=turnover,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


# ══════════════════════════════════════════════════════════════════
# ORDER PLAN GENERATOR
# ══════════════════════════════════════════════════════════════════

def generate_order_report(plan: RebalancePlan) -> str:
    """Generate a human-readable rebalance order report."""
    lines = [
        "",
        "╔══════════════════════════════════════════════════════════════════════╗",
        f"║  REBALANCE ORDER PLAN — {plan.timestamp:<42} ║",
        "╠══════════════════════════════════════════════════════════════════════╣",
    ]

    if not plan.safeguard_passed:
        lines.append("║  ⚠ SAFEGUARD BLOCK — REBALANCE NOT APPROVED                        ║")
        for msg in plan.safeguard_messages:
            lines.append(f"║    {msg:<64} ║")
        lines.append("╚══════════════════════════════════════════════════════════════════════╝")
        return "\n".join(lines)

    lines.append(f"║  {'Ticker':<8} {'Action':<6} {'Current':>8} {'Target':>8} {'Delta':>8} {'$Amount':>10} {'Urg':>4} ║")
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")

    for a in plan.actions:
        if a.action == "HOLD" and abs(a.delta) < 0.005:
            continue  # Skip trivial holds
        lines.append(
            f"║  {a.ticker:<8} {a.action:<6} {a.current_weight:>7.1%} {a.target_weight:>7.1%} "
            f"{a.delta:>+7.1%} {a.dollar_amount:>+10,.0f} {a.urgency:>4} ║"
        )

    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append(f"║  Total SELL : ${plan.total_sells:>12,.0f}{'':>38} ║")
    lines.append(f"║  Total BUY  : ${plan.total_buys:>12,.0f}{'':>38} ║")
    lines.append(f"║  Net Turnover: {plan.net_turnover:>6.1%}{'':>46} ║")
    lines.append("╚══════════════════════════════════════════════════════════════════════╝")

    return "\n".join(lines)
