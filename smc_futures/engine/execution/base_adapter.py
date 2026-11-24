import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class BaseExchangeAdapter(ABC):
    """
    Abstract interface defining the minimum institutional requirements
    for any multi-exchange execution module.
    """
    
    @abstractmethod
    async def cancel_all_orders(self, symbol: str = None) -> bool:
        """Cancel all pending/limit orders for a given symbol, or globally if symbol=None."""
        pass

    @abstractmethod
    async def close_all_positions(self, symbol: str = None) -> bool:
        """Issue market/IOC orders to flatten open lines on the exchange."""
        pass

    @abstractmethod
    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """
        Return normalized dictionary of positions.
        Expected format per position:
        {
          "symbol": str,
          "side": "long" | "short",
          "size": float,     # absolute quantity
          "unrealized_pnl": float
        }
        """
        pass
        
    @abstractmethod
    async def get_order_status(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """
        Fetch the status of a specific order.
        Returns unified status: "new" | "filled" | "canceled" | "rejected" | "partial_fill"
        """
        pass
        
    @abstractmethod
    async def execute_order(self, symbol: str, side: str, order_type: str, qty: float, price: float = None) -> Dict[str, Any]:
        """
        Submit a new order.
        Returns:
        {
           "success": bool,
           "order_id": str,
           "error_msg": str
        }
        """
        pass

    @abstractmethod
    async def place_post_only_limit(self, symbol: str, side: str, qty: float, price: float) -> Dict[str, Any]:
        """
        Submit a Maker (Post-Only) limit order.
        Returns: {"success": bool, "order_id": str, "error_msg": str}
        """
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str = None) -> bool:
        """Cancel a specific order by ID."""
        pass

    @abstractmethod
    async def get_recent_fills(self, intent_id: str = None, symbol: str = None) -> List[Dict[str, Any]]:
        """Fetch recent executions to determine partial or full fill status."""
        pass

    @abstractmethod
    async def close_position_market(self, symbol: str, side: str, qty: float) -> Dict[str, Any]:
        """Submit a Taker (Market) order to close out a specific leg (e.g. Orphan defense)."""
        pass

    @abstractmethod
    async def healthcheck(self) -> bool:
        """Ping exchange / verify API Key validity."""
        pass


class KrakenAdapter(BaseExchangeAdapter):
    """Kraken adapter stub following the standard."""
    
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        # aiohttp session ideally passed here 

    async def cancel_all_orders(self, symbol: str = None) -> bool:
        logger.info(f"KRAKEN: cancelling orders (symbol={symbol})")
        # Logic using Kraken Futures derivatives/api/v3/cancelallorders
        return True

    async def close_all_positions(self, symbol: str = None) -> bool:
        logger.info(f"KRAKEN: flattening positions (symbol={symbol})")
        # Fetch /openpositions -> issue opposite IOC
        return True

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        return []
        
    async def get_order_status(self, order_id: str, symbol: str) -> Dict[str, Any]:
        return {"status": "unknown"}
        
    async def execute_order(self, symbol: str, side: str, order_type: str, qty: float, price: float = None) -> Dict[str, Any]:
        return {"success": False, "error_msg": "Not implemented"}

    async def place_post_only_limit(self, symbol: str, side: str, qty: float, price: float) -> Dict[str, Any]:
        return {"success": False, "error_msg": "Not implemented"}

    async def cancel_order(self, order_id: str, symbol: str = None) -> bool:
        return True

    async def get_recent_fills(self, intent_id: str = None, symbol: str = None) -> List[Dict[str, Any]]:
        return []

    async def close_position_market(self, symbol: str, side: str, qty: float) -> Dict[str, Any]:
        return {"success": False, "error_msg": "Not implemented"}

    async def healthcheck(self) -> bool:
        return True


class BitgetAdapter(BaseExchangeAdapter):
    """Bitget V2 Mix adapter stub following the standard."""

    def __init__(self, api_key: str, secret_key: str, passphrase: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase

    async def cancel_all_orders(self, symbol: str = None) -> bool:
        logger.info(f"BITGET: cancelling orders (symbol={symbol})")
        # Logic using /api/v2/mix/order/cancel-all-orders
        return True

    async def close_all_positions(self, symbol: str = None) -> bool:
        logger.info(f"BITGET: flattening positions (symbol={symbol})")
        # native Bitget close-all-positions
        return True

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        return []
        
    async def get_order_status(self, order_id: str, symbol: str) -> Dict[str, Any]:
        return {"status": "unknown"}
        
    async def execute_order(self, symbol: str, side: str, order_type: str, qty: float, price: float = None) -> Dict[str, Any]:
        return {"success": False, "error_msg": "Not implemented"}

    async def place_post_only_limit(self, symbol: str, side: str, qty: float, price: float) -> Dict[str, Any]:
        return {"success": False, "error_msg": "Not implemented"}

    async def cancel_order(self, order_id: str, symbol: str = None) -> bool:
        return True

    async def get_recent_fills(self, intent_id: str = None, symbol: str = None) -> List[Dict[str, Any]]:
        return []

    async def close_position_market(self, symbol: str, side: str, qty: float) -> Dict[str, Any]:
        return {"success": False, "error_msg": "Not implemented"}

    async def healthcheck(self) -> bool:
        return True
