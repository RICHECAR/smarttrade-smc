"""
Market Data Aggregator
Converts raw trades/ticks into OHLCV candles
"""

import time
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass

@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = False

class MarketDataAggregator:
    """
    Aggregates realtime trade data into candles (1m, 5m, etc)
    """
    def __init__(self, interval_sec: int = 60, on_candle_closed: Optional[Callable[[Dict], None]] = None):
        self.interval_sec = interval_sec
        self.current_candle: Optional[Candle] = None
        self.on_candle_closed = on_candle_closed
        self.candles: List[Dict] = []
        
    def process_trade(self, price: float, volume: float, timestamp: int = None):
        """Process a single trade"""
        if not timestamp:
            timestamp = int(time.time() * 1000)
            
        # Calculate candle start time (floor to interval)
        interval_ms = self.interval_sec * 1000
        candle_start = (timestamp // interval_ms) * interval_ms
        
        # Check if new candle needed
        if self.current_candle and candle_start > self.current_candle.timestamp:
            self.close_current_candle()
            
        if not self.current_candle:
            self.current_candle = Candle(
                timestamp=candle_start,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume
            )
        else:
            # Update current candle
            c = self.current_candle
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
            c.volume += volume
            
    def close_current_candle(self):
        """Finalize the current candle"""
        if not self.current_candle:
            return
            
        c = self.current_candle
        candle_dict = {
            "timestamp": c.timestamp,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume
        }
        
        self.candles.append(candle_dict)
        if len(self.candles) > 1000:
            self.candles.pop(0)
            
        if self.on_candle_closed:
            self.on_candle_closed(candle_dict)
            
        self.current_candle = None
        
    def get_history(self) -> List[Dict]:
        """Get candle history"""
        # Return closed candles + current partial if exists?
        # Usually strategy needs closed candles.
        return self.candles
