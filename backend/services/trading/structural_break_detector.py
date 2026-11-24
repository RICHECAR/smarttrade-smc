"""
Structural Break Detector
═════════════════════════

Detects abrupt changes in market behaviour BEFORE rolling performance
metrics fully degrade.  Uses z-score deviation from rolling baselines
to fire early-warning break signals.

Break types detected:
  1. Volatility regime shock    — vol jumps > 2.5σ from 20-period mean
  2. Liquidity deterioration    — volume drops > 2.0σ
  3. Spread expansion shock     — spread widens > 2.5σ
  4. Execution condition break  — fill quality / latency degrades > 2.0σ
  5. Correlation regime break   — pairwise correlations shift > 2.0σ
  6. Opportunity quality collapse — edge quality drops > 2.0σ

Each break carries severity (NONE → CRITICAL), confidence, z-score,
and lists of affected symbols / setups.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import math
import statistics
from collections import deque

from backend.services.trading.structural_break_models import (
    BreakReport,
    BreakSeverity,
    BreakSignal,
    BreakType,
)


def _z_score(value: float, history: list[float] | deque) -> float:
    """Compute z-score of value against history."""
    if len(history) < 5:
        return 0.0
    mu = statistics.mean(history)
    sd = statistics.stdev(history)
    if sd < 1e-12:
        return 0.0
    return (value - mu) / sd


def _severity_from_z(z: float, thresholds: tuple[float, float, float, float]) -> BreakSeverity:
    """Map absolute z-score to severity using (minor, moderate, severe, critical) thresholds."""
    az = abs(z)
    if az >= thresholds[3]:
        return BreakSeverity.CRITICAL
    if az >= thresholds[2]:
        return BreakSeverity.SEVERE
    if az >= thresholds[1]:
        return BreakSeverity.MODERATE
    if az >= thresholds[0]:
        return BreakSeverity.MINOR
    return BreakSeverity.NONE


_SEV_RANK = {
    BreakSeverity.NONE: 0,
    BreakSeverity.MINOR: 1,
    BreakSeverity.MODERATE: 2,
    BreakSeverity.SEVERE: 3,
    BreakSeverity.CRITICAL: 4,
}


class StructuralBreakDetector:
    """Detect abrupt structural changes across 6 market dimensions."""

    WINDOW = 20  # rolling baseline length

    # z-score thresholds: (minor, moderate, severe, critical)
    Z_VOL        = (1.5, 2.0, 2.5, 3.5)
    Z_LIQUIDITY  = (1.5, 2.0, 2.5, 3.0)
    Z_SPREAD     = (1.5, 2.0, 2.5, 3.5)
    Z_EXECUTION  = (1.5, 2.0, 2.5, 3.0)
    Z_CORRELATION = (1.5, 2.0, 2.5, 3.0)
    Z_OPPORTUNITY = (1.5, 2.0, 2.5, 3.0)

    def __init__(self) -> None:
        # Rolling histories per symbol keyed by (symbol, metric_name)
        self._histories: dict[tuple[str, str], deque] = {}
        self._last_report: BreakReport | None = None

    # ────────────────────────────────────────────────────
    #  Data ingestion
    # ────────────────────────────────────────────────────

    def ingest(
        self,
        symbol: str,
        *,
        volatility: float | None = None,
        volume: float | None = None,
        spread: float | None = None,
        fill_quality: float | None = None,
        latency_ms: float | None = None,
        correlation_shift: float | None = None,
        edge_quality: float | None = None,
    ) -> None:
        """Feed latest market observations for one symbol."""
        if volatility is not None:
            self._push(symbol, "vol", volatility)
        if volume is not None:
            self._push(symbol, "volume", volume)
        if spread is not None:
            self._push(symbol, "spread", spread)
        if fill_quality is not None:
            self._push(symbol, "fill_q", fill_quality)
        if latency_ms is not None:
            self._push(symbol, "latency", latency_ms)
        if correlation_shift is not None:
            self._push(symbol, "corr", correlation_shift)
        if edge_quality is not None:
            self._push(symbol, "edge", edge_quality)

    # ────────────────────────────────────────────────────
    #  Detection
    # ────────────────────────────────────────────────────

    def evaluate(self, symbols: list[str] | None = None) -> BreakReport:
        """
        Scan all ingested symbols (or a subset) for structural breaks.

        Returns a BreakReport with all detected breaks.
        """
        if symbols is None:
            symbols = list({k[0] for k in self._histories})

        breaks: list[BreakSignal] = []

        for sym in symbols:
            breaks.extend(self._check_symbol(sym))

        # Compute aggregate
        max_sev = BreakSeverity.NONE
        for b in breaks:
            if _SEV_RANK[b.severity] > _SEV_RANK[max_sev]:
                max_sev = b.severity

        active = [b for b in breaks if b.severity != BreakSeverity.NONE]
        composite = self._composite_stress(active)

        report = BreakReport(
            breaks=breaks,
            max_severity=max_sev,
            composite_stress=composite,
            active_break_count=len(active),
        )
        self._last_report = report
        return report

    def get_last_report(self) -> BreakReport | None:
        return self._last_report

    # ────────────────────────────────────────────────────
    #  Internal — per-symbol checks
    # ────────────────────────────────────────────────────

    def _check_symbol(self, symbol: str) -> list[BreakSignal]:
        signals: list[BreakSignal] = []

        # 1. Volatility shock — high vol is bad
        signals.append(self._check_metric(
            symbol, "vol", BreakType.VOLATILITY_SHOCK, self.Z_VOL, positive_is_bad=True,
        ))

        # 2. Liquidity drop — low volume is bad (invert z)
        signals.append(self._check_metric(
            symbol, "volume", BreakType.LIQUIDITY_DETERIORATION, self.Z_LIQUIDITY, positive_is_bad=False,
        ))

        # 3. Spread expansion — high spread is bad
        signals.append(self._check_metric(
            symbol, "spread", BreakType.SPREAD_EXPANSION, self.Z_SPREAD, positive_is_bad=True,
        ))

        # 4. Execution break — low fill quality is bad (note: also check latency)
        sig_fq = self._check_metric(
            symbol, "fill_q", BreakType.EXECUTION_BREAK, self.Z_EXECUTION, positive_is_bad=False,
        )
        sig_lat = self._check_metric(
            symbol, "latency", BreakType.EXECUTION_BREAK, self.Z_EXECUTION, positive_is_bad=True,
        )
        # Take the worse of the two
        if _SEV_RANK.get(sig_lat.severity, 0) > _SEV_RANK.get(sig_fq.severity, 0):
            signals.append(sig_lat)
        else:
            signals.append(sig_fq)

        # 5. Correlation break — high shift is bad
        signals.append(self._check_metric(
            symbol, "corr", BreakType.CORRELATION_BREAK, self.Z_CORRELATION, positive_is_bad=True,
        ))

        # 6. Opportunity collapse — low edge is bad
        signals.append(self._check_metric(
            symbol, "edge", BreakType.OPPORTUNITY_COLLAPSE, self.Z_OPPORTUNITY, positive_is_bad=False,
        ))

        return [s for s in signals if s.severity != BreakSeverity.NONE]

    def _check_metric(
        self,
        symbol: str,
        metric: str,
        break_type: BreakType,
        thresholds: tuple[float, float, float, float],
        *,
        positive_is_bad: bool,
    ) -> BreakSignal:
        hist = self._get_history(symbol, metric)
        if len(hist) < 5:
            return BreakSignal(break_type=break_type, severity=BreakSeverity.NONE)

        current = hist[-1]
        baseline = list(hist)[:-1]  # all except current
        z = _z_score(current, baseline)

        # Direction: if positive_is_bad, positive z means break;
        # if not positive_is_bad, negative z means break
        directional_z = z if positive_is_bad else -z
        severity = _severity_from_z(directional_z, thresholds) if directional_z > 0 else BreakSeverity.NONE
        confidence = min(1.0, abs(directional_z) / thresholds[3]) if thresholds[3] > 0 else 0.0

        return BreakSignal(
            break_type=break_type,
            severity=severity,
            confidence=round(confidence, 4),
            z_score=round(z, 4),
            affected_symbols=[symbol],
            detail=f"{metric} z={z:.2f} val={current:.4f}",
        )

    # ────────────────────────────────────────────────────
    #  Composite stress
    # ────────────────────────────────────────────────────

    @staticmethod
    def _composite_stress(active_breaks: list[BreakSignal]) -> float:
        if not active_breaks:
            return 0.0
        weights = {
            BreakSeverity.MINOR: 0.15,
            BreakSeverity.MODERATE: 0.35,
            BreakSeverity.SEVERE: 0.65,
            BreakSeverity.CRITICAL: 1.00,
        }
        total = sum(weights.get(b.severity, 0) * b.confidence for b in active_breaks)
        # Normalize: max stress = multiple critical breaks
        return min(1.0, total / max(1, len(active_breaks)) + 0.1 * len(active_breaks))

    # ────────────────────────────────────────────────────
    #  History management
    # ────────────────────────────────────────────────────

    def _push(self, symbol: str, metric: str, value: float) -> None:
        key = (symbol, metric)
        if key not in self._histories:
            self._histories[key] = deque(maxlen=self.WINDOW + 1)
        self._histories[key].append(value)

    def _get_history(self, symbol: str, metric: str) -> deque:
        return self._histories.get((symbol, metric), deque())
