"""
Smart Money Concept - Fair Value Gap Engine
Detects 3-candle imbalance gaps (FVG)
"""

from typing import List, Dict, Optional
from dataclasses import dataclass
from enum import Enum

class FVGType(Enum):
    BULLISH = "bullish"      # Uptrend gap, bullish continuation
    BEARISH = "bearish"      # Downtrend gap, bearish continuation

class FVGStatus(Enum):
    OPEN = "open"
    PARTIAL_FILL = "partial_fill"
    FULLY_FILLED = "fully_filled"

@dataclass
class FairValueGap:
    """3-candle imbalance gap"""
    type: FVGType
    top: float          # Upper boundary
    bottom: float       # Lower boundary
    
    # Formation
    formation_time: int
    formation_idx: int  # Index in candle history
    
    # Gap quality
    displacement_size: float
    gap_ratio: float   # (top-bottom) / bottom
    
    # Candles involved
    candle_1_idx: int
    candle_2_idx: int
    candle_3_idx: int
    
    # Status
    status: FVGStatus = FVGStatus.OPEN
    fill_price: Optional[float] = None
    fill_time: Optional[int] = None
    fill_percent: float = 0.0
    
    # Nesting level (nested FVGs)
    nesting_level: int = 0
    is_nested: bool = False
    parent_gap: Optional['FairValueGap'] = None
    
    # Validity
    is_valid: bool = True

