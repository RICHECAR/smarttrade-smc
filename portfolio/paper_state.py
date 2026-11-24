"""
Paper State — Persistent virtual portfolio state.
══════════════════════════════════════════════════
Stores cash, positions, daily valuations, order history.
Serializes to JSON for restart-safe persistence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


DEFAULT_STATE_PATH = Path(__file__).parent / "data" / "portfolio_paper_state.json"


@dataclass
class PaperPosition:
    ticker: str
    shares: float        # fractional allowed for simulation
    avg_cost: float      # average cost basis per share
    current_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.shares * self.current_price

    @property
    def cost_basis(self) -> float:
        return self.shares * self.avg_cost

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "shares": round(self.shares, 6),
            "avg_cost": round(self.avg_cost, 4),
            "current_price": round(self.current_price, 4),
        }


@dataclass
class PaperOrder:
    timestamp: str
    ticker: str
    side: str            # BUY / SELL
    shares: float
    price: float
    cost_bps: float      # simulated cost in bps
    cost_dollar: float
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DailyValuation:
    date: str
    total_value: float
    cash: float
    invested: float
    benchmark_value: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class PaperPortfolioState:
    """Virtual portfolio with persistence."""

    def __init__(
        self,
        initial_cash: float = 100_000.0,
        state_path: Optional[Path] = None,
    ):
        self._state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self._state_path.parent.mkdir(parents=True, exist_ok=True)

        self.cash: float = initial_cash
        self.initial_cash: float = initial_cash
        self.positions: Dict[str, PaperPosition] = {}
        self.order_history: List[PaperOrder] = []
        self.valuations: List[DailyValuation] = []
        self.inception_date: str = ""
        self.last_rebal_date: str = ""
        self.rebal_count: int = 0

    # ── Valuation ─────────────────────────────────────────────────

    @property
    def invested_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def total_value(self) -> float:
        return self.cash + self.invested_value

    def weights(self) -> Dict[str, float]:
        tv = self.total_value
        if tv <= 0:
            return {}
        return {t: p.market_value / tv for t, p in self.positions.items() if p.shares > 1e-9}

    def update_prices(self, prices: Dict[str, float]) -> None:
        for t, pos in self.positions.items():
            if t in prices and not np.isnan(prices[t]):
                pos.current_price = prices[t]

    def record_valuation(self, date: str, benchmark_value: float = 0.0) -> None:
        self.valuations.append(DailyValuation(
            date=date,
            total_value=round(self.total_value, 2),
            cash=round(self.cash, 2),
            invested=round(self.invested_value, 2),
            benchmark_value=round(benchmark_value, 4),
        ))

    # ── Order execution (simulated) ──────────────────────────────

    def execute_buy(self, ticker: str, shares: float, price: float,
                    cost_bps: float = 5.0, reason: str = "") -> PaperOrder:
        dollar = shares * price
        cost = dollar * cost_bps / 10_000
        total_cost = dollar + cost

        if total_cost > self.cash + 0.01:
            # Not enough cash — reduce to available
            available = max(self.cash - cost, 0)
            shares = available / price if price > 0 else 0
            dollar = shares * price
            cost = dollar * cost_bps / 10_000
            total_cost = dollar + cost

        self.cash -= total_cost

        if ticker in self.positions:
            pos = self.positions[ticker]
            old_cost_basis = pos.shares * pos.avg_cost
            pos.shares += shares
            pos.avg_cost = (old_cost_basis + dollar) / pos.shares if pos.shares > 0 else 0
            pos.current_price = price
        else:
            self.positions[ticker] = PaperPosition(
                ticker=ticker, shares=shares, avg_cost=price, current_price=price,
            )

        order = PaperOrder(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ticker=ticker, side="BUY", shares=round(shares, 6),
            price=round(price, 4), cost_bps=cost_bps,
            cost_dollar=round(cost, 4), reason=reason,
        )
        self.order_history.append(order)
        return order

    def execute_sell(self, ticker: str, shares: float, price: float,
                     cost_bps: float = 5.0, reason: str = "") -> PaperOrder:
        if ticker not in self.positions:
            shares = 0.0
        else:
            shares = min(shares, self.positions[ticker].shares)

        dollar = shares * price
        cost = dollar * cost_bps / 10_000
        net_proceeds = dollar - cost

        self.cash += net_proceeds

        if ticker in self.positions:
            self.positions[ticker].shares -= shares
            self.positions[ticker].current_price = price
            if self.positions[ticker].shares < 1e-9:
                del self.positions[ticker]

        order = PaperOrder(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ticker=ticker, side="SELL", shares=round(shares, 6),
            price=round(price, 4), cost_bps=cost_bps,
            cost_dollar=round(cost, 4), reason=reason,
        )
        self.order_history.append(order)
        return order

    # ── Persistence ──────────────────────────────────────────────

    def save(self) -> None:
        data = {
            "cash": round(self.cash, 4),
            "initial_cash": self.initial_cash,
            "inception_date": self.inception_date,
            "last_rebal_date": self.last_rebal_date,
            "rebal_count": self.rebal_count,
            "positions": {t: p.to_dict() for t, p in self.positions.items()},
            "order_history": [o.to_dict() for o in self.order_history[-500:]],
            "valuations": [v.to_dict() for v in self.valuations[-1000:]],
        }
        tmp = self._state_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        tmp.replace(self._state_path)

    def load(self) -> bool:
        if not self._state_path.exists():
            return False
        with open(self._state_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.cash = data.get("cash", self.initial_cash)
        self.initial_cash = data.get("initial_cash", self.initial_cash)
        self.inception_date = data.get("inception_date", "")
        self.last_rebal_date = data.get("last_rebal_date", "")
        self.rebal_count = data.get("rebal_count", 0)

        self.positions = {}
        for t, d in data.get("positions", {}).items():
            self.positions[t] = PaperPosition(**d)

        self.order_history = [PaperOrder(**o) for o in data.get("order_history", [])]
        self.valuations = [DailyValuation(**v) for v in data.get("valuations", [])]
        return True

    def reset(self, initial_cash: float = None) -> None:
        if initial_cash is not None:
            self.initial_cash = initial_cash
        self.cash = self.initial_cash
        self.positions.clear()
        self.order_history.clear()
        self.valuations.clear()
        self.inception_date = ""
        self.last_rebal_date = ""
        self.rebal_count = 0
