"""
Trades Service - Database operations for trade records

Uses SQLite for persistent storage of all trades with:
- Open/close lifecycle tracking
- Preset and session metadata
- Full position sizing details

Author: SMC Trading Bot Framework
"""

import os
import sqlite3
import logging
import uuid
from datetime import date, datetime, timezone
from typing import List, Optional
from contextlib import contextmanager

from backend.models.trade import TradeOut, TradeIn, TradeUpdate, TradeResult, TradeStatus, Side

logger = logging.getLogger(__name__)

# Database path - uses data/ folder in project root
DB_PATH = os.environ.get("TRADES_DB_PATH", "data/trades.db")


def _ensure_db_dir():
    """Ensure data directory exists."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        logger.info(f"[TRADES_DB] Created directory: {db_dir}")


def _init_db():
    """Initialize trades database and create table if not exists."""
    _ensure_db_dir()
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry REAL NOT NULL,
                stop REAL NOT NULL,
                tp REAL NOT NULL,
                rr REAL NOT NULL,
                pnl REAL DEFAULT 0.0,
                session TEXT,
                preset TEXT,
                result TEXT,
                status TEXT DEFAULT 'open',
                qty REAL,
                leverage INTEGER,
                notional REAL,
                risk_amount REAL,
                stop_distance REAL,
                close_ts TEXT,
                close_price REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create indexes for common queries
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_session ON trades(session)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_preset ON trades(preset)")
        
        conn.commit()
        logger.info(f"[TRADES_DB] Initialized: {DB_PATH}")


# Initialize on module import
_init_db()


@contextmanager
def get_db():
    """Database connection context manager."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


