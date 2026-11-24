"""
Institutional Trade Journal — Single source of truth for closed trades.
═══════════════════════════════════════════════════════════════════════

Every trade that closes MUST be recorded here with a strict schema.
Missing data is explicitly flagged, never fabricated.

Storage: append-only JSONL (``logs/trade_journal_v2.jsonl``).
No DB dependency — works standalone.

Env:
  TRADE_JOURNAL_V2_PATH  — output file (default: logs/trade_journal_v2.jsonl)
  TRADE_JOURNAL_STRICT   — "true" to reject incomplete records (default: false)

Author: Chantier 5 — Trade Journal Institutionnel
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from smc_futures.analytics.exit_reason import ExitReason, normalize as normalize_exit
from smc_futures.analytics.mae_mfe import (
    ExcursionResult,
    compute_excursions,
    compute_slippage_bps,
)

logger = logging.getLogger("smarttrade.analytics.journal")

_DEFAULT_PATH = "logs/trade_journal_v2.jsonl"

# ── Required fields that must be non-None in strict mode ─────────────────────
_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "trade_id",
    "timestamp_open",
    "timestamp_close",
    "symbol",
    "side",
    "entry_price_filled",
    "exit_price_filled",
    "qty_filled",
    "exit_reason",
    "gross_pnl",
})


# ═════════════════════════════════════════════════════════════════════════════
# TRADE RECORD
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    """Strict schema for a completed trade — institutional grade."""

    # ── Identity ─────────────────────────────────────────────────────────────
    trade_id: str = ""
    timestamp_open: Optional[str] = None   # ISO-8601
    timestamp_close: Optional[str] = None  # ISO-8601
    symbol: str = ""
    broker: str = ""
    side: str = ""                         # "long" / "short"
    strategy_id: str = ""
    setup_id: Optional[str] = None

    # ── Regime context ───────────────────────────────────────────────────────
    regime_at_entry: Optional[str] = None
    regime_at_exit: Optional[str] = None

    # ── Signal quality ───────────────────────────────────────────────────────
    signal_score: Optional[float] = None
    confidence_score: Optional[float] = None

    # ── Prices ───────────────────────────────────────────────────────────────
    entry_price_expected: Optional[float] = None
    entry_price_filled: Optional[float] = None
    exit_price_expected: Optional[float] = None
    exit_price_filled: Optional[float] = None

    # ── Quantities ───────────────────────────────────────────────────────────
    qty_requested: Optional[float] = None
    qty_filled: Optional[float] = None
    leverage: Optional[float] = None

    # ── Risk levels ──────────────────────────────────────────────────────────
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None

    # ── Exit ─────────────────────────────────────────────────────────────────
    exit_reason: str = ExitReason.UNKNOWN.value

    # ── PnL ──────────────────────────────────────────────────────────────────
    gross_pnl: Optional[float] = None
    net_pnl: Optional[float] = None
    fees: Optional[float] = None

    # ── Execution quality ────────────────────────────────────────────────────
    slippage_entry_bps: Optional[float] = None
    slippage_exit_bps: Optional[float] = None
    holding_time_sec: Optional[float] = None

    # ── Excursions (computed) ────────────────────────────────────────────────
    mae: Optional[float] = None
    mfe: Optional[float] = None
    max_drawdown_during_trade: Optional[float] = None
    excursion_ratio: Optional[float] = None
    capture_ratio: Optional[float] = None
    excursion_data_quality: str = "unavailable"

    # ── Risk context ─────────────────────────────────────────────────────────
    risk_amount_at_entry: Optional[float] = None
    rr_planned: Optional[float] = None
    rr_realized: Optional[float] = None
    account_equity_at_entry: Optional[float] = None
    account_equity_at_exit: Optional[float] = None

    # ── System state ─────────────────────────────────────────────────────────
    kill_switch_state: Optional[str] = None
    drawdown_governor_state: Optional[str] = None
    protection_repair_count: Optional[int] = None
    exchange_errors_seen: Optional[int] = None

    # ── Metadata ─────────────────────────────────────────────────────────────
    notes: Optional[Dict[str, Any]] = None

    # ── Helpers ──────────────────────────────────────────────────────────────

    def compute_derived(
        self,
        candles: Optional[Sequence[dict]] = None,
    ) -> None:
        """Fill computed fields: slippage, excursion, RR, holding time."""
        # Slippage
        if self.entry_price_expected and self.entry_price_filled:
            self.slippage_entry_bps = compute_slippage_bps(
                self.entry_price_expected, self.entry_price_filled
            )
        if self.exit_price_expected and self.exit_price_filled:
            self.slippage_exit_bps = compute_slippage_bps(
                self.exit_price_expected, self.exit_price_filled
            )

        # MAE / MFE
        if self.entry_price_filled and self.exit_price_filled and self.side:
            exc = compute_excursions(
                side=self.side,
                entry_price=self.entry_price_filled,
                exit_price=self.exit_price_filled,
                candles=candles,
                realized_pnl=self.gross_pnl,
            )
            self.mae = exc.mae
            self.mfe = exc.mfe
            self.max_drawdown_during_trade = exc.max_dd_during_trade
            self.excursion_ratio = exc.excursion_ratio
            self.capture_ratio = exc.capture_ratio
            self.excursion_data_quality = exc.data_quality

        # RR realized
        if self.gross_pnl is not None and self.risk_amount_at_entry:
            if self.risk_amount_at_entry > 0:
                self.rr_realized = round(
                    self.gross_pnl / self.risk_amount_at_entry, 4
                )

        # Holding time
        if self.timestamp_open and self.timestamp_close:
            try:
                t_open = datetime.fromisoformat(self.timestamp_open)
                t_close = datetime.fromisoformat(self.timestamp_close)
                self.holding_time_sec = round(
                    (t_close - t_open).total_seconds(), 2
                )
            except (ValueError, TypeError):
                pass

        # Net PnL
        if self.gross_pnl is not None:
            self.net_pnl = round(
                self.gross_pnl - (self.fees or 0.0), 8
            )

    def missing_required(self) -> List[str]:
        """Return list of required fields that are None or empty."""
        missing = []
        d = asdict(self)
        for f in _REQUIRED_FIELDS:
            val = d.get(f)
            if val is None or val == "" or val == 0:
                # gross_pnl == 0 is valid (break-even), so only reject None
                if f == "gross_pnl" and val == 0:
                    continue
                if f == "qty_filled" and val == 0:
                    missing.append(f)
                    continue
                if val is None or val == "":
                    missing.append(f)
        return missing

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON storage."""
        return asdict(self)


