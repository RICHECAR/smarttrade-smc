"""
Trade History Sync
Syncs completed trades and active positions with the Node.js backend/Database.
"""

import httpx
import asyncio
from typing import Dict, List

class TradeHistorySync:
    """
    Syncs trade data to Node.js backend
    """
    def __init__(self, node_url: str = "http://localhost:5000"):
        self.node_url = node_url
        self.client = httpx.AsyncClient()
        
    async def sync_trade(self, trade_data: Dict):
        """Send trade update to Node backend"""
        try:
            # Endpoint to be implemented in Node backend
            await self.client.post(f"{self.node_url}/api/sync/trade", json=trade_data)
        except Exception as e:
            print(f"[SYNC] Failed to sync trade: {e}")
            
    async def sync_position(self, position_data: Dict):
        """Send position update to Node backend"""
        try:
            await self.client.post(f"{self.node_url}/api/sync/position", json=position_data)
        except Exception as e:
            print(f"[SYNC] Failed to sync position: {e}")

    async def close(self):
        await self.client.aclose()