class FVGEngine:
    """
    SMC Fair Value Gap Detection
    3-candle imbalances that represent market inefficiency
    """
    
    def __init__(self, lookback_period: int = 100):
        self.lookback_period = lookback_period
        self.candle_history = []
        self.fvg_list: List[FairValueGap] = []
        
    def add_candle(self, candle) -> None:
        """Add candle and detect FVGs"""
        self.candle_history.append(candle)
        if len(self.candle_history) > self.lookback_period:
            self.candle_history.pop(0)
        
        self._detect_fvg()
    
    def _detect_fvg(self) -> None:
        """Detect 3-candle FVG patterns"""
        if len(self.candle_history) < 3:
            return
        
        current_idx = len(self.candle_history)
        
        # Check last 3 candles for FVG
        if current_idx >= 3:
            c1 = self.candle_history[-3]
            c2 = self.candle_history[-2]
            c3 = self.candle_history[-1]
            
            # Bullish FVG: c1 up, c2 gap up (c2 low > c1 high)
            # Means c1 opened above c1.high and closed above c1.high
            if c1.close > c1.open and c2.low > c1.high:  # Gap up
                bullish_gap = FairValueGap(
                    type=FVGType.BULLISH,
                    top=c2.low,
                    bottom=c1.high,
                    formation_time=c2.timestamp,
                    formation_idx=current_idx - 2,
                    displacement_size=c2.low - c1.high,
                    gap_ratio=(c2.low - c1.high) / c1.high,
                    candle_1_idx=current_idx - 3,
                    candle_2_idx=current_idx - 2,
                    candle_3_idx=current_idx - 1
                )
                
                self._validate_fvg(bullish_gap)
                if bullish_gap.is_valid and not self._fvg_exists(bullish_gap):
                    self.fvg_list.append(bullish_gap)
            
            # Bearish FVG: c1 down, c2 gap down (c2 high < c1 low)
            if c1.close < c1.open and c2.high < c1.low:  # Gap down
                bearish_gap = FairValueGap(
                    type=FVGType.BEARISH,
                    top=c1.low,
                    bottom=c2.high,
                    formation_time=c2.timestamp,
                    formation_idx=current_idx - 2,
                    displacement_size=c1.low - c2.high,
                    gap_ratio=(c1.low - c2.high) / c1.low,
                    candle_1_idx=current_idx - 3,
                    candle_2_idx=current_idx - 2,
                    candle_3_idx=current_idx - 1
                )
                
                self._validate_fvg(bearish_gap)
                if bearish_gap.is_valid and not self._fvg_exists(bearish_gap):
                    self.fvg_list.append(bearish_gap)
        
        # Update FVG fill status
        self._update_fvg_fills()
        self._detect_nested_fvg()
        self._cleanup_old_fvgs()
    
    def _validate_fvg(self, fvg: FairValueGap) -> None:
        """Validate FVG quality rules"""
        # Minimum gap size (avoid noise)
        min_gap_ratio = 0.0005  # 0.05% minimum
        if fvg.gap_ratio < min_gap_ratio:
            fvg.is_valid = False
            return
        
        # Must have displacement (previous impulse candles)
        c1 = self.candle_history[fvg.candle_1_idx] if fvg.candle_1_idx < len(self.candle_history) else None
        if c1:
            size_ratio = abs(c1.close - c1.open) / (c1.high - c1.low) if c1.high != c1.low else 0
            
            # Weak FVGs from small candles are downgraded
            if size_ratio < 0.3:
                fvg.is_valid = False
    
    def _fvg_exists(self, new_fvg: FairValueGap) -> bool:
        """Check if similar FVG already recorded"""
        tolerance = 0.0001
        
        for existing in self.fvg_list:
            if existing.type == new_fvg.type:
                if abs(existing.top - new_fvg.top) < tolerance and abs(existing.bottom - new_fvg.bottom) < tolerance:
                    return True
        return False
    
    def _update_fvg_fills(self) -> None:
        """Update fill status of open FVGs"""
        if not self.candle_history:
            return
        
        current = self.candle_history[-1]
        
        for fvg in self.fvg_list:
            if fvg.status == FVGStatus.OPEN:
                # Check if price touches the gap
                if fvg.type == FVGType.BULLISH:
                    # Gap bottom is fvg.bottom, price fills when it reaches bottom
                    if current.low <= fvg.bottom:
                        fvg.fill_price = current.low
                        fvg.fill_time = current.timestamp
                        
                        # Check fill percent
                        if current.low <= fvg.bottom:
                            fvg.status = FVGStatus.FULLY_FILLED
                            fvg.fill_percent = 1.0
                        else:
                            fvg.status = FVGStatus.PARTIAL_FILL
                            fvg.fill_percent = (fvg.bottom - current.low) / (fvg.bottom - fvg.top)
                
                elif fvg.type == FVGType.BEARISH:
                    # Gap top is fvg.top, price fills when it reaches top
                    if current.high >= fvg.top:
                        fvg.fill_price = current.high
                        fvg.fill_time = current.timestamp
                        
                        if current.high >= fvg.top:
                            fvg.status = FVGStatus.FULLY_FILLED
                            fvg.fill_percent = 1.0
                        else:
                            fvg.status = FVGStatus.PARTIAL_FILL
                            fvg.fill_percent = (current.high - fvg.bottom) / (fvg.top - fvg.bottom)
    
    def _detect_nested_fvg(self) -> None:
        """Detect FVGs within FVGs (higher quality)"""
        for i, fvg1 in enumerate(self.fvg_list):
            for j, fvg2 in enumerate(self.fvg_list[i+1:], i+1):
                if fvg1.type == fvg2.type:
                    # FVG2 is nested in FVG1 if it's within bounds
                    if (fvg2.bottom >= fvg1.bottom and fvg2.top <= fvg1.top):
                        fvg2.is_nested = True
                        fvg2.parent_gap = fvg1
                        fvg2.nesting_level = fvg1.nesting_level + 1
    
    def _cleanup_old_fvgs(self) -> None:
        """Remove very old or fully filled FVGs"""
        cutoff_age = 100  # Keep last 100 candles
        current_idx = len(self.candle_history)
        
        self.fvg_list = [
            fvg for fvg in self.fvg_list
            if (current_idx - fvg.formation_idx) < cutoff_age or fvg.status != FVGStatus.FULLY_FILLED
        ]
    
    def get_open_fvgs(self) -> List[Dict]:
        """Get all open (unfilled) FVGs"""
        open_fvgs = [
            {
                "type": fvg.type.value,
                "top": fvg.top,
                "bottom": fvg.bottom,
                "gap_size": fvg.displacement_size,
                "gap_ratio": fvg.gap_ratio * 100,  # percentage
                "nested": fvg.is_nested,
                "nesting_level": fvg.nesting_level,
                "age_bars": len(self.candle_history) - fvg.formation_idx
            }
            for fvg in self.fvg_list
            if fvg.status == FVGStatus.OPEN and fvg.is_valid
        ]
        
        # Sort by most recent
        return sorted(open_fvgs, key=lambda x: x["age_bars"])[:5]
    
    def get_current_state(self) -> Dict:
        """Export current FVG state"""
        open_count = len([fvg for fvg in self.fvg_list if fvg.status == FVGStatus.OPEN])
        filled_count = len([fvg for fvg in self.fvg_list if fvg.status == FVGStatus.FULLY_FILLED])
        
        return {
            "open_fvgs": open_count,
            "filled_fvgs": filled_count,
            "nested_fvgs": len([fvg for fvg in self.fvg_list if fvg.is_nested]),
            "fvgs": self.get_open_fvgs()
        }
