"""
AlphaGovernor — Phase 9: Multi-Alpha Governance Layer
═════════════════════════════════════════════════════════
Monitors the health of the multi-alpha engine and defends against:

  1. Feedback loop instability (weight/capital adaptation too fast)
  2. Double penalization (strategy killed by stacking penalties)
  3. Hidden correlation (multiple strategies → same economic exposure)
  4. Slow-strategy starvation (infrequent strategies starved of capital)

Provides:
  - Starvation alert: scored >> accepted means scorer too strict
  - Concentration alert: single strategy > max_cap_pct too long
  - Dead strategy detection: strategy never selected for N cycles
  - Weight velocity dampening: limits how fast weights can change
  - Performance attribution: which strategy contributed what PnL
  - Conflict cost tracking: how much value is lost to conflict vetoes
  - Full governance snapshot for API / dashboard
"""

import logging
import time
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Alert severity ────────────────────────────────────────────────
_SEV_INFO = "info"
_SEV_WARNING = "warning"
_SEV_CRITICAL = "critical"


@dataclass
class GovernanceAlert:
    """Single governance alert event."""
    ts: float
    alert_type: str          # "starvation" | "concentration" | "dead_strategy" | "feedback_loop"
    severity: str            # "info" | "warning" | "critical"
    strategy: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConflictCostRecord:
    """Tracks the cost of a directional conflict veto."""
    ts: float
    long_strategy: str
    short_strategy: str
    long_score: float
    short_score: float
    resolution: str          # "best_score" | "no_trade" | "higher_confidence"
    winning_strategy: str    # "" if no_trade
    estimated_missed_value: float = 0.0


@dataclass
class AttributionEntry:
    """Performance attribution for one strategy."""
    strategy_name: str
    trades: int = 0
    wins: int = 0
    pnl: float = 0.0
    signals_scored: int = 0
    signals_accepted: int = 0
    signals_rejected: int = 0
    signals_shadow: int = 0
    conflicts_involved: int = 0
    dedup_merged: int = 0
    avg_score: float = 0.0
    avg_capital_fraction: float = 0.0
    current_weight: float = 1.0


