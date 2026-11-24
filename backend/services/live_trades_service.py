"""
Live Trades Service - Production-grade trade persistence

Features:
- create_live_trade(): INSERT after Kraken confirmation
- close_live_trade(): UPDATE on TP/SL/Manual close
- get_open_trades(): Recover OPEN trades on restart
- Zero duplicate writes (order_id UNIQUE constraint)
- Full retry/rollback on DB errors
- Non-blocking async wrapper for heavy operations

Author: SMC Trading Bot Framework
"""

import logging
import asyncio
import threading
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from contextlib import contextmanager
from functools import wraps

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.db.base import Base
from backend.db.session import get_session_factory, get_engine, get_db_url, assert_database_url_allowed
from backend.models.live_trade import LiveTrade
from backend.models.trade_fill import TradeFill
from backend.db.migrations import (
    ensure_schema_up_to_date,
    ensure_all_columns_exist,
    CURRENT_SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)
_trade_db_lock = threading.RLock()
_custom_session_factory = None


def set_live_trades_session_factory(session_factory=None) -> None:
    """Explicitly inject or clear the session factory used by LiveTradesService (tests/DI)."""
    global _custom_session_factory
    _custom_session_factory = session_factory

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

def init_live_trades_schema(target_engine=None) -> None:
    """Create live_trades and trade_fills tables explicitly without running file migrations."""
    eng = target_engine or get_engine()
    Base.metadata.create_all(
        bind=eng,
        tables=[LiveTrade.__table__, TradeFill.__table__],
    )
    logger.info("[LIVE_DB] live_trades + trade_fills schema initialized")


def init_live_trades_db(target_engine=None, database_url: Optional[str] = None, run_migrations: bool = True):
    """
    Initialize live_trades and trade_fills tables.
    
    Order of operations:
    1. Run schema migrations (add any missing columns) for file-backed DBs
    2. Create tables via SQLAlchemy (if not exist)
    """
    try:
        eng = target_engine or get_engine()
        url = database_url or str(eng.url) or get_db_url()
        assert_database_url_allowed(url, caller_context="init_live_trades_db")

        # Step 1: Run migrations BEFORE SQLAlchemy touches the DB (skipped automatically for :memory:)
        if run_migrations:
            try:
                from_ver, to_ver = ensure_schema_up_to_date(url)
                logger.info(f"[LIVE_DB] Schema migration: v{from_ver} → v{to_ver}")
            except Exception as mig_err:
                logger.warning(f"[LIVE_DB] Migration error (trying fallback): {mig_err}")
                ensure_all_columns_exist(url)
        
        # Step 2: Create tables via SQLAlchemy (idempotent)
        init_live_trades_schema(target_engine=eng)
        logger.info("[LIVE_DB] live_trades + trade_fills tables initialized")
        
    except Exception as e:
        logger.error(f"[LIVE_DB] Failed to initialize: {e}")
        raise


