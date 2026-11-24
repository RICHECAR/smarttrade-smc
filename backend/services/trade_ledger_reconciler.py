"""Trade Ledger Reconciliation Engine

This module provides a reconciliation engine that ensures consistency between:

1. Exchange reality (open positions)
2. Trade ledger (live_trades)
3. Analytics (trade_analytics, daily_analytics)

The reconcilier detects discrepancies and can optionally fix the safe ones.

The design is intentionally conservative: only fixes issues that are
unambiguous, logs everything, and exposes a clear dry-run mode.

Usage:
    recon = TradeLedgerReconciler()
    report = recon.reconcile(dry_run=True)

Author: SMC Trading Bot Framework
"""

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.db.session import get_session_factory
from backend.models.live_trade import LiveTrade
from backend.models.persisted_position import PersistedPosition
from backend.models.analytics import TradeAnalytics
from backend.services.log_rate_limit import rate_limited_warning
from smc_futures.app_context import build_app_context

# Backwards-compatible monkeypatch target (tests may patch reconciler_module.SessionLocal)
SessionLocal = None

from smc_futures.observability.metrics import (
    LEDGER_RECON_RUNS_TOTAL,
    LEDGER_RECON_ISSUES_TOTAL,
    LEDGER_RECON_FIXED_TOTAL,
    LEDGER_RECON_DRYRUN_TOTAL,
    LEDGER_RECON_LAST_DURATION_MS,
)

logger = logging.getLogger("ledger_reconciler")


# Issue types
ISSUE_DB_OPEN_NO_EXCHANGE = "DB_OPEN_NO_EXCHANGE_POSITION"
ISSUE_EXCHANGE_OPEN_NO_DB = "EXCHANGE_OPEN_NO_DB_POSITION"
ISSUE_MISSING_ANALYTICS = "ANALYTICS_MISSING_FOR_CLOSED_TRADE"
ISSUE_FAILED_IN_ANALYTICS = "FAILED_TRADE_IN_ANALYTICS"
ISSUE_STATUS_MISMATCH = "STATUS_MISMATCH"


