"""
PerformanceGovernor — Phase 6: Observability & Performance Governance
═════════════════════════════════════════════════════════════════════
In-memory real-time aggregator for 4 production dashboards:

  1. REJECTION dashboard    — % by filter, symbol, session, hard/soft
  2. EXECUTION QUALITY      — slippage avg/max, fill delay, retry rate, reconciler
  3. STRATEGY HEALTH        — trades, rejects, winrate/PF by regime and strategy
  4. SHADOW MODE analytics  — cost of each protection on frequency and PF

Design:
  - Thread-safe (threading.Lock)
  - Ring-buffer based (fixed memory, no unbounded growth)
  - Auto-resets daily counters at midnight UTC
  - Exposes .snapshot() for API serialization
  - Feeds Prometheus gauges for Grafana

Not a replacement for the DB-backed AnalyticsRecorder — this is the
real-time complement for production monitoring.
"""

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("perf_governor")

# Prometheus metrics (lazy import, safe if prometheus_client not installed)
try:
    from smc_futures.observability.metrics import (
        REJECTION_TOTAL, REJECTION_HARD, REJECTION_SOFT,
        SIGNAL_EVALUATED_TOTAL, SLIPPAGE_BPS_HISTOGRAM,
        FILL_DELAY_HISTOGRAM, EMERGENCY_CLOSE_TOTAL,
        RECONCILER_ORPHANS_TOTAL, RECONCILER_GHOSTS_TOTAL,
        RECONCILER_FLATTENED_TOTAL, STRATEGY_TRADES_TOTAL,
        BLOCKED_TRADES_TOTAL, BLOCKED_TRADES_BY_FILTER,
        SHADOW_PROFIT_MISSED,
        ADAPTIVE_RISK_MULTIPLIER, ADAPTIVE_RISK_MODE_TOTAL,
        ADAPTIVE_RISK_BLOCK_TOTAL, ADAPTIVE_LEVERAGE,
        ADAPTIVE_EFFECTIVE_RISK_PCT,
    )
    _prom_available = True
except ImportError:
    _prom_available = False

# Ring buffer size: keep last N events in memory
_MAX_EVENTS = 5000
_MAX_SLIPPAGE_SAMPLES = 2000


# ═══════════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class RejectionEvent:
    ts: float
    symbol: str
    filter_name: str  # SLIPPAGE_KILL_COOLDOWN, SIGNAL_TOO_OLD, CORRELATION_BLOCKED, etc.
    is_hard: bool     # Hard reject (never bypassed) vs soft (bypassed by FORCE_EXECUTE)
    strategy: str = ""
    session: str = ""  # "asia" / "london" / "newyork" / "default"


@dataclass
class FillEvent:
    ts: float
    symbol: str
    strategy: str
    direction: str
    entry: float
    fill_price: float
    slippage_bps: float
    fill_delay_ms: float  # time from signal to fill
    regime: str = ""
    pnl: Optional[float] = None  # Set on close


@dataclass
class EmergencyCloseEvent:
    ts: float
    symbol: str
    attempts: int
    success: bool
    orphan: bool = False


@dataclass
class ReconcilerEvent:
    ts: float
    orphans: int
    ghosts: int
    flattened: int


@dataclass
class ShadowFilterCost:
    """Track what a filter costs in opportunity."""
    filter_name: str
    signals_blocked: int = 0
    # Hypothetical PnL of blocked signals (set when outcome is known)
    blocked_would_have_won: int = 0
    blocked_would_have_lost: int = 0
    blocked_unknown: int = 0


# ═══════════════════════════════════════════════════════════════════════
# Governor Singleton
# ═══════════════════════════════════════════════════════════════════════

