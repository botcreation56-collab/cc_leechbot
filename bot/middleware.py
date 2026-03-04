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

from telegram import Update
from telegram.ext import ContextTypes

from bot.database import get_user, log_security_event
from config.constants import ERROR_MESSAGES
from config.settings import get_admin_ids, get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ============================================================
# ADMIN DECORATOR
# ============================================================

from functools import wraps

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

            if user_id not in admin_ids:
                logger.warning(f"🚫 Unauthorized admin access: {user_id}")
                if update.message:
                    await update.message.reply_text(
                        "⛔ You don't have permission to use this command."
                    )
                # Fire-and-forget security log (non-blocking)
                try:
                    await log_security_event(user_id, "unauthorized_admin_access", "high")
                except Exception:
                    pass
                return

            return await func(update, context)

        except Exception as e:
            logger.error(f"❌ admin_only wrapper error: {e}")
            if update.message:
                await update.message.reply_text("❌ Authentication error.")

    return wrapper


async def verify_admin(user_id: int) -> bool:
    """Simple admin check."""
    try:
        return user_id in get_admin_ids()
    except Exception as e:
        logger.error(f"❌ verify_admin error: {e}")
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
    """PTB middleware entry point."""
    await ban_check_middleware(update, context)


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

async def error_handler(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE) -> None:
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

        # Notify user
        if user_id and update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    f"❌ Something went wrong. Please try again.\n\n"
                    f"Error: {error_msg[:100]}",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

        # Log to channel
        try:
            log_channel_id = settings.LOG_CHANNEL_ID
            if log_channel_id and context.bot:
                report = (
                    f"🔴 **ERROR REPORT**\n\n"
                    f"**Type:** `{error_type}`\n"
                    f"**User:** `{user_id}`\n"
                    f"**Time:** `{timestamp}`\n\n"
                    f"**Message:**\n```\n{error_msg[:500]}\n```\n\n"
                    f"**Traceback:**\n```\n{traceback.format_exc()[:1000]}\n```"
                )
                await context.bot.send_message(
                    chat_id=log_channel_id,
                    text=report,
                    parse_mode="Markdown",
                )
        except Exception as ch_err:
            logger.error(f"Failed to send error to log channel: {ch_err}")

        # DM first admin
        try:
            admin_ids = get_admin_ids()
            if admin_ids and context.bot:
                alert = (
                    f"⚠️ **Bot Error Alert**\n\n"
                    f"Type: `{error_type}`\n"
                    f"User: `{user_id}`\n"
                    f"Time: `{timestamp}`\n\n"
                    f"Message: `{error_msg[:200]}`\n\n"
                    f"_Check log channel for full details_"
                )
                await context.bot.send_message(
                    chat_id=admin_ids[0],
                    text=alert,
                    parse_mode="Markdown",
                )
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
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"❌ {func.__name__} error: {e}")
            raise

    wrapper.__name__ = func.__name__
    return wrapper
