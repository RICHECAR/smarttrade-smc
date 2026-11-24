"""
Trade Journal — Append-only JSONL trade log.
═════════════════════════════════════════════

Every trade event (open, close, SL hit, TP hit, emergency) is recorded
as a single JSON line for post-analysis.

Output: logs/trade_journal.jsonl

Env:
  TRADE_JOURNAL_PATH=logs/trade_journal.jsonl
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.env_loader import safe_load_dotenv

safe_load_dotenv()
logger = logging.getLogger("smarttrade.journal")

_DEFAULT_PATH = "logs/trade_journal.jsonl"


class TradeJournal:
    """Thread-safe append-only trade journal (JSONL)."""

    _instance: Optional["TradeJournal"] = None

    def __init__(self, path: Optional[str] = None):
        self._path = Path(path or os.getenv("TRADE_JOURNAL_PATH", _DEFAULT_PATH))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._count = 0

    @classmethod
    def get(cls) -> "TradeJournal":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        cls._instance = None

    def log_trade(
        self,
        symbol: str,
        side: str,
        action: str,
        entry: float = 0.0,
        exit_price: float = 0.0,
        pnl: float = 0.0,
        reason: str = "",
        latency_ms: float = 0.0,
        slippage: float = 0.0,
        broker: str = "bitget",
        qty: float = 0.0,
        **extra,
    ):
        """Append one trade record to the journal."""
        record: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": side,
            "action": action,
            "entry": entry,
            "exit": exit_price,
            "pnl": round(pnl, 4),
            "reason": reason,
            "latency_ms": round(latency_ms, 1),
            "slippage": round(slippage, 6),
            "broker": broker,
            "qty": qty,
        }
        if extra:
            record["extra"] = extra

        line = json.dumps(record, default=str)

        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self._count += 1
            except Exception as e:
                logger.error("[JOURNAL] Write failed: %s", e)

    def log_open(self, symbol: str, side: str, entry: float, qty: float, broker: str = "bitget", **kw):
        self.log_trade(symbol=symbol, side=side, action="OPEN", entry=entry, qty=qty, broker=broker, **kw)

    def log_close(self, symbol: str, side: str, entry: float, exit_price: float, pnl: float, reason: str, **kw):
        self.log_trade(symbol=symbol, side=side, action="CLOSE", entry=entry, exit_price=exit_price, pnl=pnl, reason=reason, **kw)

    def log_event(self, symbol: str, action: str, reason: str = "", **kw):
        self.log_trade(symbol=symbol, side="", action=action, reason=reason, **kw)

    @property
    def count(self) -> int:
        return self._count

    @property
    def path(self) -> Path:
        return self._path
