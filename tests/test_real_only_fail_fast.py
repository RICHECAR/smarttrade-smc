"""
Test: REAL ONLY Fail-Fast Enforcement
Verify: Service refuses to boot if TRADING_MODE/AUTO_EXECUTE misconfigured
"""

import pytest
import sys
import os
from io import StringIO


class TestRealOnlyFailFast:
    """REAL ONLY configuration tests."""

    def setup_method(self):
        """Save original env vars before each test."""
        self._saved_env = {}
        for key in ("BITGET_REAL_ONLY", "TRADING_MODE", "AUTO_EXECUTE", "BITGET_PRODUCT_TYPE",
                    "BITGET_ENABLED", "KRAKEN_ENABLED"):
            self._saved_env[key] = os.environ.get(key)

    def teardown_method(self):
        """Restore original env vars after each test."""
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_real_only_requires_live_mode(self):
        """Verify BITGET_REAL_ONLY=true requires TRADING_MODE=live."""
        # Set environment
        os.environ["BITGET_REAL_ONLY"] = "true"
        os.environ["TRADING_MODE"] = "paper"
        os.environ["AUTO_EXECUTE"] = "true"
        
        # Must raise ValueError on import
        with pytest.raises(ValueError) as exc_info:
            # Force re-import to trigger config validation
            import importlib
            import smc_futures.smc_config as sc
            importlib.reload(sc)
        
        assert "[REAL_ONLY]" in str(exc_info.value)
        assert "TRADING_MODE=live" in str(exc_info.value)
        
        # Clean up
        del os.environ["BITGET_REAL_ONLY"]

    def test_real_only_requires_auto_execute(self):
        """Verify BITGET_REAL_ONLY=true requires AUTO_EXECUTE=true."""
        # Set environment
        os.environ["BITGET_REAL_ONLY"] = "true"
        os.environ["TRADING_MODE"] = "live"
        os.environ["AUTO_EXECUTE"] = "shadow"  # NOT true
        
        # Must raise ValueError on import
        with pytest.raises(ValueError) as exc_info:
            import importlib
            import smc_futures.smc_config as sc
            importlib.reload(sc)
        
        assert "[REAL_ONLY]" in str(exc_info.value)
        assert "AUTO_EXECUTE=true" in str(exc_info.value)
        
        # Clean up
        del os.environ["BITGET_REAL_ONLY"]

    def test_auto_execute_true_paper_mode_contradicts(self):
        """Verify AUTO_EXECUTE=true + TRADING_MODE=paper disables AUTO_EXECUTE."""
        # This should NOT raise, but should disable AUTO_EXECUTE internally
        os.environ["AUTO_EXECUTE"] = "true"
        os.environ["TRADING_MODE"] = "paper"
        os.environ.pop("BITGET_REAL_ONLY", None)  # Don't use strict mode
        
        # Reload config
        import importlib
        import smc_futures.smc_config as sc
        importlib.reload(sc)
        
        # AUTO_EXECUTE_ENABLED should be False due to paper/true contradiction
        assert sc.AUTO_EXECUTE_ENABLED is False

    def test_trading_mode_invalid_raises(self):
        """Verify invalid TRADING_MODE raises on startup."""
        os.environ["TRADING_MODE"] = "invalid_mode"
        
        try:
            with pytest.raises(ValueError) as exc_info:
                import importlib
                import smc_futures.smc_config as sc
                importlib.reload(sc)
            
            assert "TRADING_MODE must be 'live' or 'paper'" in str(exc_info.value)
        finally:
            os.environ["TRADING_MODE"] = "live"

    def test_product_type_enforced_usdt_futures(self):
        """Verify BITGET_PRODUCT_TYPE normalized to USDT-FUTURES."""
        os.environ["BITGET_PRODUCT_TYPE"] = "UMCBL"
        os.environ["TRADING_MODE"] = "live"
        os.environ["BITGET_ENABLED"] = "false"
        os.environ.pop("BITGET_REAL_ONLY", None)
        
        import importlib
        import smc_futures.smc_config as sc
        from unittest.mock import patch
        with patch("dotenv.load_dotenv", return_value=None):
            importlib.reload(sc)
        
        assert sc.SMC_CONFIG["BITGET_PRODUCT_TYPE"] == "USDT-FUTURES"

    def test_product_type_invalid_raises(self):
        """Verify invalid productType raises on startup."""
        os.environ["BITGET_PRODUCT_TYPE"] = "COIN-FUTURES"  # NOT a recognized type
        os.environ["TRADING_MODE"] = "live"
        os.environ["BITGET_ENABLED"] = "false"
        
        with pytest.raises(ValueError) as exc_info:
            import importlib
            import smc_futures.smc_config as sc
            from unittest.mock import patch
            with patch("dotenv.load_dotenv", return_value=None):
                importlib.reload(sc)
        
        assert "USDT-FUTURES" in str(exc_info.value)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
