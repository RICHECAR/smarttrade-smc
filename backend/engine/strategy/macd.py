"""
Moving Average Convergence Divergence (MACD) Indicator
"""
import numpy as np
import pandas as pd
from typing import List, Dict, Tuple

def calculate_macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[float, float, float]:
    """
    Calculate MACD, Signal, Histogram
    Returns: (macd_line, signal_line, histogram)
    """
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
        
    prices = pd.Series(closes)
    exp1 = prices.ewm(span=fast, adjust=False).mean()
    exp2 = prices.ewm(span=slow, adjust=False).mean()
    
    macd = exp1 - exp2
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram = macd - signal_line
    
    return macd.iloc[-1], signal_line.iloc[-1], histogram.iloc[-1]

class MACDIndicator:
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast = fast
        self.slow = slow
        self.signal = signal
        
    def compute(self, candles: List[dict]) -> Dict[str, float]:
        closes = [float(c['close']) for c in candles]
        macd, sig, hist = calculate_macd(closes, self.fast, self.slow, self.signal)
        return {
            "macd": macd,
            "signal": sig,
            "histogram": hist
        }
