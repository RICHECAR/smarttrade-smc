"""
Lightweight Pydantic order validation schemas separated from FastAPI app modules.
"""

from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class DryRunOrderRequest(BaseModel):
    """Request body for dry-run order endpoint."""

    symbol: str = Field(..., min_length=3, max_length=30, pattern=r"^[A-Za-z0-9/:\-_]+$")
    side: str = Field(..., pattern=r"^(buy|sell|long|short)$")
    pos_side: str = Field("long", pattern=r"^(long|short)$")
    entry_price: float = Field(..., gt=0, lt=1e9)
    stop_loss: float = Field(..., gt=0, lt=1e9)
    take_profit: float = Field(..., gt=0, lt=1e9)
    risk_pct: float = Field(..., gt=0, le=100)
    leverage: int = Field(10, ge=1, le=125)
    product_type: str = "USDT-FUTURES"
    model_config = ConfigDict(extra="allow")


class OrderRequest(BaseModel):
    """Request body for order placement."""

    symbol: str = Field(..., min_length=3, max_length=30, pattern=r"^[A-Za-z0-9/:\-_]+$")
    side: str = Field(..., pattern=r"^(buy|sell|long|short)$")
    orderType: str = Field("market", pattern=r"^(market|limit)$")
    quantity: Optional[float] = Field(None, gt=0, lt=1e9)
    size: Optional[float] = Field(None, gt=0, lt=1e9)
    price: Optional[float] = Field(None, ge=0, lt=1e9)
    stopLoss: Optional[float] = Field(None, gt=0, lt=1e9)
    stop_loss: Optional[float] = Field(None, gt=0, lt=1e9)
    takeProfit: Optional[float] = Field(None, gt=0, lt=1e9)
    take_profit: Optional[float] = Field(None, gt=0, lt=1e9)
    posSide: Optional[str] = None
    tradeSide: Optional[str] = "open"
    productType: Optional[str] = "USDT-FUTURES"
    marginCoin: Optional[str] = "USDT"
    marginMode: Optional[str] = "isolated"
    order_type: Optional[str] = None
    model_config = ConfigDict(extra="allow")


class ForceOrderRequest(BaseModel):
    """Request body for test force-order endpoint."""

    symbol: str = Field(..., min_length=3, max_length=30, pattern=r"^[A-Za-z0-9/:\-_]+$")
    side: str = Field(..., pattern=r"^(buy|sell)$")
    order_type: str = Field("market", pattern=r"^(market|limit)$")
    size: float = Field(..., gt=0, lt=1e9)
    price: Optional[float] = Field(None, gt=0, lt=1e9)
    mode: str = Field("shadow", pattern=r"^(shadow|paper|live)$")
    model_config = ConfigDict(extra="forbid")
