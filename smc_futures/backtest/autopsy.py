"""
Edge Autopsy — PnL Decomposition & False Edge Detection
════════════════════════════════════════════════════════
Chantier 9: Cold quantitative post-mortem.

1. PnL Decomposition per trade:
     ideal_alpha − entry_slippage − exit_slippage − fees − funding = net_pnl
   Reconciles to the penny (mathematical identity, not approximation).

2. Aggregate cost attribution: which cost kills the edge first?

3. False Edge Detector: scores 7 root-cause categories (0-100 each):
     look_ahead, fill_overestimation, spread_underestimation,
     overtrading, bad_sl_tp, wrong_regime, weak_alpha

4. Decision Matrix: KILL / SALVAGE / RESEARCH / PROMISING

5. Action Plan: binary abandon / salvage with conditions
"""

from __future__ import annotations

import math
import datetime
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple

from smc_futures.backtest.engine import RealisticTrade
from smc_futures.backtest.metrics import DetailedMetrics


# ───────────────────────────────────────────
#  Trade-Level PnL Decomposition
# ───────────────────────────────────────────

@dataclass
class TradeDecomposition:
    """PnL waterfall for one trade."""
    trade_idx: int = 0
    direction: str = ""
    setup_type: str = ""
    exit_reason: str = ""
    volatility_regime: str = ""

    # Components (USD)
    ideal_pnl: float = 0.0          # Alpha if entered/exited at reference prices
    entry_slippage: float = 0.0     # Cost of actual_entry vs intended_entry
    exit_slippage: float = 0.0      # Cost of actual_exit vs reference exit
    fees: float = 0.0               # entry_fee + exit_fee
    funding: float = 0.0            # Funding cost over holding period
    net_pnl: float = 0.0            # Final net PnL (from trade record)

    # Reconciliation
    recon_error: float = 0.0        # Should be ~0 (rounding only)

    # Relative (bps of notional)
    entry_slip_bps: float = 0.0
    exit_slip_bps: float = 0.0
    fees_bps: float = 0.0
    funding_bps: float = 0.0
    total_cost_bps: float = 0.0

    @property
    def total_cost(self) -> float:
        return self.entry_slippage + self.exit_slippage + self.fees + self.funding

    @property
    def breakeven_alpha_bps(self) -> float:
        """Minimum alpha (bps) needed to break even after costs."""
        return self.total_cost_bps


# ───────────────────────────────────────────
#  Aggregate Cost Attribution
# ───────────────────────────────────────────

@dataclass
class CostAttribution:
    """Aggregate cost breakdown across all trades."""
    total_trades: int = 0

    # Total USD
    total_ideal_pnl: float = 0.0
    total_entry_slippage: float = 0.0
    total_exit_slippage: float = 0.0
    total_fees: float = 0.0
    total_funding: float = 0.0
    total_net_pnl: float = 0.0
    total_cost: float = 0.0

    # Per-trade averages
    avg_ideal_pnl: float = 0.0
    avg_entry_slippage: float = 0.0
    avg_exit_slippage: float = 0.0
    avg_fees: float = 0.0
    avg_funding: float = 0.0
    avg_net_pnl: float = 0.0
    avg_total_cost: float = 0.0

    # Cost share (% of total_cost)
    entry_slip_pct: float = 0.0
    exit_slip_pct: float = 0.0
    fees_pct: float = 0.0
    funding_pct: float = 0.0

    # Kill ranking (1=biggest killer)
    kill_ranking: List[Tuple[str, float, float]] = field(default_factory=list)

    # Key ratios
    cost_to_alpha_ratio: float = 0.0     # total_cost / |total_ideal_pnl|
    alpha_survival_pct: float = 0.0      # net_pnl / ideal_pnl (how much alpha survives)
    breakeven_alpha_bps: float = 0.0     # Avg bps needed per trade to break even

    # Reconciliation
    max_recon_error: float = 0.0

    # Per-component detail
    decompositions: List[TradeDecomposition] = field(default_factory=list)


# ───────────────────────────────────────────
#  False Edge Detector
# ───────────────────────────────────────────

