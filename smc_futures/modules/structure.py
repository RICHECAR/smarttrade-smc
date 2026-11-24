"""
Smart Money Concept - Market Structure Engine
Detects: Swing highs/lows, Break of Structure (BOS), Change of Character (CHOCH)
"""

from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum
import numpy as np
from datetime import datetime

class TrendPhase(Enum):
    ACCUMULATION = "accumulation"
    EXPANSION = "expansion"
    DISTRIBUTION = "distribution"
    REVERSAL = "reversal"
    UNDEFINED = "undefined"

class StructureType(Enum):
    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    SUPPORT = "support"
    RESISTANCE = "resistance"
    BOS = "break_of_structure"
    CHOCH = "change_of_character"

@dataclass
class Candle:
    """OHLCV candle"""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0

@dataclass
class StructurePoint:
    """Detected structure point"""
    type: StructureType
    price: float
    timestamp: int
    strength: float = 0.5  # 0-1: weak to strong
    breached: bool = False
    breach_time: Optional[int] = None
    
@dataclass
class StructureState:
    """Current market structure state"""
    last_swing_high: Optional[StructurePoint] = None
    last_swing_low: Optional[StructurePoint] = None
    trend_phase: TrendPhase = TrendPhase.UNDEFINED
    bos_events: List[StructurePoint] = field(default_factory=list)
    choch_events: List[StructurePoint] = field(default_factory=list)
    is_uptrend: bool = False
    structure_strength: float = 0.5
    internal_structure: List[StructurePoint] = field(default_factory=list)
    consolidated_highs: List[float] = field(default_factory=list)
    consolidated_lows: List[float] = field(default_factory=list)

