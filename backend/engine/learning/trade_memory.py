"""
Trade Memory Module
Tracks trade performance and outcomes to improve future decision making.
"""

import json
import time
from typing import List, Dict, Optional
from dataclasses import dataclass

@dataclass
class TradeOutcome:
    symbol: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    pnl: float
    pnl_percent: float
    strategy_signal: Dict
    market_context: Dict # V2: Store snapshot of indicators/structure at entry

class TradeMemory:
    """
    Stores trade history and analyzes performance.
    """
    def __init__(self, memory_file: str = "trade_memory.json"):
        self.memory_file = memory_file
        self.outcomes: List[TradeOutcome] = []
        self._load_memory()
        
    def record_outcome(self, outcome: TradeOutcome):
        """Record a completed trade outcome"""
        self.outcomes.append(outcome)
        self._save_memory()
        
    def get_performance_stats(self) -> Dict:
        """Calculate win rate, avg pnl, etc"""
        if not self.outcomes:
            return {"win_rate": 0, "total_trades": 0, "pnl": 0}
            
        wins = [t for t in self.outcomes if t.pnl > 0]
        losses = [t for t in self.outcomes if t.pnl <= 0]
        
        return {
            "total_trades": len(self.outcomes),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(self.outcomes) if self.outcomes else 0,
            "total_pnl": sum(t.pnl for t in self.outcomes),
            "avg_win": sum(t.pnl for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t.pnl for t in losses) / len(losses) if losses else 0
        }
        
    def _save_memory(self):
        try:
            data = [
                {
                    "symbol": t.symbol,
                    "entry_time": t.entry_time,
                    "exit_time": t.exit_time,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "pnl": t.pnl,
                    "pnl_percent": t.pnl_percent,
                    "strategy": t.strategy_signal, # Simplified
                    "context": t.market_context # Simplified
                }
                for t in self.outcomes
            ]
            with open(self.memory_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[MEMORY] Failed to save memory: {e}")

    def _load_memory(self):
        try:
            with open(self.memory_file, 'r') as f:
                data = json.load(f)
                self.outcomes = [
                    TradeOutcome(
                        symbol=d['symbol'],
                        entry_time=d['entry_time'],
                        exit_time=d['exit_time'],
                        entry_price=d['entry_price'],
                        exit_price=d['exit_price'],
                        pnl=d['pnl'],
                        pnl_percent=d['pnl_percent'],
                        strategy_signal=d.get('strategy', {}),
                        market_context=d.get('context', {})
                    )
                    for d in data
                ]
        except FileNotFoundError:
            self.outcomes = []
        except Exception as e:
            print(f"[MEMORY] Failed to load memory: {e}")
