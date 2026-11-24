"""
Relative Strength Index (RSI) Indicator
"""
import numpy as np
from typing import List, Union

def calculate_rsi(closes: Union[List[float], np.ndarray], period: int = 14) -> float:
    """
    Calculate RSI for the last candle
    """
    if len(closes) < period + 1:
        return 50.0
        
    closes = np.array(closes)
    deltas = np.diff(closes)
    
    seed = deltas[:period+1]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / down
    rsi = 100. - 100. / (1. + rs)
    
    # Smooth calculation
    for i in range(period+1, len(closes)):
        delta = deltas[i-1]
        
        if delta > 0:
            up_val = delta
            down_val = 0.
        else:
            up_val = 0.
            down_val = -delta
            
        up = (up * (period - 1) + up_val) / period
        down = (down * (period - 1) + down_val) / period
        
        rs = up / down if down != 0 else 0
        rsi = 100. - 100. / (1. + rs)
        
    return rsi

class RSIIndicator:
    def __init__(self, period: int = 14):
        self.period = period
        
    def compute(self, candles: List[dict]) -> float:
        """Compute RSI from list of candle dicts (must have 'close')"""
        closes = [float(c['close']) for c in candles]
        return calculate_rsi(closes, self.period)
