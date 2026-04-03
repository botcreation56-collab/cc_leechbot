"""
Bot middleware — consolidated single file.
Provides:
  - ban_check_middleware  : rejects banned users, stores user in context to prevent double DB hit
  - admin_only decorator  : restricts functions to admins
  - require_admin helper  : inline admin check with user reply
  - error_handler         : global uncaught-exception handler
  - safe_async_wrapper    : decorator for fire-and-forget error safety
"""

import logging
import traceback
from datetime import datetime
from typing import Any, Callable, Optional
from functools import wraps
import time
import re

from cachetools import TTLCache
from telegram import Update
from telegram.ext import ContextTypes, ApplicationHandlerStop

from database import get_user, log_security_event
from config.constants import ERROR_MESSAGES
from config.settings import get_admin_ids, get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ============================================================
# MARKDOWN ESCAPE HELPER
# ============================================================

_MD_SPECIAL = re.compile(r"([_*`\[\]])")


def escape_md(text: str) -> str:
    """Escape Markdown v1 special characters to prevent parse errors."""
    if not text:
        return ""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


# ============================================================
# BUTTON & ACTION RATE LIMITING
# ============================================================

# TTLCache: auto-evicts entries after 120 s — zero memory leak
_ACTIVE_USERS: TTLCache = TTLCache(maxsize=10_000, ttl=120)

# Per-action spam protection - tracks specific callback_data
_ACTION_LOCKS: TTLCache = TTLCache(maxsize=50_000, ttl=60)


def rate_limit(func: Callable) -> Callable:
    """
    Decorator to prevent users from spam-clicking buttons or sending rapid requests.
    Locks the user for max 120s while the current request is processing.
    Also prevents duplicate clicks on the SAME button.
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        if not update.effective_user:
            return await func(update, context)

        user_id = update.effective_user.id
        now = time.time()

        # Get callback_data for per-action locking
        callback_data = None
        if update.callback_query and update.callback_query.data:
            callback_data = update.callback_query.data

        # Create unique action key: user_id + callback_data
        action_key = f"{user_id}:{callback_data}" if callback_data else str(user_id)

        # Check per-action lock FIRST (prevent same button spam)
        if action_key in _ACTION_LOCKS:
            logger.warning(f"⏳ Duplicate action blocked: {action_key}")
            if update.callback_query:
                try:
                    await update.callback_query.answer(
                        "⏳ Already processing...", show_alert=False
                    )
                except Exception as e:
                    logger.debug(f"Callback answer skipped (duplicate): {e}")
            return

        # Check global cooldown
        if user_id in _ACTIVE_USERS:
            last_request_time = _ACTIVE_USERS[user_id]
            if now - last_request_time < 1.5:
                logger.warning(f"⏳ Rate limit hit for {user_id}. Ignoring request.")
                if update.callback_query:
                    try:
                        await update.callback_query.answer(
                            "⏳ Please wait a moment...", show_alert=False
                        )
                    except Exception as e:
                        logger.debug(f"Callback answer skipped (rate-limit): {e}")
                return

        # Acquire locks
        _ACTIVE_USERS[user_id] = now
        _ACTION_LOCKS[action_key] = now

        try:
            return await func(update, context)
        finally:
            # Release locks after completion
            _ACTIVE_USERS[user_id] = time.time()
            # Remove action lock after a short delay to allow same action later
            _ACTION_LOCKS.pop(action_key, None)

    return wrapper


def action_lock(func: Callable) -> Callable:
    """
    Decorator for critical actions - locks by callback_data ONLY.
    Use for buttons that trigger long operations (bypass, upload, etc.)
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        if not update.callback_query or not update.callback_query.data:
            return await func(update, context)

        user_id = update.effective_user.id
        callback_data = update.callback_query.data
        action_key = f"{user_id}:{callback_data}"

        if action_key in _ACTION_LOCKS:
            try:
                await update.callback_query.answer(
                    "⏳ Processing in progress...", show_alert=True
                )
            except Exception:
                pass
            return

        _ACTION_LOCKS[action_key] = time.time()
        try:
            return await func(update, context)
        finally:
            _ACTION_LOCKS.pop(action_key, None)

    return wrapper


