"""
Phase 6 — Tests: PerformanceGovernor + Instrumentation
═══════════════════════════════════════════════════════
Covers:
  - Governor recording methods (rejection, fill, emergency close, reconciler, shadow)
  - Snapshot structure and correctness
  - Daily reset logic
  - Stats computation (slippage p50/p95, winrate, PF)
  - OrderManager → Governor integration (rejection pipeline)
  - Reconciler → Governor integration
  - API endpoint availability
"""

import time
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone


# ═══════════════════════════════════════════════════════════
#  1. Governor Core Unit Tests
# ═══════════════════════════════════════════════════════════

class TestPerformanceGovernorCore:
    """Core recording and snapshot logic."""

    def _fresh_governor(self):
        from smc_futures.observability.performance_governor import PerformanceGovernor
        return PerformanceGovernor()

    def test_empty_snapshot(self):
        """T1: Fresh governor returns valid empty snapshot."""
        gov = self._fresh_governor()
        snap = gov.snapshot()

        assert "rejection_dashboard" in snap
        assert "execution_quality" in snap
        assert "strategy_health" in snap
        assert "shadow_analytics" in snap
        assert snap["rejection_dashboard"]["total_signals"] == 0
        assert snap["execution_quality"]["total_fills"] == 0

    def test_record_rejection(self):
        """T2: Record rejection → appears in snapshot."""
        gov = self._fresh_governor()
        gov.record_signal_evaluated(strategy="DefaultSMC")
        gov.record_rejection(
            symbol="BTCUSDT",
            filter_name="SIGNAL_TOO_OLD",
            is_hard=True,
            strategy="DefaultSMC",
            session="london",
        )

        snap = gov.snapshot()
        rd = snap["rejection_dashboard"]
        assert rd["total_rejections"] == 1
        assert rd["hard_rejects"] == 1
        assert rd["soft_rejects"] == 0
        assert rd["by_filter"]["SIGNAL_TOO_OLD"] == 1
        assert rd["by_symbol"]["BTCUSDT"] == 1
        assert rd["by_session"]["london"] == 1

    def test_record_fill(self):
        """T3: Record fill → slippage stats computed."""
        gov = self._fresh_governor()
        for i in range(50):
            gov.record_fill(
                symbol="BTCUSDT",
                strategy="DefaultSMC",
                direction="long",
                entry=50000.0,
                fill_price=50000.0 + i * 0.5,
                slippage_bps=float(i),
                fill_delay_ms=100.0 + i * 10,
                regime="TREND",
            )

        snap = gov.snapshot()
        eq = snap["execution_quality"]
        assert eq["total_fills"] == 50
        assert eq["slippage"]["count"] == 50
        assert eq["slippage"]["mean"] > 0
        assert eq["slippage"]["p50"] >= 0
        assert eq["slippage"]["max"] == 49.0
        assert eq["fill_delay"]["count"] == 50

    def test_record_trade_closed_winrate(self):
        """T4: Record wins and losses → winrate + PF computed."""
        gov = self._fresh_governor()
        # 3 wins, 2 losses
        gov.record_trade_closed("BTCUSDT", "DefaultSMC", pnl=100.0, regime="TREND")
        gov.record_trade_closed("BTCUSDT", "DefaultSMC", pnl=200.0, regime="TREND")
        gov.record_trade_closed("BTCUSDT", "DefaultSMC", pnl=50.0, regime="TREND")
        gov.record_trade_closed("ETHUSDT", "DefaultSMC", pnl=-80.0, regime="RANGE")
        gov.record_trade_closed("ETHUSDT", "DefaultSMC", pnl=-70.0, regime="RANGE")

        snap = gov.snapshot()
        sh = snap["strategy_health"]
        assert sh["trades_closed"] == 5
        assert sh["winrate_pct"] == 60.0
        assert sh["pnl"] == 200.0
        assert sh["profit_factor"] == round(350.0 / 150.0, 2)

    def test_record_emergency_close(self):
        """T5: Emergency close recorded with success/failure."""
        gov = self._fresh_governor()
        gov.record_fill(
            symbol="BTCUSDT", strategy="", direction="long",
            entry=50000, fill_price=50000, slippage_bps=0,
        )
        gov.record_emergency_close("BTCUSDT", attempts=3, success=True)
        gov.record_emergency_close("ETHUSDT", attempts=3, success=False)

        snap = gov.snapshot()
        ec = snap["execution_quality"]["emergency_close"]
        assert ec["attempts"] == 2
        assert ec["successes"] == 1
        assert ec["failures"] == 1
        assert ec["orphans"] == 1

    def test_record_reconciler_cycle(self):
        """T6: Reconciler stats appear in snapshot."""
        gov = self._fresh_governor()
        gov.record_reconciler_cycle(orphans=2, ghosts=1, flattened=1)
        gov.record_reconciler_cycle(orphans=0, ghosts=0, flattened=0)

        snap = gov.snapshot()
        rc = snap["execution_quality"]["reconciler"]
        assert rc["checks"] == 2
        assert rc["orphans_detected"] == 2
        assert rc["ghosts_cleaned"] == 1
        assert rc["auto_flattened"] == 1

    def test_shadow_filter_cost(self):
        """T7: Shadow mode tracks filter opportunity cost."""
        gov = self._fresh_governor()
        gov.record_shadow_block("EXTREME_VOLATILITY", "won")
        gov.record_shadow_block("EXTREME_VOLATILITY", "lost")
        gov.record_shadow_block("EXTREME_VOLATILITY", "lost")
        gov.record_shadow_block("CORRELATION_BLOCKED", "won")

        snap = gov.snapshot()
        sa = snap["shadow_analytics"]
        assert sa["total_blocked"] == 4
        ev = sa["filters"]["EXTREME_VOLATILITY"]
        assert ev["signals_blocked"] == 3
        assert ev["blocked_would_have_won"] == 1
        assert ev["blocked_would_have_lost"] == 2

        # Ranked: EXTREME_VOLATILITY has 1 won, CORRELATION_BLOCKED has 1 won
        ranked = sa["filters_ranked_by_cost"]
        assert len(ranked) == 2

    def test_pct_by_filter(self):
        """T8: Percentage breakdown is correct."""
        gov = self._fresh_governor()
        for _ in range(3):
            gov.record_rejection("BTCUSDT", "SIGNAL_TOO_OLD", is_hard=True)
        for _ in range(7):
            gov.record_rejection("ETHUSDT", "COOLDOWN_ACTIVE", is_hard=False)

        snap = gov.snapshot()
        pct = snap["rejection_dashboard"]["pct_by_filter"]
        assert pct["SIGNAL_TOO_OLD"] == 30.0
        assert pct["COOLDOWN_ACTIVE"] == 70.0

    def test_strategy_regime_breakdown(self):
        """T9: Strategy x regime breakdown."""
        gov = self._fresh_governor()
        gov.record_fill(
            symbol="BTCUSDT", strategy="Asia", direction="short",
            entry=50000, fill_price=50000, slippage_bps=0, regime="RANGE",
        )
        gov.record_trade_closed("BTCUSDT", "Asia", pnl=50.0, regime="RANGE")

        gov.record_fill(
            symbol="BTCUSDT", strategy="DefaultSMC", direction="long",
            entry=50000, fill_price=50000, slippage_bps=0, regime="TREND",
        )
        gov.record_trade_closed("BTCUSDT", "DefaultSMC", pnl=-30.0, regime="TREND")

        snap = gov.snapshot()
        breakdown = snap["strategy_health"]["by_strategy_regime"]
        assert len(breakdown) == 2
        asia = [x for x in breakdown if x["strategy"] == "Asia"][0]
        assert asia["regime"] == "RANGE"
        assert asia["wins"] == 1
        assert asia["pnl"] == 50.0

    def test_hard_vs_soft_classification(self):
        """T10: Verify hard/soft counts."""
        gov = self._fresh_governor()
        gov.record_rejection("BTC", "KILL_SWITCH", is_hard=True)
        gov.record_rejection("BTC", "LOW_RR", is_hard=False)
        gov.record_rejection("BTC", "COOLDOWN_ACTIVE", is_hard=False)

        snap = gov.snapshot()
        rd = snap["rejection_dashboard"]
        assert rd["hard_rejects"] == 1
        assert rd["soft_rejects"] == 2


