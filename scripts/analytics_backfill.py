#!/usr/bin/env python3
"""
Analytics Backfill & Diagnostic Script
=======================================

Usage:
    python scripts/analytics_backfill.py              # Diagnostic only
    python scripts/analytics_backfill.py --backfill   # Backfill last 30 days
    python scripts/analytics_backfill.py --backfill --days 90  # Backfill 90 days
    python scripts/analytics_backfill.py --init-tables # Create missing tables

This script:
  1. Checks which analytics tables exist and their row counts
  2. Identifies missing data
  3. Optionally backfills daily_analytics from raw tables
  4. Optionally creates missing tables
"""

import argparse
import os
import sys
from datetime import date, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="Analytics Pipeline Diagnostic & Backfill")
    parser.add_argument("--backfill", action="store_true", help="Run backfill aggregation")
    parser.add_argument("--days", type=int, default=30, help="Number of days to backfill (default: 30)")
    parser.add_argument("--init-tables", action="store_true", help="Create missing DB tables")
    args = parser.parse_args()

    from backend.db.session import engine, Base, init_db, DATABASE_URL
    from sqlalchemy import inspect, text

    print("=" * 70)
    print("  ANALYTICS PIPELINE — DIAGNOSTIC")
    print("=" * 70)
    print(f"  Database: {DATABASE_URL}")
    print()

    # 1. Check tables
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    analytics_tables = [
        "system_state_snapshots",
        "signal_decisions",
        "trade_analytics",
        "risk_events",
        "daily_analytics",
        "weekly_analytics",
        "execution_attempts",
        "ops_events",
    ]

    print("  TABLE STATUS:")
    print("  " + "-" * 50)
    missing_tables = []
    with engine.connect() as conn:
        for tbl in analytics_tables:
            exists = tbl in tables
            if exists:
                result = conn.execute(text(f"SELECT COUNT(*) FROM {tbl}"))
                count = result.scalar()
                status = f"✅ {count:>8} rows"
            else:
                status = "❌ MISSING"
                missing_tables.append(tbl)
            print(f"  {tbl:<30} {status}")

    print()

    # 2. Init tables if requested or needed
    if args.init_tables or missing_tables:
        if missing_tables and not args.init_tables:
            print(f"  ⚠️  {len(missing_tables)} tables missing. Run with --init-tables to create them.")
        if args.init_tables:
            print("  Creating missing tables...")
            init_db()
            print("  ✅ Tables created successfully.")
            print()

    # 3. Check data date range
    if "signal_decisions" in tables:
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT MIN(ts), MAX(ts) FROM signal_decisions"
            ))
            row = result.fetchone()
            if row and row[0]:
                print(f"  Signal decisions range: {row[0]} → {row[1]}")
            else:
                print("  Signal decisions: NO DATA")

    if "execution_attempts" in tables:
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT MIN(ts), MAX(ts) FROM execution_attempts"
            ))
            row = result.fetchone()
            if row and row[0]:
                print(f"  Execution attempts range: {row[0]} → {row[1]}")
            else:
                print("  Execution attempts: NO DATA")

    if "trade_analytics" in tables:
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT MIN(ts_entry), MAX(ts_entry) FROM trade_analytics"
            ))
            row = result.fetchone()
            if row and row[0]:
                print(f"  Trade analytics range: {row[0]} → {row[1]}")
            else:
                print("  Trade analytics: NO DATA")

    if "system_state_snapshots" in tables:
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT MIN(ts), MAX(ts) FROM system_state_snapshots"
            ))
            row = result.fetchone()
            if row and row[0]:
                print(f"  System snapshots range: {row[0]} → {row[1]}")
            else:
                print("  System snapshots: NO DATA")

    if "risk_events" in tables:
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT MIN(ts), MAX(ts) FROM risk_events"
            ))
            row = result.fetchone()
            if row and row[0]:
                print(f"  Risk events range: {row[0]} → {row[1]}")
            else:
                print("  Risk events: NO DATA")

    print()

    # 4. Backfill
    if args.backfill:
        print("  BACKFILL AGGREGATION")
        print("  " + "-" * 50)
        from backend.services.analytics_service import AnalyticsAggregator

        today = date.today()
        start = today - timedelta(days=args.days)
        print(f"  Period: {start} → {today} ({args.days} days)")
        print("  Processing...")

        count = AnalyticsAggregator.backfill(start, today)
        print(f"  ✅ Aggregated {count} days with data.")

        # Show result
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT COUNT(*) FROM daily_analytics"
            ))
            total = result.scalar()
            print(f"  Total daily_analytics rows: {total}")
        print()

    # 5. Execution blocked analysis
    if "execution_attempts" in tables:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT block_reason, COUNT(*) as cnt
                FROM execution_attempts
                WHERE status = 'PRE_FLIGHT_REJECT'
                AND block_reason IS NOT NULL
                GROUP BY block_reason
                ORDER BY cnt DESC
                LIMIT 10
            """))
            rows = result.fetchall()
            if rows:
                print("  TOP BLOCK REASONS:")
                print("  " + "-" * 50)
                for row in rows:
                    print(f"  {row[0]:<35} {row[1]:>6}")
                print()

    print("=" * 70)
    print("  DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
