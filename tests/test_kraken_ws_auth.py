"""
Test Kraken Futures WebSocket Authentication Flow

Tests the challenge/response authentication mechanism with mocked WS messages.
Can be run without real Kraken credentials.

Usage:
    pytest tests/test_kraken_ws_auth.py -v

Features tested:
  - Handling initial "info" message from server
  - Waiting for challenge event while skipping info/heartbeat
  - Full authentication flow with mocked responses
  - Error handling for various failure scenarios
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.kraken_futures_client import (
    KrakenFuturesClient,
    KrakenAuthError,
)

# Configure logging for tests
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# MOCK WEBSOCKET
# ══════════════════════════════════════════════════════════════════════════════

class MockWebSocket:
    """Mock WebSocket that returns predefined messages."""
    
    def __init__(self, messages: List[Dict[str, Any]]):
        """
        Initialize mock with list of messages to return.
        
        Args:
            messages: List of dicts, each will be JSON-encoded and returned
                     in order by recv()
        """
        self._messages = [json.dumps(m) for m in messages]
        self._index = 0
        self._sent: List[str] = []
    
    async def recv(self) -> str:
        """Return next message from queue."""
        if self._index >= len(self._messages):
            # Simulate timeout by blocking forever (test timeout will catch)
            await asyncio.sleep(100)
            raise asyncio.TimeoutError("No more mock messages")
        
        msg = self._messages[self._index]
        self._index += 1
        return msg
    
    async def send(self, message: str) -> None:
        """Record sent messages."""
        self._sent.append(message)
    
    async def close(self) -> None:
        """Mock close."""
        pass
    
    @property
    def sent_messages(self) -> List[Dict[str, Any]]:
        """Get list of sent messages as parsed JSON."""
        return [json.loads(m) for m in self._sent]


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS - _wait_for_event
# ══════════════════════════════════════════════════════════════════════════════

class TestWaitForEvent:
    """Tests for _wait_for_event helper method."""
    
    @pytest.fixture
    def client(self):
        """Create client fixture with dummy credentials."""
        return KrakenFuturesClient(
            api_key="test_api_key_12345",
            api_secret="dGVzdF9zZWNyZXRfYmFzZTY0X2VuY29kZWQ=",  # base64 encoded
        )
    
    @pytest.mark.asyncio
    async def test_wait_skips_info_message(self, client):
        """Test that info messages are skipped while waiting for challenge."""
        # Server sends info first, then challenge
        mock_ws = MockWebSocket([
            {"event": "info", "version": 1},
            {"event": "challenge", "message": "test_challenge_string"},
        ])
        client._ws = mock_ws
        
        result = await client._wait_for_event("challenge", timeout=5.0)
        
        assert result["event"] == "challenge"
        assert result["message"] == "test_challenge_string"
    
    @pytest.mark.asyncio
    async def test_wait_skips_multiple_info_messages(self, client):
        """Test that multiple info/heartbeat messages are skipped."""
        mock_ws = MockWebSocket([
            {"event": "info", "version": 1},
            {"event": "heartbeat"},
            {"event": "subscribed", "channel": "test"},
            {"event": "pong"},
            {"event": "challenge", "message": "the_challenge"},
        ])
        client._ws = mock_ws
        
        result = await client._wait_for_event("challenge", timeout=5.0)
        
        assert result["event"] == "challenge"
        assert result["message"] == "the_challenge"
    
    @pytest.mark.asyncio
    async def test_wait_returns_first_matching_event(self, client):
        """Test that first matching event is returned."""
        mock_ws = MockWebSocket([
            {"event": "info", "version": 1},
            {"event": "challenge", "message": "first_challenge"},
            {"event": "challenge", "message": "second_challenge"},  # Should not be reached
        ])
        client._ws = mock_ws
        
        result = await client._wait_for_event("challenge", timeout=5.0)
        
        assert result["message"] == "first_challenge"
    
    @pytest.mark.asyncio
    async def test_wait_raises_on_error_event(self, client):
        """Test that error events raise KrakenAuthError."""
        mock_ws = MockWebSocket([
            {"event": "info", "version": 1},
            {"event": "error", "message": "Invalid API key"},
        ])
        client._ws = mock_ws
        
        with pytest.raises(KrakenAuthError) as exc_info:
            await client._wait_for_event("challenge", timeout=5.0)
        
        assert "Invalid API key" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_wait_handles_max_messages(self, client):
        """Test that max_messages limit is enforced."""
        # Send many info messages, no challenge
        mock_ws = MockWebSocket([
            {"event": "info", "version": 1}
            for _ in range(25)
        ])
        client._ws = mock_ws
        
        with pytest.raises(KrakenAuthError) as exc_info:
            await client._wait_for_event("challenge", timeout=10.0, max_messages=5)
        
        assert "Max messages" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_wait_handles_non_json_messages(self, client):
        """Test graceful handling of non-JSON messages."""
        # Manually set up mock that returns non-JSON
        mock_ws = MagicMock()
        mock_ws.recv = AsyncMock(side_effect=[
            "not valid json",
            json.dumps({"event": "challenge", "message": "test"}),
        ])
        client._ws = mock_ws
        
        result = await client._wait_for_event("challenge", timeout=5.0)
        
        assert result["event"] == "challenge"
    
    @pytest.mark.asyncio
    async def test_wait_handles_messages_without_event(self, client):
        """Test handling of messages without 'event' field."""
        mock_ws = MockWebSocket([
            {"channel": "executions", "data": []},  # No event field
            {"event": "challenge", "message": "test"},
        ])
        client._ws = mock_ws
        
        result = await client._wait_for_event("challenge", timeout=5.0)
        
        assert result["event"] == "challenge"


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS - Full Authentication Flow
# ══════════════════════════════════════════════════════════════════════════════

class TestAuthenticateWSFutures:
    """Tests for the full _authenticate_ws_futures method."""
    
    @pytest.fixture
    def client(self):
        """Create client with test credentials."""
        return KrakenFuturesClient(
            api_key="test_api_key_12345",
            api_secret="dGVzdF9zZWNyZXRfYmFzZTY0X2VuY29kZWQ=",
        )
    
    def test_sign_challenge_uses_sha512(self):
        """
        Test that _sign_challenge follows Kraken Futures spec:
        signed_challenge = base64( HMAC_SHA512( base64_decode(secret), SHA256(challenge) ) )
        """
        import base64
        import hashlib
        import hmac
        
        client = KrakenFuturesClient(
            api_key="test_key",
            api_secret="dGVzdF9zZWNyZXQ=",  # base64("test_secret")
        )
        
        challenge = "test-challenge-uuid"
        result = client._sign_challenge(challenge)
        
        # Verify it matches Kraken spec: HMAC-SHA512( secret, SHA256(challenge) )
        secret_decoded = base64.b64decode("dGVzdF9zZWNyZXQ=")
        sha256_of_challenge = hashlib.sha256(challenge.encode()).digest()
        expected = hmac.new(secret_decoded, sha256_of_challenge, hashlib.sha512)
        expected_b64 = base64.b64encode(expected.digest()).decode()
        
        assert result == expected_b64, "Signature must match Kraken spec"
        
        # Verify it does NOT match naive HMAC-SHA512(challenge) without SHA256 step
        wrong_no_sha256 = hmac.new(secret_decoded, challenge.encode(), hashlib.sha512)
        wrong_b64 = base64.b64encode(wrong_no_sha256.digest()).decode()
        assert result != wrong_b64, "Must SHA256 challenge first, then HMAC-SHA512"
    
    @pytest.mark.asyncio
    async def test_full_auth_flow_success(self, client):
        """Test successful authentication flow with info message first."""
        # Simulate: info -> challenge -> subscribed (per Kraken Futures spec)
        mock_ws = MockWebSocket([
            {"event": "info", "version": 1},  # Initial info
            {"event": "challenge", "message": "challenge_string_from_server"},  # Challenge
            {"event": "subscribed", "feed": "open_orders"},  # Subscription confirmation
        ])
        client._ws = mock_ws
        
        result = await client._authenticate_ws_futures()
        
        assert result is True
        
        # Verify messages sent by client
        sent = mock_ws.sent_messages
        assert len(sent) == 2
        
        # First message: challenge request
        assert sent[0]["event"] == "challenge"
        assert sent[0]["api_key"] == "test_api_key_12345"
        
        # Second message: subscribe to private feed with signature
        assert sent[1]["event"] == "subscribe"
        assert sent[1]["feed"] == "open_orders"
        assert sent[1]["api_key"] == "test_api_key_12345"
        assert sent[1]["original_challenge"] == "challenge_string_from_server"
        assert "signed_challenge" in sent[1]
    
    @pytest.mark.asyncio
    async def test_auth_flow_no_info_message(self, client):
        """Test auth flow when server doesn't send initial info."""
        # Some servers may skip the info message
        mock_ws = MockWebSocket([
            {"event": "challenge", "message": "direct_challenge"},
            {"event": "subscribed", "feed": "open_orders"},
        ])
        client._ws = mock_ws
        
        result = await client._authenticate_ws_futures()
        
        assert result is True
    
    @pytest.mark.asyncio
    async def test_auth_flow_multiple_info_messages(self, client):
        """Test auth handles multiple info/heartbeat before challenge."""
        mock_ws = MockWebSocket([
            {"event": "info", "version": 1},
            {"event": "info", "version": 2},  # Version upgrade message
            {"event": "heartbeat"},
            {"event": "challenge", "message": "the_challenge"},
            {"event": "heartbeat"},  # Heartbeat between challenge and subscription
            {"event": "subscribed", "feed": "open_orders"},
        ])
        client._ws = mock_ws
        
        result = await client._authenticate_ws_futures()
        
        assert result is True
    
    @pytest.mark.asyncio
    async def test_auth_fails_on_invalid_credentials(self, client):
        """Test auth failure with invalid API key/secret."""
        mock_ws = MockWebSocket([
            {"event": "info", "version": 1},
            {"event": "error", "message": "Invalid API key"},
        ])
        client._ws = mock_ws
        
        with pytest.raises(KrakenAuthError) as exc_info:
            await client._authenticate_ws_futures()
        
        assert "Invalid API key" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_auth_fails_on_bad_signature(self, client):
        """Test auth failure when signature is rejected."""
        mock_ws = MockWebSocket([
            {"event": "info", "version": 1},
            {"event": "challenge", "message": "test_challenge"},
            {"event": "error", "message": "authenticationError: Invalid signed challenge"},
        ])
        client._ws = mock_ws
        
        with pytest.raises(KrakenAuthError) as exc_info:
            await client._authenticate_ws_futures()
        
        assert "invalid" in str(exc_info.value).lower() or "error" in str(exc_info.value).lower()
    
    @pytest.mark.asyncio
    async def test_auth_no_ws_connection(self, client):
        """Test auth fails gracefully when WS not connected."""
        client._ws = None
        
        with pytest.raises(KrakenAuthError) as exc_info:
            await client._authenticate_ws_futures()
        
        assert "not connected" in str(exc_info.value).lower()
    
    @pytest.mark.asyncio
    async def test_auth_fails_on_empty_challenge_message(self, client):
        """Test auth fails when challenge message is empty."""
        mock_ws = MockWebSocket([
            {"event": "info", "version": 1},
            {"event": "challenge", "message": ""},  # Empty challenge
        ])
        client._ws = mock_ws
        
        with pytest.raises(KrakenAuthError) as exc_info:
            await client._authenticate_ws_futures()
        
        assert "empty" in str(exc_info.value).lower()
    
    @pytest.mark.asyncio
    async def test_auth_fails_on_challenge_without_message_field(self, client):
        """Test auth fails when challenge event has no message field."""
        mock_ws = MockWebSocket([
            {"event": "info", "version": 1},
            {"event": "challenge"},  # No message field at all
        ])
        client._ws = mock_ws
        
        with pytest.raises(KrakenAuthError) as exc_info:
            await client._authenticate_ws_futures()
        
        assert "empty" in str(exc_info.value).lower()
    
    @pytest.mark.asyncio
    async def test_auth_timeout_waiting_for_challenge(self, client):
        """Test auth times out when server doesn't send challenge."""
        # Create a mock that simulates timeout on recv
        mock_ws = MagicMock()
        mock_ws.recv = AsyncMock(side_effect=asyncio.TimeoutError("connection timed out"))
        mock_ws.send = AsyncMock()
        client._ws = mock_ws
        
        # Should raise KrakenAuthError mentioning timeout
        with pytest.raises(KrakenAuthError) as exc_info:
            await client._authenticate_ws_futures()
        
        assert "timeout" in str(exc_info.value).lower()
    
    @pytest.mark.asyncio
    async def test_auth_handles_subscribed_with_extra_fields(self, client):
        """Test auth accepts subscribed event with extra fields."""
        mock_ws = MockWebSocket([
            {"event": "challenge", "message": "test"},
            {"event": "subscribed", "feed": "open_orders", "extra": "data"},
        ])
        client._ws = mock_ws
        
        result = await client._authenticate_ws_futures()
        
        assert result is True


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TEST STUB (requires real credentials)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.skip(reason="Integration test - requires real Kraken credentials")
class TestKrakenWSIntegration:
    """
    Integration tests that connect to real Kraken Futures WebSocket.
    
    To run these tests:
    1. Set KRAKEN_API_KEY and KRAKEN_API_SECRET environment variables
    2. Run: pytest tests/test_kraken_ws_auth.py::TestKrakenWSIntegration -v --no-skip
    """
    
    @pytest.fixture
    def real_client(self):
        """Create client with real credentials from environment."""
        import os
        api_key = os.getenv("KRAKEN_API_KEY")
        api_secret = os.getenv("KRAKEN_API_SECRET")
        
        if not api_key or not api_secret:
            pytest.skip("KRAKEN_API_KEY/KRAKEN_API_SECRET not set")
        
        return KrakenFuturesClient(api_key, api_secret)
    
    @pytest.mark.asyncio
    async def test_real_ws_connection(self, real_client):
        """Test real WebSocket connection and authentication."""
        await real_client.connect()
        assert real_client.is_connected
        
        await real_client.disconnect()
        assert not real_client.is_connected


# ══════════════════════════════════════════════════════════════════════════════
# RUN TESTS DIRECTLY
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
