"""
Timing Precision Engine
═══════════════════════

Improves entry timing quality to maximize asymmetry capture.

Detects:
  • Optimal entry windows   — confirmation + timing alignment
  • Late entries            — move already expressed, risk > reward
  • Early entries           — insufficient confirmation, noise risk

Scoring:
  timing_score = weighted(confirmation, recency, momentum_phase, volume_conf)

Verdict:
  ENTER — optimal window, high confirmation
  WAIT  — conditions building but not confirmed
  SKIP  — too late, too early, or misaligned

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import statistics
from collections import deque

from backend.services.trading.opportunity_dominance_models import (
    TimingResult,
    TimingVerdict,
)


class TimingPrecisionEngine:
    """Determine optimal entry timing for opportunity exploitation."""

    WINDOW = 15

    # Thresholds
    ENTER_THRESHOLD = 0.65          # timing_score ≥ 0.65 → ENTER
    SKIP_LATE_THRESHOLD = 0.70     # move already > 70% expressed → SKIP
    SKIP_EARLY_THRESHOLD = 0.20    # confirmation < 20% → SKIP (too early)
    OPTIMAL_RECENCY = 3            # bars since signal for optimal window

    def __init__(self) -> None:
        self._momentum_hist: dict[str, deque] = {}
        self._volume_hist: dict[str, deque] = {}
        self._price_hist: dict[str, deque] = {}
        self._last_result: TimingResult | None = None

    def ingest(
        self,
        symbol: str,
        *,
        price: float = 0.0,
        momentum: float = 0.0,
        volume: float = 0.0,
    ) -> None:
        """Feed latest data for timing analysis."""
        self._push(self._price_hist, symbol, price)
        self._push(self._momentum_hist, symbol, momentum)
        self._push(self._volume_hist, symbol, volume)

    def evaluate(
        self,
        symbol: str,
        *,
        opportunity_score: float = 0.0,
        signal_age_bars: int = 0,           # bars since opportunity detected
        move_expressed_pct: float = 0.0,    # 0–1: how much of the move already happened
        confirmation_count: int = 0,         # number of confirming factors
        max_confirmations: int = 4,          # max possible confirmations
    ) -> TimingResult:
        """
        Evaluate entry timing quality.

        Returns TimingResult with score, verdict, and risk decomposition.
        """
        # 1. Confirmation strength (0–1)
        conf_strength = confirmation_count / max(1, max_confirmations)

        # 2. Recency score: closer to optimal age = better
        if signal_age_bars <= 0:
            recency = 0.3  # just detected, needs 1 bar confirmation
        elif signal_age_bars <= self.OPTIMAL_RECENCY:
            recency = 1.0 - (signal_age_bars - 1) * 0.1
        else:
            recency = max(0.0, 1.0 - (signal_age_bars - self.OPTIMAL_RECENCY) * 0.15)

        # 3. Momentum phase score
        mom_hist = self._get(self._momentum_hist, symbol)
        if len(mom_hist) >= 3:
            recent_mom = list(mom_hist)[-3:]
            # Accelerating momentum = good
            if recent_mom[-1] > recent_mom[-2] > recent_mom[-3]:
                mom_phase = 0.9
            elif recent_mom[-1] > recent_mom[-2]:
                mom_phase = 0.7
            elif recent_mom[-1] > 0:
                mom_phase = 0.5
            else:
                mom_phase = 0.2
        else:
            mom_phase = 0.5

        # 4. Volume confirmation
        vol_hist = self._get(self._volume_hist, symbol)
        if len(vol_hist) >= 5:
            baseline_vol = statistics.mean(list(vol_hist)[-6:-1])
            current_vol = vol_hist[-1]
            vol_conf = min(1.0, current_vol / max(1e-12, baseline_vol))
        else:
            vol_conf = 0.5

        # Composite timing score
        timing_score = (
            0.30 * conf_strength
            + 0.25 * recency
            + 0.25 * mom_phase
            + 0.20 * vol_conf
        )
        timing_score = max(0.0, min(1.0, timing_score))

        # Late entry risk
        late_risk = move_expressed_pct
        # Early entry risk
        early_risk = max(0.0, 1.0 - conf_strength)

        # Optimal window check
        optimal = (
            signal_age_bars >= 1
            and signal_age_bars <= self.OPTIMAL_RECENCY
            and conf_strength >= 0.5
            and move_expressed_pct < 0.50
        )

        # Verdict
        if move_expressed_pct >= self.SKIP_LATE_THRESHOLD:
            verdict = TimingVerdict.SKIP
            detail = f"late_entry: move {move_expressed_pct:.0%} expressed"
        elif conf_strength < self.SKIP_EARLY_THRESHOLD:
            verdict = TimingVerdict.SKIP
            detail = f"too_early: confirmation={conf_strength:.2f}"
        elif timing_score >= self.ENTER_THRESHOLD:
            verdict = TimingVerdict.ENTER
            detail = f"optimal_window conf={conf_strength:.2f} recency={recency:.2f}"
        else:
            verdict = TimingVerdict.WAIT
            detail = f"building: score={timing_score:.2f} conf={conf_strength:.2f}"

        result = TimingResult(
            timing_score=round(timing_score, 4),
            verdict=verdict,
            optimal_window=optimal,
            late_entry_risk=round(late_risk, 4),
            early_entry_risk=round(early_risk, 4),
            confirmation_strength=round(conf_strength, 4),
            detail=detail,
        )
        self._last_result = result
        return result

    def get_last_result(self) -> TimingResult | None:
        return self._last_result

    def _push(self, store: dict[str, deque], symbol: str, value: float) -> None:
        if symbol not in store:
            store[symbol] = deque(maxlen=self.WINDOW + 5)
        store[symbol].append(value)

    @staticmethod
    def _get(store: dict[str, deque], symbol: str) -> deque:
        return store.get(symbol, deque())