@dataclass
class FalseEdgeScore:
    """Root-cause scoring (0-100 each, higher = bigger problem)."""
    look_ahead: float = 0.0             # Engine prevents this, but sanity check
    fill_overestimation: float = 0.0    # How much naive overestimates fills
    spread_underestimation: float = 0.0 # Spread impact vs expectation
    overtrading: float = 0.0            # Too many low-quality signals
    bad_sl_tp: float = 0.0              # SL/TP placement quality
    wrong_regime: float = 0.0           # Trading in wrong volatility regimes
    weak_alpha: float = 0.0             # Alpha too small to survive costs

    # Detail strings
    details: Dict[str, str] = field(default_factory=dict)

    @property
    def total_score(self) -> float:
        return (self.look_ahead + self.fill_overestimation +
                self.spread_underestimation + self.overtrading +
                self.bad_sl_tp + self.wrong_regime + self.weak_alpha)

    @property
    def primary_cause(self) -> str:
        scores = {
            "look_ahead": self.look_ahead,
            "fill_overestimation": self.fill_overestimation,
            "spread_underestimation": self.spread_underestimation,
            "overtrading": self.overtrading,
            "bad_sl_tp": self.bad_sl_tp,
            "wrong_regime": self.wrong_regime,
            "weak_alpha": self.weak_alpha,
        }
        return max(scores, key=scores.get)


# ───────────────────────────────────────────
#  Decision Matrix
# ───────────────────────────────────────────

@dataclass
class DecisionVerdict:
    """Final classification: KILL / SALVAGE / RESEARCH / PROMISING."""
    verdict: str = "KILL"               # KILL / SALVAGE / RESEARCH / PROMISING
    confidence: str = "HIGH"            # LOW / MEDIUM / HIGH
    reasons: List[str] = field(default_factory=list)
    conditions: List[str] = field(default_factory=list)  # If SALVAGE/RESEARCH
    action_plan: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════
#  PNL DECOMPOSER
# ═══════════════════════════════════════════

