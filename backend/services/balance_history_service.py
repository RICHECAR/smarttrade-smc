"""
Balance History Service — SQLite Persistence & Scheduler for Multi-Broker Balances
═══════════════════════════════════════════════════════════════════════════════════

Provides:
  - Periodic balance saves to SQLite
  - Balance history queries
  - Multi-broker balance tracking (Bitget + Kraken)

Usage:
    from backend.services.balance_history_service import BalanceHistoryService
    
    service = BalanceHistoryService()
    await service.record_balance("bitget", 1000.0, 1200.0, "USDT")
    history = await service.get_history("bitget", days=7)
"""

import asyncio
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from core.env_loader import is_test_mode

logger = logging.getLogger("smarttrade.balance_history")

# Database path (default for production only)
DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data",
    "balance_history.sqlite3"
)


@dataclass
class BalanceRecord:
    """Balance record data class."""
    id: int
    broker: str
    available: float
    total: float
    currency: str
    margin_used: float
    unrealized_pnl: float
    timestamp: datetime
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "broker": self.broker,
            "available": self.available,
            "total": self.total,
            "currency": self.currency,
            "margin_used": self.margin_used,
            "unrealized_pnl": self.unrealized_pnl,
            "timestamp": self.timestamp.isoformat(),
        }


class BalanceHistoryService:
    """
    Service for recording and querying balance history.
    
    Features:
      - SQLite persistence
      - Scheduled periodic saves
      - Multi-broker support (bitget, kraken)
      - History queries with date range
    """
    
    _instance: Optional["BalanceHistoryService"] = None
    
    def __new__(cls, db_path: Optional[str] = None, auto_init: bool = True):
        if db_path is not None:
            inst = super().__new__(cls)
            inst._initialized = False
            return inst
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, db_path: Optional[str] = None, auto_init: bool = True):
        if self._initialized:
            return
        
        self._initialized = True
        self._scheduler_task: Optional[asyncio.Task] = None
        self._running = False
        self._save_interval = 300  # 5 minutes
        self._engine = None
        self._mem_conn: Optional[sqlite3.Connection] = None

        resolved_path = db_path or os.getenv("BALANCE_HISTORY_DB_PATH") or DB_PATH
        if is_test_mode() and db_path is None and not os.getenv("BALANCE_HISTORY_DB_PATH"):
            raise RuntimeError(
                "SMARTTRADE_TEST_MODE=1: BalanceHistoryService requires an explicit db_path "
                "(e.g. ':memory:' or temp file); fallback to data/balance_history.sqlite3 is forbidden."
            )
        self._db_path = resolved_path

        if auto_init:
            self._init_db()
            logger.info("[BALANCE_HISTORY] Service initialized | db=%s", self._db_path)

    @contextmanager
    def _connect(self):
        if self._db_path == ":memory:":
            if self._mem_conn is None:
                self._mem_conn = sqlite3.connect(":memory:")
            yield self._mem_conn
        else:
            with sqlite3.connect(self._db_path) as conn:
                yield conn
    
    def _init_db(self) -> None:
        """Create balance_history table if not exists."""
        if self._db_path != ":memory:":
            db_dir = os.path.dirname(self._db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
        
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS balance_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    broker TEXT NOT NULL,
                    available REAL NOT NULL,
                    total REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    margin_used REAL DEFAULT 0,
                    unrealized_pnl REAL DEFAULT 0,
                    timestamp TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_balance_broker_ts 
                ON balance_history(broker, timestamp)
            """)
            
            conn.commit()
        
        logger.info("[BALANCE_HISTORY] Database initialized")
    
    def set_engine(self, engine) -> None:
        """Set the multi-broker engine for scheduled balance fetching."""
        self._engine = engine
        logger.info("[BALANCE_HISTORY] Engine registered")
    
    async def record_balance(
        self,
        broker: str,
        available: float,
        total: float,
        currency: str = "USD",
        margin_used: float = 0.0,
        unrealized_pnl: float = 0.0,
    ) -> int:
        """
        Record a balance snapshot.
        
        Args:
            broker: Broker name ('bitget' or 'kraken')
            available: Available balance
            total: Total equity
            currency: Currency code
            margin_used: Margin used
            unrealized_pnl: Unrealized PnL
            
        Returns:
            Record ID
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        
        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT INTO balance_history 
                (broker, available, total, currency, margin_used, unrealized_pnl, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (broker, available, total, currency, margin_used, unrealized_pnl, timestamp))
            
            record_id = cursor.lastrowid
            conn.commit()
        
        logger.debug("[BALANCE_HISTORY] Recorded %s | available=%.2f | total=%.2f",
                    broker, available, total)
        
        return record_id
    
    async def get_history(
        self,
        broker: Optional[str] = None,
        days: int = 7,
        limit: int = 1000,
    ) -> List[BalanceRecord]:
        """
        Get balance history.
        
        Args:
            broker: Filter by broker (None for all)
            days: Number of days to look back
            limit: Maximum records to return
            
        Returns:
            List of balance records
        """
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            
            if broker:
                query = """
                    SELECT * FROM balance_history 
                    WHERE broker = ? AND timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """
                cursor = conn.execute(query, (broker, since, limit))
            else:
                query = """
                    SELECT * FROM balance_history 
                    WHERE timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """
                cursor = conn.execute(query, (since, limit))
            
            rows = cursor.fetchall()
        
        records = []
        for row in rows:
            try:
                ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            except (ValueError, TypeError, AttributeError) as _ts_err:
                logger.warning("[BALANCE_HISTORY] Timestamp parse error (row %s): %s", row.get("id", "?"), _ts_err)
                ts = datetime.now(timezone.utc)
            
            records.append(BalanceRecord(
                id=row["id"],
                broker=row["broker"],
                available=row["available"],
                total=row["total"],
                currency=row["currency"],
                margin_used=row["margin_used"] or 0,
                unrealized_pnl=row["unrealized_pnl"] or 0,
                timestamp=ts,
            ))
        
        return records
    
    async def get_latest(self, broker: str) -> Optional[BalanceRecord]:
        """Get the most recent balance for a broker."""
        records = await self.get_history(broker=broker, days=1, limit=1)
        return records[0] if records else None
    
    async def get_all_latest(self) -> Dict[str, BalanceRecord]:
        """Get latest balance for all brokers."""
        result = {}
        for broker in ("bitget", "kraken"):
            record = await self.get_latest(broker)
            if record:
                result[broker] = record
        return result
    
    async def start_scheduler(self, interval_seconds: int = 300) -> None:
        """
        Start the balance recording scheduler.
        
        Args:
            interval_seconds: Seconds between balance saves (default: 5 min)
        """
        if self._running:
            logger.warning("[BALANCE_HISTORY] Scheduler already running")
            return
        
        self._save_interval = interval_seconds
        self._running = True
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        
        logger.info("[BALANCE_HISTORY] Scheduler started | interval=%d sec", interval_seconds)
    
    async def stop_scheduler(self) -> None:
        """Stop the balance recording scheduler."""
        self._running = False
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        
        logger.info("[BALANCE_HISTORY] Scheduler stopped")
    
    async def _scheduler_loop(self) -> None:
        """Background loop for periodic balance saves."""
        logger.info("[BALANCE_HISTORY] Scheduler loop started")
        
        while self._running:
            try:
                await self._save_all_balances()
                await asyncio.sleep(self._save_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[BALANCE_HISTORY] Scheduler error: %s", e)
                await asyncio.sleep(60)  # Wait a bit before retrying
    
    async def _save_all_balances(self) -> None:
        """Save balances for all brokers."""
        if not self._engine:
            logger.debug("[BALANCE_HISTORY] No engine set, skipping save")
            return
        
        try:
            # Refresh balances from exchanges
            await self._engine.refresh_all_balances()
            
            # Get balances from engine
            balances = self._engine.get_balances()
            
            for broker_key, balance in balances.items():
                # Handle both string keys and enum keys
                broker_name = broker_key.value if hasattr(broker_key, 'value') else str(broker_key)
                await self.record_balance(
                    broker=broker_name,
                    available=balance.available,
                    total=balance.total,
                    currency=balance.currency,
                    margin_used=balance.margin_used,
                    unrealized_pnl=balance.unrealized_pnl,
                )
            
            # PHASE 6 OBSERVABILITY: Detailed JSON aggregation for 7d and 30d
            import json
            for days in [7, 30]:
                try:
                    history = await self.get_history(days=days, limit=1000)
                    if not history:
                        logger.info(json.dumps({
                            "event": "BALANCE_HISTORY_AGG",
                            "days": days,
                            "status": "NO_DATA",
                            "ts": datetime.now(timezone.utc).isoformat()
                        }))
                        continue
                    
                    # Group by broker to show progress per exchange
                    brokers = set(r.broker for r in history)
                    for b_name in brokers:
                        b_history = [r for r in history if r.broker == b_name]
                        if not b_history: continue
                        
                        # Sort by timestamp to find start/end
                        b_history.sort(key=lambda x: x.timestamp)
                        start_v = b_history[0].total
                        end_v = b_history[-1].total
                        all_values = [r.total for r in b_history]
                        
                        logger.info(json.dumps({
                            "event": "BALANCE_HISTORY_AGG",
                            "days": days,
                            "broker": b_name,
                            "start_equity": round(start_v, 2),
                            "end_equity": round(end_v, 2),
                            "min_equity": round(min(all_values), 2),
                            "max_equity": round(max(all_values), 2),
                            "change_abs": round(end_v - start_v, 2),
                            "change_pct": round(((end_v - start_v) / start_v * 100), 2) if start_v else 0,
                            "count": len(b_history),
                            "ts": datetime.now(timezone.utc).isoformat()
                        }))
                except Exception as e:
                    logger.warning("[BALANCE_HISTORY] Failed to log %dd summary: %s", days, e)

            logger.info("[BALANCE_HISTORY] Saved %d broker balances", len(balances))
            
        except Exception as e:
            logger.error("[BALANCE_HISTORY] Failed to save balances: %s", e)
    
    async def delete_old_records(self, days: int = 90) -> int:
        """
        Delete records older than specified days.
        
        Args:
            days: Delete records older than this
            
        Returns:
            Number of deleted records
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM balance_history WHERE timestamp < ?",
                (cutoff,)
            )
            deleted = cursor.rowcount
            conn.commit()
        
        logger.info("[BALANCE_HISTORY] Deleted %d old records (>%d days)", deleted, days)
        return deleted


# Global instance
_balance_service: Optional[BalanceHistoryService] = None


def reset_balance_history_service() -> None:
    """Reset global BalanceHistoryService singleton (for tests)."""
    global _balance_service
    if _balance_service is not None and getattr(_balance_service, "_mem_conn", None) is not None:
        try:
            _balance_service._mem_conn.close()
        except Exception:
            pass
    _balance_service = None
    BalanceHistoryService._instance = None


def get_balance_history_service(db_path: Optional[str] = None) -> BalanceHistoryService:
    """Get or create the balance history service singleton (or explicit instance if db_path set)."""
    global _balance_service
    if db_path is not None:
        return BalanceHistoryService(db_path=db_path)
    if _balance_service is None:
        _balance_service = BalanceHistoryService()
    return _balance_service
