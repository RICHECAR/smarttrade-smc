"""
Learning Engine — Quality scoring from institutional trade journal.
══════════════════════════════════════════════════════════════════

Reads the trade journal, computes per-(strategy, regime) quality scores,
and produces actionable decisions: ENABLE / REDUCE / DISABLE.

Quality Score formula:
  qs = w_exp * expectancy_norm
     + w_pf  * profit_factor_norm
     - w_dd  * drawdown_penalty
     - w_out * outlier_penalty
     + w_con * consistency_bonus

All components normalized to [0..1].  Final score clamped to [0..100].

Author: Chantier 6 — Learning Loop
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from smc_futures.analytics.trade_journal import TradeRecord
from smc_futures.analytics.trade_report import TradeReport, GroupStats

logger = logging.getLogger("smarttrade.analytics.learning")

# ═════════════════════════════════════════════════════════════════════════════
# CONFIG DEFAULTS
# ═════════════════════════════════════════════════════════════════════════════

_DEFAULTS = dict(
    # Minimum trades before any scoring decision
    MIN_TRADES=30,
    # Quality score thresholds
    THRESHOLD_DISABLE=40.0,     # Below → disable
    THRESHOLD_REDUCE=55.0,      # Below → reduce size
    THRESHOLD_FULL=70.0,        # Above → full allocation
    THRESHOLD_BOOST=80.0,       # Above → boost allocation
    # Score weights (sum = 1.0)
    W_EXPECTANCY=0.30,
    W_PROFIT_FACTOR=0.25,
    W_DRAWDOWN=0.15,
    W_OUTLIER=0.15,
    W_CONSISTENCY=0.15,
    # Normalization caps
    EXPECTANCY_CAP=20.0,        # USD; expectancy above this → 1.0
    PF_CAP=3.0,                 # Profit factor above this → 1.0
    DD_CAP=20.0,                # Max DD % above this → full penalty
    OUTLIER_CAP=0.10,           # Outlier rate above 10% → full penalty
    # Allocation multipliers
    MULT_DISABLE=0.0,
    MULT_REDUCE=0.5,
    MULT_FULL=1.0,
    MULT_BOOST=1.3,
    BOOST_MAX=1.5,
    # Safety
    COOLDOWN_CYCLES=5,          # Cycles after disable before re-enable check
    MAX_DISABLED_RATIO=0.7,     # Never disable more than 70% of strategies
)


# ═════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class QualityScore:
    """Computed quality score for a (strategy, regime) pair."""
    strategy: str
    regime: str                 # "*" = all regimes combined
    score: float                # 0..100
    expectancy: Optional[float]
    profit_factor: Optional[float]
    win_rate: Optional[float]
    payoff_ratio: Optional[float]
    outlier_loss_rate: Optional[float]
    avg_mae: Optional[float]
    avg_mfe: Optional[float]
    avg_slippage_entry_bps: Optional[float]
    total_trades: int
    gross_pnl: float
    # Component breakdown
    exp_component: float = 0.0
    pf_component: float = 0.0
    dd_penalty: float = 0.0
    outlier_penalty: float = 0.0
    consistency_bonus: float = 0.0


class Decision:
    """Allocation decision for a (strategy, regime) pair."""
    DISABLE = "DISABLE"
    REDUCE = "REDUCE"
    FULL = "FULL"
    BOOST = "BOOST"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass
class AllocationDecision:
    """Decision + metadata."""
    strategy: str
    regime: str
    decision: str               # Decision.XXX
    multiplier: float           # 0.0 .. 1.5
    quality_score: float        # 0..100
    total_trades: int
    reason: str
    cooldown_remaining: int = 0


# ═════════════════════════════════════════════════════════════════════════════
# LEARNING ENGINE
# ═════════════════════════════════════════════════════════════════════════════

class LearningEngine:
    """
    Reads trade journal records, scores strategies, produces allocation decisions.

    Thread-safe (stateless computation on a snapshot of records).
    """

    def __init__(self, config: Optional[Dict] = None):
        cfg = config or {}
        self.min_trades = int(cfg.get("LEARNING_MIN_TRADES", _DEFAULTS["MIN_TRADES"]))
        self.threshold_disable = float(cfg.get("LEARNING_THRESHOLD_DISABLE", _DEFAULTS["THRESHOLD_DISABLE"]))
        self.threshold_reduce = float(cfg.get("LEARNING_THRESHOLD_REDUCE", _DEFAULTS["THRESHOLD_REDUCE"]))
        self.threshold_full = float(cfg.get("LEARNING_THRESHOLD_FULL", _DEFAULTS["THRESHOLD_FULL"]))
        self.threshold_boost = float(cfg.get("LEARNING_THRESHOLD_BOOST", _DEFAULTS["THRESHOLD_BOOST"]))
        self.w_exp = float(cfg.get("LEARNING_W_EXPECTANCY", _DEFAULTS["W_EXPECTANCY"]))
        self.w_pf = float(cfg.get("LEARNING_W_PROFIT_FACTOR", _DEFAULTS["W_PROFIT_FACTOR"]))
        self.w_dd = float(cfg.get("LEARNING_W_DRAWDOWN", _DEFAULTS["W_DRAWDOWN"]))
        self.w_out = float(cfg.get("LEARNING_W_OUTLIER", _DEFAULTS["W_OUTLIER"]))
        self.w_con = float(cfg.get("LEARNING_W_CONSISTENCY", _DEFAULTS["W_CONSISTENCY"]))
        self.expectancy_cap = float(cfg.get("LEARNING_EXPECTANCY_CAP", _DEFAULTS["EXPECTANCY_CAP"]))
        self.pf_cap = float(cfg.get("LEARNING_PF_CAP", _DEFAULTS["PF_CAP"]))
        self.dd_cap = float(cfg.get("LEARNING_DD_CAP", _DEFAULTS["DD_CAP"]))
        self.outlier_cap = float(cfg.get("LEARNING_OUTLIER_CAP", _DEFAULTS["OUTLIER_CAP"]))
        self.mult_disable = float(cfg.get("LEARNING_MULT_DISABLE", _DEFAULTS["MULT_DISABLE"]))
        self.mult_reduce = float(cfg.get("LEARNING_MULT_REDUCE", _DEFAULTS["MULT_REDUCE"]))
        self.mult_full = float(cfg.get("LEARNING_MULT_FULL", _DEFAULTS["MULT_FULL"]))
        self.mult_boost = float(cfg.get("LEARNING_MULT_BOOST", _DEFAULTS["MULT_BOOST"]))
        self.boost_max = float(cfg.get("LEARNING_BOOST_MAX", _DEFAULTS["BOOST_MAX"]))
        self.max_disabled_ratio = float(cfg.get("LEARNING_MAX_DISABLED_RATIO", _DEFAULTS["MAX_DISABLED_RATIO"]))
        self.cooldown_cycles = int(cfg.get("LEARNING_COOLDOWN_CYCLES", _DEFAULTS["COOLDOWN_CYCLES"]))

        # Persistent cooldown tracking {(strategy, regime): remaining_cycles}
        self._cooldowns: Dict[Tuple[str, str], int] = {}

    # ── Public API ───────────────────────────────────────────────────────────

    def compute_scores(
        self,
        records: Sequence[TradeRecord],
    ) -> Dict[Tuple[str, str], QualityScore]:
        """
        Compute quality scores for all (strategy, regime) pairs.

        Returns:
            {(strategy, regime): QualityScore}
            Also includes (strategy, "*") for the aggregate.
        """
        scores: Dict[Tuple[str, str], QualityScore] = {}

        # 1. Per-strategy aggregate (all regimes)
        report = TradeReport(records)
        for key, gs in report.by_strategy().items():
            qs = self._score_group(key, "*", gs)
            scores[(key, "*")] = qs

        # 2. Per-(strategy, regime) cross
        strat_regime_groups = self._group_by_strat_regime(records)
        for (strat, regime), recs in strat_regime_groups.items():
            subreport = TradeReport(recs)
            gs = subreport.by_strategy().get(strat)
            if gs is None:
                # Fallback: compute as overall
                overall = subreport.full_report()["overall"]
                gs = _dict_to_groupstats(strat, overall)
            qs = self._score_group(strat, regime, gs)
            scores[(strat, regime)] = qs

        return scores

    def make_decisions(
        self,
        scores: Dict[Tuple[str, str], QualityScore],
        known_strategies: Optional[Sequence[str]] = None,
    ) -> Dict[Tuple[str, str], AllocationDecision]:
        """
        Convert quality scores into allocation decisions.

        Safety: never disables more than max_disabled_ratio of strategies.
        """
        decisions: Dict[Tuple[str, str], AllocationDecision] = {}

        # Tick cooldowns
        for k in list(self._cooldowns.keys()):
            if self._cooldowns[k] > 0:
                self._cooldowns[k] -= 1
            if self._cooldowns[k] <= 0:
                del self._cooldowns[k]

        # First pass: compute raw decisions
        for (strat, regime), qs in scores.items():
            if qs.total_trades < self.min_trades:
                decisions[(strat, regime)] = AllocationDecision(
                    strategy=strat,
                    regime=regime,
                    decision=Decision.INSUFFICIENT_DATA,
                    multiplier=self.mult_full,
                    quality_score=qs.score,
                    total_trades=qs.total_trades,
                    reason=f"only {qs.total_trades} trades (min={self.min_trades})",
                )
                continue

            # Check cooldown
            cd = self._cooldowns.get((strat, regime), 0)
            if cd > 0:
                decisions[(strat, regime)] = AllocationDecision(
                    strategy=strat,
                    regime=regime,
                    decision=Decision.REDUCE,
                    multiplier=self.mult_reduce,
                    quality_score=qs.score,
                    total_trades=qs.total_trades,
                    reason=f"cooldown remaining: {cd} cycles",
                    cooldown_remaining=cd,
                )
                continue

            decision, multiplier, reason = self._classify(qs)
            decisions[(strat, regime)] = AllocationDecision(
                strategy=strat,
                regime=regime,
                decision=decision,
                multiplier=multiplier,
                quality_score=qs.score,
                total_trades=qs.total_trades,
                reason=reason,
            )

        # Safety: enforce max disabled ratio (on aggregate "*" decisions only)
        agg_decisions = {k: v for k, v in decisions.items() if k[1] == "*"}
        all_strats = known_strategies or list({k[0] for k in scores.keys()})
        n_total = len(all_strats) if all_strats else 1
        max_disabled = max(1, int(n_total * self.max_disabled_ratio))
        # But always keep at least 1 strategy active
        max_disabled = min(max_disabled, n_total - 1) if n_total > 1 else 0

        disabled_count = sum(
            1 for v in agg_decisions.values() if v.decision == Decision.DISABLE
        )
        if disabled_count > max_disabled:
            # Sort disabled by quality score descending → re-enable best ones
            disabled_list = sorted(
                [(k, v) for k, v in agg_decisions.items() if v.decision == Decision.DISABLE],
                key=lambda x: x[1].quality_score,
                reverse=True,
            )
            for k, v in disabled_list[:disabled_count - max_disabled]:
                decisions[k] = AllocationDecision(
                    strategy=v.strategy,
                    regime=v.regime,
                    decision=Decision.REDUCE,
                    multiplier=self.mult_reduce,
                    quality_score=v.quality_score,
                    total_trades=v.total_trades,
                    reason=f"safety override: max {max_disabled}/{n_total} disabled, forced REDUCE",
                )

        # Start cooldowns for newly disabled
        for (strat, regime), dec in decisions.items():
            if dec.decision == Decision.DISABLE:
                self._cooldowns[(strat, regime)] = self.cooldown_cycles

        return decisions

    # ── Internal ─────────────────────────────────────────────────────────────

    def _score_group(self, strategy: str, regime: str, gs: GroupStats) -> QualityScore:
        """Compute quality score from GroupStats."""
        n = gs.total_trades or 1

        # ── Expectancy component [−1..1] ──
        exp_raw = gs.expectancy
        if exp_raw is None:
            exp_raw = gs.gross_pnl / n if n > 0 else 0.0
        exp_norm = max(-1.0, min(1.0, exp_raw / self.expectancy_cap))

        # ── Profit factor component [−1..1], centered at PF=1.0 ──
        pf_raw = gs.payoff_ratio
        if pf_raw is None:
            if gs.losses == 0 and gs.wins > 0:
                pf_raw = self.pf_cap
            else:
                pf_raw = 0.0
        if pf_raw >= 1.0:
            pf_norm = min((pf_raw - 1.0) / (self.pf_cap - 1.0), 1.0)
        else:
            pf_norm = max(pf_raw - 1.0, -1.0)  # PF<1 → negative

        # ── Drawdown penalty [0..1] ──
        worst = gs.worst_trade_pnl or 0.0
        if worst < 0 and gs.gross_pnl != 0:
            dd_pct = abs(worst) / max(abs(gs.gross_pnl), 1.0) * 100.0
        else:
            dd_pct = 0.0
        dd_penalty = min(dd_pct / self.dd_cap, 1.0)

        # ── Outlier penalty [0..1] ──
        outlier_rate = gs.outlier_loss_rate or 0.0
        outlier_penalty = min(outlier_rate / self.outlier_cap, 1.0)

        # ── Consistency bonus [0..1] ── (win_rate × (1 − outlier_rate))
        wr = gs.win_rate or 0.0
        consistency = wr * (1.0 - outlier_rate)

        # ── Weighted sum ──
        raw = (
            self.w_exp * exp_norm
            + self.w_pf * pf_norm
            - self.w_dd * dd_penalty
            - self.w_out * outlier_penalty
            + self.w_con * consistency
        )
        # raw range: [-0.85, +0.70].  Scale to 0..100
        score = max(0.0, min(100.0, (raw + 0.85) / 1.55 * 100.0))

        return QualityScore(
            strategy=strategy,
            regime=regime,
            score=round(score, 2),
            expectancy=gs.expectancy,
            profit_factor=gs.payoff_ratio,
            win_rate=gs.win_rate,
            payoff_ratio=gs.payoff_ratio,
            outlier_loss_rate=gs.outlier_loss_rate,
            avg_mae=gs.avg_mae,
            avg_mfe=gs.avg_mfe,
            avg_slippage_entry_bps=gs.avg_slippage_entry_bps,
            total_trades=gs.total_trades,
            gross_pnl=gs.gross_pnl,
            exp_component=round(exp_norm, 4),
            pf_component=round(pf_norm, 4),
            dd_penalty=round(dd_penalty, 4),
            outlier_penalty=round(outlier_penalty, 4),
            consistency_bonus=round(consistency, 4),
        )

    def _classify(self, qs: QualityScore) -> Tuple[str, float, str]:
        """Map quality score to decision."""
        s = qs.score
        if s < self.threshold_disable:
            return (
                Decision.DISABLE,
                self.mult_disable,
                f"score {s:.1f} < {self.threshold_disable} → DISABLE",
            )
        elif s < self.threshold_reduce:
            return (
                Decision.REDUCE,
                self.mult_reduce,
                f"score {s:.1f} < {self.threshold_reduce} → REDUCE",
            )
        elif s < self.threshold_boost:
            return (
                Decision.FULL,
                self.mult_full,
                f"score {s:.1f} ≥ {self.threshold_full} → FULL",
            )
        else:
            # Boost: scale multiplier by how far above threshold
            overshoot = (s - self.threshold_boost) / (100.0 - self.threshold_boost)
            mult = self.mult_boost + overshoot * (self.boost_max - self.mult_boost)
            mult = min(mult, self.boost_max)
            return (
                Decision.BOOST,
                round(mult, 3),
                f"score {s:.1f} ≥ {self.threshold_boost} → BOOST ×{mult:.2f}",
            )

    def _group_by_strat_regime(
        self, records: Sequence[TradeRecord],
    ) -> Dict[Tuple[str, str], List[TradeRecord]]:
        """Group records by (strategy_id, regime_at_entry)."""
        groups: Dict[Tuple[str, str], List[TradeRecord]] = {}
        for r in records:
            strat = r.strategy_id or "unknown"
            regime = r.regime_at_entry or "unknown"
            key = (strat, regime)
            if key not in groups:
                groups[key] = []
            groups[key].append(r)
        return groups


def _dict_to_groupstats(key: str, d: dict) -> GroupStats:
    """Build a GroupStats from a report dict."""
    return GroupStats(
        key=key,
        total_trades=d.get("total_trades", 0),
        wins=d.get("wins", 0),
        losses=d.get("losses", 0),
        breakeven=d.get("breakeven", 0),
        gross_pnl=d.get("gross_pnl", 0.0),
        net_pnl=d.get("net_pnl", 0.0),
        total_fees=d.get("total_fees", 0.0),
        win_rate=d.get("win_rate"),
        avg_win=d.get("avg_win"),
        avg_loss=d.get("avg_loss"),
        payoff_ratio=d.get("payoff_ratio"),
        expectancy=d.get("expectancy"),
        avg_slippage_entry_bps=d.get("avg_slippage_entry_bps"),
        avg_slippage_exit_bps=d.get("avg_slippage_exit_bps"),
        avg_holding_sec=d.get("avg_holding_sec"),
        avg_mae=d.get("avg_mae"),
        avg_mfe=d.get("avg_mfe"),
        outlier_loss_rate=d.get("outlier_loss_rate"),
        best_trade_pnl=d.get("best_trade_pnl"),
        worst_trade_pnl=d.get("worst_trade_pnl"),
    )
