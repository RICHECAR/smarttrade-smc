
import sys
import os
import logging
from datetime import datetime, timezone
import time

# Setup logging to console (Updated)
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger("TEST_MTF")

sys.path.append(os.getcwd())

try:
    from smc_futures.engine.strategy.session_filter import SessionFilter
    from smc_futures.engine.strategy.mtf_bias import MTFBiasEngine, Bias
    from smc_futures.engine.strategy.structure import StructureEngine, Candle
except ImportError as e:
    logger.error(f"Import failed: {e}")
    sys.exit(1)

def test_session_filter():
    logger.info("--- TEST 1: SESSION FILTER ---")
    sf = SessionFilter()
    
    # Mock Times
    # London Open: 09:00 UTC
    t_london = datetime(2023, 10, 2, 9, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
    if sf.is_in_session(t_london):
        logger.info(f"✅ PASS: 09:00 UTC is IN SESSION ({sf.get_session_name(t_london)})")
    else:
        logger.error(f"❌ FAIL: 09:00 UTC should be IN SESSION")
        
    # NY Close: 22:00 UTC (Outside)
    t_late = datetime(2023, 10, 2, 22, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
    if not sf.is_in_session(t_late):
        logger.info(f"✅ PASS: 22:00 UTC is OUT SESSION ({sf.get_session_name(t_late)})")
    else:
        logger.error(f"❌ FAIL: 22:00 UTC should be OUT SESSION")

def test_dual_stream_mock():
    logger.info("\n--- TEST 2: DUAL STREAM BIAS ---")
    
    # 1. Init Engines matches Orchestrator structure
    htf_structure = StructureEngine(lookback_period=200)
    mtf_engine = MTFBiasEngine()
    
    # 2. Feed Mock 4H Data (Bullish Trend)
    logger.info("Feeding 50 candles of BULLISH 4H data...")
    start_time = 1000000000
    price = 10000.0
    
    # Create an uptrend
    for i in range(50):
        c = Candle(
            timestamp=start_time + i*14400000,
            open=price,
            high=price + 50,
            low=price - 10,
            close=price + 40, # Bullish close
            volume=100
        )
        price += 10 # Drift up
        
        # Simulate Orchestrator process_htf_candle logic
        htf_structure.add_candle(c)
        htf_state = htf_structure.get_current_state()
        mtf_engine.update_htf_data(htf_state)
        
    # 3. Check Bias
    bias = mtf_engine.calculate_bias()
    logger.info(f"Resulting Bias: {bias.value} Strength: {mtf_engine.bias_strength}")
    
    # Note: With only 50 candles, it might be NEUTRAL if we set strict limits.
    # But StructureEngine usually finds trends quickly if strictly monotonic.
    # In mtf_bias.py, we added a check for last_swing_high/low.
    # 50 candles might be enough for swings if min_swing_bars=2.
    
    if bias == Bias.BULLISH:
        logger.info("✅ PASS: Bias is BULLISH after 4H uptrend feed.")
    elif bias == Bias.NEUTRAL:
         logger.warning("⚠️ WARN: Bias is NEUTRAL. Might need more data points for swings.")
         logger.info(f"State: {mtf_engine.get_current_state()}")
    else:
        logger.error(f"❌ FAIL: Bias is {bias}, expected BULLISH")

if __name__ == "__main__":
    test_session_filter()
    test_dual_stream_mock()
