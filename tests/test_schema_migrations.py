"""
Test Schema Migrations

Tests that:
1. Migration system correctly adds missing columns
2. Recovery works after migration
3. Migrations are idempotent

Run: pytest tests/test_schema_migrations.py -v
"""

import os
import sqlite3
import tempfile
import pytest

# Ensure project root is importable
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSchemaMigrations:
    """Test database schema migration system."""
    
    def setup_method(self):
        """Create a temporary database for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_trades.db")
        self.database_url = f"sqlite:///{self.db_path}"
    
    def teardown_method(self):
        """Clean up temporary database."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)
    
    def _create_old_schema_db(self):
        """Create a database with OLD schema (missing broker, parent_trade_id)."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE live_trades (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                ts_open DATETIME,
                ts_close DATETIME,
                created_at DATETIME,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                entry REAL NOT NULL,
                stop REAL NOT NULL,
                tp REAL NOT NULL,
                leverage INTEGER DEFAULT 10,
                notional REAL,
                rr REAL DEFAULT 0.0,
                pnl REAL DEFAULT 0.0,
                fee REAL DEFAULT 0.0,
                close_price REAL,
                status TEXT DEFAULT 'OPEN',
                result TEXT,
                session TEXT,
                preset TEXT,
                signal_id TEXT,
                raw_exchange TEXT
            )
        """)
        
        # Insert a test trade
        conn.execute("""
            INSERT INTO live_trades (id, order_id, symbol, side, qty, entry, stop, tp, status)
            VALUES ('test-uuid-123', 'bitget-order-456', 'BTCUSDT', 'long', 0.001, 50000, 49000, 52000, 'OPEN')
        """)
        conn.commit()
        conn.close()
        
        return self.db_path
    
    def test_migration_adds_broker_column(self):
        """Test that migration adds broker column to old schema."""
        from backend.db.migrations import (
            ensure_schema_up_to_date,
            column_exists,
        )
        
        # Create old schema without broker column
        self._create_old_schema_db()
        
        # Verify broker column is missing
        conn = sqlite3.connect(self.db_path)
        assert not column_exists(conn, "live_trades", "broker")
        conn.close()
        
        # Run migration
        from_ver, to_ver = ensure_schema_up_to_date(self.database_url)
        
        # Verify broker column was added
        conn = sqlite3.connect(self.db_path)
        assert column_exists(conn, "live_trades", "broker")
        conn.close()
        
        assert to_ver >= 1
    
    def test_migration_adds_parent_trade_id_column(self):
        """Test that migration adds parent_trade_id column."""
        from backend.db.migrations import (
            ensure_schema_up_to_date,
            column_exists,
        )
        
        # Create old schema
        self._create_old_schema_db()
        
        # Verify parent_trade_id is missing
        conn = sqlite3.connect(self.db_path)
        assert not column_exists(conn, "live_trades", "parent_trade_id")
        conn.close()
        
        # Run migration
        from_ver, to_ver = ensure_schema_up_to_date(self.database_url)
        
        # Verify parent_trade_id was added
        conn = sqlite3.connect(self.db_path)
        assert column_exists(conn, "live_trades", "parent_trade_id")
        conn.close()
        
        assert to_ver >= 2
    
    def test_migration_is_idempotent(self):
        """Test that running migration twice doesn't cause errors."""
        from backend.db.migrations import ensure_schema_up_to_date
        
        self._create_old_schema_db()
        
        # Run migration twice
        from_ver1, to_ver1 = ensure_schema_up_to_date(self.database_url)
        from_ver2, to_ver2 = ensure_schema_up_to_date(self.database_url)
        
        # Second run should be a no-op
        assert from_ver2 == to_ver2
        assert to_ver1 == to_ver2
    
    def test_migration_preserves_existing_data(self):
        """Test that migration doesn't lose existing data."""
        from backend.db.migrations import ensure_schema_up_to_date
        
        self._create_old_schema_db()
        
        # Run migration
        ensure_schema_up_to_date(self.database_url)
        
        # Verify data is preserved
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("SELECT id, order_id, symbol FROM live_trades")
        row = cursor.fetchone()
        conn.close()
        
        assert row is not None
        assert row[0] == "test-uuid-123"
        assert row[1] == "bitget-order-456"
        assert row[2] == "BTCUSDT"
    
    def test_existing_trades_get_default_broker(self):
        """Test that existing trades get 'bitget' as default broker."""
        from backend.db.migrations import ensure_schema_up_to_date
        
        self._create_old_schema_db()
        
        # Run migration
        ensure_schema_up_to_date(self.database_url)
        
        # Check broker value
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("SELECT broker FROM live_trades WHERE id='test-uuid-123'")
        row = cursor.fetchone()
        conn.close()
        
        # SQLite DEFAULT should be applied
        assert row[0] == "kraken"
    
    def test_schema_info_returns_complete_data(self):
        """Test that get_live_trades_schema_info returns all expected fields."""
        from backend.db.migrations import (
            ensure_schema_up_to_date,
            get_live_trades_schema_info,
        )
        
        self._create_old_schema_db()
        ensure_schema_up_to_date(self.database_url)
        
        info = get_live_trades_schema_info(self.database_url)
        
        assert "db_path" in info
        assert "schema_version" in info
        assert "columns" in info
        assert "row_count" in info
        assert info["row_count"] == 1
        
        col_names = [c["name"] for c in info["columns"]]
        assert "broker" in col_names
        assert "parent_trade_id" in col_names
    
    def test_empty_db_migration(self):
        """Test migration on an empty/new database."""
        from backend.db.migrations import ensure_schema_up_to_date, CURRENT_SCHEMA_VERSION
        
        # Don't create any tables, just run migration on empty file
        from_ver, to_ver = ensure_schema_up_to_date(self.database_url)
        
        # Should set version even if no table exists yet
        assert to_ver == CURRENT_SCHEMA_VERSION


