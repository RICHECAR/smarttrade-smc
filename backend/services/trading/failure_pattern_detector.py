"""
Failure Pattern Detector
════════════════════════

Detects recurring failure patterns in the trading system:
  • Bad timing (entering too late / too early)
  • Over-aggression (too large positions in bad conditions)
  • Regime mismatch (wrong strategy for current regime)
  • Signal decay (acting on stale signals)
  • Correlation traps (correlated losses)
  • Liquidity misjudgement
  • False breakouts

Automatically computes exposure reductions and can suppress patterns.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from backend.services.trading.self_evolution_models import (
    FailurePattern,
    FailureReport,
    FailureType,
)


class FailurePatternDetector:
    """Identifies and tracks recurring failure patterns to reduce exposure."""

    # ── Configuration ─────────────────────────────────────────────
    MIN_OCCURRENCES = 3             # minimum failures to form a pattern
    SUPPRESSION_THRESHOLD = 5       # occurrences to auto-suppress
    CONFIDENCE_FLOOR = 0.50         # min confidence to report
    DECAY_HALF_LIFE = 50            # older failures decay (in trade count)
    MAX_PATTERNS = 50               # cap stored patterns
    EXPOSURE_REDUCTION_SCALE = 0.10 # per occurrence above threshold

    # Failure detection thresholds
    BAD_TIMING_LATE_ENTRY_PCT = 0.60    # move >60 % expressed
    OVER_AGGRESSION_SIZE_MULT = 1.40    # size multiplier > 1.4x
    REGIME_MISMATCH_ALIGNMENT = 0.25    # regime alignment < 0.25
    SIGNAL_DECAY_AGE = 8                # signal older than 8 bars
    CORRELATION_CLUSTER_LOSSES = 3      # 3+ correlated losses
    FALSE_BREAKOUT_REVERSAL_PCT = 0.50  # price reverses > 50 %

    def __init__(self) -> None:
        # All ingested failures
        self._failures: List[Dict[str, Any]] = []
        # pattern_id → FailurePattern
        self._patterns: Dict[str, FailurePattern] = {}
        # Suppression state
        self._suppressed: Dict[str, bool] = {}
        self._trade_counter: int = 0
        self._last_report: FailureReport | None = None

    # ── Ingest ────────────────────────────────────────────────────
    def ingest_trade(
        self,
        symbol: str,
        pnl_pct: float,
        won: bool,
        context: Dict[str, Any] | None = None,
    ) -> List[FailureType]:
        """Record a trade outcome and detect any failure types present.
        Returns list of detected failure types for this trade."""
        self._trade_counter += 1
        ctx = context or {}
        detected: List[FailureType] = []

        if won:
            return detected  # only track failures

        record = {
            "symbol": symbol,
            "pnl_pct": pnl_pct,
            "trade_number": self._trade_counter,
            "ts": time.time(),
            **ctx,
        }

        # Detect failure types
        if ctx.get("move_expressed_pct", 0) > self.BAD_TIMING_LATE_ENTRY_PCT:
            detected.append(FailureType.BAD_TIMING)
            record["failure_bad_timing"] = True

        if ctx.get("signal_age_bars", 0) > self.SIGNAL_DECAY_AGE:
            detected.append(FailureType.SIGNAL_DECAY)
            record["failure_signal_decay"] = True

        if ctx.get("size_multiplier", 1.0) > self.OVER_AGGRESSION_SIZE_MULT:
            detected.append(FailureType.OVER_AGGRESSION)
            record["failure_over_aggression"] = True

        if ctx.get("regime_alignment", 1.0) < self.REGIME_MISMATCH_ALIGNMENT:
            detected.append(FailureType.REGIME_MISMATCH)
            record["failure_regime_mismatch"] = True

        if ctx.get("reversal_pct", 0) > self.FALSE_BREAKOUT_REVERSAL_PCT:
            detected.append(FailureType.FALSE_BREAKOUT)
            record["failure_false_breakout"] = True

        if ctx.get("correlated_losses", 0) >= self.CORRELATION_CLUSTER_LOSSES:
            detected.append(FailureType.CORRELATION_TRAP)
            record["failure_correlation_trap"] = True

        if ctx.get("liquidity_mismatch", False):
            detected.append(FailureType.LIQUIDITY_MISJUDGE)
            record["failure_liquidity"] = True

        record["failure_types"] = [f.value for f in detected]
        self._failures.append(record)

        # Update patterns
        for ft in detected:
            self._update_pattern(ft, record)

        return detected

    # ── Analyse ───────────────────────────────────────────────────
    def analyse(self) -> FailureReport:
        """Generate a full failure report with suppression recommendations."""
        active_patterns = []
        for pat in self._patterns.values():
            # Apply time decay to confidence
            age = self._trade_counter - pat.conditions.get("last_trade_number", 0)
            decay_factor = 0.5 ** (age / self.DECAY_HALF_LIFE) if self.DECAY_HALF_LIFE > 0 else 1.0
            pat.confidence = min(1.0, (pat.occurrence_count / (self.MIN_OCCURRENCES * 3))) * decay_factor

            if pat.confidence >= self.CONFIDENCE_FLOOR:
                active_patterns.append(pat)

            # Auto-suppress if enough occurrences
            if pat.occurrence_count >= self.SUPPRESSION_THRESHOLD and not pat.suppression_active:
                pat.suppression_active = True
                self._suppressed[pat.pattern_id] = True

        # Sort by severity (occurrence * avg loss)
        active_patterns.sort(key=lambda p: p.occurrence_count * abs(p.avg_loss_pct), reverse=True)

        active_suppressions = sum(1 for p in active_patterns if p.suppression_active)
        total_reduction = sum(p.exposure_reduction for p in active_patterns if p.suppression_active)

        report = FailureReport(
            patterns=active_patterns,
            total_patterns=len(active_patterns),
            active_suppressions=active_suppressions,
            total_exposure_reduction=min(0.80, total_reduction),  # cap at 80 %
            worst_pattern=active_patterns[0].pattern_id if active_patterns else "",
        )
        self._last_report = report
        return report

    # ── Getters ───────────────────────────────────────────────────
    def get_last_report(self) -> FailureReport | None:
        return self._last_report

    def get_pattern(self, pattern_id: str) -> Optional[FailurePattern]:
        return self._patterns.get(pattern_id)

    def get_suppressed_types(self) -> List[FailureType]:
        """Return list of currently suppressed failure types."""
        result = []
        for pat in self._patterns.values():
            if pat.suppression_active:
                result.append(pat.failure_type)
        return result

    def is_suppressed(self, failure_type: FailureType) -> bool:
        """Check if a failure type is currently suppressed."""
        for pat in self._patterns.values():
            if pat.failure_type == failure_type and pat.suppression_active:
                return True
        return False

    def get_exposure_reduction(self, failure_type: FailureType) -> float:
        """Get recommended exposure reduction for a failure type (0–1)."""
        for pat in self._patterns.values():
            if pat.failure_type == failure_type and pat.suppression_active:
                return pat.exposure_reduction
        return 0.0

    def clear_suppression(self, failure_type: FailureType) -> bool:
        """Manually clear a suppression."""
        cleared = False
        for pat in self._patterns.values():
            if pat.failure_type == failure_type:
                pat.suppression_active = False
                self._suppressed.pop(pat.pattern_id, None)
                cleared = True
        return cleared

    # ── Internal ──────────────────────────────────────────────────
    def _update_pattern(self, failure_type: FailureType, record: Dict[str, Any]) -> None:
        """Update or create a failure pattern from a new occurrence."""
        pattern_id = f"fp_{failure_type.value}"

        if pattern_id in self._patterns:
            pat = self._patterns[pattern_id]
            pat.occurrence_count += 1
            # Running average of loss
            old_avg = pat.avg_loss_pct
            pat.avg_loss_pct = old_avg + (record["pnl_pct"] - old_avg) / pat.occurrence_count
            # Update exposure reduction
            excess = max(0, pat.occurrence_count - self.MIN_OCCURRENCES)
            pat.exposure_reduction = min(0.50, excess * self.EXPOSURE_REDUCTION_SCALE)
            pat.conditions["last_trade_number"] = self._trade_counter
            pat.conditions["last_symbol"] = record.get("symbol", "")
        else:
            self._patterns[pattern_id] = FailurePattern(
                pattern_id=pattern_id,
                failure_type=failure_type,
                description=f"Recurring {failure_type.value} failures",
                occurrence_count=1,
                avg_loss_pct=record["pnl_pct"],
                conditions={
                    "last_trade_number": self._trade_counter,
                    "last_symbol": record.get("symbol", ""),
                },
                exposure_reduction=0.0,
                suppression_active=False,
                confidence=1.0 / (self.MIN_OCCURRENCES * 3),
            )

        # Trim patterns if too many
        if len(self._patterns) > self.MAX_PATTERNS:
            sorted_pats = sorted(self._patterns.values(),
                                 key=lambda p: p.occurrence_count, reverse=True)
            self._patterns = {p.pattern_id: p for p in sorted_pats[:self.MAX_PATTERNS]}