class TradesService:
    """
    Service class for trade CRUD operations.
    All methods are static for easy use.
    """
    
    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> TradeOut:
        """Convert database row to TradeOut model."""
        return TradeOut(
            id=row["id"],
            ts=datetime.fromisoformat(row["ts"]),
            symbol=row["symbol"],
            side=Side(row["side"]),
            entry=row["entry"],
            stop=row["stop"],
            tp=row["tp"],
            rr=row["rr"],
            pnl=row["pnl"] or 0.0,
            session=row["session"],
            preset=row["preset"],
            result=TradeResult(row["result"]) if row["result"] else None,
            status=TradeStatus(row["status"]) if row["status"] else TradeStatus.OPEN,
            qty=row["qty"],
            leverage=row["leverage"],
            notional=row["notional"],
            risk_amount=row["risk_amount"],
            stop_distance=row["stop_distance"],
            close_ts=datetime.fromisoformat(row["close_ts"]) if row["close_ts"] else None,
            close_price=row["close_price"],
        )
    
    @staticmethod
    def create_trade(trade: TradeIn) -> TradeOut:
        """
        Create a new trade record.
        
        Args:
            trade: TradeIn model with trade details
        
        Returns:
            Created TradeOut with generated ID
        """
        trade_id = str(uuid.uuid4())[:8]
        ts = datetime.now(timezone.utc)
        
        # Calculate R:R
        stop_dist = abs(trade.entry - trade.stop)
        tp_dist = abs(trade.tp - trade.entry)
        rr = tp_dist / stop_dist if stop_dist > 0 else 0.0
        
        with get_db() as conn:
            conn.execute("""
                INSERT INTO trades (
                    id, ts, symbol, side, entry, stop, tp, rr,
                    session, preset, status, qty, leverage,
                    notional, risk_amount, stop_distance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id,
                ts.isoformat(),
                trade.symbol,
                trade.side.value,
                trade.entry,
                trade.stop,
                trade.tp,
                rr,
                trade.session,
                trade.preset,
                TradeStatus.OPEN.value,
                trade.qty,
                trade.leverage,
                trade.notional,
                trade.risk_amount,
                trade.stop_distance,
            ))
            conn.commit()
        
        logger.info(f"[TRADES] Created trade {trade_id}: {trade.symbol} {trade.side.value} @ {trade.entry}")
        
        return TradeOut(
            id=trade_id,
            ts=ts,
            symbol=trade.symbol,
            side=trade.side,
            entry=trade.entry,
            stop=trade.stop,
            tp=trade.tp,
            rr=rr,
            pnl=0.0,
            session=trade.session,
            preset=trade.preset,
            result=None,
            status=TradeStatus.OPEN,
            qty=trade.qty,
            leverage=trade.leverage,
            notional=trade.notional,
            risk_amount=trade.risk_amount,
            stop_distance=trade.stop_distance,
        )
    
    @staticmethod
    def close_trade(trade_id: str, update: TradeUpdate) -> Optional[TradeOut]:
        """
        Close an existing trade with result and PnL.
        
        Args:
            trade_id: Trade ID to close
            update: TradeUpdate with pnl and result
        
        Returns:
            Updated TradeOut or None if not found
        """
        close_ts = datetime.now(timezone.utc)
        
        with get_db() as conn:
            cursor = conn.execute("""
                UPDATE trades 
                SET pnl = ?, result = ?, status = ?, close_ts = ?, close_price = ?
                WHERE id = ?
            """, (
                update.pnl,
                update.result.value,
                TradeStatus.CLOSED.value,
                close_ts.isoformat(),
                update.close_price,
                trade_id,
            ))
            conn.commit()
            
            if cursor.rowcount == 0:
                logger.warning(f"[TRADES] Trade not found: {trade_id}")
                return None
            
            # Fetch updated trade
            row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
            if row:
                logger.info(f"[TRADES] Closed trade {trade_id}: {update.result.value} PnL={update.pnl:.2f}")
                return TradesService._row_to_trade(row)
        
        return None
    
    @staticmethod
    def get_trade(trade_id: str) -> Optional[TradeOut]:
        """Get a single trade by ID."""
        with get_db() as conn:
            row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
            if row:
                return TradesService._row_to_trade(row)
        return None
    
    @staticmethod
    def list_trades(
        from_date: date,
        to_date: date,
        symbol: Optional[str] = None,
        session: Optional[str] = None,
        preset: Optional[str] = None,
        status: Optional[TradeStatus] = None,
        include_archived: bool = False,
        limit: int = 1000,
    ) -> List[TradeOut]:
        """
        List trades within a date range with optional filters.
        
        Args:
            from_date: Start date (inclusive)
            to_date: End date (inclusive)
            symbol: Filter by symbol
            session: Filter by session (London/NY)
            preset: Filter by preset name
            status: Filter by status
            include_archived: Whether to include archived trades
            limit: Maximum results
        
        Returns:
            List of TradeOut sorted by timestamp descending
        """
        from_ts = datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        to_ts = datetime.combine(to_date, datetime.max.time(), tzinfo=timezone.utc).isoformat()
        
        query = "SELECT * FROM trades WHERE ts >= ? AND ts <= ?"
        params: list = [from_ts, to_ts]
        
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        
        if session:
            query += " AND session = ?"
            params.append(session)
        
        if preset:
            query += " AND preset = ?"
            params.append(preset)
        
        if status:
            query += " AND status = ?"
            params.append(status.value)
            
        if not include_archived:
            query += " AND archived = 0"
        
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        
        trades = []
        with get_db() as conn:
            rows = conn.execute(query, params).fetchall()
            trades = [TradesService._row_to_trade(row) for row in rows]
        
        logger.debug(f"[TRADES] Listed {len(trades)} trades from {from_date} to {to_date}")
        return trades
    
    @staticmethod
    def get_open_trades(symbol: Optional[str] = None) -> List[TradeOut]:
        """Get all currently open trades."""
        query = "SELECT * FROM trades WHERE status = ?"
        params: list = [TradeStatus.OPEN.value]
        
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        
        query += " ORDER BY ts DESC"
        
        trades = []
        with get_db() as conn:
            rows = conn.execute(query, params).fetchall()
            trades = [TradesService._row_to_trade(row) for row in rows]
        
        return trades
    
    @staticmethod
    def get_daily_trade_count(symbol: Optional[str] = None) -> int:
        """Get trade count for today."""
        today = datetime.now(timezone.utc).date()
        from_ts = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        to_ts = datetime.combine(today, datetime.max.time(), tzinfo=timezone.utc).isoformat()
        
        query = "SELECT COUNT(*) FROM trades WHERE ts >= ? AND ts <= ?"
        params: list = [from_ts, to_ts]
        
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        
        with get_db() as conn:
            result = conn.execute(query, params).fetchone()
            return result[0] if result else 0
    
    @staticmethod
    def delete_trade(trade_id: str) -> bool:
        """Delete a trade (admin only)."""
        with get_db() as conn:
            cursor = conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
            conn.commit()
            return cursor.rowcount > 0
