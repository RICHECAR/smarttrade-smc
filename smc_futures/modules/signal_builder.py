"""
SMC Signal Builder
Combines: Structure + Liquidity + Order Blocks + FVG + MTF Bias
"""

from typing import Dict, Optional, List
from dataclasses import dataclass
from enum import Enum

class SignalDirection(Enum):
    LONG = "long"
    SHORT = "short"
    HOLD = "hold"

@dataclass
class SMCSignal:
    """Complete SMC trade signal"""
    direction: SignalDirection
    entry_price: float
    stop_loss: float
    take_profit: float
    
    # Setup details
    setup_type: str  # "OB", "FVG", "Sweep+OB", etc
    liquidity_trigger: Optional[str] = None
    
    # Quality metrics
    confidence_score: float = 0.5  # 0-1
    risk_reward_ratio: float = 1.0
    invalidation_level: float = 0.0
    
    # Context
    mtf_bias: str = "neutral"
    structure_strength: float = 0.5
    
    # Timestamp
    timestamp: int = 0
    timeframe: str = "5m"

class SignalBuilder:
    """
    Builds SMC signals from module outputs
    Validates: HTF bias + LTF entry + Risk/Reward
    """
    
    def __init__(self, min_rr_ratio: float = 1.5):
        self.min_rr_ratio = min_rr_ratio
        self.last_signal: Optional[SMCSignal] = None
        self.signal_history: List[SMCSignal] = []
        
    def build_signal(
        self,
        structure_state: Dict,
        liquidity_state: Dict,
        ob_state: Dict,
        fvg_state: Dict,
        mtf_state: Dict,
        current_price: float,
        timestamp: int
    ) -> Optional[SMCSignal]:
        """
        Build signal from all SMC components
        Returns signal if all criteria met, None otherwise
        """
        
        # Step 1: Check MTF bias alignment
        bias = mtf_state.get("bias", "neutral")
        bias_strength = mtf_state.get("bias_strength", 0.5)
        
        if bias == "neutral" or bias_strength < 0.5:
            return None  # No clear directional bias
        
        # Step 2: Check for liquidity trigger (sweep)
        recent_sweeps = liquidity_state.get("sweep_history", [])
        if not recent_sweeps:
            return None  # No recent liquidity action
        
        last_sweep = recent_sweeps[-1]
        sweep_type = last_sweep.get("type")
        
        # Step 3: Align sweep with bias
        if bias == "bullish" and sweep_type != "buyside":
            return None  # Bearish sweep contradicts bullish bias
        if bias == "bearish" and sweep_type != "sellside":
            return None  # Bullish sweep contradicts bearish bias
        
        # Step 4: Find entry zone (OB or FVG)
        entry_zone = self._find_entry_zone(bias, ob_state, fvg_state)
        if not entry_zone:
            return None  # No valid entry zone
        
        # Step 5: Build signal parameters
        if bias == "bullish":
            signal = self._build_bullish_signal(
                current_price=current_price,
                entry_zone=entry_zone,
                liquidity_state=liquidity_state,
                structure_state=structure_state,
                bias_strength=bias_strength,
                timestamp=timestamp
            )
        else:  # bearish
            signal = self._build_bearish_signal(
                current_price=current_price,
                entry_zone=entry_zone,
                liquidity_state=liquidity_state,
                structure_state=structure_state,
                bias_strength=bias_strength,
                timestamp=timestamp
            )
        
        # Step 6: Validate RR ratio
        if signal and signal.risk_reward_ratio >= self.min_rr_ratio:
            self.last_signal = signal
            self.signal_history.append(signal)
            return signal
        
        return None
    
    def _find_entry_zone(self, bias: str, ob_state: Dict, fvg_state: Dict) -> Optional[Dict]:
        """Find nearest valid entry zone (OB or FVG)"""
        zones = []
        
        # Collect active order blocks
        if ob_state.get("blocks"):
            for ob in ob_state["blocks"]:
                if bias == "bullish" and ob.get("type") == "bullish":
                    zones.append({
                        "type": "order_block",
                        "price": ob.get("low"),
                        "confidence": ob.get("confidence", 0.5),
                        "freshness": ob.get("freshness", 0.5)
                    })
                elif bias == "bearish" and ob.get("type") == "bearish":
                    zones.append({
                        "type": "order_block",
                        "price": ob.get("high"),
                        "confidence": ob.get("confidence", 0.5),
                        "freshness": ob.get("freshness", 0.5)
                    })
        
        # Collect open FVGs
        if fvg_state.get("fvgs"):
            for fvg in fvg_state["fvgs"]:
                if bias == "bullish" and fvg.get("type") == "bullish":
                    zones.append({
                        "type": "fvg",
                        "price": fvg.get("bottom"),
                        "confidence": 0.7,
                        "freshness": 1.0 / (fvg.get("age_bars", 1) + 1)
                    })
                elif bias == "bearish" and fvg.get("type") == "bearish":
                    zones.append({
                        "type": "fvg",
                        "price": fvg.get("top"),
                        "confidence": 0.7,
                        "freshness": 1.0 / (fvg.get("age_bars", 1) + 1)
                    })
        
        if not zones:
            return None
        
        # Return best zone (highest freshness * confidence)
        return max(zones, key=lambda z: z["confidence"] * z["freshness"])
    
    def _build_bullish_signal(
        self,
        current_price: float,
        entry_zone: Dict,
        liquidity_state: Dict,
        structure_state: Dict,
        bias_strength: float,
        timestamp: int
    ) -> Optional[SMCSignal]:
        """Build bullish entry signal"""
        
        entry_price = entry_zone["price"]
        
        # Entry should be at zone, or after confirmation
        if current_price < entry_price:
            return None  # Price hasn't reached entry yet
        
        # Stop loss below entry
        session_low = liquidity_state.get("session_low", current_price * 0.98)
        stop_loss = min(session_low, entry_price * 0.97)
        
        # Take profit target = entry + (entry - SL) * RR
        risk = entry_price - stop_loss
        rr_multiple = min(3.0, 1.5 + bias_strength)  # 1.5x to 3x based on strength
        take_profit = entry_price + (risk * rr_multiple)
        
        rr_ratio = (take_profit - entry_price) / (entry_price - stop_loss) if stop_loss != entry_price else 0
        
        if rr_ratio < self.min_rr_ratio:
            return None
        
        # Confidence based on setup quality
        setup_confidence = entry_zone.get("confidence", 0.5)
        mtf_confidence = bias_strength
        overall_confidence = (setup_confidence * 0.6 + mtf_confidence * 0.4)
        
        signal = SMCSignal(
            direction=SignalDirection.LONG,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            setup_type=entry_zone.get("type"),
            liquidity_trigger="buyside_sweep",
            confidence_score=overall_confidence,
            risk_reward_ratio=rr_ratio,
            invalidation_level=stop_loss,
            mtf_bias="bullish",
            structure_strength=structure_state.get("structure_strength", 0.5),
            timestamp=timestamp
        )
        
        return signal
    
    def _build_bearish_signal(
        self,
        current_price: float,
        entry_zone: Dict,
        liquidity_state: Dict,
        structure_state: Dict,
        bias_strength: float,
        timestamp: int
    ) -> Optional[SMCSignal]:
        """Build bearish entry signal"""
        
        entry_price = entry_zone["price"]
        
        # Entry should be at zone, or after confirmation
        if current_price > entry_price:
            return None  # Price hasn't reached entry yet
        
        # Stop loss above entry
        session_high = liquidity_state.get("session_high", current_price * 1.02)
        stop_loss = max(session_high, entry_price * 1.03)
        
        # Take profit target = entry - (SL - entry) * RR
        risk = stop_loss - entry_price
        rr_multiple = min(3.0, 1.5 + bias_strength)  # 1.5x to 3x based on strength
        take_profit = entry_price - (risk * rr_multiple)
        
        rr_ratio = (entry_price - take_profit) / (stop_loss - entry_price) if stop_loss != entry_price else 0
        
        if rr_ratio < self.min_rr_ratio:
            return None
        
        # Confidence based on setup quality
        setup_confidence = entry_zone.get("confidence", 0.5)
        mtf_confidence = bias_strength
        overall_confidence = (setup_confidence * 0.6 + mtf_confidence * 0.4)
        
        signal = SMCSignal(
            direction=SignalDirection.SHORT,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            setup_type=entry_zone.get("type"),
            liquidity_trigger="sellside_sweep",
            confidence_score=overall_confidence,
            risk_reward_ratio=rr_ratio,
            invalidation_level=stop_loss,
            mtf_bias="bearish",
            structure_strength=structure_state.get("structure_strength", 0.5),
            timestamp=timestamp
        )
        
        return signal
    
    def get_last_signal(self) -> Optional[Dict]:
        """Get last signal as dict"""
        if not self.last_signal:
            return None
        
        s = self.last_signal
        return {
            "direction": s.direction.value,
            "entry": s.entry_price,
            "stop_loss": s.stop_loss,
            "take_profit": s.take_profit,
            "setup": s.setup_type,
            "confidence": s.confidence_score,
            "rr_ratio": s.risk_reward_ratio,
            "invalidation": s.invalidation_level,
            "bias": s.mtf_bias,
            "timestamp": s.timestamp
        }
