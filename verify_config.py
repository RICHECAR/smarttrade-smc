
import os
import sys
# Mock .env loading
os.environ["AUTO_EXECUTE"] = "false"
os.environ["TRADING_MODE"] = "live"
os.environ["FORCE_ENTRY_ZONE"] = "true"

from smc_futures.smc_config import SMC_CONFIG, AUTO_EXECUTE_ENABLED, SHADOW_MODE

print(f"AUTO_EXECUTE_ENABLED: {AUTO_EXECUTE_ENABLED}")
print(f"SHADOW_MODE: {SHADOW_MODE}")
print(f"FORCE_ENTRY_ZONE: {SMC_CONFIG['FORCE_ENTRY_ZONE']}")

# Test priority
os.environ["AUTO_EXECUTE"] = "true"
# Reload module is tricky, so we just check logic manually in a separate block if needed.
# But for now, let's just verify the import works and values are as expected for the first case.
