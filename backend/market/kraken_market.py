from typing import List, Optional
import logging
from backend.market_data.kraken_market_client import KrakenMarketClient
from smc_futures.engine.multi_broker_engine import Candle
from smc_futures.smc_config import SMC_CONFIG

logger = logging.getLogger("smarttrade.kraken_market")

class KrakenMarket:
    """
    Adapter bridging KrakenMarketClient to the common market interface.
    """
    def __init__(self, client: Optional[KrakenMarketClient] = None):
        self.client = client or KrakenMarketClient(
            stale_threshold_ms=SMC_CONFIG.get("STALE_THRESHOLD_MS", 250)
        )
        
    async def get_candles(self, symbol: str, tf: str, limit: int, end_time: Optional[int] = None) -> List[Candle]:
        try:
            if end_time:
                raw_candles = await self.client.get_candles_with_end_time(
                    symbol=symbol,
                    tf=tf,
                    limit=limit,
                    end_time=end_time,
                )
            else:
                raw_candles = await self.client.get_candles(
                    symbol=symbol,
                    tf=tf,
                    limit=limit,
                )

            candles = [
                Candle(
                    timestamp=int(c.get("ts", c.get("timestamp", 0))),
                    open=float(c.get("open", c.get("o", 0))),
                    high=float(c.get("high", c.get("h", 0))),
                    low=float(c.get("low", c.get("l", 0))),
                    close=float(c.get("close", c.get("c", 0))),
                    volume=float(c.get("volume", c.get("vol", c.get("v", 0)))),
                )
                for c in raw_candles
            ]
            return candles
        except Exception as e:
            logger.error(f"[KRAKEN_MARKET] Failed to fetch candles for {symbol}: {e}")
            return []

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        return await self.client.get_ticker(symbol)
        
    async def get_candles_with_end_time(self, *args, **kwargs):
        raise NotImplementedError("Use get_candles with end_time instead")
