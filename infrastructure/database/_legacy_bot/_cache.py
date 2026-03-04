"""
bot/database/_cache.py — Shared TTLCache state for the database layer.

Both _users.py and _config.py import from here so they share the same
cache objects. The cache bridge (infrastructure.database.cache_bridge)
calls _bust_user_cache / _bust_config_cache to keep both layers in sync.
"""

import asyncio
from typing import Optional

from cachetools import TTLCache

# In-memory caches (single-process, single-worker safe)
# Redis is recommended for horizontal scaling.
_user_cache: TTLCache = TTLCache(maxsize=10_000, ttl=60)
_config_cache: TTLCache = TTLCache(maxsize=10, ttl=30)

_cache_lock: Optional[asyncio.Lock] = None


def _get_cache_lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def _bust_config_cache() -> None:
    """Invalidate config cache immediately (called after any config write)."""
    _config_cache.clear()
    # Also bust infrastructure ConfigRepository instances
    try:
        from infrastructure.database.cache_bridge import _bust_all_config_caches
        _bust_all_config_caches()
    except Exception:
        pass


def _bust_user_cache(user_id: int) -> None:
    """Invalidate a specific user's cache entry in BOTH layers (called after update_user)."""
    _user_cache.pop(user_id, None)
    # Also bust infrastructure UserRepository instances
    try:
        from infrastructure.database.cache_bridge import _bust_repo_caches
        _bust_repo_caches(user_id)
    except Exception:
        pass
