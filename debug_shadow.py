import sys
import json
from pathlib import Path

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from smc_futures.engine.strategy.asia_weekend_strategy import AsiaWeekendStrategy
from backend.services.ab_testing_service import ABTestingService

def debug():
    print("--- Shadow Engine Debug ---")
    test_ab_dir = project_root / "reports" / "ab_tests_debug"
    if test_ab_dir.exists():
        import shutil
        shutil.rmtree(test_ab_dir)
    test_ab_dir.mkdir(parents=True)
    
    try:
        strategy = AsiaWeekendStrategy()
        strategy.ab_test_service = ABTestingService(test_ab_dir)
        
        candle = {"high": 100, "low": 90, "close": 95, "open": 92, "volume": 1000, "timestamp": 123456789}
        candles = [candle] * 20
        
        print("Calling build_signal...")
        strategy.build_signal(
            symbol="BTCUSDT",
            candles_htf=candles,
            candles_mtf=candles,
            candles_ltf=candles,
            structure_state={},
            liquidity_state={},
            ob_state={},
            fvg_state={},
            current_price=95.0,
            timestamp=123456789,
            atr=2.0
        )
        
        journals = list(test_ab_dir.glob("decisions_*.jsonl"))
        print(f"Found journals: {journals}")
        if journals:
            with open(journals[0], "r") as f:
                print(f"Content: {f.read()}")
        else:
            print("ERROR: No journals found!")
            
    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    debug()
