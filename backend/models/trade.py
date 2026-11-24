"""
Trade Model - SQLAlchemy ORM + Pydantic schemas for trade data

Used by:
- API responses (GET /api/trades)
- WebSocket broadcasts (trade_opened, trade_closed)
- Database storage/retrieval

Author: SMC Trading Bot Framework
"""

from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime, timezone
from enum import Enum
import uuid

from sqlalchemy import Column, String, Float, Integer, DateTime, Enum as SQLEnum, Boolean
from sqlalchemy.orm import Session

from backend.db.base import Base


class Side(str, Enum):
    """Trade direction."""
    LONG = "long"
    SHORT = "short"


class TradeResult(str, Enum):
    """Trade outcome."""
    WIN = "win"
    LOSS = "loss"
    BE = "be"  # Break-even


class TradeStatus(str, Enum):
    """Trade lifecycle status."""
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


# ============================================================
# SQLAlchemy ORM Model
# ============================================================

class Trade(Base):
    """
    SQLAlchemy ORM model for trades.
    Maps to the 'trades' table in the database.
    """
    __tablename__ = "trades"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4())[:8])
    ts = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)  # "long" or "short"
    entry = Column(Float, nullable=False)
    stop = Column(Float, nullable=False)
    tp = Column(Float, nullable=False)
    rr = Column(Float, nullable=False, default=0.0)
    pnl = Column(Float, default=0.0)
    session = Column(String(50), nullable=True, index=True)
    preset = Column(String(50), nullable=True, index=True)
    result = Column(String(10), nullable=True)  # "win", "loss", "be"
    status = Column(String(20), default="open", index=True)
    qty = Column(Float, nullable=True)
    leverage = Column(Integer, nullable=True)
    notional = Column(Float, nullable=True)
    risk_amount = Column(Float, nullable=True)
    stop_distance = Column(Float, nullable=True)
    close_ts = Column(DateTime, nullable=True)
    close_price = Column(Float, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    # Multi-broker support
    broker = Column(String(20), nullable=True, default="kraken", index=True)
    broker_order_id = Column(String(64), nullable=True, index=True)
    signal_id = Column(String(64), nullable=True, index=True)  # Links multi-broker trades
    
    archived = Column(Boolean, default=False, index=True)
    archive_reason = Column(String(50), nullable=True)
    
    def to_dict(self) -> dict:
        """Convert ORM instance to dictionary."""
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "symbol": self.symbol,
            "side": self.side,
            "entry": self.entry,
            "stop": self.stop,
            "tp": self.tp,
            "rr": self.rr,
            "pnl": self.pnl or 0.0,
            "session": self.session,
            "preset": self.preset,
            "result": self.result,
            "status": self.status or "open",
            "qty": self.qty,
            "leverage": self.leverage,
            "notional": self.notional,
            "risk_amount": self.risk_amount,
            "stop_distance": self.stop_distance,
            "close_ts": self.close_ts.isoformat() if self.close_ts else None,
            "close_price": self.close_price,
            "broker": self.broker or "kraken",
            "broker_order_id": self.broker_order_id,
            "signal_id": self.signal_id,
            "archived": self.archived,
            "archive_reason": self.archive_reason,
        }
    
    def to_pydantic(self) -> "TradeOut":
        """Convert to Pydantic TradeOut model."""
        return TradeOut(
            id=self.id,
            ts=self.ts,
            symbol=self.symbol,
            side=Side(self.side),
            entry=self.entry,
            stop=self.stop,
            tp=self.tp,
            rr=self.rr,
            pnl=self.pnl or 0.0,
            session=self.session,
            preset=self.preset,
            result=TradeResult(self.result) if self.result else None,
            status=TradeStatus(self.status) if self.status else TradeStatus.OPEN,
            qty=self.qty,
            leverage=self.leverage,
            notional=self.notional,
            risk_amount=self.risk_amount,
            stop_distance=self.stop_distance,
            close_ts=self.close_ts,
            close_price=self.close_price,
            broker=self.broker or "bitget",
            broker_order_id=self.broker_order_id,
            signal_id=self.signal_id,
            archived=self.archived if self.archived is not None else False,
            archive_reason=self.archive_reason,
        )


# ============================================================
# Pydantic Schemas
# ============================================================

class TradeIn(BaseModel):
    """Input model for creating a trade."""
    symbol: str = Field(..., description="Trading pair (e.g., BTCUSDT)")
    side: Side = Field(..., description="Trade direction")
    entry: float = Field(..., gt=0, description="Entry price")
    stop: float = Field(..., gt=0, description="Stop loss price")
    tp: float = Field(..., gt=0, description="Take profit price")
    qty: float = Field(..., gt=0, description="Position quantity")
    leverage: int = Field(default=10, ge=1, le=125, description="Leverage used")
    session: Optional[str] = Field(None, description="Trading session (London/NY)")
    preset: Optional[str] = Field(None, description="Preset name used")
    notional: Optional[float] = Field(None, description="Notional value in USDT")
    risk_amount: Optional[float] = Field(None, description="Risk amount in account currency")
    stop_distance: Optional[float] = Field(None, description="Stop distance in price units")
    broker: Optional[str] = Field(default="bitget", description="Broker name (bitget/kraken)")
    signal_id: Optional[str] = Field(None, description="Signal ID for multi-broker linking")


