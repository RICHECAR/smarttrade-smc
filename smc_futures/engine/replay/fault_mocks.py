import asyncio
import random
import time
import logging
from typing import Dict, Any, List
from smc_futures.engine.execution.base_adapter import BaseExchangeAdapter

logger = logging.getLogger("replay_mocks")

class FaultInjectingMockAdapter(BaseExchangeAdapter):
    """
    Mock adapter that simulates institutional exchange execution behavior,
    including network latency, random failures, timeouts, and Maker-Taker asymmetric risks.
    """
    def __init__(self, name: str, base_latency_ms: int = 50, latency_jitter_ms: int = 20, 
                 failure_rate_pct: float = 0.0, timeout_rate_pct: float = 0.0,
                 partial_fill_rate_pct: float = 0.0, late_fill_rate_pct: float = 0.0):
        self.name = name
        self.base_latency_ms = base_latency_ms
        self.latency_jitter_ms = latency_jitter_ms
        self.failure_rate_pct = failure_rate_pct
        self.timeout_rate_pct = timeout_rate_pct
        self.partial_fill_rate_pct = partial_fill_rate_pct
        self.late_fill_rate_pct = late_fill_rate_pct
        
        self.open_positions: List[Dict[str, Any]] = []
        
        # Track orders for get_order_status
        self.mocked_orders: Dict[str, Dict[str, Any]] = {}

    async def _simulate_latency(self):
        latency = self.base_latency_ms + random.uniform(-self.latency_jitter_ms, self.latency_jitter_ms)
        latency = max(0, latency)
        await asyncio.sleep(latency / 1000.0)

    async def execute_order(self, symbol: str, side: str, order_type: str, qty: float, price: float = None) -> Dict[str, Any]:
        """Simulates placing an order with stochastic faults."""
        if random.random() < self.timeout_rate_pct:
            logger.debug(f"[Mock {self.name}] Simulating TIMEOUT (hanging for 30s)...")
            await asyncio.sleep(30.0)
            return {"success": False, "error_msg": "simulated_timeout"}
            
        await self._simulate_latency()
        
        if random.random() < self.failure_rate_pct:
            logger.debug(f"[Mock {self.name}] Simulating REJECT (e.g. insufficient margin)...")
            return {"success": False, "error_msg": "simulated_rejection"}
            
        order_id = f"{self.name}_{int(time.time()*1000)}_{random.randint(1000,9999)}"
        self.open_positions.append({
            "symbol": symbol,
            "side": side,
            "size": qty,
            "order_id": order_id
        })
        self.mocked_orders[order_id] = {"status": "filled", "filled_qty": qty, "type": order_type}
        return {"success": True, "order_id": order_id, "error_msg": ""}

    async def place_post_only_limit(self, symbol: str, side: str, qty: float, price: float) -> Dict[str, Any]:
        """Phase 4: Place Maker Limit"""
        if random.random() < self.failure_rate_pct:
            return {"success": False, "error_msg": "simulated_rejection"}
            
        await self._simulate_latency()
        order_id = f"{self.name}_LIMIT_{int(time.time()*1000)}_{random.randint(1000,9999)}"
        
        # Roll for initial Status (Simulate fill likelihood)
        status = "new" # Wait for get_order_status to poll it
        self.mocked_orders[order_id] = {"status": status, "filled_qty": 0.0, "qty": qty, "symbol": symbol, "side": side}
        return {"success": True, "order_id": order_id, "error_msg": ""}

    async def get_order_status(self, order_id: str, symbol: str) -> Dict[str, Any]:
        await asyncio.sleep(0.01)
        if order_id not in self.mocked_orders:
            return {"status": "unknown"}
            
        order = self.mocked_orders[order_id]
        if order["status"] == "new":
            # Change state randomly upon polling to test OMS
            roll = random.random()
            if roll < self.partial_fill_rate_pct:
                order["status"] = "partial_fill"
                order["filled_qty"] = order["qty"] / 2.0
            elif roll < 0.6: # Give it a 60% chance to fill completely eventually when polled
                order["status"] = "filled"
                order["filled_qty"] = order["qty"]
                
        elif order["status"] == "canceled":
            # Late fill condition!
            if random.random() < self.late_fill_rate_pct:
                logger.debug(f"[Mock {self.name}] LATE FILL on canceled order!")
                order["status"] = "filled"
                order["filled_qty"] = order["qty"]
                self.open_positions.append({
                    "symbol": order["symbol"],
                    "side": order["side"],
                    "size": order["qty"],
                    "order_id": order_id
                })
                
        return order

    async def cancel_order(self, order_id: str, symbol: str = None) -> bool:
        await self._simulate_latency()
        if order_id in self.mocked_orders:
            if self.mocked_orders[order_id]["status"] == "new":
                self.mocked_orders[order_id]["status"] = "canceled"
        return True

    async def get_recent_fills(self, intent_id: str = None, symbol: str = None) -> List[Dict[str, Any]]:
        return []

    async def close_position_market(self, symbol: str, side: str, qty: float) -> Dict[str, Any]:
        await self._simulate_latency()
        return {"success": True, "order_id": "CLOSE_MKT", "error_msg": ""}

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        await asyncio.sleep(0.02)
        return self.open_positions

    async def close_all_positions(self, symbol: str = None) -> bool:
        await asyncio.sleep(0.05)
        if symbol:
            self.open_positions = [p for p in self.open_positions if p.get("symbol") != symbol]
        else:
            self.open_positions = []
        return True

    async def cancel_all_orders(self, symbol: str = None) -> bool:
        await asyncio.sleep(0.01)
        return True

    async def healthcheck(self) -> bool:
        return True
