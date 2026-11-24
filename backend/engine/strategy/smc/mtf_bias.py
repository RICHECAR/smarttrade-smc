"""
Multi-Timeframe Bias Engine
HTF (4H/1H) provides directional bias, LTF (5m/15m) provides entry confirmation
"""

from typing import Dict, List, Optional
from dataclasses import dataclass
from enum import Enum

class Bias(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"

@dataclass
class TimeframeAnalysis:
    """Analysis state for single timeframe"""
    timeframe: str
    bias: Bias
    strength: float  # 0-1, confidence
    last_swing_high: Optional[float] = None
    last_swing_low: Optional[float] = None
    bos_count: int = 0
    order_blocks: List[Dict] = None
    fvgs: List[Dict] = None
    
    def __post_init__(self):
        if self.order_blocks is None:
            self.order_blocks = []
        if self.fvgs is None:
            self.fvgs = []

class MTFBiasEngine:
    """
    Multi-Timeframe Bias Detection
    4H/1H = HTF bias (directional)
    5M/15M = LTF entry confirmation (structure + timing)
    """
    
    def __init__(self):
        self.htf_analysis: Dict[str, TimeframeAnalysis] = {
            "4H": TimeframeAnalysis("4H", Bias.NEUTRAL, 0.5),
            "1H": TimeframeAnalysis("1H", Bias.NEUTRAL, 0.5)
        }
        
        self.ltf_analysis: Dict[str, TimeframeAnalysis] = {
            "15M": TimeframeAnalysis("15M", Bias.NEUTRAL, 0.5),
            "5M": TimeframeAnalysis("5M", Bias.NEUTRAL, 0.5)
        }
        
        self.current_bias = Bias.NEUTRAL
        self.bias_strength = 0.5
        
    def update_htf_data(self, structure_state: Dict) -> None:
        """Update higher timeframe analysis"""
        # Update 4H bias
        if structure_state.get("is_uptrend"):
            self.htf_analysis["4H"].bias = Bias.BULLISH
            self.htf_analysis["4H"].strength = min(1.0, 0.5 + structure_state.get("structure_strength", 0.5) * 0.5)
        else:
            self.htf_analysis["4H"].bias = Bias.BEARISH
            self.htf_analysis["4H"].strength = min(1.0, 0.5 + structure_state.get("structure_strength", 0.5) * 0.5)
        
        self.htf_analysis["4H"].last_swing_high = structure_state.get("last_swing_high")
        self.htf_analysis["4H"].last_swing_low = structure_state.get("last_swing_low")
        self.htf_analysis["4H"].bos_count = structure_state.get("recent_bos", 0)
    
    def update_ltf_data(self, structure_state: Dict) -> None:
        """Update lower timeframe analysis"""
        if structure_state.get("is_uptrend"):
            self.ltf_analysis["5M"].bias = Bias.BULLISH
            self.ltf_analysis["5M"].strength = min(1.0, 0.5 + structure_state.get("structure_strength", 0.5) * 0.5)
        else:
            self.ltf_analysis["5M"].bias = Bias.BEARISH
            self.ltf_analysis["5M"].strength = min(1.0, 0.5 + structure_state.get("structure_strength", 0.5) * 0.5)
        
        self.ltf_analysis["5M"].last_swing_high = structure_state.get("last_swing_high")
        self.ltf_analysis["5M"].last_swing_low = structure_state.get("last_swing_low")
    
    def update_ob_data(self, timeframe: str, ob_state: Dict) -> None:
        """Update order block data for timeframe"""
        if timeframe in self.htf_analysis:
            self.htf_analysis[timeframe].order_blocks = ob_state.get("blocks", [])
        if timeframe in self.ltf_analysis:
            self.ltf_analysis[timeframe].order_blocks = ob_state.get("blocks", [])
    
    def update_fvg_data(self, timeframe: str, fvg_state: Dict) -> None:
        """Update FVG data for timeframe"""
        if timeframe in self.htf_analysis:
            self.htf_analysis[timeframe].fvgs = fvg_state.get("fvgs", [])
        if timeframe in self.ltf_analysis:
            self.ltf_analysis[timeframe].fvgs = fvg_state.get("fvgs", [])
    
    def calculate_bias(self) -> Bias:
        """Calculate overall market bias from HTF"""
        htf_4h = self.htf_analysis["4H"]
        htf_1h = self.htf_analysis["1H"]
        
        # 4H is primary, 1H is secondary confirmation
        if htf_4h.bias == Bias.BULLISH:
            if htf_1h.bias == Bias.BULLISH:
                self.current_bias = Bias.BULLISH
                self.bias_strength = (htf_4h.strength + htf_1h.strength) / 2
            else:
                self.current_bias = Bias.BULLISH
                self.bias_strength = htf_4h.strength * 0.8
        
        elif htf_4h.bias == Bias.BEARISH:
            if htf_1h.bias == Bias.BEARISH:
                self.current_bias = Bias.BEARISH
                self.bias_strength = (htf_4h.strength + htf_1h.strength) / 2
            else:
                self.current_bias = Bias.BEARISH
                self.bias_strength = htf_4h.strength * 0.8
        
        else:
            self.current_bias = Bias.NEUTRAL
            self.bias_strength = 0.5
        
        return self.current_bias
    
    def validate_ltf_entry(self, ltf_signal: Dict) -> bool:
        """Check if LTF signal aligns with HTF bias"""
        self.calculate_bias()
        
        ltf_direction = ltf_signal.get("direction")
        
        # Entry must align with HTF bias
        if self.current_bias == Bias.BULLISH:
            return ltf_direction == "long"
        elif self.current_bias == Bias.BEARISH:
            return ltf_direction == "short"
        
        return False
    
    def check_ltf_confirmation(self, ltf_structure: Dict) -> bool:
        """Verify LTF has BOS in bias direction"""
        self.calculate_bias()
        
        if self.current_bias == Bias.BULLISH:
            # Must have bullish BOS on LTF (low breaks previous swing low)
            return ltf_structure.get("recent_bos", 0) > 0 and ltf_structure.get("is_uptrend")
        
        elif self.current_bias == Bias.BEARISH:
            # Must have bearish BOS on LTF (high breaks previous swing high)
            return ltf_structure.get("recent_bos", 0) > 0 and not ltf_structure.get("is_uptrend")
        
        return False
    
    def find_entry_zones(self) -> List[Dict]:
        """Find HT/LTF aligned entry zones"""
        entry_zones = []
        self.calculate_bias()
        
        if self.current_bias == Bias.BULLISH:
            # Entry zones = bullish OB or bullish FVG
            for ob in self.htf_analysis["4H"].order_blocks:
                if ob.get("type") == "bullish":
                    entry_zones.append({
                        "type": "order_block",
                        "level": ob.get("low"),
                        "direction": "long",
                        "timeframe": "4H",
                        "confidence": ob.get("confidence", 0.5)
                    })
            
            for fvg in self.htf_analysis["4H"].fvgs:
                if fvg.get("type") == "bullish":
                    entry_zones.append({
                        "type": "fvg",
                        "level": fvg.get("bottom"),
                        "direction": "long",
                        "timeframe": "4H",
                        "confidence": 0.7
                    })
        
        elif self.current_bias == Bias.BEARISH:
            # Entry zones = bearish OB or bearish FVG
            for ob in self.htf_analysis["4H"].order_blocks:
                if ob.get("type") == "bearish":
                    entry_zones.append({
                        "type": "order_block",
                        "level": ob.get("high"),
                        "direction": "short",
                        "timeframe": "4H",
                        "confidence": ob.get("confidence", 0.5)
                    })
            
            for fvg in self.htf_analysis["4H"].fvgs:
                if fvg.get("type") == "bearish":
                    entry_zones.append({
                        "type": "fvg",
                        "level": fvg.get("top"),
                        "direction": "short",
                        "timeframe": "4H",
                        "confidence": 0.7
                    })
        
        return entry_zones
    
    def get_current_state(self) -> Dict:
        """Export MTF bias state"""
        self.calculate_bias()
        
        return {
            "bias": self.current_bias.value,
            "bias_strength": self.bias_strength,
            "htf_4h": {
                "bias": self.htf_analysis["4H"].bias.value,
                "strength": self.htf_analysis["4H"].strength,
                "swing_high": self.htf_analysis["4H"].last_swing_high,
                "swing_low": self.htf_analysis["4H"].last_swing_low,
            },
            "htf_1h": {
                "bias": self.htf_analysis["1H"].bias.value,
                "strength": self.htf_analysis["1H"].strength,
            },
            "entry_zones": self.find_entry_zones()
        }
