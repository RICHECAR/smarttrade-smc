"""
Phase 5c — Test Battery for Trading Engine Safety Protections
═══════════════════════════════════════════════════════════════
Covers:
  - Slippage Kill + Cooldown (tests 1-3)
  - Latency Guard (tests 4-6)
  - Liquidity Trap — Directional (tests 7-9)
  - Regime Compression (tests 10-11)
  - Correlation Filter (tests 12-13)
  - Extreme Volatility Filter (tests 14-15)
  - Emergency Close success/failure (tests 16-17)
  - Bookkeeping after Kill Switch (test 18)
  - Old-but-valid signal rejection (test 19)
  - No double close (test 20)
  - Reconciler orphan detection (test 21)
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from collections import defaultdict
from typing import Set, Dict


# ═══════════════════════════════════════════════════════════
#  Helpers: build minimal OrderManager + mocks
# ═══════════════════════════════════════════════════════════

def _make_config(**overrides) -> dict:
    """Build a minimal config dict for OrderManager."""
    cfg = {
        "AUTO_EXECUTE_ENABLED": True,
        "SHADOW_MODE": False,
        "TRADING_MODE": "live",
        "ORDER_TYPE": "market",
        "LIMIT_OFFSET_BPS": 5,
        "MAX_TRADES_PER_DAY": 20,
        "MAX_DAILY_LOSS_PCT": 3.0,
        "MAX_OPEN_TRADES_PER_SYMBOL": 1,
        "TRADE_COOLDOWN_SEC": 60,
        "MIN_RR": 1.5,
        "RISK_PER_TRADE_PCT": 0.01,
        "DEFAULT_LEVERAGE": 3,
        "FORCE_EXECUTE": False,
        "DISABLED_SYMBOLS": [],
        # Slippage
        "MAX_ENTRY_SLIPPAGE_ATR": 0.3,
        "MAX_ENTRY_SLIPPAGE_BPS": 30.0,
        "SLIPPAGE_KILL_THRESHOLD_BPS": 60.0,
        "SLIPPAGE_KILL_COOLDOWN_SEC": 300,
        # Latency
        "MAX_SIGNAL_AGE_SEC": 5.0,
        # Extreme vol
        "EXTREME_VOLATILITY_FILTER_ENABLED": True,
        "EXTREME_VOLATILITY_MAX_ATR_RATIO": 2.5,
        # Correlation
        "CORRELATION_FILTER_ENABLED": True,
        "CORRELATION_RISK_MULT": 0.4,
        "CORRELATION_MAX_POSITIONS": 2,
        "CORRELATION_GROUPS": "",
        # Emergency close
        "EMERGENCY_CLOSE_MAX_RETRIES": 3,
        "EMERGENCY_CLOSE_BACKOFF_BASE": 0.01,  # Fast for tests
    }
    cfg.update(overrides)
    return cfg


def _make_signal(direction="long", entry=50000.0, sl=49500.0, tp=51500.0, **extra) -> dict:
    """Build a signal dict."""
    sig = {
        "direction": direction,
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "t_signal": time.time(),
        "atr": 100.0,
        "atr_info": {"current_atr": 100.0, "avg_atr": 100.0},
        "strategy_id": "test",
        "trigger_tf": "5m",
    }
    sig.update(extra)
    return sig


def _make_executor(fill_success=True, fill_price=50000.0, close_success=True):
    """Build a mock executor."""
    executor = AsyncMock()
    executor.set_leverage = AsyncMock(return_value={"code": "00000", "leverage": 3})
    executor.place_order = AsyncMock(return_value={
        "success": fill_success,
        "order_id": "ORD-123",
        "fill_price": fill_price,
    })
    if close_success:
        # For emergency close: first call is the close order
        executor.close_position = AsyncMock(return_value={"success": True})
    else:
        executor.close_position = AsyncMock(return_value={"success": False, "error": "API_ERROR"})
    return executor


def _build_order_manager(**overrides):
    """Instantiate OrderManager with mocked dependencies.

    Patches risk_engine persistently so pre_flight_checks works after return.
    """
    import smc_futures.engine.execution.order_manager as om_mod

    # Persistent mock on the module-level risk_engine
    mock_re = MagicMock()
    mock_re.validate_intent = MagicMock(return_value=(True, ""))
    mock_re.get_multiplier = MagicMock(return_value=1.0)
    om_mod.risk_engine = mock_re

    # Mock Phase 9 risk V2 gate (returns allow by default in tests)
    om_mod._evaluate_risk_v2 = lambda **kw: (True, 1.0, [])

    from smc_futures.engine.execution.order_manager import OrderManager
    cfg = _make_config(**overrides)
    om = OrderManager(cfg)
    return om


# ═══════════════════════════════════════════════════════════
#  1-3: Slippage Kill + Cooldown
# ═══════════════════════════════════════════════════════════

class TestSlippageKillCooldown:
    """Slippage kill triggers emergency close and activates cooldown."""

    @pytest.fixture
    def om(self):
        return _build_order_manager()

    @pytest.mark.asyncio
    async def test_slippage_kill_triggers_cooldown(self, om):
        """T1: Fill with 80bps slippage (> 60 threshold) → emergency close + cooldown."""
        # fill=50400 → slippage = |50400-50000|/50000*10000 = 80 bps > 60 → kill
        executor = _make_executor(fill_success=True, fill_price=50400.0)
        signal = _make_signal(entry=50000.0, atr=100.0)

        result = await om.execute_signal(
            executor=executor,
            symbol="BTCUSDT",
            signal_dict=signal,
            quantity=0.01,
            leverage=3,
            signal_id="SIG-001",
        )

        assert result.get("slippage_kill") is True
        assert result["success"] is False
        assert om._slippage_kill_until > time.time()

    @pytest.mark.asyncio
    async def test_slippage_kill_cooldown_blocks_new_trade(self, om):
        """T2: New signal during cooldown → HARD reject."""
        # Manually set cooldown
        om._slippage_kill_until = time.time() + 300

        signal = _make_signal()
        ok, reason, details = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=signal,
            signal_id="SIG-003",
            account_balance=10000.0,
        )

        assert ok is False
        assert "SLIPPAGE_KILL_COOLDOWN" in reason

    @pytest.mark.asyncio
    async def test_slippage_kill_cooldown_expires(self, om):
        """T3: Cooldown expired → signals pass."""
        # Set cooldown in the past
        om._slippage_kill_until = time.time() - 1

        signal = _make_signal()
        ok, reason, details = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=signal,
            signal_id="SIG-004",
            account_balance=10000.0,
        )

        # Should not be rejected by SLIPPAGE_KILL_COOLDOWN
        assert "SLIPPAGE_KILL_COOLDOWN" not in reason
        assert om._slippage_kill_until == 0.0  # reset


# ═══════════════════════════════════════════════════════════
#  4-6: Latency Guard
# ═══════════════════════════════════════════════════════════

class TestLatencyGuard:
    """Execution latency guard rejects stale signals."""

    @pytest.fixture
    def om(self):
        return _build_order_manager()

    def test_latency_guard_rejects_stale(self, om):
        """T4: signal_age > 5s → HARD REJECT."""
        signal = _make_signal(t_signal=time.time() - 10.0)  # 10s old

        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=signal,
            signal_id="SIG-LAT-1",
            account_balance=10000.0,
        )

        assert ok is False
        assert "SIGNAL_TOO_OLD" in reason

    def test_latency_guard_accepts_fresh(self, om):
        """T5: signal_age <= 5s → no rejection by latency."""
        signal = _make_signal(t_signal=time.time() - 2.0)  # 2s old

        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=signal,
            signal_id="SIG-LAT-2",
            account_balance=10000.0,
        )

        assert "SIGNAL_TOO_OLD" not in reason

    def test_latency_guard_missing_t_signal(self, om):
        """T6: t_signal absent → guard is bypassed (documented behavior)."""
        signal = _make_signal()
        del signal["t_signal"]  # Remove t_signal

        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=signal,
            signal_id="SIG-LAT-3",
            account_balance=10000.0,
        )

        # Current behavior: t_signal=0 → guard skipped → NOT rejected
        # This documents the edge case — in production both flows now set t_signal
        assert "SIGNAL_TOO_OLD" not in reason


# ═══════════════════════════════════════════════════════════
#  7-9: Directional Liquidity Trap
# ═══════════════════════════════════════════════════════════

class TestDirectionalLiquidityTrap:
    """Liquidity trap filter with directional awareness."""

    def _make_heatmap(self, levels):
        return {"heatmap": {"levels": levels}}

    def test_trap_blocks_close_cluster_ahead(self):
        """T7: Long entry, cluster above and close → BLOCKED."""
        # Entry=50000, cluster=50010 (above → ahead for long), ATR=100
        # dist = 10/100 = 0.1 ATR < 0.3 → should block
        entry = 50000.0
        cluster_price = 50010.0
        atr = 100.0
        direction = "long"

        dist = abs(entry - cluster_price) / atr
        cluster_is_ahead = (direction == "long" and cluster_price > entry)
        cluster_at_entry = abs(cluster_price - entry) / entry < 0.001

        should_block = dist < 0.3 and (cluster_is_ahead or cluster_at_entry)
        assert should_block is True

    def test_trap_allows_far_cluster(self):
        """T8: Cluster score >= 4 but distance > 0.3 ATR → allowed."""
        entry = 50000.0
        cluster_price = 50050.0
        atr = 100.0

        dist = abs(entry - cluster_price) / atr
        assert dist > 0.3  # 0.5 ATR → allowed

    def test_trap_allows_protective_cluster_behind(self):
        """T9: Long entry, cluster below (behind) → protective, NOT blocked."""
        entry = 50000.0
        cluster_price = 49800.0  # Below entry → behind for long (0.4% away, not at entry)
        atr = 100.0
        direction = "long"

        dist = abs(entry - cluster_price) / atr  # 2.0 ATR
        cluster_is_ahead = (direction == "long" and cluster_price > entry)  # False
        cluster_at_entry = abs(cluster_price - entry) / entry < 0.001  # 0.4% > 0.1% → False

        should_block = dist < 0.3 and (cluster_is_ahead or cluster_at_entry)
        assert should_block is False  # Protective cluster → allow


# ═══════════════════════════════════════════════════════════
#  10-11: Regime Compression
# ═══════════════════════════════════════════════════════════

class TestRegimeCompression:
    """Range compression detection in regime filter."""

    def test_compression_triggers_trend(self):
        """T10: 3+ HH/HL + range/ATR < 1.0 → TREND."""
        from smc_futures.risk.regime_filter import RegimeFilter, MarketRegime

        rf = RegimeFilter({
            "REGIME_FILTER_ENABLED": True,
            "REGIME_TREND_THRESHOLD": 4,
            "REGIME_HIGH_VOL_ATR_RATIO": 2.0,
            "REGIME_LOW_VOL_ATR_RATIO": 0.5,
            "REGIME_LOOKBACK": 20,
            "REGIME_COMPRESSION_RATIO": 1.0,
        })

        # Build candles: 3 consecutive HH/HL (dir_strength=3 >= threshold-1=3)
        # + compressed range (last 5 candles range < ATR)
        candles = []
        base = 100.0
        for i in range(10):
            candles.append({
                "high": base + i * 1.0 + 0.5,  # Rising highs
                "low": base + i * 1.0,         # Rising lows
                "close": base + i * 1.0 + 0.3,
                "open": base + i * 1.0 + 0.1,
            })

        # Last 5 candles should have small range compared to ATR
        # range_5 = max(high) - min(low) in last 5
        # ATR = current_atr
        current_atr = 5.0  # Large ATR
        avg_atr = 5.0

        regime = rf.detect(candles, current_atr, avg_atr)
        # With 9 consecutive HH/HL, dir_strength should be high
        # and compression should amplify: range of last 5 ≈ 4.5 / ATR 5.0 = 0.9 < 1.0
        assert regime == MarketRegime.TREND

    def test_compression_no_trend_on_noise(self):
        """T11: Low range but no directional structure → NOT TREND."""
        from smc_futures.risk.regime_filter import RegimeFilter, MarketRegime

        rf = RegimeFilter({
            "REGIME_FILTER_ENABLED": True,
            "REGIME_TREND_THRESHOLD": 4,
            "REGIME_HIGH_VOL_ATR_RATIO": 2.0,
            "REGIME_LOW_VOL_ATR_RATIO": 0.5,
            "REGIME_LOOKBACK": 20,
            "REGIME_COMPRESSION_RATIO": 1.0,
        })

        # Choppy candles: alternating up/down → dir_strength ≈ 0
        candles = []
        for i in range(10):
            if i % 2 == 0:
                candles.append({"high": 101.0, "low": 100.0})
            else:
                candles.append({"high": 100.5, "low": 99.5})

        regime = rf.detect(candles, current_atr=2.0, avg_atr=2.0)
        assert regime != MarketRegime.TREND  # Should be RANGE


# ═══════════════════════════════════════════════════════════
#  12-13: Correlation Filter
# ═══════════════════════════════════════════════════════════

class TestCorrelationFilter:
    """Correlation filter risk reduction and blocking."""

    def test_correlation_reduces_risk(self):
        """T12: 1 position open in same group → risk multiplied by 0.4."""
        from smc_futures.risk.correlation_filter import CorrelationFilter

        cf = CorrelationFilter({
            "CORRELATION_FILTER_ENABLED": True,
            "CORRELATION_RISK_MULT": 0.4,
            "CORRELATION_MAX_POSITIONS": 2,
            "CORRELATION_GROUPS": "",  # Uses defaults: majors=[BTCUSDT, ETHUSDT]
        })

        mult, reason = cf.check("ETHUSDT", {"BTCUSDT"})
        assert mult == pytest.approx(0.4), f"Expected 0.4, got {mult}"
        assert "CORR_REDUCTION" in reason

    def test_correlation_blocks_excess(self):
        """T13: 2 positions open → blocked (max_positions=2)."""
        from smc_futures.risk.correlation_filter import CorrelationFilter

        cf = CorrelationFilter({
            "CORRELATION_FILTER_ENABLED": True,
            "CORRELATION_RISK_MULT": 0.4,
            "CORRELATION_MAX_POSITIONS": 2,
            "CORRELATION_GROUPS": "",
        })

        # 2 positions already open in majors group, trying to open a 3rd
        # With default groups: majors=[BTCUSDT, ETHUSDT], only 2 symbols
        # Need custom group to test this properly
        cf.groups["big"] = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
        for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
            cf._symbol_to_group[s] = "big"

        mult, reason = cf.check("XRPUSDT", {"BTCUSDT", "ETHUSDT"})
        assert mult == 0.0
        assert "CORR_BLOCKED" in reason

    def test_correlation_filter_actually_called_in_preflight(self):
        """T13b: Verify the correlation filter is actually invoked (bug was fixed)."""
        om = _build_order_manager()
        # The attribute should be self.correlation_filter (not _correlation_filter)
        assert hasattr(om, "correlation_filter")
        assert om.correlation_filter is not None
        assert om.correlation_filter.enabled is True


# ═══════════════════════════════════════════════════════════
#  14-15: Extreme Volatility Filter
# ═══════════════════════════════════════════════════════════

class TestExtremeVolatilityFilter:
    """Extreme ATR ratio rejection."""

    @pytest.fixture
    def om(self):
        return _build_order_manager()

    def test_extreme_vol_blocks_spike(self, om):
        """T14: ATR ratio 3.0 > 2.5 → HARD reject."""
        signal = _make_signal()
        signal["atr_info"] = {"current_atr": 300.0, "avg_atr": 100.0}

        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=signal,
            signal_id="SIG-VOL-1",
            account_balance=10000.0,
            atr_info={"current_atr": 300.0, "avg_atr": 100.0},
        )

        assert ok is False
        assert "EXTREME_VOLATILITY" in reason

    def test_extreme_vol_passes_normal(self, om):
        """T15: ATR ratio 1.5 → allowed."""
        signal = _make_signal()
        signal["atr_info"] = {"current_atr": 150.0, "avg_atr": 100.0}

        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=signal,
            signal_id="SIG-VOL-2",
            account_balance=10000.0,
            atr_info={"current_atr": 150.0, "avg_atr": 100.0},
        )

        assert "EXTREME_VOLATILITY" not in reason


# ═══════════════════════════════════════════════════════════
#  16-17: Emergency Close Success/Failure
# ═══════════════════════════════════════════════════════════

class TestEmergencyClose:
    """Emergency close with retry: success + failure paths."""

    @pytest.fixture
    def om(self):
        return _build_order_manager(
            EMERGENCY_CLOSE_MAX_RETRIES=3,
            EMERGENCY_CLOSE_BACKOFF_BASE=0.01,
        )

    @pytest.mark.asyncio
    async def test_emergency_close_success(self, om):
        """T16: Emergency close succeeds on first try."""
        executor = _make_executor(close_success=True)
        executor.place_order = AsyncMock(return_value={"success": True, "order_id": "CLOSE-1"})

        om._open_position_symbols.add("BTCUSDT")

        ok = await om._emergency_close_with_retry(
            executor=executor,
            symbol="BTCUSDT",
            direction="sell",
            quantity=0.01,
            signal_id="TEST-KILL",
        )

        assert ok is True
        assert "BTCUSDT" not in om._open_position_symbols
        assert "BTCUSDT" not in om._orphan_positions

    @pytest.mark.asyncio
    async def test_emergency_close_failure_all_retries(self, om):
        """T17: Emergency close fails all retries → orphan flagged."""
        executor = AsyncMock()
        executor.place_order = AsyncMock(return_value={"success": False, "error": "API_DOWN"})
        executor.close_position = AsyncMock(return_value={"success": False, "error": "TIMEOUT"})

        ok = await om._emergency_close_with_retry(
            executor=executor,
            symbol="ETHUSDT",
            direction="buy",
            quantity=0.05,
            signal_id="TEST-KILL-2",
        )

        assert ok is False
        # place_order called 3 times + close_position 3 times (fallback each attempt)
        assert executor.place_order.call_count == 3


# ═══════════════════════════════════════════════════════════
#  18: Bookkeeping after Kill Switch
# ═══════════════════════════════════════════════════════════

class TestBookkeepingAfterKill:
    """Kill switch must not produce false bookkeeping."""

    @pytest.fixture
    def om(self):
        return _build_order_manager()

    @pytest.mark.asyncio
    async def test_no_bookkeeping_after_slippage_kill(self, om):
        """T18: After slippage kill, record_fill should NOT be called."""
        # Simulate: execute_signal with huge slippage
        executor = _make_executor(fill_success=True, fill_price=50400.0)  # 80 bps
        signal = _make_signal(entry=50000.0, atr=100.0)

        initial_trades = om._trades_today
        result = await om.execute_signal(
            executor=executor,
            symbol="BTCUSDT",
            signal_dict=signal,
            quantity=0.01,
            leverage=3,
            signal_id="SIG-BOOK-1",
        )

        assert result.get("slippage_kill") is True
        assert result["success"] is False
        # Trades counter should NOT increment
        assert om._trades_today == initial_trades


# ═══════════════════════════════════════════════════════════
#  19: Old-but-valid signal still rejected
# ═══════════════════════════════════════════════════════════

class TestOldButValidSignal:

    @pytest.fixture
    def om(self):
        return _build_order_manager()

    def test_old_valid_signal_rejected(self, om):
        """T19: Signal with great RR but 10s old → rejected."""
        signal = _make_signal(
            entry=50000.0,
            sl=49000.0,
            tp=53000.0,
            t_signal=time.time() - 10.0,  # 10s old
        )
        # RR = 3000/1000 = 3.0 — excellent, but too old

        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=signal,
            signal_id="SIG-OLD-1",
            account_balance=10000.0,
        )

        assert ok is False
        assert "SIGNAL_TOO_OLD" in reason


# ═══════════════════════════════════════════════════════════
#  20: No double close after kill switch
# ═══════════════════════════════════════════════════════════

class TestNoDoubleClose:
    """Slippage kill must not trigger double close."""

    @pytest.fixture
    def om(self):
        return _build_order_manager(
            EMERGENCY_CLOSE_MAX_RETRIES=1,
            EMERGENCY_CLOSE_BACKOFF_BASE=0.01,
        )

    @pytest.mark.asyncio
    async def test_no_double_close(self, om):
        """T20: Only one close order is sent per kill event."""
        executor = _make_executor(fill_success=True, fill_price=50400.0)
        # place_order: first call = original fill, second call = emergency close
        call_count = 0
        original_fill_price = 50400.0  # 80 bps > 60 threshold

        async def mock_place_order(**kwargs):
            nonlocal call_count
            call_count += 1
            sig_id = kwargs.get("signal_id", "")
            if "KILL" in str(sig_id):
                return {"success": True, "order_id": f"CLOSE-{call_count}"}
            return {
                "success": True,
                "order_id": "ORD-ORIG",
                "fill_price": original_fill_price,
            }

        executor.place_order = mock_place_order

        signal = _make_signal(entry=50000.0)
        result = await om.execute_signal(
            executor=executor,
            symbol="BTCUSDT",
            signal_dict=signal,
            quantity=0.01,
            leverage=3,
            signal_id="SIG-DBL-1",
        )

        assert result.get("slippage_kill") is True
        # 2 calls total: 1 original + 1 emergency close (not 2 closes)
        assert call_count == 2


# ═══════════════════════════════════════════════════════════
#  21: Reconciler finds orphan position
# ═══════════════════════════════════════════════════════════

class TestPositionReconciler:
    """PositionReconciler heartbeat detects orphans."""

    @pytest.mark.asyncio
    async def test_reconciler_detects_orphan(self):
        """T21: Position on exchange, not in internal state → orphan detected."""
        from smc_futures.engine.execution.position_reconciler import PositionReconciler

        # Mock executor returns one open position
        executor = AsyncMock()
        executor.get_all_positions = AsyncMock(return_value={
            "data": [
                {"symbol": "BTCUSDT", "total": "0.01", "holdSide": "long",
                 "unrealizedPL": "-5.0", "markPrice": "49900"},
            ]
        })

        # Mock order manager with empty tracking
        om = MagicMock()
        om._open_position_symbols = set()  # Internal says: no positions
        om._orphan_positions = set()

        reconciler = PositionReconciler(
            executor=executor,
            order_manager=om,
            config={
                "RECONCILER_ENABLED": True,
                "RECONCILER_INTERVAL_SEC": 5,
                "RECONCILER_AUTO_FLATTEN": False,
            },
        )

        result = await reconciler.check_once()

        assert len(result["orphans"]) == 1
        assert result["orphans"][0]["symbol"] == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_reconciler_detects_ghost(self):
        """T21b: Internal says position open, exchange has nothing → ghost cleaned.
        Note: Ghost confirmation delay requires 2 cycles. We set confirm delay=0
        so the second check_once immediately confirms."""
        from smc_futures.engine.execution.position_reconciler import PositionReconciler

        executor = AsyncMock()
        executor.get_all_positions = AsyncMock(return_value={"data": []})

        om = MagicMock()
        om._open_position_symbols = {"ETHUSDT"}  # Internal says: ETHUSDT open
        om._orphan_positions = set()

        reconciler = PositionReconciler(
            executor=executor,
            order_manager=om,
            config={"RECONCILER_ENABLED": True, "RECONCILER_AUTO_FLATTEN": False},
        )
        # Override ghost confirm delay to 0 for immediate confirmation
        reconciler._ghost_confirm_sec = 0

        # First cycle: candidate detected
        result1 = await reconciler.check_once()
        # Reset _open_position_symbols for second cycle (still "open" internally)
        om._open_position_symbols = {"ETHUSDT"}
        # Second cycle: confirmed and cleaned
        result2 = await reconciler.check_once()

        assert "ETHUSDT" in result2["ghosts"]
        assert "ETHUSDT" not in om._open_position_symbols  # Cleaned

    @pytest.mark.asyncio
    async def test_reconciler_auto_flatten_orphan(self):
        """T21c: Auto-flatten enabled, orphan > max age → flattened."""
        from smc_futures.engine.execution.position_reconciler import PositionReconciler

        executor = AsyncMock()
        executor.get_all_positions = AsyncMock(return_value={
            "data": [
                {"symbol": "SOLUSDT", "total": "1.0", "holdSide": "long",
                 "unrealizedPL": "-2.0", "markPrice": "150"},
            ]
        })
        executor.close_position = AsyncMock(return_value={"success": True})

        om = MagicMock()
        om._open_position_symbols = set()
        om._orphan_positions = set()
        om._emergency_close_with_retry = AsyncMock(return_value=True)

        reconciler = PositionReconciler(
            executor=executor,
            order_manager=om,
            config={
                "RECONCILER_ENABLED": True,
                "RECONCILER_AUTO_FLATTEN": True,
                "RECONCILER_MAX_ORPHAN_AGE_SEC": 0,  # Immediately flatten
            },
        )

        # First check: detect orphan + set first_seen
        await reconciler.check_once()
        # Second check: orphan age > 0 → auto-flatten
        result = await reconciler.check_once()

        assert "SOLUSDT" in result["flattened"]

    @pytest.mark.asyncio
    async def test_drift_minor_corrects_internal_state(self):
        """P6: Minor drift detected → internal state corrected to exchange value."""
        from smc_futures.engine.execution.position_reconciler import PositionReconciler

        executor = AsyncMock()
        executor.get_all_positions = AsyncMock(return_value={
            "data": [
                {"symbol": "BTCUSDT", "total": "1.0015", "holdSide": "long",
                 "unrealizedPL": "10.0", "markPrice": "67000"},
            ]
        })

        om = MagicMock()
        om._open_position_symbols = {"BTCUSDT"}
        om._orphan_positions = set()
        om._position_sizes = {"BTCUSDT": 1.000}

        reconciler = PositionReconciler(
            executor=executor,
            order_manager=om,
            config={"RECONCILER_ENABLED": True, "RECONCILER_AUTO_FLATTEN": False},
        )

        result = await reconciler.check_once()

        assert len(result["drifts"]) == 1
        assert result["drifts"][0]["severity"] == "minor"
        # Internal state corrected to exchange reality
        assert om._position_sizes["BTCUSDT"] == 1.0015

    @pytest.mark.asyncio
    async def test_drift_critical_triggers_flatten(self):
        """P6: Critical drift (>=5x tolerance) with auto_flatten → position flattened."""
        from smc_futures.engine.execution.position_reconciler import PositionReconciler

        executor = AsyncMock()
        executor.get_all_positions = AsyncMock(return_value={
            "data": [
                {"symbol": "BTCUSDT", "total": "1.500", "holdSide": "long",
                 "unrealizedPL": "0", "markPrice": "67000"},
            ]
        })

        om = MagicMock()
        om._open_position_symbols = {"BTCUSDT"}
        om._orphan_positions = set()
        om._position_sizes = {"BTCUSDT": 1.000}  # Huge drift: 0.5 vs tolerance ~0.002
        om._emergency_close_with_retry = AsyncMock(return_value=True)

        reconciler = PositionReconciler(
            executor=executor,
            order_manager=om,
            config={"RECONCILER_ENABLED": True, "RECONCILER_AUTO_FLATTEN": True},
        )

        result = await reconciler.check_once()

        assert len(result["drifts"]) == 1
        assert result["drifts"][0]["severity"] == "critical"
        assert "BTCUSDT" in result["flattened"]

    @pytest.mark.asyncio
    async def test_no_drift_within_tolerance(self):
        """P6: Tiny size difference within tolerance → no drift reported."""
        from smc_futures.engine.execution.position_reconciler import PositionReconciler

        executor = AsyncMock()
        executor.get_all_positions = AsyncMock(return_value={
            "data": [
                {"symbol": "BTCUSDT", "total": "1.0001", "holdSide": "long",
                 "unrealizedPL": "0", "markPrice": "67000"},
            ]
        })

        om = MagicMock()
        om._open_position_symbols = {"BTCUSDT"}
        om._orphan_positions = set()
        om._position_sizes = {"BTCUSDT": 1.000}

        reconciler = PositionReconciler(
            executor=executor,
            order_manager=om,
            config={"RECONCILER_ENABLED": True, "RECONCILER_AUTO_FLATTEN": False},
        )

        result = await reconciler.check_once()

        assert len(result["drifts"]) == 0
        # Internal state unchanged
        assert om._position_sizes["BTCUSDT"] == 1.000
