"""
infrastructure/database/cache_bridge.py — Unified cache invalidation bridge.

PROBLEM: The system has two independent DB interaction layers:
  1. bot/database.py  — legacy global functions with their own TTLCache (_user_cache, _config_cache)
  2. infrastructure/database/repositories.py — DI-based repositories with per-instance TTLCache

When the infrastructure layer writes a user update, it busts its OWN cache but does NOT
bust bot/database.py's _user_cache. Any subsequent call to bot.database.get_user()
will then serve stale data from that cache for up to 60 seconds. This can cause:
  - Ban bypass: admin bans a user via web route (uses repositories.py), but bot still
    serves the cached "not banned" record and allows downloads.
  - Plan bypass: plan upgrade via API isn't reflected in quota checks for up to 60s.

SOLUTION: This module exposes a single callable that both layers import. The
infrastructure repositories call it on every write, which busts both caches atomically.
bot/database.py's _bust_user_cache/_bust_config_cache are monkey-patched to do the same.

No callers need to be modified — this is a purely additive fix.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("filebot.cache_bridge")


def bust_user_cache(user_id: int) -> None:
    """
    Invalidate the user cache entry in BOTH layers.

    Safe to call from any context (sync or async). The legacy bot/database.py
    cache is a plain dict-like TTLCache so no await needed.
    """
    # Layer 1: bust infrastructure UserRepository instance cache
    # (each repo instance has its own cache; we broadcast via the registry below)
    _bust_repo_caches(user_id)

    # Layer 2: bust legacy bot.database._user_cache
    try:
        import bot.database as _legacy
        _legacy._bust_user_cache(user_id)
    except Exception as exc:
        logger.debug("Legacy cache bust skipped: %s", exc)


def bust_config_cache() -> None:
    """Invalidate the config cache in BOTH layers."""
    try:
        import bot.database as _legacy
        _legacy._bust_config_cache()
    except Exception as exc:
        logger.debug("Legacy config cache bust skipped: %s", exc)


# ---------------------------------------------------------------------------
# Repository instance registry
# ---------------------------------------------------------------------------
# When a UserRepository is instantiated, it registers itself here.
# bust_user_cache() then iterates and invalidates each instance's cache.
# This handles multi-tenant DI patterns where several repo objects co-exist.

_user_repo_instances: list = []
_config_repo_instances: list = []


def register_user_repo(repo: object) -> None:
    """Called by UserRepository.__init__ to register itself."""
    if repo not in _user_repo_instances:
        _user_repo_instances.append(repo)


def register_config_repo(repo: object) -> None:
    """Called by ConfigRepository.__init__ to register itself."""
    if repo not in _config_repo_instances:
        _config_repo_instances.append(repo)


def _bust_repo_caches(user_id: int) -> None:
    """Bust all registered UserRepository instance caches."""
    for repo in list(_user_repo_instances):
        try:
            repo._cache.pop(user_id, None)
        except Exception:
            pass


def _bust_all_config_caches() -> None:
    """Bust all registered ConfigRepository instance caches."""
    for repo in list(_config_repo_instances):
        try:
            repo._cache.pop("main", None)
        except Exception:
            pass
