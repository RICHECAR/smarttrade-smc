"""
Paper Engine — Strict monthly rebalance pipeline.
═══════════════════════════════════════════════════
Pipeline: data_check → score → safeguards → constraint_gate
          → rebalance_plan → paper_execution → journal → reporting

Supports:
  - Single rebalance step (for live monthly cron)
  - Sequential replay over historical dates (for validation)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from portfolio.features import compute_all_features, FeatureConfig
from portfolio.signals import compute_composite_score, SignalConfig
from portfolio.allocator import compute_allocation, AllocatorConfig, WeightMethod
from portfolio.safeguards import run_all_safeguards, SafeguardConfig, SafeguardResult
from portfolio.paper_constraints import (
    PaperConstraints,
    ConstraintGateResult,
    run_constraint_gate,
)
from portfolio.paper_state import PaperPortfolioState
from portfolio.paper_broker import PaperBroker
from portfolio.paper_journal import PaperJournal, JournalEntry

logger = logging.getLogger(__name__)


# ── Validated config lock (score_top5_monthly) ───────────────────

VALIDATED_SIGNAL_CONFIG = SignalConfig(weights={
    "mom_21":       0.10,
    "mom_63":       0.25,
    "mom_126":      0.20,
    "volatility":  -0.15,
    "drawdown":    -0.10,
    "trend_r2":     0.10,
    "rel_strength": 0.10,
})

VALIDATED_ALLOCATOR_CONFIG = AllocatorConfig(
    top_n=5,
    max_weight=0.30,
    method=WeightMethod.SCORE_WEIGHTED,
)

VALIDATED_FEATURE_CONFIG = FeatureConfig()


@dataclass
class RebalanceResult:
    """Result of one rebalance step."""
    date: str
    passed: bool = False
    blocked: bool = False
    block_reasons: List[str] = field(default_factory=list)
    target_weights: Dict[str, float] = field(default_factory=dict)
    old_weights: Dict[str, float] = field(default_factory=dict)
    turnover: float = 0.0
    cost: float = 0.0
    portfolio_value: float = 0.0
    order_count: int = 0
    safeguard_result: Optional[SafeguardResult] = None
    constraint_result: Optional[ConstraintGateResult] = None


class PaperEngine:
    """Orchestrates the paper trading pipeline."""

    def __init__(
        self,
        state: PaperPortfolioState,
        journal: PaperJournal,
        constraints: PaperConstraints = None,
        broker: PaperBroker = None,
    ):
        self.state = state
        self.journal = journal
        self.constraints = constraints or PaperConstraints()
        self.broker = broker or PaperBroker(cost_bps=self.constraints.cost_bps)
        self.block_counts: Dict[str, int] = {}

    def _record_block(self, reasons: List[str]) -> None:
        for r in reasons:
            self.block_counts[r] = self.block_counts.get(r, 0) + 1

    # ══════════════════════════════════════════════════════════════
    # CORE: single rebalance step
    # ══════════════════════════════════════════════════════════════

    def rebalance_step(
        self,
        prices: pd.DataFrame,
        rebal_date: pd.Timestamp,
        benchmark_ticker: str = "SPY",
    ) -> RebalanceResult:
        """
        Execute one monthly rebalance.
        
        Pipeline:
          1. data_check
          2. compute features + scores
          3. compute target allocation
          4. run safeguards
          5. run constraint gate
          6. paper execution
          7. journal write
        """
        date_str = rebal_date.strftime("%Y-%m-%d")
        result = RebalanceResult(date=date_str)

        # ── 0. Ensure prices up to rebal_date ─────────────────────
        prices_up_to = prices.loc[:rebal_date]
        if prices_up_to.empty or len(prices_up_to) < 252:
            result.blocked = True
            result.block_reasons = ["PAPER_DATA_PARTIAL"]
            self._record_block(result.block_reasons)
            self._write_blocked_journal(date_str, result, benchmark_ticker, prices_up_to)
            return result

        # ── 1. Data check ─────────────────────────────────────────
        # Update current prices in state
        last_prices = prices_up_to.iloc[-1].to_dict()
        self.state.update_prices(last_prices)
        old_weights = self.state.weights()
        result.old_weights = dict(old_weights)
        result.portfolio_value = self.state.total_value

        # ── 2. Features + Scores ──────────────────────────────────
        features = compute_all_features(prices_up_to, VALIDATED_FEATURE_CONFIG)
        composite = compute_composite_score(features, VALIDATED_SIGNAL_CONFIG)

        if rebal_date not in composite.index:
            # find closest date
            valid_dates = composite.index[composite.index <= rebal_date]
            if valid_dates.empty:
                result.blocked = True
                result.block_reasons = ["PAPER_SCORE_NAN"]
                self._record_block(result.block_reasons)
                self._write_blocked_journal(date_str, result, benchmark_ticker, prices_up_to)
                return result
            rebal_date_eff = valid_dates[-1]
        else:
            rebal_date_eff = rebal_date

        scores = composite.loc[rebal_date_eff].dropna()
        top_rankings = scores.nlargest(self.constraints.top_n).to_dict()

        # ── 3. Compute target allocation ──────────────────────────
        vol_series = features.get("volatility", pd.DataFrame())
        vol_at_date = None
        if not vol_series.empty and rebal_date_eff in vol_series.index:
            vol_at_date = vol_series.loc[rebal_date_eff]

        target_weights = compute_allocation(
            scores, vol_at_date, VALIDATED_ALLOCATOR_CONFIG,
        )
        result.target_weights = dict(target_weights)

        # ── 4. Safeguards ─────────────────────────────────────────
        sg_result = run_all_safeguards(
            prices=prices_up_to,
            scores=scores,
            weights=target_weights,
            old_weights=old_weights,
            reference_date=rebal_date,
        )
        result.safeguard_result = sg_result

        # ── 5. Constraint gate ────────────────────────────────────
        cg_result = run_constraint_gate(
            rebal_freq="M",
            signal_weights=VALIDATED_SIGNAL_CONFIG.weights,
            prices=prices_up_to,
            reference_date=rebal_date,
            scores=scores,
            old_weights=old_weights,
            new_weights=target_weights,
            cfg=self.constraints,
        )
        result.constraint_result = cg_result

        # ── Block? (hard blockers only — turnover is soft-capped) ─
        # Filter out turnover blockers from safeguards: turnover is handled
        # by the constraint gate soft-cap, not as a hard block.
        all_blockers = list(cg_result.blockers)
        if not sg_result.passed:
            non_turnover_blockers = [
                b for b in sg_result.blockers
                if "Turnover" not in b
            ]
            all_blockers.extend(non_turnover_blockers)

        if all_blockers:
            result.blocked = True
            result.block_reasons = all_blockers
            self._record_block(all_blockers)
            logger.warning("[PAPER_ENGINE] BLOCKED %s: %s", date_str, all_blockers)
            self._write_blocked_journal(date_str, result, benchmark_ticker, prices_up_to)
            return result

        # ── 5b. Turnover capping (soft constraint) ────────────────
        from portfolio.paper_constraints import cap_turnover_weights
        exec_weights = cap_turnover_weights(
            old_weights, target_weights, self.constraints.max_turnover_per_rebal,
        )
        if exec_weights != target_weights:
            cg_result.warnings.append(
                f"Turnover capped from target → exec (cap={self.constraints.max_turnover_per_rebal:.0%})"
            )
            self._record_block(["PAPER_TURNOVER_CAPPED"])

        # ── 6. Paper execution ────────────────────────────────────
        orders, cost = self.broker.execute_rebalance(
            self.state, exec_weights, last_prices,
        )
        result.cost = cost
        result.order_count = len(orders)
        result.passed = True

        # Update state metadata
        self.state.last_rebal_date = date_str
        self.state.rebal_count += 1
        if not self.state.inception_date:
            self.state.inception_date = date_str

        # Turnover (actual, after capping)
        all_t = set(old_weights) | set(exec_weights)
        turnover = sum(abs(exec_weights.get(t, 0.0) - old_weights.get(t, 0.0)) for t in all_t) / 2.0
        result.turnover = turnover

        # Record valuation
        bench_val = 0.0
        if benchmark_ticker in prices_up_to.columns:
            bench_val = float(prices_up_to[benchmark_ticker].iloc[-1])
        self.state.record_valuation(date_str, bench_val)
        result.portfolio_value = self.state.total_value

        # ── 7. Journal ────────────────────────────────────────────
        delta = {t: exec_weights.get(t, 0.0) - old_weights.get(t, 0.0)
                 for t in all_t}
        entry = JournalEntry(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            rebal_date=date_str,
            universe_valid_count=int(scores.notna().sum()),
            top_rankings={k: round(v, 4) for k, v in top_rankings.items()},
            current_weights={k: round(v, 4) for k, v in old_weights.items()},
            target_weights={k: round(v, 4) for k, v in target_weights.items()},
            delta_by_ticker={k: round(v, 4) for k, v in delta.items()},
            cash_before=round(self.state.cash + cost, 2),
            cash_after=round(self.state.cash, 2),
            portfolio_value_before=round(result.portfolio_value + cost, 2),
            portfolio_value_after=round(self.state.total_value, 2),
            cost_simulated=round(cost, 4),
            turnover=round(turnover, 4),
            safeguard_status="PASSED",
            constraint_status=cg_result.summary(),
            benchmark_value=round(bench_val, 4),
            benchmark_ticker=benchmark_ticker,
        )
        self.journal.append(entry)

        logger.info(
            "[PAPER_ENGINE] REBALANCED %s | value=$%.0f | turnover=%.1f%% | cost=$%.2f",
            date_str, self.state.total_value, turnover * 100, cost,
        )
        return result

    def _write_blocked_journal(
        self,
        date_str: str,
        result: RebalanceResult,
        benchmark_ticker: str,
        prices: pd.DataFrame,
    ) -> None:
        bench_val = 0.0
        if benchmark_ticker in prices.columns and not prices.empty:
            bench_val = float(prices[benchmark_ticker].iloc[-1])

        entry = JournalEntry(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            rebal_date=date_str,
            universe_valid_count=0,
            top_rankings={},
            current_weights=result.old_weights,
            target_weights=result.target_weights,
            delta_by_ticker={},
            cash_before=round(self.state.cash, 2),
            cash_after=round(self.state.cash, 2),
            portfolio_value_before=round(self.state.total_value, 2),
            portfolio_value_after=round(self.state.total_value, 2),
            cost_simulated=0.0,
            turnover=0.0,
            safeguard_status=" | ".join(result.block_reasons),
            constraint_status=" | ".join(result.block_reasons),
            benchmark_value=round(bench_val, 4),
            benchmark_ticker=benchmark_ticker,
            blocked=True,
            block_reasons=result.block_reasons,
            notes="REBALANCE BLOCKED",
        )
        self.journal.append(entry)

    # ══════════════════════════════════════════════════════════════
    # REPLAY: walk through historical dates sequentially
    # ══════════════════════════════════════════════════════════════

    def replay(
        self,
        prices: pd.DataFrame,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        benchmark_ticker: str = "SPY",
        warmup: int = 252,
    ) -> List[RebalanceResult]:
        """
        Replay paper trading over historical monthly dates.
        
        - Sequential processing (no lookahead)
        - State carried across months
        - Daily valuations tracked between rebalance dates
        """
        dates = prices.index
        if start_date:
            dates = dates[dates >= pd.Timestamp(start_date)]
        if end_date:
            dates = dates[dates <= pd.Timestamp(end_date)]

        # Monthly rebalance dates (last trading day of each month)
        monthly = dates.to_series().groupby(dates.to_period("M"))
        rebal_dates = [g.iloc[-1] for _, g in monthly]

        # Skip warmup
        tradeable_start = prices.index[warmup] if len(prices.index) > warmup else prices.index[0]
        rebal_dates = [d for d in rebal_dates if d >= tradeable_start]

        if not self.state.inception_date:
            self.state.inception_date = rebal_dates[0].strftime("%Y-%m-%d") if rebal_dates else ""

        results = []
        all_dates = sorted(dates)

        # Walk day by day, rebalance at month-end
        rebal_set = set(rebal_dates)
        last_rebal_idx = 0

        for d in all_dates:
            if d < tradeable_start:
                continue

            # Daily price update + valuation
            day_prices = prices.loc[d].to_dict()
            self.state.update_prices(day_prices)
            bench_val = day_prices.get(benchmark_ticker, 0.0)
            if not np.isnan(bench_val):
                self.state.record_valuation(d.strftime("%Y-%m-%d"), bench_val)

            # Rebalance on month-end
            if d in rebal_set:
                # Use only data up to this date (no lookahead)
                prices_slice = prices.loc[:d]
                r = self.rebalance_step(prices_slice, d, benchmark_ticker)
                results.append(r)

        return results