class PnLDecomposer:
    """Decompose each trade into ideal_alpha − costs = net_pnl."""

    @staticmethod
    def decompose_trade(t: RealisticTrade, idx: int = 0) -> TradeDecomposition:
        """
        Exact PnL decomposition for one trade.

        Identity: ideal_pnl − entry_slip − exit_slip − fees − funding = net_pnl
        """
        d = TradeDecomposition(
            trade_idx=idx,
            direction=t.direction,
            setup_type=t.setup_type,
            exit_reason=t.exit_reason,
            volatility_regime=t.volatility_regime,
        )

        dir_sign = 1.0 if t.direction == "long" else -1.0
        notional = t.notional_usd if t.notional_usd > 0 else t.qty * t.actual_entry

        # ── Reference exit price (before slippage) ────
        if t.exit_reason in ("sl", "tp"):
            reference_exit = t.intended_exit
        else:
            # Timeout/end_of_data: back-calculate unslipped price
            slip_frac = t.exit_slippage_bps / 10000 if t.exit_slippage_bps > 0 else 0
            if t.direction == "long":  # sold to close
                reference_exit = t.actual_exit / (1 - slip_frac) if slip_frac < 1 else t.actual_exit
            else:  # bought to close
                reference_exit = t.actual_exit / (1 + slip_frac) if slip_frac > 0 else t.actual_exit

        # ── Ideal PnL (perfect execution) ────
        d.ideal_pnl = dir_sign * (reference_exit - t.intended_entry) * t.qty

        # ── Entry slippage cost ────
        d.entry_slippage = dir_sign * (t.actual_entry - t.intended_entry) * t.qty

        # ── Exit slippage cost ────
        d.exit_slippage = dir_sign * (reference_exit - t.actual_exit) * t.qty

        # ── Fees and funding ────
        d.fees = t.total_fees
        d.funding = t.funding_cost
        d.net_pnl = t.net_pnl

        # ── Reconciliation ────
        reconstructed = d.ideal_pnl - d.entry_slippage - d.exit_slippage - d.fees - d.funding
        d.recon_error = abs(reconstructed - t.net_pnl)

        # ── Basis points (relative to notional) ────
        if notional > 0:
            d.entry_slip_bps = d.entry_slippage / notional * 10000
            d.exit_slip_bps = d.exit_slippage / notional * 10000
            d.fees_bps = d.fees / notional * 10000
            d.funding_bps = d.funding / notional * 10000
            d.total_cost_bps = (d.entry_slippage + d.exit_slippage + d.fees + d.funding) / notional * 10000

        return d

    @classmethod
    def decompose_all(cls, trades: List[RealisticTrade]) -> CostAttribution:
        """Full cost attribution across all trades."""
        attr = CostAttribution(total_trades=len(trades))
        if not trades:
            return attr

        decomps = [cls.decompose_trade(t, i) for i, t in enumerate(trades)]
        attr.decompositions = decomps

        # ── Totals ────
        attr.total_ideal_pnl = sum(d.ideal_pnl for d in decomps)
        attr.total_entry_slippage = sum(d.entry_slippage for d in decomps)
        attr.total_exit_slippage = sum(d.exit_slippage for d in decomps)
        attr.total_fees = sum(d.fees for d in decomps)
        attr.total_funding = sum(d.funding for d in decomps)
        attr.total_net_pnl = sum(d.net_pnl for d in decomps)
        attr.total_cost = (attr.total_entry_slippage + attr.total_exit_slippage +
                           attr.total_fees + attr.total_funding)

        # ── Per-trade averages ────
        n = len(trades)
        attr.avg_ideal_pnl = attr.total_ideal_pnl / n
        attr.avg_entry_slippage = attr.total_entry_slippage / n
        attr.avg_exit_slippage = attr.total_exit_slippage / n
        attr.avg_fees = attr.total_fees / n
        attr.avg_funding = attr.total_funding / n
        attr.avg_net_pnl = attr.total_net_pnl / n
        attr.avg_total_cost = attr.total_cost / n

        # ── Cost shares ────
        if attr.total_cost > 0:
            attr.entry_slip_pct = attr.total_entry_slippage / attr.total_cost * 100
            attr.exit_slip_pct = attr.total_exit_slippage / attr.total_cost * 100
            attr.fees_pct = attr.total_fees / attr.total_cost * 100
            attr.funding_pct = attr.total_funding / attr.total_cost * 100

        # ── Kill ranking ────
        components = [
            ("entry_slippage", attr.total_entry_slippage, attr.entry_slip_pct),
            ("exit_slippage", attr.total_exit_slippage, attr.exit_slip_pct),
            ("fees", attr.total_fees, attr.fees_pct),
            ("funding", attr.total_funding, attr.funding_pct),
        ]
        attr.kill_ranking = sorted(components, key=lambda x: x[1], reverse=True)

        # ── Key ratios ────
        ideal_abs = abs(attr.total_ideal_pnl)
        if ideal_abs > 0:
            attr.cost_to_alpha_ratio = attr.total_cost / ideal_abs
            attr.alpha_survival_pct = attr.total_net_pnl / attr.total_ideal_pnl * 100

        # Average breakeven alpha
        cost_bps_list = [d.total_cost_bps for d in decomps]
        attr.breakeven_alpha_bps = sum(cost_bps_list) / n if cost_bps_list else 0

        # Max reconciliation error
        attr.max_recon_error = max(d.recon_error for d in decomps) if decomps else 0

        return attr


# ═══════════════════════════════════════════
#  FALSE EDGE DETECTOR
# ═══════════════════════════════════════════

