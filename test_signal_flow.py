
import sys
import os
import logging
from datetime import datetime

# Setup logging to console
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger("TEST_SIGNAL")

# Ensure we can import from local package
sys.path.append(os.getcwd())

try:
    from smc_futures.engine.strategy.signal_builder import SignalBuilder, SignalResult
except ImportError as e:
    logger.error(f"Failed to import SignalBuilder: {e}")
    sys.exit(1)

def test_signal_flow():
    sb = SignalBuilder(min_rr_ratio=1.5)
    
    # Mock States
    current_price = 50000.0
    timestamp = int(datetime.now().timestamp() * 1000)
    
    # 1. TEST NEUTRAL BIAS (Expect NO_SIGNAL + Reason)
    logger.info("--- TEST 1: NEUTRAL BIAS ---")
    mtf_state_neutral = {"bias": "neutral", "bias_strength": 0.5}
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state={},
        liquidity_state={},
        ob_state={},
        fvg_state={},
        mtf_state=mtf_state_neutral,
        current_price=current_price,
        timestamp=timestamp
    )
    
    if result.status == "NO_SIGNAL" and "NEUTRAL_BIAS" in result.reasons[0]:
        logger.info(f"✅ PASS: Correctly rejected for NEUTRAL_BIAS. Reason: {result.reasons[0]}")
    else:
        logger.error(f"❌ FAIL: Expected NEUTRAL_BIAS. Got Status={result.status}, Reasons={result.reasons}")

    # 2. TEST NO SWEEP (Expect NO_SIGNAL + Reason)
    logger.info("\n--- TEST 2: NO SWEEP ---")
    mtf_state_bullish = {"bias": "bullish", "bias_strength": 0.8}
    liquidity_state_empty = {"sweep_history": []}
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state={},
        liquidity_state=liquidity_state_empty,
        ob_state={},
        fvg_state={},
        mtf_state=mtf_state_bullish,
        current_price=current_price,
        timestamp=timestamp
    )
    
    if result.status == "NO_SIGNAL" and "NO_LIQUIDITY_SWEEP" in result.reasons[0]:
        logger.info(f"✅ PASS: Correctly rejected for NO_LIQUIDITY_SWEEP. Reason: {result.reasons[0]}")
    else:
        logger.error(f"❌ FAIL: Expected NO_LIQUIDITY_SWEEP. Got Status={result.status}, Reasons={result.reasons}")

    # 3. TEST NO ZONE (Expect NO_SIGNAL + Reason)
    logger.info("\n--- TEST 3: NO ZONE ---")
    liquidity_state_sweep = {"sweep_history": [{"type": "buyside"}]} # Bullish needs buyside sweep? Wait.
    # In SignalBuilder: if bias == "bullish" and sweep_type != "buyside": return None
    # Wait, Bullish bias usually means we want to go LONG.
    # To go LONG, we usually want to see a SELLSIDE liquidity sweep (manipulation down) then reversal?
    # Let's check the code in SignalBuilder I just wrote/read.
    # Line 79 (Step 66): 
    # if bias == "bullish" and sweep_type != "buyside": result.reasons.append("SWEEP_MISMATCH...")
    # So the code EXPECTS "buyside" sweep for "bullish" bias.
    # This might be "Trend Continuation" logic? Or "Breakout"? 
    # Usually SMC Reversal Long = Sweep Sellside (Lows). 
    # But I must follow the EXISTING generic logic. I did not change the logic, just the return.
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state={},
        liquidity_state=liquidity_state_sweep,
        ob_state={"blocks": []},
        fvg_state={"fvgs": []},
        mtf_state=mtf_state_bullish,
        current_price=current_price,
        timestamp=timestamp
    )
    
    if result.status == "NO_SIGNAL" and "NO_ENTRY_ZONE" in result.reasons[0]:
        logger.info(f"✅ PASS: Correctly rejected for NO_ENTRY_ZONE. Reason: {result.reasons[0]}")
    else:
        logger.error(f"❌ FAIL: Expected NO_ENTRY_ZONE. Got Status={result.status}, Reasons={result.reasons}")

    # 4. TEST SUCCESS (Mocking a perfect setup)
    logger.info("\n--- TEST 4: SUCCESS ---")
    # We need an entry zone
    # Bullish bias -> Bullish OB at price lower than current (limit entry) or retest?
    # Logic: if current_price < entry_price: return None (Price hasn't reached entry yet)
    # So we need entry_price <= current_price? No.
    # Logic in _build_bullish_signal:
    # if current_price < entry_price: return None.
    # Means price must be AT or ABOVE entry?
    # If entry is LIMIT BUY at OB. We want price to come DOWN to OB.
    # If current_price < entry_price, it means price is BELOW OB.
    # Usually we place limit when price is ABOVE OB.
    # So if Current Price (50000) > Entry (49000).
    # Then current_price (50000) < entry_price (49000) is FALSE.
    # Code: if current_price < entry_price: return None.
    # So if Price is 48000 (below entry), we return None?
    # It seems to imply we only trade if price is >= entry?
    # Wait. If price is below entry, we missed it?
    # Or is it a STOP entry?
    # Let's assume standard Limit Buy at OB.
    # Current Price 50000. Entry 49000.
    # 50000 < 49000 is False. Proceeds.
    
    entry_price = 49000.0
    stop_loss = 48000.0
    take_profit = 52000.0 # RR = (52-49)/(49-48) = 3/1 = 3.0. Good.
    
    ob_state_valid = {"blocks": [{
        "type": "bullish", "low": entry_price, "confidence": 0.9, "freshness": 1.0
    }]}
    
    structure_state_valid = {"structure_strength": 0.8}
    
    # We need current_price >= entry_price (50000 >= 49000)
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state=structure_state_valid,
        liquidity_state=liquidity_state_sweep,
        ob_state=ob_state_valid,
        fvg_state={},
        mtf_state=mtf_state_bullish,
        current_price=current_price,
        timestamp=timestamp
    )
    
    if result.status == "CREATED" and result.signal:
        logger.info(f"✅ PASS: Signal CREATED. Entry: {result.signal.entry_price}, SL: {result.signal.stop_loss}")
        logger.info(f"   Details: {result.signal}")
    else:
        logger.error(f"❌ FAIL: Expected CREATED. Return: {result.status} Reasons: {result.reasons}")

if __name__ == "__main__":
    test_signal_flow()
