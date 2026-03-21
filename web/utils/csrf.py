"""
web/utils/csrf.py — CSRF Protection Middleware and Utilities

Provides:
  - CSRF token generation and validation
  - Dependency for protecting mutating endpoints
  - Rate limiting with progressive delays for brute force protection
"""

import secrets
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional

from cachetools import TTLCache
from fastapi import Request, HTTPException, Response

logger = logging.getLogger(__name__)

CSRF_TOKEN_SIZE = 32
SESSION_TIMEOUT_HOURS = 24


class CSRFProtector:
    """CSRF token generator and validator."""

    _token_cache: TTLCache = TTLCache(maxsize=10000, ttl=3600)

    @classmethod
    def generate_token(cls, session_id: str) -> str:
        """Generate a CSRF token for a session."""
        token = secrets.token_urlsafe(CSRF_TOKEN_SIZE)
        cls._token_cache[f"csrf_{session_id}"] = token
        return token

    @classmethod
    def validate_token(cls, session_id: str, token: str) -> bool:
        """Validate a CSRF token."""
        if not token or not session_id:
            return False
        cached_token = cls._token_cache.get(f"csrf_{session_id}")
        if not cached_token:
            return False
        return secrets.compare_digest(cached_token, token)

    @classmethod
    def get_token(cls, session_id: str) -> Optional[str]:
        """Get existing CSRF token for a session."""
        return cls._token_cache.get(f"csrf_{session_id}")


class BruteForceProtection:
    """Progressive brute force protection with exponential backoff."""

    _attempts: TTLCache = TTLCache(maxsize=10000, ttl=3600)
    _lockouts: TTLCache = TTLCache(maxsize=1000, ttl=3600)
    _progressive_delays: TTLCache = TTLCache(maxsize=10000, ttl=3600)

    MAX_ATTEMPTS = 5
    LOCKOUT_DURATION = 900  # 15 minutes
    PROGRESSIVE_DELAY_BASE = 2  # seconds

    @classmethod
    def check(cls, identifier: str) -> tuple[bool, Optional[int], Optional[str]]:
        """
        Check if identifier is allowed.
        Returns (allowed, delay_seconds, lockout_reason).
        """
        lockout_until = cls._lockouts.get(f"lockout_{identifier}")
        if lockout_until:
            if datetime.utcnow() < lockout_until:
                remaining = int((lockout_until - datetime.utcnow()).total_seconds())
                return False, None, f"Account locked. Try again in {remaining} seconds."

        attempts = cls._attempts.get(f"attempts_{identifier}", 0)
        if attempts >= cls.MAX_ATTEMPTS:
            lockout_until = datetime.utcnow() + timedelta(seconds=cls.LOCKOUT_DURATION)
            cls._lockouts[f"lockout_{identifier}"] = lockout_until
            cls._attempts.pop(f"attempts_{identifier}", None)
            return False, None, "Too many failed attempts. Locked for 15 minutes."

        delay_key = f"delay_{identifier}"
        last_attempt = cls._progressive_delays.get(delay_key)
        if last_attempt:
            elapsed = (datetime.utcnow() - last_attempt).total_seconds()
            expected_delay = cls.PROGRESSIVE_DELAY_BASE**attempts
            if elapsed < expected_delay:
                wait_time = int(expected_delay - elapsed)
                return False, wait_time, f"Too fast. Wait {wait_time} seconds."

        return True, None, None

    @classmethod
    def record_failure(cls, identifier: str) -> None:
        """Record a failed attempt."""
        current = cls._attempts.get(f"attempts_{identifier}", 0)
        cls._attempts[f"attempts_{identifier}"] = current + 1
        cls._progressive_delays[f"delay_{identifier}"] = datetime.utcnow()
        logger.warning(
            f"🔐 Failed attempt {current + 1}/{cls.MAX_ATTEMPTS} for: {identifier}"
        )

    @classmethod
    def record_success(cls, identifier: str) -> None:
        """Clear all failures for identifier."""
        cls._attempts.pop(f"attempts_{identifier}", None)
        cls._lockouts.pop(f"lockout_{identifier}", None)
        cls._progressive_delays.pop(f"delay_{identifier}", None)


async def csrf_token_dependency(request: Request) -> str:
    """
    FastAPI dependency that validates CSRF token for mutating requests.
    Use on POST, PUT, DELETE endpoints.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return ""

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        session_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
    else:
        session_hash = request.client.host if request.client else "unknown"

    csrf_token = request.headers.get("X-CSRF-Token", "")

    if not csrf_token:
        logger.warning(f"🚫 Missing CSRF token for session: {session_hash[:8]}")
        raise HTTPException(
            status_code=403, detail="Missing CSRF token. Include X-CSRF-Token header."
        )

    if not CSRFProtector.validate_token(session_hash, csrf_token):
        logger.warning(f"🚫 Invalid CSRF token for session: {session_hash[:8]}")
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")

    return session_hash


def generate_csrf_token(session_token: str) -> str:
    """Generate a CSRF token for a session token."""
    session_hash = hashlib.sha256(session_token.encode()).hexdigest()[:16]
    return CSRFProtector.generate_token(session_hash)


def validate_csrf_token(session_token: str, csrf_token: str) -> bool:
    """Validate a CSRF token against a session token."""
    session_hash = hashlib.sha256(session_token.encode()).hexdigest()[:16]
    return CSRFProtector.validate_token(session_hash, csrf_token)
