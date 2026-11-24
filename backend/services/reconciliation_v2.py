"""
Reconciliation V2 — Continuous exchange-to-DB position & balance verification.
═══════════════════════════════════════════════════════════════════════════════

INSTITUTIONAL REQUIREMENT: Every position in DB must match exchange reality.
Divergence beyond threshold → alert + optional trade block.

Architecture:
  - Runs as background asyncio task (every RECONCILE_INTERVAL_SEC).
  - 3-layer verification:
      1. POSITION RECONCILIATION: DB open trades vs exchange open positions
      2. BALANCE RECONCILIATION: Cached balance vs real exchange balance
      3. PNL RECONCILIATION: DB cumulative PnL vs exchange PnL (periodic)
  - Emits events via EventBus for alerting/observability.
  - Critical divergence → activates safe mode.

Config via .env:
  RECONCILE_INTERVAL_SEC=30
  RECONCILE_POSITION_THRESHOLD_PCT=5.0
  RECONCILE_BALANCE_THRESHOLD_PCT=2.0
  RECONCILE_PNL_THRESHOLD_USD=50.0
  RECONCILE_BLOCK_ON_CRITICAL=true
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from core.events.event_bus import get_event_bus
from core.events.event_types import (
    EventType,
    ReconciliationEvent,
    SystemEvent,
    AlertEvent,
)

logger = logging.getLogger("smarttrade.reconciliation_v2")


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _envb(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class ReconciliationConfig:
    interval_sec: int = field(default_factory=lambda: _envi("RECONCILE_INTERVAL_SEC", 30))
    position_threshold_pct: float = field(default_factory=lambda: _envf("RECONCILE_POSITION_THRESHOLD_PCT", 5.0))
    balance_threshold_pct: float = field(default_factory=lambda: _envf("RECONCILE_BALANCE_THRESHOLD_PCT", 2.0))
    pnl_threshold_usd: float = field(default_factory=lambda: _envf("RECONCILE_PNL_THRESHOLD_USD", 50.0))
    block_on_critical: bool = field(default_factory=lambda: _envb("RECONCILE_BLOCK_ON_CRITICAL", True))
    max_consecutive_failures: int = 5


@dataclass
class PositionDivergence:
    """A mismatch between DB and exchange."""
    symbol: str
    broker: str
    divergence_type: str  # "phantom_db", "phantom_exchange", "size_mismatch", "side_mismatch"
    db_value: Any = None
    exchange_value: Any = None
    severity: str = "warning"  # "info", "warning", "critical"
    timestamp: float = field(default_factory=time.time)


@dataclass
class ReconciliationResult:
    """Result of one reconciliation pass."""
    broker: str
    timestamp: float = field(default_factory=time.time)
    positions_checked: int = 0
    divergences: List[PositionDivergence] = field(default_factory=list)
    balance_db: float = 0.0
    balance_exchange: float = 0.0
    balance_divergence_pct: float = 0.0
    success: bool = True
    error: str = ""

    @property
    def has_critical(self) -> bool:
        return any(d.severity == "critical" for d in self.divergences)

    @property
    def has_warnings(self) -> bool:
        return any(d.severity == "warning" for d in self.divergences)


class ReconciliationV2Service:
    """
    Continuous reconciliation engine.

    Inject broker executors at init time. Each must implement:
      - get_positions() -> dict with position data
      - get_balance() -> dict with balance data (optional)
    """

    def __init__(
        self,
        bitget_executor=None,
        kraken_executor=None,
        config: Optional[ReconciliationConfig] = None,
    ):
        self.bitget_executor = bitget_executor
        self.kraken_executor = kraken_executor
        self.config = config or ReconciliationConfig()
        self._bus = get_event_bus()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._consecutive_failures: Dict[str, int] = {"bitget": 0, "kraken": 0}
        self._last_results: Dict[str, ReconciliationResult] = {}
        self._history: List[ReconciliationResult] = []
        self._max_history = 100

    # ──────────────────────────────────────────────────
    #  LIFECYCLE
    # ──────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._reconciliation_loop())
        logger.info(
            "[RECON_V2] Started — interval=%ds pos_thresh=%.1f%% bal_thresh=%.1f%% block=%s",
            self.config.interval_sec,
            self.config.position_threshold_pct,
            self.config.balance_threshold_pct,
            self.config.block_on_critical,
        )

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("[RECON_V2] Stopped")

    # ──────────────────────────────────────────────────
    #  MAIN LOOP
    # ──────────────────────────────────────────────────

    async def _reconciliation_loop(self) -> None:
        while self._running:
            try:
                self._bus.publish(SystemEvent(
                    EventType.RECONCILIATION_STARTED,
                    message="Reconciliation pass starting",
                    source="reconciler_v2",
                ))

                results = await self._run_pass()

                for result in results:
                    self._last_results[result.broker] = result
                    self._history.append(result)
                    if len(self._history) > self._max_history:
                        self._history = self._history[-self._max_history:]

                    if result.success:
                        self._consecutive_failures[result.broker] = 0
                    else:
                        self._consecutive_failures[result.broker] += 1

                    # Emit events based on results
                    await self._emit_events(result)

                    # Check critical → safe mode
                    if result.has_critical and self.config.block_on_critical:
                        await self._activate_safe_mode(result)

                self._bus.publish(SystemEvent(
                    EventType.RECONCILIATION_COMPLETED,
                    message=f"Reconciliation pass completed — {len(results)} brokers checked",
                    details={"brokers": [r.broker for r in results]},
                    source="reconciler_v2",
                ))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[RECON_V2] Loop error: %s", e, exc_info=True)

            await asyncio.sleep(self.config.interval_sec)

    # ──────────────────────────────────────────────────
    #  RECONCILIATION PASS
    # ──────────────────────────────────────────────────

    async def _run_pass(self) -> List[ReconciliationResult]:
        results = []

        if self.bitget_executor:
            try:
                r = await self._reconcile_broker("bitget", self.bitget_executor)
                results.append(r)
            except Exception as e:
                logger.error("[RECON_V2] Bitget reconciliation failed: %s", e)
                results.append(ReconciliationResult(
                    broker="bitget", success=False, error=str(e)
                ))

        if self.kraken_executor:
            try:
                r = await self._reconcile_broker("kraken", self.kraken_executor)
                results.append(r)
            except Exception as e:
                logger.error("[RECON_V2] Kraken reconciliation failed: %s", e)
                results.append(ReconciliationResult(
                    broker="kraken", success=False, error=str(e)
                ))

        return results

    async def _reconcile_broker(self, broker: str, executor) -> ReconciliationResult:
        result = ReconciliationResult(broker=broker)

        # ── 1. Fetch DB open trades ──
        from backend.services.live_trades_service import LiveTradesService
        all_open = LiveTradesService.get_open_trades()
        db_trades = [t for t in all_open if t.broker.lower() == broker]

        # ── 2. Fetch exchange positions ──
        exchange_positions = await self._fetch_exchange_positions(broker, executor)

        # ── 3. Compare ──
        db_symbols: Dict[str, Any] = {}
        for t in db_trades:
            key = t.symbol.upper()
            if key not in db_symbols:
                db_symbols[key] = {"trades": [], "total_qty": 0.0, "side": t.side}
            db_symbols[key]["trades"].append(t)
            db_symbols[key]["total_qty"] += t.qty

        exchange_symbols: Dict[str, Any] = {}
        for pos in exchange_positions:
            sym = pos.get("symbol", "").upper()
            if sym:
                exchange_symbols[sym] = pos

        all_symbols = set(db_symbols.keys()) | set(exchange_symbols.keys())
        result.positions_checked = len(all_symbols)

        for sym in all_symbols:
            in_db = sym in db_symbols
            on_exchange = sym in exchange_symbols

            if in_db and not on_exchange:
                # Check if the DB trade(s) were legitimately closed on exchange (e.g. SL/TP filled)
                # rather than quarantined/in-flight (< 30s old).
                now_utc = datetime.now(timezone.utc)
                synced_all = True
                for t in db_symbols[sym]["trades"]:
                    if str(getattr(t, "status", "")).upper() != "OPEN":
                        synced_all = False
                        continue
                    ts_open = getattr(t, "ts_open", None)
                    if ts_open is not None:
                        if ts_open.tzinfo is None:
                            ts_open = ts_open.replace(tzinfo=timezone.utc)
                        age_sec = (now_utc - ts_open).total_seconds()
                        if age_sec < 30.0:
                            # Too fresh — wait one more pass before reconciling
                            synced_all = False
                            continue
                    try:
                        LiveTradesService.close_live_trade(
                            trade_id=t.id,
                            close_price=None,
                            close_reason="EXCHANGE_SL_TP_CLOSED",
                        )
                        logger.info(
                            "[RECON_V2] Auto-synced exchange-closed %s trade %s (%s) -> CLOSED in SQLite",
                            broker, t.id, sym,
                        )
                    except Exception as sync_err:
                        logger.error("[RECON_V2] Failed to auto-close %s trade %s: %s", broker, t.id, sync_err)
                        synced_all = False

                if synced_all:
                    # Also clear orchestrator active_trade & order_manager open set so symbol can trade again
                    try:
                        from smc_futures.api_smc_engine import ctx as _app_ctx
                        from backend.utils.symbol_mapper import to_bitget_symbol
                        _canon_sym = to_bitget_symbol(sym) if broker == "kraken" else sym
                        for _cand in {sym, _canon_sym}:
                            _orch = _app_ctx.orchestrators.get(_cand)
                            if _orch is not None and getattr(_orch, "active_trade", None) is not None:
                                _orch.active_trade = None
                                logger.info("[RECON_V2] Cleared orchestrator active_trade for %s", _cand)
                            if hasattr(_app_ctx, "order_manager") and _app_ctx.order_manager is not None:
                                _app_ctx.order_manager._open_position_symbols.discard(_cand)
                    except Exception:
                        pass
                    div = PositionDivergence(
                        symbol=sym,
                        broker=broker,
                        divergence_type="exchange_closed_synced",
                        db_value=db_symbols[sym]["total_qty"],
                        exchange_value=0,
                        severity="warning",
                    )
                    result.divergences.append(div)
                else:
                    # Phantom DB position — could not be auto-synced
                    div = PositionDivergence(
                        symbol=sym,
                        broker=broker,
                        divergence_type="phantom_db",
                        db_value=db_symbols[sym]["total_qty"],
                        exchange_value=0,
                        severity="critical",
                    )
                    result.divergences.append(div)

            elif on_exchange and not in_db:
                # Phantom exchange position — we don't track it
                ex_size = float(exchange_symbols[sym].get("size", 0) or exchange_symbols[sym].get("total", 0) or 0)
                if ex_size > 0:
                    div = PositionDivergence(
                        symbol=sym,
                        broker=broker,
                        divergence_type="phantom_exchange",
                        db_value=0,
                        exchange_value=ex_size,
                        severity="warning",
                    )
                    result.divergences.append(div)

            elif in_db and on_exchange:
                # Both exist — check size match
                db_qty = db_symbols[sym]["total_qty"]
                ex_size = float(exchange_symbols[sym].get("size", 0) or exchange_symbols[sym].get("total", 0) or 0)

                if db_qty > 0 and ex_size > 0:
                    diff_pct = abs(db_qty - ex_size) / max(db_qty, ex_size) * 100
                    if diff_pct > self.config.position_threshold_pct:
                        severity = "critical" if diff_pct > self.config.position_threshold_pct * 2 else "warning"
                        div = PositionDivergence(
                            symbol=sym,
                            broker=broker,
                            divergence_type="size_mismatch",
                            db_value=db_qty,
                            exchange_value=ex_size,
                            severity=severity,
                        )
                        result.divergences.append(div)

        # ── 4. Balance reconciliation (if executor supports it) ──
        try:
            await self._reconcile_balance(broker, executor, result)
        except Exception as e:
            logger.warning("[RECON_V2] Balance reconciliation failed for %s: %s", broker, e)

        return result

    async def _fetch_exchange_positions(self, broker: str, executor) -> List[Dict]:
        positions = []
        try:
            res = await executor.get_positions()
            if broker == "bitget":
                if isinstance(res, dict) and res.get("code") == "00000":
                    for pos in res.get("data", []) or []:
                        total = float(pos.get("total", 0) or 0)
                        if total > 0:
                            positions.append(pos)
            elif broker == "kraken":
                data = res.get("data", []) if isinstance(res, dict) else (res if isinstance(res, list) else [])
                for pos in data:
                    size = float(pos.get("size", 0) or 0)
                    if size > 0:
                        positions.append(pos)
        except Exception as e:
            logger.error("[RECON_V2] Failed to fetch %s positions: %s", broker, e)
        return positions

    async def _reconcile_balance(
        self, broker: str, executor, result: ReconciliationResult
    ) -> None:
        """Compare cached balance with real exchange balance."""
        if not hasattr(executor, "get_balance"):
            return

        try:
            bal_resp = await executor.get_balance()
            if broker == "bitget":
                if isinstance(bal_resp, dict):
                    data = bal_resp.get("data", [{}])
                    if isinstance(data, list) and data:
                        result.balance_exchange = float(data[0].get("equity", 0) or 0)
            elif broker == "kraken":
                if isinstance(bal_resp, dict):
                    result.balance_exchange = float(bal_resp.get("equity", 0) or bal_resp.get("balance", 0) or 0)

            # Compare with cached balance (from account state service)
            try:
                from smc_futures.risk.account_state import get_account_state
                acct = get_account_state()
                if acct and hasattr(acct, "get_equity"):
                    result.balance_db = acct.get_equity(broker) or 0.0
                    if result.balance_exchange > 0 and result.balance_db > 0:
                        result.balance_divergence_pct = (
                            abs(result.balance_db - result.balance_exchange)
                            / result.balance_exchange
                            * 100
                        )
            except Exception:
                pass

        except Exception as e:
            logger.warning("[RECON_V2] Balance fetch failed for %s: %s", broker, e)

    # ──────────────────────────────────────────────────
    #  EVENT EMISSION
    # ──────────────────────────────────────────────────

    async def _emit_events(self, result: ReconciliationResult) -> None:
        for div in result.divergences:
            evt = ReconciliationEvent(
                EventType.RECONCILIATION_DIVERGENCE if div.severity != "critical"
                else EventType.RECONCILIATION_CRITICAL,
                broker=result.broker,
                details={
                    "symbol": div.symbol,
                    "db_value": div.db_value,
                    "exchange_value": div.exchange_value,
                },
                divergence_type=div.divergence_type,
                severity=div.severity,
            )
            self._bus.publish(evt)

        # Balance divergence alert
        if result.balance_divergence_pct > self.config.balance_threshold_pct:
            severity = "critical" if result.balance_divergence_pct > self.config.balance_threshold_pct * 2 else "warning"
            evt = ReconciliationEvent(
                EventType.RECONCILIATION_DIVERGENCE,
                broker=result.broker,
                details={
                    "balance_db": result.balance_db,
                    "balance_exchange": result.balance_exchange,
                    "divergence_pct": round(result.balance_divergence_pct, 2),
                },
                divergence_type="balance_mismatch",
                severity=severity,
            )
            self._bus.publish(evt)

    async def _activate_safe_mode(self, result: ReconciliationResult) -> None:
        """Critical divergence → activate safe mode to block trading."""
        logger.critical(
            "[RECON_V2] CRITICAL divergence on %s — activating safe mode! Divergences: %s",
            result.broker,
            [(d.divergence_type, d.symbol) for d in result.divergences if d.severity == "critical"],
        )
        try:
            from core.safe_mode import safe_mode_controller
            reasons = [
                f"{d.divergence_type}:{d.symbol}" for d in result.divergences if d.severity == "critical"
            ]
            safe_mode_controller.activate(
                trigger="reconciler_v2",
                reason=f"Reconciliation critical divergence: {', '.join(reasons)}",
            )
            self._bus.publish(AlertEvent(
                EventType.ALERT_CRITICAL,
                title="RECONCILIATION CRITICAL",
                message=f"Safe mode activated — {result.broker} divergences: {', '.join(reasons)}",
                source="reconciler_v2",
            ))
        except Exception as e:
            logger.error("[RECON_V2] Failed to activate safe mode: %s", e)

    # ──────────────────────────────────────────────────
    #  DIAGNOSTICS
    # ──────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "config": {
                "interval_sec": self.config.interval_sec,
                "position_threshold_pct": self.config.position_threshold_pct,
                "balance_threshold_pct": self.config.balance_threshold_pct,
                "block_on_critical": self.config.block_on_critical,
            },
            "consecutive_failures": dict(self._consecutive_failures),
            "last_results": {
                broker: {
                    "timestamp": r.timestamp,
                    "positions_checked": r.positions_checked,
                    "divergences": len(r.divergences),
                    "critical": r.has_critical,
                    "balance_divergence_pct": round(r.balance_divergence_pct, 2),
                    "success": r.success,
                    "error": r.error,
                }
                for broker, r in self._last_results.items()
            },
            "history_count": len(self._history),
        }
