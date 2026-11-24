import logging
from enum import Enum
from typing import Dict, Any, Optional
from smc_futures.smc_config import SMC_CONFIG

logger = logging.getLogger(__name__)

class SessionType(Enum):
    ASIA = "asia"
    LONDON = "london"
    NY = "ny"
    UNKNOWN = "unknown"

class AdaptiveThresholdManager:
    """
    Manages dynamic range thresholds based on market volatility (ATR),
    trading session, and recent performance KPIs.
    """
    
    def __init__(self):
        # Base thresholds per session
        self.session_bases = {
            SessionType.ASIA: {
                "BTC": SMC_CONFIG.get("ASIA_MAX_RANGE_RATIO_BTC", 6.4),
                "ETH": SMC_CONFIG.get("ASIA_MAX_RANGE_RATIO_ETH", 5.2),
                "ALT": SMC_CONFIG.get("ASIA_MAX_RANGE_RATIO_ALT", 5.7)
            },
            SessionType.LONDON: {
                "BTC": 7.0,
                "ETH": 6.0,
                "ALT": 6.5
            },
            SessionType.NY: {
                "BTC": 7.5,
                "ETH": 6.5,
                "ALT": 7.0
            }
        }
        
        # Hard safety rails
        self.min_ratio = SMC_CONFIG.get("ADAPTIVE_MIN_RATIO", 4.0)
        self.max_ratio = SMC_CONFIG.get("ADAPTIVE_MAX_RATIO", 8.0)

    def _get_asset_type(self, symbol: str) -> str:
        """Categorize symbol into BTC, ETH, or ALT."""
        if "BTC" in symbol: return "BTC"
        if "ETH" in symbol: return "ETH"
        return "ALT"

    def get_threshold(self, 
                      symbol: str, 
                      session_str: str, 
                      current_atr: float, 
                      avg_atr: float, 
                      recent_winrate: Optional[float] = None) -> float:
        """
        Calculate the adaptive threshold for a given context.
        
        Args:
            symbol: Trading symbol (e.g., BTCUSDT)
            session_str: Current session name
            current_atr: Recent ATR (volatility)
            avg_atr: Baseline ATR (moving average)
            recent_winrate: Optional rolling win rate (0-100)
            
        Returns:
            The calculated threshold (range_ratio limit)
        """
        try:
            # 1. Map session
            try:
                session = SessionType(session_str.lower())
            except ValueError:
                session = SessionType.UNKNOWN
                
            asset_type = self._get_asset_type(symbol)
            
            # 2. Get base threshold
            # Fallback to ASIA if session unknown or config missing
            base_config = self.session_bases.get(session, self.session_bases[SessionType.ASIA])
            threshold = base_config.get(asset_type, 6.0)
            
            # 3. Volatility Scaling (ATR-based)
            # If current volatility is significantly higher than average, tighten the threshold
            if avg_atr > 0:
                vol_ratio = current_atr / avg_atr
                if vol_ratio > 1.3:
                    # High volatility: Tighten range (e.g., reduce threshold by 10-20%)
                    adjustment = 0.9 if vol_ratio > 1.5 else 0.95
                    threshold *= adjustment
                    logger.debug(f"[ADAPTIVE] {symbol} | Volatility adjustment: {vol_ratio:.2f}x -> multiplier {adjustment}")
                elif vol_ratio < 0.8:
                    # Low volatility: Relax range slightly (e.g., increase threshold by 5%)
                    threshold *= 1.05
                    logger.debug(f"[ADAPTIVE] {symbol} | Low vol adjustment: {vol_ratio:.2f}x -> multiplier 1.05")

            # 4. Performance Coupling (Optional)
            if recent_winrate is not None:
                if recent_winrate < 40:
                    # Poor recent performance: Tighten threshold aggressively
                    threshold *= 0.85
                    logger.info(f"[ADAPTIVE] {symbol} | Performance penalty: winrate={recent_winrate:.1f}% -> multiplier 0.85")
                elif recent_winrate > 55:
                    # Great performance: Relax slightly
                    threshold *= 1.05
            
            # 5. Hard Safety Rails
            final_threshold = max(self.min_ratio, min(threshold, self.max_ratio))
            
            logger.info(
                f"[ADAPTIVE] {symbol} | session={session_str} | base={base_config.get(asset_type)} "
                f"| final={final_threshold:.2f} | vol_ratio={current_atr/avg_atr if avg_atr > 0 else 1:.2f}"
            )
            
            return final_threshold
            
        except Exception as e:
            logger.error(f"[ADAPTIVE] Error calculating threshold for {symbol}: {e}")
            # Safe default fallback from config
            return SMC_CONFIG.get(f"ASIA_MAX_RANGE_RATIO_{self._get_asset_type(symbol)}", 6.0)
