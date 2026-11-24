"""
Mean Reversion Envelope Strategy
Based on SMA and multiple envelope tiers for grid trading.
"""

from typing import Dict, List, Optional
import pandas as pd
import ta
import logging

logger = logging.getLogger(__name__)

class EnvelopeStrategy:
    """
    Mean Reversion Strategy using SMA Envelopes
    - Calculates SMA (Basis)
    - Calculates multiple tiers of Low and High Envelopes
    - Generates a grid of limit orders
    """
    
    def __init__(
        self,
        base_window: int = 5,
        envelopes: List[float] = [0.07, 0.10, 0.12],
        src: str = "close"
    ):
        self.base_window = base_window
        self.envelopes = envelopes
        self.src_col = src
        self.high_envelopes = [round(1/(1-e)-1, 3) for e in envelopes]
        self.last_levels: Dict = {}
        
    def calculate_levels(self, df: pd.DataFrame) -> Dict:
        """
        Calculate MA and Envelope levels for the current state.
        
        Args:
            df: DataFrame with OHLCV data
            
        Returns:
            Dict containing current levels (ma_base, ma_high_1..N, ma_low_1..N)
        """
        if len(df) < self.base_window + 1:
            return {}
            
        temp_df = df.copy()
        
        # Calculate MA base on previous candles (shift 1 to avoid lookahead)
        temp_df['ma_base'] = ta.trend.sma_indicator(close=temp_df[self.src_col], window=self.base_window).shift(1)
        
        for i, (e_low, e_high) in enumerate(zip(self.envelopes, self.high_envelopes), 1):
            temp_df[f'ma_high_{i}'] = temp_df['ma_base'] * (1 + e_high)
            temp_df[f'ma_low_{i}'] = temp_df['ma_base'] * (1 - e_low)
            
        latest = temp_df.iloc[-1]
        
        levels = {
            "ma_base": float(latest["ma_base"]),
            "high": [float(latest[f"ma_high_{i}"]) for i in range(1, len(self.envelopes) + 1)],
            "low": [float(latest[f"ma_low_{i}"]) for i in range(1, len(self.envelopes) + 1)]
        }
        
        self.last_levels = levels
        return levels

    def get_current_state(self) -> Dict:
        """Return the last calculated levels for dashboard display"""
        return self.last_levels

    def generate_grid(
        self,
        levels: Dict,
        balance: float,
        leverage: float = 1.0,
        active_positions: List[Dict] = [],
        open_orders: List[Dict] = []
    ) -> Dict:
        """
        Generate the desired grid of orders based on current levels and account state.
        
        Returns:
            Dict containing:
                - orders_to_place: List of new orders to create
                - positions_to_close: List of directions to close at MA
        """
        if not levels:
            return {"orders_to_place": [], "close_at_ma": False}

        orders_to_place = []
        total_buying_power = balance * leverage
        
        # 1. Close logic: If we have positions, they should close at MA
        has_long = any(p["side"] == "long" for p in active_positions)
        has_short = any(p["side"] == "short" for p in active_positions)
        
        # 2. Grid logic
        # Count existing buy/sell orders that aren't reduceOnly
        buy_orders_count = sum(1 for o in open_orders if o["side"] == "buy" and not o.get("reduceOnly", False))
        sell_orders_count = sum(1 for o in open_orders if o["side"] == "sell" and not o.get("reduceOnly", False))
        
        # We want a total of N envelopes active as orders (or filled as positions)
        num_tiers = len(self.envelopes)
        
        # Calculate amount per tier
        # In the user script: amount = (balance / len(envelopes)) / price
        
        # Place Byte orders for Low Envelopes
        for i in range(num_tiers):
            price = levels["low"][i]
            amount = (balance / num_tiers) / price
            orders_to_place.append({
                "side": "buy",
                "price": price,
                "amount": amount,
                "reduce": False,
                "tier": i + 1
            })
            
        # Place Sell orders for High Envelopes
        for i in range(num_tiers):
            price = levels["high"][i]
            amount = (balance / num_tiers) / price
            orders_to_place.append({
                "side": "sell",
                "price": price,
                "amount": amount,
                "reduce": False,
                "tier": i + 1
            })
            
        return {
            "orders_to_place": orders_to_place,
            "close_at_ma": has_long or has_short,
            "ma_price": levels["ma_base"]
        }
