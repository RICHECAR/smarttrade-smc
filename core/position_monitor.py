"""
Position Monitor — Local-vs-Exchange Position Reconciliation.
═════════════════════════════════════════════════════════════

Every POSITION_MONITOR_INTERVAL_SEC seconds:
  1. Fetch real positions from Kraken / Bitget via REST
  2. Compare against local tracked positions
  3. Detect: ghost positions, qty mismatches, missing SL/TP
  4. Alert on mismatch; auto-reconcile if configured

Env:
  POSITION_MONITOR_INTERVAL_SEC=30
  POSITION_MONITOR_AUTO_RECONCILE=false
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional

from core.env_loader import safe_load_dotenv

safe_load_dotenv()
logger = logging.getLogger("smarttrade.position_monitor")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class PositionSnapshot:
    """Represents one position at a point in time."""
    symbol: str
    side: str          # "long" | "short"
    qty: float
    entry_price: float
    has_sl: bool = False
    has_tp: bool = False
    broker: str = "bitget"


@dataclass
class Mismatch:
    """Describes a discrepancy between local and exchange state."""
    kind: str            # "ghost_local" | "ghost_exchange" | "qty_mismatch" | "missing_sl" | "missing_tp"
    symbol: str
    broker: str
    detail: str


class PositionMonitor:
    """
    Periodically reconciles local tracked positions with exchange reality.

    Usage:
        monitor = PositionMonitor()
        monitor.register_fetcher("bitget", fetch_bitget_positions_coro)
        await monitor.check_once(local_positions)
        # or
        monitor.start(get_local_positions_fn)
    """

    def __init__(self):
        self._interval = _env_int("POSITION_MONITOR_INTERVAL_SEC", 30)
        self._auto_reconcile = _env_bool("POSITION_MONITOR_AUTO_RECONCILE", False)
        self._fetchers: Dict[str, Callable[[], Coroutine[Any, Any, List[PositionSnapshot]]]] = {}
        self._last_mismatches: List[Mismatch] = []
        self._check_count = 0
        self._mismatch_total = 0
        self._task: Optional[asyncio.Task] = None

    # ──────────────────────────────────────────────────
    #  SETUP
    # ──────────────────────────────────────────────────

    def register_fetcher(self, broker: str, coro_fn: Callable[[], Coroutine[Any, Any, List[PositionSnapshot]]]):
        """Register an async function that returns exchange positions for a broker."""
        self._fetchers[broker] = coro_fn

    # ──────────────────────────────────────────────────
    #  CORE CHECK
    # ──────────────────────────────────────────────────

    async def check_once(self, local_positions: List[PositionSnapshot]) -> List[Mismatch]:
        """
        Compare local positions against exchange positions.
        Returns list of mismatches found.
        """
        mismatches: List[Mismatch] = []

        # Group local by (broker, symbol)
        local_map: Dict[str, PositionSnapshot] = {}
        for p in local_positions:
            key = f"{p.broker}:{p.symbol}:{p.side}"
            local_map[key] = p

        # Fetch exchange positions per broker
        exchange_map: Dict[str, PositionSnapshot] = {}
        for broker, fetcher in self._fetchers.items():
            try:
                exchange_positions = await fetcher()
                for p in exchange_positions:
                    key = f"{p.broker}:{p.symbol}:{p.side}"
                    exchange_map[key] = p
            except Exception as e:
                logger.error("[POS_MONITOR] Fetch failed for %s: %s", broker, e)
                mismatches.append(Mismatch(
                    kind="fetch_error",
                    symbol="*",
                    broker=broker,
                    detail=str(e),
                ))

        # Detect ghosts in local (position locally tracked but not on exchange)
        for key, lp in local_map.items():
            if key not in exchange_map:
                mismatches.append(Mismatch(
                    kind="ghost_local",
                    symbol=lp.symbol,
                    broker=lp.broker,
                    detail=f"Position {lp.side} {lp.qty} tracked locally but not on exchange",
                ))

        # Detect ghosts on exchange (position on exchange but not tracked locally)
        for key, ep in exchange_map.items():
            if key not in local_map:
                mismatches.append(Mismatch(
                    kind="ghost_exchange",
                    symbol=ep.symbol,
                    broker=ep.broker,
                    detail=f"Position {ep.side} {ep.qty} on exchange but not tracked locally",
                ))

        # Compare matching positions
        for key in local_map:
            if key in exchange_map:
                lp = local_map[key]
                ep = exchange_map[key]

                # Qty mismatch
                if abs(lp.qty - ep.qty) > 1e-8:
                    mismatches.append(Mismatch(
                        kind="qty_mismatch",
                        symbol=lp.symbol,
                        broker=lp.broker,
                        detail=f"Local qty={lp.qty} vs exchange qty={ep.qty}",
                    ))

                # Missing SL
                if not ep.has_sl:
                    mismatches.append(Mismatch(
                        kind="missing_sl",
                        symbol=ep.symbol,
                        broker=ep.broker,
                        detail=f"Position {ep.side} has no SL on exchange",
                    ))

                # Missing TP
                if not ep.has_tp:
                    mismatches.append(Mismatch(
                        kind="missing_tp",
                        symbol=ep.symbol,
                        broker=ep.broker,
                        detail=f"Position {ep.side} has no TP on exchange",
                    ))

        self._last_mismatches = mismatches
        self._check_count += 1
        self._mismatch_total += len(mismatches)

        if mismatches:
            logger.warning("[POS_MONITOR] %d mismatches detected:", len(mismatches))
            for m in mismatches:
                logger.warning("  [%s] %s@%s — %s", m.kind, m.symbol, m.broker, m.detail)

        return mismatches

    # ──────────────────────────────────────────────────
    #  BACKGROUND LOOP
    # ──────────────────────────────────────────────────

    def start(self, get_local_fn: Callable[[], Coroutine[Any, Any, List[PositionSnapshot]]]):
        """Start background monitoring loop."""
        if self._task and not self._task.done():
            return

        async def _loop():
            while True:
                try:
                    local = await get_local_fn()
                    await self.check_once(local)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("[POS_MONITOR] Loop error: %s", e)
                await asyncio.sleep(self._interval)

        self._task = asyncio.ensure_future(_loop())
        logger.info("[POS_MONITOR] Background monitoring started (interval=%ds)", self._interval)

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("[POS_MONITOR] Stopped")

    # ──────────────────────────────────────────────────
    #  STATUS
    # ──────────────────────────────────────────────────

    @property
    def last_mismatches(self) -> List[Mismatch]:
        return self._last_mismatches

    def get_status(self) -> Dict[str, Any]:
        return {
            "checks_done": self._check_count,
            "total_mismatches_found": self._mismatch_total,
            "last_mismatches_count": len(self._last_mismatches),
            "auto_reconcile": self._auto_reconcile,
            "interval_sec": self._interval,
        }
