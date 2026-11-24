"""
Persisted Position Model - SQLAlchemy ORM for cross-broker position tracking

Stores ALL positions from Bitget and Kraken with full lifecycle:
- OPEN: Position is active (entry executed)
- CLOSED: Position closed (exit executed, PnL finalized)

Author: SMC Trading Bot Framework
"""

import uuid
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from enum import Enum

from sqlalchemy import Column, String, Float, Integer, DateTime, Text, Index, Enum as SQLEnum
from sqlalchemy.orm import Session

from backend.db.base import Base


class PositionStatus(str, Enum):
    """Position lifecycle status."""
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class PositionSide(str, Enum):
    """Position direction."""
    LONG = "long"
    SHORT = "short"


class BrokerName(str, Enum):
    """Supported brokers."""
    BITGET = "bitget"
    KRAKEN = "kraken"


class PersistedPosition(Base):
    """
    SQLAlchemy ORM model for persisted positions across brokers.
    
    Tracks complete position lifecycle from entry to exit with:
    - Full broker identification
    - Entry/exit prices and times
    - PnL calculation
    - Risk metrics (R:R, stop distance)
    """
    __tablename__ = "persisted_positions"
    
    # ═══════════════════════════════════════════════════════════════
    # PRIMARY KEY & IDENTIFIERS
    # ═══════════════════════════════════════════════════════════════
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id = Column(String(64), nullable=True, index=True)  # Exchange order ID
    
    # ═══════════════════════════════════════════════════════════════
    # BROKER IDENTIFICATION
    # ═══════════════════════════════════════════════════════════════
    broker = Column(String(20), nullable=False, index=True)  # "bitget" or "kraken"
    
    # ═══════════════════════════════════════════════════════════════
    # POSITION DATA
    # ═══════════════════════════════════════════════════════════════
    symbol = Column(String(30), nullable=False, index=True)
    side = Column(String(10), nullable=False)  # "long" / "short"
    size = Column(Float, nullable=False)  # Position size/quantity
    leverage = Column(Integer, default=10)
    notional = Column(Float, nullable=True)  # size * entry_price
    
    # ═══════════════════════════════════════════════════════════════
    # ENTRY DATA
    # ═══════════════════════════════════════════════════════════════
    entry_price = Column(Float, nullable=False)
    entry_time = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    
    # ═══════════════════════════════════════════════════════════════
    # EXIT DATA (populated on close)
    # ═══════════════════════════════════════════════════════════════
    exit_price = Column(Float, nullable=True)
    exit_time = Column(DateTime, nullable=True)
    
    # ═══════════════════════════════════════════════════════════════
    # PERFORMANCE METRICS
    # ═══════════════════════════════════════════════════════════════
    pnl = Column(Float, default=0.0)  # Realized PnL
    pnl_percent = Column(Float, default=0.0)  # PnL as percentage
    fee = Column(Float, default=0.0)  # Trading fees
    rr = Column(Float, default=0.0)  # Risk:Reward ratio achieved
    rr_target = Column(Float, default=0.0)  # Target R:R at entry
    
    # ═══════════════════════════════════════════════════════════════
    # STATUS
    # ═══════════════════════════════════════════════════════════════
    status = Column(String(20), default="OPEN", index=True)  # OPEN / CLOSED / CANCELLED
    result = Column(String(10), nullable=True)  # win / loss / be (break-even)
    close_reason = Column(String(50), nullable=True)  # TP_HIT / SL_HIT / MANUAL / LIQUIDATION
    
    # ═══════════════════════════════════════════════════════════════
    # METADATA
    # ═══════════════════════════════════════════════════════════════
    session_name = Column(String(30), nullable=True, index=True)  # London / NY
    preset = Column(String(50), nullable=True, index=True)
    signal_id = Column(String(64), nullable=True)  # For dedup tracking
    notes = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    archived = Column(Integer, default=0, index=True)  # Boolean flag using Integer (0/1) for SQLite
    archive_reason = Column(String(100), nullable=True)
    
    # ═══════════════════════════════════════════════════════════════
    # TIMESTAMPS
    # ═══════════════════════════════════════════════════════════════
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), 
                       onupdate=lambda: datetime.now(timezone.utc))
    
    # ═══════════════════════════════════════════════════════════════
    # RAW EXCHANGE DATA
    # ═══════════════════════════════════════════════════════════════
    raw_entry_response = Column(Text, nullable=True)  # JSON of entry order response
    raw_exit_response = Column(Text, nullable=True)   # JSON of exit order response
    
    # ═══════════════════════════════════════════════════════════════
    # INDEXES
    # ═══════════════════════════════════════════════════════════════
    __table_args__ = (
        Index('idx_pp_broker_status', 'broker', 'status'),
        Index('idx_pp_symbol_broker', 'symbol', 'broker'),
        Index('idx_pp_entry_time', 'entry_time'),
        Index('idx_pp_exit_time', 'exit_time'),
        Index('idx_pp_status_entry', 'status', 'entry_time'),
    )
    
    # ═══════════════════════════════════════════════════════════════
    # METHODS
    # ═══════════════════════════════════════════════════════════════
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API/WebSocket responses."""
        return {
            "id": self.id,
            "order_id": self.order_id,
            "broker": self.broker,
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "leverage": self.leverage,
            "notional": self.notional,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "pnl": self.pnl,
            "pnl_percent": self.pnl_percent,
            "fee": self.fee,
            "rr": self.rr,
            "rr_target": self.rr_target,
            "status": self.status,
            "result": self.result,
            "close_reason": self.close_reason,
            "session_name": self.session_name,
            "preset": self.preset,
            "signal_id": self.signal_id,
            "notes": self.notes,
            "error_message": self.error_message,
            "archived": bool(self.archived),
            "archive_reason": self.archive_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
    
    def to_summary(self) -> Dict[str, Any]:
        """Compact summary for lists."""
        return {
            "id": self.id,
            "broker": self.broker,
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "pnl": self.pnl,
            "status": self.status,
            "result": self.result,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
        }
    
    def set_raw_entry(self, data: Dict[str, Any]) -> None:
        """Store raw entry response as JSON."""
        self.raw_entry_response = json.dumps(data, default=str)
    
    def set_raw_exit(self, data: Dict[str, Any]) -> None:
        """Store raw exit response as JSON."""
        self.raw_exit_response = json.dumps(data, default=str)
    
    def get_raw_entry(self) -> Optional[Dict[str, Any]]:
        """Parse raw entry JSON."""
        if self.raw_entry_response:
            try:
                return json.loads(self.raw_entry_response)
            except json.JSONDecodeError:
                return None
        return None
    
    def get_raw_exit(self) -> Optional[Dict[str, Any]]:
        """Parse raw exit JSON."""
        if self.raw_exit_response:
            try:
                return json.loads(self.raw_exit_response)
            except json.JSONDecodeError:
                return None
        return None
    
    @staticmethod
    def calculate_rr(
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        side: str
    ) -> float:
        """Calculate target Risk:Reward ratio."""
        if side.lower() == "long":
            risk = entry_price - stop_loss
            reward = take_profit - entry_price
        else:
            risk = stop_loss - entry_price
            reward = entry_price - take_profit
        
        if risk <= 0:
            return 0.0
        return round(reward / risk, 2)
    
    @staticmethod
    def calculate_pnl(
        entry_price: float,
        exit_price: float,
        size: float,
        side: str,
        fee: float = 0.0
    ) -> float:
        """Calculate realized PnL."""
        if side.lower() == "long":
            raw_pnl = (exit_price - entry_price) * size
        else:
            raw_pnl = (entry_price - exit_price) * size
        return round(raw_pnl - fee, 4)
    
    @staticmethod
    def calculate_pnl_percent(
        entry_price: float,
        exit_price: float,
        side: str,
        leverage: int = 1
    ) -> float:
        """Calculate PnL as percentage with leverage."""
        if entry_price == 0:
            return 0.0
        if side.lower() == "long":
            percent = ((exit_price - entry_price) / entry_price) * 100 * leverage
        else:
            percent = ((entry_price - exit_price) / entry_price) * 100 * leverage
        return round(percent, 2)
    
    @staticmethod
    def determine_result(pnl: float) -> str:
        """Determine trade result based on PnL."""
        if pnl > 0.01:  # Small threshold to avoid float issues
            return "win"
        elif pnl < -0.01:
            return "loss"
        else:
            return "be"  # Break-even
    
    def close_position(
        self,
        exit_price: float,
        exit_time: Optional[datetime] = None,
        fee: float = 0.0,
        close_reason: str = "MANUAL",
        raw_response: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Close this position with exit data.
        
        Args:
            exit_price: The exit/close price
            exit_time: Time of exit (defaults to now)
            fee: Trading fee for exit
            close_reason: Reason for close (TP_HIT, SL_HIT, MANUAL, etc.)
            raw_response: Raw exchange response for exit order
        """
        self.exit_price = exit_price
        self.exit_time = exit_time or datetime.now(timezone.utc)
        self.fee = (self.fee or 0.0) + fee
        self.close_reason = close_reason
        self.status = PositionStatus.CLOSED.value
        
        # Calculate PnL
        self.pnl = self.calculate_pnl(
            self.entry_price, exit_price, self.size, self.side, self.fee
        )
        self.pnl_percent = self.calculate_pnl_percent(
            self.entry_price, exit_price, self.side, self.leverage or 1
        )
        
        # Calculate achieved R:R
        if self.stop_loss and self.entry_price:
            risk = abs(self.entry_price - self.stop_loss)
            if risk > 0:
                reward = abs(exit_price - self.entry_price)
                self.rr = round(reward / risk, 2) if self.pnl > 0 else round(-reward / risk, 2)
        
        # Determine result
        self.result = self.determine_result(self.pnl)
        
        # Store raw response
        if raw_response:
            self.set_raw_exit(raw_response)
        
        self.updated_at = datetime.now(timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
# PYDANTIC SCHEMAS FOR API
# ══════════════════════════════════════════════════════════════════════════════

from pydantic import BaseModel, Field
from typing import Literal


class PositionCreate(BaseModel):
    """Schema for creating a new position."""
    broker: Literal["bitget", "kraken"]
    symbol: str = Field(..., min_length=1, max_length=30)
    side: Literal["long", "short"]
    size: float = Field(..., gt=0)
    entry_price: float = Field(..., gt=0)
    leverage: int = Field(default=10, ge=1, le=125)
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    order_id: Optional[str] = None
    session_name: Optional[str] = None
    preset: Optional[str] = None
    signal_id: Optional[str] = None
    notes: Optional[str] = None
    status: str = "OPEN"
    error_message: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None
    
    class Config:
        extra = "forbid"


class PositionClose(BaseModel):
    """Schema for closing a position."""
    exit_price: float = Field(..., gt=0)
    fee: float = Field(default=0.0, ge=0)
    close_reason: str = Field(default="MANUAL")
    raw_response: Optional[Dict[str, Any]] = None
    
    class Config:
        extra = "forbid"


class PositionOut(BaseModel):
    """Schema for position API responses."""
    id: str
    order_id: Optional[str]
    broker: str
    symbol: str
    side: str
    size: float
    leverage: int
    notional: Optional[float]
    entry_price: float
    entry_time: Optional[str]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    exit_price: Optional[float]
    exit_time: Optional[str]
    pnl: float
    pnl_percent: float
    fee: float
    rr: float
    rr_target: float
    status: str
    result: Optional[str]
    close_reason: Optional[str]
    session_name: Optional[str]
    preset: Optional[str]
    error_message: Optional[str]
    archived: bool = False
    archive_reason: Optional[str] = None
    
    class Config:
        from_attributes = True


class PerformanceStats(BaseModel):
    """Schema for performance statistics."""
    total_pnl: float = Field(..., description="Total realized PnL")
    win_rate: float = Field(..., ge=0, le=1, description="Win rate (0-1)")
    avg_rr: float = Field(..., description="Average R:R ratio")
    total_trades: int = Field(..., ge=0, description="Total number of closed trades")
    max_drawdown: float = Field(..., description="Maximum drawdown")
    profit_factor: float = Field(..., ge=0, description="Gross profit / Gross loss")
    wins: int = Field(..., ge=0, description="Number of winning trades")
    losses: int = Field(..., ge=0, description="Number of losing trades")
    break_even: int = Field(default=0, ge=0, description="Number of break-even trades")
    avg_win: float = Field(default=0.0, description="Average winning trade PnL")
    avg_loss: float = Field(default=0.0, description="Average losing trade PnL")
    best_trade: float = Field(default=0.0, description="Best single trade PnL")
    worst_trade: float = Field(default=0.0, description="Worst single trade PnL")
    avg_hold_time_hours: float = Field(default=0.0, description="Average position hold time in hours")
    
    class Config:
        from_attributes = True


class PositionListResponse(BaseModel):
    """Schema for position list API response."""
    positions: List[PositionOut]
    total: int
    open_count: int
    closed_count: int
    page: int = 1
    page_size: int = 50
    
    class Config:
        from_attributes = True


class PerformanceResponse(BaseModel):
    """Schema for performance API response."""
    stats: PerformanceStats
    broker_filter: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    timestamp: str
    
    class Config:
        from_attributes = True
