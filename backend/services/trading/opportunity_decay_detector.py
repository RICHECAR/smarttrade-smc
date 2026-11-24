"""
Opportunity Decay Detector
══════════════════════════

Detects when a strong opportunity is fading BEFORE PnL degrades.

Monitors 3 decay signals:
  1. Momentum slowdown  — momentum decelerating vs initial impulse
  2. Volume drop        — participation declining
  3. Failed continuation — price failing to make new highs/lows

Decay stages:
  NONE        — opportunity still fresh
  EARLY       — first signs of fading (1/3 signals)
  PROGRESSING — clear decay (2/3 signals)
  TERMINAL    — opportunity dead (3/3 or extreme single)

Early exit signal fires when decay_score ≥ 0.65.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import statistics
from collections import deque

from backend.services.trading.opportunity_dominance_models import (
    DecayResult,
    DecayStage,
)


class OpportunityDecayDetector:
    """Detect fading opportunities before PnL impact."""

    WINDOW = 15

    # Thresholds
    MOMENTUM_DECAY_THRESHOLD = 0.40     # momentum dropped > 40% from peak
    VOLUME_DROP_THRESHOLD = 0.35        # volume dropped > 35% from avg
    CONTINUATION_FAIL_BARS = 4          # no new high/low for N bars → fail
    EARLY_EXIT_THRESHOLD = 0.65         # decay_score ≥ 0.65 → exit signal

    def __init__(self) -> None:
        self._momentum_hist: dict[str, deque] = {}
        self._volume_hist: dict[str, deque] = {}
        self._price_hist: dict[str, deque] = {}
        self._peak_momentum: dict[str, float] = {}
        self._bars_since_extreme: dict[str, int] = {}
        self._last_result: dict[str, DecayResult] = {}

    def ingest(
        self,
        symbol: str,
        *,
        price: float = 0.0,
        momentum: float = 0.0,
        volume: float = 0.0,
        direction: int = 1,         # 1=long, -1=short
    ) -> None:
        """Feed latest data for decay analysis."""
        self._push(self._momentum_hist, symbol, abs(momentum))
        self._push(self._volume_hist, symbol, volume)
        self._push(self._price_hist, symbol, price)

        # Track peak momentum
        abs_mom = abs(momentum)
        if abs_mom > self._peak_momentum.get(symbol, 0):
            self._peak_momentum[symbol] = abs_mom

        # Track bars since new extreme
        prices = self._get(self._price_hist, symbol)
        if len(prices) >= 2:
            is_new_extreme = (
                (direction == 1 and price >= max(list(prices)[:-1]))
                or (direction == -1 and price <= min(list(prices)[:-1]))
            )
            if is_new_extreme:
                self._bars_since_extreme[symbol] = 0
            else:
                self._bars_since_extreme[symbol] = self._bars_since_extreme.get(symbol, 0) + 1

    def evaluate(self, symbol: str) -> DecayResult:
        """
        Evaluate opportunity decay status.

        Returns DecayResult with decay_score, stage, and exit signal.
        """
        mom_hist = self._get(self._momentum_hist, symbol)
        vol_hist = self._get(self._volume_hist, symbol)

        # 1. Momentum slowdown
        peak_mom = self._peak_momentum.get(symbol, 0)
        current_mom = mom_hist[-1] if mom_hist else 0
        if peak_mom > 1e-12:
            mom_decay_pct = 1.0 - (current_mom / peak_mom)
        else:
            mom_decay_pct = 0.0
        momentum_fading = mom_decay_pct >= self.MOMENTUM_DECAY_THRESHOLD

        # 2. Volume drop
        if len(vol_hist) >= 5:
            avg_vol = statistics.mean(list(vol_hist)[-6:-1])
            current_vol = vol_hist[-1]
            if avg_vol > 1e-12:
                vol_drop_pct = max(0.0, 1.0 - current_vol / avg_vol)
            else:
                vol_drop_pct = 0.0
        else:
            vol_drop_pct = 0.0
        volume_dropping = vol_drop_pct >= self.VOLUME_DROP_THRESHOLD

        # 3. Failed continuation
        bars_no_extreme = self._bars_since_extreme.get(symbol, 0)
        continuation_failed = bars_no_extreme >= self.CONTINUATION_FAIL_BARS

        # Composite decay score (0–1)
        signals_count = sum([momentum_fading, volume_dropping, continuation_failed])

        # Weighted score
        decay_score = (
            0.40 * min(1.0, mom_decay_pct / 0.8)
            + 0.30 * min(1.0, vol_drop_pct / 0.7)
            + 0.30 * min(1.0, bars_no_extreme / (self.CONTINUATION_FAIL_BARS * 2))
        )
        decay_score = max(0.0, min(1.0, decay_score))

        # Stage
        if signals_count >= 3 or decay_score >= 0.80:
            stage = DecayStage.TERMINAL
        elif signals_count >= 2 or decay_score >= 0.50:
            stage = DecayStage.PROGRESSING
        elif signals_count >= 1 or decay_score >= 0.25:
            stage = DecayStage.EARLY
        else:
            stage = DecayStage.NONE

        early_exit = decay_score >= self.EARLY_EXIT_THRESHOLD

        detail = (
            f"mom_decay={mom_decay_pct:.2f} vol_drop={vol_drop_pct:.2f} "
            f"bars_no_extreme={bars_no_extreme} signals={signals_count}/3"
        )

        result = DecayResult(
            decay_score=round(decay_score, 4),
            stage=stage,
            early_exit_signal=early_exit,
            momentum_fading=momentum_fading,
            volume_dropping=volume_dropping,
            continuation_failed=continuation_failed,
            detail=detail,
        )
        self._last_result[symbol] = result
        return result

    def reset(self, symbol: str) -> None:
        """Reset tracking for a symbol (e.g., after new entry)."""
        self._peak_momentum.pop(symbol, None)
        self._bars_since_extreme.pop(symbol, None)
        for store in (self._momentum_hist, self._volume_hist, self._price_hist):
            store.pop(symbol, None)
        self._last_result.pop(symbol, None)

    def get_last_result(self, symbol: str) -> DecayResult | None:
        return self._last_result.get(symbol)

    def _push(self, store: dict[str, deque], symbol: str, value: float) -> None:
        if symbol not in store:
            store[symbol] = deque(maxlen=self.WINDOW + 5)
        store[symbol].append(value)

    @staticmethod
    def _get(store: dict[str, deque], symbol: str) -> deque:
        return store.get(symbol, deque())
