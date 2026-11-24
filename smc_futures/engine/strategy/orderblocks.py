"""
Smart Money Concept - Order Block Engine
Detects valid institutional order blocks (OB)
"""

from typing import List, Dict, Optional
from dataclasses import dataclass, field
from enum import Enum
import numpy as np

class OBType(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"

class OBStatus(Enum):
    ACTIVE = "active"
    MITIGATED = "mitigated"
    EXPIRED = "expired"
    RETESTING = "retesting"

@dataclass
class OrderBlock:
    """Valid institutional order block"""
    type: OBType  # bullish or bearish
    high_price: float
    low_price: float
    
    # Formation candle
    formation_timestamp: int
    formation_close: float
    
    # Displacement metrics
    displacement_candles: int
    displacement_size: float
    displacement_percent: float
    
    # Status
    status: OBStatus = OBStatus.ACTIVE
    mitigation_price: Optional[float] = None
    mitigation_time: Optional[int] = None
    retest_count: int = 0
    
    # Freshness score (0-1, higher = fresher = better)
    freshness_score: float = 1.0
    
    # Confidence
    confidence: float = 0.5

class OrderBlockEngine:
    """
    SMC Order Block Detection
    Valid OB = Candle with displacement + causes BOS + volume expansion
    """
    
    def __init__(self, lookback_period: int = 100, min_displacement_bars: int = 2):
        self.lookback_period = lookback_period
        self.min_displacement_bars = min_displacement_bars
        self.candle_history = []
        self.order_blocks: List[OrderBlock] = []
        self.displacement_starts: List[tuple] = []
        
    def add_candle(self, candle) -> None:
        """Add candle and detect order blocks"""
        self.candle_history.append(candle)
        if len(self.candle_history) > self.lookback_period:
            self.candle_history.pop(0)
        
        self._detect_order_blocks()
    
    def _detect_order_blocks(self) -> None:
        """Detect valid order blocks"""
        if len(self.candle_history) < self.min_displacement_bars + 3:
            return
        
        self._find_displacement_candles()
        self._validate_order_blocks()
        self._update_block_status()
        self._decay_freshness()
    
    def _find_displacement_candles(self) -> None:
        """Find impulse candles that could form OB (Optimized)"""
        if len(self.candle_history) < 5:
            return
        
        # Only check the last index to avoid quadratic growth
        i = len(self.candle_history) - self.min_displacement_bars - 1
        if i < 0:
            return
            
        candle = self.candle_history[i]
        
        # Look for impulse candle (large move)
        body = abs(candle.close - candle.open)
        range_size = candle.high - candle.low
        
        if range_size > 0:
            body_ratio = body / range_size
            
            # Volume expansion check
            avg_volume = np.mean([c.volume for c in self.candle_history[max(0, i-5):i]]) if i >= 5 else candle.volume
            volume_ratio = candle.volume / (avg_volume + 1)
            
            is_impulse = body_ratio > 0.6 and volume_ratio > 1.2
            
            if is_impulse:
                displacement_start = {
                    "index": i,
                    "candle": candle,
                    "body_ratio": body_ratio,
                    "volume_ratio": volume_ratio
                }
                # Check for duplicate to avoid re-adding same candle in some loop edge cases
                if not self.displacement_starts or self.displacement_starts[-1][0] != i:
                    self.displacement_starts.append((i, displacement_start))
        
        # Cap displacement starts to prevent memory leaks
        if len(self.displacement_starts) > 50:
            self.displacement_starts.pop(0)

    def _validate_order_blocks(self) -> None:
        """Validate if displacement creates valid OB"""
        current_idx = len(self.candle_history) - 1
        
        # Only check recent displacement starts
        for start_idx, impulse_info in self.displacement_starts[-10:]:
            if start_idx >= current_idx:
                continue
            
            displacement_range = current_idx - start_idx
            impulse_candle = impulse_info["candle"]
            
            bos_high = False
            bos_low = False
            
            # Use limited scan for BOS
            for j in range(start_idx + 1, current_idx + 1):
                if self.candle_history[j].high > impulse_candle.high:
                    bos_high = True
                    break # Optimization: stop once BOS found
                if self.candle_history[j].low < impulse_candle.low:
                    bos_low = True
                    break
            
            if bos_high:
                ob = OrderBlock(
                    type=OBType.BULLISH,
                    high_price=impulse_candle.high,
                    low_price=impulse_candle.low,
                    formation_timestamp=impulse_candle.timestamp,
                    formation_close=impulse_candle.close,
                    displacement_candles=displacement_range,
                    displacement_size=abs(self.candle_history[current_idx].close - impulse_candle.close),
                    displacement_percent=(abs(self.candle_history[current_idx].close - impulse_candle.close) / impulse_candle.close * 100),
                    confidence=self._calculate_ob_confidence(impulse_info)
                )
                if not self._order_block_exists(ob):
                    self.order_blocks.append(ob)
            
            if bos_low:
                ob = OrderBlock(
                    type=OBType.BEARISH,
                    high_price=impulse_candle.high,
                    low_price=impulse_candle.low,
                    formation_timestamp=impulse_candle.timestamp,
                    formation_close=impulse_candle.close,
                    displacement_candles=displacement_range,
                    displacement_size=abs(self.candle_history[current_idx].close - impulse_candle.close),
                    displacement_percent=(abs(self.candle_history[current_idx].close - impulse_candle.close) / impulse_candle.close * 100),
                    confidence=self._calculate_ob_confidence(impulse_info)
                )
                if not self._order_block_exists(ob):
                    self.order_blocks.append(ob)
        
        # Cap total order blocks
        if len(self.order_blocks) > 100:
            self.order_blocks = self.order_blocks[-100:]

    def _update_block_status(self) -> None:
        """Update mitigation status of blocks"""
        if not self.candle_history:
            return
        
        current = self.candle_history[-1]
        
        for block in self.order_blocks:
            if block.status == OBStatus.ACTIVE:
                # Check if block is mitigated (price reached it)
                if block.type == OBType.BULLISH:
                    if current.low <= block.low_price:
                        block.status = OBStatus.MITIGATED
                        block.mitigation_price = current.low
                        block.mitigation_time = current.timestamp
                elif block.type == OBType.BEARISH:
                    if current.high >= block.high_price:
                        block.status = OBStatus.MITIGATED
                        block.mitigation_price = current.high
                        block.mitigation_time = current.timestamp
                
                # Check if retesting (price entering zone again)
                if current.close > block.low_price and current.close < block.high_price:
                    block.status = OBStatus.RETESTING
                    block.retest_count += 1
            
            # Expire old blocks naturally (already capped in _validate, but status update is good)
            if (current.timestamp - block.formation_timestamp) > (24 * 3600 * 1000): # 24h
                 block.status = OBStatus.EXPIRED
    
    def _decay_freshness(self) -> None:
        """Fresher blocks are more valuable"""
        for block in self.order_blocks:
            # Distance from formation in bars (approxmate)
            age_bars = len([c for c in self.candle_history if c.timestamp >= block.formation_timestamp])
            block.freshness_score = max(0.1, 1.0 - (age_bars / 100))
    
    def _calculate_ob_confidence(self, impulse_info: Dict) -> float:
        """Calculate confidence score for OB (0-1)"""
        base = 0.5
        base += impulse_info["body_ratio"] * 0.2  # Big impulse body = higher confidence
        base += min(0.3, impulse_info["volume_ratio"] / 5)  # Volume expansion
        return min(1.0, base)
    
    def _order_block_exists(self, new_ob: OrderBlock) -> bool:
        """Check if similar OB already exists (Optimized)"""
        tolerance = 0.002  # 0.2% tolerance
        # Only check the last 20 blocks for efficiency
        for existing in self.order_blocks[-20:]:
            if existing.type == new_ob.type:
                if abs(existing.low_price - new_ob.low_price) / new_ob.low_price < tolerance:
                    return True
        return False
    
    def get_active_blocks(self) -> List[Dict]:
        """Get active, fresh order blocks"""
        active = [
            {
                "type": block.type.value,
                "high": block.high_price,
                "low": block.low_price,
                "confidence": block.confidence,
                "freshness": block.freshness_score,
                "mitigated": block.status == OBStatus.MITIGATED,
                "retest_count": block.retest_count,
                "displacement_bars": block.displacement_candles
            }
            for block in self.order_blocks
            if block.status == OBStatus.ACTIVE and block.freshness_score > 0.3
        ]
        return sorted(active, key=lambda x: x["freshness"], reverse=True)[:5]
    
    def get_current_state(self) -> Dict:
        """Export current OB state"""
        return {
            "active_blocks": len([b for b in self.order_blocks if b.status == OBStatus.ACTIVE]),
            "mitigated_blocks": len([b for b in self.order_blocks if b.status == OBStatus.MITIGATED]),
            "retest_found": any(b.status == OBStatus.RETESTING for b in self.order_blocks),
            "blocks": self.get_active_blocks()
        }
