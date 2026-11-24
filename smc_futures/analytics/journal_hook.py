"""
Trade Journal Hook — Bridge between close paths and InstitutionalTradeJournal.
═══════════════════════════════════════════════════════════════════════════════

Called by:
  - close_position_service  (TP, SL, manual, signal reversal, …)
  - order_manager            (emergency close, slippage kill)
  - kill_switch              (L3 force close)
  - drawdown_governor        (critical tier force close)

Author: Chantier 5 — Trade Journal Institutionnel
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

from smc_futures.analytics.exit_reason import normalize as normalize_exit
from smc_futures.analytics.trade_journal import InstitutionalTradeJournal, TradeRecord

logger = logging.getLogger("smarttrade.analytics.hook")

# Module-level reference set by AppContext at boot
_journal: Optional[InstitutionalTradeJournal] = None


def set_journal(journal: InstitutionalTradeJournal) -> None:
    """Register the journal instance (called once at startup)."""
    global _journal
    _journal = journal
    logger.info("[JOURNAL_HOOK] Journal registered: strict=%s", journal.strict)


def get_journal() -> Optional[InstitutionalTradeJournal]:
    """Get the active journal (for reports)."""
    return _journal


def record_close(
    *,
    symbol: str,
    side: str,
    entry_price: float,
    exit_price: float,
    qty: float,
    pnl: float,
    fee: float = 0.0,
    reason: str = "UNKNOWN",
    broker: str = "bitget",
    trade_id: Optional[str] = None,
    order_id: Optional[str] = None,
    # Optional enrichment
    strategy_id: str = "",
    setup_id: Optional[str] = None,
    regime_at_entry: Optional[str] = None,
    regime_at_exit: Optional[str] = None,
    signal_score: Optional[float] = None,
    confidence_score: Optional[float] = None,
    entry_price_expected: Optional[float] = None,
    exit_price_expected: Optional[float] = None,
    qty_requested: Optional[float] = None,
    leverage: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
    take_profit_price: Optional[float] = None,
    risk_amount: Optional[float] = None,
    rr_planned: Optional[float] = None,
    account_equity_at_entry: Optional[float] = None,
    account_equity_at_exit: Optional[float] = None,
    kill_switch_state: Optional[str] = None,
    drawdown_governor_state: Optional[str] = None,
    timestamp_open: Optional[str] = None,
    timestamp_close: Optional[str] = None,
    candles: Optional[Sequence[dict]] = None,
    exchange_errors_seen: Optional[int] = None,
    notes: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Record a closed trade in the institutional journal.

    Returns True if recorded, False if journal unavailable or rejected.
    """
    if _journal is None:
        logger.warning(
            "[JOURNAL_HOOK] No journal registered — trade NOT recorded: %s %s",
            symbol, reason,
        )
        return False

    now = datetime.now(timezone.utc).isoformat()

    rec = TradeRecord(
        trade_id=trade_id or "",
        timestamp_open=timestamp_open,
        timestamp_close=timestamp_close or now,
        symbol=symbol,
        broker=broker,
        side=side,
        strategy_id=strategy_id,
        setup_id=setup_id,
        regime_at_entry=regime_at_entry,
        regime_at_exit=regime_at_exit,
        signal_score=signal_score,
        confidence_score=confidence_score,
        entry_price_expected=entry_price_expected,
        entry_price_filled=entry_price,
        exit_price_expected=exit_price_expected,
        exit_price_filled=exit_price,
        qty_requested=qty_requested,
        qty_filled=qty,
        leverage=leverage,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        exit_reason=reason,
        gross_pnl=pnl,
        fees=fee,
        risk_amount_at_entry=risk_amount,
        rr_planned=rr_planned,
        account_equity_at_entry=account_equity_at_entry,
        account_equity_at_exit=account_equity_at_exit,
        kill_switch_state=kill_switch_state,
        drawdown_governor_state=drawdown_governor_state,
        exchange_errors_seen=exchange_errors_seen,
        notes=notes,
    )

    return _journal.record_trade(rec, candles=candles)