# ============================================================
# ADMIN DECORATOR
# ============================================================

# (wraps already imported at top)


def admin_only(func: Callable) -> Callable:
    """
    Decorator — restricts a handler to admins only.
    Usage:
        @admin_only
        async def admin_command(update, context): ...
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        try:
            if not update.effective_user:
                return

            user_id = update.effective_user.id
            admin_ids = get_admin_ids()

            logger.info(f"🕵️ Admin check: user={user_id} | admins={admin_ids}")

            if not await is_admin(user_id):
                logger.warning(
                    f"🚫 Unauthorized admin access: {user_id} (not in admin list or DB role)"
                )
                if update.message:
                    await update.message.reply_text(
                        "⛔ You don't have permission to use this command."
                    )
                # Fire-and-forget security log (non-blocking)
                try:
                    await log_security_event(
                        user_id, "unauthorized_admin_access", "high"
                    )
                except Exception as e:
                    logger.debug(f"Security event log skipped: {e}")
                return

            return await func(update, context)

        except Exception as e:
            logger.error(f"❌ admin_only wrapper error: {e}")
            if update.message:
                await update.message.reply_text("❌ Authentication error.")

    return wrapper


async def verify_admin(user_id: int) -> bool:
    """Admin check — env-list OR DB role=admin."""
    try:
        return await is_admin(user_id)
    except Exception as e:
        logger.error(f"❌ verify_admin error: {e}")
        return False


async def is_admin(user_id: int, db_user: Optional[dict] = None) -> bool:
    """
    Unified admin check: returns True if user_id is in the hard-coded
    ADMIN_IDS list OR if their DB record has role='admin'.
    Accepts an already-fetched db_user to avoid a redundant DB round-trip.
    """
    try:
        if user_id in get_admin_ids():
            return True
        user = db_user or await get_user(user_id)
        if user and user.get("role") == "admin":
            return True
        return False
    except Exception as e:
        logger.error(f"❌ is_admin error for {user_id}: {e}")
        return False


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Inline admin check — sends a rejection message if not admin.
    Returns True if admin, False otherwise.
    """
    if not update.effective_user:
        return False

    user_id = update.effective_user.id
    is_admin = await verify_admin(user_id)

    if not is_admin and update.message:
        await update.message.reply_text("⛔ Admin only command.")

    return is_admin


# ============================================================
# BAN CHECK MIDDLEWARE
# ============================================================


