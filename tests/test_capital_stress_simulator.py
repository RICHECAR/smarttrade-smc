import os
import unittest

import smc_futures.engine.risk.capital_stress_simulator as css
from smc_futures.engine.risk.capital_stress_simulator import ExposurePosition


class TestCapitalStressSimulator(unittest.TestCase):
    def setUp(self):
        # Use a deterministic scenario where BTC/ETH drop 10% for stress
        scenario = css.StressScenario(
            name="test_shock",
            symbol_moves={"BTCUSDT": -0.10, "ETHUSDT": -0.10},
            group_moves={},
            slippage_mult=1.0,
            gap_pct=0.0,
        )
        self.sim = css.CapitalStressSimulator(scenarios=[scenario])

    def tearDown(self):
        for k in [
            "STRESS_CORRELATION_WEIGHTS",
            "STRESS_SLIPPAGE_FACTORS",
            "STRESS_SLIPPAGE_BASE_PCT",
        ]:
            if k in os.environ:
                del os.environ[k]

    def test_blocks_when_stress_loss_exceeds_limit(self):
        self.sim.max_stress_loss_usd = 100.0
        open_positions = [ExposurePosition(symbol="BTCUSDT", side="long", notional_usd=800.0)]
        result = self.sim.check_signal(
            symbol="ETHUSDT",
            side="long",
            notional_usd=200.0,
            account_balance=10000.0,
            open_positions=open_positions,
            pending_positions=[],
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "STRESS_LOSS_EXCEEDED")

    def test_includes_pending_orders_in_stress_projection(self):
        self.sim.max_stress_loss_usd = 10.0
        open_positions = [ExposurePosition(symbol="BTCUSDT", side="long", notional_usd=50.0)]
        pending_positions = [ExposurePosition(symbol="ETHUSDT", side="long", notional_usd=50.0)]
        result = self.sim.check_signal(
            symbol="SOLUSDT",
            side="long",
            notional_usd=50.0,
            account_balance=10000.0,
            open_positions=open_positions,
            pending_positions=pending_positions,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "STRESS_LOSS_EXCEEDED")

    def test_uses_correlation_weights_to_reduce_effective_loss(self):
        os.environ["STRESS_CORRELATION_WEIGHTS"] = "BTCUSDT:1.0,ETHUSDT:0.9,SOLUSDT:0.6"
        sim = css.CapitalStressSimulator(scenarios=[
            css.StressScenario(
                name="test_corr",
                symbol_moves={"BTCUSDT": -0.10, "ETHUSDT": -0.10, "SOLUSDT": -0.10},
                group_moves={},
                slippage_mult=1.0,
                gap_pct=0.0,
            )
        ])
        sim.max_stress_loss_usd = 260.0
        result = sim.check_signal(
            symbol="BTCUSDT",
            side="long",
            notional_usd=0.0,
            account_balance=10000.0,
            open_positions=[
                ExposurePosition(symbol="BTCUSDT", side="long", notional_usd=1000.0),
                ExposurePosition(symbol="ETHUSDT", side="long", notional_usd=1000.0),
                ExposurePosition(symbol="SOLUSDT", side="long", notional_usd=1000.0),
            ],
            pending_positions=[],
        )
        self.assertTrue(result["allowed"])

    def test_slippage_scales_by_liquidity_factor(self):
        os.environ["STRESS_SLIPPAGE_FACTORS"] = "ETHUSDT:3.0"
        os.environ["STRESS_SLIPPAGE_BASE_PCT"] = "0.02"
        sim = css.CapitalStressSimulator(scenarios=[
            css.StressScenario(
                name="test_slippage",
                symbol_moves={"ETHUSDT": 0.0},
                group_moves={},
                slippage_mult=3.0,
                gap_pct=0.0,
            )
        ])
        sim.max_stress_loss_usd = 10.0
        result = sim.check_signal(
            symbol="ETHUSDT",
            side="long",
            notional_usd=200.0,
            account_balance=10000.0,
            open_positions=[],
            pending_positions=[],
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "STRESS_LOSS_EXCEEDED")

    def test_throttle_reason_is_returned_before_block(self):
        self.sim.max_stress_dd_pct = 10.0
        self.sim.max_stress_loss_usd = 10000.0
        self.sim.max_stress_margin_pct = 100.0
        open_positions = [ExposurePosition(symbol="BTCUSDT", side="long", notional_usd=80.0)]
        result = self.sim.check_signal(
            symbol="ETHUSDT",
            side="long",
            notional_usd=0.0,
            account_balance=100.0,
            open_positions=open_positions,
            pending_positions=[],
        )
        self.assertTrue(result["allowed"])
        self.assertTrue(result["reason"].startswith("THROTTLE_DD_"))

    def test_blocks_when_drawdown_exceeds_limit(self):
        self.sim.max_stress_dd_pct = 1.0
        open_positions = [ExposurePosition(symbol="BTCUSDT", side="long", notional_usd=900.0)]
        result = self.sim.check_signal(
            symbol="ETHUSDT",
            side="long",
            notional_usd=200.0,
            account_balance=10000.0,
            open_positions=open_positions,
            pending_positions=[],
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "STRESS_DD_EXCEEDED")

    def test_blocks_when_margin_exceeds_limit(self):
        self.sim.max_stress_margin_pct = 10.0
        open_positions = [ExposurePosition(symbol="BTCUSDT", side="long", notional_usd=100.0)]
        result = self.sim.check_signal(
            symbol="ETHUSDT",
            side="long",
            notional_usd=2000.0,
            account_balance=10000.0,
            open_positions=open_positions,
            pending_positions=[],
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "STRESS_MARGIN_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
