"""
Security tests for Phase 3 production hardening.

Validates:
- CORS no longer allows wildcard
- ApiKeyMiddleware blocks unauthenticated POST
- Input validation rejects malformed payloads
"""
import os
import pytest

pytestmark = pytest.mark.security


# ─────────────────────────────────────────────────────────────
#  CORS restriction tests
# ─────────────────────────────────────────────────────────────

class TestCORSRestriction:
    """Verify that allow_origins is NOT ['*'] in any FastAPI app."""

    def _get_cors_origins(self, app):
        for mw in getattr(app, "user_middleware", []):
            if "CORSMiddleware" in str(mw.cls):
                return mw.kwargs.get("allow_origins", [])
        return None

    def test_api_smc_engine_cors_not_wildcard(self):
        from smc_futures.api_smc_engine import app as prod_app, create_api_app
        for target_app in (prod_app, create_api_app(enable_startup_hooks=False)):
            origins = self._get_cors_origins(target_app)
            assert origins is not None, "CORSMiddleware not found"
            assert origins != ["*"], "CORS still allows wildcard"
            assert all(o.startswith("http") for o in origins), f"Invalid origins: {origins}"

    def test_smarttrade_api_cors_not_wildcard(self):
        try:
            from backend.smarttrade_api import app
        except ImportError:
            pytest.skip("smarttrade_api not importable (missing bitget_v2_mix_client)")
        origins = self._get_cors_origins(app)
        assert origins is not None, "CORSMiddleware not found"
        assert origins != ["*"], "CORS still allows wildcard"

    def test_orchestrator_cors_not_wildcard(self):
        from smc_futures.orchestrator import app as legacy_prod_app, create_legacy_app
        for target_app in (legacy_prod_app, create_legacy_app(enable_startup_hooks=False)):
            origins = self._get_cors_origins(target_app)
            assert origins is not None, "CORSMiddleware not found"
            assert origins != ["*"], "CORS still allows wildcard"


# ─────────────────────────────────────────────────────────────
#  Auth middleware tests
# ─────────────────────────────────────────────────────────────

class TestApiKeyMiddleware:
    """Verify ApiKeyMiddleware blocks unauthenticated mutable requests."""

    def test_middleware_blocks_post_when_key_set(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_API_KEY", "test-secret-key-12345")

        from starlette.testclient import TestClient
        from fastapi import FastAPI

        test_app = FastAPI()

        from backend.auth import ApiKeyMiddleware
        test_app.add_middleware(ApiKeyMiddleware)

        @test_app.post("/test")
        async def test_endpoint():
            return {"ok": True}

        @test_app.get("/health")
        async def health():
            return {"ok": True}

        client = TestClient(test_app)

        # POST without key → 401
        resp = client.post("/test", json={})
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"

        # POST with wrong key → 401
        resp = client.post("/test", json={}, headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

        # POST with correct key → 200
        resp = client.post("/test", json={}, headers={"X-API-Key": "test-secret-key-12345"})
        assert resp.status_code == 200

        # POST with Bearer auth → 200
        resp = client.post("/test", json={}, headers={"Authorization": "Bearer test-secret-key-12345"})
        assert resp.status_code == 200

        # GET always passes
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_middleware_passthrough_when_no_key(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_API_KEY", "")

        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from backend.auth import ApiKeyMiddleware

        test_app = FastAPI()
        test_app.add_middleware(ApiKeyMiddleware)

        @test_app.post("/test")
        async def test_endpoint():
            return {"ok": True}

        client = TestClient(test_app)
        resp = client.post("/test", json={})
        assert resp.status_code == 200, "Should pass through when no API key configured"


# ─────────────────────────────────────────────────────────────
#  Input validation tests
# ─────────────────────────────────────────────────────────────

class TestInputValidation:
    """Verify Pydantic models reject malformed input."""

    def test_order_request_rejects_invalid_symbol(self):
        from smc_futures.schemas.orders import OrderRequest
        with pytest.raises(Exception):
            OrderRequest(symbol="'; DROP TABLE--", side="buy")

    def test_order_request_rejects_invalid_side(self):
        from smc_futures.schemas.orders import OrderRequest
        with pytest.raises(Exception):
            OrderRequest(symbol="BTCUSDT", side="invalid_side")

    def test_order_request_rejects_negative_quantity(self):
        from smc_futures.schemas.orders import OrderRequest
        with pytest.raises(Exception):
            OrderRequest(symbol="BTCUSDT", side="buy", quantity=-1.0)

    def test_order_request_accepts_valid(self):
        from smc_futures.schemas.orders import OrderRequest
        req = OrderRequest(symbol="BTCUSDT", side="buy", quantity=0.001)
        assert req.symbol == "BTCUSDT"
        assert req.side == "buy"

    def test_dry_run_rejects_zero_entry_price(self):
        from smc_futures.schemas.orders import DryRunOrderRequest
        with pytest.raises(Exception):
            DryRunOrderRequest(
                symbol="BTCUSDT", side="buy",
                entry_price=0, stop_loss=100, take_profit=200,
                risk_pct=1.0,
            )

    def test_dry_run_rejects_excessive_leverage(self):
        from smc_futures.schemas.orders import DryRunOrderRequest
        with pytest.raises(Exception):
            DryRunOrderRequest(
                symbol="BTCUSDT", side="buy",
                entry_price=100, stop_loss=90, take_profit=120,
                risk_pct=1.0, leverage=999,
            )

    def test_force_order_rejects_invalid_mode(self):
        from smc_futures.schemas.orders import ForceOrderRequest
        with pytest.raises(Exception):
            ForceOrderRequest(
                symbol="BTCUSDT", side="buy", size=0.001, mode="DELETE_ALL"
            )

    def test_smarttrade_order_rejects_sql_injection(self):
        try:
            from backend.smarttrade_api import OrderRequest
        except ImportError:
            pytest.skip("smarttrade_api not importable (missing bitget_v2_mix_client)")
        with pytest.raises(Exception):
            OrderRequest(symbol="' OR 1=1 --", side="buy", size="0.001")