async def ban_check_middleware(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """
    Check if user is banned before passing the update to any handler.

    Design decisions:
    - FAIL-SECURE: on DB error we DENY the request (not allow).
      A banned user on a broken DB connection is safer than allowing them through.
      Change to fail-open only if your SLA demands it.
    - Stores the fetched user in context.user_data["_cached_user"] so handlers
      can reuse it without an extra DB round-trip.

    Returns:
        True  → allow the update to pass through
        False → block the update (user is banned or fetch failed)
    """
    if not update.effective_user:
        return True  # System update (e.g., channel post), allow

    user_id = update.effective_user.id

    try:
        user = await get_user(user_id)

        if user is None:
            # Brand-new user — not banned
            return True

        # Cache for the lifetime of this update so handlers don't re-fetch
        context.user_data["_cached_user"] = user

        if user.get("banned"):
            ban_reason = user.get("ban_reason", "No reason provided")
            logger.warning(f"🚫 Banned user blocked: {user_id}")

            if update.message:
                await update.message.reply_text(
                    f"🚫 Your account has been banned.\n\n"
                    f"Reason: {ban_reason}\n\n"
                    f"Contact support if you believe this is a mistake."
                )
            elif update.callback_query:
                await update.callback_query.answer(
                    "🚫 Your account has been banned.", show_alert=True
                )

            try:
                await log_security_event(user_id, "banned_user_access_attempt", "high")
            except Exception:
                pass

            return False

        return True

    except Exception as e:
        # FAIL-SECURE: DB unreachable → deny the request
        logger.error(f"❌ ban_check_middleware error for {user_id}: {e}")
        if update.message:
            try:
                await update.message.reply_text(
                    "⚠️ Service temporarily unavailable. Please try again shortly."
                )
            except Exception:
                pass
        return False


async def apply_ban_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB TypeHandler entry point (group -1).

    Must raise ApplicationHandlerStop to prevent the update from reaching
    any further handlers when a user is banned. Returning a value does
    nothing in PTB — only the exception actually halts propagation.
    """
    allowed = await ban_check_middleware(update, context)
    if not allowed:
        raise ApplicationHandlerStop


# ============================================================
# CUSTOM EXCEPTION
# ============================================================


class FileBotException(Exception):
    """Custom exception for FileBot-specific errors."""

    def __init__(self, message: str, user_id: int = None, log_db=None):
        self.message = message
        self.user_id = user_id
        self.log_db = log_db
        super().__init__(message)


# ============================================================
# GLOBAL ERROR HANDLER
# ============================================================


async def error_handler(
    update: Optional[Update], context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Global uncaught-exception handler registered with PTB.
    Logs to log channel and notifies the first admin via DM.
    """
    try:
        error = context.error
        user_id = (
            update.effective_user.id
            if update is not None and update.effective_user is not None
            else None
        )

        error_msg = str(error)
        error_type = type(error).__name__
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        logger.error(
            f"❌ UNCAUGHT [{error_type}] | user={user_id} | {error_msg}\n"
            f"{traceback.format_exc()}"
        )

        # Notify user (generic message only - don't reveal error details)
        if user_id and update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "❌ **Something went wrong**\n\n"
                    "Please try again. If the problem persists, contact support.",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.debug(f"User error-reply skipped: {e}")

        # Log to channel
        try:
            from database import get_channel_id

            db_log_channel = await get_channel_id("log")
            log_channel_id = (
                db_log_channel if db_log_channel else settings.LOG_CHANNEL_ID
            )
            if log_channel_id and context.bot:
                report = (
                    f"🔴 **ERROR REPORT**\n\n"
                    f"**Type:** `{escape_md(error_type)}`\n"
                    f"**User:** `{user_id}`\n"
                    f"**Time:** `{timestamp}`\n\n"
                    f"**Message:**\n```\n{escape_md(error_msg[:500])}\n```\n\n"
                    f"**Traceback:**\n```\n{escape_md(traceback.format_exc()[:1000])}\n```"
                )
                await context.bot.send_message(
                    chat_id=log_channel_id,
                    text=report,
                    parse_mode="Markdown",
                )
        except Exception as ch_err:
            logger.error(f"Failed to send error to log channel: {ch_err}")

        # DM admins
        try:
            admin_ids = get_admin_ids()
            if admin_ids and context.bot:
                user_link = (
                    f"[{user_id}](tg://user?id={user_id})" if user_id else "`System`"
                )
                alert = (
                    f"⚠️ **Bot Error Alert**\n\n"
                    f"Type: `{escape_md(error_type)}`\n"
                    f"User: {user_link}\n"
                    f"Time: `{timestamp}`\n\n"
                    f"Message: `{escape_md(error_msg[:200])}`\n\n"
                    f"_Check log channel for full details_"
                )
                for admin_id in admin_ids:
                    try:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=alert,
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
        except Exception:
            pass

    except Exception as handler_err:
        logger.critical(f"🔥 error_handler itself crashed: {handler_err}")


# ============================================================
# UTILITY WRAPPER
# ============================================================


def safe_async_wrapper(func: Callable) -> Callable:
    """
    Decorator — safely runs async functions and re-raises errors after logging.
    Usage: @safe_async_wrapper
    """

    @wraps(func)  # Preserves __name__, __doc__, __module__, __annotations__
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"❌ {func.__name__} error: {e}")
            raise

    return wrapper
