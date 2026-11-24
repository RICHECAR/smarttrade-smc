"""
Base interface for all trading brokers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

@dataclass
class BrokerHealth:
    status: str  # "OK" or "ERROR"
    details: str

@dataclass
class MinOrder:
    min_qty: float
    min_notional: float
    step: float
    price_precision: int
    qty_precision: int

@dataclass
class OrderRequest:
    symbol: str
    side: str  # "long" or "short"
    qty: float
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    leverage: int = 10
    reduce_only: bool = False

@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    error: Optional[str] = None
    fill_price: Optional[float] = None

class BaseBroker(ABC):
    """
    Abstract base class defining the required interface for a broker module.
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the broker (e.g. 'kraken')."""
        pass
        
    @abstractmethod
    async def healthcheck(self) -> BrokerHealth:
        """Verify broker API connection, credentials, and right API (e.g. Futures vs Spot)."""
        pass
        
    @abstractmethod
    async def get_min_order(self, symbol: str) -> MinOrder:
        """Get contract specifications for a symbol."""
        pass
        
    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult:
        """Place an order with optional attached stop_loss and take_profit."""
        pass
        
    @abstractmethod
    async def close_position(self, symbol: str) -> OrderResult:
        """Close an existing position for a given symbol."""
        pass
        
    @abstractmethod
    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get all open positions for this broker."""
        pass
