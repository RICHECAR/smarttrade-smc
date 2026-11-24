"""
Security Guard Tests
====================
Tests to ensure security guard-rails are properly enforced.

Run with: pytest tests/test_security_guards.py -v
"""

import os
import sys
import pytest
import importlib
from unittest.mock import patch, MagicMock


class TestForceExecuteProductionBlock:
    """Tests for FORCE_EXECUTE hard-block in production."""
    
    def setup_method(self):
        """Clear relevant env vars before each test."""
        # Save original values
        self._original_env = {
            "NODE_ENV": os.environ.get("NODE_ENV"),
            "FORCE_EXECUTE": os.environ.get("FORCE_EXECUTE"),
            "TRADING_MODE": os.environ.get("TRADING_MODE"),
            "AUTO_EXECUTE": os.environ.get("AUTO_EXECUTE"),
            "BITGET_REAL_ONLY": os.environ.get("BITGET_REAL_ONLY"),
            "BITGET_ENABLED": os.environ.get("BITGET_ENABLED"),
            "KRAKEN_ENABLED": os.environ.get("KRAKEN_ENABLED"),
        }
    
    def teardown_method(self):
        """Restore original env vars after each test."""
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    
    def test_force_execute_blocked_in_production(self):
        """FORCE_EXECUTE must be false when NODE_ENV=production."""
        os.environ["NODE_ENV"] = "production"
        os.environ["FORCE_EXECUTE"] = "true"
        os.environ["TRADING_MODE"] = "live"
        os.environ["AUTO_EXECUTE"] = "true"
        os.environ["BITGET_REAL_ONLY"] = "false"
        os.environ["BITGET_ENABLED"] = "false"
        
        # Remove cached module to force reload
        if "smc_futures.smc_config" in sys.modules:
            del sys.modules["smc_futures.smc_config"]
        
        with patch("dotenv.load_dotenv", return_value=None):
            from smc_futures.smc_config import SMC_CONFIG
        
        assert SMC_CONFIG["FORCE_EXECUTE"] == False, \
            "FORCE_EXECUTE should be blocked in production (NODE_ENV=production)"
    
    def test_force_execute_allowed_in_development(self):
        """FORCE_EXECUTE can be true when NODE_ENV=development."""
        os.environ["NODE_ENV"] = "development"
        os.environ["FORCE_EXECUTE"] = "true"
        os.environ["TRADING_MODE"] = "live"
        os.environ["AUTO_EXECUTE"] = "true"
        os.environ["BITGET_REAL_ONLY"] = "false"
        os.environ["BITGET_ENABLED"] = "false"
        
        if "smc_futures.smc_config" in sys.modules:
            del sys.modules["smc_futures.smc_config"]
        
        with patch("dotenv.load_dotenv", return_value=None):
            from smc_futures.smc_config import SMC_CONFIG
        
        assert SMC_CONFIG["FORCE_EXECUTE"] == True, \
            "FORCE_EXECUTE should be allowed in development"
    
    def test_force_execute_false_by_default(self):
        """FORCE_EXECUTE should default to false."""
        os.environ.pop("FORCE_EXECUTE", None)
        os.environ["NODE_ENV"] = "development"
        os.environ["TRADING_MODE"] = "live"
        os.environ["AUTO_EXECUTE"] = "true"
        os.environ["BITGET_REAL_ONLY"] = "false"
        os.environ["BITGET_ENABLED"] = "false"
        
        if "smc_futures.smc_config" in sys.modules:
            del sys.modules["smc_futures.smc_config"]
        
        with patch("dotenv.load_dotenv", return_value=None):
            from smc_futures.smc_config import SMC_CONFIG
        
        assert SMC_CONFIG["FORCE_EXECUTE"] == False, \
            "FORCE_EXECUTE should default to false"