class TradeLedgerReconciler:
    """Reconcile exchange positions, trade ledger, and analytics."""

    def __init__(
        self,
        executor_name: str = "kraken",
        session_factory=None,
        ctx=None,
    ):
        self.ctx = ctx if ctx is not None else build_app_context()
        self.executor = self.ctx.executors.get(executor_name)
        self._session_factory = session_factory

    def _get_db(self) -> Session:
        if self._session_factory is not None:
            return self._session_factory()
        if SessionLocal is not None:
            return SessionLocal()
        return get_session_factory()()

    async def _fetch_exchange_open_positions(self) -> Dict[str, float]:
        """Return a map symbol -> open qty (sum across brokers)."""
        if not self.executor:
            return {}

        try:
            raw = await self.executor.get_open_positions()
        except Exception as e:
            rate_limited_warning(logger, "[RECONCILE] Exchange fetch failed: %s", e)
            return {}

        positions: Dict[str, float] = {}
        for pos in raw:
            sym = pos.get("symbol")
            qty = float(pos.get("size") or pos.get("total") or 0)
            if not sym or qty <= 0:
                continue
            sym = sym.upper()
            positions[sym] = positions.get(sym, 0.0) + qty
        return positions

    def _fetch_db_open_positions(self, db: Session) -> Dict[str, float]:
        rows = db.query(PersistedPosition).filter(PersistedPosition.status == "OPEN").all()
        out: Dict[str, float] = {}
        for r in rows:
            if not r.symbol:
                continue
            out[r.symbol.upper()] = out.get(r.symbol.upper(), 0.0) + (r.size or 0.0)
        return out

    def _fetch_db_trades(self, db: Session) -> List[LiveTrade]:
        return db.query(LiveTrade).all()

    def _fetch_analytics_for_trade(self, db: Session, trade_id: str) -> Optional[TradeAnalytics]:
        return db.query(TradeAnalytics).filter(TradeAnalytics.trade_id == trade_id).first()

    def _close_trade_in_db(self, db: Session, trade: LiveTrade, reason: str) -> None:
        trade.status = "CLOSED"
        trade.close_reason = reason
        trade.ts_close = datetime.now(timezone.utc)
        # NOTE: Do NOT set pnl=0.0 when unknown — that pollutes stats with
        # false break-even trades.  Leave pnl=None so the trade is excluded
        # from statistics (stats filter: pnl IS NOT NULL).
        db.add(trade)

    def _create_missing_analytics(self, db: Session, trade: LiveTrade) -> None:
        # Minimal analytics record for a closed trade
        analytics = TradeAnalytics(
            trade_id=trade.id,
            symbol=trade.symbol,
            side=trade.side,
            entry_price=trade.entry,
            exit_price=trade.close_price or trade.entry,
            size=trade.qty,
            fees=trade.fee or 0.0,
            sl_price=trade.stop,
            tp_price=trade.tp,
            pnl=trade.pnl or 0.0,
            regime_at_entry=None,
            regime_at_exit=None,
            ts_entry=trade.ts_open,
            ts_exit=trade.ts_close,
            strategy=None,
            edge_score_at_entry=None,
        )
        db.add(analytics)

    def reconcile(self, dry_run: bool = True) -> Dict[str, Any]:
        """Run reconciliation.

        Returns a report dict.
        """
        start = datetime.now(timezone.utc)
        LEDGER_RECON_RUNS_TOTAL.inc()
        if dry_run:
            LEDGER_RECON_DRYRUN_TOTAL.inc()

        report = {
            "ok": True,
            "summary": {
                "positions_checked": 0,
                "trades_checked": 0,
                "analytics_checked": 0,
                "issues_found": 0,
                "issues_fixed": 0,
            },
            "issues": [],
        }

        db = self._get_db()
        try:
            # _fetch_exchange_open_positions is async in normal operation, but tests may patch it with a sync stub.
            exchange_open_raw = self._fetch_exchange_open_positions()
            if inspect.isawaitable(exchange_open_raw):
                exchange_open = asyncio.run(exchange_open_raw)
            else:
                exchange_open = exchange_open_raw
            db_open = self._fetch_db_open_positions(db)

            report["summary"]["positions_checked"] = max(len(exchange_open), len(db_open))

            # A. DB open positions with no exchange position
            for sym, qty in db_open.items():
                ex_qty = exchange_open.get(sym, 0.0)
                if ex_qty <= 0:
                    report["summary"]["issues_found"] += 1
                    issue = {
                        "type": "DB_OPEN_EXCHANGE_MISSING",
                        "severity": "high",
                        "symbol": sym,
                        "details": {"db_qty": qty, "exchange_qty": ex_qty},
                        "action_taken": "NONE",
                    }
                    if not dry_run:
                        # Close all open trades for this symbol
                        trades = db.query(LiveTrade).filter(
                            LiveTrade.symbol == sym,
                            LiveTrade.status == "OPEN",
                        ).all()
                        for t in trades:
                            self._close_trade_in_db(db, t, "RECONCILER")
                            report["summary"]["issues_fixed"] += 1
                        issue["action_taken"] = "FORCED_DB_CLOSE"
                        db.commit()
                    report["issues"].append(issue)
                    LEDGER_RECON_ISSUES_TOTAL.labels(type=issue["type"]).inc()
                    if issue["action_taken"] != "NONE":
                        LEDGER_RECON_FIXED_TOTAL.labels(type=issue["type"]).inc()

            # B. Exchange open positions with no DB open trade
            for sym, ex_qty in exchange_open.items():
                if ex_qty <= 0:
                    continue
                db_qty = db_open.get(sym, 0.0)
                if db_qty <= 0:
                    report["summary"]["issues_found"] += 1
                    issue = {
                        "type": "EXCHANGE_OPEN_NO_DB_POSITION",
                        "severity": "high",
                        "symbol": sym,
                        "details": {"exchange_qty": ex_qty},
                        "action_taken": "NONE",
                    }
                    report["issues"].append(issue)
                    LEDGER_RECON_ISSUES_TOTAL.labels(type=issue["type"]).inc()

            # C. Analytics missing for closed trades / failed trades in analytics
            trades = self._fetch_db_trades(db)
            report["summary"]["trades_checked"] = len(trades)
            for t in trades:
                if t.status == "CLOSED":
                    report["summary"]["analytics_checked"] += 1
                    analytics = self._fetch_analytics_for_trade(db, t.id)
                    if not analytics:
                        report["summary"]["issues_found"] += 1
                        issue = {
                            "type": "ANALYTICS_MISSING_FOR_CLOSED_TRADE",
                            "severity": "medium",
                            "symbol": t.symbol,
                            "trade_id": t.id,
                            "details": {"status": t.status},
                            "action_taken": "NONE",
                        }
                        if not dry_run:
                            self._create_missing_analytics(db, t)
                            report["summary"]["issues_fixed"] += 1
                            issue["action_taken"] = "CREATED_MISSING_ANALYTICS"
                            db.commit()
                        report["issues"].append(issue)
                        LEDGER_RECON_ISSUES_TOTAL.labels(type=issue["type"]).inc()
                        if issue["action_taken"] != "NONE":
                            LEDGER_RECON_FIXED_TOTAL.labels(type=issue["type"]).inc()

                if t.status == "FAILED":
                    # If a FAILED trade appears in analytics, flag it
                    analytics = self._fetch_analytics_for_trade(db, t.id)
                    if analytics:
                        report["issues_found"] += 1
                        issue = {
                            "type": "FAILED_TRADE_IN_ANALYTICS",
                            "severity": "medium",
                            "symbol": t.symbol,
                            "trade_id": t.id,
                            "details": {"status": t.status},
                            "action_taken": "NONE",
                        }
                        report["issues"].append(issue)
                        LEDGER_RECON_ISSUES_TOTAL.labels(type=issue["type"]).inc()

            return report
        finally:
            db.close()
            duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
            LEDGER_RECON_LAST_DURATION_MS.set(duration_ms)
