"""
P0-G3.1 — Isolated Tests for Strict Kraken LIVE / DEMO Environment Separation
=============================================================================

All HTTP (httpx.AsyncClient.send) and WebSocket (websockets.connect) transports
are mocked/blocked at the transport layer — ZERO external network calls can
ever leave the process. Only dummy credentials are used.
"""

import base64
import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import websockets

from backend.exchanges.kraken_futures import KrakenFuturesClient as ExchangeKrakenClient
from backend.kraken_futures_client import KrakenFuturesClient as WsKrakenClient
from backend.kraken_futures_rest_v2 import (
    KRAKEN_FUTURES_DEMO_REST_URL,
    KRAKEN_FUTURES_DEMO_WS_URL,
    KRAKEN_FUTURES_LIVE_REST_URL,
    KRAKEN_FUTURES_LIVE_WS_URL,
    KrakenEnvironmentConfigError,
    KrakenFuturesRESTClientV2,
    sign_kraken_futures_request,
    validate_kraken_client_environment,
)

DEMO_KEY = "demo_dummy_key_001"
DEMO_SECRET = base64.b64encode(b"demo_dummy_secret_bytes_001").decode()
LIVE_KEY = "live_dummy_key_999"
LIVE_SECRET = base64.b64encode(b"live_dummy_secret_bytes_999").decode()


