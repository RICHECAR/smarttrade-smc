#!/usr/bin/env python3
"""
Multi-Broker Trading Loop — LIVE Trading Entry Point
════════════════════════════════════════════════════

Runs simultaneous LIVE trading on Bitget Futures and Kraken Futures
using the SMC (Smart Money Concept) strategy.

Features:
  - LIVE trading on BOTH exchanges
  - SMC strategy with 3-tier MTF analysis (HTF→MTF→LTF)
  - Parallel balance/candle fetching
  - Order execution on both brokers
  - Reconnection handling
  - Comprehensive logging

Usage:
    # Direct execution
    python -m smc_futures.multi_broker_loop
    
    # Or from project root
    python smc_futures/multi_broker_loop.py

Environment Variables Required:
    # Bitget
    BITGET_API_KEY=xxx
    BITGET_API_SECRET=xxx
    BITGET_PASSPHRASE=xxx
    
    # Kraken
    KRAKEN_API_KEY=xxx
    KRAKEN_API_SECRET=xxx
    KRAKEN_ENABLED=true
    
    # Trading
    BOT_SYMBOLS=BTCUSDT,ETHUSDT
    TRADING_MODE=live
    AUTO_EXECUTE=true

Author: SmartTrade AI Framework
"""

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.env_loader import safe_load_dotenv

# Ensure project imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

safe_load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("multi_broker_trading.log", mode="a"),
    ]
)

logger = logging.getLogger("multi_broker_loop")


# ══════════════════════════════════════════════════════════════════════════════
# IMPORTS (after logging configured)
# ══════════════════════════════════════════════════════════════════════════════

from smc_futures.engine.multi_broker_engine import (
    MultiBrokerEngine,
    MultiBrokerConfig,
    BrokerType,
    Signal,
    SignalType,
    Candle,
)


# ══════════════════════════════════════════════════════════════════════════════
# SMC STRATEGY INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

