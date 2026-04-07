"""
reply_context.py — Auto-trigger reply system for Telegram

When the bot sends a message that requires user response, this system:
1. Tracks the message context (what response is expected)
2. Sets reply_to_message_id so Telegram threads the reply
3. Validates incoming replies match expected context
4. Guides user to reply correctly

Usage:
    reply_ctx = ReplyContext.get_instance()
    reply_ctx.set_context(user_id, "rename", timeout=120)

    msg = await bot.send_message(
        chat_id=user_id,
        text="Enter new filename:",
        reply_to_message_id=reply_ctx.get_reply_to_id(user_id)
    )
    reply_ctx.link_message(user_id, msg.message_id)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("filebot.reply_context")


class ReplyContextType(Enum):
    """Types of responses the bot expects."""

    TEXT = "text"  # Any text input
    RENAME = "rename"  # Filename input
    METADATA = "metadata"  # Metadata field input
    NUMBER = "number"  # Numeric input
    URL = "url"  # URL input
    YES_NO = "yes_no"  # Confirmation
    FILE = "file"  # File upload
    PHOTO = "photo"  # Photo upload
    CUSTOM = "custom"  # Custom callback


@dataclass
class ReplyContext:
    """Tracks a pending reply context for a user."""

    context_type: ReplyContextType
    context_key: str  # e.g., "wiz_rename", "meta_title"
    message_id: Optional[int]  # The bot message user should reply to
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0  # Auto-expire timeout
    data: Dict[str, Any] = field(default_factory=dict)
    callback: Optional[Callable] = None  # Optional validation callback


class ReplyContextManager:
    """
    Manages reply contexts for all users.

    When bot sends a message expecting user reply:
    1. Set context with set_context() - tracks what response is expected
    2. Send message with reply_to_message_id from get_reply_to_id()
    3. Link the sent message with link_message()
    4. When user replies, validate with validate_reply()

    Telegram's reply_to_message_id creates a "thread" - user taps on the
    bot's message and their reply is automatically linked.
    """

    _instance: Optional["ReplyContextManager"] = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized") and self._initialized:
            return

        # {user_id: ReplyContext}
        self._contexts: Dict[int, ReplyContext] = {}
        # {user_id: message_id} - links user to their pending message
        self._user_messages: Dict[int, int] = {}
        # Lock for thread safety
        self._lock = asyncio.Lock()

        self._initialized = True
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def get_instance(cls) -> "ReplyContextManager":
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

    async def start(self):
        """Start the cleanup background task."""
        if self._running:
            return
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("✅ ReplyContextManager started")

    async def stop(self):
        """Stop the cleanup task."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 ReplyContextManager stopped")

    async def _cleanup_loop(self):
        """Periodically clean up expired contexts."""
        while self._running:
            try:
                await asyncio.sleep(30)
                await self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ReplyContext cleanup error: {e}")

    async def _cleanup_expired(self):
        """Remove expired contexts."""
        now = time.time()
        expired_users = []

        async with self._lock:
            for user_id, ctx in self._contexts.items():
                if now > ctx.expires_at and ctx.expires_at > 0:
                    expired_users.append(user_id)

            for user_id in expired_users:
                del self._contexts[user_id]
                self._user_messages.pop(user_id, None)

        if expired_users:
            logger.debug(f"Cleaned up {len(expired_users)} expired reply contexts")

    def set_context(
        self,
        user_id: int,
        context_type: ReplyContextType | str,
        context_key: str,
        timeout: int = 120,
        data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Set a pending reply context for a user.

        Args:
            user_id: Telegram user ID
            context_type: Type of response expected (ReplyContextType or string)
            context_key: Unique key for this context (e.g., "wiz_rename", "meta_title")
            timeout: Seconds until context expires (default 120)
            data: Optional additional data

        Returns:
            Current reply_to_message_id if exists, else 0
        """
        if isinstance(context_type, str):
            try:
                context_type = ReplyContextType(context_type)
            except ValueError:
                context_type = ReplyContextType.CUSTOM

        now = time.time()
        ctx = ReplyContext(
            context_type=context_type,
            context_key=context_key,
            message_id=None,
            created_at=now,
            expires_at=now + timeout,
            data=data or {},
        )

        self._contexts[user_id] = ctx
        logger.debug(
            f"ReplyContext set for {user_id}: {context_key} (expires in {timeout}s)"
        )

        return self._user_messages.get(user_id, 0)

    def get_reply_to_id(self, user_id: int) -> int:
        """Get the message_id the user should reply to."""
        return self._user_messages.get(user_id, 0)

    def link_message(self, user_id: int, message_id: int):
        """
        Link a sent bot message to the current context.
        The user will automatically reply to this message in Telegram.
        """
        self._user_messages[user_id] = message_id

        if user_id in self._contexts:
            self._contexts[user_id].message_id = message_id

        logger.debug(f"Linked message {message_id} to user {user_id}")

    def get_context(self, user_id: int) -> Optional[ReplyContext]:
        """Get the current reply context for a user."""
        return self._contexts.get(user_id)

    def clear_context(self, user_id: int) -> bool:
        """Clear the reply context for a user."""
        if user_id in self._contexts:
            del self._contexts[user_id]
            self._user_messages.pop(user_id, None)
            logger.debug(f"ReplyContext cleared for {user_id}")
            return True
        return False

    def validate_reply(
        self,
        user_id: int,
        message_id: Optional[int] = None,
        text: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Validate if a user's reply matches the expected context.

        Args:
            user_id: Telegram user ID
            message_id: The message_id the user replied to (from reply_to_message)
            text: The text the user sent (optional)

        Returns:
            (is_valid, error_message)
        """
        ctx = self._contexts.get(user_id)

        if not ctx:
            return False, "No pending context found. Send /cancel to clear."

        now = time.time()
        if now > ctx.expires_at:
            self.clear_context(user_id)
            return False, "Context expired. Please try again."

        # If we have a linked message, validate the reply targets it
        if ctx.message_id and message_id:
            if message_id != ctx.message_id:
                logger.warning(
                    f"Reply mismatch for {user_id}: expected {ctx.message_id}, got {message_id}"
                )
                return (
                    False,
                    f"Please reply to the highlighted message (press and hold).",
                )

        # Type-specific validation
        if text and ctx.context_type == ReplyContextType.NUMBER:
            if not text.strip().isdigit():
                return False, "Please enter a valid number."

        return True, None

    async def send_with_context(
        self,
        bot,
        user_id: int,
        text: str,
        context_type: ReplyContextType | str,
        context_key: str,
        timeout: int = 120,
        parse_mode: str = "Markdown",
        reply_markup=None,
        data: Optional[Dict[str, Any]] = None,
        keyboard=None,
    ) -> Optional[int]:
        """
        Send a message that requires user reply, automatically setting up context.

        This is the main method to use - it handles everything:
        1. Sets up reply context
        2. Gets reply_to_message_id
        3. Sends message with threading
        4. Links the sent message

        Args:
            bot: Telegram bot instance
            user_id: User to send to
            text: Message text
            context_type: Expected reply type
            context_key: Unique context key
            timeout: Context timeout in seconds
            parse_mode: Markdown/HTML
            reply_markup: Optional inline keyboard
            keyboard: Alternative name for reply_markup
            data: Extra context data

        Returns:
            Sent message_id or None
        """
        reply_to_id = self.set_context(
            user_id, context_type, context_key, timeout, data
        )

        try:
            msg = await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=parse_mode,
                reply_to_message_id=reply_to_id if reply_to_id else None,
                reply_markup=reply_markup or keyboard,
            )

            if msg:
                self.link_message(user_id, msg.message_id)
                logger.info(
                    f"Sent context message {msg.message_id} to {user_id}: {context_key}"
                )
                return msg.message_id

        except Exception as e:
            logger.error(f"Failed to send context message to {user_id}: {e}")
            self.clear_context(user_id)

        return None

    def is_awaiting_reply(self, user_id: int) -> bool:
        """Check if user has a pending reply context."""
        return user_id in self._contexts

    def get_awaiting_info(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get info about what the user is expected to reply to."""
        ctx = self._contexts.get(user_id)
        if not ctx:
            return None

        return {
            "type": ctx.context_type.value,
            "key": ctx.context_key,
            "message_id": ctx.message_id,
            "expires_in": max(0, int(ctx.expires_at - time.time())),
            "data": ctx.data,
        }


# Singleton accessor
def get_reply_context() -> ReplyContextManager:
    return ReplyContextManager.get_instance()


# Convenience functions
async def send_awaiting_message(
    bot, user_id: int, text: str, context_type: str, context_key: str, **kwargs
) -> Optional[int]:
    """Quick helper to send a message expecting reply."""
    manager = get_reply_context()
    return await manager.send_with_context(
        bot, user_id, text, context_type, context_key, **kwargs
    )


def validate_user_reply(
    user_id: int,
    reply_to_message_id: Optional[int] = None,
    text: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Quick helper to validate a user's reply."""
    manager = get_reply_context()
    return manager.validate_reply(user_id, reply_to_message_id, text)


def clear_user_context(user_id: int) -> bool:
    """Quick helper to clear a user's context."""
    manager = get_reply_context()
    return manager.clear_context(user_id)