# ═══════════════════════════════════════════════════════════
#  2. OrderManager → Governor Integration
# ═══════════════════════════════════════════════════════════

class TestOrderManagerGovernorIntegration:
    """Verify OrderManager feeds the governor on rejections and fills."""

    @pytest.fixture
    def om(self):
        with patch("smc_futures.engine.execution.order_manager.risk_engine") as mock_risk, \
             patch("smc_futures.engine.execution.order_manager._evaluate_risk_v2", return_value=(True, 1.0, [])):
            mock_risk.validate_intent = MagicMock(return_value=(True, ""))
            mock_risk.get_multiplier = MagicMock(return_value=1.0)
            from smc_futures.engine.execution.order_manager import OrderManager
            cfg = {
                "AUTO_EXECUTE_ENABLED": True,
                "SHADOW_MODE": False,
                "TRADING_MODE": "live",
                "MAX_TRADES_PER_DAY": 20,
                "MAX_DAILY_LOSS_PCT": 3.0,
                "MAX_OPEN_TRADES_PER_SYMBOL": 1,
                "TRADE_COOLDOWN_SEC": 60,
                "MIN_RR": 1.5,
                "RISK_PER_TRADE_PCT": 0.01,
                "DEFAULT_LEVERAGE": 3,
                "SLIPPAGE_KILL_THRESHOLD_BPS": 60.0,
                "SLIPPAGE_KILL_COOLDOWN_SEC": 300,
                "MAX_SIGNAL_AGE_SEC": 5.0,
                "EXTREME_VOLATILITY_FILTER_ENABLED": True,
                "EXTREME_VOLATILITY_MAX_ATR_RATIO": 2.5,
                "CORRELATION_FILTER_ENABLED": False,
                "DISABLED_SYMBOLS": [],
                "EMERGENCY_CLOSE_MAX_RETRIES": 1,
                "EMERGENCY_CLOSE_BACKOFF_BASE": 0.01,
            }
            return OrderManager(cfg)

    def test_rejection_feeds_governor(self, om):
        """T11: SIGNAL_TOO_OLD rejection feeds the governor."""
        from smc_futures.observability.performance_governor import governor

        # Reset governor for clean test
        governor._daily_rejections_by_filter = {}
        from collections import defaultdict
        governor._daily_rejections_by_filter = defaultdict(int)
        governor._daily_total_signals = 0

        signal = {
            "direction": "long",
            "entry": 50000,
            "stop_loss": 49500,
            "take_profit": 51500,
            "t_signal": time.time() - 20.0,  # 20s old → stale
            "atr": 100.0,
        }

        ok, reason, _ = om.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict=signal,
            signal_id="GOV-TEST-1",
            account_balance=10000.0,
            quantity=0.01,
        )

        assert ok is False
        assert "SIGNAL_TOO_OLD" in reason
        assert governor._daily_rejections_by_filter.get("SIGNAL_TOO_OLD", 0) >= 1

    @pytest.mark.asyncio
    async def test_fill_feeds_governor(self, om):
        """T12: Successful fill feeds governor fill metrics."""
        from smc_futures.observability.performance_governor import governor

        initial_fills = governor._daily_fills

        executor = AsyncMock()
        executor.set_leverage = AsyncMock(return_value={"code": "00000", "leverage": 3})
        executor.place_order = AsyncMock(return_value={
            "success": True,
            "order_id": "ORD-GOV-1",
            "fill_price": 50005.0,  # 1 bps slippage — under threshold
        })

        signal = {
            "direction": "long",
            "entry": 50000.0,
            "stop_loss": 49500.0,
            "take_profit": 51500.0,
            "t_signal": time.time(),
            "atr": 100.0,
        }

        result = await om.execute_signal(
            executor=executor,
            symbol="BTCUSDT",
            signal_dict=signal,
            quantity=0.01,
            leverage=3,
            signal_id="GOV-FILL-1",
        )

        assert result["success"] is True
        assert governor._daily_fills > initial_fills