class SMCMultiBrokerStrategy:
    """
    SMC Strategy adapter for multi-broker trading.
    
    Uses the existing SMC engines (Structure, FVG, Liquidity, etc.)
    to generate signals compatible with the multi-broker engine.
    """
    
    def __init__(self):
        """Initialize SMC strategy components."""
        from smc_futures.engine.strategy.structure import StructureEngine
        from smc_futures.engine.strategy.fvg import FVGEngine
        from smc_futures.engine.strategy.liquidity import LiquidityEngine
        from smc_futures.engine.strategy.mtf_bias import MTFBiasEngine
        from smc_futures.engine.strategy.session_filter import SessionFilter
        from smc_futures.smc_config import SMC_CONFIG
        
        self.config = SMC_CONFIG
        
        # Per-symbol engines (lazy initialization)
        self._engines: Dict[str, Dict[str, Any]] = {}
        
        # Session filter (shared)
        self._session_filter = SessionFilter()
        
        logger.info("[SMC_STRATEGY] Initialized with config: MIN_RR=%.1f HTF=%s LTF=%s",
                   SMC_CONFIG.get("MIN_RR", 1.5),
                   SMC_CONFIG.get("HTF_TF", "4h"),
                   SMC_CONFIG.get("ENTRY_TF", "5m"))
    
    def _get_engines(self, symbol: str) -> Dict[str, Any]:
        """Get or create engines for a symbol."""
        if symbol not in self._engines:
            from smc_futures.engine.strategy.structure import StructureEngine
            from smc_futures.engine.strategy.fvg import FVGEngine
            from smc_futures.engine.strategy.liquidity import LiquidityEngine
            from smc_futures.engine.strategy.mtf_bias import MTFBiasEngine
            from smc_futures.engine.strategy.orderblocks import OrderBlockEngine
            
            self._engines[symbol] = {
                "structure": StructureEngine(),
                "fvg": FVGEngine(),
                "liquidity": LiquidityEngine(),
                "orderblocks": OrderBlockEngine(),
                "mtf_bias": MTFBiasEngine(),
            }
            logger.debug("[SMC_STRATEGY] Created engines for %s", symbol)
        
        return self._engines[symbol]
    
    def compute_signal(
        self,
        symbol: str,
        candles: List[Candle],
        broker: BrokerType,
    ) -> Optional[Signal]:
        """
        Compute SMC-based trading signal.
        
        Args:
            symbol: Trading symbol
            candles: List of candles (LTF, e.g., 15m)
            broker: Source broker
            
        Returns:
            Signal if entry conditions met, None otherwise.
        """
        if len(candles) < 50:
            return None
        
        engines = self._get_engines(symbol)
        
        # Convert candles to structure engine format
        from smc_futures.engine.strategy.structure import Candle as StructureCandle
        
        struct_candles = [
            StructureCandle(
                timestamp=c.timestamp,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
            )
            for c in candles
        ]
        
        # Update structure with candles
        structure = engines["structure"]
        for candle in struct_candles[-50:]:
            structure.add_candle(candle)
        
        # Get current state
        state = structure.get_current_state()
        bias = state.get("bias", "NEUTRAL")
        
        # Check session filter
        if not self._session_filter.is_valid_session():
            logger.debug("[SMC_STRATEGY] %s | Session filter blocked", symbol)
            return None
        
        # Detect FVG
        fvg_engine = engines["fvg"]
        for candle in struct_candles[-20:]:
            fvg_engine.add_candle(candle)
        
        active_fvgs = fvg_engine.get_active_fvgs()
        
        # Detect liquidity sweeps
        liq_engine = engines["liquidity"]
        for candle in struct_candles[-30:]:
            liq_engine.add_candle(candle)
        
        recent_sweeps = liq_engine.get_recent_sweeps(lookback_candles=5)
        
        # Get current price
        current_price = candles[-1].close
        
        # Generate signal based on SMC logic
        return self._generate_smc_signal(
            symbol=symbol,
            bias=bias,
            current_price=current_price,
            active_fvgs=active_fvgs,
            recent_sweeps=recent_sweeps,
            candles=candles,
            broker=broker,
        )
    
    def _generate_smc_signal(
        self,
        symbol: str,
        bias: str,
        current_price: float,
        active_fvgs: List[Any],
        recent_sweeps: List[Any],
        candles: List[Candle],
        broker: BrokerType,
    ) -> Optional[Signal]:
        """
        Generate trading signal based on SMC criteria.
        
        Entry conditions:
        1. Bias is BULLISH/BEARISH (not NEUTRAL)
        2. Liquidity sweep occurred recently
        3. Price in or near FVG zone
        4. Valid session timing
        """
        min_rr = self.config.get("MIN_RR", 1.5)
        leverage = self.config.get("DEFAULT_LEVERAGE", 3)
        
        # Check for bullish setup
        if bias == "BULLISH" and recent_sweeps:
            bullish_sweeps = [s for s in recent_sweeps if s.get("type") == "SELL_SIDE"]
            
            if bullish_sweeps:
                # Look for bullish FVG to enter
                bullish_fvgs = [f for f in active_fvgs if f.get("type") == "BULLISH"]
                
                if bullish_fvgs:
                    # Entry at FVG midpoint
                    fvg = bullish_fvgs[-1]
                    entry_price = current_price  # Market entry
                    
                    # Stop below FVG low
                    stop_loss = fvg.get("low", current_price * 0.98)
                    
                    # TP based on RR
                    risk = entry_price - stop_loss
                    take_profit = entry_price + (risk * min_rr)
                    
                    logger.info("[SMC_SIGNAL] %s | BULLISH setup | sweep + FVG | broker=%s",
                               symbol, broker.value)
                    
                    return Signal(
                        symbol=symbol,
                        signal_type=SignalType.LONG,
                        entry_price=entry_price,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        leverage=leverage,
                        reason=f"SMC Bullish: Sell-side sweep + Bullish FVG ({broker.value})",
                        confidence=0.75,
                    )
        
        # Check for bearish setup
        if bias == "BEARISH" and recent_sweeps:
            bearish_sweeps = [s for s in recent_sweeps if s.get("type") == "BUY_SIDE"]
            
            if bearish_sweeps:
                # Look for bearish FVG to enter
                bearish_fvgs = [f for f in active_fvgs if f.get("type") == "BEARISH"]
                
                if bearish_fvgs:
                    fvg = bearish_fvgs[-1]
                    entry_price = current_price
                    
                    # Stop above FVG high
                    stop_loss = fvg.get("high", current_price * 1.02)
                    
                    # TP based on RR
                    risk = stop_loss - entry_price
                    take_profit = entry_price - (risk * min_rr)
                    
                    logger.info("[SMC_SIGNAL] %s | BEARISH setup | sweep + FVG | broker=%s",
                               symbol, broker.value)
                    
                    return Signal(
                        symbol=symbol,
                        signal_type=SignalType.SHORT,
                        entry_price=entry_price,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        leverage=leverage,
                        reason=f"SMC Bearish: Buy-side sweep + Bearish FVG ({broker.value})",
                        confidence=0.75,
                    )
        
        return None


# ══════════════════════════════════════════════════════════════════════════════
# MAIN TRADING LOOP
# ══════════════════════════════════════════════════════════════════════════════

