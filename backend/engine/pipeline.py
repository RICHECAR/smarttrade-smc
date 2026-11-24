"""
V2 Engine Pipeline
Orchestrates the data flow: Market Data -> Strategy -> Risk -> Execution
"""

import asyncio
import time
import traceback
import os
from dotenv import load_dotenv
from typing import Dict, Optional

from .strategy.smc_strategy import SMCStrategy
from .strategy.aggregator import MarketDataAggregator
from .execution.bitget_futures import BitgetFuturesClient
from .execution.order_manager import OrderManager
from .risk.futures_risk import FuturesRiskManager
from .learning.trade_memory import TradeMemory
from .sync.trade_history_sync import TradeHistorySync

# Load environment variables
load_dotenv()

class TradingPipeline:
    """
    Main Engine Loop
    """
    def __init__(self):
        # Configuration
        self.symbol = "BTCUSDT"
        self.api_key = os.getenv("KRAKEN_API_KEY", "") or os.getenv("BITGET_API_KEY", "")
        self.secret_key = os.getenv("KRAKEN_API_SECRET", "") or os.getenv("BITGET_API_SECRET", "")
        self.passphrase = os.getenv("KRAKEN_PASSPHRASE", "") or os.getenv("BITGET_PASSPHRASE", "")
        
        if not all([self.api_key, self.secret_key, self.passphrase]):
            print("[PIPELINE] WARNING: API credentials missing in .env")
        
        # Modules
        self.strategy = SMCStrategy()
        self.aggregator = MarketDataAggregator(interval_sec=300, on_candle_closed=self.on_candle_closed) # 5m candles
        
        self.client = BitgetFuturesClient(self.api_key, self.secret_key, self.passphrase)
        self.risk = FuturesRiskManager()
        self.sync = TradeHistorySync()
        self.memory = TradeMemory()
        
        self.order_manager = OrderManager(self.client, self.risk, self.sync, self.memory)
        
        self.is_running = False
        
    async def start(self):
        """Start the pipeline"""
        print("[PIPELINE] Starting V2 Engine...")
        self.is_running = True
        
        await self.client.init_client()
        
        # Start loops
        try:
            # 1. Market Data Loop (Simulation of Websocket for now)
            # In simulation/backtest mode, we might just loop historical data.
            # In live mode (Phase 5), we connect WS.
            # For now, we'll poll price or mock it to verify connectivity.
            await asyncio.gather(
                self.market_data_loop(),
                self.sync_loop()
            )
        except asyncio.CancelledError:
            print("[PIPELINE] Engine stopping...")
        finally:
            await self.stop()
            
    async def stop(self):
        self.is_running = False
        await self.client.close_client()
        await self.sync.close()
        print("[PIPELINE] Stopped.")
        
    async def market_data_loop(self):
        """Simulated market data loop for verification"""
        print("[PIPELINE] Market Data Loop Active")
        while self.is_running:
            try:
                # Polling ticker for now (replace with WS later)
                # This is just to prove connectivity and flow
                ticker = await self.client.request("GET", "mix/market/ticker", {"symbol": self.symbol, "productType": "umcbl"})
                
                if ticker and ticker.get("data"):
                    data = ticker["data"][0]
                    price = float(data["lastPr"])
                    # Mock volume for now
                    volume = float(data.get("volume24h", 1000)) / 24 / 60 
                    
                    # Feed aggregator
                    self.aggregator.process_trade(price, volume)
                    
                    # Also feed raw price to strategy for realtime checks?
                    # Strategy mainly works on candles, but entry builder checks current_price.
                    
                await asyncio.sleep(2) # Poll every 2s
                
            except Exception as e:
                print(f"[PIPELINE] Data Loop Error: {e}")
                await asyncio.sleep(5)
                
    def on_candle_closed(self, candle: Dict):
        """Callback when aggregator completes a candle in the market_data_loop"""
        print(f"[PIPELINE] Candle Closed: {candle['timestamp']} O:{candle['open']} C:{candle['close']}")
        
        # 1. Run Strategy
        signal = self.strategy.process_candle(candle)
        
        if signal:
            print(f"[PIPELINE] \U0001F680 SIGNAL DETECTED: {signal}")
            
            # 2. Execute Signal (async task)
            asyncio.create_task(self.execute_signal_safe(signal))
            
    async def execute_signal_safe(self, signal: Dict):
        """Execute signal with error handling"""
        try:
            # Get current balance for sizing
            # For backtest/sim, use mock balance. Live = fetch.
            # Assuming mock balance for now in Phase 2/3
            balance = 10000.0 
            
            await self.order_manager.execute_signal(signal, balance)
        except Exception as e:
            print(f"[PIPELINE] Execution Error: {e}")
            traceback.print_exc()
            
    async def sync_loop(self):
        """Periodic sync of state to Node.js"""
        while self.is_running:
            try:
                # Sync logic here (e.g. heartbeat)
                await asyncio.sleep(10)
            except Exception:
                pass

if __name__ == "__main__":
    pipeline = TradingPipeline()
    try:
        asyncio.run(pipeline.start())
    except KeyboardInterrupt:
        asyncio.run(pipeline.stop())
