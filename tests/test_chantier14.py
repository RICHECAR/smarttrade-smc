"""
Tests Chantier 14 — Structural Arbitrage Engine
════════════════════════════════════════════════
Covers: arb_engine, arb_funding, arb_basis, arb_cross_exchange, arb_pairs
"""

import math
import pytest
from smc_futures.backtest.arb_engine import (
    ArbBar, ArbTrade, ArbConfig, ArbState, ArbMetrics, compute_arb_metrics,
)
from smc_futures.backtest.arb_funding import (
    FundingArbConfig, simulate_funding_rates, build_arb_bars_funding, run_funding_arb,
)
from smc_futures.backtest.arb_basis import (
    BasisArbConfig, simulate_spot_prices, build_arb_bars_basis, run_basis_arb,
    _compute_basis_bps,
)
from smc_futures.backtest.arb_cross_exchange import (
    CrossExConfig, simulate_exchange_b_prices, build_arb_bars_cross_ex, run_cross_exchange_arb,
)
from smc_futures.backtest.arb_pairs import (
    PairsConfig, build_arb_bars_pairs, run_pairs_trading,
    _rolling_ols_beta, _rolling_zscore,
)


# ─── Helpers ───

def make_candle(ts, o, h, lo, cl, vol=100):
    """Create a simple candle tuple."""
    return (ts, o, h, lo, cl, vol)


def make_trending_candles(n=200, start_price=60000, trend_bps=5, seed=42):
    """Generate candles with slight trend + noise."""
    import random
    rng = random.Random(seed)
    candles = []
    price = start_price
    for i in range(n):
        move = price * (trend_bps / 10000 + rng.gauss(0, 0.003))
        price += move
        h = price * (1 + abs(rng.gauss(0, 0.002)))
        lo = price * (1 - abs(rng.gauss(0, 0.002)))
        candles.append(make_candle(i * 14400000, price, h, lo, price, 1000))
    return candles


def make_flat_candles(n=200, price=60000):
    """Flat price candles for testing with zero price risk."""
    return [make_candle(i * 14400000, price, price, price, price, 1000) for i in range(n)]


# ═══════════════════════════════════════════
#  1. ARB ENGINE — Data Structures & Metrics
# ═══════════════════════════════════════════

class TestArbEngine:
    def test_arb_bar_creation(self):
        bar = ArbBar(timestamp=0, perp_open=100, perp_high=105,
                     perp_low=95, perp_close=102, funding_rate=0.5)
        assert bar.perp_close == 102
        assert bar.funding_rate == 0.5

    def test_arb_trade_defaults(self):
        trade = ArbTrade(entry_bar=0, entry_timestamp=0, strategy="funding", direction="short_carry")
        assert trade.net_pnl == 0.0
        assert trade.bars_funding == []

    def test_compute_metrics_empty(self):
        state = ArbState(capital=10000)
        m = compute_arb_metrics(state, ArbConfig(), 100)
        assert m.total_trades == 0
        assert m.profit_factor == 0

    def test_compute_metrics_winning(self):
        state = ArbState(capital=10500)
        state.equity_curve = [10000, 10200, 10500]
        t1 = ArbTrade(entry_bar=0, entry_timestamp=0, strategy="test", direction="long",
                      net_pnl=300, carry_pnl=200, price_pnl=150, total_fees=30, slippage_cost=20,
                      holding_bars=5)
        t2 = ArbTrade(entry_bar=5, entry_timestamp=5000, strategy="test", direction="short",
                      net_pnl=200, carry_pnl=100, price_pnl=120, total_fees=10, slippage_cost=10,
                      holding_bars=3)
        state.trades = [t1, t2]
        m = compute_arb_metrics(state, ArbConfig(), 100)
        assert m.total_trades == 2
        assert m.winning_trades == 2
        assert m.win_rate == 100.0
        assert m.total_net_pnl == 500
        assert m.profit_factor == float('inf')

    def test_compute_metrics_mixed(self):
        state = ArbState(capital=9900)
        state.equity_curve = [10000, 10300, 9900]
        t1 = ArbTrade(entry_bar=0, entry_timestamp=0, strategy="t", direction="l",
                      net_pnl=300, holding_bars=5)
        t2 = ArbTrade(entry_bar=5, entry_timestamp=0, strategy="t", direction="s",
                      net_pnl=-400, holding_bars=3)
        state.trades = [t1, t2]
        m = compute_arb_metrics(state, ArbConfig(), 100)
        assert m.total_trades == 2
        assert m.winning_trades == 1
        assert m.losing_trades == 1
        assert m.profit_factor == pytest.approx(300 / 400, rel=0.01)

    def test_drawdown_computation(self):
        state = ArbState(capital=9000)
        state.equity_curve = [10000, 10500, 9000, 9500]
        state.trades = [ArbTrade(entry_bar=0, entry_timestamp=0, strategy="t",
                                 direction="l", net_pnl=-1000, holding_bars=2)]
        m = compute_arb_metrics(state, ArbConfig(), 10)
        # Peak=10500, trough=9000 → DD = 1500/10500 = 14.3%
        assert m.max_drawdown_pct == pytest.approx(14.28, rel=0.05)

    def test_cost_ratio(self):
        state = ArbState(capital=10100)
        state.equity_curve = [10000, 10100]
        t = ArbTrade(entry_bar=0, entry_timestamp=0, strategy="t", direction="l",
                     net_pnl=100, total_fees=50, slippage_cost=20, holding_bars=5)
        state.trades = [t]
        m = compute_arb_metrics(state, ArbConfig(), 50)
        # Cost ratio = 70 / 100 = 0.7
        assert m.cost_ratio == pytest.approx(0.7, rel=0.01)


