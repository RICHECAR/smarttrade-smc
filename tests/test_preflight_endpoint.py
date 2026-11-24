"""
Test: Preflight Endpoint
========================
Verify: GET /api/preflight returns expected structure with
  trading_mode, auto_execute, brokers, ws_routes, etc.

Run:  pytest tests/test_preflight_endpoint.py -v
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_ctx():
    """Build a minimal mock for the app context."""
    ctx = MagicMock()
    ctx.executors = {}
    ctx.get_broker_status.return_value = {
        "default_broker": "bitget",
        "brokers": {
            "bitget": {"enabled": True, "connected": False},
            "kraken": {"enabled": False, "connected": False},
        },
    }
    return ctx


@pytest.fixture
def mock_config():
    """Minimal SMC_CONFIG for preflight."""
    return {
        "TRADING_MODE": "paper",
        "AUTO_EXECUTE_MODE": "false",
        "MIRROR_TRADING_ENABLED": False,
    }


@pytest.mark.asyncio
async def test_preflight_returns_200(mock_ctx, mock_config):
    """GET /api/preflight should return 200 with expected fields."""
    with patch("smc_futures.api_smc_engine.ctx", mock_ctx), \
         patch("smc_futures.api_smc_engine.SMC_CONFIG", mock_config):
        
        from smc_futures.api_smc_engine import app
        from httpx import AsyncClient, ASGITransport

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/preflight")
        
        assert resp.status_code == 200
        data = resp.json()

        # Required fields
        assert "trading_mode" in data
        assert "auto_execute" in data
        assert "mirror_enabled" in data
        assert "brokers" in data
        assert "ws_routes" in data
        assert "balances" in data
        assert "open_positions_exchange" in data
        assert "timestamp" in data


@pytest.mark.asyncio
async def test_preflight_mirror_disabled(mock_ctx, mock_config):
    """mirror_enabled should be False when MIRROR_TRADING_ENABLED is off."""
    mock_config["MIRROR_TRADING_ENABLED"] = False

    with patch("smc_futures.api_smc_engine.ctx", mock_ctx), \
         patch("smc_futures.api_smc_engine.SMC_CONFIG", mock_config):

        from smc_futures.api_smc_engine import app
        from httpx import AsyncClient, ASGITransport

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/preflight")

        data = resp.json()
        assert data["mirror_enabled"] is False


@pytest.mark.asyncio
async def test_preflight_ws_routes_populated(mock_ctx, mock_config):
    """ws_routes should contain at least /ws and /ws/trades."""
    with patch("smc_futures.api_smc_engine.ctx", mock_ctx), \
         patch("smc_futures.api_smc_engine.SMC_CONFIG", mock_config):

        from smc_futures.api_smc_engine import app
        from httpx import AsyncClient, ASGITransport

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/preflight")

        data = resp.json()
        ws_routes = data.get("ws_routes", [])
        # Should have at least the main WS routes
        assert any("/ws" in r for r in ws_routes), f"Expected /ws route, got {ws_routes}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