class AlphaGovernor:
    """
    Governance layer that watches over the multi-alpha engine.
    Call check() periodically (e.g. every analysis cycle) to run all monitors.

    Usage:
        gov = AlphaGovernor(weight_tracker=wt, capital_allocator=ca)
        alerts = gov.check()          # returns List[GovernanceAlert]
        snap = gov.snapshot()          # full governance dashboard dict
    """

    ALL_STRATEGIES = [
        "DefaultSMC", "AsiaWeekend", "FundingReversion",
        "VWAPReversion", "SweepScalp", "LiqCascade", "OBImbalance",
    ]

    def __init__(
        self,
        weight_tracker=None,
        capital_allocator=None,
        # Starvation detection
        starvation_window: int = 50,
        starvation_accept_ratio: float = 0.05,
        # Concentration detection
        concentration_max_pct: float = 0.40,
        concentration_window_s: float = 3600.0,
        # Dead strategy detection
        dead_strategy_cycles: int = 200,
        # Feedback dampening
        max_weight_velocity: float = 0.3,
        dampening_alpha: float = 0.7,
        # Double-penalization floor
        combined_penalty_floor: float = 0.15,
    ):
        self._tracker = weight_tracker
        self._allocator = capital_allocator
        self._lock = threading.Lock()

        # Config
        self.starvation_window = starvation_window
        self.starvation_accept_ratio = starvation_accept_ratio
        self.concentration_max_pct = concentration_max_pct
        self.concentration_window_s = concentration_window_s
        self.dead_strategy_cycles = dead_strategy_cycles
        self.max_weight_velocity = max_weight_velocity
        self.dampening_alpha = dampening_alpha
        self.combined_penalty_floor = combined_penalty_floor

        # ── Tracking state ──────────────────────────────
        # Per-strategy rolling counters
        self._scored_count: Dict[str, int] = defaultdict(int)
        self._accepted_count: Dict[str, int] = defaultdict(int)
        self._rejected_count: Dict[str, int] = defaultdict(int)
        self._score_sums: Dict[str, float] = defaultdict(float)
        self._accepted_score_sums: Dict[str, float] = defaultdict(float)
        self._rejected_score_sums: Dict[str, float] = defaultdict(float)
        self._shadow_count: Dict[str, int] = defaultdict(int)
        self._shadow_score_sums: Dict[str, float] = defaultdict(float)
        self._last_accepted_cycle: Dict[str, int] = defaultdict(int)

        # Weight velocity tracking
        self._prev_weights: Dict[str, float] = {}
        self._dampened_weights: Dict[str, float] = {}

        # Concentration tracking
        self._concentration_start: Dict[str, float] = {}  # strategy → timestamp when first exceeded

        # Conflict cost tracking
        self._conflict_history: deque = deque(maxlen=200)
        self._conflicts_total: int = 0
        self._dedup_total: int = 0

        # Attribution
        self._attribution: Dict[str, AttributionEntry] = {
            name: AttributionEntry(strategy_name=name) for name in self.ALL_STRATEGIES
        }

        # Alerts
        self._alert_history: deque = deque(maxlen=500)
        self._check_cycle: int = 0

    # ═══════════════════════════════════════════════════════════════
    # Recording methods (called by orchestrator on each signal)
    # ═══════════════════════════════════════════════════════════════

    def record_scored(self, strategy_name: str, score: float) -> None:
        """Record that a signal was scored."""
        with self._lock:
            self._scored_count[strategy_name] += 1
            self._score_sums[strategy_name] += score
            attr = self._attribution.get(strategy_name)
            if attr:
                attr.signals_scored += 1
                total = self._scored_count[strategy_name]
                attr.avg_score = self._score_sums[strategy_name] / total if total > 0 else 0

    def record_accepted(self, strategy_name: str, capital_fraction: float, score: float = 0.0) -> None:
        """Record that a signal passed the alpha gate."""
        with self._lock:
            self._accepted_count[strategy_name] += 1
            self._accepted_score_sums[strategy_name] += score
            self._last_accepted_cycle[strategy_name] = self._check_cycle
            attr = self._attribution.get(strategy_name)
            if attr:
                attr.signals_accepted += 1
                # Running average of capital fraction
                n = attr.signals_accepted
                attr.avg_capital_fraction = (
                    attr.avg_capital_fraction * (n - 1) + capital_fraction
                ) / n

    def record_rejected(self, strategy_name: str, reason: str = "", score: float = 0.0) -> None:
        """Record that a signal was rejected by the alpha gate."""
        with self._lock:
            self._rejected_count[strategy_name] += 1
            self._rejected_score_sums[strategy_name] += score
            attr = self._attribution.get(strategy_name)
            if attr:
                attr.signals_rejected += 1

    def record_shadow(self, strategy_name: str, score: float = 0.0) -> None:
        """Record a valid signal that was not chosen (outscored by another overlay)."""
        with self._lock:
            self._shadow_count[strategy_name] += 1
            self._shadow_score_sums[strategy_name] += score
            attr = self._attribution.get(strategy_name)
            if attr:
                attr.signals_shadow += 1

    def record_conflict(
        self,
        long_strategy: str,
        short_strategy: str,
        long_score: float,
        short_score: float,
        resolution: str,
        winner: str,
    ) -> None:
        """Record a directional conflict event and its resolution."""
        record = ConflictCostRecord(
            ts=time.time(),
            long_strategy=long_strategy,
            short_strategy=short_strategy,
            long_score=long_score,
            short_score=short_score,
            resolution=resolution,
            winning_strategy=winner,
        )
        with self._lock:
            self._conflict_history.append(record)
            self._conflicts_total += 1
            for name in (long_strategy, short_strategy):
                attr = self._attribution.get(name)
                if attr:
                    attr.conflicts_involved += 1

    def record_dedup(self, merged_strategy: str, kept_strategy: str) -> None:
        """Record a correlation dedup merge."""
        with self._lock:
            self._dedup_total += 1
            attr = self._attribution.get(merged_strategy)
            if attr:
                attr.dedup_merged += 1

    def record_trade_outcome(self, strategy_name: str, pnl: float, won: bool) -> None:
        """Record trade result for performance attribution."""
        with self._lock:
            attr = self._attribution.get(strategy_name)
            if attr:
                attr.trades += 1
                attr.pnl += pnl
                if won:
                    attr.wins += 1

    # ═══════════════════════════════════════════════════════════════
    # Check cycle — run all monitors
    # ═══════════════════════════════════════════════════════════════

    def check(self) -> List[GovernanceAlert]:
        """
        Run all governance checks. Returns list of new alerts (may be empty).
        Call this once per orchestrator analysis cycle.
        """
        self._check_cycle += 1
        alerts: List[GovernanceAlert] = []

        alerts.extend(self._check_starvation())
        alerts.extend(self._check_concentration())
        alerts.extend(self._check_dead_strategies())
        alerts.extend(self._check_feedback_velocity())
        alerts.extend(self._check_double_penalization())

        with self._lock:
            for a in alerts:
                self._alert_history.append(a)

        if alerts:
            for a in alerts:
                log_fn = logger.warning if a.severity != _SEV_INFO else logger.info
                log_fn(f"[ALPHA_GOV] {a.severity.upper()} | {a.alert_type} | {a.strategy} | {a.message}")

        return alerts

    # ── Monitor 1: Starvation ─────────────────────────────────────

    def _check_starvation(self) -> List[GovernanceAlert]:
        """Detect if scorer rejects almost everything."""
        alerts: List[GovernanceAlert] = []
        with self._lock:
            for name in self.ALL_STRATEGIES:
                scored = self._scored_count.get(name, 0)
                accepted = self._accepted_count.get(name, 0)
                if scored < self.starvation_window:
                    continue
                accept_rate = accepted / scored if scored > 0 else 0
                if accept_rate < self.starvation_accept_ratio:
                    alerts.append(GovernanceAlert(
                        ts=time.time(),
                        alert_type="starvation",
                        severity=_SEV_WARNING,
                        strategy=name,
                        message=(
                            f"Accept rate {accept_rate:.1%} < {self.starvation_accept_ratio:.0%}"
                            f" ({accepted}/{scored})"
                        ),
                        details={"scored": scored, "accepted": accepted, "rate": accept_rate},
                    ))
        return alerts

    # ── Monitor 2: Concentration ──────────────────────────────────

    def _check_concentration(self) -> List[GovernanceAlert]:
        """Detect if one strategy dominates capital allocation."""
        alerts: List[GovernanceAlert] = []
        if self._allocator is None:
            return alerts

        fracs = self._allocator.get_all_fractions()
        now = time.time()

        with self._lock:
            for name, frac in fracs.items():
                if frac > self.concentration_max_pct:
                    if name not in self._concentration_start:
                        self._concentration_start[name] = now
                    elapsed = now - self._concentration_start[name]
                    if elapsed > self.concentration_window_s:
                        alerts.append(GovernanceAlert(
                            ts=now,
                            alert_type="concentration",
                            severity=_SEV_WARNING,
                            strategy=name,
                            message=(
                                f"Capital fraction {frac:.1%} > {self.concentration_max_pct:.0%}"
                                f" for {elapsed / 60:.0f}min"
                            ),
                            details={"fraction": frac, "duration_s": elapsed},
                        ))
                else:
                    self._concentration_start.pop(name, None)

        return alerts

    # ── Monitor 3: Dead strategies ────────────────────────────────

    def _check_dead_strategies(self) -> List[GovernanceAlert]:
        """Detect strategies that haven't been selected for too long."""
        alerts: List[GovernanceAlert] = []
        with self._lock:
            for name in self.ALL_STRATEGIES:
                last = self._last_accepted_cycle.get(name, 0)
                gap = self._check_cycle - last
                if gap > self.dead_strategy_cycles and self._scored_count.get(name, 0) > 0:
                    alerts.append(GovernanceAlert(
                        ts=time.time(),
                        alert_type="dead_strategy",
                        severity=_SEV_INFO if gap < self.dead_strategy_cycles * 2 else _SEV_WARNING,
                        strategy=name,
                        message=f"Not selected for {gap} cycles (threshold={self.dead_strategy_cycles})",
                        details={"cycles_since_last": gap, "total_scored": self._scored_count[name]},
                    ))
        return alerts

    # ── Monitor 4: Feedback velocity ──────────────────────────────

    def _check_feedback_velocity(self) -> List[GovernanceAlert]:
        """Detect if adaptive weights are changing too fast."""
        alerts: List[GovernanceAlert] = []
        if self._tracker is None:
            return alerts

        current_weights = self._tracker.get_weights()
        with self._lock:
            for name, w in current_weights.items():
                prev = self._prev_weights.get(name, w)
                delta = abs(w - prev)

                if delta > self.max_weight_velocity:
                    # Apply dampening: blend toward previous to reduce shock
                    dampened = prev * self.dampening_alpha + w * (1.0 - self.dampening_alpha)
                    self._dampened_weights[name] = dampened
                    alerts.append(GovernanceAlert(
                        ts=time.time(),
                        alert_type="feedback_loop",
                        severity=_SEV_WARNING,
                        strategy=name,
                        message=(
                            f"Weight velocity {delta:.3f} > max {self.max_weight_velocity:.3f}"
                            f" | {prev:.3f}→{w:.3f}, dampened→{dampened:.3f}"
                        ),
                        details={"prev": prev, "current": w, "delta": delta, "dampened": dampened},
                    ))
                else:
                    self._dampened_weights.pop(name, None)

            self._prev_weights = dict(current_weights)

        return alerts

    # ── Monitor 5: Double penalization ────────────────────────────

    def _check_double_penalization(self) -> List[GovernanceAlert]:
        """Detect if a strategy is being penalized by multiple layers simultaneously."""
        alerts: List[GovernanceAlert] = []
        if self._tracker is None or self._allocator is None:
            return alerts

        weights = self._tracker.get_weights()
        fracs = self._allocator.get_all_fractions()
        base_fracs = self._allocator._base

        with self._lock:
            for name in self.ALL_STRATEGIES:
                w = weights.get(name, 1.0)
                f = fracs.get(name, 0.15)
                base_f = base_fracs.get(name, 0.15)

                # Combined penalty = weight reduction * allocation reduction
                weight_reduction = w / 1.0 if w < 1.0 else 1.0
                alloc_reduction = f / base_f if base_f > 0 and f < base_f else 1.0
                combined = weight_reduction * alloc_reduction

                if combined < self.combined_penalty_floor:
                    alerts.append(GovernanceAlert(
                        ts=time.time(),
                        alert_type="double_penalization",
                        severity=_SEV_WARNING,
                        strategy=name,
                        message=(
                            f"Combined penalty {combined:.2f} < floor {self.combined_penalty_floor:.2f}"
                            f" | weight={w:.3f} alloc={f:.1%}(base={base_f:.1%})"
                        ),
                        details={
                            "weight": w, "fraction": f, "base_fraction": base_f,
                            "combined_penalty": combined,
                        },
                    ))

        return alerts

    # ── Dampened weights access ───────────────────────────────────

    def get_dampened_weight(self, strategy_name: str) -> Optional[float]:
        """
        If a strategy's weight is being dampened due to velocity breach,
        return the dampened value. Else return None (use tracker weight).
        """
        with self._lock:
            return self._dampened_weights.get(strategy_name)

    # ═══════════════════════════════════════════════════════════════
    # Snapshot — Full governance dashboard for API
    # ═══════════════════════════════════════════════════════════════

    def snapshot(self) -> Dict[str, Any]:
        """Build complete governance dashboard."""
        with self._lock:
            return self._build_snapshot()

    def _build_snapshot(self) -> Dict[str, Any]:
        """Must hold lock."""
        # Attribution breakdown
        attribution = []
        for name in self.ALL_STRATEGIES:
            attr = self._attribution.get(name)
            if attr is None:
                continue
            attribution.append({
                "strategy": attr.strategy_name,
                "trades": attr.trades,
                "wins": attr.wins,
                "winrate_pct": round(attr.wins / attr.trades * 100, 1) if attr.trades > 0 else 0,
                "pnl": round(attr.pnl, 2),
                "signals_scored": attr.signals_scored,
                "signals_accepted": attr.signals_accepted,
                "signals_rejected": attr.signals_rejected,
                "signals_shadow": attr.signals_shadow,
                "accept_rate_pct": round(
                    attr.signals_accepted / attr.signals_scored * 100, 1
                ) if attr.signals_scored > 0 else 0,
                "conflicts_involved": attr.conflicts_involved,
                "dedup_merged": attr.dedup_merged,
                "avg_score": round(attr.avg_score, 4),
                "avg_capital_fraction": round(attr.avg_capital_fraction, 4),
                "current_weight": round(attr.current_weight, 3),
            })

        # Conflict summary
        recent_conflicts = list(self._conflict_history)[-20:]
        conflict_summary = {
            "total": self._conflicts_total,
            "dedup_total": self._dedup_total,
            "recent": [
                {
                    "ts": c.ts,
                    "long": c.long_strategy,
                    "short": c.short_strategy,
                    "resolution": c.resolution,
                    "winner": c.winning_strategy,
                }
                for c in recent_conflicts
            ],
        }

        # Active alerts
        recent_alerts = [a for a in self._alert_history if time.time() - a.ts < 3600]
        alerts_summary = [
            {
                "ts": a.ts,
                "type": a.alert_type,
                "severity": a.severity,
                "strategy": a.strategy,
                "message": a.message,
            }
            for a in recent_alerts
        ]

        # Weight velocity
        weight_status = {}
        if self._tracker:
            weights = self._tracker.get_weights()
            for name in self.ALL_STRATEGIES:
                w = weights.get(name, 1.0)
                dampened = self._dampened_weights.get(name)
                weight_status[name] = {
                    "weight": round(w, 3),
                    "dampened": round(dampened, 3) if dampened is not None else None,
                    "is_dampened": dampened is not None,
                }
                # Update attribution current_weight
                attr = self._attribution.get(name)
                if attr:
                    attr.current_weight = w

        # Capital fractions
        capital_status = {}
        if self._allocator:
            fracs = self._allocator.get_all_fractions()
            for name in self.ALL_STRATEGIES:
                capital_status[name] = round(fracs.get(name, 0), 4)

        return {
            "cycle": self._check_cycle,
            "generated_at": time.time(),
            "attribution": attribution,
            "conflicts": conflict_summary,
            "alerts": alerts_summary,
            "alerts_count_by_type": self._alert_counts(),
            "weights": weight_status,
            "capital_fractions": capital_status,
            "health": {
                "strategies_active": sum(
                    1 for a in attribution if a.get("signals_accepted", 0) > 0
                ),
                "strategies_total": len(self.ALL_STRATEGIES),
                "alpha_breadth": round(
                    sum(1 for a in attribution if a.get("signals_accepted", 0) > 0)
                    / len(self.ALL_STRATEGIES), 2
                ) if self.ALL_STRATEGIES else 0,
                "signal_quality_spread": round(self._compute_sqs(), 4),
                "shadow_total": sum(self._shadow_count.values()),
                "strategies_starved": sum(
                    1 for a in attribution
                    if a.get("signals_scored", 0) > self.starvation_window
                    and a.get("accept_rate_pct", 100) < self.starvation_accept_ratio * 100
                ),
                "strategies_dead": sum(
                    1 for name in self.ALL_STRATEGIES
                    if (self._check_cycle - self._last_accepted_cycle.get(name, 0))
                    > self.dead_strategy_cycles
                    and self._scored_count.get(name, 0) > 0
                ),
            },
        }

    def _compute_sqs(self) -> float:
        """Signal Quality Spread: avg_score(accepted) - avg_score(rejected)."""
        total_acc = sum(self._accepted_count.values())
        total_rej = sum(self._rejected_count.values())
        avg_acc = sum(self._accepted_score_sums.values()) / total_acc if total_acc > 0 else 0.0
        avg_rej = sum(self._rejected_score_sums.values()) / total_rej if total_rej > 0 else 0.0
        return avg_acc - avg_rej

    def _alert_counts(self) -> Dict[str, int]:
        """Count alerts by type in recent history."""
        counts: Dict[str, int] = defaultdict(int)
        cutoff = time.time() - 3600
        for a in self._alert_history:
            if a.ts > cutoff:
                counts[a.alert_type] += 1
        return dict(counts)
