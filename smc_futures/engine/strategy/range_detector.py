"""
Range Detector — Session/HTF Range Detection for Mean Reversion
════════════════════════════════════════════════════════════════
Detects consolidation ranges on HTF/MTF timeframes for:
    - Session ranges (Asia, London, NY open ranges)
    - Multi-bar consolidation zones
    - Range breakout/sweep detection

Used by AsiaWeekendStrategy for mean reversion setups.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class RangeType(Enum):
    """Type of detected range."""
    SESSION = "session"          # Daily session range (Asia, London, NY)
    CONSOLIDATION = "consolidation"  # Multi-bar consolidation
    HTF_RANGE = "htf_range"      # Higher timeframe defined range


@dataclass
class RangeLevel:
    """A detected range with high/low boundaries."""
    range_type: RangeType
    high: float
    low: float
    mid: float
    range_size: float           # Absolute size (high - low)
    range_pct: float            # Size as % of price
    start_ts: int               # Start timestamp
    end_ts: int                 # End timestamp
    num_bars: int               # Number of bars in range
    atr: float = 0.0            # ATR at time of range
    range_atr_ratio: float = 0.0  # Range size / ATR
    is_valid: bool = True
    invalidation_reason: Optional[str] = None


@dataclass
class RangeSweep:
    """Detected sweep of a range level."""
    range_ref: RangeLevel
    sweep_side: str             # "high" or "low"
    sweep_price: float          # Price that swept
    sweep_ts: int               # Timestamp of sweep
    wick_size: float            # Size of the wick
    closed_inside: bool         # Did candle close back inside range?
    is_valid_sweep: bool = True


@dataclass  
class RangeState:
    """Current state of range analysis."""
    current_range: Optional[RangeLevel] = None
    recent_sweeps: List[RangeSweep] = field(default_factory=list)
    trend_strength: float = 0.0      # 0.0 = ranging, 1.0 = strong trend
    is_ranging: bool = False
    last_update_ts: int = 0


class RangeDetector:
    """
    Detects consolidation ranges and sweeps for mean reversion strategies.
    
    Logic:
        1. Identify range boundaries (session or N-bar range)
        2. Calculate range metrics (size, ATR ratio, validity)
        3. Detect sweeps of range boundaries
        4. Track if price returns inside range (confirmation)
    
    Filters:
        - Range too small (< min_range_atr)
        - Range too old (> max_range_age)
        - HTF trend too strong (invalidates mean reversion)
    
    Dynamic Thresholds:
        - max_range_atr can be updated per-symbol via set_max_range_atr()
        - Supports asset-specific thresholds (BTC > ETH > ALT)
    """
    
    def __init__(
        self,
        lookback_bars: int = 20,
        min_range_atr: float = 0.5,      # Min range size in ATRs
        max_range_atr: float = 3.0,      # Max range (above = trending) — DEFAULT
        min_range_bars: int = 4,         # Min bars to form valid range
        max_range_age_bars: int = 50,    # Max age of range in bars
        sweep_confirm_bars: int = 3,     # Bars to confirm sweep (close inside)
        htf_trend_threshold: float = 0.7,  # Above this = too trendy
    ):
        self.lookback_bars = lookback_bars
        self._base_lookback_bars = lookback_bars  # Store original
        self.min_range_atr = min_range_atr
        self.max_range_atr = max_range_atr
        self._base_max_range_atr = max_range_atr  # Store original for logging
        self.min_range_bars = min_range_bars
        self.max_range_age_bars = max_range_age_bars
        self.sweep_confirm_bars = sweep_confirm_bars
        self.htf_trend_threshold = htf_trend_threshold
        
        # Metadata for logging
        self._current_threshold_source = "DEFAULT"
        self._last_range_ratio = 0.0
        
        # Adaptive lookback state
        self._atr_history: list = []
        self._max_atr_samples = 50
        
        self.current_state = RangeState()
        self._candle_history: List[dict] = []
    
    # ------------------------------------------------------------------ #
    #                      Dynamic Threshold Methods                      #
    # ------------------------------------------------------------------ #
    
    def set_max_range_atr(self, value: float, source: str = "DYNAMIC") -> None:
        """
        Update max_range_atr threshold dynamically.
        
        Args:
            value: New threshold (e.g., 6.0 for BTC)
            source: Label for logging (e.g., "BTC_PROFILE", "WEEKEND_RELAX")
        """
        self.max_range_atr = value
        self._current_threshold_source = source
    
    def reset_max_range_atr(self) -> None:
        """Reset to default threshold."""
        self.max_range_atr = self._base_max_range_atr
        self._current_threshold_source = "DEFAULT"
    
    def adapt_lookback(self, current_atr: float) -> int:
        """
        Dynamically adapt lookback_bars based on ATR volatility.
        
        High volatility (ATR >> avg) → shorter lookback (faster range detection)
        Low volatility (ATR << avg)  → longer lookback (stable range detection)
        
        Returns:
            New lookback value applied
        """
        from smc_futures.smc_config import SMC_CONFIG
        
        if not SMC_CONFIG.get("ADAPTIVE_RANGE_LOOKBACK_ENABLED", True):
            return self.lookback_bars
        
        if current_atr <= 0:
            return self.lookback_bars
        
        # Track ATR history
        self._atr_history.append(current_atr)
        if len(self._atr_history) > self._max_atr_samples:
            self._atr_history.pop(0)
        
        if len(self._atr_history) < 5:
            return self.lookback_bars
        
        avg_atr = sum(self._atr_history) / len(self._atr_history)
        if avg_atr <= 0:
            return self.lookback_bars
        
        # Ratio: >1 = high vol, <1 = low vol
        vol_ratio = current_atr / avg_atr
        
        min_lb = SMC_CONFIG.get("ADAPTIVE_RANGE_LOOKBACK_MIN", 8)
        max_lb = SMC_CONFIG.get("ADAPTIVE_RANGE_LOOKBACK_MAX", 30)
        
        # Inverse mapping: high vol → short lookback, low vol → long lookback
        # vol_ratio 0.5 → max_lb, vol_ratio 2.0 → min_lb
        clamped = max(0.5, min(vol_ratio, 2.0))
        normalized = (clamped - 0.5) / 1.5  # 0.0 to 1.0
        new_lb = int(max_lb - normalized * (max_lb - min_lb))
        new_lb = max(min_lb, min(new_lb, max_lb))
        
        if new_lb != self.lookback_bars:
            logger.debug(
                f"[RANGE_DETECTOR] Adaptive lookback: {self.lookback_bars} -> {new_lb}"
                f" | vol_ratio={vol_ratio:.2f} | atr={current_atr:.4f} | avg={avg_atr:.4f}"
            )
            self.lookback_bars = new_lb
        
        return new_lb
    
    def check_volatility_filter(self, current_atr: float) -> bool:
        """
        Check if current volatility is sufficient for trading.
        
        Returns:
            True if volatility is OK (trading allowed), False if market is dead.
        """
        from smc_futures.smc_config import SMC_CONFIG
        
        if not SMC_CONFIG.get("VOLATILITY_FILTER_ENABLED", True):
            return True
        
        if current_atr <= 0 or len(self._atr_history) < 10:
            return True  # Not enough data to filter
        
        avg_atr = sum(self._atr_history) / len(self._atr_history)
        if avg_atr <= 0:
            return True
        
        min_ratio = SMC_CONFIG.get("VOLATILITY_FILTER_MIN_ATR_RATIO", 0.4)
        ratio = current_atr / avg_atr
        
        if ratio < min_ratio:
            logger.info(
                f"[VOLATILITY_FILTER] DEAD_MARKET | atr={current_atr:.6f}"
                f" | avg_atr={avg_atr:.6f} | ratio={ratio:.2f} < {min_ratio}"
            )
            return False
        
        return True
    
    def get_threshold_info(self) -> dict:
        """Get current threshold metadata for logging."""
        return {
            "max_range_atr": self.max_range_atr,
            "base_threshold": self._base_max_range_atr,
            "source": self._current_threshold_source,
            "last_ratio": self._last_range_ratio,
        }
    
    def update(self, candles: List[dict], atr: float = 0.0) -> RangeState:
        """
        Update range detection with new candles.
        
        Args:
            candles: List of OHLCV candles (must have open, high, low, close, timestamp)
            atr: Current ATR value for range validation
        
        Returns:
            RangeState with current analysis
        """
        if not candles or len(candles) < self.min_range_bars:
            return self.current_state
        
        # Calculate ATR if not provided
        if atr <= 0:
            atr = self._calculate_atr(candles)
        
        # Adaptive lookback: adjust window based on volatility
        self.adapt_lookback(atr)
        
        self._candle_history = candles[-self.lookback_bars:]
        
        # Detect current range
        current_range = self._detect_range(candles, atr)
        
        # Check for sweeps if range exists
        recent_sweeps = []
        if current_range and current_range.is_valid:
            recent_sweeps = self._detect_sweeps(candles, current_range, atr)
        
        # Calculate trend strength
        trend_strength = self._calculate_trend_strength(candles)
        
        # Determine if market is ranging
        is_ranging = (
            current_range is not None 
            and current_range.is_valid 
            and trend_strength < self.htf_trend_threshold
        )
        
        self.current_state = RangeState(
            current_range=current_range,
            recent_sweeps=recent_sweeps,
            trend_strength=trend_strength,
            is_ranging=is_ranging,
            last_update_ts=candles[-1].get("timestamp", 0) if candles else 0,
        )
        
        return self.current_state
    
    def _calculate_atr(self, candles: List[dict], period: int = 14) -> float:
        """Calculate Average True Range."""
        if len(candles) < period + 1:
            return 0.0
        
        tr_values = []
        for i in range(1, min(period + 1, len(candles))):
            high = candles[i].get("high", 0)
            low = candles[i].get("low", 0)
            prev_close = candles[i - 1].get("close", 0)
            
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            tr_values.append(tr)
        
        return sum(tr_values) / len(tr_values) if tr_values else 0.0
    
    def _detect_range(self, candles: List[dict], atr: float) -> Optional[RangeLevel]:
        """
        Detect consolidation range from recent candles.
        Uses the highest high and lowest low of the lookback period.
        """
        if len(candles) < self.min_range_bars:
            return None
        
        lookback = candles[-self.lookback_bars:]
        
        highest_high = max(c.get("high", 0) for c in lookback)
        lowest_low = min(c.get("low", 0) for c in lookback)
        mid = (highest_high + lowest_low) / 2
        
        range_size = highest_high - lowest_low
        range_pct = (range_size / mid) * 100 if mid > 0 else 0
        range_atr_ratio = range_size / atr if atr > 0 else 0
        
        # Store for logging
        self._last_range_ratio = range_atr_ratio
        
        # Validate range
        is_valid = True
        invalidation_reason = None
        
        if range_atr_ratio < self.min_range_atr:
            is_valid = False
            invalidation_reason = f"RANGE_TOO_SMALL (ratio={range_atr_ratio:.2f} < {self.min_range_atr})"
        elif range_atr_ratio > self.max_range_atr:
            is_valid = False
            invalidation_reason = f"RANGE_TOO_LARGE (ratio={range_atr_ratio:.2f} > {self.max_range_atr}, source={self._current_threshold_source})"
        
        return RangeLevel(
            range_type=RangeType.CONSOLIDATION,
            high=highest_high,
            low=lowest_low,
            mid=mid,
            range_size=range_size,
            range_pct=range_pct,
            start_ts=lookback[0].get("timestamp", 0),
            end_ts=lookback[-1].get("timestamp", 0),
            num_bars=len(lookback),
            atr=atr,
            range_atr_ratio=range_atr_ratio,
            is_valid=is_valid,
            invalidation_reason=invalidation_reason,
        )
    
    def _detect_sweeps(
        self, 
        candles: List[dict], 
        range_level: RangeLevel,
        atr: float
    ) -> List[RangeSweep]:
        """
        Detect sweeps of range highs/lows.
        A valid sweep: wick beyond level, close back inside (rejection).
        """
        sweeps = []
        
        if len(candles) < 2:
            return sweeps
        
        # Check recent candles for sweeps
        for i in range(-self.sweep_confirm_bars, 0):
            if abs(i) > len(candles):
                continue
            
            candle = candles[i]
            high = candle.get("high", 0)
            low = candle.get("low", 0)
            close = candle.get("close", 0)
            timestamp = candle.get("timestamp", 0)
            
            # Check high sweep
            if high > range_level.high:
                wick_size = high - max(candle.get("open", 0), close)
                closed_inside = close <= range_level.high
                
                sweep = RangeSweep(
                    range_ref=range_level,
                    sweep_side="high",
                    sweep_price=high,
                    sweep_ts=timestamp,
                    wick_size=wick_size,
                    closed_inside=closed_inside,
                    is_valid_sweep=closed_inside and wick_size > atr * 0.1,
                )
                sweeps.append(sweep)
            
            # Check low sweep
            if low < range_level.low:
                wick_size = min(candle.get("open", 0), close) - low
                closed_inside = close >= range_level.low
                
                sweep = RangeSweep(
                    range_ref=range_level,
                    sweep_side="low",
                    sweep_price=low,
                    sweep_ts=timestamp,
                    wick_size=wick_size,
                    closed_inside=closed_inside,
                    is_valid_sweep=closed_inside and wick_size > atr * 0.1,
                )
                sweeps.append(sweep)
        
        return sweeps
    
    def _calculate_trend_strength(self, candles: List[dict]) -> float:
        """
        Calculate trend strength (0.0 = ranging, 1.0 = strong trend).
        Uses directional movement and range expansion.
        """
        if len(candles) < 10:
            return 0.0
        
        recent = candles[-10:]
        
        # Calculate net movement vs total range
        start_price = recent[0].get("close", 0)
        end_price = recent[-1].get("close", 0)
        net_move = abs(end_price - start_price)
        
        total_range = sum(
            abs(c.get("high", 0) - c.get("low", 0)) 
            for c in recent
        )
        
        if total_range == 0:
            return 0.0
        
        # Ratio of net directional move to total range traversed
        efficiency = net_move / total_range
        
        # Count consecutive up/down closes
        consecutive = 0
        for i in range(1, len(recent)):
            if recent[i].get("close", 0) > recent[i-1].get("close", 0):
                if consecutive >= 0:
                    consecutive += 1
                else:
                    consecutive = 1
            elif recent[i].get("close", 0) < recent[i-1].get("close", 0):
                if consecutive <= 0:
                    consecutive -= 1
                else:
                    consecutive = -1
        
        consecutive_factor = min(abs(consecutive) / 5, 1.0)
        
        # Combined trend strength
        return min((efficiency + consecutive_factor) / 2, 1.0)
    
    def get_current_state(self) -> RangeState:
        """Return current range state."""
        return self.current_state
    
    def has_valid_range(self) -> bool:
        """Check if there's a valid range for trading."""
        return (
            self.current_state.current_range is not None 
            and self.current_state.current_range.is_valid
            and self.current_state.is_ranging
        )
    
    def has_recent_sweep(self, within_bars: int = 3) -> Optional[RangeSweep]:
        """
        Check if there's a recent valid sweep.
        
        Returns the most recent valid sweep or None.
        """
        for sweep in reversed(self.current_state.recent_sweeps):
            if sweep.is_valid_sweep:
                return sweep
        return None
    
    def get_range_targets(self) -> Optional[Tuple[float, float, float]]:
        """
        Get target levels for range trading.
        
        Returns:
            Tuple of (mid_target, opposite_boundary, range_size) or None
        """
        if not self.has_valid_range():
            return None
        
        r = self.current_state.current_range
        return (r.mid, r.high if r else 0, r.low if r else 0)
