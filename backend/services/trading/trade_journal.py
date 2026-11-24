"""
Trade Journal Service — Full Decision Audit Trail
═════════════════════════════════════════════════

Persists every trading decision with complete context:
  - raw signal + confidence
  - regime at entry
  - setup score
  - risk decision
  - sizing result
  - execution outcome
  - P&L result + exit reason

Storage: append-only JSONL file (one line per entry).
Export: JSON array or CSV on demand.

Thread-safe, no external DB required, works in-memory with optional flush.

Config via .env:
  JOURNAL_ENABLED=true
  JOURNAL_FILE=data/trade_journal.jsonl
  JOURNAL_MAX_MEMORY=2000
  JOURNAL_AUTO_FLUSH=true
"""

import csv
import io
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from backend.services.trading.adaptive_models import (
    JournalEntry,
    PolicyDecision,
    RegimeSnapshot,
    SetupScore,
)
from backend.services.trading.models import (
    ExecutionPlan,
    RiskVerdict,
    SizingResult,
    TradeSignal,
)

logger = logging.getLogger("smarttrade.adaptive.journal")


def _envb(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _envs(key: str, default: str) -> str:
    return os.getenv(key, default)


class TradeJournalService:
    """
    Append-only journal that records every trading decision.

    Typical call sequence per trade:
        entry = journal.open_entry(signal, regime, score, risk, sizing, policy)
        ... later, on trade close ...
        journal.close_entry(entry.journal_id, exit_price, pnl, reason)
    """

    def __init__(self) -> None:
        self._enabled = _envb("JOURNAL_ENABLED", True)
        self._filepath = _envs("JOURNAL_FILE", "data/trade_journal.jsonl")
        self._max_memory = _envi("JOURNAL_MAX_MEMORY", 2000)
        self._auto_flush = _envb("JOURNAL_AUTO_FLUSH", True)

        self._entries: Deque[JournalEntry] = deque(maxlen=self._max_memory)
        self._index: Dict[str, JournalEntry] = {}  # signal_id → entry (open only)
        self._lock = threading.Lock()

    # ──────────────────────────────────────────────────
    #  RECORDING
    # ──────────────────────────────────────────────────

    def open_entry(
        self,
        signal: TradeSignal,
        regime: Optional[RegimeSnapshot] = None,
        score: Optional[SetupScore] = None,
        risk: Optional[RiskVerdict] = None,
        sizing: Optional[SizingResult] = None,
        policy: Optional[PolicyDecision] = None,
        executed: bool = False,
        execution_action: str = "",
        fill_price: float = 0.0,
    ) -> JournalEntry:
        """
        Record the opening of a trading decision.  Returns the JournalEntry
        (also stored internally for later close).
        """
        entry = JournalEntry(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            direction=signal.direction.value,
            timestamp=signal.timestamp,
            signal_confidence=signal.confidence,
            signal_strength=signal.strength.value,
            signal_reasons=list(signal.reasons),
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            risk_reward=signal.risk_reward,
        )

        if regime:
            entry.regime = regime.regime.value
            entry.regime_confidence = regime.confidence

        if score:
            entry.setup_score = score.score
            entry.setup_priority = score.priority
            entry.setup_vetoed = score.veto

        if risk:
            entry.risk_action = risk.action.value
            entry.risk_reason = risk.reason
            entry.drawdown_pct = risk.current_drawdown_pct
            entry.daily_loss_pct = risk.daily_loss_pct
            entry.consecutive_losses = risk.consecutive_losses

        if sizing:
            entry.quantity = sizing.quantity
            entry.notional = sizing.notional
            entry.risk_amount = sizing.risk_amount
            entry.size_multiplier = sizing.final_multiplier

        if policy:
            entry.adapted_confidence_threshold = policy.adaptation.confidence_threshold
            entry.adapted_size_mult = policy.adaptation.size_multiplier
            entry.policy_action = policy.action.value

        entry.executed = executed
        entry.execution_action = execution_action
        entry.fill_price = fill_price

        with self._lock:
            self._entries.append(entry)
            self._index[signal.signal_id] = entry

        if self._auto_flush and self._enabled:
            self._flush_entry(entry)

        logger.info(
            "[JOURNAL] Opened %s | %s %s score=%.3f regime=%s executed=%s",
            entry.journal_id, entry.symbol, entry.direction,
            entry.setup_score, entry.regime, entry.executed,
        )
        return entry

    def close_entry(
        self,
        signal_id: str,
        exit_price: float,
        pnl: float,
        exit_reason: str = "",
    ) -> Optional[JournalEntry]:
        """
        Record outcome for a previously opened entry.
        Returns the updated entry, or None if not found.
        """
        with self._lock:
            entry = self._index.pop(signal_id, None)

        if not entry:
            logger.warning("[JOURNAL] No open entry for signal_id=%s", signal_id)
            return None

        entry.exit_price = exit_price
        entry.pnl = pnl
        entry.exit_reason = exit_reason
        entry.closed = True
        entry.hold_seconds = time.time() - entry.timestamp

        if entry.entry_price > 0 and entry.quantity > 0:
            cost = entry.entry_price * entry.quantity
            entry.pnl_pct = (pnl / cost) * 100 if cost > 0 else 0.0

        if self._auto_flush and self._enabled:
            self._flush_entry(entry)

        logger.info(
            "[JOURNAL] Closed %s | pnl=%.2f exit=%s",
            entry.journal_id, pnl, exit_reason,
        )
        return entry

    def record_blocked(
        self,
        signal: TradeSignal,
        regime: Optional[RegimeSnapshot] = None,
        score: Optional[SetupScore] = None,
        risk: Optional[RiskVerdict] = None,
        block_reason: str = "",
    ) -> JournalEntry:
        """Record a signal that was evaluated but NOT executed."""
        entry = self.open_entry(
            signal=signal,
            regime=regime,
            score=score,
            risk=risk,
            executed=False,
            execution_action="BLOCKED",
        )
        entry.exit_reason = block_reason
        entry.closed = True
        return entry

    # ──────────────────────────────────────────────────
    #  QUERIES
    # ──────────────────────────────────────────────────

    def get_entries(
        self,
        symbol: Optional[str] = None,
        closed_only: bool = False,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return recent journal entries as dicts."""
        with self._lock:
            entries = list(self._entries)

        if symbol:
            entries = [e for e in entries if e.symbol == symbol]
        if closed_only:
            entries = [e for e in entries if e.closed]

        return [e.to_dict() for e in entries[-limit:]]

    def get_closed_entries(self, limit: int = 500) -> List[JournalEntry]:
        """Return closed JournalEntry objects (not dicts) for analysis."""
        with self._lock:
            return [e for e in self._entries if e.closed][-limit:]

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def open_count(self) -> int:
        with self._lock:
            return len(self._index)

    # ──────────────────────────────────────────────────
    #  EXPORT
    # ──────────────────────────────────────────────────

    def export_json(self, filepath: Optional[str] = None) -> str:
        """Export all entries to a JSON file or return as string."""
        data = [e.to_dict() for e in self._entries]
        content = json.dumps(data, indent=2)

        if filepath:
            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info("[JOURNAL] Exported %d entries to %s", len(data), filepath)

        return content

    def export_csv(self, filepath: Optional[str] = None) -> str:
        """Export closed entries as CSV."""
        entries = [e for e in self._entries if e.closed]
        if not entries:
            return ""

        output = io.StringIO()
        fieldnames = list(entries[0].to_dict().keys())
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for e in entries:
            row = e.to_dict()
            # Flatten lists for CSV
            if isinstance(row.get("signal_reasons"), list):
                row["signal_reasons"] = "; ".join(row["signal_reasons"])
            writer.writerow(row)

        content = output.getvalue()

        if filepath:
            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            with open(filepath, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            logger.info("[JOURNAL] CSV exported %d entries to %s", len(entries), filepath)

        return content

    # ──────────────────────────────────────────────────
    #  PERSISTENCE
    # ──────────────────────────────────────────────────

    def _flush_entry(self, entry: JournalEntry) -> None:
        """Append a single entry to the JSONL file."""
        try:
            os.makedirs(os.path.dirname(self._filepath) or ".", exist_ok=True)
            with open(self._filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict()) + "\n")
        except OSError as e:
            logger.error("[JOURNAL] Flush failed: %s", e)

    def load_from_file(self) -> int:
        """
        Load existing JSONL journal into memory (on startup).
        Returns number of entries loaded.
        """
        if not os.path.exists(self._filepath):
            return 0

        count = 0
        try:
            with open(self._filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        entry = JournalEntry(
                            journal_id=data.get("journal_id", ""),
                            signal_id=data.get("signal_id", ""),
                            symbol=data.get("symbol", ""),
                            direction=data.get("direction", ""),
                            timestamp=data.get("timestamp", 0),
                            signal_confidence=data.get("signal_confidence", 0),
                            signal_strength=data.get("signal_strength", ""),
                            entry_price=data.get("entry_price", 0),
                            stop_loss=data.get("stop_loss", 0),
                            take_profit_1=data.get("take_profit_1", 0),
                            take_profit_2=data.get("take_profit_2", 0),
                            risk_reward=data.get("risk_reward", 0),
                            regime=data.get("regime", "unknown"),
                            setup_score=data.get("setup_score", 0),
                            pnl=data.get("pnl", 0),
                            exit_reason=data.get("exit_reason", ""),
                            closed=data.get("closed", False),
                        )
                        with self._lock:
                            self._entries.append(entry)
                        count += 1
                    except (json.JSONDecodeError, KeyError):
                        continue
        except OSError as e:
            logger.error("[JOURNAL] Load failed: %s", e)

        logger.info("[JOURNAL] Loaded %d entries from %s", count, self._filepath)
        return count