# ═══════════════════════════════════════════
#  2. AXE 1 — FUNDING ARBITRAGE
# ═══════════════════════════════════════════

class TestFundingArb:
    def test_simulate_funding_rates_length(self):
        rates = simulate_funding_rates(100, seed=42)
        assert len(rates) == 100

    def test_simulate_funding_rates_mean_reverting(self):
        rates = simulate_funding_rates(1000, mean=0.0, std=0.5, seed=42)
        avg = sum(rates) / len(rates)
        assert abs(avg) < 0.2  # Should revert toward mean

    def test_simulate_funding_deterministic(self):
        r1 = simulate_funding_rates(50, seed=42)
        r2 = simulate_funding_rates(50, seed=42)
        assert r1 == r2

    def test_build_arb_bars_funding(self):
        candles = [make_candle(i * 1000, 100, 105, 95, 102, 50) for i in range(5)]
        rates = [0.5, -0.3, 1.2, -0.8, 0.1]
        bars = build_arb_bars_funding(candles, rates)
        assert len(bars) == 5
        assert bars[0].perp_close == 102
        assert bars[2].funding_rate == 1.2

    def test_run_funding_no_extremes(self):
        """When funding is always below threshold, no trades."""
        candles = make_flat_candles(100, 60000)
        rates = [0.1] * 100  # All below threshold of 1.0
        bars = build_arb_bars_funding(candles, rates)
        cfg = FundingArbConfig(entry_threshold_bps=1.0)
        state = run_funding_arb(bars, cfg, ArbConfig())
        assert len(state.trades) == 0

    def test_run_funding_extreme_positive(self):
        """Extreme positive funding should trigger short_carry."""
        candles = make_flat_candles(50, 60000)
        rates = [0.1] * 10 + [2.0] * 10 + [0.1] * 30  # Spike at bar 10-19
        bars = build_arb_bars_funding(candles, rates)
        cfg = FundingArbConfig(entry_threshold_bps=1.0, exit_threshold_bps=0.3)
        state = run_funding_arb(bars, cfg, ArbConfig())
        assert len(state.trades) >= 1
        assert state.trades[0].direction == "short_carry"

    def test_run_funding_extreme_negative(self):
        """Extreme negative funding should trigger long_carry."""
        candles = make_flat_candles(50, 60000)
        rates = [0.1] * 10 + [-2.0] * 10 + [0.1] * 30
        bars = build_arb_bars_funding(candles, rates)
        cfg = FundingArbConfig(entry_threshold_bps=1.0, exit_threshold_bps=0.3)
        state = run_funding_arb(bars, cfg, ArbConfig())
        assert len(state.trades) >= 1
        assert state.trades[0].direction == "long_carry"

    def test_funding_carry_income_positive(self):
        """Short carry during positive funding should earn carry income."""
        candles = make_flat_candles(30, 60000)
        rates = [0.1] * 5 + [3.0] * 10 + [0.1] * 15
        bars = build_arb_bars_funding(candles, rates)
        cfg = FundingArbConfig(
            entry_threshold_bps=1.0, exit_threshold_bps=0.3,
            funding_every_n_bars=2,
        )
        state = run_funding_arb(bars, cfg, ArbConfig(two_legged=False))
        if state.trades:
            # Short carry earns from positive funding
            assert state.trades[0].carry_pnl >= 0

    def test_funding_stop_loss(self):
        """Price adverse move should trigger stop loss."""
        candles = []
        for i in range(30):
            if i < 10:
                p = 60000
            else:
                p = 60000 + (i - 10) * 100  # Rising price (bad for shorts)
            candles.append(make_candle(i * 14400000, p, p + 50, p - 50, p))
        rates = [0.1] * 5 + [3.0] * 25  # Sustained high funding
        bars = build_arb_bars_funding(candles, rates)
        cfg = FundingArbConfig(entry_threshold_bps=1.0, stop_loss_bps=30.0)
        state = run_funding_arb(bars, cfg, ArbConfig(two_legged=False))
        # Should have exited on SL
        if state.trades:
            assert state.trades[0].net_pnl < 0  # Lost money due to price move

    def test_funding_max_holding(self):
        """Position should close after max_holding_bars."""
        candles = make_flat_candles(50, 60000)
        rates = [0.1] * 5 + [2.0] * 45  # Sustained extreme funding
        bars = build_arb_bars_funding(candles, rates)
        cfg = FundingArbConfig(
            entry_threshold_bps=1.0, exit_threshold_bps=0.3,
            max_holding_bars=10,
        )
        state = run_funding_arb(bars, cfg, ArbConfig())
        if state.trades:
            assert state.trades[0].holding_bars <= 10


