"""
SMC Futures Backtest Engine
Simulate strategy performance on historical data
"""

from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import numpy as np

@dataclass
class BacktestTrade:
    """Executed trade in backtest"""
    entry_time: int
    entry_price: float
    stop_loss: float
    take_profit: float
    direction: str  # "long" or "short"
    quantity: float
    
    exit_time: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # "tp", "sl", "invalidation"
    
    pnl: float = 0.0
    pnl_percent: float = 0.0
    rr_achieved: float = 0.0
    
    setup_type: str = "OB"
    confidence: float = 0.5

@dataclass
class BacktestMetrics:
    """Backtest performance metrics"""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    
    win_rate: float = 0.0
    avg_win_percent: float = 0.0
    avg_loss_percent: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_percent: float = 0.0
    
    avg_rr_achieved: float = 0.0
    best_trade: Optional[BacktestTrade] = None
    worst_trade: Optional[BacktestTrade] = None
    
    start_balance: float = 10000.0
    final_balance: float = 10000.0
    
    trades: List[BacktestTrade] = field(default_factory=list)

class SMCBacktester:
    """
    Backtest SMC strategy on historical candle data
    """
    
    def __init__(self, initial_balance: float = 10000.0, risk_per_trade: float = 1.0):
        self.initial_balance = initial_balance
        self.risk_per_trade = risk_per_trade
        self.current_balance = initial_balance
        
        self.metrics = BacktestMetrics(start_balance=initial_balance)
        self.equity_curve: List[float] = [initial_balance]
        self.active_position: Optional[BacktestTrade] = None
        
    def run_backtest(
        self,
        candle_history: List[Dict],
        signal_generator_fn,  # Function that generates signals
        risk_engine_fn  # Function that sizes positions
    ) -> BacktestMetrics:
        """
        Run backtest on historical price data
        
        Args:
            candle_history: List of OHLCV candles
            signal_generator_fn: Function(candles) -> SMCSignal or None
            risk_engine_fn: Function(signal, balance) -> position_size
        """
        
        for i in range(len(candle_history)):
            current_candle = candle_history[i]
            
            # Check if active position should close
            if self.active_position:
                self._check_position_exit(current_candle)
            
            # Generate new signal
            signal = signal_generator_fn(candle_history[:i+1])
            
            if signal and not self.active_position:
                # Size position
                pos_size = risk_engine_fn(signal, self.current_balance)
                
                # Enter trade
                self.active_position = BacktestTrade(
                    entry_time=current_candle.get("timestamp"),
                    entry_price=signal.get("entry"),
                    stop_loss=signal.get("stop_loss"),
                    take_profit=signal.get("take_profit"),
                    direction=signal.get("direction"),
                    quantity=pos_size,
                    setup_type=signal.get("setup", "OB"),
                    confidence=signal.get("confidence", 0.5)
                )
            
            # Update equity
            if self.active_position:
                self._update_position_equity(current_candle)
            
            self.equity_curve.append(self.current_balance)
        
        # Calculate metrics
        self._calculate_metrics()
        
        return self.metrics
    
    def _check_position_exit(self, candle: Dict) -> None:
        """Check if position hits SL, TP, or invalidation"""
        if not self.active_position:
            return
        
        direction = self.active_position.direction
        entry = self.active_position.entry_price
        sl = self.active_position.stop_loss
        tp = self.active_position.take_profit
        
        high = candle.get("high")
        low = candle.get("low")
        close = candle.get("close")
        
        exit_price = None
        exit_reason = None
        
        if direction == "long":
            # TP hit
            if high >= tp:
                exit_price = tp
                exit_reason = "tp"
            # SL hit
            elif low <= sl:
                exit_price = sl
                exit_reason = "sl"
        
        else:  # short
            # TP hit
            if low <= tp:
                exit_price = tp
                exit_reason = "tp"
            # SL hit
            elif high >= sl:
                exit_price = sl
                exit_reason = "sl"
        
        if exit_price:
            self._close_position(candle.get("timestamp"), exit_price, exit_reason)
    
    def _update_position_equity(self, candle: Dict) -> None:
        """Update equity based on current position P&L"""
        if not self.active_position:
            return
        
        entry = self.active_position.entry_price
        quantity = self.active_position.quantity
        direction = self.active_position.direction
        
        current_price = candle.get("close")
        
        if direction == "long":
            pnl = (current_price - entry) * quantity
        else:  # short
            pnl = (entry - current_price) * quantity
        
        self.current_balance = self.initial_balance + pnl
    
    def _close_position(self, timestamp: int, exit_price: float, reason: str) -> None:
        """Close active position"""
        if not self.active_position:
            return
        
        entry = self.active_position.entry_price
        quantity = self.active_position.quantity
        direction = self.active_position.direction
        sl = self.active_position.stop_loss
        tp = self.active_position.take_profit
        
        # Calculate P&L
        if direction == "long":
            pnl = (exit_price - entry) * quantity
        else:
            pnl = (entry - exit_price) * quantity
        
        pnl_percent = (pnl / (entry * quantity)) * 100
        
        # Calculate RR achieved
        if direction == "long":
            risk = entry - sl
            reward = exit_price - entry
        else:
            risk = sl - entry
            reward = entry - exit_price
        
        rr_achieved = reward / risk if risk > 0 else 0
        
        # Update balance
        self.current_balance += pnl
        
        # Record trade
        self.active_position.exit_time = timestamp
        self.active_position.exit_price = exit_price
        self.active_position.exit_reason = reason
        self.active_position.pnl = pnl
        self.active_position.pnl_percent = pnl_percent
        self.active_position.rr_achieved = rr_achieved
        
        # Add to metrics
        self.metrics.trades.append(self.active_position)
        
        self.active_position = None
    
    def _calculate_metrics(self) -> None:
        """Calculate backtest performance metrics"""
        trades = self.metrics.trades
        
        if not trades:
            return
        
        self.metrics.total_trades = len(trades)
        self.metrics.final_balance = self.current_balance
        
        # Wins and losses
        winning = [t for t in trades if t.pnl > 0]
        losing = [t for t in trades if t.pnl < 0]
        
        self.metrics.winning_trades = len(winning)
        self.metrics.losing_trades = len(losing)
        self.metrics.win_rate = (len(winning) / len(trades) * 100) if trades else 0
        
        # Averages
        if winning:
            self.metrics.avg_win_percent = np.mean([t.pnl_percent for t in winning])
        if losing:
            self.metrics.avg_loss_percent = np.mean([t.pnl_percent for t in losing])
        
        # Profit factor
        gross_profit = sum([t.pnl for t in winning])
        gross_loss = abs(sum([t.pnl for t in losing]))
        self.metrics.profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
        
        # Expectancy
        avg_win = self.metrics.avg_win_percent / 100 * self.initial_balance if self.metrics.avg_win_percent else 0
        avg_loss = self.metrics.avg_loss_percent / 100 * self.initial_balance if self.metrics.avg_loss_percent else 0
        self.metrics.expectancy = (
            (self.metrics.win_rate / 100) * avg_win + 
            ((100 - self.metrics.win_rate) / 100) * avg_loss
        )
        
        # Total return
        self.metrics.total_pnl = self.current_balance - self.initial_balance
        
        # Drawdown
        peak = self.initial_balance
        max_dd = 0
        for equity in self.equity_curve:
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
        
        self.metrics.max_drawdown = max_dd
        self.metrics.max_drawdown_percent = (max_dd / self.initial_balance * 100) if self.initial_balance > 0 else 0
        
        # Best and worst
        if trades:
            self.metrics.best_trade = max(trades, key=lambda t: t.pnl)
            self.metrics.worst_trade = min(trades, key=lambda t: t.pnl)
            self.metrics.avg_rr_achieved = np.mean([t.rr_achieved for t in trades])
    
    def get_report(self) -> Dict:
        """Generate backtest report"""
        m = self.metrics
        
        return {
            "summary": {
                "total_trades": m.total_trades,
                "winning": m.winning_trades,
                "losing": m.losing_trades,
                "win_rate": f"{m.win_rate:.1f}%",
                "profit_factor": f"{m.profit_factor:.2f}",
                "expectancy": f"${m.expectancy:.2f}"
            },
            "returns": {
                "initial": f"${m.start_balance:.2f}",
                "final": f"${m.final_balance:.2f}",
                "pnl": f"${m.total_pnl:.2f}",
                "return_percent": f"{(m.total_pnl / m.start_balance * 100):.2f}%"
            },
            "risk": {
                "max_drawdown": f"${m.max_drawdown:.2f}",
                "max_drawdown_percent": f"{m.max_drawdown_percent:.2f}%"
            },
            "trades": {
                "best": {
                    "pnl": f"${m.best_trade.pnl:.2f}" if m.best_trade else "N/A",
                    "pnl_percent": f"{m.best_trade.pnl_percent:.2f}%" if m.best_trade else "N/A"
                },
                "worst": {
                    "pnl": f"${m.worst_trade.pnl:.2f}" if m.worst_trade else "N/A",
                    "pnl_percent": f"{m.worst_trade.pnl_percent:.2f}%" if m.worst_trade else "N/A"
                },
                "avg_rr": f"{m.avg_rr_achieved:.2f}x"
            }
        }
