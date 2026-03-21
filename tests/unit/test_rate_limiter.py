"""
tests/unit/test_rate_limiter.py — Unit tests for web/utils/rate_limiter.py

Covers:
  - RateLimiter: in-memory fallback (database mocked)
  - _is_trusted_proxy: IP validation
  - RequestSizeLimitMiddleware: body size enforcement
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

from web.utils.rate_limiter import (
    RateLimiter,
    RequestSizeLimitMiddleware,
    _is_trusted_proxy,
)


class TestIsTrustedProxy:
    """Tests for trusted proxy detection."""

    def test_empty_env_returns_false(self):
        with patch("web.utils.rate_limiter._TRUSTED_PROXY_ENV", ""):
            assert _is_trusted_proxy("192.168.1.1") is False

    def test_matching_cidr(self):
        with patch("web.utils.rate_limiter._TRUSTED_PROXY_ENV", "10.0.0.0/8"):
            assert _is_trusted_proxy("10.1.2.3") is True

    def test_non_matching_cidr(self):
        with patch("web.utils.rate_limiter._TRUSTED_PROXY_ENV", "10.0.0.0/8"):
            assert _is_trusted_proxy("192.168.1.1") is False

    def test_exact_ip_match(self):
        with patch("web.utils.rate_limiter._TRUSTED_PROXY_ENV", "127.0.0.1"):
            assert _is_trusted_proxy("127.0.0.1") is True

    def test_multiple_cidrs(self):
        with patch(
            "web.utils.rate_limiter._TRUSTED_PROXY_ENV", "10.0.0.0/8,172.16.0.0/12"
        ):
            assert _is_trusted_proxy("10.5.5.5") is True
            assert _is_trusted_proxy("172.20.1.1") is True
            assert _is_trusted_proxy("192.168.1.1") is False


class TestRateLimiter:
    """Tests for the RateLimiter class."""

    def setup_method(self):
        RateLimiter._memory_cache = None
        RateLimiter._memory_timestamps = {}

    def test_allows_within_limit(self):
        limiter = RateLimiter(times=5, seconds=60)
        mock_request = MagicMock()
        mock_request.client.host = "192.168.1.100"

        async def run():
            return await limiter._check_memory_rate_limit("192.168.1.100")

        allowed, retry = asyncio.get_event_loop().run_until_complete(run())
        assert allowed is True
        assert retry is None

    def test_blocks_after_limit(self):
        limiter = RateLimiter(times=2, seconds=60)
        ip = "192.168.1.101"

        async def run():
            await limiter._check_memory_rate_limit(ip)
            await limiter._check_memory_rate_limit(ip)
            return await limiter._check_memory_rate_limit(ip)

        allowed, retry = asyncio.get_event_loop().run_until_complete(run())
        assert allowed is False
        assert retry is not None
        assert retry > 0

    def test_key_prefix_isolation(self):
        limiter1 = RateLimiter(times=1, seconds=60, key_prefix="api")
        limiter2 = RateLimiter(times=1, seconds=60, key_prefix="web")
        ip = "192.168.1.102"

        async def run():
            allowed1, _ = await limiter1._check_memory_rate_limit(ip)
            allowed2, _ = await limiter2._check_memory_rate_limit(ip)
            return allowed1, allowed2

        allowed1, allowed2 = asyncio.get_event_loop().run_until_complete(run())
        assert allowed1 is False  # api rate limited
        assert allowed2 is True  # web is separate

    @pytest.mark.asyncio
    async def test_database_fallback_on_error(self):
        limiter = RateLimiter(times=5, seconds=60)
        mock_db = MagicMock()
        mock_db.rate_limits.find_one_and_update.side_effect = AttributeError("DB error")

        with patch.object(limiter, "_get_db", return_value=mock_db):
            allowed, retry = await limiter._check_database_rate_limit("192.168.1.103")
            assert allowed is True
            assert retry is None

    @pytest.mark.asyncio
    async def test_get_db_returns_none_when_unavailable(self):
        limiter = RateLimiter(times=5, seconds=60)

        with patch(
            "web.utils.rate_limiter.get_db", side_effect=RuntimeError("Not connected")
        ):
            db = await limiter._get_db()
            assert db is None


class TestRequestSizeLimitMiddleware:
    """Tests for request body size enforcement."""

    def test_allows_small_request(self):
        middleware = RequestSizeLimitMiddleware()
        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.headers = {"content-length": "100"}
        mock_request.body = AsyncMock(return_value=b"x" * 100)

        async def call_next(req):
            return MagicMock()

        result = asyncio.get_event_loop().run_until_complete(
            middleware(mock_request, call_next)
        )
        assert result is not None

    def test_rejects_large_content_length(self):
        middleware = RequestSizeLimitMiddleware()
        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.headers = {"content-length": str(100 * 1024 * 1024)}

        async def call_next(req):
            return MagicMock()

        result = asyncio.get_event_loop().run_until_complete(
            middleware(mock_request, call_next)
        )
        assert result.status_code == 413

    def test_get_requests_passthrough(self):
        middleware = RequestSizeLimitMiddleware()
        mock_request = MagicMock()
        mock_request.method = "GET"

        async def call_next(req):
            return MagicMock()

        result = asyncio.get_event_loop().run_until_complete(
            middleware(mock_request, call_next)
        )
        call_next.assert_called_once()
