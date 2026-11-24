"""
Futures Risk Manager
Enforces position limits, drawdown protection, and stop-loss/take-profit management
"""

import time
import json
from typing import Dict, List, Optional
from dataclasses import dataclass

@dataclass
class RiskLimits:
    max_risk_per_trade: float = 2.0      # % of account
    max_daily_risk: float = 5.0          # % of account
    max_positions: int = 5
    max_drawdown: float = 20.0           # % (stop trading)
    min_balance: float = 100.0           # USDT
    max_leverage: int = 20

@dataclass
class TradeRecord:
    symbol: str
    side: str
    entry_price: float
    quantity: float
    leverage: int
    timestamp: int
    status: str = "OPEN" # OPEN, CLOSED
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    closed_at: Optional[int] = None

class FuturesRiskManager:
    """
    Manages trading risk: validations, limits, and kill switch.
    """
    
    def __init__(self, limits: Optional[Dict] = None):
        self.limits = RiskLimits(**(limits or {}))
        self.kill_switch = False
        self.balance_history: List[float] = []
        self.trades: List[TradeRecord] = []
        self.daily_start_balance = 0.0
        self.last_reset_day = 0
        
    def update_balance(self, balance: float):
        """Update current balance and track drawdown"""
        self.balance_history.append(balance)
        
        # Reset daily tracking if new day
        current_day = int(time.time() / 86400)
        if current_day > self.last_reset_day:
            self.daily_start_balance = balance
            self.last_reset_day = current_day
            
        # Check drawdown
        if self._calculate_drawdown(balance) > self.limits.max_drawdown:
            print(f"[RISK] Max drawdown exceeded! Activating Kill Switch.")
            self.kill_switch = True
            
    def check_trade_risk(self, symbol: str, size: float, entry_price: float, stop_loss: float, balance: float) -> Tuple[bool, str]:
        """
        Validate if a new trade is allowed
        """
        if self.kill_switch:
            return False, "Kill switch is active"
            
        if balance < self.limits.min_balance:
            return False, f"Insufficient balance ({balance} < {self.limits.min_balance})"
            
        # 1. Check max positions
        open_positions = [t for t in self.trades if t.status == "OPEN"]
        if len(open_positions) >= self.limits.max_positions:
            return False, f"Max positions reached ({len(open_positions)})"
            
        # 2. Check risk per trade
        risk_amount = abs(entry_price - stop_loss) * size
        risk_percent = (risk_amount / balance) * 100
        
        if risk_percent > self.limits.max_risk_per_trade:
             return False, f"Risk per trade too high ({risk_percent:.2f}% > {self.limits.max_risk_per_trade}%)"
             
        # 3. Check daily risk limit
        daily_pnl = balance - self.daily_start_balance
        daily_loss_limit = -(self.daily_start_balance * (self.limits.max_daily_risk / 100))
        
        # If we are already down for the day, check if this additional risk would breach limit
        if daily_pnl - risk_amount < daily_loss_limit:
            return False, f"Daily risk limit would be breached"

        return True, "Allowed"

    def record_trade_open(self, trade: Dict):
        """Record new open trade"""
        record = TradeRecord(
            symbol=trade['symbol'],
            side=trade['side'],
            entry_price=trade['entry_price'],
            quantity=trade['quantity'],
            leverage=trade['leverage'],
            timestamp=int(time.time() * 1000)
        )
        self.trades.append(record)
        
    def record_trade_close(self, symbol: str, exit_price: float, pnl: float):
        """Close an open trade"""
        for trade in reversed(self.trades):
            if trade.symbol == symbol and trade.status == "OPEN":
                trade.status = "CLOSED"
                trade.exit_price = exit_price
                trade.pnl = pnl
                trade.closed_at = int(time.time() * 1000)
                break
                
    def _calculate_drawdown(self, current_balance: float) -> float:
        """Calculate drawdown from peak"""
        if not self.balance_history:
            return 0.0
        peak = max(self.balance_history)
        if peak == 0: return 0.0
        return ((peak - current_balance) / peak) * 100
        
    def get_state(self) -> Dict:
        """Get risk manager state"""
        return {
            "kill_switch": self.kill_switch,
            "open_positions": len([t for t in self.trades if t.status == "OPEN"]),
            "daily_drawdown": 0.0, # Todo: calc based on daily start
            "total_drawdown": self._calculate_drawdown(self.balance_history[-1] if self.balance_history else 0)
        }