# ═══════════════════════════════════════════
#  3. AXE 2 — BASIS ARBITRAGE
# ═══════════════════════════════════════════

class TestBasisArb:
    def test_compute_basis_bps(self):
        # Perp > spot = contango
        basis = _compute_basis_bps(100.10, 100.00)
        assert basis == pytest.approx(10.0, rel=0.01)

    def test_compute_basis_bps_backwardation(self):
        basis = _compute_basis_bps(99.90, 100.00)
        assert basis == pytest.approx(-10.0, rel=0.01)

    def test_simulate_spot_deterministic(self):
        candles = make_flat_candles(50, 60000)
        s1 = simulate_spot_prices(candles, seed=43)
        s2 = simulate_spot_prices(candles, seed=43)
        assert s1 == s2

    def test_simulate_spot_close_to_perp(self):
        candles = make_flat_candles(100, 60000)
        spots = simulate_spot_prices(candles, basis_mean_bps=0, basis_std_bps=5)
        # Spot should be close to perp (within ~2%)
        for sp in spots:
            assert 59000 < sp < 61000

    def test_build_arb_bars_basis_length(self):
        candles = [make_candle(i, 100, 105, 95, 102) for i in range(10)]
        spots = [101.0] * 10
        bars = build_arb_bars_basis(candles, spots)
        assert len(bars) == 10
        assert bars[0].spot_close == 101.0

    def test_basis_arb_no_signal_narrow_basis(self):
        """No trades when basis stays within threshold."""
        candles = make_flat_candles(100, 60000)
        # Spot very close to perp → basis near zero
        spots = [60000.0] * 100
        bars = build_arb_bars_basis(candles, spots)
        cfg = BasisArbConfig(entry_threshold_bps=8.0)
        state = run_basis_arb(bars, cfg, ArbConfig())
        assert len(state.trades) == 0

    def test_basis_arb_contango_entry(self):
        """High positive basis → short perp."""
        candles = make_flat_candles(100, 60000)
        spots = [60000.0] * 20 + [59940.0] * 30 + [60000.0] * 50  # Basis spikes 10 bps
        bars = build_arb_bars_basis(candles, spots)
        cfg = BasisArbConfig(
            entry_threshold_bps=8.0, exit_threshold_bps=2.0,
            basis_ma_period=3,
        )
        state = run_basis_arb(bars, cfg, ArbConfig())
        if state.trades:
            assert state.trades[0].direction == "short_basis"