@pytest.fixture(autouse=True)
def block_all_external_transports(monkeypatch):
    """Hard-block all real HTTP and WebSocket connections for every test."""
    recorded = {"http": [], "ws": []}

    async def _fake_httpx_send(self, request, *args, **kwargs):
        recorded["http"].append((request.method, str(request.url), dict(request.headers)))
        path = request.url.path
        if path.endswith("/tickers"):
            return httpx.Response(200, json={"result": "success", "tickers": []}, request=request)
        if path.endswith("/openpositions"):
            return httpx.Response(200, json={"result": "success", "openPositions": []}, request=request)
        if path.endswith("/accounts"):
            return httpx.Response(200, json={"result": "success", "accounts": {"flex": {"availableMargin": 100.0, "portfolioValue": 100.0}}}, request=request)
        return httpx.Response(200, json={"result": "success"}, request=request)

    class _FakeWS:
        def __init__(self, url):
            self.url = url
            self.closed = False
        async def send(self, msg):
            pass
        async def recv(self):
            return '{"event":"challenge","message":"dummy_challenge"}'
        async def close(self):
            self.closed = True

    async def _fake_ws_connect(url, *args, **kwargs):
        recorded["ws"].append(str(url))
        return _FakeWS(str(url))

    monkeypatch.setattr(httpx.AsyncClient, "send", _fake_httpx_send)
    monkeypatch.setattr(websockets, "connect", _fake_ws_connect)
    # Clean env vars by default
    for var in ("KRAKEN_USE_DEMO", "KRAKEN_DEMO_API_KEY", "KRAKEN_DEMO_API_SECRET", "KRAKEN_API_KEY", "KRAKEN_API_SECRET", "KRAKEN_FUTURES_API_KEY", "KRAKEN_FUTURES_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    return recorded


# ═══════════════════════════════════════════════════════════════════════════════
# POSITIVE TESTS (1-6)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_pos1_2_3_4_5_demo_mode_routes_all_three_clients_and_signs_with_demo_creds(monkeypatch, block_all_external_transports):
    monkeypatch.setenv("KRAKEN_USE_DEMO", "true")
    monkeypatch.setenv("KRAKEN_DEMO_API_KEY", DEMO_KEY)
    monkeypatch.setenv("KRAKEN_DEMO_API_SECRET", DEMO_SECRET)
    monkeypatch.setenv("KRAKEN_API_KEY", LIVE_KEY)
    monkeypatch.setenv("KRAKEN_API_SECRET", LIVE_SECRET)

    # All 3 clients resolve to DEMO URLs and DEMO credentials ONLY
    rest_v2 = KrakenFuturesRESTClientV2()
    exch_c = ExchangeKrakenClient()
    ws_c = WsKrakenClient()

    for c in (rest_v2, exch_c, ws_c):
        assert c.use_demo is True
        assert c.environment == "DEMO"
        assert c.base_url == KRAKEN_FUTURES_DEMO_REST_URL
        assert c.ws_url == KRAKEN_FUTURES_DEMO_WS_URL
        assert c.api_key == DEMO_KEY
        assert c.api_secret == DEMO_SECRET

    # Public REST (/tickers) and private REST (/openpositions, /accounts) both hit demo-futures.kraken.com
    await rest_v2.get_tickers()
    await rest_v2.get_positions(force_refresh=True)
    await exch_c.get_positions(force_refresh=True)
    await ws_c._futures_rest_get("/derivatives/api/v3/accounts")

    assert len(block_all_external_transports["http"]) == 4
    for method, url, hdrs in block_all_external_transports["http"]:
        assert url.startswith("https://demo-futures.kraken.com/"), f"Non-demo URL hit: {url}"
        assert "://futures.kraken.com" not in url

    # Verify private request signed with DEMO_KEY & DEMO_SECRET (not LIVE)
    priv_hdrs = block_all_external_transports["http"][1][2]
    # httpx lowercases header keys
    api_key_hdr = priv_hdrs.get("apikey") or priv_hdrs.get("APIKey")
    nonce_hdr = priv_hdrs.get("nonce") or priv_hdrs.get("Nonce")
    authent_hdr = priv_hdrs.get("authent") or priv_hdrs.get("Authent")
    assert api_key_hdr == DEMO_KEY
    expected_sig = sign_kraken_futures_request(DEMO_SECRET, "/api/v3/openpositions", nonce_hdr, "")
    assert authent_hdr == expected_sig


@pytest.mark.asyncio
async def test_pos6_ws_client_preserves_demo_env_across_simulated_reconnect(monkeypatch, block_all_external_transports):
    monkeypatch.setenv("KRAKEN_USE_DEMO", "true")
    monkeypatch.setenv("KRAKEN_DEMO_API_KEY", DEMO_KEY)
    monkeypatch.setenv("KRAKEN_DEMO_API_SECRET", DEMO_SECRET)

    ws_c = WsKrakenClient()
    ws_c._authenticate_ws_futures = AsyncMock(return_value=None)
    ws_c._receive_loop = AsyncMock(return_value=None)

    await ws_c.connect()
    assert block_all_external_transports["ws"] == [KRAKEN_FUTURES_DEMO_WS_URL]

    await ws_c.disconnect()
    await ws_c.connect()
    assert block_all_external_transports["ws"] == [KRAKEN_FUTURES_DEMO_WS_URL, KRAKEN_FUTURES_DEMO_WS_URL]
    assert ws_c.use_demo is True
    assert ws_c.ws_url == KRAKEN_FUTURES_DEMO_WS_URL


# ═══════════════════════════════════════════════════════════════════════════════
# NEGATIVE P0 TESTS (1-10)
# ═══════════════════════════════════════════════════════════════════════════════


def test_neg1_demo_without_demo_key_refused_even_when_live_key_exists(monkeypatch):
    monkeypatch.setenv("KRAKEN_USE_DEMO", "true")
    monkeypatch.setenv("KRAKEN_DEMO_API_SECRET", DEMO_SECRET)
    monkeypatch.setenv("KRAKEN_API_KEY", LIVE_KEY)
    monkeypatch.setenv("KRAKEN_API_SECRET", LIVE_SECRET)

    for cls in (KrakenFuturesRESTClientV2, ExchangeKrakenClient, WsKrakenClient):
        with pytest.raises(KrakenEnvironmentConfigError, match="DEMO_CREDENTIALS_MISSING"):
            cls()


def test_neg2_demo_without_demo_secret_refused(monkeypatch):
    monkeypatch.setenv("KRAKEN_USE_DEMO", "true")
    monkeypatch.setenv("KRAKEN_DEMO_API_KEY", DEMO_KEY)
    monkeypatch.setenv("KRAKEN_API_KEY", LIVE_KEY)
    monkeypatch.setenv("KRAKEN_API_SECRET", LIVE_SECRET)

    for cls in (KrakenFuturesRESTClientV2, ExchangeKrakenClient, WsKrakenClient):
        with pytest.raises(KrakenEnvironmentConfigError, match="DEMO_CREDENTIALS_MISSING"):
            cls()


def test_neg3_demo_with_injected_live_url_refused(monkeypatch):
    monkeypatch.setenv("KRAKEN_USE_DEMO", "true")
    monkeypatch.setenv("KRAKEN_DEMO_API_KEY", DEMO_KEY)
    monkeypatch.setenv("KRAKEN_DEMO_API_SECRET", DEMO_SECRET)

    with pytest.raises(KrakenEnvironmentConfigError, match="DEMO_LIVE_FALLBACK_BLOCKED"):
        KrakenFuturesRESTClientV2(base_url=KRAKEN_FUTURES_LIVE_REST_URL)
    with pytest.raises(KrakenEnvironmentConfigError, match="DEMO_LIVE_FALLBACK_BLOCKED"):
        ExchangeKrakenClient(base_url=KRAKEN_FUTURES_LIVE_REST_URL)
    with pytest.raises(KrakenEnvironmentConfigError, match="DEMO_LIVE_FALLBACK_BLOCKED"):
        WsKrakenClient(base_url=KRAKEN_FUTURES_LIVE_REST_URL)


def test_neg4_5_8_mixed_demo_and_live_preinitialized_clients_refused(monkeypatch):
    # Pre-initialized LIVE REST client
    live_rest = KrakenFuturesRESTClientV2(api_key=LIVE_KEY, api_secret=LIVE_SECRET, use_demo=False)

    monkeypatch.setenv("KRAKEN_USE_DEMO", "true")
    monkeypatch.setenv("KRAKEN_DEMO_API_KEY", DEMO_KEY)
    monkeypatch.setenv("KRAKEN_DEMO_API_SECRET", DEMO_SECRET)

    # Neg 4: DEMO mode with pre-initialized LIVE REST client -> refused
    with pytest.raises(KrakenEnvironmentConfigError, match="ENVIRONMENT_MISMATCH"):
        WsKrakenClient(rest_client=live_rest)

    # Neg 5: DEMO mode with LIVE WebSocket URL -> refused
    with pytest.raises(KrakenEnvironmentConfigError, match="DEMO_LIVE_FALLBACK_BLOCKED"):
        WsKrakenClient(ws_url=KRAKEN_FUTURES_LIVE_WS_URL)

    # Neg 8: Explicit validation of mixed REST Demo / WS LIVE object -> refused
    mixed = SimpleNamespace(use_demo=True, base_url=KRAKEN_FUTURES_DEMO_REST_URL, ws_url=KRAKEN_FUTURES_LIVE_WS_URL)
    with pytest.raises(KrakenEnvironmentConfigError, match="DEMO_LIVE_FALLBACK_BLOCKED"):
        validate_kraken_client_environment(mixed, expected_use_demo=True, label="mixed_client")


def test_neg6_demo_refuses_live_key_passed_explicitly(monkeypatch):
    monkeypatch.setenv("KRAKEN_USE_DEMO", "true")
    monkeypatch.setenv("KRAKEN_API_KEY", LIVE_KEY)
    monkeypatch.setenv("KRAKEN_API_SECRET", LIVE_SECRET)

    with pytest.raises(KrakenEnvironmentConfigError, match="DEMO_LIVE_FALLBACK_BLOCKED"):
        KrakenFuturesRESTClientV2(api_key=LIVE_KEY, api_secret=LIVE_SECRET, use_demo=True)


@pytest.mark.asyncio
async def test_neg7_no_fallback_to_live_after_simulated_network_error(monkeypatch):
    monkeypatch.setenv("KRAKEN_USE_DEMO", "true")
    monkeypatch.setenv("KRAKEN_DEMO_API_KEY", DEMO_KEY)
    monkeypatch.setenv("KRAKEN_DEMO_API_SECRET", DEMO_SECRET)
    seen_urls = []

    async def _fail_send(self, request, *args, **kwargs):
        seen_urls.append(str(request.url))
        raise httpx.TimeoutException("simulated demo timeout")

    monkeypatch.setattr(httpx.AsyncClient, "send", _fail_send)
    rest_v2 = KrakenFuturesRESTClientV2()
    with pytest.raises((httpx.TimeoutException, ValueError), match="timeout"):
        await rest_v2.get_positions(force_refresh=True)

    assert len(seen_urls) >= 1
    assert all("demo-futures.kraken.com" in u for u in seen_urls)
    assert all("://futures.kraken.com" not in u for u in seen_urls)

    # Even if base_url is mutated at runtime to LIVE, _request() blocks before sending!
    rest_v2.base_url = KRAKEN_FUTURES_LIVE_REST_URL
    with pytest.raises(KrakenEnvironmentConfigError, match="DEMO_LIVE_FALLBACK_BLOCKED"):
        await rest_v2.get_positions(force_refresh=True)


def test_neg9_ambiguous_use_demo_env_value_refused(monkeypatch):
    monkeypatch.setenv("KRAKEN_USE_DEMO", "maybe")
    with pytest.raises(KrakenEnvironmentConfigError, match="Ambiguous KRAKEN_USE_DEMO"):
        KrakenFuturesRESTClientV2()


def test_neg10_live_mode_never_loads_demo_credentials(monkeypatch):
    monkeypatch.setenv("KRAKEN_USE_DEMO", "false")
    monkeypatch.setenv("KRAKEN_DEMO_API_KEY", DEMO_KEY)
    monkeypatch.setenv("KRAKEN_DEMO_API_SECRET", DEMO_SECRET)
    # LIVE keys intentionally unset

    rest_v2 = KrakenFuturesRESTClientV2()
    exch_c = ExchangeKrakenClient()
    assert rest_v2.use_demo is False
    assert rest_v2.base_url == KRAKEN_FUTURES_LIVE_REST_URL
    assert rest_v2.api_key == ""
    assert rest_v2.api_secret == ""
    assert exch_c.use_demo is False
    assert exch_c.base_url == KRAKEN_FUTURES_LIVE_REST_URL
    assert exch_c.api_key == ""
    assert exch_c.api_secret == ""
