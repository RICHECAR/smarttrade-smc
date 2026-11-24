import numpy as np
from typing import List, Optional

def calculate_log_return_sigma(prices: List[float], window: int = 20) -> Optional[float]:
    """
    Calculate the standard deviation of log returns (volatility sigma).
    
    Args:
        prices: List of prices (e.g. 1m close prices)
        window: Rolling window size
        
    Returns:
        Sigma value or None if not enough data
    """
    if len(prices) < window + 1:
        return None
        
    # Take the last window + 1 prices
    segment = prices[-(window + 1):]
    
    # Validation: Ensure no zero/negative prices before log
    if any(p <= 0 for p in segment):
        return None
        
    # Calculate log returns: ln(P_t / P_{t-1})
    try:
        log_returns = np.diff(np.log(segment))
        
        # Calculate standard deviation
        sigma = np.std(log_returns)
        
        if not np.isfinite(sigma):
            return None
            
        return float(sigma)
    except Exception:
        return None