class PerformanceGovernor:
    """
    Central performance monitor — aggregates real-time metrics
    from OrderManager, Reconciler, and Emergency Close.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._day_key: str = ""  # "YYYY-MM-DD"

        # ── Ring buffers ──────────────────────────────────
        self._rejections: deque = deque(maxlen=_MAX_EVENTS)
        self._fills: deque = deque(maxlen=_MAX_EVENTS)
        self._emergency_closes: deque = deque(maxlen=500)
        self._reconciler_events: deque = deque(maxlen=500)

        # ── Daily counters ────────────────────────────────
        self._daily_rejections_by_filter: Dict[str, int] = defaultdict(int)
        self._daily_rejections_by_symbol: Dict[str, int] = defaultdict(int)
        self._daily_rejections_by_session: Dict[str, int] = defaultdict(int)
        self._daily_hard_rejects: int = 0
        self._daily_soft_rejects: int = 0
        self._daily_total_signals: int = 0

        self._daily_fills: int = 0
        self._daily_wins: int = 0
        self._daily_losses: int = 0
        self._daily_pnl: float = 0.0
        self._daily_slippage_samples: deque = deque(maxlen=_MAX_SLIPPAGE_SAMPLES)
        self._daily_fill_delay_samples: deque = deque(maxlen=_MAX_SLIPPAGE_SAMPLES)

        self._daily_emergency_attempts: int = 0
        self._daily_emergency_successes: int = 0
        self._daily_orphans: int = 0

        self._daily_reconciler_checks: int = 0
        self._daily_reconciler_orphans: int = 0
        self._daily_reconciler_ghosts: int = 0
        self._daily_reconciler_flattened: int = 0

        # ── Strategy-level counters ───────────────────────
        # key = (strategy, regime)
        self._strategy_trades: Dict[Tuple[str, str], int] = defaultdict(int)
        self._strategy_wins: Dict[Tuple[str, str], int] = defaultdict(int)
        self._strategy_pnl: Dict[Tuple[str, str], float] = defaultdict(float)
        self._strategy_rejects: Dict[str, int] = defaultdict(int)

        # ── Shadow analytics ──────────────────────────────
        self._shadow_filter_costs: Dict[str, ShadowFilterCost] = {}

        # ── Lifecycle ─────────────────────────────────────
        self._started_at = time.time()
        self._maybe_reset_day()

        logger.info("[PERF_GOVERNOR] Initialized")

    # ─────────────────────────────────────────────────────────────────
    # Day boundary management
    # ─────────────────────────────────────────────────────────────────

    def _maybe_reset_day(self) -> None:
        """Reset daily counters if we crossed midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day_key:
            with self._lock:
                if today != self._day_key:
                    self._day_key = today
                    self._daily_rejections_by_filter = defaultdict(int)
                    self._daily_rejections_by_symbol = defaultdict(int)
                    self._daily_rejections_by_session = defaultdict(int)
                    self._daily_hard_rejects = 0
                    self._daily_soft_rejects = 0
                    self._daily_total_signals = 0
                    self._daily_fills = 0
                    self._daily_wins = 0
                    self._daily_losses = 0
                    self._daily_pnl = 0.0
                    self._daily_slippage_samples = deque(maxlen=_MAX_SLIPPAGE_SAMPLES)
                    self._daily_fill_delay_samples = deque(maxlen=_MAX_SLIPPAGE_SAMPLES)
                    self._daily_emergency_attempts = 0
                    self._daily_emergency_successes = 0
                    self._daily_orphans = 0
                    self._daily_reconciler_checks = 0
                    self._daily_reconciler_orphans = 0
                    self._daily_reconciler_ghosts = 0
                    self._daily_reconciler_flattened = 0
                    self._strategy_trades = defaultdict(int)
                    self._strategy_wins = defaultdict(int)
                    self._strategy_pnl = defaultdict(float)
                    self._strategy_rejects = defaultdict(int)
                    self._daily_adaptive_by_mode = defaultdict(int)
                    self._daily_adaptive_block_reasons = defaultdict(int)
                    self._daily_adaptive_multipliers = deque(maxlen=_MAX_SLIPPAGE_SAMPLES)
                    self._daily_adaptive_decisions = 0
                    logger.info(f"[PERF_GOVERNOR] Daily reset → {today}")

    # ─────────────────────────────────────────────────────────────────
    # Recording methods (called by OrderManager / Reconciler)
    # ─────────────────────────────────────────────────────────────────

    def record_signal_evaluated(self, strategy: str = "") -> None:
        """Count every signal that enters the pipeline."""
        self._maybe_reset_day()
        with self._lock:
            self._daily_total_signals += 1
        if _prom_available:
            SIGNAL_EVALUATED_TOTAL.inc()

    def record_rejection(
        self,
        symbol: str,
        filter_name: str,
        is_hard: bool,
        strategy: str = "",
        session: str = "",
    ) -> None:
        """Record a pre-flight rejection."""
        self._maybe_reset_day()
        evt = RejectionEvent(
            ts=time.time(),
            symbol=symbol,
            filter_name=filter_name,
            is_hard=is_hard,
            strategy=strategy,
            session=session,
        )
        with self._lock:
            self._rejections.append(evt)
            self._daily_rejections_by_filter[filter_name] += 1
            self._daily_rejections_by_symbol[symbol] += 1
            if session:
                self._daily_rejections_by_session[session] += 1
            if is_hard:
                self._daily_hard_rejects += 1
            else:
                self._daily_soft_rejects += 1
            self._strategy_rejects[strategy] += 1
        if _prom_available:
            REJECTION_TOTAL.labels(filter=filter_name, symbol=symbol).inc()
            (REJECTION_HARD if is_hard else REJECTION_SOFT).inc()

    def record_fill(
        self,
        symbol: str,
        strategy: str,
        direction: str,
        entry: float,
        fill_price: float,
        slippage_bps: float,
        fill_delay_ms: float = 0.0,
        regime: str = "",
    ) -> None:
        """Record a successful fill."""
        self._maybe_reset_day()
        evt = FillEvent(
            ts=time.time(),
            symbol=symbol,
            strategy=strategy,
            direction=direction,
            entry=entry,
            fill_price=fill_price,
            slippage_bps=slippage_bps,
            fill_delay_ms=fill_delay_ms,
            regime=regime,
        )
        with self._lock:
            self._fills.append(evt)
            self._daily_fills += 1
            self._daily_slippage_samples.append(slippage_bps)
            if fill_delay_ms > 0:
                self._daily_fill_delay_samples.append(fill_delay_ms)
            key = (strategy, regime)
            self._strategy_trades[key] += 1
        if _prom_available:
            SLIPPAGE_BPS_HISTOGRAM.observe(slippage_bps)
            if fill_delay_ms > 0:
                FILL_DELAY_HISTOGRAM.observe(fill_delay_ms)
            STRATEGY_TRADES_TOTAL.labels(strategy=strategy, regime=regime).inc()

    def record_trade_closed(
        self,
        symbol: str,
        strategy: str,
        pnl: float,
        regime: str = "",
    ) -> None:
        """Record trade outcome."""
        self._maybe_reset_day()
        with self._lock:
            self._daily_pnl += pnl
            if pnl >= 0:
                self._daily_wins += 1
            else:
                self._daily_losses += 1
            key = (strategy, regime)
            self._strategy_wins[key] += (1 if pnl >= 0 else 0)
            self._strategy_pnl[key] += pnl

    def record_emergency_close(
        self,
        symbol: str,
        attempts: int,
        success: bool,
    ) -> None:
        """Record emergency close attempt."""
        self._maybe_reset_day()
        evt = EmergencyCloseEvent(
            ts=time.time(),
            symbol=symbol,
            attempts=attempts,
            success=success,
            orphan=not success,
        )
        with self._lock:
            self._emergency_closes.append(evt)
            self._daily_emergency_attempts += 1
            if success:
                self._daily_emergency_successes += 1
            else:
                self._daily_orphans += 1
        if _prom_available:
            EMERGENCY_CLOSE_TOTAL.labels(outcome="success" if success else "failure").inc()

    def record_reconciler_cycle(
        self,
        orphans: int,
        ghosts: int,
        flattened: int,
    ) -> None:
        """Record one reconciler heartbeat result."""
        self._maybe_reset_day()
        evt = ReconcilerEvent(
            ts=time.time(),
            orphans=orphans,
            ghosts=ghosts,
            flattened=flattened,
        )
        with self._lock:
            self._reconciler_events.append(evt)
            self._daily_reconciler_checks += 1
            self._daily_reconciler_orphans += orphans
            self._daily_reconciler_ghosts += ghosts
            self._daily_reconciler_flattened += flattened
        if _prom_available:
            RECONCILER_ORPHANS_TOTAL.inc(orphans)
            RECONCILER_GHOSTS_TOTAL.inc(ghosts)
            RECONCILER_FLATTENED_TOTAL.inc(flattened)

    def record_shadow_block(
        self,
        filter_name: str,
        outcome: str = "unknown",  # "won" / "lost" / "unknown"
    ) -> None:
        """Track cost of a filter in shadow mode."""
        with self._lock:
            if filter_name not in self._shadow_filter_costs:
                self._shadow_filter_costs[filter_name] = ShadowFilterCost(
                    filter_name=filter_name
                )
            cost = self._shadow_filter_costs[filter_name]
            cost.signals_blocked += 1
            if outcome == "won":
                cost.blocked_would_have_won += 1
            elif outcome == "lost":
                cost.blocked_would_have_lost += 1
            else:
                cost.blocked_unknown += 1
        if _prom_available:
            BLOCKED_TRADES_TOTAL.inc()
            BLOCKED_TRADES_BY_FILTER.labels(filter=filter_name).inc()
            SHADOW_PROFIT_MISSED.labels(filter=filter_name, outcome=outcome).inc()

    def record_adaptive_risk_decision(
        self,
        mode: str,
        risk_multiplier: float,
        effective_leverage: int,
        effective_risk_pct: float,
        reasons: List[str],
        symbol: str = "",
        strategy: str = "",
    ) -> None:
        """Record an adaptive risk engine decision."""
        self._maybe_reset_day()
        with self._lock:
            self._daily_adaptive_decisions += 1
            self._daily_adaptive_by_mode[mode] += 1
            self._daily_adaptive_multipliers.append(risk_multiplier)
            if mode == "blocked":
                for r in reasons:
                    self._daily_adaptive_block_reasons[r] += 1
        if _prom_available:
            ADAPTIVE_RISK_MULTIPLIER.set(risk_multiplier)
            ADAPTIVE_RISK_MODE_TOTAL.labels(mode=mode).inc()
            ADAPTIVE_LEVERAGE.set(effective_leverage)
            ADAPTIVE_EFFECTIVE_RISK_PCT.set(effective_risk_pct)
            if mode == "blocked":
                for r in reasons:
                    ADAPTIVE_RISK_BLOCK_TOTAL.labels(reason=r).inc()

    # ─────────────────────────────────────────────────────────────────
    # Snapshot — Full dashboard state for API
    # ─────────────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """
        Return complete dashboard state for API serialization.
        Thread-safe, zero-copy of mutable state.
        """
        self._maybe_reset_day()
        with self._lock:
            return self._build_snapshot()

    def _build_snapshot(self) -> Dict[str, Any]:
        """Build the full snapshot dict (must hold lock)."""
        total_rejects = self._daily_hard_rejects + self._daily_soft_rejects

        # ── Dashboard 1: Rejections ───────────────────────
        rejection_dashboard = {
            "date": self._day_key,
            "total_signals": self._daily_total_signals,
            "total_rejections": total_rejects,
            "rejection_rate_pct": round(
                total_rejects / self._daily_total_signals * 100, 1
            ) if self._daily_total_signals > 0 else 0.0,
            "hard_rejects": self._daily_hard_rejects,
            "soft_rejects": self._daily_soft_rejects,
            "by_filter": dict(self._daily_rejections_by_filter),
            "by_symbol": dict(self._daily_rejections_by_symbol),
            "by_session": dict(self._daily_rejections_by_session),
            "pct_by_filter": self._pct_dict(
                self._daily_rejections_by_filter, total_rejects
            ),
        }

        # ── Dashboard 2: Execution Quality ────────────────
        slip_samples = list(self._daily_slippage_samples)
        delay_samples = list(self._daily_fill_delay_samples)

        execution_dashboard = {
            "date": self._day_key,
            "total_fills": self._daily_fills,
            "slippage": self._stats_summary(slip_samples, "bps"),
            "fill_delay": self._stats_summary(delay_samples, "ms"),
            "emergency_close": {
                "attempts": self._daily_emergency_attempts,
                "successes": self._daily_emergency_successes,
                "failures": self._daily_emergency_attempts - self._daily_emergency_successes,
                "retry_rate_pct": round(
                    self._daily_emergency_attempts / self._daily_fills * 100, 1
                ) if self._daily_fills > 0 else 0.0,
                "orphans": self._daily_orphans,
            },
            "reconciler": {
                "checks": self._daily_reconciler_checks,
                "orphans_detected": self._daily_reconciler_orphans,
                "ghosts_cleaned": self._daily_reconciler_ghosts,
                "auto_flattened": self._daily_reconciler_flattened,
            },
        }

        # ── Dashboard 3: Strategy Health ──────────────────
        closed = self._daily_wins + self._daily_losses
        strategy_dashboard = {
            "date": self._day_key,
            "trades_opened": self._daily_fills,
            "trades_closed": closed,
            "total_rejections": total_rejects,
            "pnl": round(self._daily_pnl, 2),
            "winrate_pct": round(
                self._daily_wins / closed * 100, 1
            ) if closed > 0 else 0.0,
            "profit_factor": self._compute_pf(),
            "by_strategy_regime": self._strategy_breakdown(),
            "rejects_by_strategy": dict(self._strategy_rejects),
        }

        # ── Dashboard 4: Shadow Mode Analytics ────────────
        shadow_dashboard = {
            "filters": {
                name: asdict(cost)
                for name, cost in self._shadow_filter_costs.items()
            },
            "total_blocked": sum(
                c.signals_blocked for c in self._shadow_filter_costs.values()
            ),
            "filters_ranked_by_cost": self._rank_filters_by_cost(),
        }

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "uptime_sec": round(time.time() - self._started_at),
            "rejection_dashboard": rejection_dashboard,
            "execution_quality": execution_dashboard,
            "strategy_health": strategy_dashboard,
            "shadow_analytics": shadow_dashboard,
            "adaptive_risk": self._adaptive_risk_summary(),
        }

    def _adaptive_risk_summary(self) -> Dict[str, Any]:
        """Build Phase 7 adaptive risk dashboard section (must hold lock)."""
        mult_samples = list(self._daily_adaptive_multipliers)
        return {
            "total_decisions": self._daily_adaptive_decisions,
            "by_mode": dict(self._daily_adaptive_by_mode),
            "block_reasons": dict(self._daily_adaptive_block_reasons),
            "multiplier": self._stats_summary(mult_samples, "x") if mult_samples else {"count": 0, "unit": "x"},
        }

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _pct_dict(counter: Dict[str, int], total: int) -> Dict[str, float]:
        """Convert count dict to percentage dict."""
        if total <= 0:
            return {}
        return {k: round(v / total * 100, 1) for k, v in counter.items()}

    @staticmethod
    def _stats_summary(samples: List[float], unit: str) -> Dict[str, Any]:
        """Compute mean, p50, p95, p99, max for a sample list."""
        if not samples:
            return {"count": 0, "unit": unit}
        s = sorted(samples)
        n = len(s)
        return {
            "count": n,
            "unit": unit,
            "mean": round(sum(s) / n, 2),
            "p50": round(s[n // 2], 2),
            "p95": round(s[int(n * 0.95)], 2) if n >= 20 else round(s[-1], 2),
            "p99": round(s[int(n * 0.99)], 2) if n >= 100 else round(s[-1], 2),
            "max": round(s[-1], 2),
        }

    def _compute_pf(self) -> float:
        """Profit factor from strategy_pnl breakdown."""
        gross_win = sum(v for v in self._strategy_pnl.values() if v > 0)
        gross_loss = abs(sum(v for v in self._strategy_pnl.values() if v < 0))
        if gross_loss == 0:
            return round(gross_win, 2) if gross_win > 0 else 0.0
        return round(gross_win / gross_loss, 2)

    def _strategy_breakdown(self) -> List[Dict[str, Any]]:
        """Build per-(strategy, regime) breakdown."""
        result = []
        all_keys = set(self._strategy_trades.keys()) | set(self._strategy_pnl.keys())
        for key in sorted(all_keys):
            strategy, regime = key
            trades = self._strategy_trades.get(key, 0)
            wins = self._strategy_wins.get(key, 0)
            pnl = self._strategy_pnl.get(key, 0.0)
            result.append({
                "strategy": strategy,
                "regime": regime,
                "trades": trades,
                "wins": wins,
                "winrate_pct": round(wins / trades * 100, 1) if trades > 0 else 0.0,
                "pnl": round(pnl, 2),
            })
        return result

    def _rank_filters_by_cost(self) -> List[Dict[str, Any]]:
        """Rank filters by opportunity cost (blocked signals that would have won)."""
        ranked = sorted(
            self._shadow_filter_costs.values(),
            key=lambda c: c.blocked_would_have_won,
            reverse=True,
        )
        return [
            {
                "filter": c.filter_name,
                "blocked": c.signals_blocked,
                "would_have_won": c.blocked_would_have_won,
                "would_have_lost": c.blocked_would_have_lost,
                "unknown": c.blocked_unknown,
                "net_cost_ratio": round(
                    c.blocked_would_have_won / c.signals_blocked, 2
                ) if c.signals_blocked > 0 else 0.0,
            }
            for c in ranked
            if c.signals_blocked > 0
        ]


# ═══════════════════════════════════════════════════════════════════════
# Module-level singleton
# ═══════════════════════════════════════════════════════════════════════

governor = PerformanceGovernor()
