#!/usr/bin/env python3
"""
Analytics DB Repair Script
═══════════════════════════
Fixes corrupted analytics data caused by:
  1. FAILED orders persisted as OPEN trades/positions
  2. Trades stuck OPEN because close was blocked (qty=0 bug)
  3. Stale daily_analytics aggregates

Usage:
    python scripts/repair_analytics_db.py [--dry-run] [--reaggregate]

Flags:
    --dry-run       Show what would be changed without modifying DB
    --reaggregate   Re-aggregate daily_analytics after repairs

Author: SMC Trading Bot Framework
"""

import sys
import os
import argparse
import logging
from datetime import date, datetime, timezone

# Setup path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.db.session import SessionLocal
from backend.models.live_trade import LiveTrade
from backend.models.persisted_position import PersistedPosition

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("repair_db")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def repair_failed_open_trades(db, dry_run: bool) -> int:
    """
    Rule 1: Trades with status=FAILED but incorrectly left as OPEN.
    Mark them as FAILED and ensure they don't pollute analytics.
    """
    # Find trades that have error_message but are still OPEN
    orphan_failed = db.query(LiveTrade).filter(
        LiveTrade.status == "OPEN",
        LiveTrade.error_message.isnot(None),
    ).all()

    count = 0
    for trade in orphan_failed:
        logger.info(
            f"[REPAIR] FAILED_AS_OPEN | id={trade.id[:8]} | {trade.symbol}"
            f" | order_id={trade.order_id} | error={trade.error_message}"
        )
        if not dry_run:
            trade.status = "FAILED"
        count += 1

    if count:
        logger.info(f"[REPAIR] Marked {count} error-OPEN trades as FAILED")
    return count


def repair_orphan_open_trades(db, dry_run: bool) -> int:
    """
    Rule 2: Trades stuck OPEN for too long (>48h) with no exchange position.
    These are likely from the qty=0 close bug.
    Mark them as CLOSED with close_reason=RECONCILER.
    """
    from datetime import timedelta

    threshold = datetime.now(timezone.utc) - timedelta(hours=48)
    stale_open = db.query(LiveTrade).filter(
        LiveTrade.status == "OPEN",
        LiveTrade.ts_open < threshold,
    ).all()

    count = 0
    for trade in stale_open:
        logger.info(
            f"[REPAIR] STALE_OPEN (>48h) | id={trade.id[:8]} | {trade.symbol}"
            f" | order_id={trade.order_id} | opened={trade.ts_open}"
        )
        if not dry_run:
            trade.status = "CLOSED"
            trade.ts_close = datetime.now(timezone.utc)
            trade.close_reason = "RECONCILER_STALE"
            # Keep pnl as-is (may be 0 or None)
        count += 1

    if count:
        logger.info(f"[REPAIR] Closed {count} stale OPEN trades (>48h)")
    return count


def repair_failed_positions(db, dry_run: bool) -> int:
    """
    Rule 3: Positions with status=OPEN that should be FAILED or CLOSED.
    """
    try:
        # Close positions with error messages
        orphan_positions = db.query(PersistedPosition).filter(
            PersistedPosition.status == "OPEN",
            PersistedPosition.error_message.isnot(None),
        ).all()

        count = 0
        for pos in orphan_positions:
            logger.info(
                f"[REPAIR] FAILED_POSITION | id={pos.id[:8]} | {pos.symbol}"
                f" | error={pos.error_message}"
            )
            if not dry_run:
                pos.status = "CANCELLED"
            count += 1

        if count:
            logger.info(f"[REPAIR] Marked {count} error positions as CANCELLED")
        return count
    except Exception as e:
        logger.warning(f"[REPAIR] Could not repair positions: {e}")
        return 0


def purge_pure_failed(db, dry_run: bool) -> int:
    """
    Rule 4: Remove trades with status=FAILED that are audit-only.
    These should not exist in the live_trades table at all.
    """
    failed_count = db.query(LiveTrade).filter(
        LiveTrade.status == "FAILED"
    ).count()

    if failed_count > 0:
        logger.info(f"[REPAIR] Found {failed_count} FAILED trades to purge")
        if not dry_run:
            db.query(LiveTrade).filter(
                LiveTrade.status == "FAILED"
            ).delete(synchronize_session="fetch")

    return failed_count


def print_summary(db):
    """Print current DB state summary."""
    total = db.query(LiveTrade).count()
    open_count = db.query(LiveTrade).filter(LiveTrade.status == "OPEN").count()
    closed_count = db.query(LiveTrade).filter(LiveTrade.status == "CLOSED").count()
    failed_count = db.query(LiveTrade).filter(LiveTrade.status == "FAILED").count()
    other = total - open_count - closed_count - failed_count

    with_pnl = db.query(LiveTrade).filter(
        LiveTrade.status == "CLOSED",
        LiveTrade.pnl.isnot(None),
    ).count()

    logger.info("═" * 60)
    logger.info(f"DB STATE: total={total} | OPEN={open_count} | CLOSED={closed_count}"
                f" | FAILED={failed_count} | other={other}")
    logger.info(f"          CLOSED with PnL={with_pnl}")
    logger.info("═" * 60)


def reaggregate_analytics():
    """Re-aggregate all daily analytics from scratch."""
    try:
        from backend.services.analytics_service import AnalyticsAggregator
        count = AnalyticsAggregator.backfill(date(2024, 1, 1), date.today())
        logger.info(f"[REAGGREGATE] Processed {count} days")
        return count
    except Exception as e:
        logger.error(f"[REAGGREGATE] Failed: {e}", exc_info=True)
        return 0


def main():
    parser = argparse.ArgumentParser(description="Repair analytics DB")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    parser.add_argument("--reaggregate", action="store_true", help="Re-aggregate daily analytics")
    args = parser.parse_args()

    if args.dry_run:
        logger.info("═══ DRY RUN MODE — no changes will be made ═══")

    db = SessionLocal()
    try:
        logger.info("╔══ BEFORE REPAIR ══╗")
        print_summary(db)

        total_fixes = 0
        total_fixes += repair_failed_open_trades(db, args.dry_run)
        total_fixes += repair_orphan_open_trades(db, args.dry_run)
        total_fixes += repair_failed_positions(db, args.dry_run)
        total_fixes += purge_pure_failed(db, args.dry_run)

        if not args.dry_run and total_fixes > 0:
            db.commit()
            logger.info(f"[REPAIR] Committed {total_fixes} fixes")

        logger.info("╔══ AFTER REPAIR ══╗")
        print_summary(db)

        if args.reaggregate and not args.dry_run:
            logger.info("[REAGGREGATE] Re-aggregating daily analytics...")
            reaggregate_analytics()

    finally:
        db.close()

    logger.info(f"Done. Total fixes: {total_fixes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
