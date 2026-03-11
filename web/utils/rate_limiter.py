import os
import time
from fastapi import HTTPException, Request, Response
from cachetools import TTLCache
import logging

logger = logging.getLogger(__name__)

# Parse trusted proxy CIDRs/IPs from env (comma-separated).
# Example: TRUSTED_PROXIES="10.0.0.0/8,172.16.0.0/12,127.0.0.1"
# If empty, X-Forwarded-For is NEVER trusted and client.host is always used.
_TRUSTED_PROXY_ENV = os.getenv("TRUSTED_PROXIES", "")


def _is_trusted_proxy(ip: str) -> bool:
    """Return True if *ip* is in TRUSTED_PROXIES."""
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
    In-memory rate limiter using TTLCache to prevent memory leaks.

    X-Forwarded-For is only trusted when the connecting IP is listed in the
    TRUSTED_PROXIES environment variable (comma-separated CIDRs / IPs).
    Otherwise request.client.host is used directly, preventing header spoofing.
    """
    def __init__(self, times: int = 5, seconds: int = 60):
        self.times = times
        self.seconds = seconds
        # maxsize 10,000 IPs, TTL automatically purges old entries
        self._requests = TTLCache(maxsize=10000, ttl=seconds)

    async def __call__(self, request: Request, response: Response):
        # Resolve real client IP — only unpack X-Forwarded-For from trusted proxies.
        connecting_ip = request.client.host if request.client else "unknown"
        if _is_trusted_proxy(connecting_ip):
            xff = request.headers.get("X-Forwarded-For", "")
            client_ip = xff.split(",")[0].strip() if xff else connecting_ip
        else:
            client_ip = connecting_ip

        now = time.time()

        # Get existing history or initialize empty list
        history = self._requests.get(client_ip, [])

        # Keep only requests within the time window
        history = [t for t in history if now - t < self.seconds]

        if len(history) >= self.times:
            logger.warning(f"🚫 Rate limit exceeded for IP: {client_ip}")
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded. Try again in {self.seconds} seconds."
            )

        history.append(now)
        self._requests[client_ip] = history
