import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta
from backend.services.trade_reconciler import PositionSyncService
from backend.models.live_trade import LiveTrade

class TestReconciliationHeuristic(unittest.IsolatedAsyncioTestCase):
    async def test_bitget_heuristic_matching(self):
        # 1. Setup Mock Executor
        executor = MagicMock()
        executor.client = MagicMock()
        
        # Mock Bitget fills response
        # We simulate a "sell" fill for a "long" trade that was closed via TP plan order (new OID)
        mock_fills = {
            "code": "00000",
            "data": {
                "fillList": [
                    {
                        "orderId": "plan_order_123", # New OID
                        "symbol": "BTCUSDT",
                        "side": "sell",
                        "baseVolume": "0.1",
                        "price": "50500",
                        "fee": "5.0",
                        "profit": "50.0",
                        "cTime": str(int((datetime.now(timezone.utc)).timestamp() * 1000))
                    }
                ]
            }
        }
        executor.client._request.return_value = mock_fills
        
        # 2. Setup Mock Trade
        trade = LiveTrade(
            id="test_trade_id",
            order_id="original_entry_oid",
            symbol="BTCUSDT",
            side="long",
            qty=0.1,
            entry=50000,
            ts_open=datetime.now(timezone.utc) - timedelta(minutes=10)
        )
        
        # 3. Initialize Reconciler
        reconciler = PositionSyncService(bitget_executor=executor)
        
        # 4. Test Heuristic Fetch
        start_tm = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)
        end_tm = int(datetime.now(timezone.utc).timestamp() * 1000)
        
        fills = await reconciler._fetch_bitget_fills(trade, start_tm, end_tm)
        
        # 5. Assertions
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["order_id"], "plan_order_123")
        self.assertEqual(fills[0]["pnl"], 50.0)
        print("\n✅ Heuristic matching successful: found fill 'plan_order_123' for trade 'original_entry_oid'")

if __name__ == "__main__":
    unittest.main()
