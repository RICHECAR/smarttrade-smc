"""
Phase 5 — Institutional-Grade Hardening Tests
══════════════════════════════════════════════

Tests for all Phase 5 critical fixes:
  1. Kill switch fail-closed on corruption
  2. Broker response default-to-success → FAIL
  3. UNIQUE constraint on order_id+broker
  4. Rate limiter token bucket
  5. WS reconnect bounded + exponential backoff
  6. Balance staleness guard (10s cache)
  7. Safe mode thread-safety (locking)
  8. Watchdog equity/position timeout
  9. Signal dedup index
  10. Migration v14 dedup
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ═══════════════════════════════════════════════════════════════
# 1. KILL SWITCH FAIL-CLOSED ON CORRUPTION
# ═══════════════════════════════════════════════════════════════

class TestKillSwitchFailClosed:
    """Kill switch MUST activate on corrupted/unreadable file."""

    def _make_engine(self, file_content: str, file_path: Path):
        """Create a RiskEngine with a custom kill_switch file."""
        file_path.write_text(file_content, encoding="utf-8")
        with patch.object(
            sys.modules.get("backend.services.trading.risk_engine", __import__("backend.services.trading.risk_engine")),
            "_envf", side_effect=lambda k, d: d
        ):
            pass
        from backend.services.trading.risk_engine import RiskEngine
        engine = RiskEngine.__new__(RiskEngine)
        engine._dd_reduce_pct = 3.0
        engine._dd_block_pct = 5.0
        engine._max_consec_losses = 4
        engine._daily_max_loss_pct = 3.0
        engine._kill_pct = 3.0
        engine._kill_window_sec = 300.0
        engine._cooldown_after_loss_sec = 120.0
        engine._peak_equity = 0.0
        engine._daily_peak_equity = 0.0
        engine._daily_start_equity = 0.0
        engine._daily_start_date = ""
        engine._consecutive_losses = 0
        engine._consecutive_wins = 0
        engine._last_loss_time = 0.0
        engine._kill_switch = False
        engine._kill_reason = ""
        engine._lock = asyncio.Lock()
        from collections import deque
        engine._equity_snapshots = deque(maxlen=1000)
        engine._KILL_SWITCH_FILE = file_path
        engine._restore_kill_switch()
        return engine

    def test_corrupted_json_activates_kill_switch(self, tmp_path):
        """Corrupted JSON in kill_switch.json MUST activate kill switch."""
        ks_file = tmp_path / "kill_switch.json"
        engine = self._make_engine("{corrupted json!!!", ks_file)
        assert engine._kill_switch is True
        assert "FAIL-CLOSED" in engine._kill_reason

    def test_invalid_type_activates_kill_switch(self, tmp_path):
        """Non-dict JSON MUST activate kill switch."""
        ks_file = tmp_path / "kill_switch.json"
        engine = self._make_engine("[1, 2, 3]", ks_file)
        assert engine._kill_switch is True
        assert "FAIL-CLOSED" in engine._kill_reason

    def test_valid_active_restores(self, tmp_path):
        """Valid active kill switch file restores correctly."""
        ks_file = tmp_path / "kill_switch.json"
        content = json.dumps({"active": True, "reason": "test crash"})
        engine = self._make_engine(content, ks_file)
        assert engine._kill_switch is True
        assert "test crash" in engine._kill_reason

    def test_valid_inactive_stays_off(self, tmp_path):
        """Valid inactive kill switch file stays inactive."""
        ks_file = tmp_path / "kill_switch.json"
        content = json.dumps({"active": False, "reason": ""})
        engine = self._make_engine(content, ks_file)
        assert engine._kill_switch is False

    def test_missing_file_stays_off(self, tmp_path):
        """Missing kill_switch.json = first run, no kill switch."""
        from backend.services.trading.risk_engine import RiskEngine
        engine = RiskEngine.__new__(RiskEngine)
        engine._kill_switch = False
        engine._kill_reason = ""
        engine._KILL_SWITCH_FILE = tmp_path / "nonexistent.json"
        engine._restore_kill_switch()
        assert engine._kill_switch is False

    def test_empty_file_activates_kill_switch(self, tmp_path):
        """Empty file is corrupted JSON → fail-closed."""
        ks_file = tmp_path / "kill_switch.json"
        engine = self._make_engine("", ks_file)
        assert engine._kill_switch is True


# ═══════════════════════════════════════════════════════════════
# 2. BROKER RESPONSE DEFAULT-TO-FAIL
# ═══════════════════════════════════════════════════════════════

class TestBrokerResponseDefaultToFail:
    """Unknown response format MUST NOT default to success."""

    def test_unknown_format_returns_failed(self):
        from backend.execution.broker_router import BrokerRouter, BrokerStatus
        router = BrokerRouter()
        
        # Unknown format: no "code", "success", or "result" key
        response = {"weird_key": "unexpected_value", "data": 42}
        result = router._parse_executor_response("test_broker", response)
        
        assert result.success is False
        assert result.status == BrokerStatus.FAILED
        assert "Unknown response format" in (result.error or "")

    def test_null_response_returns_failed(self):
        from backend.execution.broker_router import BrokerRouter, BrokerStatus
        router = BrokerRouter()
        
        result = router._parse_executor_response("test_broker", None)
        assert result.success is False
        assert result.status == BrokerStatus.FAILED

    def test_kraken_success_detected(self):
        from backend.execution.broker_router import BrokerRouter, BrokerStatus
        router = BrokerRouter()
        
        response = {"result": "success", "orderId": "abc123"}
        result = router._parse_executor_response("kraken", response)
        assert result.success is True
        assert result.order_id == "abc123"

    def test_kraken_failure_detected(self):
        from backend.execution.broker_router import BrokerRouter, BrokerStatus
        router = BrokerRouter()
        
        response = {"result": "error", "error": "insufficient funds"}
        result = router._parse_executor_response("kraken", response)
        assert result.success is False


# ═══════════════════════════════════════════════════════════════
# 3. RATE LIMITER TOKEN BUCKET
# ═══════════════════════════════════════════════════════════════

class TestRateLimiter:
    """Token bucket rate limiter correctness."""

    @pytest.mark.asyncio
    async def test_acquire_within_budget(self):
        from core.rate_limiter import TokenBucketRateLimiter, RateLimiterConfig
        limiter = TokenBucketRateLimiter(RateLimiterConfig(max_tokens=10, refill_rate=10))
        
        # Should succeed immediately for 10 tokens
        for _ in range(10):
            assert await limiter.acquire() is True

    @pytest.mark.asyncio
    async def test_acquire_blocks_when_empty(self):
        from core.rate_limiter import TokenBucketRateLimiter, RateLimiterConfig
        limiter = TokenBucketRateLimiter(RateLimiterConfig(max_tokens=1, refill_rate=100))
        
        # Drain the bucket
        assert await limiter.acquire() is True
        
        # Next one should still succeed quickly (100/s refill)
        start = time.monotonic()
        assert await limiter.acquire(timeout=1.0) is True
        elapsed = time.monotonic() - start
        assert elapsed < 0.5  # Should refill fast

    @pytest.mark.asyncio
    async def test_timeout_on_empty_slow_refill(self):
        from core.rate_limiter import TokenBucketRateLimiter, RateLimiterConfig
        limiter = TokenBucketRateLimiter(
            RateLimiterConfig(max_tokens=1, refill_rate=0.5, initial_tokens=0)
        )
        
        # With 0 tokens and 0.5/s refill, 0.1s timeout should fail
        result = await limiter.acquire(timeout=0.1)
        assert result is False

    @pytest.mark.asyncio
    async def test_stats_tracking(self):
        from core.rate_limiter import TokenBucketRateLimiter, RateLimiterConfig
        limiter = TokenBucketRateLimiter(RateLimiterConfig(max_tokens=5, refill_rate=5))
        
        await limiter.acquire()
        await limiter.acquire()
        stats = limiter.get_stats()
        assert stats["total_acquired"] == 2

    def test_get_rate_limiter_singleton(self):
        from core.rate_limiter import get_rate_limiter, reset_all_limiters
        reset_all_limiters()
        
        l1 = get_rate_limiter("kraken_private")
        l2 = get_rate_limiter("kraken_private")
        assert l1 is l2
        
        l3 = get_rate_limiter("kraken_public")
        assert l3 is not l1
        
        reset_all_limiters()


# ═══════════════════════════════════════════════════════════════
# 4. WS RECONNECT BOUNDED
# ═══════════════════════════════════════════════════════════════

class TestWSReconnectBounded:
    """WebSocket reconnect MUST have a bounded retry limit."""

    def test_reconnect_has_max_attempts(self):
        """Verify _reconnect method references MAX_RECONNECT_ATTEMPTS."""
        import inspect
        from backend.kraken_futures_client import KrakenFuturesClient
        source = inspect.getsource(KrakenFuturesClient._reconnect)
        assert "MAX_RECONNECT_ATTEMPTS" in source
        assert "10" in source or "max_reconnect" in source.lower()

    def test_reconnect_logs_critical_on_exhaustion(self):
        """Verify reconnect logs CRITICAL when exhausted."""
        import inspect
        from backend.kraken_futures_client import KrakenFuturesClient
        source = inspect.getsource(KrakenFuturesClient._reconnect)
        assert "RECONNECT EXHAUSTED" in source
        assert "critical" in source.lower()


# ═══════════════════════════════════════════════════════════════
# 5. SAFE MODE THREAD SAFETY
# ═══════════════════════════════════════════════════════════════

class TestSafeModeThreadSafety:
    """Safe mode controller must be thread-safe."""

    def test_has_lock(self):
        from core.safe_mode import SafeModeController
        ctrl = SafeModeController(state_file=Path(tempfile.mktemp(suffix=".json")))
        assert hasattr(ctrl, '_lock')
        assert isinstance(ctrl._lock, threading.Lock)

    def test_concurrent_activate_deactivate(self):
        """Concurrent activate/deactivate should not corrupt state."""
        state_file = Path(tempfile.mktemp(suffix=".json"))
        from core.safe_mode import SafeModeController
        ctrl = SafeModeController(state_file=state_file)
        errors = []

        def activate_loop():
            try:
                for i in range(50):
                    ctrl.activate(f"TRIGGER_{i}", f"reason_{i}")
                    ctrl.deactivate(operator="test")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=activate_loop) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Thread errors: {errors}"
        # Clean up
        if state_file.exists():
            state_file.unlink()


# ═══════════════════════════════════════════════════════════════
# 6. WATCHDOG EQUITY / POSITION TIMEOUT
# ═══════════════════════════════════════════════════════════════

class TestWatchdogTimeout:
    """Watchdog must timeout on slow equity/position providers."""

    @pytest.mark.asyncio
    async def test_equity_provider_timeout(self):
        from core.global_risk_watchdog import GlobalRiskWatchdog
        
        async def slow_equity():
            await asyncio.sleep(30)  # Intentionally too slow
            return 1000.0
        
        wd = GlobalRiskWatchdog()
        wd.register_equity_provider(slow_equity)
        wd.set_initial_equity(1000.0)
        
        # Should not hang — timeout after 5s
        result = await asyncio.wait_for(wd.check_once(), timeout=10.0)
        
        # Should have recorded an error or action for timeout
        actions = result.get("actions", [])
        has_timeout = any("timeout" in a.lower() for a in actions)
        assert has_timeout, f"Expected timeout action, got: {actions}"

    @pytest.mark.asyncio
    async def test_position_provider_timeout(self):
        from core.global_risk_watchdog import GlobalRiskWatchdog
        
        async def slow_positions():
            await asyncio.sleep(30)
            return []
        
        wd = GlobalRiskWatchdog()
        wd.register_position_provider(slow_positions)
        
        result = await asyncio.wait_for(wd.check_once(), timeout=10.0)
        actions = result.get("actions", [])
        has_timeout = any("timeout" in a.lower() for a in actions)
        assert has_timeout, f"Expected timeout action, got: {actions}"


# ═══════════════════════════════════════════════════════════════
# 7. UNIQUE CONSTRAINT MODEL DEFINITION
# ═══════════════════════════════════════════════════════════════

class TestUniqueConstraintModel:
    """LiveTrade model must have UNIQUE constraint on order_id+broker."""

    def test_unique_constraint_defined(self):
        from backend.models.live_trade import LiveTrade
        constraints = LiveTrade.__table_args__
        unique_names = [
            c.name for c in constraints 
            if hasattr(c, 'name') and c.name and 'uq_' in c.name
        ]
        assert "uq_live_trades_order_broker" in unique_names, (
            f"Missing UNIQUE constraint on order_id+broker. Found: {unique_names}"
        )

    def test_signal_broker_index_defined(self):
        from backend.models.live_trade import LiveTrade
        constraints = LiveTrade.__table_args__
        index_names = [
            c.name for c in constraints 
            if hasattr(c, 'name') and c.name
        ]
        assert "idx_live_trades_signal_broker" in index_names


# ═══════════════════════════════════════════════════════════════
# 8. MIGRATION V14 TEST
# ═══════════════════════════════════════════════════════════════

class TestMigrationV14:
    """Migration v13→v14 handles duplicates and creates constraints."""

    def test_migration_v14_exists(self):
        from backend.db.migrations import MIGRATIONS, CURRENT_SCHEMA_VERSION
        assert CURRENT_SCHEMA_VERSION == 14
        versions = [(f, t) for f, t, _ in MIGRATIONS]
        assert (13, 14) in versions

    def test_migration_v14_on_empty_db(self, tmp_path):
        """Migration should work on a fresh database."""
        import sqlite3
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        
        # Create minimal live_trades table
        conn.execute("""
            CREATE TABLE live_trades (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                broker TEXT NOT NULL DEFAULT 'kraken',
                signal_id TEXT,
                status TEXT DEFAULT 'OPEN',
                ts_open TIMESTAMP,
                archived INTEGER DEFAULT 0,
                archive_reason TEXT
            )
        """)
        conn.commit()
        
        from backend.db.migrations import migrate_v13_to_v14
        migrate_v13_to_v14(conn)
        
        # Verify unique index exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_live_trades_order_broker'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_migration_v14_dedup_existing(self, tmp_path):
        """Migration should archive duplicate order_id+broker pairs."""
        import sqlite3
        db_path = tmp_path / "test_dedup.db"
        conn = sqlite3.connect(str(db_path))
        
        conn.execute("""
            CREATE TABLE live_trades (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                broker TEXT NOT NULL DEFAULT 'kraken',
                signal_id TEXT,
                status TEXT DEFAULT 'OPEN',
                ts_open TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                archived INTEGER DEFAULT 0,
                archive_reason TEXT
            )
        """)
        # Insert duplicate order_id+broker
        conn.execute(
            "INSERT INTO live_trades (id, order_id, broker) VALUES ('a1', 'ORD123', 'kraken')"
        )
        conn.execute(
            "INSERT INTO live_trades (id, order_id, broker) VALUES ('a2', 'ORD123', 'kraken')"
        )
        conn.execute(
            "INSERT INTO live_trades (id, order_id, broker) VALUES ('b1', 'ORD456', 'kraken')"
        )
        conn.commit()
        
        from backend.db.migrations import migrate_v13_to_v14
        migrate_v13_to_v14(conn)
        
        # One of the ORD123 duplicates should be archived
        cursor = conn.execute(
            "SELECT COUNT(*) FROM live_trades WHERE order_id='ORD123' AND archived=1"
        )
        archived_count = cursor.fetchone()[0]
        assert archived_count >= 1, "At least one duplicate should be archived"
        
        # Non-duplicate should NOT be archived
        cursor = conn.execute(
            "SELECT archived FROM live_trades WHERE order_id='ORD456'"
        )
        assert cursor.fetchone()[0] == 0
        conn.close()


# ═══════════════════════════════════════════════════════════════
# 9. BALANCE CACHE TTL REDUCED
# ═══════════════════════════════════════════════════════════════

class TestBalanceCacheTTL:
    """Balance cache must use 10s TTL, not 30s."""

    def test_cache_ttl_is_10s(self):
        """Verify the source references 10s cache, not 30s."""
        import inspect
        from backend.kraken_futures_rest_v2 import KrakenFuturesRESTClientV2
        source = inspect.getsource(KrakenFuturesRESTClientV2.get_balance)
        assert "cache_age < 10" in source, "Balance cache TTL should be 10s"
        assert "cache_age < 30" not in source, "Balance cache TTL should NOT be 30s"


# ═══════════════════════════════════════════════════════════════
# 10. RATE LIMITER INTEGRATION IN REST CLIENT
# ═══════════════════════════════════════════════════════════════

class TestRateLimiterIntegration:
    """Rate limiter must be called in REST client _request."""

    def test_rest_client_imports_rate_limiter(self):
        import inspect
        from backend import kraken_futures_rest_v2
        source = inspect.getsource(kraken_futures_rest_v2)
        assert "rate_limiter" in source
        assert "limiter.acquire" in source or "await limiter.acquire" in source
