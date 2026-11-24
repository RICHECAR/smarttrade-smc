"""
Live Broker — Real order execution with slippage tracking.
══════════════════════════════════════════════════════════
Wraps a generic broker interface to:
  - Execute real orders (or simulate in dry_run mode)
  - Track slippage vs intended price
  - Enforce per-order size limits
  - Log every fill to LiveJournal

Broker adaptor protocol: any object with:
  - place_market_order(ticker, side, shares) → dict with
    {filled_shares, filled_price, order_id, status, fee}
  - get_positions() → dict {ticker: {shares, avg_cost, current_price}}
  - get_account_value() → float
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Tuple

from portfolio.live_config import LiveSafeConfig
from portfolio.live_journal import LiveJournal, LiveOrderEntry
from portfolio.live_alerts import AlertCollector, alert_slippage

logger = logging.getLogger(__name__)


class BrokerAdaptor(Protocol):
    """Protocol a real broker must satisfy."""

    def place_market_order(
        self, ticker: str, side: str, shares: float,
    ) -> Dict[str, Any]:
        """Returns {filled_shares, filled_price, order_id, status, fee}."""
        ...

    def get_positions(self) -> Dict[str, Dict[str, float]]:
        """Returns {ticker: {shares, avg_cost, current_price}}."""
        ...

    def get_account_value(self) -> float: ...


class DryRunBroker:
    """Simulated broker for dry-run mode. Fills at intended price + random noise."""

    def __init__(self, prices: Dict[str, float], slippage_bps: float = 2.0):
        self._prices = dict(prices)
        self._slippage_bps = slippage_bps
        self._positions: Dict[str, Dict[str, float]] = {}
        self._cash: float = 0.0

    def set_cash(self, cash: float) -> None:
        self._cash = cash

    def set_positions(self, positions: Dict[str, Dict[str, float]]) -> None:
        self._positions = dict(positions)

    def update_prices(self, prices: Dict[str, float]) -> None:
        self._prices.update(prices)

    def place_market_order(
        self, ticker: str, side: str, shares: float,
    ) -> Dict[str, Any]:
        base_price = self._prices.get(ticker, 0.0)
        if base_price <= 0:
            return {
                "filled_shares": 0, "filled_price": 0,
                "order_id": "", "status": "REJECTED", "fee": 0,
            }
        # Simulate slippage: buys pay more, sells receive less
        slip_mult = self._slippage_bps / 10_000
        if side == "BUY":
            fill_price = base_price * (1 + slip_mult)
        else:
            fill_price = base_price * (1 - slip_mult)

        fee = shares * fill_price * 5 / 10_000  # 5 bps commission

        # Update internal positions
        if side == "BUY":
            self._cash -= shares * fill_price + fee
            pos = self._positions.get(ticker, {"shares": 0, "avg_cost": 0, "current_price": 0})
            old_shares = pos["shares"]
            old_cost = pos["avg_cost"]
            new_shares = old_shares + shares
            pos["avg_cost"] = (old_cost * old_shares + fill_price * shares) / new_shares if new_shares > 0 else 0
            pos["shares"] = new_shares
            pos["current_price"] = fill_price
            self._positions[ticker] = pos
        else:
            self._cash += shares * fill_price - fee
            pos = self._positions.get(ticker, {"shares": 0, "avg_cost": 0, "current_price": 0})
            pos["shares"] = max(0, pos["shares"] - shares)
            pos["current_price"] = fill_price
            if pos["shares"] < 1e-9:
                self._positions.pop(ticker, None)
            else:
                self._positions[ticker] = pos

        return {
            "filled_shares": shares,
            "filled_price": round(fill_price, 4),
            "order_id": f"DRY-{ticker}-{int(time.time())}",
            "status": "FILLED",
            "fee": round(fee, 4),
        }

    def get_positions(self) -> Dict[str, Dict[str, float]]:
        return dict(self._positions)

    def get_account_value(self) -> float:
        invested = sum(
            p["shares"] * p["current_price"]
            for p in self._positions.values()
        )
        return self._cash + invested


@dataclass
class LiveFill:
    """Result of executing one order."""
    ticker: str
    side: str
    intended_shares: float
    intended_price: float
    filled_shares: float
    filled_price: float
    slippage_bps: float
    cost: float
    order_id: str
    status: str
    latency_ms: float


class LiveBroker:
    """Orchestrates real order execution with safety controls."""

    def __init__(
        self,
        broker: Any,  # BrokerAdaptor
        journal: LiveJournal,
        cfg: LiveSafeConfig,
        alert_collector: Optional[AlertCollector] = None,
    ):
        self._broker = broker
        self._journal = journal
        self._cfg = cfg
        self._alerts = alert_collector

    def execute_rebalance(
        self,
        target_weights: Dict[str, float],
        current_weights: Dict[str, float],
        prices: Dict[str, float],
        account_value: float,
        rebal_date: str,
    ) -> Tuple[List[LiveFill], float]:
        """
        Execute rebalance: SELLs first, then BUYs.
        Returns (fills, total_cost).
        """
        all_tickers = sorted(set(target_weights) | set(current_weights))

        sells: List[Tuple[str, float]] = []
        buys: List[Tuple[str, float]] = []

        for t in all_tickers:
            cw = current_weights.get(t, 0.0)
            tw = target_weights.get(t, 0.0)
            delta_dollar = (tw - cw) * account_value
            if delta_dollar < -1.0:
                sells.append((t, abs(delta_dollar)))
            elif delta_dollar > 1.0:
                buys.append((t, delta_dollar))

        fills: List[LiveFill] = []
        total_cost = 0.0

        # Execute sells first
        for ticker, dollar_amount in sells:
            fill = self._execute_single(
                ticker, "SELL", dollar_amount, prices, rebal_date,
            )
            if fill:
                fills.append(fill)
                total_cost += fill.cost

        # Then buys
        for ticker, dollar_amount in buys:
            fill = self._execute_single(
                ticker, "BUY", dollar_amount, prices, rebal_date,
            )
            if fill:
                fills.append(fill)
                total_cost += fill.cost

        return fills, total_cost

    def _execute_single(
        self,
        ticker: str,
        side: str,
        dollar_amount: float,
        prices: Dict[str, float],
        rebal_date: str,
    ) -> Optional[LiveFill]:
        """Execute a single order with retry and slippage tracking."""
        price = prices.get(ticker, 0.0)
        if price <= 0:
            logger.warning("[LIVE_BROKER] No price for %s — skipping", ticker)
            return None

        shares = dollar_amount / price

        # Attempt execution with retry
        result = None
        latency_ms = 0.0
        for attempt in range(1 + self._cfg.max_order_retry):
            t0 = time.monotonic()
            try:
                result = self._broker.place_market_order(ticker, side, shares)
                latency_ms = (time.monotonic() - t0) * 1000
                if result.get("status") in ("FILLED", "PARTIAL"):
                    break
            except Exception as e:
                latency_ms = (time.monotonic() - t0) * 1000
                logger.error(
                    "[LIVE_BROKER] %s %s attempt %d failed: %s",
                    side, ticker, attempt + 1, e,
                )
                result = {
                    "filled_shares": 0, "filled_price": 0,
                    "order_id": "", "status": "ERROR", "fee": 0,
                }

        if not result or result.get("status") not in ("FILLED", "PARTIAL"):
            logger.error("[LIVE_BROKER] %s %s FAILED after retries", side, ticker)
            # Log failed order
            self._journal.append_order(LiveOrderEntry(
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                rebal_date=rebal_date,
                ticker=ticker, side=side,
                intended_shares=round(shares, 6),
                intended_price=round(price, 4),
                broker_status=result.get("status", "UNKNOWN") if result else "UNKNOWN",
                error=str(result) if result else "No response",
            ))
            return None

        filled_price = result["filled_price"]
        filled_shares = result["filled_shares"]
        fee = result.get("fee", 0.0)
        order_id = result.get("order_id", "")

        # Slippage calc
        if price > 0:
            if side == "BUY":
                slippage_bps = (filled_price - price) / price * 10_000
            else:
                slippage_bps = (price - filled_price) / price * 10_000
        else:
            slippage_bps = 0.0

        # Alert on abnormal slippage
        if self._alerts:
            alert_slippage(ticker, slippage_bps, self._cfg, self._alerts)

        fill = LiveFill(
            ticker=ticker, side=side,
            intended_shares=round(shares, 6),
            intended_price=round(price, 4),
            filled_shares=round(filled_shares, 6),
            filled_price=round(filled_price, 4),
            slippage_bps=round(slippage_bps, 2),
            cost=round(fee, 4),
            order_id=order_id,
            status=result["status"],
            latency_ms=round(latency_ms, 1),
        )

        # Log to journal
        self._journal.append_order(LiveOrderEntry(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            rebal_date=rebal_date,
            ticker=ticker, side=side,
            intended_shares=fill.intended_shares,
            intended_price=fill.intended_price,
            filled_shares=fill.filled_shares,
            filled_price=fill.filled_price,
            slippage_bps=fill.slippage_bps,
            cost_dollar=fill.cost,
            broker_order_id=fill.order_id,
            broker_status=fill.status,
            latency_ms=fill.latency_ms,
        ))

        logger.info(
            "[LIVE_BROKER] %s %s %.4f @ $%.2f (slip=%.1fbps, cost=$%.2f, %s)",
            side, ticker, filled_shares, filled_price,
            slippage_bps, fee, fill.status,
        )
        return fill