class FalseEdgeDetector:
    """Score 7 root causes of false edges."""

    @staticmethod
    def analyze(
        trades: List[RealisticTrade],
        cost_attr: CostAttribution,
        realistic_metrics: Optional[DetailedMetrics] = None,
        naive_metrics: Optional[DetailedMetrics] = None,
    ) -> FalseEdgeScore:
        score = FalseEdgeScore()
        if not trades:
            return score

        decomps = cost_attr.decompositions
        n = len(trades)

        # ── 1. Look-Ahead Bias (0-100) ────
        # Engine prevents this by construction. Score is low unless something is suspicious.
        # Suspicious: win rate > 75% AND profit factor > 3 on realistic → possible data leak
        if realistic_metrics and realistic_metrics.win_rate > 75 and realistic_metrics.profit_factor > 3:
            score.look_ahead = 60.0
            score.details["look_ahead"] = (
                f"WR={realistic_metrics.win_rate:.1f}%, PF={realistic_metrics.profit_factor:.2f} "
                f"even with realistic costs → suspicious signal accuracy"
            )
        else:
            score.look_ahead = 0.0
            score.details["look_ahead"] = "No look-ahead detected (engine enforces closed-bar-only signals)"

        # ── 2. Fill Overestimation (0-100) ────
        # Compare naive vs realistic: how much does perfect fill assumption inflate results?
        if naive_metrics and realistic_metrics:
            naive_exp = naive_metrics.expectancy
            real_exp = realistic_metrics.expectancy
            if naive_exp > 0:
                degradation = (naive_exp - real_exp) / naive_exp * 100
                score.fill_overestimation = min(100, max(0, degradation))
                score.details["fill_overestimation"] = (
                    f"Naive exp=${naive_exp:.2f}, Realistic exp=${real_exp:.2f}, "
                    f"degradation={degradation:.1f}%"
                )
            elif naive_exp <= 0:
                score.fill_overestimation = 0
                score.details["fill_overestimation"] = "Naive already unprofitable — fills not the issue"
        else:
            # Use cost-to-alpha ratio as proxy
            if cost_attr.cost_to_alpha_ratio > 1:
                score.fill_overestimation = min(100, cost_attr.cost_to_alpha_ratio * 50)
            score.details["fill_overestimation"] = (
                f"Cost/Alpha ratio={cost_attr.cost_to_alpha_ratio:.2f} "
                f"(>1 means costs exceed alpha)"
            )

        # ── 3. Spread/Slippage Underestimation (0-100) ────
        # How much of total cost is slippage?
        slip_total = cost_attr.total_entry_slippage + cost_attr.total_exit_slippage
        if cost_attr.total_cost > 0:
            slip_share = slip_total / cost_attr.total_cost * 100
            score.spread_underestimation = min(100, slip_share * 1.2)
            score.details["spread_underestimation"] = (
                f"Slippage is {slip_share:.1f}% of total costs "
                f"(entry=${cost_attr.total_entry_slippage:.2f}, exit=${cost_attr.total_exit_slippage:.2f})"
            )
        else:
            score.spread_underestimation = 0
            score.details["spread_underestimation"] = "No costs detected"

        # ── 4. Overtrading (0-100) ────
        # Low confidence signals, too many trades, or negative expectancy with high frequency
        low_conf = sum(1 for t in trades if t.confidence < 0.5)
        low_conf_pct = low_conf / n * 100
        losing_trades = sum(1 for t in trades if t.net_pnl < 0)
        loss_pct = losing_trades / n * 100

        overtrade_score = 0.0
        if low_conf_pct > 50:
            overtrade_score += 30
        if loss_pct > 60:
            overtrade_score += 30
        # Avg holding too short → churning
        avg_hold = sum(t.holding_bars for t in trades) / n
        if avg_hold < 5:
            overtrade_score += 20
        # Too many signals per 1000 bars
        if trades:
            bar_span = max(1, trades[-1].exit_bar_idx - trades[0].entry_bar_idx)
            freq = n / bar_span * 1000
            if freq > 100:  # More than 1 trade per 10 bars
                overtrade_score += 20

        score.overtrading = min(100, overtrade_score)
        score.details["overtrading"] = (
            f"Low confidence: {low_conf_pct:.0f}%, "
            f"Loss rate: {loss_pct:.0f}%, "
            f"Avg hold: {avg_hold:.1f} bars"
        )

        # ── 5. Bad SL/TP Placement (0-100) ────
        sl_trades = [t for t in trades if t.exit_reason == "sl"]
        tp_trades = [t for t in trades if t.exit_reason == "tp"]
        timeout_trades = [t for t in trades if t.exit_reason in ("timeout", "end_of_data")]

        sl_pct = len(sl_trades) / n * 100 if n else 0
        tp_pct = len(tp_trades) / n * 100 if n else 0
        timeout_pct = len(timeout_trades) / n * 100 if n else 0

        bad_sl_score = 0.0
        if sl_pct > 65:
            bad_sl_score += 40  # SL hit too often → too tight
        if timeout_pct > 30:
            bad_sl_score += 30  # Too many timeouts → TP too ambitious
        # RR analysis
        rr_list = [t.rr_achieved for t in trades if t.rr_achieved != 0]
        avg_rr = sum(rr_list) / len(rr_list) if rr_list else 0
        if avg_rr < 0:
            bad_sl_score += 30  # Average RR negative → SL/TP ratio is bad

        # MAE/MFE ratio: if MFE >> MAE but still losing, SL is cutting winners
        winners_mae = [abs(t.mae) for t in tp_trades if t.mae != 0]
        winners_mfe = [t.mfe for t in tp_trades if t.mfe > 0]
        losers_mae = [abs(t.mae) for t in sl_trades if t.mae != 0]
        losers_mfe = [t.mfe for t in sl_trades if t.mfe > 0]
        if losers_mfe:
            avg_loser_mfe = sum(losers_mfe) / len(losers_mfe)
            avg_loser_mae = sum(losers_mae) / len(losers_mae) if losers_mae else 1
            if avg_loser_mfe > avg_loser_mae * 0.5 and len(losers_mfe) > 5:
                bad_sl_score += 20  # Losers had favorable excursion before SL → SL too tight

        score.bad_sl_tp = min(100, bad_sl_score)
        score.details["bad_sl_tp"] = (
            f"SL={sl_pct:.0f}%, TP={tp_pct:.0f}%, Timeout={timeout_pct:.0f}%, "
            f"Avg RR={avg_rr:.2f}"
        )

        # ── 6. Wrong Regime (0-100) ────
        regime_pnl: Dict[str, float] = {}
        regime_count: Dict[str, int] = {}
        for t in trades:
            r = t.volatility_regime or "unknown"
            regime_pnl[r] = regime_pnl.get(r, 0) + t.net_pnl
            regime_count[r] = regime_count.get(r, 0) + 1

        losing_regimes = [r for r, pnl in regime_pnl.items() if pnl < 0]
        losing_regime_trades = sum(regime_count.get(r, 0) for r in losing_regimes)
        losing_regime_pct = losing_regime_trades / n * 100 if n else 0

        wrong_regime_score = 0.0
        if losing_regime_pct > 70:
            wrong_regime_score = 80
        elif losing_regime_pct > 50:
            wrong_regime_score = 50
        elif losing_regime_pct > 30:
            wrong_regime_score = 25

        # Check if extreme volatility is killing us
        extreme_trades = [t for t in trades if t.volatility_regime in ("high", "extreme")]
        if extreme_trades:
            extreme_pnl = sum(t.net_pnl for t in extreme_trades)
            if extreme_pnl < 0 and abs(extreme_pnl) > abs(cost_attr.total_net_pnl) * 0.3:
                wrong_regime_score += 20

        score.wrong_regime = min(100, wrong_regime_score)
        regime_detail = ", ".join(f"{r}={pnl:+.2f}({regime_count[r]}t)" for r, pnl in regime_pnl.items())
        score.details["wrong_regime"] = f"Regime PnL: {regime_detail}"

        # ── 7. Weak Alpha (0-100) ────
        # Is the raw signal alpha strong enough to survive ANY costs?
        ideal_pnls = [d.ideal_pnl for d in decomps]
        positive_alpha = sum(1 for x in ideal_pnls if x > 0)
        alpha_wr = positive_alpha / n * 100 if n else 0

        avg_ideal = sum(ideal_pnls) / n if n else 0
        avg_cost = cost_attr.avg_total_cost

        weak_score = 0.0
        if alpha_wr < 50:
            weak_score += 40  # Less than half have positive ideal PnL
        if avg_ideal <= 0:
            weak_score += 40  # Average ideal PnL negative → no alpha at all
        elif avg_ideal < avg_cost:
            weak_score += 30  # Alpha exists but smaller than costs
        elif avg_ideal < avg_cost * 2:
            weak_score += 15  # Alpha barely covers costs

        # Check if alpha is mean-reverting (sum of ideal small, but variance large)
        if n > 1:
            ideal_std = math.sqrt(sum((x - avg_ideal) ** 2 for x in ideal_pnls) / (n - 1))
            if ideal_std > 0 and abs(avg_ideal) / ideal_std < 0.3:
                weak_score += 20  # Very noisy alpha, near-zero signal-to-noise

        score.weak_alpha = min(100, weak_score)
        score.details["weak_alpha"] = (
            f"Ideal WR={alpha_wr:.1f}%, Avg ideal=${avg_ideal:.2f}, "
            f"Avg cost=${avg_cost:.2f}, "
            f"Alpha covers {avg_ideal / avg_cost * 100:.0f}% of costs" if avg_cost > 0 else
            f"Ideal WR={alpha_wr:.1f}%, Avg ideal=${avg_ideal:.2f}"
        )

        return score


