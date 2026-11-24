"""
Startup Safety Check — Pre-Trading Validation.
═══════════════════════════════════════════════

Before the bot starts trading, this module:

  1. Fetches ALL open positions from each configured exchange
  2. Verifies every position has SL + TP attached
  3. Flags orphan positions (on exchange but not in local state)
  4. Verifies exchange API connectivity (ping/ticker)
  5. Checks kill switch is not already triggered
  6. Returns PASS/FAIL — bot must NOT trade until all checks pass

Env:
  STARTUP_CHECK_ENABLED=true
  STARTUP_AUTO_REPAIR=false      (if true, attempt to add missing SL/TP)
  STARTUP_BLOCK_ON_ORPHAN=true   (block trading if orphan positions found)
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, List, Optional

from core.env_loader import safe_load_dotenv

safe_load_dotenv()
logger = logging.getLogger("smarttrade.startup_check")


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


class StartupSafetyCheck:
    """
    Runs a set of pre-flight checks before trading begins.

    Usage:
        checker = StartupSafetyCheck()
        checker.register_connectivity_check("kraken", ping_kraken_coro)
        checker.register_position_fetcher("kraken", fetch_kraken_positions_coro)
        results = await checker.run_all()
        if checker.all_passed(results):
            bot.start_trading()
        else:
            bot.abort("Startup checks failed")
    """

    def __init__(self):
        self._enabled = _env_bool("STARTUP_CHECK_ENABLED", True)
        # CRITICAL: In live mode, startup checks are MANDATORY (cannot be disabled)
        trading_mode = os.getenv("TRADING_MODE", "paper").lower()
        if trading_mode == "live" and not self._enabled:
            logger.critical(
                "[STARTUP] STARTUP_CHECK_ENABLED=false is FORBIDDEN in TRADING_MODE=live — forcing ON"
            )
            self._enabled = True
        self._auto_repair = _env_bool("STARTUP_AUTO_REPAIR", False)
        self._block_on_orphan = _env_bool("STARTUP_BLOCK_ON_ORPHAN", True)
        self._connectivity_checks: Dict[str, Callable[[], Coroutine]] = {}
        self._position_fetchers: Dict[str, Callable[[], Coroutine]] = {}
        self._kill_switch_provider: Optional[Callable[[], bool]] = None
        self._db_position_fetcher: Optional[Callable[[], List[Dict[str, Any]]]] = None

    # ──────────────────────────────────────────────────
    #  REGISTRATION
    # ──────────────────────────────────────────────────

    def register_connectivity_check(self, broker: str, coro_fn: Callable[[], Coroutine]):
        """Async fn that raises on connectivity failure."""
        self._connectivity_checks[broker] = coro_fn

    def register_position_fetcher(self, broker: str, coro_fn: Callable[[], Coroutine]):
        """Async fn returning list of position dicts with 'symbol', 'side', 'has_sl', 'has_tp'."""
        self._position_fetchers[broker] = coro_fn

    def register_kill_switch_provider(self, fn: Callable[[], bool]):
        """Sync fn returning True if kill switch is already engaged."""
        self._kill_switch_provider = fn

    def register_db_position_fetcher(self, fn: Callable[[], List[Dict[str, Any]]]):
        """Sync fn returning list of DB-known open positions for reconciliation."""
        self._db_position_fetcher = fn

    # ──────────────────────────────────────────────────
    #  CORE
    # ──────────────────────────────────────────────────

    async def run_all(self) -> List[CheckResult]:
        """Run all registered checks. Returns list of results."""
        if not self._enabled:
            logger.info("[STARTUP] Safety checks disabled")
            return [CheckResult("startup_check_enabled", True, "Disabled by config")]

        results: List[CheckResult] = []

        # ── 1. Kill switch ────────────────────────────
        if self._kill_switch_provider:
            halted = self._kill_switch_provider()
            results.append(CheckResult(
                name="kill_switch",
                passed=not halted,
                detail="Kill switch OFF" if not halted else "Kill switch ACTIVE — trading blocked",
            ))

        # ── 2. Exchange connectivity ──────────────────
        for broker, check_fn in self._connectivity_checks.items():
            try:
                await check_fn()
                results.append(CheckResult(
                    name=f"connectivity_{broker}",
                    passed=True,
                    detail=f"{broker} API reachable",
                ))
            except Exception as e:
                results.append(CheckResult(
                    name=f"connectivity_{broker}",
                    passed=False,
                    detail=f"{broker} API unreachable: {e}",
                ))

        # ── 3. Position safety ────────────────────────
        for broker, fetch_fn in self._position_fetchers.items():
            try:
                positions = await fetch_fn()
                if not positions:
                    results.append(CheckResult(
                        name=f"positions_{broker}",
                        passed=True,
                        detail=f"{broker}: no open positions",
                    ))
                    continue

                unprotected = []
                for pos in positions:
                    has_sl = pos.get("has_sl", False)
                    has_tp = pos.get("has_tp", False)
                    if not has_sl or not has_tp:
                        unprotected.append(pos.get("symbol", "?"))

                if unprotected:
                    detail = f"{broker}: {len(unprotected)} unprotected positions: {unprotected}"
                    results.append(CheckResult(
                        name=f"positions_{broker}",
                        passed=False,
                        detail=detail,
                    ))
                else:
                    results.append(CheckResult(
                        name=f"positions_{broker}",
                        passed=True,
                        detail=f"{broker}: {len(positions)} positions, all have SL+TP",
                    ))
            except Exception as e:
                results.append(CheckResult(
                    name=f"positions_{broker}",
                    passed=False,
                    detail=f"{broker} position fetch failed: {e}",
                ))

        # ── 4. Position reconciliation (exchange vs DB) ──
        if self._db_position_fetcher:
            try:
                db_positions = self._db_position_fetcher()
                db_symbols = {p.get("symbol", "").upper() for p in db_positions if p.get("status") == "OPEN"}

                for broker, fetch_fn in self._position_fetchers.items():
                    try:
                        exchange_positions = await fetch_fn()
                        exchange_symbols = {
                            p.get("symbol", "").upper()
                            for p in (exchange_positions or [])
                            if float(p.get("size", 0) or p.get("quantity", 0) or 0) != 0
                        }

                        # Orphans: on exchange but not in DB
                        orphans = exchange_symbols - db_symbols
                        # Ghosts: in DB but not on exchange
                        ghosts = db_symbols - exchange_symbols

                        issues = []
                        if orphans:
                            issues.append(f"ORPHAN positions on exchange not in DB: {sorted(orphans)}")
                        if ghosts:
                            issues.append(f"GHOST positions in DB not on exchange: {sorted(ghosts)}")

                        if issues:
                            detail = f"{broker}: " + " | ".join(issues)
                            passed = not self._block_on_orphan
                            results.append(CheckResult(
                                name=f"reconciliation_{broker}",
                                passed=passed,
                                detail=detail,
                            ))
                            logger.warning("[STARTUP] RECONCILIATION MISMATCH: %s", detail)
                        else:
                            results.append(CheckResult(
                                name=f"reconciliation_{broker}",
                                passed=True,
                                detail=f"{broker}: {len(exchange_symbols)} positions match DB",
                            ))
                    except Exception as e:
                        results.append(CheckResult(
                            name=f"reconciliation_{broker}",
                            passed=False,
                            detail=f"{broker} reconciliation failed: {e}",
                        ))
            except Exception as e:
                results.append(CheckResult(
                    name="reconciliation",
                    passed=False,
                    detail=f"DB position fetch failed: {e}",
                ))

        # ── Summary log ───────────────────────────────
        passed = sum(1 for r in results if r.passed)
        failed = sum(1 for r in results if not r.passed)
        logger.info("[STARTUP] Safety check complete: %d PASS, %d FAIL", passed, failed)
        for r in results:
            symbol = "PASS" if r.passed else "FAIL"
            log_fn = logger.info if r.passed else logger.error
            log_fn("[STARTUP]   [%s] %s — %s", symbol, r.name, r.detail)

        return results

    @staticmethod
    def all_passed(results: List[CheckResult]) -> bool:
        return all(r.passed for r in results)

    @staticmethod
    def get_failures(results: List[CheckResult]) -> List[CheckResult]:
        return [r for r in results if not r.passed]
