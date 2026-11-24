"""
SQLite Schema Migrations for Live Trades Database

Handles schema versioning and auto-migration on startup.
Uses PRAGMA user_version for version tracking (no extra table needed).

Approach:
- Schema version stored in SQLite's pragma user_version (integer 0+)
- Each migration is a function migrate_vX_to_vY()
- On startup: compare current vs expected, apply pending migrations
- Migrations are idempotent (check if column exists before ADD)

Author: SMC Trading Bot Framework
"""

import logging
import sqlite3
from typing import List, Tuple, Optional, Callable
from pathlib import Path

from core.env_loader import is_test_mode

logger = logging.getLogger(__name__)

# Current schema version - increment when adding new migrations
CURRENT_SCHEMA_VERSION = 14


def _is_in_memory_sqlite_url(database_url: str) -> bool:
    """Return True if database_url points strictly to an in-memory SQLite database."""
    from backend.db.session import _is_sqlite_memory_url
    return _is_sqlite_memory_url(database_url)


def _assert_migration_target_allowed(database_url_or_path: str) -> None:
    """Prevent accidental test migrations against unauthorized or production SQLite files."""
    from backend.db.session import assert_database_url_allowed
    url = (
        database_url_or_path
        if "://" in database_url_or_path or database_url_or_path.lower().startswith("file:")
        else f"sqlite:///{database_url_or_path}"
    )
    assert_database_url_allowed(url, caller_context="migrations")

# ═══════════════════════════════════════════════════════════════════════════════
# SCHEMA INSPECTION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_db_path_from_url(database_url: str) -> str:
    """Extract file path from SQLite URL."""
    if is_test_mode():
        _assert_migration_target_allowed(database_url)
    if _is_in_memory_sqlite_url(database_url):
        return ":memory:"
    if database_url.startswith("sqlite:///"):
        path = database_url.replace("sqlite:///", "").split("?", 1)[0]
        # Handle relative paths (./data/trades.db)
        if path.startswith("./"):
            path = path[2:]
        return path
    raise ValueError(f"Not a SQLite URL: {database_url}")


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Get current schema version from PRAGMA user_version."""
    cursor = conn.execute("PRAGMA user_version")
    return cursor.fetchone()[0]


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Set schema version in PRAGMA user_version."""
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Check if a table exists in the database."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cursor.fetchone() is not None


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    return column_name in columns


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    """Get list of column names for a table."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    """Check if an index exists."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,)
    )
    return cursor.fetchone() is not None


def add_column_if_not_exists(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_def: str,
) -> bool:
    """
    Add a column to a table if it doesn't exist.
    
    Args:
        conn: SQLite connection
        table_name: Target table
        column_name: New column name
        column_def: Column definition (e.g., "TEXT", "TEXT DEFAULT 'kraken'")
    
    Returns:
        True if column was added, False if already existed
    """
    if column_exists(conn, table_name, column_name):
        logger.debug(f"[MIGRATION] Column {table_name}.{column_name} already exists")
        return False
    
    sql = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}"
    conn.execute(sql)
    conn.commit()
    logger.info(f"[MIGRATION] Added column {table_name}.{column_name}")
    return True


