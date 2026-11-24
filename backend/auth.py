"""
API Key authentication middleware for mutable endpoints.

Protects POST / PUT / DELETE / PATCH endpoints with a bearer token
read dynamically from the DASHBOARD_API_KEY environment variable.

GET / OPTIONS / WebSocket upgrades pass through without auth.
Health endpoints (/health, /api/health) are always public.
"""
import os
import hmac
import logging
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Optional explicit override (None = read DASHBOARD_API_KEY dynamically from environment)
_API_KEY: Optional[str] = None


def get_dashboard_api_key() -> str:
    """Resolve effective dashboard API key dynamically without requiring module reload."""
    if _API_KEY is not None:
        return _API_KEY
    return os.getenv("DASHBOARD_API_KEY", "")


# Endpoints that are always public (no auth required even for POST)
_PUBLIC_PATHS = frozenset({
    "/health",
    "/api/health",
    "/docs",
    "/openapi.json",
    "/redoc",
})

# Methods that don't require auth
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject mutable requests when DASHBOARD_API_KEY is set and header is missing/wrong."""

    async def dispatch(self, request: Request, call_next):
        api_key = get_dashboard_api_key()

        # No key configured -> skip auth (dev mode)
        if not api_key:
            return await call_next(request)

        # Safe methods and public paths pass through
        if request.method in _SAFE_METHODS:
            return await call_next(request)

        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        # WebSocket upgrade — auth checked at WS level, not here
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        # Extract token from Authorization header or X-API-Key header
        token = ""
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            token = request.headers.get("x-api-key", "")

        if not token or not hmac.compare_digest(token, api_key):
            logger.warning(
                "[AUTH] Rejected %s %s from %s — invalid or missing API key",
                request.method,
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": "Valid API key required for mutable operations"},
            )

        return await call_next(request)