# ═══════════════════════════════════════════════════════════
#  3. Reconciler → Governor Integration
# ═══════════════════════════════════════════════════════════

class TestReconcilerGovernorIntegration:
    """Verify Reconciler feeds the governor on each cycle."""

    @pytest.mark.asyncio
    async def test_reconciler_feeds_governor(self):
        """T13: Reconciler cycle feeds governor metrics."""
        from smc_futures.observability.performance_governor import governor
        from smc_futures.engine.execution.position_reconciler import PositionReconciler

        initial_checks = governor._daily_reconciler_checks

        executor = AsyncMock()
        executor.get_all_positions = AsyncMock(return_value={"data": []})

        om = MagicMock()
        om._open_position_symbols = set()  # No ghosts — empty internal
        om._orphan_positions = set()

        reconciler = PositionReconciler(
            executor=executor,
            order_manager=om,
            config={"RECONCILER_ENABLED": True},
        )

        result = await reconciler.check_once()

        assert governor._daily_reconciler_checks > initial_checks

    @pytest.mark.asyncio
    async def test_ghost_confirmation_delay(self):
        """T13b: Ghost requires confirmation delay before cleanup."""
        from smc_futures.engine.execution.position_reconciler import PositionReconciler

        executor = AsyncMock()
        executor.get_all_positions = AsyncMock(return_value={"data": []})

        om = MagicMock()
        om._open_position_symbols = {"BTCUSDT"}  # Ghost candidate
        om._orphan_positions = set()

        reconciler = PositionReconciler(
            executor=executor,
            order_manager=om,
            config={"RECONCILER_ENABLED": True, "RECONCILER_GHOST_CONFIRM_SEC": 0},
        )

        # First check: ghost is a candidate, NOT yet cleaned
        result1 = await reconciler.check_once()
        assert "BTCUSDT" not in result1["ghosts"]
        assert "BTCUSDT" in om._open_position_symbols  # Still there

        # Second check: ghost confirmed (0s confirm delay)
        result2 = await reconciler.check_once()
        assert "BTCUSDT" in result2["ghosts"]


