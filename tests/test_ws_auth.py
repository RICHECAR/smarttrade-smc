"""
Test WebSocket Authentication for /ws/trades

Tests the WebSocket endpoint authentication:
- Origin check allows whitelisted origins
- Token auth works when WS_AUTH_TOKEN is set
- DEV mode bypasses auth checks
- Proper rejection codes for auth failures
- WebSocket connection tests with TestClient

Usage:
    pytest tests/test_ws_auth.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


# ══════════════════════════════════════════════════════════════════════════════
# MOCK WEBSOCKET FOR TESTING
# ══════════════════════════════════════════════════════════════════════════════

class MockWebSocket:
    """Mock WebSocket for testing auth functions."""
    
    def __init__(self, origin: str = "", token: str = "", client_host: str = "127.0.0.1"):
        self.headers = {"origin": origin}
        self.query_params = {"token": token} if token else {}
        self.client = MagicMock()
        self.client.host = client_host
        self.closed = False
        self.close_code = None
        self.close_reason = None
    
    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True
        self.close_code = code
        self.close_reason = reason


# ══════════════════════════════════════════════════════════════════════════════
# TEST: _check_ws_origin
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckWsOrigin:
    """Tests for WebSocket origin checking."""
    
    def test_allows_localhost_3000(self):
        """Test that localhost:3000 is allowed."""
        # Import inside test to avoid import order issues
        from smc_futures.api_smc_engine import _check_ws_origin
        
        ws = MockWebSocket(origin="http://localhost:3000")
        assert _check_ws_origin(ws) is True
    
    def test_allows_localhost_5173(self):
        """Test that localhost:5173 (Vite dev) is allowed."""
        from smc_futures.api_smc_engine import _check_ws_origin
        
        ws = MockWebSocket(origin="http://localhost:5173")
        assert _check_ws_origin(ws) is True
    
    def test_allows_127_0_0_1(self):
        """Test that 127.0.0.1 is allowed."""
        from smc_futures.api_smc_engine import _check_ws_origin
        
        ws = MockWebSocket(origin="http://127.0.0.1:8000")
        assert _check_ws_origin(ws) is True
    
    def test_allows_no_origin(self):
        """Test that connections without origin (direct) are allowed."""
        from smc_futures.api_smc_engine import _check_ws_origin
        
        ws = MockWebSocket(origin="")
        assert _check_ws_origin(ws) is True
    
    def test_allows_all_when_star_in_list(self):
        """Test that '*' in whitelist allows all origins."""
        from smc_futures.api_smc_engine import _check_ws_origin, WS_ALLOWED_ORIGINS
        
        # The default list includes "*"
        assert "*" in WS_ALLOWED_ORIGINS
        
        ws = MockWebSocket(origin="https://evil.com")
        # Should be allowed because "*" is in list
        assert _check_ws_origin(ws) is True


# ══════════════════════════════════════════════════════════════════════════════
# TEST: _check_ws_auth
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckWsAuth:
    """Tests for WebSocket token authentication."""
    
    def test_no_auth_required_when_token_empty(self):
        """Test that auth passes when WS_AUTH_TOKEN is not set."""
        from smc_futures.api_smc_engine import _check_ws_auth
        
        with patch("smc_futures.api_smc_engine.WS_AUTH_TOKEN", ""), \
             patch("smc_futures.api_smc_engine.WS_DEV_MODE", False):
            ws = MockWebSocket()
            ok, reason = _check_ws_auth(ws)
            assert ok is True
            assert reason == "no_auth_required"
    
    def test_auth_fails_missing_token(self):
        """Test that auth fails when token required but not provided."""
        from smc_futures.api_smc_engine import _check_ws_auth
        
        with patch("smc_futures.api_smc_engine.WS_AUTH_TOKEN", "secret123"), \
             patch("smc_futures.api_smc_engine.WS_DEV_MODE", False):
            ws = MockWebSocket(token="")
            ok, reason = _check_ws_auth(ws)
            assert ok is False
            assert reason == "missing_token"
    
    def test_auth_fails_invalid_token(self):
        """Test that auth fails when token is wrong."""
        from smc_futures.api_smc_engine import _check_ws_auth
        
        with patch("smc_futures.api_smc_engine.WS_AUTH_TOKEN", "secret123"), \
             patch("smc_futures.api_smc_engine.WS_DEV_MODE", False):
            ws = MockWebSocket(token="wrongtoken")
            ok, reason = _check_ws_auth(ws)
            assert ok is False
            assert reason == "invalid_token"
    
    def test_auth_passes_valid_token(self):
        """Test that auth passes with correct token."""
        from smc_futures.api_smc_engine import _check_ws_auth
        
        with patch("smc_futures.api_smc_engine.WS_AUTH_TOKEN", "secret123"), \
             patch("smc_futures.api_smc_engine.WS_DEV_MODE", False):
            ws = MockWebSocket(token="secret123")
            ok, reason = _check_ws_auth(ws)
            assert ok is True
            assert reason == "token_valid"
    
    def test_dev_mode_bypasses_auth(self):
        """Test that WS_DEV_MODE=1 bypasses all auth checks."""
        from smc_futures.api_smc_engine import _check_ws_auth
        
        with patch("smc_futures.api_smc_engine.WS_AUTH_TOKEN", "secret123"), \
             patch("smc_futures.api_smc_engine.WS_DEV_MODE", True):
            # Even with wrong token, should pass in dev mode
            ws = MockWebSocket(token="wrongtoken")
            ok, reason = _check_ws_auth(ws)
            assert ok is True
            assert reason == "dev_mode"


class TestDevModeOrigin:
    """Tests for WS_DEV_MODE origin bypass."""
    
    def test_dev_mode_allows_all_origins(self):
        """Test that WS_DEV_MODE allows any origin."""
        from smc_futures.api_smc_engine import _check_ws_origin
        
        with patch("smc_futures.api_smc_engine.WS_DEV_MODE", True):
            ws = MockWebSocket(origin="https://evil.com")
            assert _check_ws_origin(ws) is True


# ══════════════════════════════════════════════════════════════════════════════
# TEST: WebSocket URL format
# ══════════════════════════════════════════════════════════════════════════════

class TestWebSocketUrlFormat:
    """Tests for correct WebSocket URL formatting."""
    
    def test_correct_ws_url_format(self):
        """Test that the correct WS URL format is documented."""
        # The correct URL format should be:
        # ws://localhost:8000/ws/trades
        # NOT ws://localhost:8000/ws/ws/trades
        
        correct_urls = [
            "ws://localhost:8000/ws",
            "ws://localhost:8000/ws/trades",
        ]
        
        # Double /ws is wrong
        incorrect_urls = [
            "ws://localhost:8000/ws/ws/trades",
            "ws://localhost:8000/ws/ws",
        ]
        
        for url in correct_urls:
            assert "/ws/ws" not in url
        
        for url in incorrect_urls:
            assert "/ws/ws" in url
    
    def test_vite_config_produces_correct_url(self):
        """Document that VITE_WS_URL should NOT include /ws suffix."""
        # CORRECT: VITE_WS_URL=ws://localhost:8000
        # WRONG: VITE_WS_URL=ws://localhost:8000/ws
        
        # Frontend code: `${WS_BASE}/ws/trades`
        # With correct base: ws://localhost:8000/ws/trades ✓
        # With wrong base: ws://localhost:8000/ws/ws/trades ✗
        
        correct_base = "ws://localhost:8000"
        path = "/ws/trades"
        full_url = f"{correct_base}{path}"
        
        assert full_url == "ws://localhost:8000/ws/trades"
        assert "/ws/ws" not in full_url


# ══════════════════════════════════════════════════════════════════════════════
# TEST: WebSocket Integration (using TestClient)
# ══════════════════════════════════════════════════════════════════════════════

class TestWebSocketIntegration:
    """
    Integration tests for WebSocket endpoints.
    
    Uses FastAPI TestClient to test actual WebSocket connections.
    """
    
    @pytest.fixture(autouse=True)
    def setup_dev_mode(self):
        """Enable dev mode for all integration tests."""
        with patch("smc_futures.api_smc_engine.WS_DEV_MODE", True), \
             patch("smc_futures.api_smc_engine.WS_AUTH_TOKEN", ""):
            yield
    
    def test_ws_endpoint_connects(self):
        """Test that /ws endpoint accepts connections."""
        from fastapi.testclient import TestClient
        from smc_futures.api_smc_engine import create_api_app
        
        ws_app = create_api_app(enable_startup_hooks=False, include_ws_only=True)
        client = TestClient(ws_app)
        
        with client.websocket_connect("/ws") as websocket:
            # First message is "connected" confirmation
            first_msg = websocket.receive_json()
            assert first_msg["type"] == "connected"
            
            # Send ping, expect pong
            websocket.send_text("ping")
            data = websocket.receive_text()
            assert data == "pong"
    
    def test_ws_trades_endpoint_connects(self):
        """Test that /ws/trades endpoint accepts connections."""
        from fastapi.testclient import TestClient
        from smc_futures.api_smc_engine import create_api_app
        
        ws_app = create_api_app(enable_startup_hooks=False, include_ws_only=True)
        client = TestClient(ws_app)
        
        with client.websocket_connect("/ws/trades") as websocket:
            # First message is "connected" confirmation
            first_msg = websocket.receive_json()
            assert first_msg["type"] == "connected"
            
            # Send ping, expect pong
            websocket.send_text("ping")
            data = websocket.receive_text()
            assert data == "pong"
    
    def test_ws_status_command(self):
        """Test that status command returns connection info."""
        from fastapi.testclient import TestClient
        from smc_futures.api_smc_engine import create_api_app
        
        ws_app = create_api_app(enable_startup_hooks=False, include_ws_only=True)
        client = TestClient(ws_app)
        
        with client.websocket_connect("/ws") as websocket:
            # First message is "connected" confirmation
            _ = websocket.receive_json()
            
            # Send status command
            websocket.send_text("status")
            data = websocket.receive_json()
            assert data["type"] == "status"
            assert data["connected"] is True
            assert "clients" in data
    
    def test_ws_wrong_path_returns_error(self):
        """Test that wrong path /ws/ws/trades returns error."""
        from fastapi.testclient import TestClient
        from smc_futures.api_smc_engine import create_api_app
        
        ws_app = create_api_app(enable_startup_hooks=False, include_ws_only=True)
        client = TestClient(ws_app)
        
        with client.websocket_connect("/ws/ws/trades") as websocket:
            # Should receive error message
            data = websocket.receive_json()
            assert data["type"] == "error"
            assert "Unknown WebSocket path" in data["error"]
            assert "/ws" in data["valid_paths"]
            assert "/ws/trades" in data["valid_paths"]


# ══════════════════════════════════════════════════════════════════════════════
# RUN TESTS
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
