"""
Meta-Learning Engine
════════════════════

Learns from past trades, failed signals, and missed opportunities
to identify patterns not explicitly coded in the system.

Pipeline:
  1. Ingest trade outcomes with full context (regime, timing, aggression, etc.)
  2. Cluster outcomes by contextual dimensions
  3. Identify statistically significant patterns
  4. Generate actionable recommendations

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from backend.services.trading.self_evolution_models import (
    LearnedPattern,
    LearningSource,
    MetaLearningReport,
)


class MetaLearningEngine:
    """Discovers implicit patterns from trade history and system decisions."""

    # ── Configuration ─────────────────────────────────────────────
    MIN_SAMPLE_SIZE = 10        # minimum events to form a pattern
    CORRELATION_THRESHOLD = 0.25  # |correlation| must exceed this
    CONFIDENCE_THRESHOLD = 0.60   # confidence above this → actionable
    MAX_PATTERNS = 100           # cap stored patterns
    CONTEXT_DIMENSIONS = [
        "regime", "volatility_bucket", "session", "day_of_week",
        "aggression_level", "timing_verdict", "setup_family",
        "position_size_bucket", "correlation_cluster",
    ]

    def __init__(self) -> None:
        # List of all ingested outcomes
        self._outcomes: List[Dict[str, Any]] = []
        # dimension_value → list of (pnl, won) tuples
        self._buckets: Dict[str, List[tuple]] = defaultdict(list)
        # Known patterns by ID
        self._patterns: Dict[str, LearnedPattern] = {}
        self._last_report: MetaLearningReport | None = None

    # ── Ingest ────────────────────────────────────────────────────
    def ingest_outcome(
        self,
        pnl_pct: float,
        won: bool,
        source: LearningSource = LearningSource.WINNING_TRADE,
        context: Dict[str, Any] | None = None,
    ) -> None:
        """Record one outcome with its full context for pattern mining."""
        ctx = context or {}
        record = {
            "pnl_pct": pnl_pct,
            "won": won,
            "source": source.value,
            "ts": time.time(),
            **ctx,
        }
        self._outcomes.append(record)

        # Place into dimensional buckets
        for dim in self.CONTEXT_DIMENSIONS:
            if dim in ctx:
                bucket_key = f"{dim}={ctx[dim]}"
                self._buckets[bucket_key].append((pnl_pct, won))

    def ingest_missed_opportunity(
        self,
        symbol: str,
        expected_pnl: float,
        reason_skipped: str,
        context: Dict[str, Any] | None = None,
    ) -> None:
        """Record a missed opportunity for later learning."""
        ctx = context or {}
        ctx["symbol"] = symbol
        ctx["reason_skipped"] = reason_skipped
        self.ingest_outcome(
            pnl_pct=-expected_pnl,  # negative of the opportunity cost
            won=False,
            source=LearningSource.MISSED_OPPORTUNITY,
            context=ctx,
        )

    def ingest_false_signal(
        self,
        symbol: str,
        signal_type: str,
        actual_outcome_pct: float,
        context: Dict[str, Any] | None = None,
    ) -> None:
        """Record a signal that didn't produce the expected result."""
        ctx = context or {}
        ctx["symbol"] = symbol
        ctx["signal_type"] = signal_type
        self.ingest_outcome(
            pnl_pct=actual_outcome_pct,
            won=False,
            source=LearningSource.FALSE_SIGNAL,
            context=ctx,
        )

    # ── Analyse ───────────────────────────────────────────────────
    def analyse(self) -> MetaLearningReport:
        """Mine patterns from all ingested outcomes."""
        new_patterns: List[LearnedPattern] = []

        for bucket_key, records in self._buckets.items():
            if len(records) < self.MIN_SAMPLE_SIZE:
                continue

            pattern = self._analyse_bucket(bucket_key, records)
            if pattern is not None:
                self._patterns[pattern.pattern_id] = pattern
                new_patterns.append(pattern)

        # Cross-dimensional patterns (2D interactions)
        cross_patterns = self._analyse_cross_dimensions()
        for cp in cross_patterns:
            self._patterns[cp.pattern_id] = cp
            new_patterns.append(cp)

        # Trim to MAX_PATTERNS (keep highest confidence)
        if len(self._patterns) > self.MAX_PATTERNS:
            sorted_p = sorted(self._patterns.values(),
                              key=lambda p: p.confidence, reverse=True)
            self._patterns = {p.pattern_id: p for p in sorted_p[:self.MAX_PATTERNS]}

        all_patterns = list(self._patterns.values())
        actionable = [p for p in all_patterns if p.actionable]
        high_conf = [p for p in all_patterns if p.confidence >= 0.80]

        report = MetaLearningReport(
            patterns_discovered=new_patterns,
            total_patterns=len(all_patterns),
            actionable_count=len(actionable),
            high_confidence_count=len(high_conf),
            newest_pattern=new_patterns[-1].pattern_id if new_patterns else "",
        )
        self._last_report = report
        return report

    # ── Getters ───────────────────────────────────────────────────
    def get_last_report(self) -> MetaLearningReport | None:
        return self._last_report

    def get_pattern(self, pattern_id: str) -> LearnedPattern | None:
        return self._patterns.get(pattern_id)

    def get_all_patterns(self) -> List[LearnedPattern]:
        return list(self._patterns.values())

    def get_actionable_patterns(self) -> List[LearnedPattern]:
        return [p for p in self._patterns.values() if p.actionable]

    # ── Internal: Single-dimension analysis ───────────────────────
    def _analyse_bucket(self, bucket_key: str, records: List[tuple]) -> Optional[LearnedPattern]:
        """Analyse a single context bucket for significance."""
        pnls = [r[0] for r in records]
        wins = [r[1] for r in records]
        n = len(records)

        # Win rate in this bucket
        wr_bucket = sum(1 for w in wins if w) / n

        # Global win rate for comparison
        all_wins = sum(1 for rec in self._outcomes if rec.get("won", False))
        wr_global = all_wins / max(1, len(self._outcomes))

        # PnL mean in bucket vs global
        pnl_mean_bucket = sum(pnls) / n
        pnl_mean_global = (sum(r.get("pnl_pct", 0) for r in self._outcomes)
                           / max(1, len(self._outcomes)))

        # Correlation: is this bucket significantly different?
        wr_diff = wr_bucket - wr_global
        pnl_diff = pnl_mean_bucket - pnl_mean_global

        # Composite correlation measure
        composite = 0.5 * wr_diff + 0.5 * max(-1, min(1, pnl_diff / max(0.001, abs(pnl_mean_global)) if pnl_mean_global != 0 else pnl_diff))

        if abs(composite) < self.CORRELATION_THRESHOLD:
            return None

        # Confidence scales with sample size
        confidence = min(1.0, n / (self.MIN_SAMPLE_SIZE * 3)) * min(1.0, abs(composite) / 0.5)

        pattern_id = self._make_pattern_id(bucket_key)
        dim, val = bucket_key.split("=", 1) if "=" in bucket_key else (bucket_key, "")

        direction = "positive" if composite > 0 else "negative"
        action = (f"increase_exposure when {dim}={val}"
                  if composite > 0
                  else f"reduce_exposure when {dim}={val}")

        return LearnedPattern(
            pattern_id=pattern_id,
            source=LearningSource.PARAMETER_OUTCOME,
            description=f"{direction} edge in {dim}={val}: wr_diff={wr_diff:+.2%}, pnl_diff={pnl_diff:+.4f}",
            conditions={dim: val},
            outcome_correlation=composite,
            sample_size=n,
            confidence=confidence,
            actionable=confidence >= self.CONFIDENCE_THRESHOLD,
            recommended_action=action,
        )

    def _analyse_cross_dimensions(self) -> List[LearnedPattern]:
        """Find 2D cross-dimensional patterns (e.g., regime × session)."""
        patterns: List[LearnedPattern] = []
        dim_pairs: Dict[str, List[tuple]] = defaultdict(list)

        for rec in self._outcomes:
            dims_present = [(d, rec[d]) for d in self.CONTEXT_DIMENSIONS if d in rec]
            # Pair-wise combinations
            for i in range(len(dims_present)):
                for j in range(i + 1, len(dims_present)):
                    key = f"{dims_present[i][0]}={dims_present[i][1]}__x__{dims_present[j][0]}={dims_present[j][1]}"
                    dim_pairs[key].append((rec.get("pnl_pct", 0), rec.get("won", False)))

        for key, records in dim_pairs.items():
            if len(records) < self.MIN_SAMPLE_SIZE:
                continue
            pnls = [r[0] for r in records]
            wins = [r[1] for r in records]
            n = len(records)

            wr = sum(1 for w in wins if w) / n
            wr_global = (sum(1 for r in self._outcomes if r.get("won", False))
                         / max(1, len(self._outcomes)))
            pnl_mean = sum(pnls) / n

            wr_diff = wr - wr_global
            if abs(wr_diff) < self.CORRELATION_THRESHOLD:
                continue

            confidence = min(1.0, n / (self.MIN_SAMPLE_SIZE * 3)) * min(1.0, abs(wr_diff) / 0.5)
            if confidence < 0.4:
                continue

            parts = key.split("__x__")
            conditions = {}
            for part in parts:
                if "=" in part:
                    d, v = part.split("=", 1)
                    conditions[d] = v

            pattern_id = self._make_pattern_id(key)
            direction = "positive" if wr_diff > 0 else "negative"

            patterns.append(LearnedPattern(
                pattern_id=pattern_id,
                source=LearningSource.PARAMETER_OUTCOME,
                description=f"cross-dim {direction} edge: {key} wr_diff={wr_diff:+.2%}",
                conditions=conditions,
                outcome_correlation=wr_diff,
                sample_size=n,
                confidence=confidence,
                actionable=confidence >= self.CONFIDENCE_THRESHOLD,
                recommended_action=f"{'increase' if wr_diff > 0 else 'reduce'}_exposure when {' AND '.join(f'{k}={v}' for k, v in conditions.items())}",
            ))

        return patterns

    @staticmethod
    def _make_pattern_id(key: str) -> str:
        return "pat_" + hashlib.md5(key.encode()).hexdigest()[:12]
