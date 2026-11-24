"""
Persisted Positions Service - CRUD and Performance Analytics

Features:
- create_position(): INSERT after order confirmation
- close_position(): UPDATE on TP/SL/Manual close
- get_positions(): Retrieve positions with filters
- calculate_performance(): Calculate all performance metrics
- Thread-safe via session-per-operation pattern

Author: SMC Trading Bot Framework
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any, Tuple
from contextlib import contextmanager
from functools import wraps
from dataclasses import dataclass

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import func, and_, or_, desc, asc

from backend.db.base import Base
from backend.db.session import get_session_factory, get_engine
from backend.models.persisted_position import (
    PersistedPosition, 
    PositionStatus,
    PositionCreate,
    PositionClose,
    PerformanceStats
)

logger = logging.getLogger(__name__)
_custom_session_factory = None


def set_positions_session_factory(session_factory=None) -> None:
    """Explicitly inject or clear the session factory for PersistedPositionsService (tests/DI)."""
    global _custom_session_factory
    _custom_session_factory = session_factory


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

def init_persisted_positions_db(target_engine=None):
    """Create persisted_positions table if not exists."""
    try:
        eng = target_engine or get_engine()
        Base.metadata.create_all(
            bind=eng,
            tables=[PersistedPosition.__table__]
        )
        logger.info("[POSITIONS_DB] persisted_positions table initialized")
    except Exception as e:
        logger.error(f"[POSITIONS_DB] Failed to initialize: {e}")
        raise


# ═══════════════════════════════════════════════════════════════════════════════
# SESSION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@contextmanager
def get_db_session(session_factory=None):
    """Context manager for database sessions with auto-rollback."""
    factory = session_factory or _custom_session_factory or get_session_factory()
    db = factory()
    try:
        yield db
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"[POSITIONS_DB] Session error, rolling back: {e}")
        raise
    finally:
        db.close()


def retry_on_db_error(max_retries: int = 3, delay: float = 0.1):
    """Decorator for retrying database operations."""
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
                        f"[POSITIONS_DB] Retry {attempt + 1}/{max_retries} for {func.__name__}: {e}"
                    )
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(delay * (attempt + 1))
            raise last_error
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
# PERFORMANCE CALCULATOR
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PerformanceMetrics:
    """Performance metrics container."""
    total_pnl: float = 0.0
    win_rate: float = 0.0
    avg_rr: float = 0.0
    total_trades: int = 0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0
    wins: int = 0
    losses: int = 0
    break_even: int = 0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    avg_hold_time_hours: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_pnl": round(self.total_pnl, 2),
            "win_rate": round(self.win_rate, 4),
            "avg_rr": round(self.avg_rr, 2),
            "total_trades": self.total_trades,
            "max_drawdown": round(self.max_drawdown, 2),
            "profit_factor": round(self.profit_factor, 2),
            "wins": self.wins,
            "losses": self.losses,
            "break_even": self.break_even,
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "best_trade": round(self.best_trade, 2),
            "worst_trade": round(self.worst_trade, 2),
            "avg_hold_time_hours": round(self.avg_hold_time_hours, 2),
        }


class PerformanceCalculator:
    """Calculate performance metrics from positions list."""
    
    @staticmethod
    def calculate(positions: List[PersistedPosition]) -> PerformanceMetrics:
        """
        Calculate comprehensive performance metrics.
        
        Args:
            positions: List of CLOSED positions
            
        Returns:
            PerformanceMetrics with all calculated values
        """
        metrics = PerformanceMetrics()
        
        if not positions:
            return metrics
        
        # Filter only closed positions
        closed = [p for p in positions if p.status == PositionStatus.CLOSED.value]
        
        if not closed:
            return metrics
        
        metrics.total_trades = len(closed)
        
        # PnL calculations
        pnls = [p.pnl or 0.0 for p in closed]
        metrics.total_pnl = sum(pnls)
        
        # Win/Loss classification
        winning = [p for p in closed if (p.pnl or 0) > 0.01]
        losing = [p for p in closed if (p.pnl or 0) < -0.01]
        breakeven = [p for p in closed if abs(p.pnl or 0) <= 0.01]
        
        metrics.wins = len(winning)
        metrics.losses = len(losing)
        metrics.break_even = len(breakeven)
        
        # Win rate (excluding break-even)
        decisive = metrics.wins + metrics.losses
        metrics.win_rate = metrics.wins / decisive if decisive > 0 else 0.0
        
        # Average R:R
        rrs = [p.rr for p in closed if p.rr is not None]
        metrics.avg_rr = sum(rrs) / len(rrs) if rrs else 0.0
        
        # Profit factor
        gross_profit = sum(p.pnl for p in winning if p.pnl)
        gross_loss = abs(sum(p.pnl for p in losing if p.pnl))
        metrics.profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
            gross_profit if gross_profit > 0 else 0.0
        )
        
        # Average win/loss
        metrics.avg_win = gross_profit / len(winning) if winning else 0.0
        metrics.avg_loss = gross_loss / len(losing) if losing else 0.0
        
        # Best/Worst trade
        metrics.best_trade = max(pnls) if pnls else 0.0
        metrics.worst_trade = min(pnls) if pnls else 0.0
        
        # Maximum drawdown (cumulative equity curve)
        sorted_positions = sorted(closed, key=lambda p: p.exit_time or p.entry_time)
        cum_pnl = 0.0
        peak = 0.0
        max_dd = 0.0
        
        for p in sorted_positions:
            cum_pnl += (p.pnl or 0)
            peak = max(peak, cum_pnl)
            dd = peak - cum_pnl
            max_dd = max(max_dd, dd)
        
        metrics.max_drawdown = max_dd
        
        # Average hold time
        hold_times = []
        for p in closed:
            if p.entry_time and p.exit_time:
                delta = p.exit_time - p.entry_time
                hold_times.append(delta.total_seconds() / 3600)  # hours
        
        metrics.avg_hold_time_hours = sum(hold_times) / len(hold_times) if hold_times else 0.0
        
        return metrics


# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTED POSITIONS SERVICE
# ═══════════════════════════════════════════════════════════════════════════════

class PersistedPositionsService:
    """
    Service for persisted position CRUD and analytics.
    
    All methods are static for easy module-level access.
    Thread-safe via session-per-operation pattern.
    """
    
    # ─────────────────────────────────────────────────────────────
    # CREATE POSITION
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    @retry_on_db_error(max_retries=3)
    def create_position(data: PositionCreate) -> Optional[PersistedPosition]:
        """
        Create a new position record.
        
        Args:
            data: PositionCreate schema with position details
            
        Returns:
            Created PersistedPosition or None on failure
        """
        with get_db_session() as db:
            try:
                # Calculate target R:R if SL and TP provided
                rr_target = 0.0
                if data.stop_loss and data.take_profit:
                    rr_target = PersistedPosition.calculate_rr(
                        data.entry_price,
                        data.stop_loss,
                        data.take_profit,
                        data.side
                    )
                
                # Calculate notional
                notional = data.size * data.entry_price
                
                position = PersistedPosition(
                    broker=data.broker,
                    symbol=data.symbol.upper(),
                    side=data.side,
                    size=data.size,
                    entry_price=data.entry_price,
                    entry_time=datetime.now(timezone.utc),
                    leverage=data.leverage,
                    notional=notional,
                    stop_loss=data.stop_loss,
                    take_profit=data.take_profit,
                    rr_target=rr_target,
                    order_id=data.order_id,
                    session_name=data.session_name,
                    preset=data.preset,
                    signal_id=data.signal_id,
                    notes=data.notes,
                    status=data.status,
                    error_message=data.error_message,
                )
                
                if data.raw_response:
                    position.set_raw_entry(data.raw_response)
                
                db.add(position)
                db.commit()
                db.refresh(position)
                
                logger.info(
                    f"[POSITIONS] Created: {position.id} | {position.broker} | "
                    f"{position.symbol} {position.side} @ {position.entry_price}"
                )
                
                return position
                
            except IntegrityError as e:
                db.rollback()
                logger.warning(f"[POSITIONS] Duplicate position: {e}")
                return None
            except Exception as e:
                db.rollback()
                logger.error(f"[POSITIONS] Create error: {e}")
                raise
    
    # ─────────────────────────────────────────────────────────────
    # CLOSE POSITION
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    @retry_on_db_error(max_retries=3)
    def close_position(
        position_id: str,
        exit_price: float,
        fee: float = 0.0,
        close_reason: str = "MANUAL",
        raw_response: Optional[Dict[str, Any]] = None
    ) -> Optional[PersistedPosition]:
        """
        Close an existing position.
        
        Args:
            position_id: Position ID to close
            exit_price: Exit/close price
            fee: Trading fee for exit
            close_reason: Reason for close
            raw_response: Raw exchange response
            
        Returns:
            Updated PersistedPosition or None if not found
        """
        with get_db_session() as db:
            try:
                position = db.query(PersistedPosition).filter(
                    PersistedPosition.id == position_id
                ).first()
                
                if not position:
                    logger.warning(f"[POSITIONS] Position not found: {position_id}")
                    return None
                
                if position.status == PositionStatus.CLOSED.value:
                    logger.warning(f"[POSITIONS] Position already closed: {position_id}")
                    return position
                
                # Use the model's close method
                position.close_position(
                    exit_price=exit_price,
                    fee=fee,
                    close_reason=close_reason,
                    raw_response=raw_response
                )
                
                db.commit()
                db.refresh(position)
                
                logger.info(
                    f"[POSITIONS] Closed: {position.id} | PnL={position.pnl:.2f} | "
                    f"Result={position.result} | Reason={close_reason}"
                )
                
                return position
                
            except Exception as e:
                db.rollback()
                logger.error(f"[POSITIONS] Close error: {e}")
                raise
    
    # ─────────────────────────────────────────────────────────────
    # CLOSE BY ORDER ID
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    @retry_on_db_error(max_retries=3)
    def close_by_order_id(
        order_id: str,
        exit_price: float,
        fee: float = 0.0,
        close_reason: str = "MANUAL",
        raw_response: Optional[Dict[str, Any]] = None
    ) -> Optional[PersistedPosition]:
        """Close position by exchange order ID."""
        with get_db_session() as db:
            try:
                position = db.query(PersistedPosition).filter(
                    PersistedPosition.order_id == order_id,
                    PersistedPosition.status == PositionStatus.OPEN.value
                ).first()
                
                if not position:
                    logger.warning(f"[POSITIONS] No open position with order_id: {order_id}")
                    return None
                
                position.close_position(
                    exit_price=exit_price,
                    fee=fee,
                    close_reason=close_reason,
                    raw_response=raw_response
                )
                
                db.commit()
                db.refresh(position)
                
                logger.info(
                    f"[POSITIONS] Closed by order_id: {order_id} | PnL={position.pnl:.2f}"
                )
                
                return position
                
            except Exception as e:
                db.rollback()
                logger.error(f"[POSITIONS] Close by order_id error: {e}")
                raise
    
    # ─────────────────────────────────────────────────────────────
    # GET POSITIONS
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    def get_positions(
        broker: Optional[str] = None,
        status: Optional[str] = None,
        symbol: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "entry_time",
        order_desc: bool = True
    ) -> Tuple[List[PersistedPosition], int]:
        """
        Get positions with filters.
        
        Args:
            broker: Filter by broker (bitget/kraken)
            status: Filter by status (OPEN/CLOSED)
            symbol: Filter by symbol
            date_from: Filter by entry_time >= date_from
            date_to: Filter by entry_time <= date_to
            limit: Maximum results
            offset: Pagination offset
            order_by: Column to sort by
            order_desc: Sort descending
            
        Returns:
            Tuple of (positions list, total count)
        """
        with get_db_session() as db:
            query = db.query(PersistedPosition)
            
            # Apply filters
            if broker:
                query = query.filter(PersistedPosition.broker == broker.lower())
            
            if status:
                query = query.filter(PersistedPosition.status == status.upper())
            
            if symbol:
                query = query.filter(PersistedPosition.symbol == symbol.upper())
            
            if date_from:
                query = query.filter(PersistedPosition.entry_time >= date_from)
            
            if date_to:
                query = query.filter(PersistedPosition.entry_time <= date_to)
            
            # Filter out archived positions by default unless explicitly requested
            if not include_archived and status not in ("FAILED", "ARCHIVED"):
                query = query.filter(PersistedPosition.archived == 0)
            
            # Get total count before pagination
            total = query.count()
            
            # Apply ordering
            order_column = getattr(PersistedPosition, order_by, PersistedPosition.entry_time)
            if order_desc:
                query = query.order_by(desc(order_column))
            else:
                query = query.order_by(asc(order_column))
            
            # Apply pagination
            positions = query.offset(offset).limit(limit).all()
            
            return positions, total
    
    # ─────────────────────────────────────────────────────────────
    # GET SINGLE POSITION
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    def get_position(position_id: str) -> Optional[PersistedPosition]:
        """Get a single position by ID."""
        with get_db_session() as db:
            return db.query(PersistedPosition).filter(
                PersistedPosition.id == position_id
            ).first()
    
    # ─────────────────────────────────────────────────────────────
    # GET OPEN POSITIONS
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    def get_open_positions(broker: Optional[str] = None) -> List[PersistedPosition]:
        """Get all open positions, optionally filtered by broker."""
        with get_db_session() as db:
            query = db.query(PersistedPosition).filter(
                PersistedPosition.status == PositionStatus.OPEN.value
            )
            
            if broker:
                query = query.filter(PersistedPosition.broker == broker.lower())
            
            return query.order_by(desc(PersistedPosition.entry_time)).all()
    
    # ─────────────────────────────────────────────────────────────
    # CALCULATE PERFORMANCE
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    def calculate_performance(
        broker: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None
    ) -> PerformanceMetrics:
        """
        Calculate performance metrics for closed positions.
        
        Args:
            broker: Filter by broker
            date_from: Filter by exit_time >= date_from
            date_to: Filter by exit_time <= date_to
            
        Returns:
            PerformanceMetrics with calculated values
        """
        with get_db_session() as db:
            query = db.query(PersistedPosition).filter(
                PersistedPosition.status == PositionStatus.CLOSED.value
            )
            
            if broker:
                query = query.filter(PersistedPosition.broker == broker.lower())
            
            if date_from:
                query = query.filter(PersistedPosition.exit_time >= date_from)
            
            if date_to:
                query = query.filter(PersistedPosition.exit_time <= date_to)
            
            positions = query.order_by(asc(PersistedPosition.exit_time)).all()
            
            return PerformanceCalculator.calculate(positions)
    
    # ─────────────────────────────────────────────────────────────
    # GET PERFORMANCE BY BROKER
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    def get_performance_by_broker(
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None
    ) -> Dict[str, PerformanceMetrics]:
        """
        Get performance metrics grouped by broker.
        
        Returns:
            Dict with broker name as key and PerformanceMetrics as value
        """
        result = {}
        
        for broker in ["bitget", "kraken"]:
            metrics = PersistedPositionsService.calculate_performance(
                broker=broker,
                date_from=date_from,
                date_to=date_to
            )
            result[broker] = metrics
        
        return result
    
    # ─────────────────────────────────────────────────────────────
    # GET POSITIONS COUNT
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    def get_counts(broker: Optional[str] = None) -> Dict[str, int]:
        """Get position counts by status."""
        with get_db_session() as db:
            query = db.query(PersistedPosition)
            
            if broker:
                query = query.filter(PersistedPosition.broker == broker.lower())
            
            total = query.count()
            open_count = query.filter(
                PersistedPosition.status == PositionStatus.OPEN.value
            ).count()
            closed_count = query.filter(
                PersistedPosition.status == PositionStatus.CLOSED.value
            ).count()
            
            return {
                "total": total,
                "open": open_count,
                "closed": closed_count,
            }
    
    # ─────────────────────────────────────────────────────────────
    # DELETE POSITION (for testing/cleanup)
    # ─────────────────────────────────────────────────────────────
    
    @staticmethod
    def delete_position(position_id: str) -> bool:
        """Delete a position by ID. Returns True if deleted."""
        with get_db_session() as db:
            try:
                result = db.query(PersistedPosition).filter(
                    PersistedPosition.id == position_id
                ).delete()
                db.commit()
                return result > 0
            except Exception as e:
                db.rollback()
                logger.error(f"[POSITIONS] Delete error: {e}")
                return False

    @staticmethod
    def purge_failed_positions() -> int:
        """Remove all FAILED positions from the database. Returns count deleted."""
        with get_db_session() as db:
            count = db.query(PersistedPosition).filter(
                PersistedPosition.status == "FAILED"
            ).delete(synchronize_session="fetch")
            db.commit()
            logger.info(f"[POSITIONS] Purged {count} FAILED positions")
            return count


# ═══════════════════════════════════════════════════════════════════════════════
# SINGLETON SERVICE INSTANCE
# ═══════════════════════════════════════════════════════════════════════════════

_service_instance: Optional[PersistedPositionsService] = None


def reset_positions_service() -> None:
    """Reset the singleton positions service and injected session factory (for tests)."""
    global _service_instance, _custom_session_factory
    _service_instance = None
    _custom_session_factory = None


def _extract_engine_from_session_factory(session_factory):
    """Extract the bound SQLAlchemy Engine/Connection from a sessionmaker, if present."""
    if session_factory is None:
        return None
    kw = getattr(session_factory, "kw", None)
    if isinstance(kw, dict) and kw.get("bind") is not None:
        return kw["bind"]
    bind = getattr(session_factory, "bind", None)
    if bind is not None:
        return bind
    return None


def get_positions_service(
    session_factory=None,
    target_engine=None,
    initialize_schema: bool = True,
) -> PersistedPositionsService:
    """Get or create the singleton service instance (with optional DI)."""
    global _service_instance
    if session_factory is not None:
        factory_bind = _extract_engine_from_session_factory(session_factory)
        if (
            target_engine is not None
            and factory_bind is not None
            and factory_bind is not target_engine
        ):
            raise ValueError(
                "Conflicting engines passed to get_positions_service(): "
                "session_factory bind does not match target_engine."
            )
        resolved_engine = target_engine if target_engine is not None else factory_bind
        if initialize_schema and resolved_engine is None:
            raise ValueError(
                "get_positions_service(session_factory=..., initialize_schema=True) "
                "requires a bound session_factory or an explicit target_engine; "
                "silent fallback to global get_engine() is forbidden."
            )
        set_positions_session_factory(session_factory)
        if initialize_schema:
            init_persisted_positions_db(target_engine=resolved_engine)
        return PersistedPositionsService()
    
    if _service_instance is None:
        if initialize_schema:
            init_persisted_positions_db(target_engine=target_engine)
        _service_instance = PersistedPositionsService()
        logger.info("[POSITIONS] Service initialized")
    
    return _service_instance


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def record_position_open(
    broker: str,
    symbol: str,
    side: str,
    size: float,
    entry_price: float,
    leverage: int = 10,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    order_id: Optional[str] = None,
    session_name: Optional[str] = None,
    preset: Optional[str] = None,
    signal_id: Optional[str] = None,
    status: str = "OPEN",
    error_message: Optional[str] = None,
    raw_response: Optional[Dict[str, Any]] = None,
) -> Optional[PersistedPosition]:
    """
    Convenience function to record a new position open.
    
    Use this in trade execution hooks.
    """
    service = get_positions_service()
    
    data = PositionCreate(
        broker=broker,
        symbol=symbol,
        side=side,
        size=size,
        entry_price=entry_price,
        leverage=leverage,
        stop_loss=stop_loss,
        take_profit=take_profit,
        order_id=order_id,
        session_name=session_name,
        preset=preset,
        signal_id=signal_id,
        status=status,
        error_message=error_message,
        raw_response=raw_response,
    )
    
    return service.create_position(data)


def record_position_close(
    position_id: Optional[str] = None,
    order_id: Optional[str] = None,
    exit_price: float = 0.0,
    fee: float = 0.0,
    close_reason: str = "MANUAL",
    raw_response: Optional[Dict[str, Any]] = None,
) -> Optional[PersistedPosition]:
    """
    Convenience function to record a position close.
    
    Use this in trade close hooks.
    Either position_id or order_id must be provided.
    """
    service = get_positions_service()
    
    if position_id:
        return service.close_position(
            position_id=position_id,
            exit_price=exit_price,
            fee=fee,
            close_reason=close_reason,
            raw_response=raw_response
        )
    elif order_id:
        return service.close_by_order_id(
            order_id=order_id,
            exit_price=exit_price,
            fee=fee,
            close_reason=close_reason,
            raw_response=raw_response
        )
    else:
        logger.error("[POSITIONS] Either position_id or order_id required")
        return None