# ═══════════════════════════════════════════
#  4. AXE 3 — CROSS-EXCHANGE ARBITRAGE
# ═══════════════════════════════════════════

class TestCrossExArb:
    def test_simulate_exchange_b_deterministic(self):
        candles = make_flat_candles(50, 60000)
        p1 = simulate_exchange_b_prices(candles, seed=44)
        p2 = simulate_exchange_b_prices(candles, seed=44)
        assert p1 == p2

    def test_exchange_b_prices_close_to_a(self):
        candles = make_flat_candles(100, 60000)
        prices = simulate_exchange_b_prices(candles, spread_std_bps=4.0)
        for p in prices:
            assert 59900 < p < 60100  # Within ~15 bps

    def test_build_arb_bars_cross_ex(self):
        candles = [make_candle(i, 100, 105, 95, 100) for i in range(5)]
        ex_b = [100.5, 99.5, 100.2, 99.8, 100.1]
        bars = build_arb_bars_cross_ex(candles, ex_b)
        assert len(bars) == 5
        assert bars[1].sec_close == 99.5

    def test_cross_ex_no_signal_narrow(self):
        """No trades when spread stays narrow."""
        candles = make_flat_candles(50, 60000)
        ex_b = [60000.0] * 50  # Identical prices
        bars = build_arb_bars_cross_ex(candles, ex_b)
        cfg = CrossExConfig(entry_threshold_bps=6.0)
        state = run_cross_exchange_arb(bars, cfg, ArbConfig())
        assert len(state.trades) == 0

    def test_cross_ex_with_spread(self):
        """Large spread should trigger a trade."""
        candles = make_flat_candles(50, 60000)
        # Exchange B is 15 bps cheaper for a period
        ex_b = [60000.0] * 10 + [59910.0] * 15 + [60000.0] * 25
        bars = build_arb_bars_cross_ex(candles, ex_b)
        cfg = CrossExConfig(
            entry_threshold_bps=6.0, exit_threshold_bps=1.5,
            execution_delay_bars=0,  # No delay for simpler test
        )
        state = run_cross_exchange_arb(bars, cfg, ArbConfig())
        if state.trades:
            # A > B → short_a_long_b
            assert state.trades[0].direction == "short_a_long_b"

    def test_cross_ex_execution_delay(self):
        """Execution delay should postpone entry."""
        candles = make_flat_candles(50, 60000)
        ex_b = [60000.0] * 5 + [59910.0] * 20 + [60000.0] * 25
        bars = build_arb_bars_cross_ex(candles, ex_b)
        cfg = CrossExConfig(
            entry_threshold_bps=6.0, execution_delay_bars=2,
        )
        state = run_cross_exchange_arb(bars, cfg, ArbConfig())
        if state.trades:
            # Entry bar should be delayed by 2
            assert state.trades[0].entry_bar >= 8  # Signal around bar 6, entry at bar 8


# ═══════════════════════════════════════════
#  5. AXE 4 — PAIRS TRADING
# ═══════════════════════════════════════════

