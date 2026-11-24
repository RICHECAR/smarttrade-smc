"""Execution modules — Order Execution and Multi-Broker Trading."""

# Unified Broker Router (preferred)
from backend.execution.broker_router import (
    BrokerRouter,
    BrokerResult,
    BrokerStatus,
    TradeIntent,
    MultiExecutionResult,
    get_broker_router,
    reset_broker_router,
    is_require_both_brokers,
    is_multi_broker_enabled,
    get_default_brokers,
)

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    create_kraken_executor,
    kraken_symbol_mapper,
)

# Legacy Mirror Executor (for backwards compatibility)
from backend.execution.mirror_executor import (
    MirrorExecutionService,
    MirrorResult,
    BrokerExecution,
    TradeIntent as MirrorTradeIntent,
)

__all__ = [
    # Broker Router
    "BrokerRouter",
    "BrokerResult",
    "BrokerStatus",
    "TradeIntent",
    "MultiExecutionResult",
    "get_broker_router",
    "reset_broker_router",
    "is_require_both_brokers",
    "is_multi_broker_enabled",
    "get_default_brokers",
    # Executors
    "KrakenFuturesExecutorWrapper",
    "create_kraken_executor",
    "kraken_symbol_mapper",
    # Legacy
    "MirrorExecutionService",
    "MirrorResult",
    "BrokerExecution",
    "MirrorTradeIntent",
]
