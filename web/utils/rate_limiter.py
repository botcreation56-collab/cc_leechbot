import os
import time
import logging
from typing import Optional
from datetime import datetime
from fastapi import HTTPException, Request, Response

logger = logging.getLogger(__name__)

_TRUSTED_PROXY_ENV = os.getenv("TRUSTED_PROXIES", "")


def _is_trusted_proxy(ip: str) -> bool:
    if not _TRUSTED_PROXY_ENV:
        return False
    try:
        import ipaddress

        client_addr = ipaddress.ip_address(ip)
        for entry in _TRUSTED_PROXY_ENV.split(","):
            entry = entry.strip()
            if not entry:
                continue
            network = ipaddress.ip_network(entry, strict=False)
            if client_addr in network:
                return True
    except ValueError:
        pass
    return False


class RateLimiter:
    """
    Database-backed rate limiter for multi-worker scalability.

    Falls back to in-memory TTLCache if database is unavailable.
    Uses MongoDB for distributed rate limiting across workers.
    """

    _db_initialized = False
    _use_database = True

    def __init__(self, times: int = 5, seconds: int = 60, key_prefix: str = "rl"):
        self.times = times
        self.seconds = seconds
        self.key_prefix = key_prefix

    async def _get_db(self):
        """Get database instance for rate limit storage."""
        try:
            from database.connection import get_db

            return get_db()
        except (RuntimeError, AttributeError) as e:
            logger.debug(f"Database not available: {e}")
            return None

    async def _check_database_rate_limit(
        self, client_ip: str
    ) -> tuple[bool, Optional[int]]:
        """Check rate limit using database (for multi-worker support)."""
        db = await self._get_db()
        if db is None:
            return await self._check_memory_rate_limit(client_ip)

        key = f"{self.key_prefix}_{client_ip}"
        now = time.time()
        window_start = now - self.seconds

        try:
            result = await db.rate_limits.find_one_and_update(
                {"key": key, "window_start": {"$gte": window_start}},
                {"$inc": {"count": 1}, "$set": {"last_seen": datetime.utcnow()}},
                upsert=True,
                return_document=True,
            )

            count = result.get("count", 1) if result else 1

            if count > self.times:
                ttl = int(self.seconds - (now - window_start))
                return False, max(1, ttl)

            await db.rate_limits.delete_many(
                {"key": key, "window_start": {"$lt": window_start}}
            )

            return True, None

        except (AttributeError, KeyError, TypeError) as e:
            logger.warning(f"Rate limit DB error, falling back to memory: {e}")
            return await self._check_memory_rate_limit(client_ip)

    async def _check_memory_rate_limit(
        self, client_ip: str
    ) -> tuple[bool, Optional[int]]:
        """Fallback in-memory rate limiting."""
        from cachetools import TTLCache

        if not hasattr(RateLimiter, "_memory_cache"):
            RateLimiter._memory_cache = TTLCache(maxsize=10000, ttl=self.seconds)
            RateLimiter._memory_timestamps = {}

        cache = RateLimiter._memory_cache
        timestamps = RateLimiter._memory_timestamps

        now = time.time()
        key = f"{self.key_prefix}_{client_ip}"

        history = cache.get(key, [])
        history = [t for t in history if now - t < self.seconds]

        if len(history) >= self.times:
            remaining = self.seconds - int(now - history[0])
            return False, max(1, remaining)

        history.append(now)
        cache[key] = history
        timestamps[key] = now

        return True, None

    async def __call__(self, request: Request, response: Response):
        connecting_ip = request.client.host if request.client else "unknown"
        if _is_trusted_proxy(connecting_ip):
            xff = request.headers.get("X-Forwarded-For", "")
            client_ip = xff.split(",")[0].strip() if xff else connecting_ip
        else:
            client_ip = connecting_ip

        allowed, retry_after = await self._check_database_rate_limit(client_ip)

        if not allowed:
            logger.warning(f"Rate limit exceeded for IP: {client_ip}")
            response.headers["Retry-After"] = str(retry_after or self.seconds)
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded. Try again in {retry_after or self.seconds} seconds.",
            )


class RequestSizeLimitMiddleware:
    """Middleware to enforce maximum request body size."""

    MAX_BODY_SIZE = int(os.getenv("MAX_REQUEST_BODY_SIZE", "10")) * 1024 * 1024

    async def __call__(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > self.MAX_BODY_SIZE:
                return Response(
                    content="Request body too large",
                    status_code=413,
                    media_type="text/plain",
                )

            body = await request.body()
            if len(body) > self.MAX_BODY_SIZE:
                return Response(
                    content="Request body too large",
                    status_code=413,
                    media_type="text/plain",
                )

        return await call_next(request)
