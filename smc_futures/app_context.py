"""
AppContext — Dependency Injection container for the SMC trading bot.

Single source of truth for shared state:
  - config (SMC_CONFIG)
  - metrics (DecisionMetrics)
  - order_manager (OrderManager)
  - market_client (BitgetV2Client)
  - orchestrators registry
  - executors registry (multi-broker: "futures", "kraken_futures")
  - ws_manager (WebSocket broadcaster)

Usage:
    from smc_futures.app_context import build_app_context
    ctx = build_app_context()
    
    # Multi-broker support
    executor = ctx.get_active_executor("kraken")  # or "bitget"
"""

import logging
import os
from typing import Any, Dict, Literal, Optional

from smc_futures.smc_config import SMC_CONFIG
from smc_futures.smc_logger import DecisionMetrics
from smc_futures.engine.execution.order_manager import OrderManager
from smc_futures.engine.execution.bitget_v2_client import BitgetV2Client
from smc_futures.engine.small_account_manager import SmallAccountManager
from smc_futures.engine.analysis.latency_volatility_analyzer import LatencyVolatilityAnalyzer
from smc_futures.engine.analysis.latency_tracker import LatencyTracker
from smc_futures.engine.risk.scale_readiness_gate import ScaleReadinessGate
from smc_futures.engine.risk.capital_allocation import AdaptiveCapitalAllocator
from smc_futures.risk.account_state import AccountStateService
from smc_futures.analytics.trade_journal import InstitutionalTradeJournal
from smc_futures.analytics.strategy_allocator import StrategyAllocator
from smc_futures.analytics import journal_hook

logger = logging.getLogger(__name__)

# Broker type alias
BrokerName = Literal["bitget", "kraken"]


