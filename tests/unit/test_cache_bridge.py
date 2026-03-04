"""
tests/unit/test_cache_bridge.py — Verifies the dual-brain cache bridge eliminates stale reads.

Scenario being tested:
  Admin updates user.plan = "premium" via web route (infrastructure UserRepository).
  Next call to bot.database.get_user() must NOT return the stale "free" cached value.
"""

import pytest
from unittest.mock import MagicMock
from cachetools import TTLCache

from infrastructure.database.cache_bridge import (
    bust_user_cache,
    bust_config_cache,
    register_user_repo,
    register_config_repo,
    _bust_repo_caches,
    _bust_all_config_caches,
)


class TestCacheBridgeUserCache:

    def test_bust_user_cache_clears_legacy_cache(self, monkeypatch):
        """bust_user_cache() from the bridge must clear bot.database._user_cache."""
        import bot.database as legacy_db

        # Inject a stale entry into the legacy cache
        legacy_db._user_cache[999] = {"telegram_id": 999, "plan": "free"}
        assert 999 in legacy_db._user_cache

        # Call the bridge bust
        bust_user_cache(999)

        # The legacy cache entry must be gone
        assert 999 not in legacy_db._user_cache

    def test_bust_user_cache_clears_repo_instance_cache(self):
        """bust_user_cache() must also clear registered UserRepository instance caches."""
        # Simulate a UserRepository instance with its own TTLCache
        mock_repo = MagicMock()
        mock_repo._cache = TTLCache(maxsize=10, ttl=120)
        mock_repo._cache[42] = {"telegram_id": 42, "plan": "free"}

        register_user_repo(mock_repo)
        _bust_repo_caches(42)

        assert 42 not in mock_repo._cache

    def test_legacy_bust_propagates_to_repo_instance(self):
        """Calling _bust_user_cache() in bot.database MUST also bust infra repo caches."""
        import bot.database as legacy_db

        mock_repo = MagicMock()
        mock_repo._cache = TTLCache(maxsize=10, ttl=120)
        mock_repo._cache[77] = {"telegram_id": 77, "plan": "pro"}

        register_user_repo(mock_repo)

        # Simulate a write via the legacy layer
        legacy_db._bust_user_cache(77)

        # Infrastructure cache should also be cleared
        assert 77 not in mock_repo._cache


class TestCacheBridgeConfigCache:

    def test_bust_config_clears_legacy_config_cache(self, monkeypatch):
        """bust_config_cache() must clear bot.database._config_cache."""
        import bot.database as legacy_db

        legacy_db._config_cache["something"] = {"dump_channel": -1001234567890}
        bust_config_cache()

        assert len(legacy_db._config_cache) == 0

    def test_legacy_config_bust_propagates_to_repo(self):
        """_bust_config_cache() in bot.database must bust infra ConfigRepository caches."""
        import bot.database as legacy_db

        mock_config_repo = MagicMock()
        mock_config_repo._cache = TTLCache(maxsize=4, ttl=60)
        mock_config_repo._cache["main"] = {"max_file_size": 1024}

        register_config_repo(mock_config_repo)

        # Write through legacy layer
        legacy_db._bust_config_cache()

        # Must be gone in the registered config repo too
        assert "main" not in mock_config_repo._cache
