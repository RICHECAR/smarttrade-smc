
import asyncio
import logging
import os
from datetime import datetime, timedelta
import random

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestRepro")

# Set Valid Env Vars for Orchestrator Init
os.environ["BITGET_API_KEY"] = "dummy"
os.environ["BITGET_API_SECRET"] = "dummy"
os.environ["BITGET_PASSPHRASE"] = "dummy"
os.environ["SMC_NEUTRAL_PERMISSIVE"] = "false" # Strict Mode

from smc_futures.orchestrator import SMCFuturesOrchestrator
from smc_futures.engine.utils.reason_codes import ReasonCode

def generate_candles(count=100, timeframe="5m", trend="neutral"):
    candles = []
    base_price = 50000.0
    now = datetime.now()
    
    tf_min = 5 if timeframe == "5m" else (15 if timeframe == "15m" else 240)
    
    for i in range(count):
        ts = int((now - timedelta(minutes=tf_min * (count - i))).timestamp() * 1000)
        
        # Simple trend simulation
        if trend == "bullish":
            change = random.uniform(-50, 150)
        elif trend == "bearish":
            change = random.uniform(-150, 50)
        else:
            change = random.uniform(-100, 100)
            
        open_p = base_price
        close_p = base_price + change
        high_p = max(open_p, close_p) + random.uniform(0, 50)
        low_p = min(open_p, close_p) - random.uniform(0, 50)
        
        candles.append({
            "timestamp": str(ts),
            "ts": str(ts),
            "open": str(open_p),
            "high": str(high_p),
            "low": str(low_p),
            "close": str(close_p),
            "volume": "100"
        })
        base_price = close_p
        
    return candles

async def run_test():
    logger.info("🧪 Starting Standardization Validation Test...")
    
    # Init Orchestrator
    orch = SMCFuturesOrchestrator("BTCUSDT_UMCBL", live_trading=False, timeframe="15m")
    
    logger.info(f"✅ Orchestrator Initialized (HTF Required: {orch.mtf_engine.min_htf_candles})")
    
    # TEST 1: Insufficient HTF Data
    logger.info("\n--- TEST 1: Insufficient HTF Data (Strict Gate) ---")
    htf_candles_short = generate_candles(count=100, timeframe="4h", trend="bullish") # < 1000
    orch.process_htf_context("BTCUSDT", htf_candles_short)
    
    bias_state = orch.mtf_engine.htf_analysis["4H"]
    reason = orch.mtf_engine.rejection_reason
    
    logger.info(f"HTF Bias: {bias_state.bias} (Strength: {bias_state.strength})")
    logger.info(f"Rejection Reason: {reason}")
    
    if reason == ReasonCode.INSUFFICIENT_DATA and bias_state.bias.value == "neutral":
        logger.info("✅ PASS: HTF Gate rejected insufficient data.")
    else:
        logger.error(f"❌ FAIL: Expected INSUFFICIENT_DATA, got {reason}")

    # TEST 2: Valid HTF Data (Bullish)
    logger.info("\n--- TEST 2: Valid HTF Data (Bullish) ---")
    htf_candles_long = generate_candles(count=1200, timeframe="4h", trend="bullish") # > 1000
    orch.process_htf_context("BTCUSDT", htf_candles_long)
    
    bias_state = orch.mtf_engine.htf_analysis["4H"]
    reason = orch.mtf_engine.rejection_reason
    
    logger.info(f"HTF Bias: {bias_state.bias} (Strength: {bias_state.strength})")
    
    if bias_state.bias.value == "bullish" and reason is None:
        logger.info("✅ PASS: HTF Bias is Bullish and sufficient data.")
    else:
        logger.error(f"❌ FAIL: Expected Bullish/None, got {bias_state.bias}/{reason}")

    # TEST 3: LTF Signal Generation (with Session Check)
    logger.info("\n--- TEST 3: Session Filter Gate ---")
    # Mock Session Filter to be OUT of session
    orch.session_filter.is_in_session = lambda x: False
    
    ltf_candles = generate_candles(count=300, timeframe="15m", trend="bullish")
    result = await orch.process_uptodate_candle("BTCUSDT", ltf_candles)
    
    if result is None:
        logger.info("✅ PASS: Signal rejected due to Session Filter (Return None)")
    else:
        logger.error("❌ FAIL: Signal generated outside session!")

if __name__ == "__main__":
    asyncio.run(run_test())