class AppContext:
    """
    Centralised DI container.
    Every component that needs shared state receives an AppContext instance.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        metrics: Optional[DecisionMetrics] = None,
        order_manager: Optional[OrderManager] = None,
        market_client: Optional[BitgetV2Client] = None,
        small_account_manager: Optional[SmallAccountManager] = None,
    ):
        self.config: Dict[str, Any] = config or SMC_CONFIG
        self.metrics: DecisionMetrics = metrics or DecisionMetrics()
        self.order_manager: OrderManager = order_manager or OrderManager(self.config)
        
        # ── AccountStateService — broker-aware source of truth for balance/equity ──
        staleness = float(self.config.get("ACCOUNT_STATE_MAX_STALENESS_SEC", 120))
        from smc_futures.risk.account_state import resolve_execution_broker_mode
        _exec_mode = os.getenv("EXECUTION_BROKER_MODE", "") or None
        self.account_state: AccountStateService = AccountStateService(
            max_staleness_sec=staleness,
            execution_broker_mode=_exec_mode,
        )

        # ── InstitutionalTradeJournal — post-trade truth ──
        strict = str(self.config.get("TRADE_JOURNAL_STRICT", "false")).lower() in ("true", "1", "yes")
        self.trade_journal: InstitutionalTradeJournal = InstitutionalTradeJournal(strict=strict)

        # Small Account Mode manager
        self.small_account_manager: SmallAccountManager = small_account_manager or SmallAccountManager(self.config)

        self.market_client: BitgetV2Client = market_client or BitgetV2Client(
            price_source=self.config.get("PRICE_SOURCE", "last"),
            stale_threshold_ms=self.config.get("STALE_THRESHOLD_MS", 250),
        )
        
        # Phase 4.5 Analysis & Scaling
        self.analyzers: Dict[str, LatencyVolatilityAnalyzer] = {}
        self.latency_tracker: LatencyTracker = LatencyTracker()
        self.scale_gate: ScaleReadinessGate = ScaleReadinessGate()
        self.capital_allocator: AdaptiveCapitalAllocator = AdaptiveCapitalAllocator(config=self.config)

        # ── Learning Engine — adaptive strategy gating & sizing ──
        self.strategy_allocator: StrategyAllocator = StrategyAllocator(
            journal=self.trade_journal, config=self.config,
        )

        # [HARDENING AUDIT] JSON Proof of effective threshold
        import json
        logger.info(json.dumps({
            "component": "AppContext",
            "event": "STALE_GUARD_INIT",
            "stale_threshold_ms": self.market_client.stale_threshold_ms,
            "source_of_truth": "SMC_CONFIG"
        }))
        
        # Fail-fast if inconsistency detected
        assert self.market_client.stale_threshold_ms == self.config.get("STALE_THRESHOLD_MS", 250), "STALE_THRESHOLD_MS MISMATCH!"

        # Registries — populated at startup
        self.orchestrators: Dict[str, Any] = {}
        self.executors: Dict[str, Any] = {}
        self.ws_manager: Any = None
        self.guardian: Any = None

    # ── Convenience helpers ───────────────────────────────

    def set_ws_manager(self, manager: Any) -> None:
        self.ws_manager = manager
        logger.info("[CTX] WebSocket manager registered")

    def set_guardian(self, guardian: Any) -> None:
        self.guardian = guardian
        logger.info("[CTX] PositionGuardian registered")

    def register_orchestrator(self, symbol: str, orch: Any) -> None:
        self.orchestrators[symbol] = orch
        logger.info(f"[CTX] Orchestrator registered for {symbol}")

    def get_executor(self, kind: str = "futures") -> Optional[Any]:
        return self.executors.get(kind)

    def get_analyzer(self, symbol: str) -> LatencyVolatilityAnalyzer:
        """Get or create volatility analyzer for symbol."""
        if symbol not in self.analyzers:
            self.analyzers[symbol] = LatencyVolatilityAnalyzer(symbol)
        return self.analyzers[symbol]

    # ── Multi-Broker Helpers ──────────────────────────────

    @property
    def default_broker(self) -> BrokerName:
        """Get default broker from config or environment."""
        return os.getenv("DEFAULT_BROKER", "bitget").lower()  # type: ignore

    @property
    def kraken_enabled(self) -> bool:
        """Check if Kraken is enabled and configured."""
        return os.getenv("KRAKEN_ENABLED", "false").lower() in ("true", "1", "yes")

    @property
    def bitget_enabled(self) -> bool:
        """Check if Bitget is enabled and configured."""
        return os.getenv("BITGET_ENABLED", "false").lower() in ("true", "1", "yes")

    @property
    def execution_broker_mode(self) -> str:
        """Active execution broker mode from AccountStateService."""
        return self.account_state.execution_broker_mode

    def get_active_executor(self, broker: Optional[BrokerName] = None) -> Optional[Any]:
        """
        Get executor for specified broker, or default broker if not specified.
        
        Args:
            broker: "bitget" or "kraken". If None, uses DEFAULT_BROKER env var.
        
        Returns:
            Executor instance or None if not configured.
        
        Example:
            executor = ctx.get_active_executor("kraken")
            if executor:
                await executor.place_order_market("BTCUSDT", "buy", "0.001")
        """
        target_broker = broker or self.default_broker
        
        if target_broker == "kraken":
            return self.executors.get("kraken_futures")
        else:
            return self.executors.get("futures")  # Bitget

    def get_broker_status(self) -> Dict[str, Any]:
        """
        Get status of all configured brokers.
        
        Returns:
            Dict with broker statuses:
            {
                "default_broker": "bitget",
                "brokers": {
                    "bitget": {"enabled": True, "connected": True},
                    "kraken": {"enabled": True, "connected": False}
                }
            }
        """
        bitget_executor = self.executors.get("futures")
        kraken_executor = self.executors.get("kraken_futures")
        
        return {
            "default_broker": self.default_broker,
            "execution_broker_mode": self.execution_broker_mode,
            "brokers": {
                "bitget": {
                    "enabled": self.bitget_enabled,
                    "connected": bitget_executor is not None,
                },
                "kraken": {
                    "enabled": self.kraken_enabled,
                    "connected": kraken_executor is not None,
                }
            }
        }


def build_app_context(
    config: Optional[Dict[str, Any]] = None,
    metrics: Optional[DecisionMetrics] = None,
    order_manager: Optional[OrderManager] = None,
    market_client: Optional[BitgetV2Client] = None,
    small_account_manager: Optional[SmallAccountManager] = None,
) -> AppContext:
    """
    Factory that builds a fully-wired AppContext.
    Called once at module load in orchestrator.py.
    """
    ctx = AppContext(
        config=config,
        metrics=metrics,
        order_manager=order_manager,
        market_client=market_client,
        small_account_manager=small_account_manager,
    )
    # Wire AccountStateService into OrderManager
    ctx.order_manager.set_account_state(ctx.account_state)
    # Wire InstitutionalTradeJournal via hook
    journal_hook.set_journal(ctx.trade_journal)
    # Wire StrategyAllocator into OrderManager
    ctx.order_manager.set_strategy_allocator(ctx.strategy_allocator)
    logger.info("[BOOT] AppContext built — metrics=%s  order_manager=%s  market_client=%s  small_acc=%s",
                type(ctx.metrics).__name__, type(ctx.order_manager).__name__,
                type(ctx.market_client).__name__, 
                "enabled" if ctx.small_account_manager.enabled else "disabled")
    return ctx

