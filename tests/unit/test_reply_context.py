"""
tests/unit/test_reply_context.py — Tests for ReplyContextManager
"""

import asyncio
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch


class TestReplyContextManager:
    """Tests for ReplyContextManager functionality."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before each test."""
        from core.reply_context import ReplyContextManager

        ReplyContextManager._instance = None
        yield
        ReplyContextManager._instance = None

    @pytest.fixture
    def manager(self):
        """Create a fresh ReplyContextManager instance."""
        from core.reply_context import ReplyContextManager

        return ReplyContextManager()

    def test_singleton_pattern(self):
        """Test that ReplyContextManager is a singleton."""
        from core.reply_context import ReplyContextManager

        m1 = ReplyContextManager()
        m2 = ReplyContextManager()

        assert m1 is m2

    def test_set_context(self, manager):
        """Test setting a reply context."""
        user_id = 12345

        reply_to_id = manager.set_context(
            user_id=user_id, context_type="text", context_key="test_key", timeout=60
        )

        ctx = manager.get_context(user_id)

        assert ctx is not None
        assert ctx.context_type.value == "text"
        assert ctx.context_key == "test_key"
        assert ctx.expires_at > time.time()

    def test_link_message(self, manager):
        """Test linking a message to a context."""
        user_id = 12345
        message_id = 999

        manager.set_context(user_id, "text", "test")
        manager.link_message(user_id, message_id)

        assert manager.get_reply_to_id(user_id) == message_id

    def test_clear_context(self, manager):
        """Test clearing a context."""
        user_id = 12345

        manager.set_context(user_id, "text", "test")
        result = manager.clear_context(user_id)

        assert result is True
        assert manager.get_context(user_id) is None

    def test_expired_context(self, manager):
        """Test that expired contexts are rejected."""
        user_id = 12345

        manager.set_context(user_id, "text", "test", timeout=-1)

        is_valid, error = manager.validate_reply(user_id)

        assert is_valid is False
        assert "expired" in error.lower()

    def test_is_awaiting_reply(self, manager):
        """Test checking if user is awaiting reply."""
        user_id = 12345

        assert manager.is_awaiting_reply(user_id) is False

        manager.set_context(user_id, "text", "test")

        assert manager.is_awaiting_reply(user_id) is True

    def test_get_awaiting_info(self, manager):
        """Test getting info about awaiting reply."""
        user_id = 12345

        manager.set_context(user_id, "rename", "test_rename", timeout=120)

        info = manager.get_awaiting_info(user_id)

        assert info is not None
        assert info["type"] == "rename"
        assert info["key"] == "test_rename"
        assert info["expires_in"] > 0


class TestReplyContextValidation:
    """Tests for reply validation logic."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before each test."""
        from core.reply_context import ReplyContextManager

        ReplyContextManager._instance = None
        yield
        ReplyContextManager._instance = None

    @pytest.fixture
    def manager(self):
        """Create a fresh ReplyContextManager instance."""
        from core.reply_context import ReplyContextManager

        return ReplyContextManager()

    def test_validate_number_type(self, manager):
        """Test that number validation works."""
        user_id = 12345
        manager.set_context(user_id, "number", "test", timeout=60)

        is_valid, error = manager.validate_reply(user_id, text="123")
        assert is_valid is True

        is_valid, error = manager.validate_reply(user_id, text="abc")
        assert is_valid is False
        assert "number" in error.lower()

    def test_validate_reply_to_message_id(self, manager):
        """Test validation of reply_to_message_id."""
        user_id = 12345
        expected_msg_id = 100

        manager.set_context(user_id, "text", "test", timeout=60)
        manager.link_message(user_id, expected_msg_id)

        is_valid, _ = manager.validate_reply(user_id, message_id=expected_msg_id)
        assert is_valid is True

        is_valid, _ = manager.manager.validate_reply(user_id, message_id=999)
        assert is_valid is False
