"""
Trade Fill Model - SQLAlchemy ORM for partial fill tracking

Tracks individual fills for position close/reduce operations:
- Links to parent LiveTrade
- Records qty, price, fee, pnl per fill
- Aggregates to parent for total PnL

Author: SMC Trading Bot Framework
"""

import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from sqlalchemy import Column, String, Float, DateTime, Text, ForeignKey, Index
from sqlalchemy.orm import relationship

from backend.db.base import Base


class TradeFill(Base):
    """
    SQLAlchemy ORM model for trade fills.
    
    Records partial fills for close/reduce operations.
    Links to parent LiveTrade via trade_id.
    """
    __tablename__ = "trade_fills"
    
    # ═══════════════════════════════════════════════════════════════
    # PRIMARY KEY & FOREIGN KEY
    # ═══════════════════════════════════════════════════════════════
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    trade_id = Column(String(36), ForeignKey("live_trades.id"), nullable=False, index=True)
    order_id = Column(String(64), nullable=True, index=True)  # Bitget order ID for this fill
    
    # ═══════════════════════════════════════════════════════════════
    # FILL DATA
    # ═══════════════════════════════════════════════════════════════
    action = Column(String(10), nullable=False)  # "OPEN", "REDUCE", "CLOSE"
    qty = Column(Float, nullable=False)          # Fill quantity
    price = Column(Float, nullable=False)        # Fill price
    fee = Column(Float, default=0.0)             # Trading fee for this fill
    pnl = Column(Float, default=0.0)             # Realized PnL for this fill
    
    # ═══════════════════════════════════════════════════════════════
    # TIMESTAMPS
    # ═══════════════════════════════════════════════════════════════
    ts = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    # ═══════════════════════════════════════════════════════════════
    # METADATA
    # ═══════════════════════════════════════════════════════════════
    raw_response = Column(Text, nullable=True)  # JSON: Bitget response
    
    # ═══════════════════════════════════════════════════════════════
    # INDEXES
    # ═══════════════════════════════════════════════════════════════
    __table_args__ = (
        Index("ix_trade_fills_trade_action", "trade_id", "action"),
    )
    
    # ═══════════════════════════════════════════════════════════════
    # METHODS
    # ═══════════════════════════════════════════════════════════════
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to API-friendly dictionary."""
        return {
            "id": self.id,
            "trade_id": self.trade_id,
            "order_id": self.order_id,
            "action": self.action,
            "qty": self.qty,
            "price": self.price,
            "fee": self.fee,
            "pnl": self.pnl,
            "ts": self.ts.isoformat() if self.ts else None,
        }
    
    def __repr__(self) -> str:
        return (
            f"<TradeFill {self.id[:8]}... "
            f"action={self.action} qty={self.qty} pnl={self.pnl}>"
        )
    
    # ═══════════════════════════════════════════════════════════════
    # STATIC HELPERS
    # ═══════════════════════════════════════════════════════════════
    
    @staticmethod
    def calculate_pnl(
        action: str,
        side: str,
        qty: float,
        entry_price: float,
        exit_price: float,
    ) -> float:
        """
        Calculate PnL for a fill.
        
        Args:
            action: "OPEN", "REDUCE", or "CLOSE"
            side: "long" or "short"
            qty: Fill quantity
            entry_price: Original entry price
            exit_price: Fill/exit price
        
        Returns:
            Realized PnL (positive = profit, negative = loss)
        """
        if action == "OPEN":
            return 0.0  # Opening fills have no PnL yet
        
        if side.lower() == "long":
            pnl = (exit_price - entry_price) * qty
        else:  # short
            pnl = (entry_price - exit_price) * qty
        
        return round(pnl, 4)


# ═══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def aggregate_fills_pnl(fills: list) -> Dict[str, float]:
    """
    Aggregate PnL and fees from a list of fills.
    
    Returns:
        {"total_pnl": float, "total_fee": float, "fill_count": int}
    """
    total_pnl = sum(f.pnl for f in fills if f.pnl)
    total_fee = sum(f.fee for f in fills if f.fee)
    
    return {
        "total_pnl": round(total_pnl, 4),
        "total_fee": round(total_fee, 4),
        "fill_count": len(fills),
    }
