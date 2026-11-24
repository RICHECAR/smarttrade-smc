"""
Regime Transition Monitor
═════════════════════════

Detects unstable or transitional market states — not just stable regimes,
but the dangerous in-between phases where setups are most fragile.

Transition types:
  1. Trend exhaustion          — momentum decaying, reversal risk rising
  2. Range-to-breakout         — compression narrowing, breakout imminent
  3. Breakout failure          — breakout attempted then rejected
  4. Compression-to-expansion  — volatility expanding from low base
  5. Unstable mixed            — no clear regime, high uncertainty

Uses rolling metrics (momentum, range width, volatility slope) to score
transitions and estimate fragility impact on active setups.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import statistics
from collections import deque

from backend.services.trading.structural_break_models import (
    TransitionReport,
    TransitionSignal,
    TransitionType,
)


class RegimeTransitionMonitor:
    """Detect regime transitions and estimate fragility impact."""

    WINDOW = 20

    # Thresholds for transition detection
    MOMENTUM_DECAY_THRESHOLD = 0.3      # momentum drops below 30% of peak
    RANGE_NARROW_THRESHOLD = 0.4        # range narrows to 40% of recent avg
    BREAKOUT_FAIL_RETRACE = 0.7         # price retraces 70% of breakout move
    VOL_EXPANSION_RATIO = 2.0           # vol doubles from recent low
    MIXED_REGIME_ENTROPY = 0.6          # regime scoring entropy threshold

    def __init__(self) -> None:
        self._momentum_hist: dict[str, deque] = {}
        self._range_hist: dict[str, deque] = {}
        self._vol_hist: dict[str, deque] = {}
        self._breakout_flags: dict[str, float] = {}   # symbol → breakout price
        self._regime_scores: dict[str, deque] = {}     # symbol → regime clarity
        self._last_report: TransitionReport | None = None

    # ────────────────────────────────────────────────────
    #  Data ingestion
    # ────────────────────────────────────────────────────

    def ingest(
        self,
        symbol: str,
        *,
        momentum: float | None = None,
        range_width: float | None = None,
        volatility: float | None = None,
        breakout_price: float | None = None,
        current_price: float | None = None,
        regime_clarity: float | None = None,
    ) -> None:
        """Feed latest regime-related observations for one symbol."""
        if momentum is not None:
            self._push(self._momentum_hist, symbol, momentum)
        if range_width is not None:
            self._push(self._range_hist, symbol, range_width)
        if volatility is not None:
            self._push(self._vol_hist, symbol, volatility)
        if breakout_price is not None:
            self._breakout_flags[symbol] = breakout_price
        if current_price is not None and symbol in self._breakout_flags:
            bp = self._breakout_flags[symbol]
            move = abs(current_price - bp)
            retrace = abs(bp - current_price) / max(move, 1e-12) if move > 0 else 0
            # Store retrace ratio for breakout failure detection
            self._push(self._range_hist, f"{symbol}_retrace", retrace)
        if regime_clarity is not None:
            self._push(self._regime_scores, symbol, regime_clarity)

    # ────────────────────────────────────────────────────
    #  Detection
    # ────────────────────────────────────────────────────

    def evaluate(self, symbols: list[str] | None = None) -> TransitionReport:
        """Scan symbols for regime transitions."""
        if symbols is None:
            all_syms = set()
            for d in (self._momentum_hist, self._range_hist, self._vol_hist, self._regime_scores):
                all_syms.update(k for k in d if not k.endswith("_retrace"))
            symbols = list(all_syms)

        transitions: list[TransitionSignal] = []
        for sym in symbols:
            transitions.extend(self._check_symbol(sym))

        is_unstable = any(t.confidence > 0.5 for t in transitions)
        max_frag = max((t.fragility_impact for t in transitions), default=0.0)

        report = TransitionReport(
            transitions=transitions,
            is_unstable=is_unstable,
            max_fragility=max_frag,
            transition_count=len(transitions),
        )
        self._last_report = report
        return report

    def get_last_report(self) -> TransitionReport | None:
        return self._last_report

    # ────────────────────────────────────────────────────
    #  Per-symbol checks
    # ────────────────────────────────────────────────────

    def _check_symbol(self, symbol: str) -> list[TransitionSignal]:
        signals: list[TransitionSignal] = []

        # 1. Trend exhaustion — momentum decaying
        mom = self._get(self._momentum_hist, symbol)
        if len(mom) >= 5:
            peak_mom = max(abs(v) for v in mom)
            current_mom = abs(mom[-1])
            if peak_mom > 0 and current_mom / peak_mom < self.MOMENTUM_DECAY_THRESHOLD:
                ratio = current_mom / peak_mom
                confidence = min(1.0, (self.MOMENTUM_DECAY_THRESHOLD - ratio) / self.MOMENTUM_DECAY_THRESHOLD)
                signals.append(TransitionSignal(
                    transition_type=TransitionType.TREND_EXHAUSTION,
                    confidence=round(confidence, 4),
                    fragility_impact=round(0.5 + 0.5 * confidence, 4),
                    source_regime="trending",
                    target_regime="exhaustion",
                    detail=f"momentum={current_mom:.4f} peak={peak_mom:.4f} ratio={ratio:.2f}",
                ))

        # 2. Range-to-breakout — range narrowing
        rng = self._get(self._range_hist, symbol)
        if len(rng) >= 5:
            avg_range = statistics.mean(list(rng)[:-1])
            current_range = rng[-1]
            if avg_range > 0 and current_range / avg_range < self.RANGE_NARROW_THRESHOLD:
                ratio = current_range / avg_range
                confidence = min(1.0, (self.RANGE_NARROW_THRESHOLD - ratio) / self.RANGE_NARROW_THRESHOLD)
                signals.append(TransitionSignal(
                    transition_type=TransitionType.RANGE_TO_BREAKOUT,
                    confidence=round(confidence, 4),
                    fragility_impact=round(0.3 + 0.4 * confidence, 4),
                    source_regime="ranging",
                    target_regime="pre_breakout",
                    detail=f"range={current_range:.4f} avg={avg_range:.4f} ratio={ratio:.2f}",
                ))

        # 3. Breakout failure — retrace exceeds threshold
        retrace_hist = self._get(self._range_hist, f"{symbol}_retrace")
        if retrace_hist and retrace_hist[-1] > self.BREAKOUT_FAIL_RETRACE:
            ret = retrace_hist[-1]
            confidence = min(1.0, (ret - self.BREAKOUT_FAIL_RETRACE) / (1.0 - self.BREAKOUT_FAIL_RETRACE + 1e-9))
            signals.append(TransitionSignal(
                transition_type=TransitionType.BREAKOUT_FAILURE,
                confidence=round(confidence, 4),
                fragility_impact=round(0.6 + 0.4 * confidence, 4),
                source_regime="breakout",
                target_regime="failed_breakout",
                detail=f"retrace={ret:.2f}",
            ))

        # 4. Compression-to-expansion — vol expanding from low base
        vol = self._get(self._vol_hist, symbol)
        if len(vol) >= 5:
            vol_list = list(vol)
            recent_low = min(vol_list[:-1])
            current_vol = vol_list[-1]
            if recent_low > 0 and current_vol / recent_low > self.VOL_EXPANSION_RATIO:
                ratio = current_vol / recent_low
                confidence = min(1.0, (ratio - self.VOL_EXPANSION_RATIO) / self.VOL_EXPANSION_RATIO)
                signals.append(TransitionSignal(
                    transition_type=TransitionType.COMPRESSION_TO_EXPANSION,
                    confidence=round(confidence, 4),
                    fragility_impact=round(0.4 + 0.4 * confidence, 4),
                    source_regime="low_volatility",
                    target_regime="expanding",
                    detail=f"vol={current_vol:.4f} low={recent_low:.4f} ratio={ratio:.1f}x",
                ))

        # 5. Unstable mixed — regime clarity too low
        clarity = self._get(self._regime_scores, symbol)
        if clarity and clarity[-1] < self.MIXED_REGIME_ENTROPY:
            c = clarity[-1]
            confidence = min(1.0, (self.MIXED_REGIME_ENTROPY - c) / self.MIXED_REGIME_ENTROPY)
            signals.append(TransitionSignal(
                transition_type=TransitionType.UNSTABLE_MIXED,
                confidence=round(confidence, 4),
                fragility_impact=round(0.3 + 0.5 * confidence, 4),
                source_regime="unknown",
                target_regime="mixed",
                detail=f"regime_clarity={c:.2f}",
            ))

        return signals

    # ────────────────────────────────────────────────────
    #  Helpers
    # ────────────────────────────────────────────────────

    def _push(self, store: dict[str, deque], key: str, value: float) -> None:
        if key not in store:
            store[key] = deque(maxlen=self.WINDOW + 1)
        store[key].append(value)

    @staticmethod
    def _get(store: dict[str, deque], key: str) -> deque:
        return store.get(key, deque())
