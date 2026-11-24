#!/usr/bin/env python3
"""
Database Migration CLI Tool

Migrates the SQLite database schema to the latest version.
Can also display current schema information.

Usage:
    python -m backend.tools.migrate_db              # Migrate and show status
    python -m backend.tools.migrate_db --info       # Show schema info only
    python -m backend.tools.migrate_db --force      # Force re-check all columns

Author: SMC Trading Bot Framework
"""

import argparse
import logging
import os
import sys

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="SQLite Database Migration Tool for Live Trades",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m backend.tools.migrate_db              # Run migrations
    python -m backend.tools.migrate_db --info       # Show schema info
    python -m backend.tools.migrate_db --db data/trades.db  # Custom DB path
        """
    )
    parser.add_argument(
        "--info", "-i",
        action="store_true",
        help="Show schema information without migrating"
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Force re-check all columns even if version matches"
    )
    parser.add_argument(
        "--db", "-d",
        type=str,
        default=None,
        help="Path to SQLite database (default: from DATABASE_URL or ./data/trades.db)"
    )
    args = parser.parse_args()
    
    # Determine database URL
    if args.db:
        database_url = f"sqlite:///{args.db}"
    else:
        database_url = os.getenv("DATABASE_URL", "sqlite:///./data/trades.db")
    
    print("=" * 60)
    print("SQLite Database Migration Tool")
    print("=" * 60)
    print(f"Database URL: {database_url}")
    print()
    
    from backend.db.migrations import (
        ensure_schema_up_to_date,
        ensure_all_columns_exist,
        get_live_trades_schema_info,
        CURRENT_SCHEMA_VERSION,
    )
    
    # Show current schema info
    print("Current Schema Status:")
    print("-" * 40)
    info = get_live_trades_schema_info(database_url)
    
    if "error" in info:
        print(f"  Error: {info['error']}")
        return 1
    
    print(f"  DB Path:         {info.get('db_path', 'N/A')}")
    print(f"  Schema Version:  {info.get('schema_version', 0)} (target: {CURRENT_SCHEMA_VERSION})")
    print(f"  Needs Migration: {info.get('needs_migration', False)}")
    
    if "columns" in info:
        print(f"  Row Count:       {info.get('row_count', 0)}")
        print(f"  Open Trades:     {info.get('open_trades', 0)}")
        print()
        print("  Columns:")
        for col in info["columns"]:
            default_str = f" DEFAULT {col['default']}" if col['default'] else ""
            nullable_str = " NULL" if col['nullable'] else " NOT NULL"
            print(f"    - {col['name']}: {col['type']}{nullable_str}{default_str}")
        print()
        print(f"  Indexes: {', '.join(info.get('indexes', [])) or 'None'}")
    else:
        print("  Table: live_trades does not exist yet")
    
    print()
    
    if args.info:
        print("Info mode - no changes made")
        return 0
    
    # Run migrations
    print("Running Migrations...")
    print("-" * 40)
    
    try:
        from_ver, to_ver = ensure_schema_up_to_date(database_url)
        print(f"  Migration: v{from_ver} → v{to_ver}")
        
        if from_ver == to_ver:
            print("  Status: Already up-to-date")
        else:
            print("  Status: Migrations applied successfully")
        
        # Force re-check columns if requested
        if args.force:
            print()
            print("Force mode - checking all columns...")
            added = ensure_all_columns_exist(database_url)
            if added:
                print("  Additional columns were added")
            else:
                print("  All columns already exist")
        
        print()
        print("=" * 60)
        print("Migration complete!")
        print("=" * 60)
        
        # Show updated info
        print()
        print("Updated Schema:")
        print("-" * 40)
        updated_info = get_live_trades_schema_info(database_url)
        if "columns" in updated_info:
            col_names = [c["name"] for c in updated_info["columns"]]
            print(f"  Columns: {', '.join(col_names)}")
            print(f"  Version: {updated_info.get('schema_version', 0)}")
        
        return 0
        
    except Exception as e:
        logger.error(f"Migration failed: {e}", exc_info=True)
        print(f"\nError: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