# ═══════════════════════════════════════════════════════════════════════════════
# SESSION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@contextmanager
def get_db_session(session_factory=None):
    """
    Context manager for database sessions with auto-rollback.
    
    Usage:
        with get_db_session() as db:
            db.query(LiveTrade).all()
    """
    factory = session_factory or _custom_session_factory or get_session_factory()
    db = factory()
    try:
        yield db
    except SQLAlchemyError as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"[LIVE_DB] Session error, rolling back: {e}")
        raise
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def retry_on_db_error(max_retries: int = 3, delay: float = 0.1):
    """
    Decorator for retrying database operations.
    
    Args:
        max_retries: Maximum retry attempts
        delay: Delay between retries (seconds)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except SQLAlchemyError as e:
                    last_error = e
                    logger.warning(
                        f"[LIVE_DB] Retry {attempt + 1}/{max_retries} for {func.__name__}: {e}"
                    )
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(delay * (attempt + 1))  # Exponential backoff
            raise last_error
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE TRADES SERVICE
# ═══════════════════════════════════════════════════════════════════════════════

class LiveTradesService:
    """
    Service for LIVE trade persistence.
    
    All methods are static for easy module-level access.
    Thread-safe via session-per-operation pattern.
    """
    
    # ─────────────────────────────────────────────────────────────
    # CREATE TRADE (After Bitget confirmation)
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    @retry_on_db_error(max_retries=3)
    def create_live_trade(
        order_id: str,
        symbol: str,
        side: str,
        qty: float,
        entry: float,
        stop: float,
        tp: float,
        leverage: int = 10,
        session_name: Optional[str] = None,
        preset: Optional[str] = None,
        signal_id: Optional[str] = None,
        raw_exchange: Optional[Dict[str, Any]] = None,
        broker: str = "kraken",
        parent_trade_id: Optional[str] = None,
        status: str = "OPEN",
        error_message: Optional[str] = None,
        client_oid: Optional[str] = None,
    ) -> Optional[LiveTrade]:
        """
        Insert a new LIVE trade after broker order confirmation (or failure).
        
        Args:
            order_id: Broker order ID (unique per broker)
            symbol: Trading pair (e.g., BTCUSDT)
            side: "long" or "short"
            qty: Position quantity
            entry: Entry price
            stop: Stop loss price
            tp: Take profit price
            leverage: Leverage used
            session_name: Trading session (London/NY)
            preset: Preset name
            signal_id: Signal ID for dedup
            raw_exchange: Raw broker API response
            broker: Broker name ("bitget" or "kraken")
            parent_trade_id: Links mirror trades together
            status: Trade status (OPEN, FAILED, etc.)
            error_message: Optional execution error string
        
        Returns:
            Created LiveTrade or None if duplicate
        
        Raises:
            SQLAlchemyError: On non-duplicate DB errors
        """
        with _trade_db_lock:
            with get_db_session() as db:
                # Check for existing order_id for this broker (prevent INSERT even before constraint fires)
                existing = db.query(LiveTrade).filter(
                    LiveTrade.order_id == order_id,
                    LiveTrade.broker == broker
                ).first()
                if existing:
                    logger.warning(f"[LIVE_DB] DUPLICATE order_id={order_id} broker={broker}, merging metadata")
                    if raw_exchange:
                        existing_raw = existing.get_raw_exchange() or {}
                        if "protections" in existing_raw and "protections" not in raw_exchange:
                            raw_exchange["protections"] = existing_raw["protections"]
                        existing.set_raw_exchange(raw_exchange)
                    if entry and (not existing.entry or existing.entry == 0.0):
                        existing.entry = entry
                    if stop and (not existing.stop or existing.stop == 0.0):
                        existing.stop = stop
                    if tp and (not existing.tp or existing.tp == 0.0):
                        existing.tp = tp
                    if session_name and not existing.session:
                        existing.session = session_name
                    if preset and not existing.preset:
                        existing.preset = preset
                    if signal_id and not existing.signal_id:
                        existing.signal_id = signal_id
                    if client_oid and not existing.client_oid:
                        existing.client_oid = client_oid
                    db.commit()
                    db.refresh(existing)
                    return existing
            
            # Calculate derived fields
            rr = LiveTrade.calculate_rr(entry, stop, tp, side)
            notional = qty * entry  # Without leverage for base value
            
            # Create trade record
            trade = LiveTrade(
                order_id=order_id,
                broker=broker,
                parent_trade_id=parent_trade_id,
                symbol=symbol.upper(),
                side=side.lower(),
                qty=qty,
                entry=entry,
                stop=stop,
                tp=tp,
                leverage=leverage,
                notional=notional,
                rr=rr,
                pnl=0.0,
                fee=0.0,
                status=status.upper(),
                error_message=error_message,
                session=session_name,
                preset=preset,
                signal_id=signal_id,
                client_oid=client_oid,
            )
            
            # Store raw exchange data
            if raw_exchange:
                trade.set_raw_exchange(raw_exchange)
            
            try:
                db.add(trade)
                db.commit()
                db.refresh(trade)
                
                logger.info(
                    f"[LIVE_DB] TRADE CREATED | id={trade.id} | broker={broker}"
                    f" | order_id={order_id} | {symbol} {side.upper()}"
                    f" | qty={qty} | entry={entry} | RR={rr} | status={status.upper()}"
                )
                return trade
                
            except IntegrityError as e:
                db.rollback()
                if "UNIQUE constraint" in str(e) or "duplicate" in str(e).lower():
                    logger.warning(f"[LIVE_DB] DUPLICATE INSERT blocked: order_id={order_id}")
                    return db.query(LiveTrade).filter(LiveTrade.order_id == order_id).first()
                raise
    
    # ─────────────────────────────────────────────────────────────
    # CLOSE TRADE (On TP/SL/Manual)
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    @retry_on_db_error(max_retries=3)
    def close_live_trade(
        trade_id: Optional[str] = None,
        order_id: Optional[str] = None,
        close_price: Optional[float] = None,
        pnl: Optional[float] = None,
        fee: float = 0.0,
        funding: float = 0.0,
        raw_exchange: Optional[Dict[str, Any]] = None,
        close_reason: Optional[str] = None,
    ) -> Optional[LiveTrade]:
        """
        Close an existing LIVE trade.
        
        Args:
            trade_id: Internal trade ID (UUID)
            order_id: Bitget order ID (alternative lookup)
            close_price: Actual exit price (None if unavailable)
            pnl: Realized PnL (calculated if not provided and price available)
            fee: Trading fees
            funding: Funding rate cost/income (USDT)
            raw_exchange: Raw close order response
        
        Returns:
            Updated LiveTrade or None if not found
        """
        with get_db_session() as db:
            # Find trade by ID or order_id
            if trade_id:
                trade = db.query(LiveTrade).filter(LiveTrade.id == trade_id).first()
            elif order_id:
                trade = db.query(LiveTrade).filter(LiveTrade.order_id == order_id).first()
            else:
                logger.error("[LIVE_DB] close_live_trade requires trade_id or order_id")
                return None
            
            if not trade:
                logger.warning(f"[LIVE_DB] Trade not found: id={trade_id} order_id={order_id}")
                return None
            
            if trade.status == "CLOSED":
                logger.warning(f"[LIVE_DB] Trade already CLOSED: id={trade.id}")
                return trade

            # Strict close price validation: None if <= 0 or None (never artificial 0.0)
            valid_close_price = close_price if (close_price is not None and close_price > 0) else None

            # Calculate PnL if not provided AND no fills have accumulated PnL
            if pnl is None:
                if trade.pnl is not None:
                    # Fills already accumulated PnL (including true 0.0 break-even) — use the existing value
                    pnl = trade.pnl
                    logger.info(f"[LIVE_DB] Using fill-accumulated PnL={pnl} for trade {trade.id}")
                elif valid_close_price is not None:
                    pnl = LiveTrade.calculate_pnl(
                        entry=trade.entry,
                        close_price=valid_close_price,
                        qty=trade.qty,
                        side=trade.side,
                        fee=fee
                    )
                else:
                    # Close price unavailable and no fills accumulated PnL: PnL is unknown (None)
                    pnl = None
            
            # Preserve fill-accumulated fees if caller passes fee=0
            if fee == 0.0 and (trade.fee or 0.0) > 0:
                fee = trade.fee
            
            # Determine result based on actual available data (never artificial "be" on missing price)
            result = LiveTrade.determine_result(
                pnl=pnl,
                entry=trade.entry,
                close_price=valid_close_price,
                tp=trade.tp,
                stop=trade.stop
            )
            
            # Update trade
            trade.status = "CLOSED"
            trade.ts_close = datetime.now(timezone.utc)
            trade.close_price = valid_close_price
            trade.pnl = pnl  # Can be None if genuinely unknown, never fake 0.0
            trade.fee = fee
            trade.funding = (trade.funding or 0.0) + funding  # Accumulate funding
            trade.result = result
            if close_reason:
                trade.close_reason = close_reason
            
            # Append close response to raw_exchange
            if raw_exchange:
                existing_raw = trade.get_raw_exchange() or {}
                existing_raw["close_response"] = raw_exchange
                trade.set_raw_exchange(existing_raw)
            
            db.commit()
            db.refresh(trade)
            
            logger.info(
                f"[LIVE_DB] TRADE CLOSED | id={trade.id} | order_id={trade.order_id}"
                f" | {trade.symbol} {trade.side.upper()}"
                f" | close_price={valid_close_price if valid_close_price is not None else 'UNAVAILABLE'}"
                f" | PnL={pnl if pnl is not None else 'UNKNOWN'} | result={result.upper()}"
            )
            return trade

    @staticmethod
    @retry_on_db_error(max_retries=3)
    def update_trade_quantity(
        order_id: str,
        remaining_qty: float,
        close_order_id: Optional[str] = None,
        close_price: Optional[float] = None,
        broker: str = "kraken",
    ) -> bool:
        """
        Update the open quantity of an active trade following a partial reduction.
        Keeps trade status as OPEN and records the reduction event in raw_exchange metadata.
        Returns True ONLY after successful commit.
        """
        with _trade_db_lock:
            with get_db_session() as db:
                try:
                    try:
                        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    except Exception:
                        pass

                    broker_clean = (broker or "kraken").lower()
                    oid_clean = str(order_id)

                    trade = db.query(LiveTrade).filter(
                        LiveTrade.order_id == oid_clean,
                        LiveTrade.broker == broker_clean,
                    ).first()

                    if not trade:
                        logger.error("[LIVE_DB] update_trade_quantity: trade not found order_id=%s broker=%s", oid_clean, broker_clean)
                        return False

                    if trade.status != "OPEN":
                        logger.warning(
                            "[LIVE_DB] update_trade_quantity: refusing to update non-OPEN trade (id=%s, status=%s)",
                            trade.id, trade.status,
                        )
                        return False

                    old_qty = trade.qty
                    trade.qty = float(remaining_qty)

                    raw = trade.get_raw_exchange() or {}
                    reductions = raw.get("reductions", [])
                    reduction_event = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "old_qty": old_qty,
                        "remaining_qty": float(remaining_qty),
                        "close_order_id": str(close_order_id) if close_order_id else None,
                        "close_price": float(close_price) if close_price is not None else None,
                    }
                    reductions.append(reduction_event)
                    raw["reductions"] = reductions
                    raw["last_reduction"] = reduction_event
                    trade.set_raw_exchange(raw)

                    db.commit()
                    logger.info(
                        "[LIVE_DB] TRADE QUANTITY UPDATED | order_id=%s | symbol=%s | old_qty=%s -> remaining_qty=%s",
                        oid_clean, trade.symbol, old_qty, remaining_qty,
                    )
                    return True
                except Exception as e:
                    logger.critical(
                        "[LIVE_DB] Failed to update trade quantity for order_id=%s: %s",
                        order_id, e,
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    return False
    
    # ─────────────────────────────────────────────────────────────
    # ADD FILL (For reduce/close tracking)
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    @retry_on_db_error(max_retries=3)
    def add_fill(
        trade_id: str,
        action: str,
        qty: float,
        price: float,
        fee: float = 0.0,
        pnl: Optional[float] = None,
        order_id: Optional[str] = None,
        raw_response: Optional[Dict[str, Any]] = None,
        close_reason: Optional[str] = None,
    ) -> Optional[TradeFill]:
        """
        Add a fill record for a trade (OPEN/REDUCE/CLOSE).
        
        - Records fill in trade_fills table
        - Updates aggregated PnL/fee on parent LiveTrade
        - If action=CLOSE, sets trade status=CLOSED
        
        Args:
            trade_id: Parent LiveTrade UUID
            action: "OPEN", "REDUCE", or "CLOSE"
            qty: Fill quantity
            price: Fill price
            fee: Trading fee
            pnl: Realized PnL (for REDUCE/CLOSE)
            order_id: Bitget order ID for this fill
            raw_response: Raw exchange response
        
        Returns:
            Created TradeFill or None
        """
        import json as _json
        
        with get_db_session() as db:
            # 1. Find parent trade
            trade = db.query(LiveTrade).filter(LiveTrade.id == trade_id).first()
            if not trade:
                logger.error(f"[LIVE_DB] add_fill: trade not found, id={trade_id}")
                return None
            
            # 2. Create fill record
            fill = TradeFill(
                trade_id=trade_id,
                order_id=order_id,
                action=action.upper(),
                qty=qty,
                price=price,
                fee=fee,
                pnl=pnl,
                ts=datetime.now(timezone.utc),
                raw_response=_json.dumps(raw_response) if raw_response else None,
            )
            db.add(fill)
            
            # 3. Update aggregated fields on parent trade
            current_pnl = trade.pnl or 0.0
            current_fee = trade.fee or 0.0
            if pnl is not None:
                trade.pnl = current_pnl + pnl
            trade.fee = current_fee + fee
            
            # 4. If CLOSE action, close the trade
            if action.upper() == "CLOSE":
                trade.status = "CLOSED"
                trade.ts_close = datetime.now(timezone.utc)
                trade.close_price = price
                if close_reason:
                    trade.close_reason = close_reason
                trade.result = LiveTrade.determine_result(
                    pnl=trade.pnl,
                    entry=trade.entry,
                    close_price=price,
                    tp=trade.tp,
                    stop=trade.stop,
                )
                logger.info(
                    f"[LIVE_DB] FILL CLOSE | trade={trade_id[:8]}... | {trade.symbol}"
                    f" | total_pnl={trade.pnl if trade.pnl is not None else 'UNKNOWN'} | result={trade.result.upper()}"
                    f" | reason={close_reason or 'N/A'}"
                )
            else:
                logger.info(
                    f"[LIVE_DB] FILL {action.upper()} | trade={trade_id[:8]}... | {trade.symbol}"
                    f" | qty={qty} | pnl={pnl if pnl is not None else 'UNKNOWN'}"
                )
            
            db.commit()
            db.refresh(fill)
            
            # Detach for external use
            db.expunge(fill)
            return fill
    
    @staticmethod
    def get_fills_for_trade(trade_id: str) -> List[TradeFill]:
        """Get all fills for a trade."""
        with get_db_session() as db:
            fills = (
                db.query(TradeFill)
                .filter(TradeFill.trade_id == trade_id)
                .order_by(TradeFill.ts.asc())
                .all()
            )
            for f in fills:
                db.expunge(f)
            return fills
    
    # ─────────────────────────────────────────────────────────────
    # QUERY METHODS
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    def get_open_trades(
        symbol: Optional[str] = None,
        broker: Optional[str] = None,
    ) -> List[LiveTrade]:
        """
        Get all OPEN trades, optionally filtered by symbol and/or broker.
        Used for crash recovery and dashboard sync.
        
        Args:
            symbol: Filter by trading pair
            broker: Filter by broker ("bitget", "kraken", or None for all)
        """
        with get_db_session() as db:
            query = db.query(LiveTrade).filter(LiveTrade.status == "OPEN")
            if symbol:
                query = query.filter(LiveTrade.symbol == symbol.upper())
            if broker:
                query = query.filter(LiveTrade.broker == broker.lower())
            
            trades = query.order_by(LiveTrade.ts_open.desc()).all()
            
            # Detach from session for external use
            for t in trades:
                db.expunge(t)
            
            broker_info = f" broker={broker}" if broker else ""
            symbol_info = f" symbol={symbol}" if symbol else ""
            logger.info(f"[LIVE_DB] Found {len(trades)} OPEN trades{symbol_info}{broker_info}")
            return trades

    @staticmethod
    def get_quarantined_trades(
        symbol: Optional[str] = None,
        broker: Optional[str] = None,
    ) -> List[LiveTrade]:
        """Get all QUARANTINED trades for startup reconciliation."""
        with get_db_session() as db:
            query = db.query(LiveTrade).filter(LiveTrade.status == "QUARANTINED")
            if symbol:
                query = query.filter(LiveTrade.symbol == symbol.upper())
            if broker:
                query = query.filter(LiveTrade.broker == broker.lower())
            trades = query.order_by(LiveTrade.ts_open.desc()).all()
            for t in trades:
                db.expunge(t)
            return trades

    @staticmethod
    @retry_on_db_error(max_retries=3)
    def mark_trade_quarantined(
        symbol: str,
        order_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Mark an open trade as QUARANTINED in SQLite to persist manual reconciliation requirement."""
        with _trade_db_lock:
            with get_db_session() as db:
                try:
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                except Exception:
                    pass
                sym_clean = symbol.upper()
                query = db.query(LiveTrade).filter(
                    (LiveTrade.symbol == sym_clean) | (LiveTrade.symbol == sym_clean.replace("PF_", "").replace("USD", "USDT")),
                    LiveTrade.status == "OPEN"
                )
                if order_id:
                    query = query.filter(LiveTrade.order_id == str(order_id))
                trade = query.first()
                if trade:
                    trade.status = "QUARANTINED"
                    if reason:
                        trade.error_message = (trade.error_message or "") + f" | QUARANTINE: {reason}"
                    db.commit()
                    logger.warning(f"[LIVE_DB] Marked trade {trade.id} on {symbol} as QUARANTINED ({reason})")
                    return True
                db.rollback()
                return False

    @staticmethod
    def record_exchange_quarantine(
        symbol: str,
        order_id: str,
        anomaly_type: str,  # "position" or "order"
        side: Optional[str] = None,
        qty: float = 0.0,
        entry_price: Optional[float] = None,
        client_oid: Optional[str] = None,
        reason: Optional[str] = None,
        broker: str = "kraken",
        raw_exchange: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        P0 (Lot 2.2.3.1): Persist an untracked exchange anomaly (orphan position or order)
        directly into SQLite with status=QUARANTINED, without requiring a pre-existing OPEN trade.
        Returns True ONLY if commit succeeded, False on any rollback or failure.
        """
        with _trade_db_lock:
            with get_db_session() as db:
                try:
                    try:
                        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    except Exception:
                        pass

                    broker_clean = (broker or "kraken").lower()
                    sym_clean = symbol.upper()
                    oid_clean = str(order_id)

                    # Check if a record already exists with this order_id and broker
                    existing = db.query(LiveTrade).filter(
                        LiveTrade.order_id == oid_clean,
                        LiveTrade.broker == broker_clean,
                    ).first()

                    msg_prefix = f"[{anomaly_type.upper()}_ORPHAN]"
                    err_msg = f"{msg_prefix} {reason}" if reason else f"{msg_prefix} Untracked exchange anomaly"

                    raw_payload = dict(raw_exchange) if isinstance(raw_exchange, dict) else {}
                    raw_payload["anomaly_type"] = anomaly_type
                    raw_payload["detected_at"] = datetime.now(timezone.utc).isoformat()

                    if existing:
                        existing.status = "QUARANTINED"
                        if reason:
                            existing.error_message = (existing.error_message or "") + f" | {err_msg}"
                        existing.set_raw_exchange(raw_payload)
                        if client_oid and not existing.client_oid:
                            existing.client_oid = str(client_oid)
                    else:
                        trade = LiveTrade(
                            order_id=oid_clean,
                            broker=broker_clean,
                            symbol=sym_clean,
                            side=(side or "unknown").lower(),
                            qty=float(qty or 0.0),
                            entry=float(entry_price or 0.0),
                            stop=0.0,
                            tp=0.0,
                            leverage=1,
                            status="QUARANTINED",
                            error_message=err_msg,
                            client_oid=str(client_oid) if client_oid else None,
                        )
                        trade.set_raw_exchange(raw_payload)
                        db.add(trade)

                    db.commit()
                    logger.warning(
                        "[LIVE_DB] Persisted %s quarantine in SQLite: symbol=%s order_id=%s (%s)",
                        anomaly_type, sym_clean, oid_clean, reason
                    )
                    return True
                except Exception as e:
                    logger.critical(
                        "[LIVE_DB] CRITICAL: Failed to persist %s quarantine in SQLite for %s (%s): %s",
                        anomaly_type, symbol, order_id, e
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    return False
    
    @staticmethod
    def get_trade_by_order_id(order_id: str, broker: Optional[str] = None) -> Optional[LiveTrade]:
        """Get trade by broker order ID, optionally filtering by broker."""
        with get_db_session() as db:
            query = db.query(LiveTrade).filter(LiveTrade.order_id == order_id)
            if broker:
                query = query.filter(LiveTrade.broker == broker.lower())
            trade = query.first()
            if trade:
                db.expunge(trade)
            return trade
    
    @staticmethod
    def get_trades_by_parent_id(parent_trade_id: str) -> List[LiveTrade]:
        """Get all trades linked by parent_trade_id (mirror trades)."""
        with get_db_session() as db:
            trades = db.query(LiveTrade).filter(
                LiveTrade.parent_trade_id == parent_trade_id
            ).all()
            for t in trades:
                db.expunge(t)
            return trades
    
    @staticmethod
    def get_trade_by_id(trade_id: str) -> Optional[LiveTrade]:
        """Get trade by internal ID."""
        with get_db_session() as db:
            trade = db.query(LiveTrade).filter(LiveTrade.id == trade_id).first()
            if trade:
                db.expunge(trade)
            return trade
    
    @staticmethod
    def get_trades_by_date_range(
        from_date: datetime,
        to_date: datetime,
        status: Optional[str] = None,
        include_archived: bool = False,
    ) -> List[LiveTrade]:
        """Get trades within date range."""
        with get_db_session() as db:
            query = db.query(LiveTrade).filter(
                LiveTrade.ts_open >= from_date,
                LiveTrade.ts_open <= to_date
            )
            if status:
                query = query.filter(LiveTrade.status == status.upper())
            if not include_archived:
                query = query.filter(LiveTrade.archived == False)
            
            trades = query.order_by(LiveTrade.ts_open.desc()).all()
            
            for t in trades:
                db.expunge(t)
            return trades
    

    @staticmethod
    def get_open_trade_for_symbol(
        symbol: str,
        broker: Optional[str] = None,
    ) -> Optional[LiveTrade]:
        """Get single OPEN trade for symbol (for position check), optionally by broker."""
        with get_db_session() as db:
            query = db.query(LiveTrade).filter(
                LiveTrade.symbol == symbol.upper(),
                LiveTrade.status == "OPEN"
            )
            if broker:
                query = query.filter(LiveTrade.broker == broker.lower())
            
            trade = query.first()
            
            if trade:
                db.expunge(trade)
            return trade

    # ─────────────────────────────────────────────────────────────
    # CONVENIENCE HELPERS (Bitget Adapter)
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    async def create_live_trade_from_order(
        order_data: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Optional[LiveTrade]:
        """
        Create a live trade from a broker order response/execution result.
        
        Args:
            order_data: Order result from executor (success=True dict)
            context: Signal context (signal_dict, session, preset, broker, etc.)
            
        Returns:
            Created LiveTrade or None on failure/duplicate
        """
        try:
            # Extract key fields
            order_id = order_data.get("entry_order_id") or order_data.get("orderId")
            if not order_id:
                logger.error("[LIVE_DB] create_from_order: Missing order_id")
                return None
                
            entry_price = float(order_data.get("price_executed") or order_data.get("price") or context.get("entry") or 0)
            qty = float(order_data.get("quantity") or order_data.get("size") or 0)
            
            # Context fields
            symbol = context.get("symbol", "").upper()
            side = context.get("direction", "").lower()
            stop = float(context.get("stop_loss", 0))
            tp = float(context.get("take_profit", 0) or context.get("tp1", 0))
            leverage = int(context.get("leverage", 10))
            
            # Metadata
            session = context.get("session")
            preset = context.get("preset")
            signal_id = str(context.get("signal_id", ""))
            client_oid = str(context.get("client_oid")) if context.get("client_oid") else None
            
            # Multi-broker fields
            broker = context.get("broker", "kraken")
            parent_trade_id = context.get("parent_trade_id")
            
            # Deep validate
            if entry_price <= 0 or qty <= 0:
                logger.error(f"[LIVE_DB] create_from_order: Invalid price/qty for {order_id} ({entry_price}, {qty})")
                return None
                
            return await create_live_trade_async(
                order_id=str(order_id),
                symbol=symbol,
                side=side,
                qty=qty,
                entry=entry_price,
                stop=stop,
                tp=tp,
                leverage=leverage,
                session_name=session,
                preset=preset,
                signal_id=signal_id,
                raw_exchange=order_data,
                broker=broker,
                parent_trade_id=parent_trade_id,
                status=context.get("status", "OPEN"),
                error_message=context.get("error_message"),
                client_oid=client_oid,
            )
            
        except Exception as e:
            logger.error(f"[LIVE_DB] create_from_order FAILED: {e}", exc_info=True)
            return None

    @staticmethod
    async def close_live_trade_from_fill(
        trade_id: Optional[str],
        order_id: Optional[str],
        fill_data: Dict[str, Any]
    ) -> Optional[LiveTrade]:
        """
        Close a live trade from a Bitget fill/position update.
        
        Args:
            trade_id: Internal DB ID (optional)
            order_id: Bitget Order ID (optional, preferred)
            fill_data: Dictionary containing close details (price, pnl, fee)
            
        Returns:
            Updated LiveTrade or None
        """
        try:
            close_price = float(fill_data.get("close_price") or fill_data.get("price") or 0)
            pnl = float(fill_data.get("pnl") or fill_data.get("realizedPnl") or 0)
            fee = float(fill_data.get("fee") or 0)
            close_reason = fill_data.get("close_reason")
            
            # If we don't have PnL but have prices, let the service calculate it
            pnl_arg = pnl if (pnl != 0 or close_price == 0) else None
            
            return await close_live_trade_async(
                trade_id=trade_id,
                order_id=order_id,
                close_price=close_price,
                pnl=pnl_arg,
                fee=fee,
                raw_exchange=fill_data,
                close_reason=close_reason,
            )
            
        except Exception as e:
            logger.error(f"[LIVE_DB] close_from_fill FAILED: {e}", exc_info=True)
            return None

    @staticmethod
    def purge_failed_trades() -> int:
        """Remove all FAILED trades from the database. Returns count deleted."""
        with get_db_session() as db:
            count = db.query(LiveTrade).filter(
                LiveTrade.status == "FAILED"
            ).delete(synchronize_session="fetch")
            db.commit()
            logger.info(f"[LIVE_DB] Purged {count} FAILED trades")
            return count

    # ─────────────────────────────────────────────────────────────
    # SL/TP PROTECTIONS LIFECYCLE (Phase 3.2 - Lot 1)
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    @retry_on_db_error(max_retries=3)
    def record_protection_intent(
        symbol: str,
        protection_type: str,  # "sl" or "tp"
        client_oid: str,
        order_type: str,  # "stp" or "take_profit"
        trigger_price: float,
        placed_qty: float,
        order_id: Optional[str] = None,
        status: str = "INTENT_RECORDED",
        broker: str = "kraken",
        trade_defaults: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Persist a protection (SL/TP) intention to SQLite BEFORE transmitting the order to the exchange.
        Enforces fail-closed safety: if this fails, the caller MUST NOT send the order to Kraken.
        Enforces strict trade association: NEVER falls back to symbol matching.
        Uses BEGIN IMMEDIATE for multi-process SQLite write locking.
        """
        with _trade_db_lock:
            with get_db_session() as db:
                try:
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                except Exception:
                    pass

                sym_clean = symbol.upper()
                trade = None
                if order_id:
                    trade = db.query(LiveTrade).filter(
                        LiveTrade.order_id == str(order_id),
                        LiveTrade.broker == broker.lower()
                    ).first()
                elif trade_defaults and trade_defaults.get("client_oid"):
                    trade = db.query(LiveTrade).filter(
                        LiveTrade.client_oid == str(trade_defaults.get("client_oid")),
                        LiveTrade.broker == broker.lower()
                    ).first()

                now_iso = datetime.now(timezone.utc).isoformat()
                prot_data = {
                    "order_id": None,
                    "client_oid": str(client_oid),
                    "order_type": str(order_type),
                    "trigger_price": float(trigger_price),
                    "placed_qty": float(placed_qty),
                    "filled_qty": 0.0,
                    "status": str(status),
                    "created_at": now_iso,
                    "updated_at": now_iso,
                }

                if not trade:
                    # Create trade on-the-fly ONLY if both trade_defaults AND valid order_id are supplied.
                    # PENDING_* synthetic trades are strictly prohibited.
                    if trade_defaults and order_id:
                        defaults = trade_defaults or {}
                        trade = LiveTrade(
                            order_id=str(order_id),
                            broker=broker.lower(),
                            symbol=sym_clean,
                            side=str(defaults.get("side", "long")).lower(),
                            qty=float(placed_qty),
                            entry=float(defaults.get("entry", 0.0) or 0.0),
                            stop=float(defaults.get("stop", 0.0) or 0.0),
                            tp=float(defaults.get("tp", 0.0) or 0.0),
                            leverage=int(defaults.get("leverage", 10) or 10),
                            status="OPEN",
                            client_oid=defaults.get("client_oid"),
                        )
                        raw = {"protections": {"version": 1, protection_type.lower(): prot_data}}
                        trade.set_raw_exchange(raw)
                        db.add(trade)
                        db.commit()
                        logger.info(
                            f"[LIVE_DB] Created trade {trade.order_id} with {protection_type.upper()} intent "
                            f"(client_oid={client_oid}, trigger={trigger_price})"
                        )
                        return True
                    else:
                        logger.error(
                            f"[LIVE_DB] Cannot record protection intent: no trade found matching order_id={order_id} for {symbol}. "
                            f"Strict association enforced (symbol fallback and PENDING_* trades prohibited)."
                        )
                        db.rollback()
                        db.rollback()
                    return False

                raw = trade.get_raw_exchange() or {}
                protections = raw.get("protections", {})
                if not isinstance(protections, dict):
                    protections = {}
                protections["version"] = 1
                existing_prot = protections.get(protection_type.lower())
                if isinstance(existing_prot, dict):
                    prot_data["created_at"] = existing_prot.get("created_at") or now_iso
                    if existing_prot.get("order_id"):
                        prot_data["order_id"] = existing_prot["order_id"]
                    if existing_prot.get("filled_qty"):
                        prot_data["filled_qty"] = existing_prot["filled_qty"]

                protections[protection_type.lower()] = prot_data
                raw["protections"] = protections
                trade.set_raw_exchange(raw)
                db.commit()
                logger.info(
                    f"[LIVE_DB] Recorded {protection_type.upper()} intent on trade {trade.order_id} "
                    f"(status={status}, client_oid={client_oid}, trigger={trigger_price})"
                )
                return True

    @staticmethod
    @retry_on_db_error(max_retries=3)
    def update_protection_status(
        symbol: str,
        protection_type: str,
        status: str,
        order_id: Optional[str] = None,
        client_oid: Optional[str] = None,
        protection_order_id: Optional[str] = None,
        filled_qty: Optional[float] = None,
        error_message: Optional[str] = None,
        broker: str = "kraken",
    ) -> bool:
        """
        Update the status and order_id of a protection in SQLite.
        Called after exchange returns (ATTACHED, UNCONFIRMED, CANCELLED_CONFIRMED, TRIGGERED, etc.).
        Strict association: lookup strictly by order_id or client_oid or protection_order_id.
        NEVER falls back to symbol matching.
        Uses BEGIN IMMEDIATE for multi-process SQLite write locking.
        """
        with _trade_db_lock:
            with get_db_session() as db:
                try:
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                except Exception:
                    pass

                trade = None
                if order_id:
                    trade = db.query(LiveTrade).filter(
                        LiveTrade.order_id == str(order_id),
                        LiveTrade.broker == broker.lower()
                    ).first()
                if not trade and client_oid:
                    trade = db.query(LiveTrade).filter(
                        LiveTrade.client_oid == str(client_oid),
                        LiveTrade.broker == broker.lower()
                    ).first()
                if not trade and (client_oid or protection_order_id):
                    # Search within raw_exchange protections of active/quarantined trades
                    candidates = db.query(LiveTrade).filter(
                        LiveTrade.broker == broker.lower(),
                        LiveTrade.status.in_(["OPEN", "QUARANTINED"])
                    ).all()
                    for t in candidates:
                        raw_ex = t.get_raw_exchange() or {}
                        if isinstance(raw_ex, dict):
                            prots = raw_ex.get("protections", {})
                            if isinstance(prots, dict):
                                p_data = prots.get(protection_type.lower())
                                if isinstance(p_data, dict):
                                    if client_oid and p_data.get("client_oid") == str(client_oid):
                                        trade = t
                                        break
                                    if protection_order_id and p_data.get("order_id") == str(protection_order_id):
                                        trade = t
                                        break

                if not trade:
                    logger.error(
                        f"[LIVE_DB] Cannot update protection status: no trade found matching "
                        f"order_id={order_id}, client_oid={client_oid}, protection_order_id={protection_order_id} for {symbol}. "
                        f"Strict association enforced (symbol fallback prohibited)."
                    )
                    db.rollback()
                    return False

                raw = trade.get_raw_exchange() or {}
                protections = raw.get("protections", {})
                if not isinstance(protections, dict):
                    protections = {}
                protections["version"] = 1
                prot = protections.get(protection_type.lower())
                if not isinstance(prot, dict):
                    prot = {}

                now_iso = datetime.now(timezone.utc).isoformat()
                prot["status"] = str(status)
                prot["updated_at"] = now_iso
                if protection_order_id:
                    prot["order_id"] = str(protection_order_id)
                if client_oid:
                    prot["client_oid"] = str(client_oid)
                if filled_qty is not None:
                    prot["filled_qty"] = float(filled_qty)
                if error_message:
                    prot["error_message"] = str(error_message)

                protections[protection_type.lower()] = prot
                raw["protections"] = protections
                trade.set_raw_exchange(raw)
                db.commit()
                logger.info(
                    f"[LIVE_DB] Updated {protection_type.upper()} status on trade {trade.order_id} -> {status} "
                    f"(protection_order_id={protection_order_id or prot.get('order_id')})"
                )
                return True

    @staticmethod
    def get_protections(
        symbol: str,
        order_id: Optional[str] = None,
        client_oid: Optional[str] = None,
        broker: str = "kraken",
    ) -> Dict[str, Any]:
        """Get the protections dict stored in raw_exchange for a trade."""
        with _trade_db_lock:
            with get_db_session() as db:
                trade = None
                if order_id:
                    trade = db.query(LiveTrade).filter(
                        LiveTrade.order_id == str(order_id),
                        LiveTrade.broker == broker.lower()
                    ).first()
                if not trade and client_oid:
                    trade = db.query(LiveTrade).filter(
                        LiveTrade.client_oid == str(client_oid),
                        LiveTrade.broker == broker.lower()
                    ).first()
                if not trade and not order_id and not client_oid:
                    sym_clean = symbol.upper()
                    trade = db.query(LiveTrade).filter(
                        (LiveTrade.symbol == sym_clean) | (LiveTrade.symbol == sym_clean.replace("PF_", "").replace("USD", "USDT")),
                        LiveTrade.status.in_(["OPEN", "QUARANTINED"]),
                        LiveTrade.broker == broker.lower()
                    ).order_by(LiveTrade.ts_open.desc()).first()

                if trade:
                    raw = trade.get_raw_exchange() or {}
                    return raw.get("protections", {}) if isinstance(raw, dict) else {}
                return {}

    @staticmethod
    def find_trade_by_protection_client_oid(
        client_oid: str,
        broker: str = "kraken",
    ) -> Optional[LiveTrade]:
        """Find an active/quarantined trade matching a protection client_oid inside raw_exchange JSON."""
        with _trade_db_lock:
            with get_db_session() as db:
                trades = db.query(LiveTrade).filter(
                    LiveTrade.status.in_(["OPEN", "QUARANTINED"]),
                    LiveTrade.broker == broker.lower()
                ).all()
                for t in trades:
                    raw = t.get_raw_exchange() or {}
                    if isinstance(raw, dict):
                        protections = raw.get("protections", {})
                        if isinstance(protections, dict):
                            for p_type in ("sl", "tp"):
                                p_data = protections.get(p_type)
                                if isinstance(p_data, dict) and p_data.get("client_oid") == client_oid:
                                    db.expunge(t)
                                    return t
                return None


# ═══════════════════════════════════════════════════════════════════════════════
# ASYNC WRAPPERS (Non-blocking for event loop)
# ═══════════════════════════════════════════════════════════════════════════════

async def create_live_trade_async(**kwargs) -> Optional[LiveTrade]:
    """Non-blocking wrapper for create_live_trade."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: LiveTradesService.create_live_trade(**kwargs)
    )


async def close_live_trade_async(**kwargs) -> Optional[LiveTrade]:
    """Non-blocking wrapper for close_live_trade."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: LiveTradesService.close_live_trade(**kwargs)
    )


async def get_open_trades_async(symbol: Optional[str] = None) -> List[LiveTrade]:
    """Non-blocking wrapper for get_open_trades."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: LiveTradesService.get_open_trades(symbol)
    )


async def add_fill_async(**kwargs) -> Optional[TradeFill]:
    """Non-blocking wrapper for add_fill."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: LiveTradesService.add_fill(**kwargs)
    )
