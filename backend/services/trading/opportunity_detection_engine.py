"""
Opportunity Detection Engine
═════════════════════════════

Detects high-asymmetry situations BEFORE they become obvious.
Scans 5 opportunity types per symbol:

  1. Liquidity Sweep      — price hunted below/above key levels then reversed
  2. Volatility Expansion  — compressed vol starting to break out
  3. Breakout Compression  — tight range near key level, energy building
  4. Momentum Ignition     — sudden volume + directional acceleration
  5. Imbalance Zone        — unfilled institutional order flow imbalance

Each signal carries opportunity_score (0–1), expected_asymmetry (R:R),
timing_quality, and confidence.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import statistics
from collections import deque

from backend.services.trading.opportunity_dominance_models import (
    DetectionReport,
    OpportunitySignal,
    OpportunityType,
)


class OpportunityDetectionEngine:
    """Detect high-asymmetry opportunities before market expression."""

    WINDOW = 20

    # Per-type detection thresholds
    SWEEP_WICK_RATIO = 0.60        # wick > 60% of candle = potential sweep
    VOL_EXPANSION_MULT = 1.80      # vol expanding > 1.8x baseline
    COMPRESSION_RATIO = 0.40       # range < 40% of avg = compression
    IGNITION_VOL_MULT = 2.50       # volume spike > 2.5x
    IMBALANCE_RATIO = 0.65         # buy/sell ratio threshold

    def __init__(self) -> None:
        self._price_hist: dict[str, deque] = {}
        self._high_hist: dict[str, deque] = {}
        self._low_hist: dict[str, deque] = {}
        self._vol_hist: dict[str, deque] = {}      # volatility
        self._volume_hist: dict[str, deque] = {}    # volume
        self._range_hist: dict[str, deque] = {}     # high - low
        self._buy_vol_hist: dict[str, deque] = {}
        self._sell_vol_hist: dict[str, deque] = {}
        self._last_report: DetectionReport | None = None

    # ──────────────────────────────────────────────────
    #  Data ingestion
    # ──────────────────────────────────────────────────

    def ingest(
        self,
        symbol: str,
        *,
        price: float = 0.0,
        high: float = 0.0,
        low: float = 0.0,
        close: float = 0.0,
        volatility: float = 0.0,
        volume: float = 0.0,
        candle_range: float = 0.0,
        buy_volume: float = 0.0,
        sell_volume: float = 0.0,
        wick_upper: float = 0.0,
        wick_lower: float = 0.0,
    ) -> None:
        """Feed latest market data for one symbol."""
        self._push(self._price_hist, symbol, close if close else price)
        self._push(self._high_hist, symbol, high)
        self._push(self._low_hist, symbol, low)
        self._push(self._vol_hist, symbol, volatility)
        self._push(self._volume_hist, symbol, volume)
        rng = candle_range if candle_range else (high - low) if (high and low) else 0.0
        self._push(self._range_hist, symbol, rng)
        self._push(self._buy_vol_hist, symbol, buy_volume)
        self._push(self._sell_vol_hist, symbol, sell_volume)

    # ──────────────────────────────────────────────────
    #  Detection
    # ──────────────────────────────────────────────────

    def detect(self, symbols: list[str] | None = None) -> DetectionReport:
        """Scan all symbols for high-asymmetry opportunities."""
        if symbols is None:
            symbols = list(self._price_hist.keys())

        all_signals: list[OpportunitySignal] = []

        for sym in symbols:
            all_signals.extend(self._scan_symbol(sym))

        # Sort by opportunity_score descending
        all_signals.sort(key=lambda s: s.opportunity_score, reverse=True)

        max_score = all_signals[0].opportunity_score if all_signals else 0.0
        avg_asym = (
            statistics.mean(s.expected_asymmetry for s in all_signals)
            if all_signals else 0.0
        )

        report = DetectionReport(
            opportunities=all_signals,
            total_detected=len(all_signals),
            max_score=max_score,
            avg_asymmetry=round(avg_asym, 4),
        )
        self._last_report = report
        return report

    def get_last_report(self) -> DetectionReport | None:
        return self._last_report

    # ──────────────────────────────────────────────────
    #  Per-symbol scanning
    # ──────────────────────────────────────────────────

    def _scan_symbol(self, symbol: str) -> list[OpportunitySignal]:
        signals = []

        sig = self._detect_liquidity_sweep(symbol)
        if sig:
            signals.append(sig)

        sig = self._detect_volatility_expansion(symbol)
        if sig:
            signals.append(sig)

        sig = self._detect_breakout_compression(symbol)
        if sig:
            signals.append(sig)

        sig = self._detect_momentum_ignition(symbol)
        if sig:
            signals.append(sig)

        sig = self._detect_imbalance_zone(symbol)
        if sig:
            signals.append(sig)

        return signals

    def _detect_liquidity_sweep(self, symbol: str) -> OpportunitySignal | None:
        """Detect price hunting below/above key levels then reversal."""
        ranges = self._get(self._range_hist, symbol)
        lows = self._get(self._low_hist, symbol)
        prices = self._get(self._price_hist, symbol)
        if len(ranges) < 5 or len(lows) < 5:
            return None

        current_range = ranges[-1]
        if current_range < 1e-12:
            return None

        # Check for lower wick sweep: price went below recent low then reversed
        recent_low = min(list(lows)[-6:-1]) if len(lows) > 5 else lows[-2]
        current_low = lows[-1]
        current_close = prices[-1]

        if current_low < recent_low and current_close > recent_low:
            # Sweep detected — price went below then recovered
            sweep_depth = (recent_low - current_low) / current_range if current_range > 0 else 0
            if sweep_depth > 0.1:
                confidence = min(1.0, sweep_depth / self.SWEEP_WICK_RATIO)
                asymmetry = 2.0 + confidence * 2.0  # 2:1 to 4:1 R:R
                score = min(1.0, 0.4 + confidence * 0.6)
                return OpportunitySignal(
                    symbol=symbol,
                    opportunity_type=OpportunityType.LIQUIDITY_SWEEP,
                    opportunity_score=round(score, 4),
                    expected_asymmetry=round(asymmetry, 2),
                    timing_quality=round(0.7 + confidence * 0.3, 4),
                    confidence=round(confidence, 4),
                    detail=f"sweep_depth={sweep_depth:.2f} below_recent_low",
                )
        return None

    def _detect_volatility_expansion(self, symbol: str) -> OpportunitySignal | None:
        """Detect volatility breaking out of compressed state."""
        vols = self._get(self._vol_hist, symbol)
        if len(vols) < 10:
            return None

        baseline = list(vols)[-11:-1]
        current_vol = vols[-1]
        avg_vol = statistics.mean(baseline) if baseline else 0
        if avg_vol < 1e-12:
            return None

        expansion = current_vol / avg_vol

        # Was vol recently compressed? (avg of last 5 < 80% of avg of last 10)
        recent_avg = statistics.mean(list(vols)[-6:-1]) if len(vols) > 5 else avg_vol
        was_compressed = recent_avg < avg_vol * 0.80

        if expansion >= self.VOL_EXPANSION_MULT and was_compressed:
            confidence = min(1.0, (expansion - 1.0) / 3.0)
            asymmetry = 1.5 + confidence * 2.5
            score = min(1.0, 0.3 + confidence * 0.7)
            return OpportunitySignal(
                symbol=symbol,
                opportunity_type=OpportunityType.VOLATILITY_EXPANSION,
                opportunity_score=round(score, 4),
                expected_asymmetry=round(asymmetry, 2),
                timing_quality=round(0.6 + confidence * 0.3, 4),
                confidence=round(confidence, 4),
                detail=f"vol_expansion={expansion:.2f}x was_compressed",
            )
        return None

    def _detect_breakout_compression(self, symbol: str) -> OpportunitySignal | None:
        """Detect tight range compression near key levels (energy building)."""
        ranges = self._get(self._range_hist, symbol)
        if len(ranges) < 10:
            return None

        baseline_ranges = list(ranges)[-11:-1]
        avg_range = statistics.mean(baseline_ranges) if baseline_ranges else 0
        if avg_range < 1e-12:
            return None

        # Last 3 candles compressed?
        recent_ranges = list(ranges)[-3:]
        recent_avg = statistics.mean(recent_ranges)
        compression = recent_avg / avg_range

        if compression <= self.COMPRESSION_RATIO:
            confidence = min(1.0, (1.0 - compression) / 0.6)
            asymmetry = 2.0 + confidence * 3.0
            score = min(1.0, 0.35 + confidence * 0.65)
            return OpportunitySignal(
                symbol=symbol,
                opportunity_type=OpportunityType.BREAKOUT_COMPRESSION,
                opportunity_score=round(score, 4),
                expected_asymmetry=round(asymmetry, 2),
                timing_quality=round(0.5 + confidence * 0.4, 4),
                confidence=round(confidence, 4),
                detail=f"compression={compression:.2f} (3bar avg vs baseline)",
            )
        return None

    def _detect_momentum_ignition(self, symbol: str) -> OpportunitySignal | None:
        """Detect sudden volume + directional acceleration."""
        volumes = self._get(self._volume_hist, symbol)
        prices = self._get(self._price_hist, symbol)
        if len(volumes) < 10 or len(prices) < 5:
            return None

        baseline_vol = list(volumes)[-11:-1]
        avg_vol = statistics.mean(baseline_vol) if baseline_vol else 0
        if avg_vol < 1e-12:
            return None

        current_vol = volumes[-1]
        vol_spike = current_vol / avg_vol

        # Directional: last 3 prices all moving same direction
        recent_prices = list(prices)[-4:]
        diffs = [recent_prices[i+1] - recent_prices[i] for i in range(len(recent_prices)-1)]
        all_up = all(d > 0 for d in diffs)
        all_down = all(d < 0 for d in diffs)
        directional = all_up or all_down

        if vol_spike >= self.IGNITION_VOL_MULT and directional:
            confidence = min(1.0, (vol_spike - 1.0) / 4.0)
            asymmetry = 1.5 + confidence * 2.0
            score = min(1.0, 0.45 + confidence * 0.55)
            return OpportunitySignal(
                symbol=symbol,
                opportunity_type=OpportunityType.MOMENTUM_IGNITION,
                opportunity_score=round(score, 4),
                expected_asymmetry=round(asymmetry, 2),
                timing_quality=round(0.8 + confidence * 0.2, 4),
                confidence=round(confidence, 4),
                detail=f"vol_spike={vol_spike:.2f}x direction={'up' if all_up else 'down'}",
            )
        return None

    def _detect_imbalance_zone(self, symbol: str) -> OpportunitySignal | None:
        """Detect unfilled institutional order flow imbalance."""
        buy_vols = self._get(self._buy_vol_hist, symbol)
        sell_vols = self._get(self._sell_vol_hist, symbol)
        if len(buy_vols) < 5 or len(sell_vols) < 5:
            return None

        total_buy = sum(list(buy_vols)[-5:])
        total_sell = sum(list(sell_vols)[-5:])
        total = total_buy + total_sell
        if total < 1e-12:
            return None

        buy_ratio = total_buy / total
        imbalance = abs(buy_ratio - 0.5) * 2  # 0 = balanced, 1 = fully one-sided

        if imbalance >= self.IMBALANCE_RATIO:
            confidence = min(1.0, imbalance)
            asymmetry = 1.5 + confidence * 2.5
            score = min(1.0, 0.3 + confidence * 0.7)
            direction = "buy_dominant" if buy_ratio > 0.5 else "sell_dominant"
            return OpportunitySignal(
                symbol=symbol,
                opportunity_type=OpportunityType.IMBALANCE_ZONE,
                opportunity_score=round(score, 4),
                expected_asymmetry=round(asymmetry, 2),
                timing_quality=round(0.6 + confidence * 0.3, 4),
                confidence=round(confidence, 4),
                detail=f"imbalance={imbalance:.2f} {direction} buy_ratio={buy_ratio:.2f}",
            )
        return None

    # ──────────────────────────────────────────────────
    #  Helpers
    # ──────────────────────────────────────────────────

    def _push(self, store: dict[str, deque], symbol: str, value: float) -> None:
        if symbol not in store:
            store[symbol] = deque(maxlen=self.WINDOW + 5)
        store[symbol].append(value)

    @staticmethod
    def _get(store: dict[str, deque], symbol: str) -> deque:
        return store.get(symbol, deque())
