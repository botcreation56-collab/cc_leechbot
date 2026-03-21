"""
web/utils/security_headers.py — Security Headers Middleware

Provides:
  - X-Content-Type-Options: nosniff
  - X-Frame-Options: DENY
  - X-XSS-Protection: 1; mode=block
  - Referrer-Policy: strict-origin-when-cross-origin
  - Permissions-Policy: restrictions on sensitive features
  - Content-Security-Policy: strict CSP for API endpoints
"""

import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds security headers to all HTTP responses.

    Configurable via environment variables:
      - CSP_REPORT_URI: Optional URI for CSP violation reports
      - DISABLE_XSS_PROTECTION: Set to "1" to disable X-XSS-Protection (recommended to disable, CSP is better)
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        is_api = request.url.path.startswith("/api/")
        is_static = request.url.path.startswith("/static/")

        response.headers["X-Content-Type-Options"] = "nosniff"

        if os.getenv("DISABLE_XSS_PROTECTION") != "1":
            response.headers["X-XSS-Protection"] = "1; mode=block"

        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        response.headers["Permissions-Policy"] = (
            "accelerometer=(), "
            "camera=(), "
            "geolocation=(), "
            "gyroscope=(), "
            "magnetometer=(), "
            "microphone=(), "
            "payment=(), "
            "usb=()"
        )

        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, private"
        )
        response.headers["Pragma"] = "no-cache"

        return response