# ═══════════════════════════════════════════════════════════
#  4. API Endpoint Smoke Tests
# ═══════════════════════════════════════════════════════════

class TestPhase6APIEndpoints:
    """Verify API endpoints return valid responses."""

    def test_performance_governor_endpoint(self):
        """T14: /api/analytics/performance-governor returns valid snapshot."""
        from backend.routes.analytics import performance_governor_snapshot
        result = performance_governor_snapshot()
        assert "rejection_dashboard" in result
        assert "execution_quality" in result
        assert "strategy_health" in result
        assert "shadow_analytics" in result

    def test_rejection_dashboard_endpoint(self):
        """T15: /api/analytics/rejection-dashboard returns rejection data."""
        from backend.routes.analytics import rejection_dashboard
        result = rejection_dashboard()
        assert "total_signals" in result
        assert "by_filter" in result

    def test_execution_quality_endpoint(self):
        """T16: /api/analytics/execution-quality-live returns exec data."""
        from backend.routes.analytics import execution_quality_live
        result = execution_quality_live()
        assert "total_fills" in result
        assert "slippage" in result
        assert "emergency_close" in result
        assert "reconciler" in result

    def test_strategy_health_endpoint(self):
        """T17: /api/analytics/strategy-health returns strategy data."""
        from backend.routes.analytics import strategy_health
        result = strategy_health()
        assert "trades_opened" in result
        assert "by_strategy_regime" in result

    def test_shadow_analytics_endpoint(self):
        """T18: /api/analytics/shadow-analytics returns shadow data."""
        from backend.routes.analytics import shadow_analytics
        result = shadow_analytics()
        assert "filters" in result
        assert "total_blocked" in result
