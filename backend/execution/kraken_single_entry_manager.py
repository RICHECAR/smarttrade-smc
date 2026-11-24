"""
Kraken Single-Entry Manager — Persistent Single-Trade Gate for Kraken Futures
=============================================================================

Enforces strict single-entry execution mode for live trading validation:
1. Persistent Lock: Survives backend reboots via SQLite (trades.db).
2. Atomic Reservation: Pre-allocates single permission before exchange dispatch,
   preventing concurrent signals from opening multiple positions.
3. First Fill Consumes: Any fill (full or partial) permanently consumes authorization.
4. Ambiguous Protection: Timeout or unknown response locks authorization indefinitely
   until explicit reconciliation.
5. Asymmetric Permission: Blocks all new entry orders while allowing protection
   orders (SL/TP), reductions, and closures.
6. Fail-Closed: Any database or state error blocks new entries.

Author: SmartTrade Framework
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("smarttrade.execution.single_entry")


def _get_default_db_path() -> str:
    """Resolve sqlite db path from DATABASE_URL or default."""
    db_url = os.getenv("DATABASE_URL", "sqlite:///./data/trades.db")
    if db_url.startswith("sqlite:///"):
        path = db_url.replace("sqlite:///", "")
        if path.startswith("./"):
            path = path[2:]
        return path
    return "data/trades.db"


class KrakenSingleEntryManager:
    """Thread-safe persistent manager for the single-entry execution gate."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or _get_default_db_path()
        self._lock = threading.RLock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection with timeout."""
        # Ensure parent directory exists
        db_dir = os.path.dirname(self._db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Initialize the single-entry state table if not exists."""
        with self._lock:
            try:
                conn = self._get_connection()
                try:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS kraken_single_entry_state (
                            id INTEGER PRIMARY KEY CHECK (id = 1),
                            state TEXT NOT NULL DEFAULT 'IDLE',
                            symbol TEXT,
                            client_oid TEXT,
                            order_id TEXT,
                            fill_qty REAL DEFAULT 0.0,
                            reserved_at TEXT,
                            consumed_at TEXT,
                            locked_reason TEXT,
                            updated_at TEXT
                        )
                    """)
                    conn.execute("""
                        INSERT OR IGNORE INTO kraken_single_entry_state 
                        (id, state, updated_at) 
                        VALUES (1, 'IDLE', datetime('now'))
                    """)
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:
                logger.critical("[SINGLE_ENTRY_MGR] Failed to initialize DB table: %s", e)
                raise

    def get_state(self) -> Dict[str, Any]:
        """Fetch current single-entry state from SQLite."""
        with self._lock:
            try:
                conn = self._get_connection()
                try:
                    cursor = conn.execute(
                        "SELECT state, symbol, client_oid, order_id, fill_qty, "
                        "reserved_at, consumed_at, locked_reason, updated_at "
                        "FROM kraken_single_entry_state WHERE id = 1"
                    )
                    row = cursor.fetchone()
                    if not row:
                        return {"state": "ERROR", "locked_reason": "ROW_NOT_FOUND"}
                    return dict(row)
                finally:
                    conn.close()
            except Exception as e:
                logger.critical("[SINGLE_ENTRY_MGR] Failed to read state: %s", e)
                return {"state": "ERROR", "locked_reason": f"DB_READ_ERROR: {e}"}

    def reserve_entry(self, symbol: str, client_oid: str) -> Tuple[bool, str]:
        """
        Atomically reserve the single-entry permission BEFORE dispatching order.
        
        Returns:
            (True, "RESERVED") if successfully reserved.
            (False, reason) if already reserved, consumed, or in error.
        """
        with self._lock:
            try:
                conn = self._get_connection()
                try:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    cursor = conn.execute("""
                        UPDATE kraken_single_entry_state
                        SET state = 'RESERVED',
                            symbol = ?,
                            client_oid = ?,
                            reserved_at = ?,
                            updated_at = ?
                        WHERE id = 1 AND state = 'IDLE'
                    """, (symbol, client_oid, now_iso, now_iso))
                    conn.commit()

                    if cursor.rowcount == 1:
                        logger.info(
                            "[SINGLE_ENTRY_MGR] Successfully reserved entry for %s | client_oid=%s",
                            symbol, client_oid,
                        )
                        return True, "RESERVED"

                    # Could not reserve - check current state
                    cur_state = self.get_state()
                    state_val = cur_state.get("state", "UNKNOWN")
                    reason = cur_state.get("locked_reason") or f"Already in state {state_val}"
                    logger.warning(
                        "[SINGLE_ENTRY_MGR] Reservation rejected: state=%s (%s)",
                        state_val, reason,
                    )
                    return False, f"SINGLE_ENTRY_{state_val}"
                finally:
                    conn.close()
            except Exception as e:
                logger.critical("[SINGLE_ENTRY_MGR] Exception during reservation: %s", e)
                return False, f"DB_RESERVATION_ERROR: {e}"

    def consume_entry(self, order_id: str, fill_qty: float, symbol: Optional[str] = None) -> bool:
        """
        Mark the single-entry permission as permanently CONSUMED.
        Triggered on any fill (full or partial).
        """
        with self._lock:
            try:
                conn = self._get_connection()
                try:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    conn.execute("""
                        UPDATE kraken_single_entry_state
                        SET state = 'CONSUMED',
                            order_id = ?,
                            fill_qty = ?,
                            symbol = COALESCE(?, symbol),
                            consumed_at = ?,
                            updated_at = ?
                        WHERE id = 1
                    """, (order_id, fill_qty, symbol, now_iso, now_iso))
                    conn.commit()
                    logger.info(
                        "[SINGLE_ENTRY_MGR] Permission permanently CONSUMED | order_id=%s qty=%s",
                        order_id, fill_qty,
                    )
                    return True
                finally:
                    conn.close()
            except Exception as e:
                logger.critical("[SINGLE_ENTRY_MGR] Failed to consume entry: %s", e)
                return False

    def lock_ambiguous(self, reason: str, symbol: Optional[str] = None, client_oid: Optional[str] = None) -> bool:
        """
        Lock authorization due to timeout or ambiguous response.
        FAIL-CLOSED: Never allows another entry until manual resolution.
        """
        with self._lock:
            try:
                conn = self._get_connection()
                try:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    conn.execute("""
                        UPDATE kraken_single_entry_state
                        SET state = 'LOCKED_AMBIGUOUS',
                            locked_reason = ?,
                            symbol = COALESCE(?, symbol),
                            client_oid = COALESCE(?, client_oid),
                            updated_at = ?
                        WHERE id = 1
                    """, (reason, symbol, client_oid, now_iso))
                    conn.commit()
                    logger.critical(
                        "[SINGLE_ENTRY_MGR] Permission LOCKED_AMBIGUOUS: %s (symbol=%s)",
                        reason, symbol,
                    )
                    return True
                finally:
                    conn.close()
            except Exception as e:
                logger.critical("[SINGLE_ENTRY_MGR] Failed to set LOCKED_AMBIGUOUS: %s", e)
                return False

    def release_reservation(self, client_oid: str) -> bool:
        """
        Cleanly release reservation back to IDLE if an order failed PRE-FLIGHT
        before being transmitted to the exchange.
        """
        with self._lock:
            try:
                conn = self._get_connection()
                try:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    cursor = conn.execute("""
                        UPDATE kraken_single_entry_state
                        SET state = 'IDLE',
                            symbol = NULL,
                            client_oid = NULL,
                            reserved_at = NULL,
                            updated_at = ?
                        WHERE id = 1 AND state = 'RESERVED' AND client_oid = ?
                    """, (now_iso, client_oid))
                    conn.commit()
                    if cursor.rowcount == 1:
                        logger.info(
                            "[SINGLE_ENTRY_MGR] Reservation released back to IDLE for client_oid=%s",
                            client_oid,
                        )
                        return True
                    return False
                finally:
                    conn.close()
            except Exception as e:
                logger.critical("[SINGLE_ENTRY_MGR] Failed to release reservation: %s", e)
                return False

    def reset_for_test(self) -> bool:
        """Reset state back to IDLE (only for unit / integration tests)."""
        with self._lock:
            try:
                conn = self._get_connection()
                try:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    conn.execute("""
                        UPDATE kraken_single_entry_state
                        SET state = 'IDLE',
                            symbol = NULL,
                            client_oid = NULL,
                            order_id = NULL,
                            fill_qty = 0.0,
                            reserved_at = NULL,
                            consumed_at = NULL,
                            locked_reason = NULL,
                            updated_at = ?
                        WHERE id = 1
                    """, (now_iso,))
                    conn.commit()
                    return True
                finally:
                    conn.close()
            except Exception as e:
                logger.critical("[SINGLE_ENTRY_MGR] Failed to reset for test: %s", e)
                return False
