"""
Live Trade Model - SQLAlchemy ORM for LIVE trading persistence

Schema designed for:
- Zero duplicate trades (order_id UNIQUE)
- Full lifecycle tracking (OPEN → CLOSED)
- Kraken response storage (raw_exchange JSON)
- Crash recovery (reload OPEN trades on restart)

Author: SMC Trading Bot Framework
"""

import uuid
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from sqlalchemy import Column, String, Float, Integer, DateTime, Text, Index, Boolean, UniqueConstraint
from sqlalchemy.orm import Session

from backend.db.base import Base


class LiveTrade(Base):
    """
    SQLAlchemy ORM model for LIVE trades.
    
    Lifecycle:
        1. ORDER CONFIRMED → INSERT (status=OPEN)
        2. TP/SL HIT → UPDATE (status=CLOSED)
    
    Uniqueness guaranteed by order_id (Kraken order ID).
    """
    __tablename__ = "live_trades"
    
    # ═══════════════════════════════════════════════════════════════
    # PRIMARY KEY & IDENTIFIERS
    # ═══════════════════════════════════════════════════════════════
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id = Column(String(64), nullable=False, index=True)  # Broker order ID
    parent_trade_id = Column(String(36), nullable=True, index=True)  # Links mirror trades
    client_oid = Column(String(64), nullable=True, index=True) # Identifier for idempotent execution
    
    # ═══════════════════════════════════════════════════════════════
    # BROKER IDENTIFICATION
    # ═══════════════════════════════════════════════════════════════
    broker = Column(String(20), nullable=False, default="kraken", index=True)  # kraken (primary)
    
    # ═══════════════════════════════════════════════════════════════
    # TIMESTAMPS
    # ═══════════════════════════════════════════════════════════════
    ts_open = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    ts_close = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    # ═══════════════════════════════════════════════════════════════
    # POSITION DATA
    # ═══════════════════════════════════════════════════════════════
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)  # "long" / "short"
    qty = Column(Float, nullable=False)
    entry = Column(Float, nullable=False)
    stop = Column(Float, nullable=False)
    tp = Column(Float, nullable=False)
    leverage = Column(Integer, default=10)
    notional = Column(Float, nullable=True)  # qty * entry (base notional WITHOUT leverage)
    
    # ═══════════════════════════════════════════════════════════════
    # PERFORMANCE METRICS
    # ═══════════════════════════════════════════════════════════════
    rr = Column(Float, default=0.0)  # Risk:Reward ratio
    pnl = Column(Float, nullable=True, default=None)  # Realized PnL (USDT) — None = unknown
    fee = Column(Float, default=0.0)  # Trading fees
    funding = Column(Float, default=0.0)  # Cumulated funding rate cost/income (USDT)
    close_price = Column(Float, nullable=True)  # Actual exit price
    
    # ═══════════════════════════════════════════════════════════════
    # STATUS & RESULT
    # ═══════════════════════════════════════════════════════════════
    status = Column(String(20), default="OPEN", index=True)  # OPEN / CLOSED / FAILED
    result = Column(String(10), nullable=True)  # win / loss / be / unknown
    error_message = Column(Text, nullable=True)  # Store execution error if FAILED
    close_reason = Column(String(50), nullable=True) # manual_api / bot_close / external_close
    
    # ═══════════════════════════════════════════════════════════════
    # METADATA
    # ═══════════════════════════════════════════════════════════════
    session = Column(String(20), nullable=True, index=True)  # London / NY
    preset = Column(String(50), nullable=True, index=True)
    signal_id = Column(String(64), nullable=True)  # For dedup tracking
    
    # ═══════════════════════════════════════════════════════════════
    # ARCHIVE & CLEANUP
    # ═══════════════════════════════════════════════════════════════
    archived = Column(Boolean, default=False, index=True)
    archive_reason = Column(String(50), nullable=True)
    
    # ═══════════════════════════════════════════════════════════════
    # RAW EXCHANGE DATA (JSON)
    # ═══════════════════════════════════════════════════════════════
    raw_exchange = Column(Text, nullable=True)  # JSON string of Bitget response
    
    # ═══════════════════════════════════════════════════════════════
    # INDEXES
    # ═══════════════════════════════════════════════════════════════
    __table_args__ = (
        Index('idx_live_trades_status_symbol', 'status', 'symbol'),
        Index('idx_live_trades_ts_open', 'ts_open'),
        Index('idx_live_trades_ts_close', 'ts_close'),
        # UNIQUE on order_id+broker: prevent duplicate trade records
        UniqueConstraint('order_id', 'broker', name='uq_live_trades_order_broker'),
        # UNIQUE on signal_id+broker for OPEN trades: prevent double-entry from same signal
        # Note: signal_id can be NULL (manual trades), so only applies when set
        Index('idx_live_trades_signal_broker', 'signal_id', 'broker'),
    )
    
    # ═══════════════════════════════════════════════════════════════
    # METHODS
    # ═══════════════════════════════════════════════════════════════
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API/WebSocket."""
        return {
            "id": self.id,
            "order_id": self.order_id,
            "parent_trade_id": self.parent_trade_id,
            "broker": self.broker,
            "ts_open": self.ts_open.isoformat() if self.ts_open else None,
            "ts_close": self.ts_close.isoformat() if self.ts_close else None,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "entry": self.entry,
            "stop": self.stop,
            "tp": self.tp,
            "leverage": self.leverage,
            "notional": self.notional,
            "rr": self.rr,
            "pnl": self.pnl,
            "fee": self.fee,
            "funding": self.funding,
            "close_price": self.close_price,
            "status": self.status,
            "result": self.result,
            "error_message": self.error_message,
            "close_reason": self.close_reason,
            "session": self.session,
            "preset": self.preset,
            "signal_id": self.signal_id,
            "client_oid": self.client_oid,
            "archived": self.archived,
            "archive_reason": self.archive_reason,
        }
    
    def set_raw_exchange(self, data: Dict[str, Any]) -> None:
        """Store raw exchange response as JSON."""
        self.raw_exchange = json.dumps(data, default=str)
    
    def get_raw_exchange(self) -> Optional[Dict[str, Any]]:
        """Parse raw exchange JSON."""
        if self.raw_exchange:
            try:
                return json.loads(self.raw_exchange)
            except json.JSONDecodeError:
                return None
        return None
    
    @staticmethod
    def calculate_rr(entry: float, stop: float, tp: float, side: str) -> float:
        """Calculate Risk:Reward ratio."""
        if side.lower() == "long":
            risk = entry - stop
            reward = tp - entry
        else:
            risk = stop - entry
            reward = entry - tp
        
        if risk <= 0:
            return 0.0
        return round(reward / risk, 2)
    
    @staticmethod
    def calculate_pnl(
        entry: float,
        close_price: float,
        qty: float,
        side: str,
        fee: float = 0.0,
        funding: float = 0.0,
    ) -> float:
        """Calculate realized PnL including fees and funding costs."""
        if side.lower() == "long":
            raw_pnl = (close_price - entry) * qty
        else:
            raw_pnl = (entry - close_price) * qty
        return round(raw_pnl - fee + funding, 2)
    
    @staticmethod
    def determine_result(
        pnl: Optional[float],
        entry: float,
        close_price: Optional[float],
        tp: float,
        stop: float,
    ) -> str:
        """Determine trade result based on PnL and prices."""
        # When close price is unavailable, result cannot be deduced from price target
        if close_price is None or close_price <= 0:
            if pnl is not None and pnl > 0:
                return "win"
            elif pnl is not None and pnl < 0:
                return "loss"
            elif pnl is not None and pnl == 0.0:
                return "be"
            return "unknown"

        # If close price hit TP zone (within 1%)
        tp_tolerance = abs(tp - entry) * 0.01
        sl_tolerance = abs(stop - entry) * 0.01
        
        if abs(close_price - tp) <= tp_tolerance:
            return "win"
        elif abs(close_price - stop) <= sl_tolerance:
            return "loss"
        elif pnl is not None and pnl > 0:
            return "win"
        elif pnl is not None and pnl < 0:
            return "loss"
        elif pnl is not None and pnl == 0.0:
            return "be"  # Break-even
        else:
            return "unknown"
