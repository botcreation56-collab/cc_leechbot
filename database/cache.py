"""
database/cache.py — Cache management for users and config.

Provides TTLCache instances with thread-safe access for user and config caching.
The cache is shared across all database operations for consistency.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from cachetools import TTLCache

logger = logging.getLogger("filebot.db.cache")

_user_cache: TTLCache = TTLCache(maxsize=10000, ttl=120)
_config_cache: TTLCache = TTLCache(maxsize=10, ttl=60)
_cache_lock = asyncio.Lock()


def _get_cache_lock() -> asyncio.Lock:
    """Get the cache lock for thread-safe cache access."""
    return _cache_lock


def _bust_user_cache(user_id: int) -> None:
    """Invalidate a specific user's cache entry."""
    _user_cache.pop(user_id, None)


def _bust_config_cache() -> None:
    """Invalidate the config cache."""
    _config_cache.pop("global", None)
    _config_cache.pop("main", None)


def bust_user_cache(user_id: int) -> None:
    """Public: Invalidate user cache in all layers."""
    _bust_user_cache(user_id)


def bust_config_cache() -> None:
    """Public: Invalidate config cache in all layers."""
    _bust_config_cache()
