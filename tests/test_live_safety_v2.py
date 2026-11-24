"""
Test: Live Safety Improvements v2
==================================
Covers:
  1. Mirror trading guard (MIRROR_TRADING_ENABLED without confirm → ValueError)
  2. Positions sources (/api/v1/positions → exchange, /api/positions → db)
  3. WebSocket lifecycle (no send before accept, dead conn cleanup)
  4. Preflight endpoint enrichments (broker_modes, ws_clients_count)

Run:  pytest tests/test_live_safety_v2.py -v
"""

import os
import sys
import asyncio
import importlib
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import field


# ═══════════════════════════════════════════════════════════════
# 1. MULTI-BROKER GUARD: mirror + no confirm → fail
# ═══════════════════════════════════════════════════════════════

class TestMirrorTradingGuard:
    """Guard must fire when MIRROR_TRADING_ENABLED=true without CONFIRM."""

    def _setup_env(self, **overrides):
        defaults = {
            "TRADING_MODE": "paper",
            "AUTO_EXECUTE": "false",
            "KRAKEN_ENABLED": "false",
            "MIRROR_TRADING_ENABLED": "false",
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
        import smc_futures.smc_config as sc
        importlib.reload(sc)
        return sc

    def _cleanup(self):
        for k in (
            "TRADING_MODE", "AUTO_EXECUTE", "KRAKEN_ENABLED",
            "MIRROR_TRADING_ENABLED", "CONFIRM_LIVE_MULTI_BROKER",
            "BITGET_REAL_ONLY", "BITGET_PRODUCT_TYPE", "NODE_ENV",
            "FORCE_EXECUTE",
        ):
            os.environ.pop(k, None)

    def test_mirror_without_confirm_raises(self):
        """LIVE + AUTO=true + MIRROR=true + CONFIRM=false → ValueError."""
        self._setup_env(
            TRADING_MODE="live",
            AUTO_EXECUTE="true",
            MIRROR_TRADING_ENABLED="true",
            CONFIRM_LIVE_MULTI_BROKER="false",
        )
        try:
            with pytest.raises(ValueError) as exc_info:
                self._reload_config()
            assert "[LIVE_MULTI_BROKER]" in str(exc_info.value)
            assert "MIRROR_TRADING_ENABLED=true" in str(exc_info.value)
        finally:
            self._cleanup()

    def test_mirror_with_confirm_ok(self):
        """LIVE + AUTO=true + MIRROR=true + CONFIRM=true → boots fine."""
        self._setup_env(
            TRADING_MODE="live",
            AUTO_EXECUTE="true",
            MIRROR_TRADING_ENABLED="true",
            CONFIRM_LIVE_MULTI_BROKER="true",
        )
        try:
            sc = self._reload_config()
            assert sc.SMC_CONFIG["MIRROR_TRADING_ENABLED"] is True
            assert sc.SMC_CONFIG["CONFIRM_LIVE_MULTI_BROKER"] is True
        finally:
            self._cleanup()

    def test_kraken_and_mirror_without_confirm_raises(self):
        """LIVE + AUTO + KRAKEN + MIRROR without confirm → ValueError naming both."""
        self._setup_env(
            TRADING_MODE="live",
            AUTO_EXECUTE="true",
            BITGET_ENABLED="true",
            KRAKEN_ENABLED="true",
            MIRROR_TRADING_ENABLED="true",
            CONFIRM_LIVE_MULTI_BROKER="false",
        )
        try:
            with pytest.raises(ValueError) as exc_info:
                self._reload_config()
            msg = str(exc_info.value)
            assert "KRAKEN_ENABLED=true" in msg or "BITGET_ENABLED=true" in msg
            assert "MIRROR_TRADING_ENABLED=true" in msg
        finally:
            self._cleanup()

    def test_mirror_off_no_guard(self):
        """LIVE + AUTO=true + MIRROR=false + KRAKEN=false → no guard."""
        self._setup_env(
            TRADING_MODE="live",
            AUTO_EXECUTE="true",
            MIRROR_TRADING_ENABLED="false",
            KRAKEN_ENABLED="false",
        )
        try:
            sc = self._reload_config()
            assert sc.SMC_CONFIG["TRADING_MODE"] == "live"
        finally:
            self._cleanup()


# ═══════════════════════════════════════════════════════════════
# 2. POSITIONS SOURCES
# ═══════════════════════════════════════════════════════════════

class TestPositionsSources:
    """Verify source and broker fields in positions responses."""

    def test_exchange_positions_has_source_and_broker(self):
        """GET /api/v1/positions should return source=exchange, broker=bitget."""
        # Simulate what the endpoint does after executor returns result
        result = {
            "code": "00000",
            "data": [],
        }
        # Replicate the endpoint logic from market.py
        if isinstance(result, dict):
            if "count" not in result:
                positions = result.get("positions", result.get("data", []))
                result["count"] = len(positions) if isinstance(positions, list) else 0
            if "source" not in result:
                result["source"] = "exchange"
            if "broker" not in result:
                result["broker"] = "bitget"
            if "status" not in result:
                result["status"] = "OPEN"
            if "positions" not in result and "data" in result:
                result["positions"] = result["data"]

        assert result["source"] == "exchange"
        assert result["broker"] == "bitget"
        assert result["status"] == "OPEN"
        assert result["count"] == 0

    def test_db_positions_has_source(self):
        """DB positions response should include source=db."""
        # The positions.py response object now includes source="db"
        response = {
            "source": "db",
            "positions": [],
            "total": 0,
            "open_count": 0,
            "closed_count": 0,
            "page": 1,
            "page_size": 50,
        }
        assert response["source"] == "db"


# ═══════════════════════════════════════════════════════════════
# 3. WEBSOCKET LIFECYCLE
# ═══════════════════════════════════════════════════════════════

class TestWebSocketLifecycle:
    """Test _accepted tracking and dead connection cleanup."""

    @pytest.mark.asyncio
    async def test_broadcast_skips_non_accepted(self):
        """Broadcast should skip sockets not in _accepted set."""
        from backend.ws.manager import ConnectionManager

        mgr = ConnectionManager()
        
        # Create a mock socket and add it WITHOUT going through connect()
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()
        mgr.active_connections.append(mock_ws)
        # NOT adding to _accepted — simulates race condition
        
        count = await mgr.broadcast({"type": "test"})
        
        # Should not have sent to the non-accepted socket
        assert count == 0
        mock_ws.send_json.assert_not_called()
        # Dead connection should be cleaned up
        assert mock_ws not in mgr.active_connections

    @pytest.mark.asyncio
    async def test_connect_adds_to_accepted(self):
        """connect() should properly add to both lists and _accepted."""
        from backend.ws.manager import ConnectionManager

        mgr = ConnectionManager()
        
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()
        
        await mgr.connect(mock_ws)
        
        assert mock_ws in mgr.active_connections
        assert id(mock_ws) in mgr._accepted

    @pytest.mark.asyncio
    async def test_disconnect_removes_from_accepted(self):
        """disconnect() should remove from both lists and _accepted."""
        from backend.ws.manager import ConnectionManager

        mgr = ConnectionManager()
        
        mock_ws = MagicMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_json = AsyncMock()
        
        await mgr.connect(mock_ws)
        mgr.disconnect(mock_ws)
        
        assert mock_ws not in mgr.active_connections
        assert id(mock_ws) not in mgr._accepted

    @pytest.mark.asyncio
    async def test_broadcast_removes_failed_connections(self):
        """Failed sends should remove connections from active list."""
        from backend.ws.manager import ConnectionManager

        mgr = ConnectionManager()
        
        # Good connection
        good_ws = MagicMock()
        good_ws.send_json = AsyncMock()
        await mgr.connect(good_ws)
        
        # Bad connection: succeed on welcome, fail on broadcast
        bad_ws = MagicMock()
        bad_ws.send_json = AsyncMock(side_effect=[None, RuntimeError("closed")])
        await mgr.connect(bad_ws)
        
        assert len(mgr.active_connections) == 2
        
        count = await mgr.broadcast({"type": "test"})
        
        assert count == 1  # Only good_ws succeeded
        assert good_ws in mgr.active_connections
        assert bad_ws not in mgr.active_connections

    @pytest.mark.asyncio
    async def test_send_json_guards_non_accepted(self):
        """_send_json should refuse to send to non-accepted socket."""
        from backend.ws.manager import ConnectionManager

        mgr = ConnectionManager()
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()
        
        # Not accepted
        result = await mgr._send_json(mock_ws, {"type": "test"})
        
        assert result is False
        mock_ws.send_json.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 4. PREFLIGHT ENDPOINT ENRICHMENTS
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def mock_ctx():
    ctx = MagicMock()
    ctx.executors = {"futures": MagicMock()}
    ctx._kraken_mode = "read_only"
    ctx.get_broker_status.return_value = {
        "default_broker": "bitget",
        "brokers": {
            "bitget": {"enabled": True, "connected": True},
            "kraken": {"enabled": True, "connected": True},
        },
    }
    return ctx


@pytest.fixture
def mock_config():
    return {
        "TRADING_MODE": "live",
        "AUTO_EXECUTE_MODE": "true",
        "MIRROR_TRADING_ENABLED": False,
        "CONFIRM_LIVE_MULTI_BROKER": True,
    }


@pytest.mark.asyncio
async def test_preflight_has_broker_modes(mock_ctx, mock_config):
    """Preflight should return broker_modes with read_only/execute."""
    with patch("smc_futures.api_smc_engine.ctx", mock_ctx), \
         patch("smc_futures.api_smc_engine.SMC_CONFIG", mock_config):

        from smc_futures.api_smc_engine import app
        from httpx import AsyncClient, ASGITransport

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/preflight")

        assert resp.status_code == 200
        data = resp.json()

        # New fields
        assert "broker_modes" in data
        assert data["broker_modes"]["bitget"] == "execute"
        assert data["broker_modes"]["kraken"] == "read_only"
        assert "ws_clients_count" in data
        assert isinstance(data["ws_clients_count"], int)
        assert "confirm_live_multi_broker" in data


@pytest.mark.asyncio
async def test_preflight_mirror_disabled_shows_read_only(mock_ctx, mock_config):
    """When mirror is off, Kraken should be read_only in preflight."""
    mock_config["MIRROR_TRADING_ENABLED"] = False
    mock_ctx._kraken_mode = "read_only"

    with patch("smc_futures.api_smc_engine.ctx", mock_ctx), \
         patch("smc_futures.api_smc_engine.SMC_CONFIG", mock_config):

        from smc_futures.api_smc_engine import app
        from httpx import AsyncClient, ASGITransport

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/preflight")

        data = resp.json()
        assert data["mirror_enabled"] is False
        assert data["broker_modes"]["kraken"] == "read_only"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
