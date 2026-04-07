"""
tests/unit/test_bot_settings.py — Tests for dynamic bot settings
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestBotSettings:
    """Tests for bot_settings configuration system."""

    @pytest.fixture
    def mock_db_config(self):
        """Mock database config structure."""
        return {
            "type": "global",
            "bot_settings": {
                "queue": {
                    "batch_size": 5,
                    "sleep_interval": 1.0,
                    "idle_timeout": 120,
                    "pro_bypass_limit": 5,
                },
                "rate_limits": {
                    "free_per_minute": 10,
                    "pro_per_minute": 50,
                    "window_seconds": 60,
                },
                "webhook": {
                    "batch_processing": True,
                    "max_queue_size": 2000,
                },
            },
        }

    def test_bot_settings_defaults(self):
        """Test that BOT_SETTINGS_DEFAULTS has correct structure."""
        from database.config import BOT_SETTINGS_DEFAULTS

        assert "queue" in BOT_SETTINGS_DEFAULTS
        assert "rate_limits" in BOT_SETTINGS_DEFAULTS
        assert "webhook" in BOT_SETTINGS_DEFAULTS
        assert "cache" in BOT_SETTINGS_DEFAULTS
        assert "performance" in BOT_SETTINGS_DEFAULTS

        assert "batch_size" in BOT_SETTINGS_DEFAULTS["queue"]
        assert "sleep_interval" in BOT_SETTINGS_DEFAULTS["queue"]
        assert "free_per_minute" in BOT_SETTINGS_DEFAULTS["rate_limits"]
        assert "pro_per_minute" in BOT_SETTINGS_DEFAULTS["rate_limits"]

    def test_queue_settings_structure(self):
        """Test queue settings have all required fields."""
        from database.config import BOT_SETTINGS_DEFAULTS

        queue = BOT_SETTINGS_DEFAULTS["queue"]

        assert "batch_size" in queue
        assert "sleep_interval" in queue
        assert "idle_timeout" in queue
        assert "pro_bypass_limit" in queue

        assert isinstance(queue["batch_size"], int)
        assert isinstance(queue["sleep_interval"], (int, float))
        assert isinstance(queue["idle_timeout"], int)
        assert isinstance(queue["pro_bypass_limit"], int)

    def test_rate_limits_structure(self):
        """Test rate limits have all required fields."""
        from database.config import BOT_SETTINGS_DEFAULTS

        rate_limits = BOT_SETTINGS_DEFAULTS["rate_limits"]

        assert "free_per_minute" in rate_limits
        assert "pro_per_minute" in rate_limits
        assert "window_seconds" in rate_limits

        assert isinstance(rate_limits["free_per_minute"], int)
        assert isinstance(rate_limits["pro_per_minute"], int)
        assert isinstance(rate_limits["window_seconds"], int)

        assert rate_limits["free_per_minute"] < rate_limits["pro_per_minute"]

    def test_webhook_settings_structure(self):
        """Test webhook settings have all required fields."""
        from database.config import BOT_SETTINGS_DEFAULTS

        webhook = BOT_SETTINGS_DEFAULTS["webhook"]

        assert "batch_processing" in webhook
        assert "max_queue_size" in webhook

        assert isinstance(webhook["batch_processing"], bool)
        assert isinstance(webhook["max_queue_size"], int)


class TestBotSettingsIntegration:
    """Integration tests for bot settings with mocked database."""

    @pytest.mark.asyncio
    async def test_get_bot_setting_fallback(self):
        """Test fallback to defaults when config is empty."""
        with patch("database.config.get_config", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None

            from database.config import get_bot_setting

            result = await get_bot_setting("queue", "batch_size", 3)

            assert result == 3

    @pytest.mark.asyncio
    async def test_get_bot_setting_from_db(self):
        """Test reading from database config."""
        mock_config = {
            "bot_settings": {
                "queue": {
                    "batch_size": 10,
                }
            }
        }

        with patch("database.config.get_config", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_config

            from database.config import get_bot_setting

            result = await get_bot_setting("queue", "batch_size", 3)

            assert result == 10

    @pytest.mark.asyncio
    async def test_get_all_bot_settings_merges_defaults(self):
        """Test that get_all_bot_settings merges DB values with defaults."""
        partial_config = {
            "bot_settings": {
                "queue": {
                    "batch_size": 10,
                }
            }
        }

        with patch("database.config.get_config", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = partial_config

            from database.config import get_all_bot_settings

            result = await get_all_bot_settings()

            assert result["queue"]["batch_size"] == 10
            assert "sleep_interval" in result["queue"]
            assert "pro_bypass_limit" in result["queue"]
