import os
import importlib
import unittest

import smc_futures.engine.risk.global_exposure_guardian as global_exposure_guardian
from smc_futures.engine.risk.global_exposure_guardian import ExposureLimits


class TestGlobalExposureGuardian(unittest.TestCase):
    def setUp(self):
        # Ensure env vars are set for deterministic tests
        self._orig = {
            "MAX_TOTAL_EXPOSURE_USD": os.environ.get("MAX_TOTAL_EXPOSURE_USD"),
        }
        os.environ["MAX_TOTAL_EXPOSURE_USD"] = "1000"
        # Reload module to pick up new env vars
        importlib.reload(global_exposure_guardian)

        self.guardian = global_exposure_guardian.GlobalExposureGuardian()
        self.Position = global_exposure_guardian.ExposurePosition

    def tearDown(self):
        # Restore env
        if self._orig["MAX_TOTAL_EXPOSURE_USD"] is None:
            os.environ.pop("MAX_TOTAL_EXPOSURE_USD", None)
        else:
            os.environ["MAX_TOTAL_EXPOSURE_USD"] = self._orig["MAX_TOTAL_EXPOSURE_USD"]

    def test_blocks_when_total_exposure_exceeds_limit(self):
        # Existing open positions total 800 USD
        open_positions = [
            self.Position(symbol="BTCUSDT", side="long", notional_usd=800.0),
        ]
        # New signal would add 300 USD, exceeding max_total=1000
        result = self.guardian.check_signal(
            symbol="ETHUSDT",
            side="long",
            notional_usd=300.0,
            open_positions=open_positions,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "MAX_TOTAL_EXPOSURE")

    def test_allows_when_within_limits(self):
        open_positions = [
            self.Position(symbol="BTCUSDT", side="long", notional_usd=500.0),
        ]
        result = self.guardian.check_signal(
            symbol="ETHUSDT",
            side="short",
            notional_usd=400.0,
            open_positions=open_positions,
        )
        self.assertTrue(result["allowed"])

    def test_throttle_total_exposure(self):
        limits = ExposureLimits(
            max_total_usd=1000,
            max_net_usd=1000,
            max_symbol_usd=1000,
            max_group_usd=1000,
            max_broker_usd=1000,
            throttle_pct=0.8,
        )
        guard = global_exposure_guardian.GlobalExposureGuardian(limits=limits)

        open_positions = [
            self.Position(symbol="BTCUSDT", side="long", notional_usd=700.0),
        ]
        result = guard.check_signal(
            symbol="ETHUSDT",
            side="long",
            notional_usd=150.0,
            open_positions=open_positions,
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "THROTTLE_TOTAL_EXPOSURE")
        self.assertGreater(result.get("throttle_level", 0), 0.8)

    def test_pending_orders_count_towards_exposure(self):
        limits = ExposureLimits(
            max_total_usd=1000,
            max_net_usd=1000,
            max_symbol_usd=1000,
            max_group_usd=1000,
            max_broker_usd=1000,
        )
        guard = global_exposure_guardian.GlobalExposureGuardian(limits=limits)

        open_positions = [
            self.Position(symbol="BTCUSDT", side="long", notional_usd=700.0),
        ]
        pending_positions = [
            self.Position(symbol="BTCUSDT", side="long", notional_usd=100.0),
        ]
        result = guard.check_signal(
            symbol="ETHUSDT",
            side="long",
            notional_usd=250.0,
            open_positions=open_positions,
            pending_positions=pending_positions,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "MAX_TOTAL_EXPOSURE")

    def test_blocks_net_exposure_limit(self):
        limits = ExposureLimits(
            max_total_usd=10000,
            max_net_usd=1000,
            max_symbol_usd=10000,
            max_group_usd=10000,
            max_broker_usd=10000,
        )
        guard = global_exposure_guardian.GlobalExposureGuardian(limits=limits)

        open_positions = [
            self.Position(symbol="BTCUSDT", side="short", notional_usd=900.0),
        ]
        result = guard.check_signal(
            symbol="ETHUSDT",
            side="short",
            notional_usd=200.0,
            open_positions=open_positions,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "MAX_NET_EXPOSURE")

    def test_blocks_symbol_exposure_limit(self):
        limits = ExposureLimits(
            max_total_usd=10000,
            max_net_usd=10000,
            max_symbol_usd=500,
            max_group_usd=10000,
            max_broker_usd=10000,
        )
        guard = global_exposure_guardian.GlobalExposureGuardian(limits=limits)

        open_positions = [
            self.Position(symbol="BTCUSDT", side="long", notional_usd=400.0),
        ]
        result = guard.check_signal(
            symbol="BTCUSDT",
            side="long",
            notional_usd=200.0,
            open_positions=open_positions,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "MAX_SYMBOL_EXPOSURE")

    def test_blocks_broker_exposure_limit(self):
        limits = ExposureLimits(
            max_total_usd=10000,
            max_net_usd=10000,
            max_symbol_usd=10000,
            max_group_usd=10000,
            max_broker_usd=500,
        )
        guard = global_exposure_guardian.GlobalExposureGuardian(limits=limits)

        open_positions = [
            self.Position(symbol="BTCUSDT", side="long", notional_usd=400.0, broker="kraken"),
        ]
        result = guard.check_signal(
            symbol="ETHUSDT",
            side="long",
            notional_usd=200.0,
            open_positions=open_positions,
            broker="kraken",
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "MAX_BROKER_EXPOSURE")

    def test_blocks_group_exposure_limit(self):
        limits = ExposureLimits(
            max_total_usd=10000,
            max_net_usd=10000,
            max_symbol_usd=10000,
            max_group_usd=500,
            max_broker_usd=10000,
            groups_map_raw="BTCUSDT:crypto_beta\nETHUSDT:crypto_beta",
        )
        guard = global_exposure_guardian.GlobalExposureGuardian(limits=limits)

        open_positions = [
            self.Position(symbol="BTCUSDT", side="long", notional_usd=400.0),
        ]
        result = guard.check_signal(
            symbol="ETHUSDT",
            side="long",
            notional_usd=200.0,
            open_positions=open_positions,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "MAX_GROUP_EXPOSURE")


if __name__ == "__main__":
    unittest.main()
