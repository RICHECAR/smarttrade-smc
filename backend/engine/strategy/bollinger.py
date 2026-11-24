"""
Bollinger Bands Indicator
"""
import numpy as np
from typing import List, Dict, Tuple

def calculate_bollinger_bands(closes: List[float], period: int = 20, std_dev: float = 2.0) -> Tuple[float, float, float]:
    """
    Calculate Bollinger Bands
    Returns: (upper, middle, lower)
    """
    if len(closes) < period:
        return 0.0, 0.0, 0.0
        
    recent = closes[-period:]
    middle = np.mean(recent)
    std = np.std(recent)
    
    upper = middle + (std * std_dev)
    lower = middle - (std * std_dev)
    
    return upper, middle, lower

class BollingerIndicator:
    def __init__(self, period: int = 20, std_dev: float = 2.0):
        self.period = period
        self.std_dev = std_dev
        
    def compute(self, candles: List[dict]) -> Dict[str, float]:
        closes = [float(c['close']) for c in candles]
        upper, middle, lower = calculate_bollinger_bands(closes, self.period, self.std_dev)
        return {
            "upper": upper,
            "middle": middle,
            "lower": lower,
            "bandwidth": (upper - lower) / middle if middle != 0 else 0
        }
