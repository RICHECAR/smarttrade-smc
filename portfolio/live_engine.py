"""
Live Engine — Secured live rebalance pipeline.
═══════════════════════════════════════════════
Pipeline: paper_rebalance → risk_guards → live_execution → dual_tracking → alerts → journal

Runs paper engine as reference, then executes live orders
only if all risk guards pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from portfolio.paper_engine import (
    PaperEngine,
    RebalanceResult,
    VALIDATED_SIGNAL_CONFIG,
    VALIDATED_ALLOCATOR_CONFIG,
)
from portfolio.paper_state import PaperPortfolioState
from portfolio.paper_broker import PaperBroker
from portfolio.paper_journal import PaperJournal
from portfolio.paper_constraints import PaperConstraints, cap_turnover_weights

from portfolio.live_config import LiveSafeConfig
from portfolio.live_broker import LiveBroker, LiveFill
from portfolio.live_journal import LiveJournal, LiveRebalanceEntry
from portfolio.live_risk_guards import run_pre_trade_guards, RiskGuardReport
from portfolio.live_alerts import (
    AlertCollector,
    AlertSeverity,
    run_all_alerts,
)
from portfolio.live_dual_tracker import DualTracker

logger = logging.getLogger(__name__)


@dataclass
class LiveRebalanceResult:
    """Result of one live rebalance cycle."""
    date: str
    # Paper side
    paper_passed: bool = False
    paper_blocked: bool = False
    paper_block_reasons: List[str] = field(default_factory=list)
    paper_target_weights: Dict[str, float] = field(default_factory=dict)
    paper_exec_weights: Dict[str, float] = field(default_factory=dict)
    paper_value: float = 0.0
    paper_turnover: float = 0.0
    paper_cost: float = 0.0
    # Risk guards
    risk_guard_passed: bool = False
    risk_guard_blockers: List[str] = field(default_factory=list)
    # Live side
    live_executed: bool = False
    live_fills: List[LiveFill] = field(default_factory=list)
    live_cost: float = 0.0
    avg_slippage_bps: float = 0.0
    live_value_after: float = 0.0
    live_weights_after: Dict[str, float] = field(default_factory=dict)
    # Alerts
    alerts: List[str] = field(default_factory=list)
    # Tracking
    return_divergence: float = 0.0
    mode: str = "dry_run"


class LiveEngine:
    """Orchestrates secured live rebalance pipeline."""

    def __init__(
        self,
        # Paper components (reference)
        paper_state: PaperPortfolioState,
        paper_journal: PaperJournal,
        paper_engine: PaperEngine,
        # Live components
        live_broker: LiveBroker,
        live_journal: LiveJournal,
        # Dual tracking
        tracker: DualTracker,
        # Config
        cfg: LiveSafeConfig,
        alert_collector: Optional[AlertCollector] = None,
    ):
        self.paper_state = paper_state
        self.paper_journal = paper_journal
        self.paper_engine = paper_engine
        self.live_broker = live_broker
        self.live_journal = live_journal
        self.tracker = tracker
        self.cfg = cfg
        self.alerts = alert_collector or AlertCollector()

    def rebalance_step(
        self,
        prices: pd.DataFrame,
        rebal_date: pd.Timestamp,
        live_weights: Dict[str, float],
        live_invested: float,
        live_account_value: float,
        benchmark_ticker: str = "SPY",
    ) -> LiveRebalanceResult:
        """
        Full live rebalance pipeline:
          1. Run paper engine (reference)
          2. Pre-trade risk guards
          3. Execute live orders (if guards pass)
          4. Dual tracking update
          5. Post-trade alerts
          6. Journal write
        """
        date_str = rebal_date.strftime("%Y-%m-%d")
        result = LiveRebalanceResult(date=date_str)
        result.mode = "dry_run" if self.cfg.dry_run else "live"
        self.alerts.reset()

        # ── 1. Paper engine (reference rebalance) ─────────────────
        paper_result = self.paper_engine.rebalance_step(
            prices, rebal_date, benchmark_ticker,
        )
        result.paper_passed = paper_result.passed
        result.paper_blocked = paper_result.blocked
        result.paper_block_reasons = paper_result.block_reasons
        result.paper_target_weights = paper_result.target_weights
        result.paper_value = self.paper_state.total_value
        result.paper_turnover = paper_result.turnover
        result.paper_cost = paper_result.cost

        # If paper blocked, live also blocked
        if paper_result.blocked:
            result.risk_guard_passed = False
            result.risk_guard_blockers = [
                f"PAPER_BLOCKED: {' | '.join(paper_result.block_reasons)}"
            ]
            self._write_journal(result, date_str)
            return result

        # Get the exec weights (paper applied turnover cap)
        paper_exec_weights = self.paper_state.weights()
        result.paper_exec_weights = dict(paper_exec_weights)

        # ── 2. Pre-trade risk guards ──────────────────────────────
        last_prices = prices.loc[:rebal_date].iloc[-1].to_dict()
        guard_report = run_pre_trade_guards(
            target_weights=paper_exec_weights,
            prices=last_prices,
            account_value=live_account_value,
            current_invested=live_invested,
            peak_value=self.tracker.peak_live_value,
            paper_weights=paper_exec_weights,
            live_weights=live_weights,
            paper_return=self.tracker.paper_return,
            live_return=self.tracker.live_return,
            cfg=self.cfg,
        )
        result.risk_guard_passed = guard_report.passed
        result.risk_guard_blockers = guard_report.blockers

        # Collect warnings as alerts
        for w in guard_report.warnings:
            self.alerts.emit(AlertSeverity.WARNING, "RISK_GUARD_WARN", w)

        if not guard_report.passed:
            for b in guard_report.blockers:
                self.alerts.emit(AlertSeverity.CRITICAL, "RISK_GUARD_BLOCK", b)
            result.alerts = self.alerts.summary()
            self._write_journal(result, date_str)
            return result

        # ── 3. Execute live orders ────────────────────────────────
        fills, total_cost = self.live_broker.execute_rebalance(
            target_weights=paper_exec_weights,
            current_weights=live_weights,
            prices=last_prices,
            account_value=live_account_value,
            rebal_date=date_str,
        )
        result.live_executed = True
        result.live_fills = fills
        result.live_cost = total_cost

        # Average slippage
        slips = [f.slippage_bps for f in fills if f.status == "FILLED"]
        result.avg_slippage_bps = sum(slips) / len(slips) if slips else 0.0

        # ── 4. Dual tracking ─────────────────────────────────────
        # Update paper side
        self.tracker.update_paper(
            self.paper_state.total_value,
            self.paper_state.weights(),
            paper_result.cost,
        )

        # Update live side (get fresh value from broker)
        live_value_after = live_account_value - total_cost  # Simplified
        # Build live weights after execution
        live_weights_after = dict(live_weights)
        for fill in fills:
            delta_w = (fill.filled_shares * fill.filled_price) / live_account_value
            if fill.side == "BUY":
                live_weights_after[fill.ticker] = live_weights_after.get(fill.ticker, 0.0) + delta_w
            else:
                live_weights_after[fill.ticker] = live_weights_after.get(fill.ticker, 0.0) - delta_w
                if live_weights_after[fill.ticker] < 0.001:
                    live_weights_after.pop(fill.ticker, None)

        self.tracker.update_live(live_value_after, live_weights_after, total_cost)
        result.live_value_after = live_value_after
        result.live_weights_after = live_weights_after
        result.return_divergence = self.tracker.return_divergence

        # Record trade comparisons (paper vs live)
        paper_prices = last_prices
        for fill in fills:
            self.tracker.record_trade_comparison(
                ticker=fill.ticker,
                side=fill.side,
                paper_shares=fill.intended_shares,
                paper_price=fill.intended_price,
                live_shares=fill.filled_shares,
                live_price=fill.filled_price,
                cost_diff=fill.cost,
            )

        self.tracker.take_snapshot(date_str)

        # ── 5. Post-trade alerts ──────────────────────────────────
        positions_pnl: Dict[str, float] = {}
        for fill in fills:
            if fill.intended_price > 0:
                pnl_pct = (fill.filled_price - fill.intended_price) / fill.intended_price
                positions_pnl[fill.ticker] = pnl_pct

        run_all_alerts(
            current_value=live_value_after,
            peak_value=self.tracker.peak_live_value,
            paper_return=self.tracker.paper_return,
            live_return=self.tracker.live_return,
            paper_weights=paper_exec_weights,
            live_weights=live_weights_after,
            positions_pnl=positions_pnl,
            cfg=self.cfg,
            collector=self.alerts,
        )
        result.alerts = self.alerts.summary()

        # ── 6. Journal ───────────────────────────────────────────
        self._write_journal(result, date_str)

        logger.info(
            "[LIVE_ENGINE] %s | paper=$%.0f | live=$%.0f | divergence=%.2f%% | fills=%d | cost=$%.2f",
            date_str, self.paper_state.total_value, live_value_after,
            self.tracker.return_divergence * 100, len(fills), total_cost,
        )
        return result

    def _write_journal(self, result: LiveRebalanceResult, date_str: str) -> None:
        entry = LiveRebalanceEntry(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            rebal_date=date_str,
            mode=result.mode,
            paper_weights=result.paper_exec_weights,
            live_weights_before={},
            live_weights_after=result.live_weights_after,
            target_weights=result.paper_target_weights,
            orders_count=len(result.live_fills),
            orders_filled=sum(1 for f in result.live_fills if f.status == "FILLED"),
            orders_failed=sum(1 for f in result.live_fills if f.status != "FILLED"),
            total_cost=result.live_cost,
            total_slippage_bps=result.avg_slippage_bps,
            turnover=result.paper_turnover,
            portfolio_value_before=0.0,
            portfolio_value_after=result.live_value_after,
            risk_guard_status="PASSED" if result.risk_guard_passed else "BLOCKED",
            alerts=result.alerts,
            blocked=not result.live_executed,
            block_reasons=result.risk_guard_blockers if not result.risk_guard_passed else result.paper_block_reasons,
        )
        self.live_journal.append_rebalance(entry)
