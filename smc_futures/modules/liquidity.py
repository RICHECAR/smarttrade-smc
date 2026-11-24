"""
Smart Money Concept - Liquidity Engine
Detects: Stop clusters, liquidity pools, sweeps, engineered liquidity grabs
"""

from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import numpy as np
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class SweepType(Enum):
    BUYSIDE = "buyside"
    SELLSIDE = "sellside"

@dataclass
class LiquidityCluster:
    """Zone of accumulated buy/sell stops"""
    price: float
    type: str  # "buyside" or "sellside"
    strength: float  # 0-1: weak to strong
    timestamp: int
    size: float = 0
    touched: bool = False
    touched_price: Optional[float] = None
    
@dataclass
class LiquiditySweep:
    """Liquidity sweep event"""
    sweep_type: SweepType
    sweep_price: float
    timestamp: int
    displacement_size: float
    retracement: float
    wick_size: float = 0.0
    is_valid: bool = True

class LiquidityEngine:
    """
    SMC Liquidity Detection
    Identifies stop clusters, equal highs/lows, sweeps
    
    Configurable via:
      - lookback_period: session high/low tracking window
      - cluster_tolerance: equal high/low detection tolerance  
      - min_wick_atr: minimum wick size as ATR multiple
      - allow_equal_hl: allow equal high/low as sweep zones
      - require_close_beyond: require close beyond level (stricter)
    """
    
    def __init__(
        self,
        lookback_period: int = 50,
        cluster_tolerance: float = 0.003,
        min_wick_atr: float = 0.3,
        allow_equal_hl: bool = True,
        require_close_beyond: bool = False,
    ):
        self.lookback_period = lookback_period
        self.cluster_tolerance = cluster_tolerance
        self.min_wick_atr = min_wick_atr
        self.allow_equal_hl = allow_equal_hl
        self.require_close_beyond = require_close_beyond
        
        self.candle_history = []
        self.liquidity_clusters: List[LiquidityCluster] = []
        self.sweep_history: List[LiquiditySweep] = []
        self.session_high: Optional[float] = None
        self.session_low: Optional[float] = None
        self._atr: float = 0.0
        
    def add_candle(self, candle) -> None:
        """Add candle and detect liquidity"""
        self.candle_history.append(candle)
        if len(self.candle_history) > self.lookback_period:
            self.candle_history.pop(0)
        
        self._update_atr()
        self._detect_liquidity()
    
    def _update_atr(self, period: int = 14) -> None:
        """Calculate ATR for wick size validation"""
        if len(self.candle_history) < period:
            return
        
        trs = []
        for i in range(-period, 0):
            c = self.candle_history[i]
            prev_c = self.candle_history[i-1] if i > -period else c
            tr = max(
                c.high - c.low,
                abs(c.high - prev_c.close),
                abs(c.low - prev_c.close)
            )
            trs.append(tr)
        self._atr = sum(trs) / len(trs) if trs else 0
    
    def _detect_liquidity(self) -> None:
        """Main liquidity detection logic"""
        if len(self.candle_history) < 5:
            return
        
        self._detect_session_levels()
        self._detect_liquidity_sweeps()
        if self.allow_equal_hl:
            self._detect_equal_highs_lows()
        self._update_cluster_touches()
        self._score_clusters()
    
    def _detect_session_levels(self) -> None:
        """Track session high/low using rolling window EXCLUDING current candle."""
        if len(self.candle_history) < 2:
            if self.candle_history:
                self.session_high = self.candle_history[0].high
                self.session_low = self.candle_history[0].low
            return
        prev_candles = self.candle_history[:-1]
        self.session_high = max(c.high for c in prev_candles)
        self.session_low = min(c.low for c in prev_candles)
    
    def _detect_equal_highs_lows(self) -> None:
        """Detect equal highs and equal lows (stop clusters)"""
        if len(self.candle_history) < 3:
            return
        
        recent = self.candle_history[-20:]
        highs = [(i, c.high) for i, c in enumerate(recent)]
        lows = [(i, c.low) for i, c in enumerate(recent)]
        
        # Find equal highs (within tolerance)
        for i, (idx1, h1) in enumerate(highs):
            for idx2, h2 in highs[i+1:]:
                if abs(h2 - h1) / h1 < self.cluster_tolerance:
                    cluster = LiquidityCluster(
                        price=h1,
                        type="sellside",
                        strength=0.7,
                        timestamp=self.candle_history[idx1].timestamp,
                        size=2
                    )
                    if cluster not in self.liquidity_clusters:
                        self.liquidity_clusters.append(cluster)
        
        # Find equal lows (within tolerance)
        for i, (idx1, l1) in enumerate(lows):
            for idx2, l2 in lows[i+1:]:
                if abs(l2 - l1) / l1 < self.cluster_tolerance:
                    cluster = LiquidityCluster(
                        price=l1,
                        type="buyside",
                        strength=0.7,
                        timestamp=self.candle_history[idx1].timestamp,
                        size=2
                    )
                    if cluster not in self.liquidity_clusters:
                        self.liquidity_clusters.append(cluster)
    
    def _detect_liquidity_sweeps(self) -> None:
        """Detect liquidity grabs (sweep of session high/low)"""
        if len(self.candle_history) < 2:
            return
        
        current = self.candle_history[-1]
        prev = self.candle_history[-2]
        
        min_wick = self._atr * self.min_wick_atr if self._atr > 0 else 0
        
        # Buyside liquidity sweep (wick below session low, then recover)
        if current.low < self.session_low and prev.low >= self.session_low:
            displacement = self.session_low - current.low
            wick_size = current.close - current.low if current.close > current.low else 0
            retracement = (current.close - current.low) / (current.high - current.low) if current.high != current.low else 0
            
            # Check if close recovered beyond the swept level (stricter)
            close_beyond = current.close > self.session_low
            
            # Validate sweep
            is_valid = True
            reject_reason = ""
            
            if min_wick > 0 and wick_size < min_wick:
                is_valid = False
                reject_reason = f"wick={wick_size:.4f} < min={min_wick:.4f}"
            
            if self.require_close_beyond and not close_beyond:
                is_valid = False
                reject_reason = f"close={current.close:.2f} did not recover above session_low={self.session_low:.2f}"
            
            logger.info(
                f"[SWEEP_CHECK] BUYSIDE | session_low={self.session_low:.2f}"
                f" | swept_to={current.low:.2f} | displacement={displacement:.4f}"
                f" | wick={wick_size:.4f} | min_wick={min_wick:.4f}"
                f" | close={current.close:.2f} | close_beyond={close_beyond}"
                f" | valid={is_valid}" + (f" | reject={reject_reason}" if reject_reason else "")
            )
            
            sweep = LiquiditySweep(
                sweep_type=SweepType.BUYSIDE,
                sweep_price=current.low,
                timestamp=current.timestamp,
                displacement_size=displacement,
                retracement=retracement,
                wick_size=wick_size,
                is_valid=is_valid
            )
            if is_valid:
                self.sweep_history.append(sweep)
        
        # Sellside liquidity sweep (wick above session high, then reject)
        if current.high > self.session_high and prev.high <= self.session_high:
            displacement = current.high - self.session_high
            wick_size = current.high - current.close if current.high > current.close else 0
            retracement = (current.high - current.close) / (current.high - current.low) if current.high != current.low else 0
            
            # Check if close rejected below the swept level (stricter)
            close_beyond = current.close < self.session_high
            
            # Validate sweep
            is_valid = True
            reject_reason = ""
            
            if min_wick > 0 and wick_size < min_wick:
                is_valid = False
                reject_reason = f"wick={wick_size:.4f} < min={min_wick:.4f}"
            
            if self.require_close_beyond and not close_beyond:
                is_valid = False
                reject_reason = f"close={current.close:.2f} did not reject below session_high={self.session_high:.2f}"
            
            logger.info(
                f"[SWEEP_CHECK] SELLSIDE | session_high={self.session_high:.2f}"
                f" | swept_to={current.high:.2f} | displacement={displacement:.4f}"
                f" | wick={wick_size:.4f} | min_wick={min_wick:.4f}"
                f" | close={current.close:.2f} | close_beyond={close_beyond}"
                f" | valid={is_valid}" + (f" | reject={reject_reason}" if reject_reason else "")
            )
            
            sweep = LiquiditySweep(
                sweep_type=SweepType.SELLSIDE,
                sweep_price=current.high,
                timestamp=current.timestamp,
                displacement_size=displacement,
                retracement=retracement,
                wick_size=wick_size,
                is_valid=is_valid
            )
            if is_valid:
                self.sweep_history.append(sweep)
    
    def _update_cluster_touches(self) -> None:
        """Mark clusters that have been touched/swept"""
        if not self.liquidity_clusters:
            return
        
        current = self.candle_history[-1]
        
        for cluster in self.liquidity_clusters:
            if not cluster.touched:
                tolerance = cluster.price * self.cluster_tolerance
                
                if cluster.type == "sellside":
                    if current.high >= cluster.price - tolerance:
                        cluster.touched = True
                        cluster.touched_price = current.high
                else:  # buyside
                    if current.low <= cluster.price + tolerance:
                        cluster.touched = True
                        cluster.touched_price = current.low
    
    def _score_clusters(self) -> None:
        """Calculate strength of each cluster"""
        for cluster in self.liquidity_clusters:
            base_strength = 0.5
            
            # Strength increases if cluster was swept
            if cluster.touched:
                base_strength += 0.3
            
            # Strength increases if multiple candles tested it
            if cluster.size >= 3:
                base_strength += 0.2
            
            cluster.strength = min(1.0, base_strength)
    
    def get_recent_sweeps(self, count: int = 5) -> List[Dict]:
        """Get recent liquidity sweep events"""
        recent = self.sweep_history[-count:] if self.sweep_history else []
        return [
            {
                "type": s.sweep_type.value,
                "price": s.sweep_price,
                "displacement": s.displacement_size,
                "retracement": s.retracement,
                "wick_size": s.wick_size,
                "valid": s.is_valid
            }
            for s in recent
        ]
    
    def get_liquidity_zones(self) -> List[Dict]:
        """Get all active liquidity zones"""
        zones = []
        for cluster in self.liquidity_clusters[-10:]:  # Last 10 clusters
            zones.append({
                "price": cluster.price,
                "type": cluster.type,
                "strength": cluster.strength,
                "touched": cluster.touched,
                "size_tests": cluster.size
            })
        return zones
    
    def get_current_state(self) -> Dict:
        """Export current liquidity state with diagnostic info"""
        return {
            "session_high": self.session_high,
            "session_low": self.session_low,
            "atr": round(self._atr, 6) if self._atr else 0,
            "min_wick_threshold": round(self._atr * self.min_wick_atr, 6) if self._atr else 0,
            "lookback": self.lookback_period,
            "candle_count": len(self.candle_history),
            "recent_sweeps": len(self.sweep_history),
            "active_clusters": len(self.liquidity_clusters),
            "sweep_history": self.get_recent_sweeps(3),
            "liquidity_zones": self.get_liquidity_zones(),
            # Config echo for debugging
            "_config": {
                "min_wick_atr": self.min_wick_atr,
                "cluster_tolerance": self.cluster_tolerance,
                "allow_equal_hl": self.allow_equal_hl,
                "require_close_beyond": self.require_close_beyond,
            }
        }