class TestRealOnlyMode:
    """Tests for BITGET_REAL_ONLY enforcement."""
    
    def setup_method(self):
        self._original_env = {
            "BITGET_REAL_ONLY": os.environ.get("BITGET_REAL_ONLY"),
            "TRADING_MODE": os.environ.get("TRADING_MODE"),
            "AUTO_EXECUTE": os.environ.get("AUTO_EXECUTE"),
            "BITGET_ENABLED": os.environ.get("BITGET_ENABLED"),
            "KRAKEN_ENABLED": os.environ.get("KRAKEN_ENABLED"),
        }
    
    def teardown_method(self):
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    
    def test_real_only_requires_live_mode(self):
        """BITGET_REAL_ONLY=true must fail if TRADING_MODE != live."""
        os.environ["BITGET_REAL_ONLY"] = "true"
        os.environ["TRADING_MODE"] = "paper"
        os.environ["AUTO_EXECUTE"] = "true"
        
        if "smc_futures.smc_config" in sys.modules:
            del sys.modules["smc_futures.smc_config"]
        
        with pytest.raises(ValueError, match=r"BITGET_REAL_ONLY.*requires TRADING_MODE=live"):
            from smc_futures.smc_config import SMC_CONFIG
    
    def test_real_only_requires_auto_execute_true(self):
        """BITGET_REAL_ONLY=true must fail if AUTO_EXECUTE != true."""
        os.environ["BITGET_REAL_ONLY"] = "true"
        os.environ["TRADING_MODE"] = "live"
        os.environ["AUTO_EXECUTE"] = "shadow"
        
        if "smc_futures.smc_config" in sys.modules:
            del sys.modules["smc_futures.smc_config"]
        
        with pytest.raises(ValueError, match=r"BITGET_REAL_ONLY.*requires AUTO_EXECUTE=true"):
            from smc_futures.smc_config import SMC_CONFIG
    
    def test_real_only_default_is_false(self):
        """BITGET_REAL_ONLY should default to false (safe default)."""
        os.environ.pop("BITGET_REAL_ONLY", None)
        os.environ["TRADING_MODE"] = "paper"
        os.environ["AUTO_EXECUTE"] = "false"
        
        if "smc_futures.smc_config" in sys.modules:
            del sys.modules["smc_futures.smc_config"]
        
        # Should NOT raise because REAL_ONLY default is false
        from smc_futures.smc_config import REAL_ONLY_MODE
        
        assert REAL_ONLY_MODE == False, \
            "BITGET_REAL_ONLY should default to false"


class TestManualTradesGuard:
    """Tests for manual trades route protection."""
    
    def setup_method(self):
        self._original_env = {
            "ALLOW_MANUAL_TRADES": os.environ.get("ALLOW_MANUAL_TRADES"),
            "TRADING_MODE": os.environ.get("TRADING_MODE"),
            "AUTO_EXECUTE": os.environ.get("AUTO_EXECUTE"),
            "BITGET_REAL_ONLY": os.environ.get("BITGET_REAL_ONLY"),
            "BITGET_ENABLED": os.environ.get("BITGET_ENABLED"),
            "KRAKEN_ENABLED": os.environ.get("KRAKEN_ENABLED"),
        }
    
    def teardown_method(self):
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    
    def test_manual_trades_blocked_in_paper_mode(self):
        """Manual trades should be blocked when TRADING_MODE=paper."""
        os.environ["TRADING_MODE"] = "paper"
        os.environ["ALLOW_MANUAL_TRADES"] = "true"
        os.environ["AUTO_EXECUTE"] = "false"
        os.environ["BITGET_REAL_ONLY"] = "false"
        os.environ["BITGET_ENABLED"] = "false"
        
        # Reload config first
        if "smc_futures.smc_config" in sys.modules:
            del sys.modules["smc_futures.smc_config"]
        
        # Reload manual module
        if "backend.routes.manual" in sys.modules:
            del sys.modules["backend.routes.manual"]
        
        with patch("dotenv.load_dotenv", return_value=None):
            from backend.routes.manual import is_manual_trades_allowed
        
        assert is_manual_trades_allowed() == False, \
            "Manual trades should be blocked in paper mode"
    
    def test_manual_trades_allowed_in_live_mode(self):
        """Manual trades should be allowed when TRADING_MODE=live and ALLOW_MANUAL_TRADES=true."""
        os.environ["TRADING_MODE"] = "live"
        os.environ["ALLOW_MANUAL_TRADES"] = "true"
        os.environ["AUTO_EXECUTE"] = "true"
        os.environ["BITGET_REAL_ONLY"] = "false"
        os.environ["BITGET_ENABLED"] = "false"
        
        if "smc_futures.smc_config" in sys.modules:
            del sys.modules["smc_futures.smc_config"]
        
        if "backend.routes.manual" in sys.modules:
            del sys.modules["backend.routes.manual"]
        
        with patch("dotenv.load_dotenv", return_value=None):
            from backend.routes.manual import is_manual_trades_allowed
        
        assert is_manual_trades_allowed() == True, \
            "Manual trades should be allowed in live mode"
    
    def test_manual_trades_disabled_by_flag(self):
        """Manual trades should be blocked when ALLOW_MANUAL_TRADES=false."""
        os.environ["TRADING_MODE"] = "live"
        os.environ["ALLOW_MANUAL_TRADES"] = "false"
        os.environ["AUTO_EXECUTE"] = "true"
        os.environ["BITGET_REAL_ONLY"] = "false"
        os.environ["BITGET_ENABLED"] = "false"
        
        if "smc_futures.smc_config" in sys.modules:
            del sys.modules["smc_futures.smc_config"]
        
        if "backend.routes.manual" in sys.modules:
            del sys.modules["backend.routes.manual"]
        
        with patch("dotenv.load_dotenv", return_value=None):
            from backend.routes.manual import is_manual_trades_allowed
        
        assert is_manual_trades_allowed() == False, \
            "Manual trades should be blocked when ALLOW_MANUAL_TRADES=false"


