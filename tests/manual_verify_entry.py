import sys
import os
import logging

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from smc_futures.engine.strategy.signal_builder import SignalBuilder

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VERIFY")

def test_force_entry_bypass():
    logger.info("--- Testing FORCE_ENTRY_ZONE Bypass ---")
    sb = SignalBuilder(force_entry_zone=True)
    
    # State: Bullish bias but NO zones
    mtf_state = {"bias": "bullish", "bias_strength": 0.8}
    ob_state = {"blocks": []}
    fvg_state = {"fvgs": []}
    liq_state = {
        "sweep_history": [{"type": "sellside", "price": 95}], 
        "atr": 1.0, 
        "session_low": 90
    }
    struct_state = {"structure_strength": 0.5}
    
    current_price = 100.0
    
    result = sb.build_signal(
        symbol="TEST",
        structure_state=struct_state,
        liquidity_state=liq_state,
        ob_state=ob_state,
        fvg_state=fvg_state,
        mtf_state=mtf_state,
        current_price=current_price,
        timestamp=1000
    )
    
    if result.status == "CREATED" and result.signal.setup_type == "FORCE_ENTRY":
        logger.info("PASS: Signal created via FORCE_ENTRY")
    else:
        logger.error(f"FAIL: Expected FORCE_ENTRY signal, got {result.status} {result.reasons}")

def test_padding_relaxation():
    logger.info("--- Testing PADDING Relaxation ---")
    # Standard: should reject if price < entry
    sb_strict = SignalBuilder(force_entry_zone=False, entry_zone_padding=0.0)
    
    # Zone at 100. Price at 99.
    ob_state = {"blocks": [{"type": "bullish", "low": 100, "confidence": 0.8, "freshness": 1.0, "price": 100}]}
    # Note: _find_entry_zone uses 'low' for bullish OB.
    
    mtf_state = {"bias": "bullish", "bias_strength": 0.8}
    fvg_state = {"fvgs": []}
    liq_state = {"sweep_history": [{"type": "sellside"}], "atr": 1.0, "session_low": 90}
    struct_state = {"structure_strength": 0.5}
    
    res_strict = sb_strict.build_signal(
        symbol="TEST",
        structure_state=struct_state,
        liquidity_state=liq_state,
        ob_state=ob_state,
        fvg_state=fvg_state,
        mtf_state=mtf_state,
        current_price=99.0, # Below entry (100)
        timestamp=1000
    )
    
    if res_strict.status == "NO_SIGNAL" or res_strict.status == "REJECTED": # Actually returns None -> REJECTED/SIGNAL_BUILD_FAILED inside?
        # build_signal logic: 
        # 1. find_entry_zone -> finds 100.
        # 2. _build_bullish -> check price < 100 -> returns None.
        # 3. build_signal -> if not signal: REJECTED (SIGNAL_BUILD_FAILED)
        logger.info(f"PASS: Strict mode rejected signal as expected ({res_strict.status})")
    else:
        logger.error(f"FAIL: Strict mode accepted signal! {res_strict.status}")

    # Relaxed: padding = 2.0 ATR. ATR = 1.0. Padding = 2.0.
    # Entry = 100. Floor = 98. Price 99 is > 98. Should ACCEPT.
    sb_relaxed = SignalBuilder(force_entry_zone=False, entry_zone_padding=2.0)
    
    res_relaxed = sb_relaxed.build_signal(
        symbol="TEST",
        structure_state=struct_state,
        liquidity_state=liq_state,
        ob_state=ob_state,
        fvg_state=fvg_state,
        mtf_state=mtf_state,
        current_price=99.0, 
        timestamp=1000
    )
    
    if res_relaxed.status == "CREATED":
        logger.info("PASS: Relaxed mode accepted signal via PADDING")
    else:
        logger.error(f"FAIL: Relaxed mode rejected signal! {res_relaxed.status} {res_relaxed.reasons}")

if __name__ == "__main__":
    test_force_entry_bypass()
    test_padding_relaxation()
