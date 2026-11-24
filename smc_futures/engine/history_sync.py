"""
History Sync Module
Keeps local OHLCV data and trade history updated
"""

import asyncio
import logging
import pandas as pd
from datetime import datetime
from smc_futures.data_downloader import fetch_ohlcv, save_csv
import os

logger = logging.getLogger(__name__)

class HistorySync:
    def __init__(self, symbol: str, exchange: str = "bitget"):
        self.symbol = symbol
        self.exchange = exchange
        self.data_dir = os.path.join("smc_futures", "data")
        os.makedirs(self.data_dir, exist_ok=True)

    async def sync_ohlcv(self, timeframe: str = "1h", limit: int = 500):
        """Sync historical candles to local CSV"""
        try:
            logger.info(f"Syncing OHLCV for {self.symbol} on {self.exchange}...")
            df = fetch_ohlcv(self.exchange, self.symbol, timeframe=timeframe, limit=limit)
            
            filename = f"{self.symbol.replace('/', '')}_{timeframe}.csv"
            filepath = os.path.join(self.data_dir, filename)
            save_csv(df, filepath)
            
            logger.info(f"Successfully synced {len(df)} candles to {filepath}")
            return True
        except Exception as e:
            logger.error(f"Failed to sync OHLCV: {e}")
            return False

    async def sync_trades(self, executor):
        """Sync trade history from exchange"""
        try:
            # This would typically save to a database
            # For now we just fetch them
            history = await executor.get_trade_history(self.symbol)
            return history
        except Exception as e:
            logger.error(f"Failed to sync trades: {e}")
            return None