# ═══════════════════════════════════════════
#  DECISION MATRIX
# ═══════════════════════════════════════════

class DecisionMatrix:
    """Classify strategy: KILL / SALVAGE / RESEARCH / PROMISING."""

    @staticmethod
    def evaluate(
        cost_attr: CostAttribution,
        false_edge: FalseEdgeScore,
        realistic_metrics: Optional[DetailedMetrics] = None,
        naive_metrics: Optional[DetailedMetrics] = None,
        segment_survivors: int = 0,        # Number of profitable segments found
        filter_survivors: int = 0,         # Number of filters that improve expectancy
    ) -> DecisionVerdict:
        v = DecisionVerdict()

        # ── Gather evidence ────
        has_alpha = cost_attr.total_ideal_pnl > 0
        alpha_covers_costs = cost_attr.alpha_survival_pct > 0
        cost_ratio = cost_attr.cost_to_alpha_ratio
        primary_cause = false_edge.primary_cause
        false_edge_total = false_edge.total_score

        naive_profitable = naive_metrics and naive_metrics.expectancy > 0 if naive_metrics else False
        real_profitable = realistic_metrics and realistic_metrics.expectancy > 0 if realistic_metrics else False

        # ── Decision Logic ────

        # PROMISING: very rare — realistic edge actually survives
        if real_profitable and has_alpha and cost_ratio < 0.5:
            v.verdict = "PROMISING"
            v.confidence = "MEDIUM"
            v.reasons.append("Realistic expectancy > 0 with cost ratio < 50%")
            if segment_survivors > 0:
                v.reasons.append(f"{segment_survivors} profitable segment(s) identified")
            v.action_plan = [
                "Walk-forward with real market data (not synthetic)",
                "Paper trade for 30 days minimum",
                "Monitor cost assumptions vs real fills",
                "Set kill switch at 2x expected max drawdown",
            ]
            return v

        # RESEARCH: alpha exists but crushed by costs, with pockets of profit
        if has_alpha and (segment_survivors >= 2 or filter_survivors >= 2):
            v.verdict = "RESEARCH"
            v.confidence = "MEDIUM"
            v.reasons.append(f"Ideal alpha is positive (${cost_attr.total_ideal_pnl:.2f})")
            v.reasons.append(f"But costs consume {cost_ratio * 100:.0f}% of alpha")
            v.reasons.append(f"{segment_survivors} seg + {filter_survivors} filter survivors")
            v.conditions = [
                f"Fix primary cause: {primary_cause} (score={getattr(false_edge, primary_cause):.0f})",
                "Reduce trading frequency to highest-conviction signals only",
                "Focus on regimes where alpha exceeds costs",
                "Re-test with limit orders (maker fees) where possible",
            ]
            v.action_plan = [
                "Restrict to profitable segments only",
                "Implement survival filters identified in analysis",
                "Re-run backtest on filtered subset: target PF > 1.2",
                "If still unprofitable after filters → KILL",
            ]
            return v

        # SALVAGE: naive profitable, some segments survive, but overall fails
        if naive_profitable and (segment_survivors >= 1 or filter_survivors >= 1) and false_edge_total < 450:
            v.verdict = "SALVAGE"
            v.confidence = "LOW"
            v.reasons.append("Naive backtest profitable but realistic fails")
            v.reasons.append(f"Primary cause: {primary_cause}")
            if segment_survivors:
                v.reasons.append(f"{segment_survivors} segment(s) might survive with filters")
            v.conditions = [
                "Must achieve PF > 1.2 after ALL realistic costs on filtered subset",
                "Must survive walk-forward on real data",
                "Max 3-month rebuild deadline",
            ]
            v.action_plan = [
                f"Attack {primary_cause}: {false_edge.details.get(primary_cause, '')}",
                "Apply survival filters to remove toxic segments",
                "Reduce position size to minimum for real-market validation",
                "Set hard deadline: if not PF > 1.0 in 3 months, kill permanently",
            ]
            return v

        # KILL: default — no alpha, or costs completely dominate
        v.verdict = "KILL"
        v.confidence = "HIGH"

        if not has_alpha:
            v.reasons.append(f"No raw alpha: ideal PnL = ${cost_attr.total_ideal_pnl:.2f}")
        elif cost_ratio > 2:
            v.reasons.append(f"Costs are {cost_ratio:.1f}x the alpha — no economically feasible fix")
        if false_edge_total > 400:
            v.reasons.append(f"False edge score {false_edge_total:.0f}/700 — systemic problems")
        if not naive_profitable:
            v.reasons.append("Even naive backtest (zero costs) is unprofitable")
        if segment_survivors == 0:
            v.reasons.append("Zero profitable segments found across all dimensions")
        if filter_survivors == 0:
            v.reasons.append("No filter combination improves expectancy above zero")

        v.action_plan = [
            "STOP trading this strategy immediately",
            "Do NOT allocate more development time",
            "Archive the code and analysis for reference",
            "Investigate fundamentally different signal sources",
            "Consider: the market microstructure of crypto perpetuals "
            "requires much stronger alpha than the 2-bar candle pattern provides",
        ]

        return v


