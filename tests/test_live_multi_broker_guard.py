"""
Test: LIVE Multi-Broker Fail-Fast Guard
========================================
Verify: smc_config.py refuses to boot when
  TRADING_MODE=live + AUTO_EXECUTE=true + KRAKEN_ENABLED=true
  without CONFIRM_LIVE_MULTI_BROKER=true.

Run:  pytest tests/test_live_multi_broker_guard.py -v
"""

import os
import sys
import pytest
import importlib


class TestLiveMultiBrokerGuard:
    """Tests for the CONFIRM_LIVE_MULTI_BROKER fail-fast safety guard."""

    def _setup_env(self, **overrides):
        """Set env vars with sane defaults, apply overrides."""
        defaults = {
            "TRADING_MODE": "paper",
            "AUTO_EXECUTE": "false",
            "BITGET_ENABLED": "false",
            "KRAKEN_ENABLED": "false",
            "CONFIRM_LIVE_MULTI_BROKER": "false",
            "BITGET_REAL_ONLY": "false",
            "BITGET_PRODUCT_TYPE": "USDT-FUTURES",
            "NODE_ENV": "development",
            "FORCE_EXECUTE": "false",
        }
        defaults.update(overrides)
        for k, v in defaults.items():
            os.environ[k] = v

    def _reload_config(self):
        """Force reimport of smc_config to re-run module-level checks."""
        import smc_futures.smc_config as sc
        importlib.reload(sc)
        return sc

    def _cleanup(self):
        """Remove test env vars."""
        for k in (
            "TRADING_MODE", "AUTO_EXECUTE", "BITGET_ENABLED", "KRAKEN_ENABLED",
            "CONFIRM_LIVE_MULTI_BROKER", "BITGET_REAL_ONLY",
            "BITGET_PRODUCT_TYPE", "NODE_ENV", "FORCE_EXECUTE",
        ):
            os.environ.pop(k, None)

    # ── Test: guard triggers (should RAISE) ──────────────────

    def test_live_auto_kraken_without_confirm_raises(self):
        """LIVE + AUTO_EXECUTE=true + dual broker without confirm → ValueError."""
        self._setup_env(
            TRADING_MODE="live",
            AUTO_EXECUTE="true",
            BITGET_ENABLED="true",
            KRAKEN_ENABLED="true",
            CONFIRM_LIVE_MULTI_BROKER="false",
        )
        try:
            with pytest.raises(ValueError) as exc_info:
                self._reload_config()
            assert "[LIVE_MULTI_BROKER]" in str(exc_info.value)
            assert "CONFIRM_LIVE_MULTI_BROKER" in str(exc_info.value)
        finally:
            self._cleanup()

    # ── Test: guard passes (should NOT raise) ─────────────────

    def test_live_auto_kraken_with_confirm_ok(self):
        """LIVE + AUTO_EXECUTE=true + KRAKEN_ENABLED=true + CONFIRM=true → boots fine."""
        self._setup_env(
            TRADING_MODE="live",
            AUTO_EXECUTE="true",
            KRAKEN_ENABLED="true",
            CONFIRM_LIVE_MULTI_BROKER="true",
        )
        try:
            sc = self._reload_config()
            assert sc.SMC_CONFIG["CONFIRM_LIVE_MULTI_BROKER"] is True
            assert sc.SMC_CONFIG["TRADING_MODE"] == "live"
        finally:
            self._cleanup()

    def test_paper_mode_kraken_no_guard(self):
        """PAPER + KRAKEN_ENABLED=true → guard skipped (not live+auto)."""
        self._setup_env(
            TRADING_MODE="paper",
            AUTO_EXECUTE="false",
            KRAKEN_ENABLED="true",
        )
        try:
            sc = self._reload_config()  # Should not raise
            assert sc.SMC_CONFIG["TRADING_MODE"] == "paper"
        finally:
            self._cleanup()

    def test_live_shadow_kraken_no_guard(self):
        """LIVE + AUTO_EXECUTE=shadow + KRAKEN=true → guard skipped (shadow ≠ true)."""
        self._setup_env(
            TRADING_MODE="live",
            AUTO_EXECUTE="shadow",
            KRAKEN_ENABLED="true",
        )
        try:
            sc = self._reload_config()  # Should not raise
            assert sc.SMC_CONFIG["TRADING_MODE"] == "live"
        finally:
            self._cleanup()

    def test_live_auto_no_kraken_no_guard(self):
        """LIVE + AUTO_EXECUTE=true + KRAKEN_ENABLED=false → guard skipped."""
        self._setup_env(
            TRADING_MODE="live",
            AUTO_EXECUTE="true",
            KRAKEN_ENABLED="false",
        )
        try:
            sc = self._reload_config()  # Should not raise
            assert sc.SMC_CONFIG["TRADING_MODE"] == "live"
        finally:
            self._cleanup()

    # ── Test: MIRROR_TRADING_ENABLED config key ───────────────

    def test_mirror_trading_enabled_in_config(self):
        """MIRROR_TRADING_ENABLED appears in SMC_CONFIG."""
        self._setup_env(MIRROR_TRADING_ENABLED="true")
        os.environ["MIRROR_TRADING_ENABLED"] = "true"
        try:
            sc = self._reload_config()
            assert sc.SMC_CONFIG["MIRROR_TRADING_ENABLED"] is True
        finally:
            self._cleanup()
            os.environ.pop("MIRROR_TRADING_ENABLED", None)

    def test_mirror_trading_default_false(self):
        """MIRROR_TRADING_ENABLED defaults to false when not set in env."""
        self._setup_env()
        # Must explicitly set to false (dotenv would re-read .env value)
        os.environ["MIRROR_TRADING_ENABLED"] = "false"
        try:
            sc = self._reload_config()
            assert sc.SMC_CONFIG["MIRROR_TRADING_ENABLED"] is False
        finally:
            self._cleanup()
            os.environ.pop("MIRROR_TRADING_ENABLED", None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
