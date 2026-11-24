"""
Soft-Cleanup Script — Archive old FAILED / SKIPPED trades
=========================================================
Sets status -> 'ARCHIVED' so they are excluded from UI and stats
while remaining in the DB for audit purposes.

Operates on BOTH tables:
  - live_trades (LiveTrade)  — main source of truth for LIVE trading
  - trades      (Trade)      — legacy/backtest table

Usage:
    # Dry-run (preview only):
    python -m backend.tools.cleanup_failed_trades --dry-run

    # Apply:
    python -m backend.tools.cleanup_failed_trades --apply
"""

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("cleanup")

STATUSES_TO_ARCHIVE = ["FAILED", "failed", "SKIPPED", "skipped"]


def _archive_table(db, model_cls, table_label: str, dry_run: bool) -> int:
    """Archive FAILED/SKIPPED trades in a single table. Returns count archived."""
    candidates = (
        db.query(model_cls)
        .filter(model_cls.status.in_(STATUSES_TO_ARCHIVE))
        .all()
    )

    if not candidates:
        logger.info(f"[{table_label}] No FAILED/SKIPPED trades found. Already clean.")
        return 0

    # Summary
    symbols: dict[str, int] = {}
    for t in candidates:
        sym = getattr(t, "symbol", "UNKNOWN") or "UNKNOWN"
        symbols[sym] = symbols.get(sym, 0) + 1

    logger.info(f"[{table_label}] Found {len(candidates)} FAILED/SKIPPED trades:")
    for sym, count in sorted(symbols.items()):
        logger.info(f"  {sym}: {count} trades")

    if dry_run:
        logger.info(f"[{table_label}] DRY-RUN — no changes applied.")
        return 0

    # Apply
    archived = 0
    for t in candidates:
        t.status = "ARCHIVED"
        if hasattr(t, "close_reason"):
            t.close_reason = "pre_fix_failed_trade"
        archived += 1

    return archived


def run(dry_run: bool = True):
    from backend.db.session import SessionLocal

    db = SessionLocal()
    total_archived = 0

    try:
        # 1. LiveTrade table (primary)
        try:
            from backend.models.live_trade import LiveTrade
            total_archived += _archive_table(db, LiveTrade, "live_trades", dry_run)
        except Exception as e:
            logger.warning(f"[live_trades] Skipped: {e}")

        # 2. Legacy Trade table
        try:
            from backend.models.trade import Trade
            total_archived += _archive_table(db, Trade, "trades", dry_run)
        except Exception as e:
            logger.warning(f"[trades] Skipped: {e}")

        if not dry_run and total_archived > 0:
            db.commit()
            logger.info(f"✅ Archived {total_archived} trades successfully.")
            logger.info("   These trades are now excluded from UI and stats.")
        elif dry_run:
            logger.info("Run with --apply to archive these trades.")
        else:
            logger.info("Nothing to archive.")

    except Exception as e:
        db.rollback()
        logger.error(f"Cleanup failed: {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Archive old FAILED/SKIPPED trades")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    group.add_argument("--apply", action="store_true", help="Apply cleanup")
    args = parser.parse_args()

    run(dry_run=args.dry_run)
