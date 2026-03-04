import time
from fastapi import HTTPException, Request, Response
from cachetools import TTLCache
import logging

logger = logging.getLogger(__name__)

class RateLimiter:
    """
    In-memory rate limiter using TTLCache to prevent memory leaks.
    Safely resolves X-Forwarded-For to handle proxy IPs.
    """
    def __init__(self, times: int = 5, seconds: int = 60):
        self.times = times
        self.seconds = seconds
        # maxsize 10,000 IPs, TTL automatically purges old entries
        self._requests = TTLCache(maxsize=10000, ttl=seconds)

    async def __call__(self, request: Request, response: Response):
        # Resolve real IP behind proxies (Render/Cloudflare)
        client_ip = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
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
