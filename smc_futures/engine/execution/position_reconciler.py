"""
PositionReconciler — Active Heartbeat: Exchange Reality vs Internal State
═════════════════════════════════════════════════════════════════════════
Phase 5c safety component.

Runs on a configurable interval and detects divergences:
  1. ORPHAN on exchange: position exists on exchange but NOT in internal state
     → alert + optional auto-flatten
  2. GHOST in state: internal state says position open, but exchange has nothing
     → clean up internal state
  3. SIZE MISMATCH: position size differs between exchange and bookkeeping
     → alert

This is INDEPENDENT from reconcile_positions() in OrderManager:
  - reconcile_positions only checks orchestrators that HAVE active_trade
  - This reconciler checks ALL exchange positions, catching orphans
    from slippage kill failures, multi-broker race conditions, etc.

Config via .env:
  RECONCILER_ENABLED=true
  RECONCILER_INTERVAL_SEC=30
  RECONCILER_AUTO_FLATTEN=false   (manual review by default)
  RECONCILER_MAX_ORPHAN_AGE_SEC=300  (alert if orphan > 5 min)
  RECONCILER_DRIFT_TOL_PCT=0.001     (0.1% drift tolerance)
  RECONCILER_DRIFT_CRITICAL_PCT=0.05 (5% drift → auto-flatten)

Drift detection uses dynamic tolerance:
  tolerance = max(position_size * DRIFT_MIN_PCT, lot_size * DRIFT_LOT_SIZE_MULT)

  Severity levels:
    minor    (drift < 2× tolerance): update internal state + log warning
    moderate (drift < 5× tolerance): reconcile + metrics increment
    critical (drift >= 5× tolerance): auto-flatten (if enabled) + alert
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("position_reconciler")

# Phase 6: Performance Governor (in-memory metrics)
try:
    from smc_futures.observability.performance_governor import governor as _governor
except ImportError:
    _governor = None

# Phase 6.5: Prometheus drift metrics
try:
    from smc_futures.observability.metrics import (
        DRIFT_DETECTED_TOTAL,
        DRIFT_SIZE,
        DRIFT_AUTO_ADJUST_TOTAL,
    )
    _drift_metrics = True
except ImportError:
    _drift_metrics = False


class PositionReconciler:
    """
    Heartbeat service: poll exchange positions, compare with internal tracking,
    detect and handle divergences.
    """

    def __init__(
        self,
        executor,
        order_manager,
        config: Dict[str, Any],
    ):
        self.executor = executor
        self.order_manager = order_manager
        self.enabled: bool = config.get("RECONCILER_ENABLED", True)
        self.interval_sec: int = config.get("RECONCILER_INTERVAL_SEC", 30)
        self.auto_flatten: bool = config.get("RECONCILER_AUTO_FLATTEN", False)
        self.max_orphan_age_sec: int = config.get("RECONCILER_MAX_ORPHAN_AGE_SEC", 300)

        # Track when orphans were first detected (symbol → first_seen_ts)
        self._orphan_first_seen: Dict[str, float] = {}
        # Track when ghosts were first detected (symbol → first_seen_ts)
        self._ghost_first_seen: Dict[str, float] = {}
        self._ghost_confirm_sec: int = config.get("RECONCILER_GHOST_CONFIRM_SEC", 30)
        # Position drift detection (dynamic tolerance)
        self._drift_lot_size_mult: float = config.get("RECONCILER_DRIFT_LOT_SIZE_MULT", 2.0)
        self._drift_min_pct: float = config.get("RECONCILER_DRIFT_MIN_PCT", 0.001)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._stats = {
            "checks": 0,
            "orphans_detected": 0,
            "ghosts_cleaned": 0,
            "drifts_minor": 0,
            "drifts_moderate": 0,
            "drifts_critical": 0,
            "drifts_corrected": 0,
            "auto_flattened": 0,
            "last_check_ts": 0.0,
        }

        # Analytics recorder (lazy import)
        self._ar = None
        try:
            from backend.services.analytics_service import AnalyticsRecorder
            self._ar = AnalyticsRecorder
        except ImportError:
            pass

    # ── Lifecycle ─────────────────────────────────────────

    async def start(self) -> None:
        """Start the reconciliation heartbeat loop."""
        if not self.enabled:
            logger.info("[RECONCILER] Disabled by config")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[RECONCILER] Started | interval={self.interval_sec}s"
            f" | auto_flatten={self.auto_flatten}"
        )

    async def stop(self) -> None:
        """Stop the heartbeat."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[RECONCILER] Stopped")

    async def _loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await self.check_once()
            except Exception as e:
                logger.error(f"[RECONCILER] Heartbeat error: {e}", exc_info=True)
            await asyncio.sleep(self.interval_sec)

    # ── Core Reconciliation ───────────────────────────────

    async def check_once(self) -> Dict[str, Any]:
        """
        Run one reconciliation cycle.

        Returns summary dict with divergences found.
        """
        if not self.executor:
            return {"error": "no_executor"}

        self._stats["checks"] += 1
        self._stats["last_check_ts"] = time.time()

        result = {
            "orphans": [],      # On exchange, not in state
            "ghosts": [],       # In state, not on exchange
            "mismatches": [],   # Size discrepancies
            "drifts": [],       # Position size drift detected
            "flattened": [],    # Auto-flattened orphans
        }

        # 1. Get ALL open positions from exchange
        try:
            exchange_positions = await self._fetch_exchange_positions()
        except Exception as e:
            logger.error(f"[RECONCILER] Failed to fetch exchange positions: {e}")
            return {"error": str(e)}

        exchange_symbols = set(exchange_positions.keys())

        # 2. Get internal tracking state
        internal_symbols = set(self.order_manager._open_position_symbols)
        known_orphans = set(self.order_manager._orphan_positions)

        # 3. Detect ORPHANS: on exchange but NOT tracked internally
        orphan_symbols = (exchange_symbols - internal_symbols) | known_orphans
        for sym in orphan_symbols:
            if sym not in exchange_positions:
                # Was marked orphan but position already closed on exchange
                if sym in known_orphans:
                    self.order_manager._orphan_positions.discard(sym)
                    logger.info(f"[RECONCILER] {sym} orphan resolved (position closed on exchange)")
                continue

            pos = exchange_positions[sym]
            size = pos.get("size", 0)

            # Track first detection time
            if sym not in self._orphan_first_seen:
                self._orphan_first_seen[sym] = time.time()

            age = time.time() - self._orphan_first_seen[sym]

            logger.warning(
                f"[RECONCILER] ORPHAN DETECTED | {sym}"
                f" | size={size} | age={age:.0f}s"
                f" | tracked={sym in internal_symbols}"
                f" | known_orphan={sym in known_orphans}"
            )
            self._stats["orphans_detected"] += 1
            result["orphans"].append({
                "symbol": sym,
                "size": size,
                "age_sec": age,
            })

            # Record risk event
            if self._ar:
                try:
                    self._ar.record_risk_event(
                        event_type="ORPHAN_POSITION",
                        severity="CRITICAL",
                        detail={"symbol": sym, "size": size, "age_sec": age},
                        symbol=sym,
                    )
                except Exception:
                    pass

            # Auto-flatten if enabled and orphan is old enough
            if self.auto_flatten and age > self.max_orphan_age_sec:
                flatten_ok = await self._flatten_orphan(sym, pos)
                if flatten_ok:
                    result["flattened"].append(sym)
                    self._stats["auto_flattened"] += 1
                    self._orphan_first_seen.pop(sym, None)
                    self.order_manager._orphan_positions.discard(sym)

        # 4. Detect GHOSTS: tracked internally but NOT on exchange
        #    Require confirmation delay to avoid false positives from
        #    temporary exchange API lag (1-3 seconds).
        ghost_symbols = internal_symbols - exchange_symbols
        confirmed_ghosts = []
        now = time.time()
        for sym in ghost_symbols:
            if sym not in self._ghost_first_seen:
                self._ghost_first_seen[sym] = now
                logger.info(
                    f"[RECONCILER] GHOST CANDIDATE | {sym}"
                    f" | waiting {self._ghost_confirm_sec}s for confirmation"
                )
                continue
            age = now - self._ghost_first_seen[sym]
            if age < self._ghost_confirm_sec:
                continue  # Not yet confirmed
            logger.warning(
                f"[RECONCILER] GHOST CONFIRMED | {sym}"
                f" | in _open_position_symbols but NOT on exchange"
                f" | confirmed after {age:.0f}s — cleaning"
            )
            self.order_manager._open_position_symbols.discard(sym)
            self._stats["ghosts_cleaned"] += 1
            confirmed_ghosts.append(sym)
            self._ghost_first_seen.pop(sym, None)

        # Clean resolved ghost candidates
        resolved_ghosts = set(self._ghost_first_seen.keys()) - ghost_symbols
        for sym in resolved_ghosts:
            self._ghost_first_seen.pop(sym, None)

        result["ghosts"] = confirmed_ghosts

        # 5. Detect POSITION DRIFT: both tracked, but size differs
        internal_sizes = getattr(self.order_manager, "_position_sizes", {})
        matched_symbols = exchange_symbols & internal_symbols
        for sym in matched_symbols:
            exchange_size = exchange_positions[sym].get("size", 0)
            internal_size = internal_sizes.get(sym, 0)
            if internal_size <= 0:
                continue

            drift = abs(exchange_size - internal_size)
            lot_size = self._get_lot_size(sym)
            tolerance = max(internal_size * self._drift_min_pct, lot_size * self._drift_lot_size_mult)

            if drift <= tolerance:
                continue

            # Classify severity: minor (<2x tol), moderate (<5x tol), critical (>=5x tol)
            drift_ratio = drift / tolerance
            if drift_ratio >= 5.0:
                severity = "critical"
            elif drift_ratio >= 2.0:
                severity = "moderate"
            else:
                severity = "minor"

            drift_info = {
                "symbol": sym,
                "exchange_size": exchange_size,
                "internal_size": internal_size,
                "drift_abs": drift,
                "tolerance": tolerance,
                "severity": severity,
            }
            result["drifts"].append(drift_info)

            # Prometheus metrics
            if _drift_metrics:
                DRIFT_DETECTED_TOTAL.labels(severity=severity).inc()
                DRIFT_SIZE.observe(drift)

            log_msg = (
                f"POSITION_DRIFT symbol={sym}"
                f" exchange={exchange_size} internal={internal_size}"
                f" drift={drift} tolerance={tolerance}"
            )

            if severity == "critical":
                self._stats["drifts_critical"] += 1
                logger.critical(f"[RECONCILER] CRITICAL {log_msg}")
                if self._ar:
                    try:
                        self._ar.record_risk_event(
                            event_type="POSITION_DRIFT_CRITICAL",
                            severity="CRITICAL",
                            detail=drift_info,
                            symbol=sym,
                        )
                    except Exception:
                        pass
                # Auto-flatten on critical drift if enabled
                if self.auto_flatten:
                    flatten_ok = await self._flatten_orphan(sym, exchange_positions[sym])
                    if flatten_ok:
                        result["flattened"].append(sym)
                        self._stats["auto_flattened"] += 1
                        internal_sizes.pop(sym, None)
                        continue
            elif severity == "moderate":
                self._stats["drifts_moderate"] += 1
                logger.warning(f"[RECONCILER] MODERATE {log_msg}")
            else:
                self._stats["drifts_minor"] += 1
                logger.warning(f"[RECONCILER] MINOR {log_msg}")

            # Correct internal state to match exchange reality
            internal_sizes[sym] = exchange_size
            self._stats["drifts_corrected"] += 1
            if _drift_metrics:
                DRIFT_AUTO_ADJUST_TOTAL.inc()
            logger.info(
                f"[RECONCILER] DRIFT_CORRECTED | {sym}"
                f" | internal adjusted {internal_size} → {exchange_size}"
            )

        # 6. Clean resolved orphans from first_seen tracker
        resolved = set(self._orphan_first_seen.keys()) - orphan_symbols
        for sym in resolved:
            self._orphan_first_seen.pop(sym, None)

        if result["orphans"] or result["ghosts"] or result["drifts"]:
            logger.warning(
                f"[RECONCILER] Summary | orphans={len(result['orphans'])}"
                f" | ghosts={len(result['ghosts'])}"
                f" | drifts={len(result['drifts'])}"
                f" | flattened={len(result['flattened'])}"
            )

        # Phase 6: Feed PerformanceGovernor
        if _governor:
            _governor.record_reconciler_cycle(
                orphans=len(result["orphans"]),
                ghosts=len(result["ghosts"]),
                flattened=len(result["flattened"]),
            )

        return result

    # ── Exchange Position Fetching ────────────────────────

    async def _fetch_exchange_positions(self) -> Dict[str, Dict]:
        """
        Fetch all open positions from exchange.
        Returns dict of symbol → position_info.
        """
        positions: Dict[str, Dict] = {}

        # Try get_all_positions first, then per-symbol fallback
        if hasattr(self.executor, "get_all_positions"):
            raw = await self.executor.get_all_positions()
        elif hasattr(self.executor, "get_positions"):
            raw = await self.executor.get_positions()
        else:
            return positions

        # Normalize response format (BitgetV2 returns {code, data})
        if isinstance(raw, dict) and "data" in raw:
            pos_list = raw["data"]
        elif isinstance(raw, list):
            pos_list = raw
        else:
            return positions

        if not pos_list:
            return positions

        for p in pos_list:
            size = float(p.get("total", 0) or p.get("size", 0) or p.get("available", 0) or 0)
            if size > 0:
                sym = p.get("symbol", "UNKNOWN")
                positions[sym] = {
                    "size": size,
                    "side": p.get("holdSide", p.get("side", "")),
                    "unrealizedPnl": float(p.get("unrealizedPL", 0) or 0),
                    "markPrice": float(p.get("markPrice", 0) or 0),
                    "raw": p,
                }

        return positions
    # ── Lot Size Helper ─────────────────────────────────────

    def _get_lot_size(self, symbol: str) -> float:
        """Get minimum lot size for a symbol. Falls back to a conservative default."""
        try:
            from smc_futures.engine.execution.contract_specs import ContractSpecsManager
            mgr = ContractSpecsManager.get_instance()
            specs = mgr.get_specs(symbol) if mgr else None
            if specs:
                return specs.min_qty
        except Exception:
            pass
        # Conservative fallback (BTC-class)
        return 0.0001
    # ── Auto-Flatten ──────────────────────────────────────

    async def _flatten_orphan(self, symbol: str, pos_info: Dict) -> bool:
        """
        Attempt to flatten an orphan position via emergency close with retry.
        Uses OrderManager._emergency_close_with_retry if available.
        """
        size = pos_info.get("size", 0)
        side = pos_info.get("side", "").lower()
        close_dir = "sell" if side in ("long", "buy") else "buy"

        logger.critical(
            f"[RECONCILER] AUTO-FLATTEN | {symbol} | size={size} | side={side}"
        )

        if hasattr(self.order_manager, "_emergency_close_with_retry"):
            ok = await self.order_manager._emergency_close_with_retry(
                executor=self.executor,
                symbol=symbol,
                direction=close_dir,
                quantity=size,
                signal_id="RECONCILER-FLATTEN",
            )
        else:
            # Direct attempt
            try:
                if hasattr(self.executor, "close_position"):
                    res = await self.executor.close_position(symbol)
                    ok = res and res.get("success", False)
                else:
                    ok = False
            except Exception as e:
                logger.critical(f"[RECONCILER] Flatten failed for {symbol}: {e}")
                ok = False

        if ok:
            logger.critical(f"[RECONCILER] {symbol} FLATTENED successfully")
        else:
            logger.critical(f"[RECONCILER] {symbol} flatten FAILED — manual intervention required")

        return ok

    # ── Status ────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Return reconciler statistics for monitoring dashboard."""
        internal_sizes = getattr(self.order_manager, "_position_sizes", {})
        return {
            **self._stats,
            "active_orphans": list(self._orphan_first_seen.keys()),
            "known_orphans": list(self.order_manager._orphan_positions),
            "tracked_positions": {k: v for k, v in internal_sizes.items()},
            "enabled": self.enabled,
            "auto_flatten": self.auto_flatten,
            "interval_sec": self.interval_sec,
        }
