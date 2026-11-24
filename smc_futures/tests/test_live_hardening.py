import unittest
import os
import logging
from unittest.mock import MagicMock, patch
import asyncio

from smc_futures.app_context import AppContext
from smc_futures.engine.execution.bitget_v2_client import BitgetV2Client
from smc_futures.security.redaction import RedactingFormatter
from smc_futures.smc_config import SMC_CONFIG

class TestLiveHardening(unittest.IsolatedAsyncioTestCase):

    def test_app_context_stale_threshold_default(self):
        """Verify AppContext defaults to 250ms if not in config."""
        # Force a config without STALE_THRESHOLD_MS
        mock_config = SMC_CONFIG.copy()
        if "STALE_THRESHOLD_MS" in mock_config:
            del mock_config["STALE_THRESHOLD_MS"]
            
        ctx = AppContext(config=mock_config)
        self.assertEqual(ctx.market_client.stale_threshold_ms, 250)

    @patch("smc_futures.engine.execution.bitget_v2_client.os.getenv")
    @patch("smc_futures.engine.execution.bitget_v2_client.asyncio.sleep")
    async def test_bitget_v2_live_stale_no_sleep(self, mock_sleep, mock_getenv):
        """Verify that in LIVE mode, stale data triggers immediate reject without sleep."""
        # Setup
        mock_getenv.side_effect = lambda k, v=None: "live" if k == "TRADING_MODE" else v
        client = BitgetV2Client(stale_threshold_ms=10)
        
        # Mock _fetch_ticker_once to return stale data
        client._fetch_ticker_once = MagicMock()
        client._fetch_ticker_once.return_value = asyncio.Future()
        client._fetch_ticker_once.return_value.set_result({
            "stale": True,
            "age_ms": 500,
            "price": 50000.0
        })
        
        # Execute
        result = await client.get_ticker("BTCUSDT")
        
        # Verify
        self.assertTrue(result["stale"])
        mock_sleep.assert_not_called()

    def test_log_redaction_exact_match(self):
        """Verify that secrets are redacted regardless of length."""
        # Register a fake secret
        secret = "MY_VERY_SECRET_KEY_123"
        short_secret = "KEY"
        
        from smc_futures.security.secrets import _LOADED_SECRETS
        _LOADED_SECRETS.add(secret)
        _LOADED_SECRETS.add(short_secret)
        
        formatter = RedactingFormatter("%(message)s")
        record = logging.LogRecord("test", logging.INFO, "src", 10, f"Connecting with {secret} and {short_secret}", None, None)
        
        output = formatter.format(record)
        self.assertNotIn(secret, output)
        self.assertNotIn(short_secret, output)
        self.assertIn("[REDACTED]", output)
        self.assertEqual(output.count("[REDACTED]"), 2)

    def test_redaction_operational_keys(self):
        """Verify that operational keys (EXEC/KILL) are redacted."""
        from smc_futures.security.secrets import _LOADED_SECRETS
        mock_exec = "kraken_execute_primary_999"
        mock_kill = "bitget_kill_switch_001"
        _LOADED_SECRETS.add(mock_exec)
        _LOADED_SECRETS.add(mock_kill)
        
        formatter = RedactingFormatter("%(message)s")
        log_line = f"DEBUG: using {mock_exec} and {mock_kill} for operation"
        record = logging.LogRecord("test", logging.DEBUG, "src", 20, log_line, None, None)
        
        output = formatter.format(record)
        self.assertNotIn(mock_exec, output)
        self.assertNotIn(mock_kill, output)
        self.assertIn("[REDACTED]", output)

    @patch("smc_futures.engine.execution.bitget_v2_client.os.getenv")
    async def test_prometheus_stale_metric(self, mock_getenv):
        """Verify that REJECTS_TOTAL is incremented on stale data in live mode."""
        from smc_futures.observability.metrics import REJECTS_TOTAL
        
        # Get count before
        try:
            before = REJECTS_TOTAL.labels(reason="STALE_DATA")._value.get()
        except:
            before = 0
            
        # Setup live mode
        mock_getenv.side_effect = lambda k, v=None: "live" if k == "TRADING_MODE" else v
        client = BitgetV2Client(stale_threshold_ms=10)
        client._fetch_ticker_once = MagicMock()
        client._fetch_ticker_once.return_value = asyncio.Future()
        client._fetch_ticker_once.return_value.set_result({"stale": True, "age_ms": 500})
        
        # Execute
        await client.get_ticker("BTCUSDT")
        
        # Verify increment
        after = REJECTS_TOTAL.labels(reason="STALE_DATA")._value.get()
        self.assertEqual(after, before + 1)

if __name__ == "__main__":
    unittest.main()
