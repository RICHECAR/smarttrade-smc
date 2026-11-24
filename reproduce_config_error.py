
import os
import sys

# Mock environment variables to ensure we don't depend on .env file existence for this test
os.environ["AUTO_EXECUTE"] = "true"

try:
    from smc_futures.smc_config import SMC_CONFIG
    print("SMC_CONFIG loaded successfully.")
except ImportError:
    # Adjust path if running from root
    sys.path.append(os.getcwd())
    from smc_futures.smc_config import SMC_CONFIG
    print("SMC_CONFIG loaded successfully (with path adjust).")

def check_config_key():
    print(f"Keys in SMC_CONFIG: {list(SMC_CONFIG.keys())}")
    
    try:
        val = SMC_CONFIG["AUTO_EXECUTE"]
        print(f"FOUND: SMC_CONFIG['AUTO_EXECUTE'] = {val}")
    except KeyError:
        print("ERROR: SMC_CONFIG['AUTO_EXECUTE'] NOT FOUND! This confirms the bug.")

    try:
        val = SMC_CONFIG["AUTO_EXECUTE_ENABLED"]
        print(f"FOUND: SMC_CONFIG['AUTO_EXECUTE_ENABLED'] = {val}")
    except KeyError:
        print("ERROR: SMC_CONFIG['AUTO_EXECUTE_ENABLED'] NOT FOUND!")

if __name__ == "__main__":
    check_config_key()