class TestMaxDailyLossEnvVar:
    """Tests for MAX_DAILY_LOSS_PCT env var reading."""
    
    def setup_method(self):
        self._original_env = {
            "MAX_DAILY_LOSS_PCT": os.environ.get("MAX_DAILY_LOSS_PCT"),
            "SMC_MAX_DAILY_LOSS_PCT": os.environ.get("SMC_MAX_DAILY_LOSS_PCT"),
            "TRADING_MODE": os.environ.get("TRADING_MODE"),
            "AUTO_EXECUTE": os.environ.get("AUTO_EXECUTE"),
            "BITGET_REAL_ONLY": os.environ.get("BITGET_REAL_ONLY"),
            "BITGET_ENABLED": os.environ.get("BITGET_ENABLED"),
            "KRAKEN_ENABLED": os.environ.get("KRAKEN_ENABLED"),
        }
    
    def teardown_method(self):
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    
    def test_reads_max_daily_loss_pct_primary(self):
        """Should read MAX_DAILY_LOSS_PCT as primary env var."""
        os.environ["MAX_DAILY_LOSS_PCT"] = "5.0"
        os.environ.pop("SMC_MAX_DAILY_LOSS_PCT", None)
        os.environ["TRADING_MODE"] = "live"
        os.environ["AUTO_EXECUTE"] = "true"
        os.environ["BITGET_REAL_ONLY"] = "false"
        os.environ["BITGET_ENABLED"] = "false"
        
        if "smc_futures.smc_config" in sys.modules:
            del sys.modules["smc_futures.smc_config"]
        
        with patch("dotenv.load_dotenv", return_value=None):
            from smc_futures.smc_config import SMC_CONFIG
        
        assert SMC_CONFIG["MAX_DAILY_LOSS_PCT"] == 5.0, \
            "Should read MAX_DAILY_LOSS_PCT from env"
    
    def test_falls_back_to_smc_prefix(self):
        """Should fallback to SMC_MAX_DAILY_LOSS_PCT if MAX_DAILY_LOSS_PCT not set."""
        os.environ.pop("MAX_DAILY_LOSS_PCT", None)
        os.environ["SMC_MAX_DAILY_LOSS_PCT"] = "4.0"
        os.environ["TRADING_MODE"] = "live"
        os.environ["AUTO_EXECUTE"] = "true"
        os.environ["BITGET_REAL_ONLY"] = "false"
        os.environ["BITGET_ENABLED"] = "false"
        
        if "smc_futures.smc_config" in sys.modules:
            del sys.modules["smc_futures.smc_config"]
        
        with patch("dotenv.load_dotenv", return_value=None):
            from smc_futures.smc_config import SMC_CONFIG
        
        assert SMC_CONFIG["MAX_DAILY_LOSS_PCT"] == 4.0, \
            "Should fallback to SMC_MAX_DAILY_LOSS_PCT"
    
    def test_default_value(self):
        """Should default to 3.0 if no env var set."""
        os.environ.pop("MAX_DAILY_LOSS_PCT", None)
        os.environ.pop("SMC_MAX_DAILY_LOSS_PCT", None)
        os.environ["TRADING_MODE"] = "live"
        os.environ["AUTO_EXECUTE"] = "true"
        os.environ["BITGET_REAL_ONLY"] = "false"
        os.environ["BITGET_ENABLED"] = "false"
        
        if "smc_futures.smc_config" in sys.modules:
            del sys.modules["smc_futures.smc_config"]
        
        with patch("dotenv.load_dotenv", return_value=None):
            from smc_futures.smc_config import SMC_CONFIG
        
        assert SMC_CONFIG["MAX_DAILY_LOSS_PCT"] == 3.0, \
            "Should default to 3.0"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