# ═══════════════════════════════════════════
#  CONVENIENCE: Full Autopsy
# ═══════════════════════════════════════════

@dataclass
class AutopsyResult:
    """Complete autopsy output."""
    cost_attribution: CostAttribution = field(default_factory=CostAttribution)
    false_edge: FalseEdgeScore = field(default_factory=FalseEdgeScore)
    decision: DecisionVerdict = field(default_factory=DecisionVerdict)
    report: str = ""


def run_autopsy(
    trades: List[RealisticTrade],
    realistic_metrics: Optional[DetailedMetrics] = None,
    naive_metrics: Optional[DetailedMetrics] = None,
    segment_survivors: int = 0,
    filter_survivors: int = 0,
) -> AutopsyResult:
    """Run the complete PnL autopsy."""
    result = AutopsyResult()

    # Step 1: PnL decomposition
    result.cost_attribution = PnLDecomposer.decompose_all(trades)

    # Step 2: False edge detection
    result.false_edge = FalseEdgeDetector.analyze(
        trades, result.cost_attribution, realistic_metrics, naive_metrics,
    )

    # Step 3: Decision matrix
    result.decision = DecisionMatrix.evaluate(
        result.cost_attribution,
        result.false_edge,
        realistic_metrics,
        naive_metrics,
        segment_survivors,
        filter_survivors,
    )

    # Step 4: Format report
    result.report = format_autopsy_report(result)
    return result