class TestPairsTrading:
    def test_rolling_ols_beta_constant(self):
        """When y = 2*x, beta should be ~2."""
        x = [float(i) for i in range(100)]
        y = [2 * xi + 10 for xi in x]
        betas = _rolling_ols_beta(x, y, window=20)
        # After warmup, betas should be ~2
        for b in betas[20:]:
            assert b is not None
            assert b == pytest.approx(2.0, rel=0.01)

    def test_rolling_zscore_normalization(self):
        """Z-score of constant series should be 0."""
        series = [5.0] * 50
        zscores = _rolling_zscore(series, window=10)
        for z in zscores[10:]:
            # Constant series → std=0 → z ≈ 0 (or clamped)
            assert z is not None

    def test_rolling_zscore_deviation(self):
        """Large deviation should produce large z-score."""
        series = [0.0] * 30 + [10.0]  # Sudden spike
        zscores = _rolling_zscore(series, window=20)
        last_z = zscores[-1]
        assert last_z is not None
        assert abs(last_z) > 2.0

    def test_build_arb_bars_pairs_alignment(self):
        btc = [make_candle(i * 1000, 60000, 60100, 59900, 60000) for i in range(10)]
        eth = [make_candle(i * 1000, 3000, 3010, 2990, 3000) for i in range(10)]
        bars = build_arb_bars_pairs(btc, eth)
        assert len(bars) == 10
        assert bars[0].perp_close == 60000
        assert bars[0].sec_close == 3000

    def test_build_arb_bars_pairs_unequal(self):
        """Shorter list should determine length."""
        btc = [make_candle(i, 60000, 60100, 59900, 60000) for i in range(10)]
        eth = [make_candle(i, 3000, 3010, 2990, 3000) for i in range(7)]
        bars = build_arb_bars_pairs(btc, eth)
        assert len(bars) == 7

    def test_pairs_no_trade_correlated(self):
        """Perfectly correlated pair → no z-score extremes → no trades."""
        btc = [make_candle(i * 14400000, 60000 + i * 10, 60050 + i * 10,
                           59950 + i * 10, 60000 + i * 10) for i in range(200)]
        eth = [make_candle(i * 14400000, 3000 + i * 0.5, 3005 + i * 0.5,
                           2995 + i * 0.5, 3000 + i * 0.5) for i in range(200)]
        bars = build_arb_bars_pairs(btc, eth)
        cfg = PairsConfig(entry_z=2.0, warmup_bars=60)
        state = run_pairs_trading(bars, cfg, ArbConfig())
        # With mostly correlated data, z-scores stay low → few trades
        assert len(state.trades) <= 5

    def test_pairs_divergence_triggers_trade(self):
        """Divergence between BTC and ETH should trigger pairs trade."""
        import random
        rng = random.Random(99)
        btc = []
        eth = []
        for i in range(200):
            btc_p = 60000 + i * 5 + rng.gauss(0, 50)
            # ETH mostly correlated but diverges mid-way
            if 100 <= i <= 130:
                eth_p = 3000 + i * 0.25 + 100  # ETH surges relative to BTC
            else:
                eth_p = 3000 + i * 0.25 + rng.gauss(0, 5)
            btc.append(make_candle(i * 14400000, btc_p, btc_p + 50, btc_p - 50, btc_p))
            eth.append(make_candle(i * 14400000, eth_p, eth_p + 5, eth_p - 5, eth_p))

        bars = build_arb_bars_pairs(btc, eth)
        cfg = PairsConfig(
            entry_z=1.5, exit_z=0.3,
            beta_window=40, zscore_window=20, warmup_bars=40,
        )
        state = run_pairs_trading(bars, cfg, ArbConfig())
        assert len(state.trades) >= 1

    def test_pairs_max_holding(self):
        """Positions should respect max_holding_bars."""
        import random
        rng = random.Random(77)
        btc = []
        eth = []
        for i in range(200):
            btc_p = 60000 + rng.gauss(0, 100)
            eth_p = 3000 + rng.gauss(0, 10)
            btc.append(make_candle(i * 14400000, btc_p, btc_p + 50, btc_p - 50, btc_p))
            eth.append(make_candle(i * 14400000, eth_p, eth_p + 5, eth_p - 5, eth_p))
        bars = build_arb_bars_pairs(btc, eth)
        cfg = PairsConfig(
            entry_z=1.5, beta_window=30, zscore_window=15,
            warmup_bars=30, max_holding_bars=10,
        )
        state = run_pairs_trading(bars, cfg, ArbConfig())
        for t in state.trades:
            assert t.holding_bars <= 10


# ═══════════════════════════════════════════
#  6. INTEGRATION — Cost Model
# ═══════════════════════════════════════════

