"""
🤖 SMC Futures Trading Engine 🚀
Pure Smart Money Concept + Bitget Futures Integration

Powered by institutional-style trading logic:
- Structure detection (BOS, CHOCH, trend phases)
- Liquidity analysis (sweeps, clusters)
- Order block validation
- Fair value gap detection
- Multi-timeframe bias confirmation
- Futures risk management (leverage, liquidation)
- Bitget perpetual execution

Ready for scalping and position trading.
"""

from smc_futures.modules import (
    StructureEngine, LiquidityEngine, OrderBlockEngine, FVGEngine,
    MTFBiasEngine, SignalBuilder
)
from smc_futures.risk import FuturesRiskEngine, TradeValidator
from smc_futures.engine.execution import BitgetFuturesExecutor
from smc_futures.backtest import SMCBacktester

# NOTE: SMCFuturesOrchestrator is NOT imported here to avoid
# double-creating the FastAPI app when uvicorn loads orchestrator.py
# Import it directly: from smc_futures.orchestrator import SMCFuturesOrchestrator

__all__ = [
    # Detection modules
    "StructureEngine",
    "LiquidityEngine",
    "OrderBlockEngine",
    "FVGEngine",
    "MTFBiasEngine",
    "SignalBuilder",
    
    # Risk & Execution
    "FuturesRiskEngine",
    "TradeValidator",
    "BitgetFuturesExecutor",
    
    # Backtesting
    "SMCBacktester",
    
    # Main orchestrator
    "SMCFuturesOrchestrator"
]

__version__ = "1.0.0"
__author__ = "Smart Money Concept Futures Team"
