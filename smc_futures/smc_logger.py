"""
smc_logger — Structured decision tracing & metrics for the SMC bot.

Provides:
  - TraceContext : per-decision trace with structured steps
  - DecisionMetrics : aggregated counters (PASS / REJECT / BLOCK / SKIP / ERROR)
  - new_trace(symbol) : factory shortcut
"""

import logging
import time
from collections import defaultdict
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────
#  TraceContext — one per process_ltf_execution call
# ──────────────────────────────────────────────────

class TraceContext:
    """
    Accumulates structured steps for a single decision path.
    Logged at the end via .log(logger).
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.steps: list = []
        self.start_ts = time.time()

    def add(self, stage: str, action: str, reason: str, **kwargs: Any) -> None:
        """Append a step.  kwargs are attached as extra detail."""
        step = {"stage": stage, "action": action, "reason": reason}
        if kwargs:
            step["detail"] = kwargs
        self.steps.append(step)

    def log(self, log: Optional[logging.Logger] = None) -> None:
        """Emit the full trace at INFO level."""
        _log = log or logger
        elapsed = (time.time() - self.start_ts) * 1000
        summary_parts = [f"{s['stage']}:{s['action']}" for s in self.steps]
        _log.info(
            "[TRACE] %s | %.0fms | %s",
            self.symbol,
            elapsed,
            " → ".join(summary_parts),
        )


def new_trace(symbol: str) -> TraceContext:
    """Factory — one trace per LTF execution cycle."""
    return TraceContext(symbol)


# ──────────────────────────────────────────────────
#  DecisionMetrics — aggregated counters
# ──────────────────────────────────────────────────

class DecisionMetrics:
    """
    Thread-safe(ish) decision outcome counters.
    Methods are safe to call from any coroutine — never raise.
    """

    def __init__(self):
        # {symbol: {action: count}}
        self._counters: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # {symbol: {"current": int, "required": int}}
        self._htf_progress: Dict[str, Dict[str, int]] = {}
        self._started_at = time.time()

    # ── Recording ──────────────────────────────────

    def safe_record(
        self,
        symbol: str,
        action: str,
        trace: Optional[TraceContext] = None,
    ) -> None:
        """
        Increment counter for *action* on *symbol*.
        Never raises — wraps everything in try/except.
        """
        try:
            self._counters[symbol][action] += 1
        except Exception as exc:
            logger.debug("[METRICS] safe_record error: %s", exc)

    def update_htf_progress(self, symbol: str, current: int, required: int) -> None:
        """Track how many HTF candles have been ingested vs. minimum required."""
        try:
            self._htf_progress[symbol] = {"current": current, "required": required}
        except Exception as exc:
            logger.debug("[METRICS] update_htf_progress error: %s", exc)

    # ── Querying ───────────────────────────────────

    def get_ratios(self, symbol: str) -> Dict[str, float]:
        """Return PASS / REJECT / BLOCK ratios for a symbol."""
        counts = self._counters.get(symbol, {})
        total = sum(counts.values()) or 1
        return {action: cnt / total for action, cnt in counts.items()}

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the /api/bot/metrics endpoint."""
        return {
            "uptime_sec": round(time.time() - self._started_at),
            "counters": {sym: dict(acts) for sym, acts in self._counters.items()},
            "htf_progress": dict(self._htf_progress),
        }