# ═══════════════════════════════════════════
#  REPORT FORMATTER
# ═══════════════════════════════════════════

def format_autopsy_report(result: AutopsyResult) -> str:
    """Format the autopsy as a human-readable report."""
    lines = []
    SEP = "═" * 72
    sep = "─" * 72

    lines.append(SEP)
    lines.append("  EDGE AUTOPSY — COLD QUANTITATIVE POST-MORTEM")
    lines.append(SEP)
    lines.append("")

    # ── Decision Verdict ──
    d = result.decision
    lines.append(f"  ╔══════════════════════════════════════════╗")
    lines.append(f"  ║  VERDICT: {d.verdict:>10}  (conf: {d.confidence})      ║")
    lines.append(f"  ╚══════════════════════════════════════════╝")
    lines.append("")
    for r in d.reasons:
        lines.append(f"  • {r}")
    lines.append("")

    # ── PnL Waterfall ──
    ca = result.cost_attribution
    lines.append(sep)
    lines.append("  1. PnL DECOMPOSITION (WATERFALL)")
    lines.append(sep)
    lines.append(f"  {'Component':<25} {'Total ($)':>12} {'Per Trade':>12} {'Share':>8}")
    lines.append(f"  {'─' * 25} {'─' * 12} {'─' * 12} {'─' * 8}")
    lines.append(f"  {'Ideal Alpha':<25} {ca.total_ideal_pnl:>+12.2f} {ca.avg_ideal_pnl:>+12.2f} {'100%':>8}")
    lines.append(f"  {'− Entry Slippage':<25} {-ca.total_entry_slippage:>12.2f} {-ca.avg_entry_slippage:>12.2f} {ca.entry_slip_pct:>7.1f}%")
    lines.append(f"  {'− Exit Slippage':<25} {-ca.total_exit_slippage:>12.2f} {-ca.avg_exit_slippage:>12.2f} {ca.exit_slip_pct:>7.1f}%")
    lines.append(f"  {'− Fees':<25} {-ca.total_fees:>12.2f} {-ca.avg_fees:>12.2f} {ca.fees_pct:>7.1f}%")
    lines.append(f"  {'− Funding':<25} {-ca.total_funding:>12.2f} {-ca.avg_funding:>12.2f} {ca.funding_pct:>7.1f}%")
    lines.append(f"  {'─' * 25} {'─' * 12} {'─' * 12} {'─' * 8}")
    lines.append(f"  {'= NET PnL':<25} {ca.total_net_pnl:>+12.2f} {ca.avg_net_pnl:>+12.2f}")
    lines.append("")
    lines.append(f"  Cost-to-Alpha Ratio : {ca.cost_to_alpha_ratio:.2f}x")
    lines.append(f"  Alpha Survival      : {ca.alpha_survival_pct:.1f}%")
    lines.append(f"  Breakeven Alpha     : {ca.breakeven_alpha_bps:.1f} bps/trade")
    lines.append(f"  Recon Max Error     : ${ca.max_recon_error:.4f}")
    lines.append("")

    # ── Kill Ranking ──
    lines.append("  Cost Killer Ranking:")
    for rank, (name, total, pct) in enumerate(ca.kill_ranking, 1):
        lines.append(f"    #{rank} {name:<20} ${total:>10.2f} ({pct:.1f}%)")
    lines.append("")

    # ── False Edge Detection ──
    fe = result.false_edge
    lines.append(sep)
    lines.append("  2. FALSE EDGE ROOT CAUSES")
    lines.append(sep)
    lines.append(f"  {'Root Cause':<28} {'Score':>6} {'Bar':>40}")
    lines.append(f"  {'─' * 28} {'─' * 6} {'─' * 40}")

    causes = [
        ("Look-Ahead Bias", fe.look_ahead),
        ("Fill Overestimation", fe.fill_overestimation),
        ("Spread/Slippage", fe.spread_underestimation),
        ("Overtrading", fe.overtrading),
        ("Bad SL/TP Placement", fe.bad_sl_tp),
        ("Wrong Regime", fe.wrong_regime),
        ("Weak Alpha", fe.weak_alpha),
    ]
    for name, sc in causes:
        bar = "█" * int(sc / 2.5) + "░" * (40 - int(sc / 2.5))
        lines.append(f"  {name:<28} {sc:>5.0f}  {bar}")
    lines.append(f"\n  PRIMARY CAUSE: {fe.primary_cause.upper()}")
    lines.append(f"  TOTAL FALSE EDGE SCORE: {fe.total_score:.0f} / 700")
    lines.append("")

    for key, detail in fe.details.items():
        lines.append(f"  [{key}] {detail}")
    lines.append("")

    # ── Action Plan ──
    lines.append(sep)
    lines.append("  3. ACTION PLAN")
    lines.append(sep)
    if d.conditions:
        lines.append("  Conditions:")
        for c in d.conditions:
            lines.append(f"    ⚠ {c}")
        lines.append("")
    lines.append("  Actions:")
    for i, a in enumerate(d.action_plan, 1):
        lines.append(f"    {i}. {a}")
    lines.append("")

    lines.append(SEP)
    lines.append("  \"Ne protège pas la stratégie. Protège le capital.\"")
    lines.append(SEP)

    return "\n".join(lines)