async def run_multi_broker_trading():
    """
    Main function to run multi-broker LIVE trading.
    
    Initializes:
    1. MultiBrokerEngine (Bitget + Kraken)
    2. SMC Strategy
    3. Trading loop
    """
    logger.info("=" * 70)
    logger.info("🚀 MULTI-BROKER LIVE TRADING - STARTING")
    logger.info("=" * 70)
    
    # Validate environment
    trading_mode = os.getenv("TRADING_MODE", "paper")
    auto_execute = os.getenv("AUTO_EXECUTE", "false").lower()
    kraken_enabled = os.getenv("KRAKEN_ENABLED", "false").lower() == "true"
    
    logger.info("[CONFIG] TRADING_MODE=%s AUTO_EXECUTE=%s KRAKEN_ENABLED=%s",
               trading_mode, auto_execute, kraken_enabled)
    
    if trading_mode != "live":
        logger.error("❌ TRADING_MODE must be 'live' for multi-broker trading!")
        logger.error("   Set TRADING_MODE=live in .env")
        return
    
    if auto_execute not in ("true", "shadow"):
        logger.error("❌ AUTO_EXECUTE must be 'true' or 'shadow'!")
        logger.error("   Set AUTO_EXECUTE=true in .env")
        return
    
    # Create SMC strategy
    try:
        smc_strategy = SMCMultiBrokerStrategy()
    except Exception as e:
        logger.error("❌ Failed to initialize SMC strategy: %s", e)
        smc_strategy = None
    
    # Create strategy callback
    def strategy_callback(
        symbol: str,
        candles: List[Candle],
        broker: BrokerType,
    ) -> Optional[Signal]:
        """Strategy callback for multi-broker engine."""
        if smc_strategy:
            return smc_strategy.compute_signal(symbol, candles, broker)
        return None
    
    # Create config
    config = MultiBrokerConfig.from_env()
    
    # Override with explicit settings
    if not config.kraken_enabled and kraken_enabled:
        config.kraken_enabled = True
    
    # Create engine
    engine = MultiBrokerEngine(
        config=config,
        strategy_callback=strategy_callback,
    )
    
    # Setup shutdown handler
    shutdown_event = asyncio.Event()
    
    def handle_shutdown(signum, frame):
        logger.info("⚠️ Shutdown signal received (signal %d)", signum)
        shutdown_event.set()
    
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    try:
        # Initialize connections
        logger.info("[INIT] Connecting to brokers...")
        await engine.initialize()
        
        # Log initial balances
        balances = engine.get_balances()
        for broker_name, balance in balances.items():
            logger.info("[BALANCE] %s: $%.2f available / $%.2f total",
                       broker_name.upper(), balance.available, balance.total)
        
        # Start trading loop in background
        loop_task = asyncio.create_task(engine.run_strategy_loop())
        
        # Wait for shutdown signal
        await shutdown_event.wait()
        
        # Cancel loop
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        
    except Exception as e:
        logger.error("❌ Fatal error: %s", e, exc_info=True)
    finally:
        # Cleanup
        logger.info("[SHUTDOWN] Cleaning up...")
        await engine.shutdown()
        
        # Final statistics
        status = engine.get_status()
        stats = status.get("statistics", {})
        
        logger.info("=" * 70)
        logger.info("📊 FINAL STATISTICS")
        logger.info("   Signals Generated: %d", stats.get("signals_generated", 0))
        logger.info("   Orders Sent: %d", stats.get("orders_sent", 0))
        logger.info("   Orders Filled: %d", stats.get("orders_filled", 0))
        logger.info("   Orders Failed: %d", stats.get("orders_failed", 0))
        logger.info("=" * 70)
        logger.info("🛑 MULTI-BROKER TRADING - STOPPED")


# ══════════════════════════════════════════════════════════════════════════════
# STANDALONE FUNCTIONS FOR TESTING
# ══════════════════════════════════════════════════════════════════════════════

async def test_connections():
    """Test broker connections without trading."""
    logger.info("Testing broker connections...")
    
    config = MultiBrokerConfig.from_env()
    engine = MultiBrokerEngine(config)
    
    await engine.initialize()
    
    # Fetch balances
    balances = await engine.refresh_all_balances()
    
    for broker, balance in balances.items():
        if balance:
            logger.info("✅ %s: $%.2f available", broker.value, balance.available)
        else:
            logger.warning("❌ %s: Connection failed", broker.value)
    
    await engine.shutdown()


async def test_market_data():
    """Test market data fetching without trading."""
    logger.info("Testing market data fetching...")
    
    config = MultiBrokerConfig.from_env()
    engine = MultiBrokerEngine(config)
    
    await engine.initialize()
    
    # Fetch candles for first symbol
    symbol = config.symbols[0] if config.symbols else "BTCUSDT"
    
    candles = await engine.fetch_candles_all(symbol, "15m", 50)
    
    for broker, data in candles.items():
        if data:
            latest = data[-1]
            logger.info("✅ %s %s: %d candles | Latest close: $%.2f",
                       broker.value, symbol, len(data), latest.close)
        else:
            logger.warning("❌ %s %s: No data", broker.value, symbol)
    
    await engine.shutdown()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Multi-Broker LIVE Trading")
    parser.add_argument("--test-connections", action="store_true",
                       help="Test broker connections only")
    parser.add_argument("--test-market-data", action="store_true",
                       help="Test market data fetching only")
    
    args = parser.parse_args()
    
    if args.test_connections:
        asyncio.run(test_connections())
    elif args.test_market_data:
        asyncio.run(test_market_data())
    else:
        asyncio.run(run_multi_broker_trading())