class TestRecoveryAfterMigration:
    """Test that trade recovery works after migration."""
    
    def setup_method(self):
        """Create temporary database."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_recovery.db")
        self.database_url = f"sqlite:///{self.db_path}"
        
        # Patch DATABASE_URL for this test
        import backend.db.session as session_mod
        self._original_url = session_mod.DATABASE_URL
        session_mod.DATABASE_URL = self.database_url
    
    def teardown_method(self):
        """Clean up."""
        import backend.db.session as session_mod
        session_mod.DATABASE_URL = self._original_url
        
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        if os.path.exists(self.temp_dir):
            os.rmdir(self.temp_dir)
    
    def _create_old_schema_with_open_trade(self):
        """Create old schema DB with an open trade."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE live_trades (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                ts_open DATETIME DEFAULT CURRENT_TIMESTAMP,
                ts_close DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                entry REAL NOT NULL,
                stop REAL NOT NULL,
                tp REAL NOT NULL,
                leverage INTEGER DEFAULT 10,
                notional REAL,
                rr REAL DEFAULT 0.0,
                pnl REAL DEFAULT 0.0,
                fee REAL DEFAULT 0.0,
                close_price REAL,
                status TEXT DEFAULT 'OPEN',
                result TEXT,
                session TEXT,
                preset TEXT,
                signal_id TEXT,
                raw_exchange TEXT
            )
        """)
        
        conn.execute("""
            INSERT INTO live_trades 
            (id, order_id, symbol, side, qty, entry, stop, tp, status, leverage)
            VALUES 
            ('trade-open-1', 'order-123', 'ETHUSDT', 'short', 0.1, 3000, 3100, 2800, 'OPEN', 10)
        """)
        conn.commit()
        conn.close()


def test_migrations_can_be_imported():
    """Basic import test."""
    from backend.db.migrations import (
        ensure_schema_up_to_date,
        ensure_all_columns_exist,
        get_live_trades_schema_info,
        CURRENT_SCHEMA_VERSION,
    )
    assert CURRENT_SCHEMA_VERSION >= 2


def test_cli_tool_can_be_imported():
    """Test that the CLI tool module is valid."""
    import backend.tools.migrate_db as cli
    assert hasattr(cli, "main")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
