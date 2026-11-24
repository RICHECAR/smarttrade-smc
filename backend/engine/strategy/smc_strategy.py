"""
SMC Strategy V2
Main orchestrator for Smart Money Concepts strategy.
Combines all SMC sub-modules into a single cohesive strategy.
"""

from typing import Dict, List, Optional
from dataclasses import dataclass
import time

from .smc.structure import StructureEngine, Candle
from .smc.liquidity import LiquidityEngine
from .smc.order_blocks import OrderBlockEngine
from .smc.fvg import FVGEngine
from .smc.mtf_bias import MTFBiasEngine
from .smc.entry_builder import EntryBuilder, SMCEntry

class SMCStrategy:
    """
    Core SMC Strategy Logic
    """
    
    def __init__(self):
        # Initialize sub-engines
        self.structure = StructureEngine()
        self.liquidity = LiquidityEngine()
        self.order_blocks = OrderBlockEngine()
        self.fvg = FVGEngine()
        self.mtf_bias = MTFBiasEngine()
        self.entry_builder = EntryBuilder()
        
        self.last_update_time = 0
        self.current_signal: Optional[SMCEntry] = None
        
    def process_candle(self, candle_data: Dict) -> Optional[Dict]:
        """
        Process incoming candle data (OHLCV)
        Returns signal dict if entry found, else None
        """
        try:
            # Create Candle object
            candle = Candle(
                timestamp=candle_data.get('timestamp', int(time.time() * 1000)),
                open=float(candle_data['open']),
                high=float(candle_data['high']),
                low=float(candle_data['low']),
                close=float(candle_data['close']),
                volume=float(candle_data.get('volume', 0))
            )
            
            self.last_update_time = candle.timestamp
            
            # 1. Update Core Engines
            self.structure.add_candle(candle)
            self.liquidity.add_candle(candle)
            self.order_blocks.add_candle(candle)
            self.fvg.add_candle(candle)
            
            # 2. Get States
            structure_state = self.structure.get_current_state()
            liquidity_state = self.liquidity.get_current_state()
            ob_state = self.order_blocks.get_current_state()
            fvg_state = self.fvg.get_current_state()
            
            # 3. Update MTF Bias (Simulated MTF for now using same stream, 
            # ideally should feed 4H/1H candles separately)
            # For V2 MVP, we might assume 5m stream and approximate higher TFs 
            # or rely on external aggregator to feed different TFs.
            # Here we just pass the structure state to update 'simulated' HTF/LTF states
            self.mtf_bias.update_htf_data(structure_state) # Using same data for now (needs improvement)
            self.mtf_bias.update_ltf_data(structure_state)
            self.mtf_bias.update_ob_data("4H", ob_state)
            self.mtf_bias.update_fvg_data("4H", fvg_state)
            
            mtf_state = self.mtf_bias.get_current_state()
            
            # 4. Check for Entry
            signal = self.entry_builder.build_signal(
                structure_state=structure_state,
                liquidity_state=liquidity_state,
                ob_state=ob_state,
                fvg_state=fvg_state,
                mtf_state=mtf_state,
                current_price=candle.close,
                timestamp=candle.timestamp
            )
            
            if signal:
                self.current_signal = signal
                return self.entry_builder.get_last_signal()
                
            return None
            
        except Exception as e:
            print(f"[SMCStrategy] Error processing candle: {e}")
            return None

    def get_strategy_state(self) -> Dict:
        """Get comprehensive strategy state for UI/Debugging"""
        return {
            "timestamp": self.last_update_time,
            "structure": self.structure.get_current_state(),
            "liquidity": self.liquidity.get_current_state(),
            "order_blocks": self.order_blocks.get_current_state(),
            "fvg": self.fvg.get_current_state(),
            "mtf_bias": self.mtf_bias.get_current_state(),
            "last_signal": self.entry_builder.get_last_signal()
        }