# ═════════════════════════════════════════════════════════════════════════════
# TRADE JOURNAL
# ═════════════════════════════════════════════════════════════════════════════

class InstitutionalTradeJournal:
    """
    Append-only trade journal with strict validation.

    Thread-safe.  No DB dependency.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        strict: bool = False,
    ):
        self._path = Path(
            path or os.getenv("TRADE_JOURNAL_V2_PATH", _DEFAULT_PATH)
        )
        self._strict = strict or _bool(
            os.getenv("TRADE_JOURNAL_STRICT", "false")
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._records: List[TradeRecord] = []
        self._errors: List[Dict[str, Any]] = []

    # ── Public API ───────────────────────────────────────────────────────────

    def record_trade(
        self,
        rec: TradeRecord,
        candles: Optional[Sequence[dict]] = None,
    ) -> bool:
        """
        Validate, enrich and persist a closed trade.

        Returns True if recorded, False if rejected (strict mode).
        """
        # Auto-generate trade_id if missing
        if not rec.trade_id:
            rec.trade_id = str(uuid.uuid4())

        # Auto-set close timestamp if missing
        if not rec.timestamp_close:
            rec.timestamp_close = datetime.now(timezone.utc).isoformat()

        # Normalize exit reason
        rec.exit_reason = normalize_exit(rec.exit_reason).value

        # Compute derived fields
        rec.compute_derived(candles=candles)

        # Validate
        missing = rec.missing_required()
        if missing:
            err = {
                "trade_id": rec.trade_id,
                "missing_fields": missing,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._errors.append(err)
            logger.error(
                "[JOURNAL] Missing required fields: %s for trade %s",
                missing,
                rec.trade_id,
            )
            if self._strict:
                logger.critical(
                    "[JOURNAL] STRICT MODE — trade REJECTED: %s missing %s",
                    rec.trade_id,
                    missing,
                )
                return False

        # Persist
        self._records.append(rec)
        self._write_line(rec)

        logger.info(
            "[JOURNAL] Trade recorded: %s %s %s pnl=%.4f exit=%s",
            rec.trade_id[:8],
            rec.symbol,
            rec.side,
            rec.gross_pnl or 0.0,
            rec.exit_reason,
        )
        return True

    @property
    def records(self) -> List[TradeRecord]:
        """In-memory records (current session)."""
        return list(self._records)

    @property
    def errors(self) -> List[Dict[str, Any]]:
        """Validation errors encountered."""
        return list(self._errors)

    @property
    def count(self) -> int:
        return len(self._records)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def strict(self) -> bool:
        return self._strict

    def load_from_file(self) -> List[TradeRecord]:
        """Load all records from the JSONL file (for reports)."""
        records: List[TradeRecord] = []
        if not self._path.exists():
            return records
        with open(self._path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    rec = _dict_to_record(data)
                    records.append(rec)
                except (json.JSONDecodeError, Exception) as e:
                    logger.warning(
                        "[JOURNAL] Corrupt line %d: %s", line_num, e
                    )
        return records

    # ── Internal ─────────────────────────────────────────────────────────────

    def _write_line(self, rec: TradeRecord) -> None:
        """Append one JSON line to the file."""
        line = json.dumps(rec.to_dict(), default=str)
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                logger.error("[JOURNAL] Write failed: %s", e)


def _dict_to_record(data: dict) -> TradeRecord:
    """Construct a TradeRecord from a dict, ignoring unknown keys."""
    valid_keys = {f.name for f in fields(TradeRecord)}
    filtered = {k: v for k, v in data.items() if k in valid_keys}
    return TradeRecord(**filtered)


def _bool(val: str) -> bool:
    return val.strip().lower() in ("true", "1", "yes")
