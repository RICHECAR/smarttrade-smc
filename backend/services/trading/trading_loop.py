"""
Trading Loop — Async Feedback Loop Orchestrator
═══════════════════════════════════════════════

Continuous async loop that ties all modules together:
  Price → PnL → Risk → Decision → Sizing → Execution → Performance → loop

Consumes EXISTING services (PnL, Price, Position) — never modifies them.
Delegates order placement to existing BrokerRouter.

Config via .env:
  TRADING_LOOP_ENABLED=false
  TRADING_LOOP_INTERVAL_SEC=5
  TRADING_LOOP_SYMBOLS=PF_XBTUSD,PF_ETHUSD
  TRADING_LOOP_DRY_RUN=true
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from backend.services.trading.decision_engine import DecisionEngine
from backend.services.trading.execution_engine import ExecutionEngine
from backend.services.trading.models import (
    ExecutionAction,
    ExecutionPlan,
    LoopState,
    RiskAction,
    VolatilityRegime,
)
from backend.services.trading.performance_tracker import PerformanceTracker
from backend.services.trading.position_sizing import PositionSizingEngine
from backend.services.trading.risk_engine import RiskEngine

try:
    from core.safe_mode import safe_mode_controller
except ImportError:
    safe_mode_controller = None  # graceful degradation if module missing

logger = logging.getLogger("smarttrade.trading.loop")


def _envb(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")

def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default

def _envs(key: str, default: str) -> str:
    return os.getenv(key, default)


class TradingLoop:
    """
    Async orchestrator: runs the decision→risk→sizing→execution pipeline.

    Plug-in architecture:
      - price_fetcher:   async (symbol) -> float   (mark price)
      - balance_fetcher: async () -> float          (account equity)
      - order_sender:    async (ExecutionPlan) -> bool  (delegates to broker)
      - candle_fetcher:  async (symbol) -> dict     (OHLCV, optional)

    All fetchers are injected — the loop never creates broker connections.
    """

    def __init__(
        self,
        price_fetcher: Optional[Callable] = None,
        balance_fetcher: Optional[Callable] = None,
        order_sender: Optional[Callable] = None,
        candle_fetcher: Optional[Callable] = None,
    ) -> None:
        # ── Config ──
        self._enabled = _envb("TRADING_LOOP_ENABLED", False)
        self._interval = _envf("TRADING_LOOP_INTERVAL_SEC", 5.0)
        self._symbols = [
            s.strip() for s in _envs("TRADING_LOOP_SYMBOLS", "PF_XBTUSD").split(",")
            if s.strip()
        ]
        self._dry_run = _envb("TRADING_LOOP_DRY_RUN", True)

        # ── Modules ──
        self.decision = DecisionEngine()
        self.sizing = PositionSizingEngine()
        self.risk = RiskEngine()
        self.execution = ExecutionEngine()
        self.performance = PerformanceTracker()

        # ── External dependencies (injected) ──
        self._price_fetcher = price_fetcher
        self._balance_fetcher = balance_fetcher
        self._order_sender = order_sender
        self._candle_fetcher = candle_fetcher

        # ── State ──
        self._state = LoopState()
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    # ──────────────────────────────────────────────────
    #  LIFECYCLE
    # ──────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the trading loop as a background task."""
        if self._state.running:
            logger.warning("[LOOP] Already running")
            return

        if not self._enabled:
            logger.info("[LOOP] Disabled (TRADING_LOOP_ENABLED=false)")
            return

        self._stop_event.clear()
        self._state.running = True
        self._state.started_at = datetime.now(timezone.utc).isoformat()
        self._state.errors = []

        logger.info(
            "[LOOP] Starting | symbols=%s interval=%.1fs dry_run=%s",
            self._symbols, self._interval, self._dry_run,
        )

        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Gracefully stop the loop."""
        if not self._state.running:
            return

        self._stop_event.set()
        self._state.running = False

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("[LOOP] Stopped after %d cycles", self._state.cycle_count)

    @property
    def state(self) -> LoopState:
        """Current loop state for monitoring."""
        self._state.performance = self.performance.snapshot()
        if self._state.started_at:
            started = datetime.fromisoformat(self._state.started_at)
            self._state.uptime_seconds = (
                datetime.now(timezone.utc) - started
            ).total_seconds()
        return self._state

    # ──────────────────────────────────────────────────
    #  MAIN LOOP
    # ──────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Core async loop."""
        while not self._stop_event.is_set():
            cycle_start = time.time()
            try:
                await self._cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                err_msg = f"cycle {self._state.cycle_count}: {type(e).__name__}: {e}"
                logger.error("[LOOP] Error in %s", err_msg)
                self._state.errors.append(err_msg)
                if len(self._state.errors) > 50:
                    self._state.errors = self._state.errors[-50:]

            self._state.cycle_count += 1
            self._state.last_cycle_ms = (time.time() - cycle_start) * 1000

            # Sleep until next interval
            elapsed = time.time() - cycle_start
            sleep_time = max(0, self._interval - elapsed)
            if sleep_time > 0:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=sleep_time
                    )
                except asyncio.TimeoutError:
                    pass

    async def _cycle(self) -> None:
        """Single cycle: fetch data → decide → risk check → size → execute."""

        # ── 1. Fetch balance ──
        balance = 0.0
        if self._balance_fetcher:
            balance = await self._balance_fetcher()
        if balance <= 0:
            return

        # Update equity tracking
        self.risk.update_equity(balance)
        self.performance.update_equity(balance)

        if self.performance._starting_equity == 0:
            self.performance.set_starting_equity(balance)

        # ── 2. Fetch prices for all symbols ──
        prices: Dict[str, float] = {}
        for sym in self._symbols:
            if self._price_fetcher:
                p = await self._price_fetcher(sym)
                if p > 0:
                    prices[sym] = p

        if not prices:
            return

        # ── 3. Feed candles to decision engine (if available) ──
        for sym in self._symbols:
            if self._candle_fetcher:
                candle = await self._candle_fetcher(sym)
                if candle:
                    self.decision.ingest_candle(
                        symbol=sym,
                        open_=candle.get("open", 0),
                        high=candle.get("high", 0),
                        low=candle.get("low", 0),
                        close=candle.get("close", 0),
                    )

        # ── 4. Manage existing positions ──
        atr_values = {sym: self.decision._compute_atr(sym) for sym in self._symbols}
        mgmt_plans = self.execution.manage_positions(prices, atr_values)
        for plan in mgmt_plans:
            await self._execute_plan(plan, balance)

        # ── 5. Check for new entry signals ──
        # Safe mode gate — block all new entries
        if safe_mode_controller and safe_mode_controller.is_active:
            logger.warning("[LOOP] Safe mode active — skipping new entries: %s", safe_mode_controller.reason)
            return

        for sym in self._symbols:
            price = prices.get(sym, 0)
            if price <= 0:
                continue

            # Skip if already in position
            if self.execution.has_position(sym):
                continue

            signal = self.decision.evaluate(sym, price)
            if not signal:
                continue

            self._state.last_signal = signal

            # Risk gate — FAIL-CLOSED: if risk engine throws, trade is blocked
            try:
                verdict = self.risk.evaluate(signal, balance)
            except Exception as exc:
                logger.critical("[LOOP] Risk engine EXCEPTION — trade BLOCKED (fail-closed): %s", exc)
                continue
            self._state.last_risk_verdict = verdict

            if verdict.action == RiskAction.KILL:
                # Emergency close all
                logger.critical("[LOOP] KILL triggered — closing all")
                close_plans = self.execution.plan_emergency_close_all(prices)
                for plan in close_plans:
                    await self._execute_plan(plan, balance)
                return

            if verdict.action == RiskAction.BLOCK:
                logger.info("[LOOP] Signal blocked: %s", verdict.reason)
                continue

            # Sizing
            vol_regime = self.decision.get_volatility_regime(sym)
            perf = self.performance.snapshot()
            sizing = self.sizing.compute(
                signal=signal,
                balance=balance,
                consecutive_wins=perf.consecutive_wins,
                consecutive_losses=perf.consecutive_losses,
                volatility_regime=vol_regime,
            )

            if sizing.rejected:
                logger.info("[LOOP] Sizing rejected: %s", sizing.reject_reason)
                continue

            # Execution plan
            plan = self.execution.plan_entry(signal, sizing, verdict)
            if plan:
                self._state.last_execution = plan
                await self._execute_plan(plan, balance)

    # ──────────────────────────────────────────────────
    #  ORDER DISPATCH
    # ──────────────────────────────────────────────────

    async def _execute_plan(self, plan: ExecutionPlan, balance: float) -> None:
        """Dispatch an execution plan to the order sender."""
        if self._dry_run:
            logger.info(
                "[DRY_RUN] %s %s %s qty=%.6f @ %.2f | %s",
                plan.action.value, plan.symbol, plan.side,
                plan.quantity, plan.price, plan.reason,
            )
            # In dry-run, simulate trade completion for CLOSE actions
            if plan.action == ExecutionAction.CLOSE:
                self._record_simulated_close(plan)
            return

        if not self._order_sender:
            logger.warning("[LOOP] No order_sender configured, cannot execute")
            return

        try:
            success = await self._order_sender(plan)
            if success and plan.action == ExecutionAction.CLOSE:
                self._record_simulated_close(plan)
            elif not success:
                logger.warning("[LOOP] Order sender returned failure for %s", plan.signal_id)
        except Exception as e:
            logger.error("[LOOP] Order execution failed: %s", e)

    def _record_simulated_close(self, plan: ExecutionPlan) -> None:
        """Record a close in PerformanceTracker and update RiskEngine streaks."""
        pos = None
        # Try to find the tracked position info
        tracked = self.execution.get_tracked_positions()
        if plan.symbol in tracked:
            pos = tracked[plan.symbol]

        entry_price = pos["entry_price"] if pos else plan.price
        side = pos["side"] if pos else plan.side

        if side in ("long", "buy"):
            pnl = (plan.price - entry_price) * plan.quantity
        else:
            pnl = (entry_price - plan.price) * plan.quantity

        self.performance.record_trade(
            signal_id=plan.signal_id,
            symbol=plan.symbol,
            side=side,
            entry_price=entry_price,
            exit_price=plan.price,
            quantity=plan.quantity,
            pnl=pnl,
            entry_time=time.time() - 60,
            exit_time=time.time(),
            exit_reason=plan.reason,
        )

        if pnl > 0:
            self.risk.record_win()
        else:
            self.risk.record_loss()


# ═══════════════════════════════════════════════════════════════
#  SINGLETON
# ═══════════════════════════════════════════════════════════════

_instance: Optional[TradingLoop] = None


def get_trading_loop(**kwargs) -> TradingLoop:
    """Get or create the singleton TradingLoop."""
    global _instance
    if _instance is None:
        _instance = TradingLoop(**kwargs)
    return _instance
