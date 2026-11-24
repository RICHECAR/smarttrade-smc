"""
Order Manager
Handles high-level order execution, risk checks, and trade recording.
Wrapper around BitgetFuturesClient and FuturesRiskManager.
"""

import time
import asyncio
from typing import Dict, Optional, Tuple

from .bitget_futures import BitgetFuturesClient
from ..risk.futures_risk import FuturesRiskManager
from ..sync.trade_history_sync import TradeHistorySync
from ..learning.trade_memory import TradeMemory, TradeOutcome

class OrderManager:
    """
    Executes trading signals safely.
    """
    def __init__(
        self,
        client: BitgetFuturesClient,
        risk_manager: FuturesRiskManager,
        sync: TradeHistorySync,
        memory: TradeMemory
    ):
        self.client = client
        self.risk = risk_manager
        self.sync = sync
        self.memory = memory
        self.leverage = 20 # Default or config
        
    async def execute_signal(self, signal: Dict, current_balance: float) -> bool:
        """
        Execute a trading signal:
        1. Validate with Risk Manager
        2. Calculate Size
        3. Place Order
        4. Record Trade
        """
        symbol = "BTCUSDT" # V2 MVP focus on BTC
        direction = signal['direction']
        entry_price = signal['entry']
        stop_loss = signal['stop_loss']
        take_profit = signal['take_profit']
        
        # 1. Calculate Risk & Size
        # Risk per trade = % of balance
        risk_per_trade_percent = self.risk.limits.max_risk_per_trade
        risk_amount_usdt = current_balance * (risk_per_trade_percent / 100)
        
        # Risk per unit = |Entry - SL|
        risk_per_unit = abs(entry_price - stop_loss)
        
        if risk_per_unit == 0:
            print("[EXEC] Invalid SL (Risk per unit is 0)")
            return False
            
        # Size = Risk Amount / Risk Per Unit
        # e.g. Fail if size exceeds max leverage
        quantity = risk_amount_usdt / risk_per_unit
        
        # Check Max Leverage constraint
        # Notional Value = Quantity * Entry Price
        # Margin Required = Notional / Leverage
        # Ensure Notional <= Balance * MaxLeverage
        max_notional = current_balance * self.risk.limits.max_leverage
        if quantity * entry_price > max_notional:
            quantity = max_notional / entry_price
            print(f"[EXEC] Size capped by leverage to {quantity}")
            
        # Round quantity (BTC usually 3 decimals, check symbol rules)
        quantity = round(quantity, 3) 
        if quantity <= 0:
            print("[EXEC] Calculated quantity is 0")
            return False

        # 2. Risk Check
        allowed, reason = self.risk.check_trade_risk(
            symbol=symbol,
            size=quantity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            balance=current_balance
        )
        
        if not allowed:
            print(f"[EXEC] Risk Check Failed: {reason}")
            return False
            
        # 3. Execute Order
        print(f"[EXEC] Placing {direction} order for {quantity} {symbol} @ {entry_price}")
        
        side = "buy" if direction == "long" else "sell"
        
        # A. Set Leverage (idempotent usually)
        await self.client.set_leverage(symbol, self.leverage)
        
        # B. Place Market Order (Simplification for MVP, ideal is Limit)
        # Using market for instant entry as 'entry_builder' confirmation implies we are ready
        try:
            order_res = await self.client.place_order(
                symbol=symbol,
                side=side,
                order_type="market",
                quantity=quantity,
                preset_stop_loss=stop_loss,
                preset_take_profit=take_profit
            )
            
            if order_res.get("code") != "00000":
                print(f"[EXEC] Order Placement Failed: {order_res}")
                return False
                
            print(f"[EXEC] Order Placed: {order_res.get('data')}")
            
            # 4. Record Trade
            trade_data = {
                "symbol": symbol,
                "side": direction.upper(), # 'LONG'/'SHORT' match frontend
                "entry_price": entry_price, # Approx, actual execution price might differ
                "quantity": quantity,
                "leverage": self.leverage
            }
            
            self.risk.record_trade_open(trade_data)
            await self.sync.sync_trade(trade_data) # Sync to node
            
            return True
            
        except Exception as e:
            print(f"[EXEC] Execution Exception: {e}")
            return False

    async def close_position(self, symbol: str):
        """Force close"""
        print(f"[EXEC] Closing position for {symbol}")
        res = await self.client.close_position(symbol)
        if res.get("code") == "00000":
             # We need actual fill price to calculate PnL correctly
             # For now, approximate or fetch pos details
             # In a real engine, we'd wait for ws fill update.
             # Here we just mark it closed in risk manager
             # Assuming close at market price (fetch current?)
             # self.risk.record_trade_close(...)
             pass
        return res