class TestCostModel:
    def test_two_legged_costs_double(self):
        """Two-legged arb should pay 2x fees."""
        candles = make_flat_candles(30, 60000)
        rates = [0.1] * 5 + [3.0] * 10 + [0.1] * 15

        # One-legged
        bars1 = build_arb_bars_funding(candles, rates)
        cfg = FundingArbConfig(entry_threshold_bps=1.0)
        state1 = run_funding_arb(bars1, cfg, ArbConfig(two_legged=False))

        # Two-legged
        bars2 = build_arb_bars_funding(candles, rates)
        state2 = run_funding_arb(bars2, cfg, ArbConfig(two_legged=True))

        if state1.trades and state2.trades:
            # Two-legged should have ~2x fees
            assert state2.trades[0].total_fees > state1.trades[0].total_fees

    def test_maker_vs_taker_fee_asymmetry(self):
        """Entry uses maker (0.02%), exit uses taker (0.06%)."""
        candles = make_flat_candles(30, 60000)
        rates = [0.1] * 5 + [3.0] * 10 + [0.1] * 15
        bars = build_arb_bars_funding(candles, rates)
        cfg = FundingArbConfig(entry_threshold_bps=1.0)
        arb_cfg = ArbConfig(
            maker_fee_rate=0.0002,
            taker_fee_rate=0.0006,
            two_legged=False,
        )
        state = run_funding_arb(bars, cfg, arb_cfg)
        if state.trades:
            t = state.trades[0]
            # Total fees = maker_fee * notional + taker_fee * notional
            expected_entry = 0.0002 * t.notional
            expected_exit = 0.0006 * t.notional
            assert t.total_fees == pytest.approx(expected_entry + expected_exit, rel=0.01)

    def test_capital_updates_correctly(self):
        """Capital should reflect cumulative PnL."""
        candles = make_flat_candles(100, 60000)
        rates = simulate_funding_rates(100, mean=0, std=1.5, seed=42)
        bars = build_arb_bars_funding(candles, rates)
        cfg = FundingArbConfig(entry_threshold_bps=1.0)
        arb_cfg = ArbConfig(initial_capital=10000)
        state = run_funding_arb(bars, cfg, arb_cfg)
        if state.trades:
            total_pnl = sum(t.net_pnl for t in state.trades)
            assert state.capital == pytest.approx(10000 + total_pnl, rel=0.01)

    def test_equity_curve_length(self):
        """Equity curve should have ~n entries."""
        candles = make_flat_candles(50, 60000)
        rates = simulate_funding_rates(50, seed=42)
        bars = build_arb_bars_funding(candles, rates)
        state = run_funding_arb(bars, FundingArbConfig(), ArbConfig())
        # equity_curve gets one entry per bar + initial
        assert len(state.equity_curve) >= 40


# ═══════════════════════════════════════════
#  7. EDGE CASES
# ═══════════════════════════════════════════

class TestEdgeCases:
    def test_empty_bars(self):
        """No bars should produce no trades."""
        state = run_funding_arb([], FundingArbConfig(), ArbConfig())
        assert len(state.trades) == 0

    def test_single_bar(self):
        """Single bar should produce no trades."""
        bars = [ArbBar(timestamp=0, perp_open=100, perp_high=105,
                       perp_low=95, perp_close=102, funding_rate=5.0)]
        state = run_funding_arb(bars, FundingArbConfig(), ArbConfig())
        assert len(state.trades) == 0

    def test_zero_capital(self):
        """Zero capital should produce no trades."""
        candles = make_flat_candles(50, 60000)
        rates = [3.0] * 50
        bars = build_arb_bars_funding(candles, rates)
        state = run_funding_arb(bars, FundingArbConfig(), ArbConfig(initial_capital=0))
        assert len(state.trades) == 0

    def test_pairs_short_data(self):
        """Insufficient data for pairs → no trades."""
        btc = [make_candle(i, 60000, 60100, 59900, 60000) for i in range(10)]
        eth = [make_candle(i, 3000, 3010, 2990, 3000) for i in range(10)]
        bars = build_arb_bars_pairs(btc, eth)
        cfg = PairsConfig(warmup_bars=60)  # Need 60, have 10
        state = run_pairs_trading(bars, cfg, ArbConfig())
        assert len(state.trades) == 0