class StructureEngine:
    """
    SMC Market Structure Detection
    Identifies swing points, BOS, CHOCH, trend phases
    """
    
    def __init__(self, lookback_period: int = 50, min_swing_bars: int = 2):
        self.lookback_period = lookback_period
        self.min_swing_bars = min_swing_bars
        self.candle_history: List[Candle] = []
        self.structure_history: List[StructurePoint] = []
        self.state = StructureState()
        self._total_candles: int = 0
        
    def add_candle(self, candle: Candle) -> None:
        """Add candle to history and update structure"""
        self._total_candles += 1
        self.candle_history.append(candle)
        if len(self.candle_history) > self.lookback_period:
            self.candle_history.pop(0)
        self.detect_structure()
        
    def detect_structure(self) -> StructureState:
        """Main structure detection logic"""
        if len(self.candle_history) < self.min_swing_bars + 2:
            return self.state
            
        self._detect_swing_points()
        self._detect_bos_and_choch()
        self._classify_trend_phase()
        self._detect_internal_structure()
        
        return self.state
    
    def _detect_swing_points(self) -> None:
        """Detect swing highs and lows"""
        if len(self.candle_history) < 5:
            return
            
        current_idx = len(self.candle_history) - 1
        
        # Check for swing high (middle candle is highest)
        if current_idx >= self.min_swing_bars:
            center_idx = current_idx - self.min_swing_bars
            is_swing_high = True
            
            center_high = self.candle_history[center_idx].high
            
            for i in range(max(0, center_idx - self.min_swing_bars),
                          min(len(self.candle_history), center_idx + self.min_swing_bars + 1)):
                if i != center_idx and self.candle_history[i].high >= center_high:
                    is_swing_high = False
                    break
            
            if is_swing_high:
                strength = self._calculate_structure_strength(center_idx, "high")
                point = StructurePoint(
                    type=StructureType.SWING_HIGH,
                    price=center_high,
                    timestamp=self.candle_history[center_idx].timestamp,
                    strength=strength
                )
                self.state.last_swing_high = point
                self.structure_history.append(point)
        
        # Check for swing low (middle candle is lowest)
        if current_idx >= self.min_swing_bars:
            center_idx = current_idx - self.min_swing_bars
            is_swing_low = True
            
            center_low = self.candle_history[center_idx].low
            
            for i in range(max(0, center_idx - self.min_swing_bars),
                          min(len(self.candle_history), center_idx + self.min_swing_bars + 1)):
                if i != center_idx and self.candle_history[i].low <= center_low:
                    is_swing_low = False
                    break
            
            if is_swing_low:
                strength = self._calculate_structure_strength(center_idx, "low")
                point = StructurePoint(
                    type=StructureType.SWING_LOW,
                    price=center_low,
                    timestamp=self.candle_history[center_idx].timestamp,
                    strength=strength
                )
                self.state.last_swing_low = point
                self.structure_history.append(point)
    
    def _detect_bos_and_choch(self) -> None:
        """Detect Break of Structure and Change of Character"""
        if not self.state.last_swing_high or not self.state.last_swing_low:
            return
        
        current = self.candle_history[-1]
        
        # BOS = Close breaks previous structure without closing back inside
        # Uptrend BOS = Low breaks previous swing low
        # Downtrend BOS = High breaks previous swing high
        
        if self.state.is_uptrend:
            if current.low < self.state.last_swing_low.price:
                bos = StructurePoint(
                    type=StructureType.BOS,
                    price=current.low,
                    timestamp=current.timestamp,
                    strength=0.8
                )
                self.state.bos_events.append(bos)
        else:
            if current.high > self.state.last_swing_high.price:
                bos = StructurePoint(
                    type=StructureType.BOS,
                    price=current.high,
                    timestamp=current.timestamp,
                    strength=0.8
                )
                self.state.bos_events.append(bos)
        
        # CHOCH = Swing in opposite direction + structure flip
        if len(self.structure_history) >= 3:
            recent = self.structure_history[-3:]
            if (recent[0].type == StructureType.SWING_HIGH and
                recent[1].type == StructureType.SWING_LOW and
                recent[2].type == StructureType.SWING_HIGH and
                recent[2].price > recent[0].price):
                choch = StructurePoint(
                    type=StructureType.CHOCH,
                    price=recent[2].price,
                    timestamp=recent[2].timestamp,
                    strength=0.9
                )
                self.state.choch_events.append(choch)
    
    def _classify_trend_phase(self) -> None:
        """Classify current trend phase"""
        if len(self.candle_history) < 10:
            self.state.trend_phase = TrendPhase.UNDEFINED
            return
        
        recent = self.candle_history[-10:]
        highs = [c.high for c in recent]
        lows = [c.low for c in recent]
        
        # Uptrend: higher highs and higher lows
        if highs[-1] > max(highs[:-1]) and lows[-1] > max(lows[:-1]):
            self.state.is_uptrend = True
            if len(self.candle_history) > 20:
                older = self.candle_history[-20:-10]
                older_high = max([c.high for c in older])
                if highs[-1] > older_high * 1.02:
                    self.state.trend_phase = TrendPhase.EXPANSION
                else:
                    self.state.trend_phase = TrendPhase.ACCUMULATION
        
        # Downtrend: lower highs and lower lows
        elif highs[-1] < max(highs[:-1]) and lows[-1] < max(lows[:-1]):
            self.state.is_uptrend = False
            if len(self.candle_history) > 20:
                older = self.candle_history[-20:-10]
                older_low = min([c.low for c in older])
                if lows[-1] < older_low * 0.98:
                    self.state.trend_phase = TrendPhase.EXPANSION
                else:
                    self.state.trend_phase = TrendPhase.DISTRIBUTION
    
    def _detect_internal_structure(self) -> None:
        """Detect smaller structures within larger moves"""
        if len(self.candle_history) < 5:
            return
        
        # Consolidated highs = 2+ equal highs within tolerance
        recent_highs = [c.high for c in self.candle_history[-10:]]
        tolerance = max(recent_highs) * 0.001
        
        self.state.consolidated_highs = []
        for i, h in enumerate(recent_highs):
            for j in range(i + 1, len(recent_highs)):
                if abs(recent_highs[j] - h) < tolerance:
                    self.state.consolidated_highs.append(h)
        
        recent_lows = [c.low for c in self.candle_history[-10:]]
        self.state.consolidated_lows = []
        for i, l in enumerate(recent_lows):
            for j in range(i + 1, len(recent_lows)):
                if abs(recent_lows[j] - l) < tolerance:
                    self.state.consolidated_lows.append(l)
    
    def _calculate_structure_strength(self, candle_idx: int, point_type: str) -> float:
        """Calculate strength of structure point (0-1)"""
        candle = self.candle_history[candle_idx]
        
        # Strength based on:
        # 1. Volume expansion
        avg_volume = np.mean([c.volume for c in self.candle_history[-20:]])
        volume_strength = min(1.0, candle.volume / (avg_volume + 1))
        
        # 2. Candle body size
        body_size = abs(candle.close - candle.open)
        hl_range = candle.high - candle.low
        body_strength = body_size / (hl_range + 0.0001)
        
        # 3. Wick size (rejection strength)
        if point_type == "high":
            wick = candle.high - max(candle.open, candle.close)
        else:
            wick = min(candle.open, candle.close) - candle.low
        
        wick_strength = wick / (hl_range + 0.0001)
        
        return (volume_strength * 0.3 + body_strength * 0.3 + wick_strength * 0.4)
    
    def get_current_state(self) -> Dict:
        """Export current structure state as dict"""
        return {
            "candle_count": self._total_candles,
            "last_swing_high": self.state.last_swing_high.price if self.state.last_swing_high else None,
            "last_swing_low": self.state.last_swing_low.price if self.state.last_swing_low else None,
            "trend_phase": self.state.trend_phase.value,
            "is_uptrend": self.state.is_uptrend,
            "structure_strength": self.state.structure_strength,
            "recent_bos": len(self.state.bos_events),
            "recent_choch": len(self.state.choch_events),
            "consolidated_highs": self.state.consolidated_highs[-3:] if self.state.consolidated_highs else [],
            "consolidated_lows": self.state.consolidated_lows[-3:] if self.state.consolidated_lows else [],
        }