class TradeOut(BaseModel):
    """
    Output model for trade data.
    Used in API responses and WebSocket broadcasts.
    """
    id: str = Field(..., description="Unique trade ID")
    ts: datetime = Field(..., description="Trade timestamp (open time)")
    symbol: str = Field(..., description="Trading pair")
    side: Side = Field(..., description="Trade direction")
    entry: float = Field(..., description="Entry price")
    stop: float = Field(..., description="Stop loss price")
    tp: float = Field(..., description="Take profit price")
    rr: float = Field(..., description="Risk:Reward ratio")
    pnl: Optional[float] = Field(default=None, description="Realized PnL in USDT")
    session: Optional[str] = Field(None, description="Trading session")
    preset: Optional[str] = Field(None, description="Preset name")
    result: Optional[TradeResult] = Field(None, description="Trade result (win/loss/be)")
    status: TradeStatus = Field(default=TradeStatus.OPEN, description="Trade status")
    
    # Extended fields for detailed tracking
    qty: Optional[float] = Field(None, description="Position quantity")
    leverage: Optional[int] = Field(None, description="Leverage used")
    notional: Optional[float] = Field(None, description="Notional value")
    risk_amount: Optional[float] = Field(None, description="Risk amount")
    stop_distance: Optional[float] = Field(None, description="Stop distance")
    close_ts: Optional[datetime] = Field(None, description="Close timestamp")
    close_price: Optional[float] = Field(None, description="Actual close price")
    
    # Multi-broker fields
    broker: Optional[str] = Field(default="bitget", description="Broker name")
    broker_order_id: Optional[str] = Field(None, description="Broker-specific order ID")
    signal_id: Optional[str] = Field(None, description="Signal ID linking multi-broker trades")
    
    # Archive fields
    archived: bool = Field(default=False, description="Is trade archived")
    archive_reason: Optional[str] = Field(None, description="Reason for archiving")
    
    class Config:
        use_enum_values = True
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


class TradeUpdate(BaseModel):
    """Model for updating an existing trade (close)."""
    pnl: Optional[float] = Field(default=None, description="Realized PnL")
    result: Optional[TradeResult] = Field(None, description="Trade result")
    close_price: Optional[float] = Field(None, description="Actual close price")


class TradeSummary(BaseModel):
    """Summary statistics for a period."""
    pnl: float = Field(default=0.0, description="Total PnL")
    win_rate: float = Field(default=0.0, ge=0, le=1, description="Win rate 0-1")
    avg_rr: float = Field(default=0.0, description="Average R:R")
    trades: int = Field(default=0, ge=0, description="Total trade count")
    max_dd: float = Field(default=0.0, ge=0, description="Maximum drawdown")
    profit_factor: float = Field(default=0.0, ge=0, description="Profit factor")
    wins: int = Field(default=0, ge=0, description="Winning trades")
    losses: int = Field(default=0, ge=0, description="Losing trades")
    closed_with_pnl: int = Field(default=0, ge=0, description="Closed trades with known PnL")
    closed_unknown: int = Field(default=0, ge=0, description="Closed trades with unknown PnL")
    failed_trades: int = Field(default=0, ge=0, description="Failed trades")
    
    class Config:
        json_schema_extra = {
            "example": {
                "pnl": 150.25,
                "win_rate": 0.65,
                "avg_rr": 1.8,
                "trades": 20,
                "max_dd": 45.50,
                "profit_factor": 2.1,
                "wins": 13,
                "losses": 7,
                "closed_with_pnl": 20,
                "closed_unknown": 0,
                "failed_trades": 0
            }
        }


class RRPoint(BaseModel):
    """Single point in RR time series."""
    ts: str = Field(..., description="ISO timestamp")
    rr: float = Field(..., description="R:R value")


class HeatCell(BaseModel):
    """Heatmap cell data for day/hour analysis."""
    dow: int = Field(..., ge=0, le=6, description="Day of week (0=Sun, 6=Sat)")
    hour: int = Field(..., ge=0, le=23, description="Hour of day (0-23)")
    trades: int = Field(default=0, ge=0, description="Trade count")
    pnl: float = Field(default=0.0, description="Total PnL for this cell")
    win_rate: float = Field(default=0.0, ge=0, le=1, description="Win rate 0-1")


# WebSocket message types
class WSTradeMessage(BaseModel):
    """WebSocket message for trade events."""
    type: Literal["trade_opened", "trade_closed"] = Field(..., description="Event type")
    trade: TradeOut = Field(..., description="Trade data")
    timestamp: str = Field(..., description="Event timestamp ISO")


class WSHeartbeat(BaseModel):
    """WebSocket heartbeat message."""
    type: Literal["heartbeat"] = "heartbeat"
    timestamp: str = Field(..., description="Heartbeat timestamp ISO")
    connected_clients: int = Field(default=0, description="Number of connected clients")
