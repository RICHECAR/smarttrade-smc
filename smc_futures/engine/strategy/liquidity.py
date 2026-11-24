"""
Smart Money Concept - Liquidity Engine
Detects: Stop clusters, liquidity pools, sweeps, engineered liquidity grabs
         Heatmap scoring, breakout pools, density-based clusters
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


@dataclass
class LiquidityLevel:
    """Scored liquidity level for the heatmap."""
    price: float
    side: str           # "buyside" (below price → longs' stops) or "sellside" (above → shorts' stops)
    score: float = 0.0  # composite score — higher = more attractive target
    # Component breakdown
    equal_hl_hits: int = 0
    cluster_density: int = 0
    breakout_pool: bool = False
    htf_swing: bool = False
    swept: bool = False
    timestamp: int = 0

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
        # Heatmap config
        heatmap_enabled: bool = True,
        heatmap_cluster_pct: float = 0.001,   # 0.1% — highs/lows within this % are a cluster
        heatmap_min_cluster_size: int = 3,     # minimum touches to form a stop cluster
        heatmap_breakout_sl_atr: float = 0.5,  # breakout traders' SL = range edge ± 0.5 ATR
        heatmap_max_levels: int = 20,          # max scored levels to retain
        # Scoring weights
        weight_equal_hl: float = 2.0,
        weight_cluster_density: float = 3.0,
        weight_breakout: float = 1.5,
        weight_htf_level: float = 2.0,
    ):
        self.lookback_period = lookback_period
        self.cluster_tolerance = cluster_tolerance
        self.min_wick_atr = min_wick_atr
        self.allow_equal_hl = allow_equal_hl
        self.require_close_beyond = require_close_beyond
        
        # Heatmap
        self._heatmap_enabled = heatmap_enabled
        self._heatmap_cluster_pct = heatmap_cluster_pct
        self._heatmap_min_cluster_size = heatmap_min_cluster_size
        self._heatmap_breakout_sl_atr = heatmap_breakout_sl_atr
        self._heatmap_max_levels = heatmap_max_levels
        self._w_equal_hl = weight_equal_hl
        self._w_cluster = weight_cluster_density
        self._w_breakout = weight_breakout
        self._w_htf = weight_htf_level
        
        self.candle_history = []
        self.liquidity_clusters: List[LiquidityCluster] = []
        self.sweep_history: List[LiquiditySweep] = []
        self.session_high: Optional[float] = None
        self.session_low: Optional[float] = None
        self._atr: float = 0.0
        
        # Heatmap state
        self._heatmap_levels: List[LiquidityLevel] = []
        self._htf_swing_highs: List[float] = []
        self._htf_swing_lows: List[float] = []
        self._breakout_ranges: List[Tuple[float, float, int]] = []  # (high, low, timestamp)
        
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
        
        # IMPORTANT: detect session levels FIRST (excludes current candle)
        # then check sweeps so the current candle can sweep previous range
        self._detect_session_levels()
        self._detect_liquidity_sweeps()
        if self.allow_equal_hl:
            self._detect_equal_highs_lows()
        self._update_cluster_touches()
        self._score_clusters()
        
        # Heatmap: density clusters + breakout pools + scoring
        if self._heatmap_enabled and len(self.candle_history) >= 10:
            self._detect_stop_clusters()
            self._detect_breakout_pools()
            self._build_heatmap()
    
    def _detect_session_levels(self) -> None:
        """Track session high/low using rolling window EXCLUDING current candle.
        This way the current candle can actually sweep the previous range."""
        if len(self.candle_history) < 2:
            if self.candle_history:
                self.session_high = self.candle_history[0].high
                self.session_low = self.candle_history[0].low
            return
        prev_candles = self.candle_history[:-1]
        self.session_high = max(c.high for c in prev_candles)
        self.session_low = min(c.low for c in prev_candles)
    
    def _detect_equal_highs_lows(self) -> None:
        """Detect equal highs and equal lows (stop clusters) - Optimized"""
        if len(self.candle_history) < 3:
            return
        
        # Only check pairs involving the NEWEST candle to avoid O(N^2) every time
        current_idx = len(self.candle_history) - 1
        current_candle = self.candle_history[current_idx]
        
        recent = self.candle_history[-20:-1] # Previous 19 candles
        
        # Check current high against recent highs
        for i, prev_candle in enumerate(recent):
            if abs(current_candle.high - prev_candle.high) / prev_candle.high < self.cluster_tolerance:
                cluster = LiquidityCluster(
                    price=prev_candle.high,
                    type="sellside",
                    strength=0.7,
                    timestamp=prev_candle.timestamp,
                    size=2
                )
                if not self._cluster_exists(cluster):
                    self.liquidity_clusters.append(cluster)
                    break # One cluster per newest candle is enough
        
        # Check current low against recent lows
        for i, prev_candle in enumerate(recent):
            if abs(current_candle.low - prev_candle.low) / prev_candle.low < self.cluster_tolerance:
                cluster = LiquidityCluster(
                    price=prev_candle.low,
                    type="buyside",
                    strength=0.7,
                    timestamp=prev_candle.timestamp,
                    size=2
                )
                if not self._cluster_exists(cluster):
                    self.liquidity_clusters.append(cluster)
                    break

        # Cap clusters and sweeps to prevent memory/CPU leak
        if len(self.liquidity_clusters) > 50:
            self.liquidity_clusters.pop(0)
        if len(self.sweep_history) > 50:
            self.sweep_history.pop(0)

    def _cluster_exists(self, new_cluster: LiquidityCluster) -> bool:
        """Efficiently check if cluster already exists"""
        # Only check the last 10 clusters for performance
        for c in self.liquidity_clusters[-10:]:
            if c.type == new_cluster.type and abs(c.price - new_cluster.price) / c.price < self.cluster_tolerance:
                 return True
        return False
    
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

    # ──────────────────────────────────────────────────────
    #  Liquidity Heatmap — stop clusters, breakout pools, HTF levels
    # ──────────────────────────────────────────────────────

    def update_htf_levels(self, structure_state: Dict) -> None:
        """
        Receive HTF swing highs/lows from the StructureEngine.
        Called by the orchestrator after structure detection.
        """
        # Extract consolidated highs/lows from structure state
        self._htf_swing_highs = list(structure_state.get("consolidated_highs", []))
        self._htf_swing_lows = list(structure_state.get("consolidated_lows", []))
        # Also use last swing high/low (returned as float by StructureEngine)
        sh = structure_state.get("last_swing_high")
        sl = structure_state.get("last_swing_low")
        if sh and isinstance(sh, (int, float)) and sh > 0:
            if sh not in self._htf_swing_highs:
                self._htf_swing_highs.append(sh)
        if sl and isinstance(sl, (int, float)) and sl > 0:
            if sl not in self._htf_swing_lows:
                self._htf_swing_lows.append(sl)

    def _detect_stop_clusters(self) -> None:
        """
        Density-based stop cluster detection.
        Groups highs/lows within `heatmap_cluster_pct` and counts touches.
        A cluster of ≥ min_cluster_size consecutive highs at similar price
        represents accumulated stop-loss orders.
        """
        if len(self.candle_history) < self._heatmap_min_cluster_size:
            return

        highs = [c.high for c in self.candle_history]
        lows = [c.low for c in self.candle_history]
        threshold = self._heatmap_cluster_pct

        # Cluster highs (sellside liquidity — shorts' stops above)
        self._cluster_from_prices(highs, "sellside", threshold)
        # Cluster lows (buyside liquidity — longs' stops below)
        self._cluster_from_prices(lows, "buyside", threshold)

    def _cluster_from_prices(
        self, prices: List[float], side: str, threshold: float
    ) -> None:
        """Group prices within `threshold` % and create levels for dense clusters."""
        if not prices:
            return
        sorted_prices = sorted(prices)
        i = 0
        while i < len(sorted_prices):
            ref = sorted_prices[i]
            cluster_prices = [ref]
            j = i + 1
            while j < len(sorted_prices) and abs(sorted_prices[j] - ref) / ref < threshold:
                cluster_prices.append(sorted_prices[j])
                j += 1
            if len(cluster_prices) >= self._heatmap_min_cluster_size:
                avg_price = sum(cluster_prices) / len(cluster_prices)
                # Only add if we don't already have a close level
                if not self._heatmap_level_exists(avg_price, side):
                    level = LiquidityLevel(
                        price=round(avg_price, 8),
                        side=side,
                        cluster_density=len(cluster_prices),
                        timestamp=self.candle_history[-1].timestamp,
                    )
                    self._heatmap_levels.append(level)
            i = j  # skip past the cluster

    def _heatmap_level_exists(self, price: float, side: str) -> bool:
        """Check if a level close to `price` already exists in the heatmap."""
        for lv in self._heatmap_levels:
            if lv.side == side and abs(lv.price - price) / price < self._heatmap_cluster_pct:
                return True
        return False

    def _detect_breakout_pools(self) -> None:
        """
        When price breaks a range, breakout traders enter and place stops
        on the other side. Those stops become a liquidity pool.
        
        Example: range high = 68000, price breaks above → breakout longs have SL ≈ 67950
        → buyside liquidity pool just below the broken range high.
        """
        if self._atr <= 0 or self.session_high is None or self.session_low is None:
            return

        current = self.candle_history[-1]
        sl_offset = self._atr * self._heatmap_breakout_sl_atr

        # Breakout above session high → pool below (buyside)
        if current.close > self.session_high:
            pool_price = round(self.session_high - sl_offset, 8)
            if not self._heatmap_level_exists(pool_price, "buyside"):
                level = LiquidityLevel(
                    price=pool_price,
                    side="buyside",
                    breakout_pool=True,
                    timestamp=current.timestamp,
                )
                self._heatmap_levels.append(level)

        # Breakout below session low → pool above (sellside)
        if current.close < self.session_low:
            pool_price = round(self.session_low + sl_offset, 8)
            if not self._heatmap_level_exists(pool_price, "sellside"):
                level = LiquidityLevel(
                    price=pool_price,
                    side="sellside",
                    breakout_pool=True,
                    timestamp=current.timestamp,
                )
                self._heatmap_levels.append(level)

    def _build_heatmap(self) -> None:
        """
        Score every LiquidityLevel using weighted components:
          score = equal_hl_hits × w_eq + cluster_density × w_cl
                  + breakout × w_br + htf_swing × w_htf
        Removes swept levels and caps the list.
        """
        current_price = self.candle_history[-1].close if self.candle_history else 0

        # Enrich levels with equal-HL and HTF data
        for lv in self._heatmap_levels:
            # ── Equal HL enrichment ──
            for cluster in self.liquidity_clusters:
                if cluster.type == lv.side and abs(cluster.price - lv.price) / lv.price < self._heatmap_cluster_pct:
                    lv.equal_hl_hits = max(lv.equal_hl_hits, int(cluster.size))

            # ── HTF swing level enrichment ──
            swing_list = self._htf_swing_highs if lv.side == "sellside" else self._htf_swing_lows
            for sp in swing_list:
                if abs(sp - lv.price) / lv.price < self._heatmap_cluster_pct * 2:
                    lv.htf_swing = True
                    break

            # ── Mark swept (already taken out) ──
            if current_price > 0:
                if lv.side == "sellside" and current_price > lv.price:
                    lv.swept = True
                elif lv.side == "buyside" and current_price < lv.price:
                    lv.swept = True

            # ── Composite score ──
            lv.score = (
                lv.equal_hl_hits * self._w_equal_hl
                + lv.cluster_density * self._w_cluster
                + (self._w_breakout if lv.breakout_pool else 0)
                + (self._w_htf if lv.htf_swing else 0)
            )

        # Remove swept levels (liquidity already taken)
        self._heatmap_levels = [lv for lv in self._heatmap_levels if not lv.swept]

        # Sort by score descending, keep top N
        self._heatmap_levels.sort(key=lambda lv: lv.score, reverse=True)
        self._heatmap_levels = self._heatmap_levels[:self._heatmap_max_levels]

    def get_liquidity_heatmap(self) -> Dict:
        """
        Public API: returns the scored liquidity heatmap.
        Strategy uses this to know where the market is most likely headed.
        """
        if not self._heatmap_levels:
            return {"levels": [], "major_target_above": None, "major_target_below": None}

        current_price = self.candle_history[-1].close if self.candle_history else 0
        above = [lv for lv in self._heatmap_levels if lv.price > current_price]
        below = [lv for lv in self._heatmap_levels if lv.price <= current_price]

        major_above = max(above, key=lambda lv: lv.score) if above else None
        major_below = max(below, key=lambda lv: lv.score) if below else None

        def _fmt(lv: LiquidityLevel) -> Dict:
            return {
                "price": lv.price,
                "side": lv.side,
                "score": round(lv.score, 2),
                "cluster_density": lv.cluster_density,
                "equal_hl_hits": lv.equal_hl_hits,
                "breakout_pool": lv.breakout_pool,
                "htf_swing": lv.htf_swing,
            }

        return {
            "levels": [_fmt(lv) for lv in self._heatmap_levels[:10]],
            "major_target_above": _fmt(major_above) if major_above else None,
            "major_target_below": _fmt(major_below) if major_below else None,
            "total_levels": len(self._heatmap_levels),
        }
    
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
        state = {
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
        # Heatmap (if enabled)
        if self._heatmap_enabled:
            state["heatmap"] = self.get_liquidity_heatmap()
        return state