def create_index_if_not_exists(
    conn: sqlite3.Connection,
    index_name: str,
    table_name: str,
    columns: List[str],
) -> bool:
    """Create an index if it doesn't exist."""
    if index_exists(conn, index_name):
        logger.debug(f"[MIGRATION] Index {index_name} already exists")
        return False
    
    cols = ", ".join(columns)
    sql = f"CREATE INDEX {index_name} ON {table_name} ({cols})"
    conn.execute(sql)
    conn.commit()
    logger.info(f"[MIGRATION] Created index {index_name}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# MIGRATION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def migrate_v0_to_v1(conn: sqlite3.Connection) -> None:
    """
    Migration v0 → v1: Add broker column to live_trades.
    
    This handles databases created before multi-broker support.
    """
    logger.info("[MIGRATION] Applying migration v0 → v1: Add broker column")
    
    if not table_exists(conn, "live_trades"):
        logger.info("[MIGRATION] live_trades table doesn't exist yet, skipping")
        return
    
    # Add broker column with default 'kraken' for existing trades
    add_column_if_not_exists(
        conn, "live_trades", "broker", "TEXT DEFAULT 'kraken'"
    )
    
    # Create index on broker for filtering
    create_index_if_not_exists(
        conn, "idx_live_trades_broker", "live_trades", ["broker"]
    )
    
    logger.info("[MIGRATION] Migration v0 → v1 complete")


def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """
    Migration v1 → v2: Add parent_trade_id column to live_trades.
    
    This handles the mirror trading feature linking related trades.
    """
    logger.info("[MIGRATION] Applying migration v1 → v2: Add parent_trade_id column")
    
    if not table_exists(conn, "live_trades"):
        logger.info("[MIGRATION] live_trades table doesn't exist yet, skipping")
        return
    
    # Add parent_trade_id column (nullable, no default)
    add_column_if_not_exists(
        conn, "live_trades", "parent_trade_id", "TEXT"
    )
    
    # Create index for efficient parent lookups
    create_index_if_not_exists(
        conn, "idx_live_trades_parent_id", "live_trades", ["parent_trade_id"]
    )
    
    logger.info("[MIGRATION] Migration v1 → v2 complete")


def migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """
    Migration v2 → v3: Add multi-broker columns to trades table.
    
    This adds broker identification and signal linking to the main trades table
    for full multi-broker support (both auto and manual trades).
    
    New columns:
      - broker: Identifies which broker executed the trade (kraken/...)
      - broker_order_id: The order ID on the broker platform
      - signal_id: Links trades executed from the same signal across brokers
    """
    logger.info("[MIGRATION] Applying migration v2 → v3: Add multi-broker columns to trades")
    
    # Update trades table if it exists
    if table_exists(conn, "trades"):
        # Add broker column with default 'kraken' for existing trades
        add_column_if_not_exists(
            conn, "trades", "broker", "TEXT DEFAULT 'kraken'"
        )
        
        # Add broker_order_id column (nullable)
        add_column_if_not_exists(
            conn, "trades", "broker_order_id", "TEXT"
        )
        
        # Add signal_id column for linking multi-broker trades
        add_column_if_not_exists(
            conn, "trades", "signal_id", "TEXT"
        )
        
        # Create indexes for efficient filtering
        create_index_if_not_exists(
            conn, "idx_trades_broker", "trades", ["broker"]
        )
        create_index_if_not_exists(
            conn, "idx_trades_broker_order_id", "trades", ["broker_order_id"]
        )
        create_index_if_not_exists(
            conn, "idx_trades_signal_id", "trades", ["signal_id"]
        )
        
        logger.info("[MIGRATION] Updated trades table with multi-broker columns")
    else:
        logger.info("[MIGRATION] trades table doesn't exist yet, skipping")
    
    # Also ensure live_trades has signal_id if missing
    if table_exists(conn, "live_trades"):
        add_column_if_not_exists(
            conn, "live_trades", "signal_id", "TEXT"
        )
        create_index_if_not_exists(
            conn, "idx_live_trades_signal_id", "live_trades", ["signal_id"]
        )
    
    logger.info("[MIGRATION] Migration v2 → v3 complete")

def migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """
    Migration v3 → v4: Add error_message column to live_trades.
    
    This allows storing error details for failed mirror trades directly in the DB.
    """
    logger.info("[MIGRATION] Applying migration v3 → v4: Add error_message column")
    
    if not table_exists(conn, "live_trades"):
        logger.info("[MIGRATION] live_trades table doesn't exist yet, skipping")
        return
    
    # Add error_message column (nullable, no default)
    add_column_if_not_exists(
        conn, "live_trades", "error_message", "TEXT"
    )
    
    logger.info("[MIGRATION] Migration v3 → v4 complete")


def migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """
    Migration v4 to v5:
    - Add error_message column to persisted_positions table for UI display.
    """
    logger.info("[MIGRATION] Applying migration v4 → v5: Add error_message column to persisted_positions")
    try:
        # Check if table exists before altering
        if table_exists(conn, "persisted_positions"):
            add_column_if_not_exists(conn, "persisted_positions", "error_message", "TEXT")
        else:
            logger.info("[MIGRATION] Table persisted_positions does not exist yet, skipping alter.")
    except Exception as e:
        logger.error(f"[MIGRATION] Failed v4 to v5 migration: {e}")
        raise


def migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """
    Migration v5 to v6:
    - Create trade_orders table for multi-broker trade execution and persistence.
    """
    logger.info("[MIGRATION] Applying migration v5 → v6: Create trade_orders table")
    try:
        sql = """
        CREATE TABLE IF NOT EXISTS trade_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT UNIQUE NOT NULL,
            signal_id TEXT,
            broker TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            entry REAL,
            sl REAL,
            tp REAL,
            rr REAL,
            status TEXT NOT NULL,
            order_id TEXT,
            error TEXT,
            fills TEXT,
            pnl REAL,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
        conn.execute(sql)
        create_index_if_not_exists(conn, "idx_trade_orders_signal_id", "trade_orders", ["signal_id"])
        create_index_if_not_exists(conn, "idx_trade_orders_broker", "trade_orders", ["broker"])
        create_index_if_not_exists(conn, "idx_trade_orders_status", "trade_orders", ["status"])
        conn.commit()
    except Exception as e:
        logger.error(f"[MIGRATION] Failed v5 to v6 migration: {e}")
        raise


def migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """
    Migration v6 to v7:
    - Add close_reason and client_oid to live_trades to track external closes reliably.
    """
    logger.info("[MIGRATION] Applying migration v6 → v7: Add close_reason and client_oid to live_trades")
    try:
        if table_exists(conn, "live_trades"):
            add_column_if_not_exists(conn, "live_trades", "close_reason", "TEXT")
            add_column_if_not_exists(conn, "live_trades", "client_oid", "TEXT")
        else:
            logger.info("[MIGRATION] Table live_trades does not exist yet, skipping alter.")
    except Exception as e:
        logger.error(f"[MIGRATION] Failed v6 to v7 migration: {e}")
        raise


def migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    """
    Migration v7 to v8:
    - Add archived and archive_reason columns to live_trades and trades tables.
    """
    logger.info("[MIGRATION] Applying migration v7 → v8: Add archived and archive_reason")
    try:
        # Update live_trades
        if table_exists(conn, "live_trades"):
            add_column_if_not_exists(conn, "live_trades", "archived", "BOOLEAN DEFAULT 0")
            add_column_if_not_exists(conn, "live_trades", "archive_reason", "TEXT")
            create_index_if_not_exists(conn, "idx_live_trades_archived", "live_trades", ["archived"])
        else:
            logger.info("[MIGRATION] Table live_trades does not exist yet, skipping alter.")
            
        # Update trades
        if table_exists(conn, "trades"):
            add_column_if_not_exists(conn, "trades", "archived", "BOOLEAN DEFAULT 0")
            add_column_if_not_exists(conn, "trades", "archive_reason", "TEXT")
            create_index_if_not_exists(conn, "idx_trades_archived", "trades", ["archived"])
        else:
            logger.info("[MIGRATION] Table trades does not exist yet, skipping alter.")
            
    except Exception as e:
        logger.error(f"[MIGRATION] Failed v7 to v8 migration: {e}")
        raise


def migrate_v8_to_v9(conn: sqlite3.Connection) -> None:
    """
    Migration v8 to v9:
    - Add archived and archive_reason columns to persisted_positions table.
    """
    logger.info("[MIGRATION] Applying migration v8 → v9: Add archived and archive_reason to persisted_positions")
    try:
        if table_exists(conn, "persisted_positions"):
            add_column_if_not_exists(conn, "persisted_positions", "archived", "INTEGER DEFAULT 0")
            add_column_if_not_exists(conn, "persisted_positions", "archive_reason", "TEXT")
            create_index_if_not_exists(conn, "idx_persisted_positions_archived", "persisted_positions", ["archived"])
        else:
            logger.info("[MIGRATION] Table persisted_positions does not exist yet, skipping alter.")
            
    except Exception as e:
        logger.error(f"[MIGRATION] Failed v8 to v9 migration: {e}")
        raise


def migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    """
    Migration v9 → v10: Analytics Pipeline Tables
    Creates 5 new tables for the data-driven analytics architecture.
    All tables are idempotent (CREATE IF NOT EXISTS).
    """
    logger.info("[MIGRATION] Applying migration v9 → v10: Analytics Pipeline Tables")

    # ── Table 1: system_state_snapshots ─────────────────────────────
    if not table_exists(conn, "system_state_snapshots"):
        conn.execute("""
            CREATE TABLE system_state_snapshots (
                id TEXT PRIMARY KEY,
                ts TIMESTAMP NOT NULL,
                regime TEXT NOT NULL,
                regime_confidence REAL,
                risk_factor REAL NOT NULL DEFAULT 1.0,
                adaptive_weight REAL,
                capital_utilization REAL,
                mode TEXT NOT NULL DEFAULT 'LIVE',
                kill_switch_active INTEGER NOT NULL DEFAULT 0,
                volatility_pause_active INTEGER NOT NULL DEFAULT 0,
                recovery_step INTEGER NOT NULL DEFAULT 0,
                open_positions INTEGER DEFAULT 0,
                daily_pnl REAL DEFAULT 0.0,
                equity REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sss_ts_regime ON system_state_snapshots(ts, regime)")
        logger.info("[MIGRATION] Created table: system_state_snapshots")

    # ── Table 2: signal_decisions ──────────────────────────────────
    if not table_exists(conn, "signal_decisions"):
        conn.execute("""
            CREATE TABLE signal_decisions (
                id TEXT PRIMARY KEY,
                ts TIMESTAMP NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT,
                strategy TEXT,
                side TEXT,
                regime TEXT,
                regime_confidence REAL,
                decision TEXT NOT NULL,
                reason TEXT,
                rr_estimated REAL,
                edge_score REAL,
                risk_pct_requested REAL,
                risk_pct_allocated REAL,
                execution_mode TEXT,
                broker TEXT,
                fallback_used INTEGER NOT NULL DEFAULT 0,
                trade_id TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sd_ts ON signal_decisions(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sd_symbol ON signal_decisions(symbol)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sd_ts_symbol ON signal_decisions(ts, symbol)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sd_decision ON signal_decisions(decision)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sd_trade_id ON signal_decisions(trade_id)")
        logger.info("[MIGRATION] Created table: signal_decisions")

    # ── Table 3: trade_analytics ───────────────────────────────────
    if not table_exists(conn, "trade_analytics"):
        conn.execute("""
            CREATE TABLE trade_analytics (
                trade_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                size REAL,
                fees REAL DEFAULT 0.0,
                sl_price REAL,
                tp_price REAL,
                pnl REAL,
                mae REAL,
                mfe REAL,
                duration_sec INTEGER,
                execution_mode TEXT,
                broker TEXT,
                adaptive_version TEXT,
                regime_at_entry TEXT,
                regime_at_exit TEXT,
                ts_entry TIMESTAMP,
                ts_exit TIMESTAMP,
                strategy TEXT,
                edge_score_at_entry REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ta_symbol_ts ON trade_analytics(symbol, ts_entry)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ta_regime ON trade_analytics(regime_at_entry)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ta_broker ON trade_analytics(broker)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ta_ts_entry ON trade_analytics(ts_entry)")
        logger.info("[MIGRATION] Created table: trade_analytics")

    # ── Table 4: risk_events ───────────────────────────────────────
    if not table_exists(conn, "risk_events"):
        conn.execute("""
            CREATE TABLE risk_events (
                id TEXT PRIMARY KEY,
                ts TIMESTAMP NOT NULL,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'INFO',
                detail TEXT,
                symbol TEXT,
                trade_id TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_re_ts ON risk_events(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_re_type_ts ON risk_events(event_type, ts)")
        logger.info("[MIGRATION] Created table: risk_events")

    # ── Table 5: daily_analytics ───────────────────────────────────
    if not table_exists(conn, "daily_analytics"):
        conn.execute("""
            CREATE TABLE daily_analytics (
                date DATE PRIMARY KEY,
                pnl REAL NOT NULL DEFAULT 0.0,
                profit_factor REAL,
                winrate REAL,
                expectancy REAL,
                max_drawdown REAL,
                total_trades INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                avg_edge_score REAL,
                capital_efficiency REAL,
                avg_rr REAL,
                maker_count INTEGER NOT NULL DEFAULT 0,
                taker_count INTEGER NOT NULL DEFAULT 0,
                fallback_count INTEGER NOT NULL DEFAULT 0,
                avg_mae REAL,
                avg_mfe REAL,
                regime_breakdown TEXT,
                broker_breakdown TEXT,
                signals_total INTEGER DEFAULT 0,
                signals_accepted INTEGER DEFAULT 0,
                signals_rejected INTEGER DEFAULT 0,
                top_reject_reasons TEXT
            )
        """)
        logger.info("[MIGRATION] Created table: daily_analytics")

    # ── Table 6: weekly_analytics ──────────────────────────────────
    if not table_exists(conn, "weekly_analytics"):
        conn.execute("""
            CREATE TABLE weekly_analytics (
                week_key TEXT PRIMARY KEY,
                week_start DATE NOT NULL,
                pnl REAL NOT NULL DEFAULT 0.0,
                profit_factor REAL,
                winrate REAL,
                expectancy REAL,
                max_drawdown REAL,
                total_trades INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                avg_edge_score REAL,
                capital_efficiency REAL,
                regime_breakdown TEXT,
                broker_breakdown TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_week_start ON weekly_analytics(week_start)")
        logger.info("[MIGRATION] Created table: weekly_analytics")

    conn.commit()
    logger.info("[MIGRATION] v9 → v10 complete: 6 analytics tables created")


# ═══════════════════════════════════════════════════════════════════════════════
# MIGRATION v10 → v11: execution_attempts + ops_events tables, daily_analytics update
# ═══════════════════════════════════════════════════════════════════════════════

def migrate_v10_to_v11(conn: sqlite3.Connection) -> None:
    """
    v10 → v11:
    - Create execution_attempts table
    - Create ops_events table
    - Add execution_blocked columns to daily_analytics
    """
    logger.info("[MIGRATION] Starting v10 → v11: execution_attempts, ops_events, daily_analytics update")

    # 1. execution_attempts
    if not table_exists(conn, "execution_attempts"):
        conn.execute("""
            CREATE TABLE execution_attempts (
                id TEXT PRIMARY KEY,
                ts DATETIME NOT NULL DEFAULT (datetime('now')),
                symbol TEXT NOT NULL,
                signal_id TEXT,
                side TEXT,
                broker TEXT,
                is_multi_broker BOOLEAN NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                block_reason TEXT,
                block_details TEXT,
                entry_price REAL,
                quantity REAL,
                account_balance REAL,
                regime TEXT,
                order_id TEXT,
                fill_price REAL,
                latency_ms INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ea_ts ON execution_attempts(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ea_ts_status ON execution_attempts(ts, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ea_symbol_ts ON execution_attempts(symbol, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ea_block_reason ON execution_attempts(block_reason)")
        logger.info("[MIGRATION] Created execution_attempts table")

    # 2. ops_events
    if not table_exists(conn, "ops_events"):
        conn.execute("""
            CREATE TABLE ops_events (
                id TEXT PRIMARY KEY,
                ts DATETIME NOT NULL DEFAULT (datetime('now')),
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'INFO',
                source TEXT,
                message TEXT,
                detail TEXT,
                symbol TEXT,
                broker TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_oe_ts ON ops_events(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_oe_type_ts ON ops_events(event_type, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_oe_severity ON ops_events(severity)")
        logger.info("[MIGRATION] Created ops_events table")

    # 3. Add execution_blocked columns to daily_analytics
    if table_exists(conn, "daily_analytics"):
        add_column_if_not_exists(conn, "daily_analytics", "execution_blocked_total", "INTEGER DEFAULT 0")
        add_column_if_not_exists(conn, "daily_analytics", "execution_blocked_by_reason", "TEXT")
        add_column_if_not_exists(conn, "daily_analytics", "execution_dispatch_total", "INTEGER DEFAULT 0")
        add_column_if_not_exists(conn, "daily_analytics", "execution_success_total", "INTEGER DEFAULT 0")
        logger.info("[MIGRATION] Updated daily_analytics with execution_blocked columns")

    conn.commit()
    logger.info("[MIGRATION] v10 → v11 complete: execution_attempts + ops_events + daily_analytics updated")


def migrate_v11_to_v12(conn: sqlite3.Connection) -> None:
    """
    v11 → v12:
    - Add funding column to live_trades for funding rate tracking
    """
    logger.info("[MIGRATION] Starting v11 → v12: add funding column to live_trades")

    if table_exists(conn, "live_trades"):
        add_column_if_not_exists(conn, "live_trades", "funding", "REAL DEFAULT 0.0")
        logger.info("[MIGRATION] Added funding column to live_trades")

    conn.commit()
    logger.info("[MIGRATION] v11 → v12 complete: funding column added")


def migrate_v12_to_v13(conn: sqlite3.Connection) -> None:
    """
    v12 → v13:
    - Add index on live_trades.ts_close (all stats queries now filter on ts_close)
    """
    logger.info("[MIGRATION] Starting v12 → v13: add ts_close index")

    if table_exists(conn, "live_trades"):
        create_index_if_not_exists(
            conn, "idx_live_trades_ts_close", "live_trades", ["ts_close"],
        )
        logger.info("[MIGRATION] Added idx_live_trades_ts_close index")

    conn.commit()
    logger.info("[MIGRATION] v12 → v13 complete: ts_close index added")


def migrate_v13_to_v14(conn: sqlite3.Connection) -> None:
    """
    v13 → v14:
    - Add UNIQUE constraint on (order_id, broker) to prevent duplicate trades
    - Add index on (signal_id, broker) for dedup queries
    
    CRITICAL: This prevents the same exchange order from being recorded twice,
    which would cause double PnL accounting and phantom positions.
    """
    logger.info("[MIGRATION] Starting v13 → v14: unique order_id+broker, signal dedup index")

    if table_exists(conn, "live_trades"):
        # Check for existing duplicates before adding constraint
        cursor = conn.execute(
            "SELECT order_id, broker, COUNT(*) as cnt "
            "FROM live_trades GROUP BY order_id, broker HAVING cnt > 1"
        )
        duplicates = cursor.fetchall()
        if duplicates:
            logger.warning(
                "[MIGRATION] Found %d duplicate order_id+broker pairs — "
                "archiving older duplicates before adding UNIQUE constraint",
                len(duplicates),
            )
            for order_id, broker, cnt in duplicates:
                # Keep the newest, archive the rest
                conn.execute(
                    "UPDATE live_trades SET archived = 1, archive_reason = 'migration_v14_dedup' "
                    "WHERE order_id = ? AND broker = ? AND id NOT IN ("
                    "  SELECT id FROM live_trades WHERE order_id = ? AND broker = ? "
                    "  ORDER BY ts_open DESC LIMIT 1"
                    ")",
                    (order_id, broker, order_id, broker),
                )
                logger.info(
                    "[MIGRATION] Archived %d duplicate(s) for order_id=%s broker=%s",
                    cnt - 1, order_id, broker,
                )

        # Create unique index (acts as UNIQUE constraint in SQLite)
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_live_trades_order_broker "
                "ON live_trades (order_id, broker)"
            )
            logger.info("[MIGRATION] Created UNIQUE index uq_live_trades_order_broker")
        except Exception as e:
            logger.error("[MIGRATION] Failed to create unique index: %s", e)

        # Signal dedup index
        create_index_if_not_exists(
            conn, "idx_live_trades_signal_broker", "live_trades", ["signal_id", "broker"],
        )
        logger.info("[MIGRATION] Added idx_live_trades_signal_broker index")

    conn.commit()
    logger.info("[MIGRATION] v13 → v14 complete: unique constraints added")


# Registry of migrations: (from_version, to_version, function)
MIGRATIONS: List[Tuple[int, int, Callable[[sqlite3.Connection], None]]] = [
    (0, 1, migrate_v0_to_v1),
    (1, 2, migrate_v1_to_v2),
    (2, 3, migrate_v2_to_v3),
    (3, 4, migrate_v3_to_v4),
    (4, 5, migrate_v4_to_v5),
    (5, 6, migrate_v5_to_v6),
    (6, 7, migrate_v6_to_v7),
    (7, 8, migrate_v7_to_v8),
    (8, 9, migrate_v8_to_v9),
    (9, 10, migrate_v9_to_v10),
    (10, 11, migrate_v10_to_v11),
    (11, 12, migrate_v11_to_v12),
    (12, 13, migrate_v12_to_v13),
    (13, 14, migrate_v13_to_v14),
]


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN MIGRATION ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def ensure_schema_up_to_date(database_url: str) -> Tuple[int, int]:
    """
    Ensure the database schema is up-to-date.
    
    Applies any pending migrations to bring the schema to CURRENT_SCHEMA_VERSION.
    Should be called BEFORE any SQLAlchemy operations that reference new columns.
    
    Args:
        database_url: SQLite connection URL (e.g., "sqlite:///./data/trades.db")
    
    Returns:
        Tuple of (starting_version, final_version)
    
    Raises:
        RuntimeError: If migration fails critically
    """
    if is_test_mode():
        _assert_migration_target_allowed(database_url)

    if not database_url.startswith("sqlite"):
        logger.warning("[MIGRATION] Non-SQLite database, skipping migrations")
        return (0, 0)

    if _is_in_memory_sqlite_url(database_url):
        logger.debug("[MIGRATION] In-memory SQLite detected, skipping file migrations")
        return (CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION)
    
    try:
        _assert_migration_target_allowed(database_url)
        db_path = get_db_path_from_url(database_url)
        _assert_migration_target_allowed(db_path)

        # Ensure directory exists ONLY after authorization succeeds
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        
        conn = sqlite3.connect(db_path)
        
        try:
            current_version = get_schema_version(conn)
            target_version = CURRENT_SCHEMA_VERSION
            
            logger.info(
                f"[MIGRATION] Schema version: current={current_version}, target={target_version}"
            )
            
            if current_version >= target_version:
                logger.info("[MIGRATION] Schema is up-to-date")
                return (current_version, current_version)
            
            # Apply pending migrations
            for from_ver, to_ver, migrate_fn in MIGRATIONS:
                if from_ver >= current_version and to_ver <= target_version:
                    if current_version <= from_ver:
                        try:
                            migrate_fn(conn)
                            set_schema_version(conn, to_ver)
                            logger.info(f"[MIGRATION] Applied migration v{from_ver} → v{to_ver}")
                        except Exception as e:
                            logger.error(f"[MIGRATION] Failed v{from_ver} → v{to_ver}: {e}")
                            raise RuntimeError(f"Migration v{from_ver} → v{to_ver} failed: {e}")
            
            # Verify final version
            final_version = get_schema_version(conn)
            logger.info(f"[MIGRATION] Migrations complete: v{current_version} → v{final_version}")
            
            return (current_version, final_version)
            
        finally:
            conn.close()
            
    except Exception as e:
        logger.error(f"[MIGRATION] Schema migration failed: {e}", exc_info=True)
        raise


def ensure_all_columns_exist(database_url: str) -> bool:
    """
    Ensure all required columns exist in live_trades table.
    
    This is a safety check that runs independently of versioning.
    Useful for manual recovery or when version tracking gets out of sync.
    
    Returns:
        True if any columns were added, False if all existed
    """
    if is_test_mode():
        _assert_migration_target_allowed(database_url)

    if not database_url.startswith("sqlite"):
        return False

    if _is_in_memory_sqlite_url(database_url):
        return False
    
    required_columns = {
        "broker": "TEXT DEFAULT 'kraken'",
        "parent_trade_id": "TEXT",
        "signal_id": "TEXT",
        "error_message": "TEXT",
        "close_reason": "TEXT",
        "client_oid": "TEXT",
        "funding": "REAL DEFAULT 0.0",
    }
    
    try:
        db_path = get_db_path_from_url(database_url)
        _assert_migration_target_allowed(db_path)
        conn = sqlite3.connect(db_path)
        
        try:
            if not table_exists(conn, "live_trades"):
                return False
            
            any_added = False
            for col_name, col_def in required_columns.items():
                if add_column_if_not_exists(conn, "live_trades", col_name, col_def):
                    any_added = True
            
            return any_added
            
        finally:
            conn.close()
            
    except Exception as e:
        logger.error(f"[MIGRATION] ensure_all_columns_exist failed: {e}")
        return False


def get_live_trades_schema_info(database_url: str) -> dict:
    """
    Get detailed schema information for live_trades table.
    
    Useful for debugging and the CLI tool.
    
    Returns:
        Dict with version, columns, indexes, and row count
    """
    if is_test_mode():
        _assert_migration_target_allowed(database_url)
    if not database_url.startswith("sqlite"):
        return {"error": "Not a SQLite database"}
    
    try:
        db_path = get_db_path_from_url(database_url)
        conn = sqlite3.connect(db_path)
        
        try:
            result = {
                "db_path": db_path,
                "schema_version": get_schema_version(conn),
                "target_version": CURRENT_SCHEMA_VERSION,
                "needs_migration": get_schema_version(conn) < CURRENT_SCHEMA_VERSION,
            }
            
            if table_exists(conn, "live_trades"):
                # Get columns
                cursor = conn.execute("PRAGMA table_info(live_trades)")
                result["columns"] = [
                    {"name": row[1], "type": row[2], "nullable": not row[3], "default": row[4]}
                    for row in cursor.fetchall()
                ]
                
                # Get indexes
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='live_trades'"
                )
                result["indexes"] = [row[0] for row in cursor.fetchall()]
                
                # Get row count
                cursor = conn.execute("SELECT COUNT(*) FROM live_trades")
                result["row_count"] = cursor.fetchone()[0]
                
                # Get open trade count
                cursor = conn.execute("SELECT COUNT(*) FROM live_trades WHERE status='OPEN'")
                result["open_trades"] = cursor.fetchone()[0]
            else:
                result["table_exists"] = False
            
            return result
            
        finally:
            conn.close()
            
    except Exception as e:
        return {"error": str(e)}
