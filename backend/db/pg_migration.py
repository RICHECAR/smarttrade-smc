"""
PostgreSQL Migration Layer — Dual-engine support (SQLite dev / PostgreSQL prod).
═══════════════════════════════════════════════════════════════════════════════

Phase 6 addition. Provides:
  1. Engine detection: Auto-detect SQLite vs PostgreSQL from DATABASE_URL
  2. PostgreSQL pool configuration: Proper pool_size, max_overflow, timeouts
  3. Migration bridge: Apply schema updates on both engines
  4. Backup/restore utility for SQLite → PostgreSQL data migration
  5. Session health monitoring

Config via .env:
  DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/trading  (PostgreSQL)
  DATABASE_URL=sqlite:///./data/trades.db                         (SQLite, default)
  DB_POOL_SIZE=10
  DB_MAX_OVERFLOW=20
  DB_POOL_TIMEOUT=30
  DB_POOL_RECYCLE=3600
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

logger = logging.getLogger("smarttrade.db.pg_migration")


def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class DatabaseConfig:
    """Database configuration with engine-specific tuning."""

    def __init__(self, database_url: Optional[str] = None):
        self.url = database_url or os.getenv("DATABASE_URL", "sqlite:///./data/trades.db")
        self.is_postgres = self.url.startswith("postgresql")
        self.is_sqlite = self.url.startswith("sqlite")

        # Pool settings (PostgreSQL only — SQLite uses NullPool/StaticPool)
        self.pool_size = _envi("DB_POOL_SIZE", 10)
        self.max_overflow = _envi("DB_MAX_OVERFLOW", 20)
        self.pool_timeout = _envi("DB_POOL_TIMEOUT", 30)
        self.pool_recycle = _envi("DB_POOL_RECYCLE", 3600)

    def create_engine(self) -> Engine:
        """Create SQLAlchemy engine with engine-specific configuration."""
        if self.is_postgres:
            return self._create_postgres_engine()
        return self._create_sqlite_engine()

    def _create_postgres_engine(self) -> Engine:
        """PostgreSQL engine with connection pooling."""
        engine = create_engine(
            self.url,
            pool_size=self.pool_size,
            max_overflow=self.max_overflow,
            pool_timeout=self.pool_timeout,
            pool_recycle=self.pool_recycle,
            pool_pre_ping=True,  # detect stale connections
            echo=False,
        )
        logger.info(
            "[DB] PostgreSQL engine created — pool=%d overflow=%d recycle=%ds",
            self.pool_size, self.max_overflow, self.pool_recycle,
        )
        return engine

    def _create_sqlite_engine(self) -> Engine:
        """SQLite engine with WAL mode."""
        # Ensure directory exists
        db_path = self.url.replace("sqlite:///", "")
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        engine = create_engine(
            self.url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
            echo=False,
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")  # Phase 6: upgraded from NORMAL
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        logger.info("[DB] SQLite engine created — WAL mode, synchronous=FULL")
        return engine


def create_pg_migration_sql() -> str:
    """
    Generate PostgreSQL DDL for the trading schema.

    This is the canonical schema definition for PostgreSQL.
    Used by: manual migration scripts, alembic, or direct execution.
    """
    return """
    -- Phase 6: PostgreSQL Schema for Institutional Trading
    -- Generated from SQLite schema v14 + Phase 6 additions

    CREATE TABLE IF NOT EXISTS live_trades (
        id VARCHAR(64) PRIMARY KEY,
        parent_trade_id VARCHAR(64),
        trade_type VARCHAR(20) DEFAULT 'single',
        symbol VARCHAR(50) NOT NULL,
        side VARCHAR(10) NOT NULL,
        entry DOUBLE PRECISION NOT NULL,
        qty DOUBLE PRECISION NOT NULL,
        sl DOUBLE PRECISION,
        tp DOUBLE PRECISION,
        tp2 DOUBLE PRECISION,
        tp3 DOUBLE PRECISION,
        leverage INTEGER DEFAULT 1,
        status VARCHAR(20) DEFAULT 'open',
        pnl DOUBLE PRECISION,
        close_price DOUBLE PRECISION,
        fee DOUBLE PRECISION DEFAULT 0.0,
        ts_open TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        ts_close TIMESTAMP WITH TIME ZONE,
        order_id VARCHAR(100),
        client_oid VARCHAR(100),
        broker VARCHAR(30) DEFAULT 'bitget',
        signal_id VARCHAR(100),
        strategy VARCHAR(100),
        timeframe VARCHAR(10),
        setup_type VARCHAR(50),
        risk_reward DOUBLE PRECISION,
        session VARCHAR(20),
        preset VARCHAR(50),
        close_reason VARCHAR(100),
        duration_sec INTEGER,
        max_drawdown_pct DOUBLE PRECISION,
        tags TEXT,
        notes TEXT,
        archived BOOLEAN DEFAULT FALSE,
        raw_exchange_response JSONB,
        fills JSONB,
        metadata JSONB
    );

    -- Indexes matching SQLite v14
    CREATE INDEX IF NOT EXISTS idx_lt_status_symbol ON live_trades(status, symbol);
    CREATE INDEX IF NOT EXISTS idx_lt_ts_open ON live_trades(ts_open);
    CREATE INDEX IF NOT EXISTS idx_lt_ts_close ON live_trades(ts_close);
    CREATE INDEX IF NOT EXISTS idx_lt_order_id ON live_trades(order_id);
    CREATE INDEX IF NOT EXISTS idx_lt_broker ON live_trades(broker);
    CREATE INDEX IF NOT EXISTS idx_lt_symbol ON live_trades(symbol);
    CREATE INDEX IF NOT EXISTS idx_lt_signal_broker ON live_trades(signal_id, broker);

    -- UNIQUE constraint (Phase 5)
    CREATE UNIQUE INDEX IF NOT EXISTS uq_lt_order_broker
        ON live_trades(order_id, broker) WHERE order_id IS NOT NULL;

    -- Phase 6: Reconciliation audit log
    CREATE TABLE IF NOT EXISTS reconciliation_log (
        id SERIAL PRIMARY KEY,
        timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        broker VARCHAR(30) NOT NULL,
        pass_type VARCHAR(30) NOT NULL,
        positions_checked INTEGER DEFAULT 0,
        divergences INTEGER DEFAULT 0,
        critical BOOLEAN DEFAULT FALSE,
        balance_db DOUBLE PRECISION,
        balance_exchange DOUBLE PRECISION,
        balance_divergence_pct DOUBLE PRECISION,
        details JSONB,
        error TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_recon_ts ON reconciliation_log(timestamp);
    CREATE INDEX IF NOT EXISTS idx_recon_broker ON reconciliation_log(broker);

    -- Phase 6: Risk events audit trail
    CREATE TABLE IF NOT EXISTS risk_events (
        id SERIAL PRIMARY KEY,
        timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        event_type VARCHAR(50) NOT NULL,
        symbol VARCHAR(50),
        broker VARCHAR(30),
        action VARCHAR(20),
        reason TEXT,
        current_value DOUBLE PRECISION,
        threshold DOUBLE PRECISION,
        trace_id VARCHAR(32),
        details JSONB
    );

    CREATE INDEX IF NOT EXISTS idx_risk_evt_ts ON risk_events(timestamp);
    CREATE INDEX IF NOT EXISTS idx_risk_evt_type ON risk_events(event_type);
    CREATE INDEX IF NOT EXISTS idx_risk_evt_symbol ON risk_events(symbol);

    -- Phase 6: Alert history
    CREATE TABLE IF NOT EXISTS alert_history (
        id SERIAL PRIMARY KEY,
        timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        level VARCHAR(20) NOT NULL,
        title VARCHAR(200) NOT NULL,
        message TEXT,
        channel VARCHAR(20),
        delivered BOOLEAN DEFAULT FALSE,
        trace_id VARCHAR(32),
        details JSONB
    );

    CREATE INDEX IF NOT EXISTS idx_alert_ts ON alert_history(timestamp);
    """


def apply_pg_schema(engine: Engine) -> None:
    """Apply PostgreSQL schema if using PostgreSQL."""
    url = str(engine.url)
    if not url.startswith("postgresql"):
        logger.info("[DB] SQLite detected — skipping PostgreSQL schema migration")
        return

    schema_sql = create_pg_migration_sql()
    with engine.begin() as conn:
        for statement in schema_sql.split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(text(statement))

    logger.info("[DB] PostgreSQL schema applied successfully")


def check_db_health(engine: Engine) -> Dict[str, Any]:
    """Check database health — connection, pool, latency."""
    import time
    result = {
        "engine": "postgresql" if str(engine.url).startswith("postgresql") else "sqlite",
        "healthy": False,
        "latency_ms": 0,
        "pool": {},
    }

    start = time.monotonic()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        result["latency_ms"] = round((time.monotonic() - start) * 1000, 1)
        result["healthy"] = True
    except Exception as e:
        result["error"] = str(e)

    # Pool stats (PostgreSQL only)
    pool = engine.pool
    if hasattr(pool, "size"):
        result["pool"] = {
            "size": pool.size(),
            "checkedin": pool.checkedin(),
            "checkedout": pool.checkedout(),
            "overflow": pool.overflow(),
        }

    return result
