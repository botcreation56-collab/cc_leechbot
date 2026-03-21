"""
bot/utils/error_handler.py — Centralized error handling for the bot.

- User-friendly error messages (no technical details exposed)
- Technical errors are logged and sent to admin via PM
- Prevents injection attacks via input sanitization
"""

import logging
import traceback
import re
from typing import Optional, Callable, Any
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# User-friendly error messages (no technical details)
USER_ERROR_MESSAGES = {
    "default": "Something went wrong. Please try again later.",
    "download": "Failed to download the file. The source might be unavailable.",
    "processing": "Failed to process the file. Please try again.",
    "upload": "Failed to send the file. Please try again.",
    "ffmpeg": "Failed to process the media file. The file might be corrupted.",
    "timeout": "The operation took too long. Please try with a smaller file.",
    "quota": "You've reached your processing limit. Please upgrade your plan.",
    "invalid": "Invalid input. Please check your request and try again.",
    "disk_full": "Server storage is temporarily full. Please try again later.",
    "network": "Network error occurred. Please check your connection and try again.",
    "auth": "Authentication failed. Please restart the bot.",
    "banned": "Your account has been suspended.",
}

# Error categories for user messages
ERROR_CATEGORIES = {
    "DownloadError": "download",
    "FFmpegError": "ffmpeg",
    "UploadError": "upload",
    "TelegramUploadError": "upload",
    "RcloneUploadError": "upload",
    "ProcessingError": "processing",
    "TimeoutError": "timeout",
    "asyncio.TimeoutError": "timeout",
    "DailyQuotaExceededError": "quota",
    "StorageQuotaExceededError": "quota",
    "QuotaError": "quota",
    "FileTooLargeError": "invalid",
    "InvalidURLError": "invalid",
    "InvalidFilenameError": "invalid",
    "ValidationError": "invalid",
    "UserBannedError": "banned",
    "DiskFullError": "disk_full",
    "OSError": "disk_full",  # Often disk-related
    "IOError": "network",
    "ConnectionError": "network",
    "httpx.ConnectError": "network",
    "DatabaseError": "default",
    "InfrastructureError": "default",
}


def sanitize_for_log(text: str, max_length: int = 500) -> str:
    """Remove sensitive data and limit length for safe logging."""
    if not text:
        return ""
    # Remove potential injection patterns
    text = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", text)
    # Truncate long messages
    if len(text) > max_length:
        text = text[:max_length] + "...[TRUNCATED]"
    return text.strip()


def sanitize_for_display(text: str) -> str:
    """Remove any characters that could be used for injection in messages."""
    if not text:
        return ""
    # Remove markdown/HTML special characters for safe display
    text = re.sub(r"[*_`\[\]()~>#+\-=|{}.!\\]", "", text)
    # Remove control characters
    text = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", text)
    return text.strip()[:200]  # Limit to 200 chars for display


def get_user_error_message(error: Exception) -> str:
    """Get safe user-friendly error message without technical details."""
    error_type = type(error).__name__
    error_category = ERROR_CATEGORIES.get(error_type, "default")
    return USER_ERROR_MESSAGES.get(error_category, USER_ERROR_MESSAGES["default"])


def get_error_category(error: Exception) -> str:
    """Determine error category for proper handling."""
    error_type = type(error).__name__
    return ERROR_CATEGORIES.get(error_type, "default")


async def notify_admin(
    bot,
    error: Exception,
    context: dict,
    user_id: Optional[int] = None,
    task_id: Optional[str] = None,
    phase: str = "unknown",
) -> None:
    """
    Send technical error details to admin via PM.
    Includes sanitization to prevent any injection.
    """
    try:
        from config.settings import get_admin_ids

        admin_ids = get_admin_ids()

        if not admin_ids:
            logger.warning("No admin IDs configured for error notification")
            return

        error_type = type(error).__name__
        error_msg = sanitize_for_log(str(error))
        trace = sanitize_for_log(traceback.format_exc())

        # Build error report
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        report_lines = [
            "🚨 **Bot Error Report**",
            "─" * 40,
            f"**Time:** {timestamp}",
            f"**Phase:** {sanitize_for_log(phase)}",
        ]

        if user_id:
            report_lines.append(f"**User:** `{user_id}`")

        if task_id:
            report_lines.append(f"**Task:** `{sanitize_for_log(task_id)}`")

        report_lines.extend(
            [
                f"**Error Type:** `{error_type}`",
                f"**Message:** {error_msg[:300]}",
                "─" * 40,
                "**Traceback:**",
                f"```",
                trace[-1500:],  # Limit traceback size
                "```",
            ]
        )

        if context:
            safe_context = {
                k: sanitize_for_log(str(v))[:100] for k, v in context.items() if v
            }
            report_lines.extend(
                [
                    "─" * 40,
                    "**Context:**",
                    str(safe_context)[:500],
                ]
            )

        report = "\n".join(report_lines)

        # Send to all admins
        for admin_id in admin_ids:
            try:
                await bot.send_message(
                    chat_id=admin_id, text=report, parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")

    except Exception as e:
        logger.error(f"Error in notify_admin: {e}")


async def handle_processing_error(
    update: Optional[Update],
    context: ContextTypes.DEFAULT_TYPE,
    error: Exception,
    phase: str = "processing",
    user_id: Optional[int] = None,
    task_id: Optional[str] = None,
    edit_message: bool = True,
    delete_after: int = 0,
) -> None:
    """
    Central error handler for processing errors.

    - Logs the full error with context
    - Sends technical details to admin
    - Shows user-friendly message only
    """
    error_type = type(error).__name__
    error_category = get_error_category(error)
    user_message = get_user_error_message(error)

    # Log full error with context
    log_context = {
        "phase": phase,
        "error_type": error_type,
        "user_id": user_id or (update.effective_user.id if update else None),
        "task_id": task_id,
    }

    logger.error(
        f"❌ Processing Error [{phase}] [{error_type}]: {error}",
        exc_info=True,
        extra=log_context,
    )

    # Notify admin with technical details
    await notify_admin(
        bot=context.bot,
        error=error,
        context=log_context,
        user_id=user_id,
        task_id=task_id,
        phase=phase,
    )

    # Send user-friendly message
    uid = user_id or (update.effective_user.id if update else None)

    if not uid:
        return

    # Keyboard for retry/help
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Try Again", callback_data="retry_last")],
            [InlineKeyboardButton("📞 Contact Support", callback_data="us_support")],
        ]
    )

    try:
        if (
            update
            and hasattr(update, "callback_query")
            and update.callback_query
            and edit_message
        ):
            try:
                await update.callback_query.answer("❌ Error", show_alert=True)
                await update.callback_query.message.edit_text(
                    f"❌ {user_message}", reply_markup=keyboard, parse_mode="Markdown"
                )
            except Exception:
                # If edit fails, send new message
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"❌ {user_message}",
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                )
        elif update and hasattr(update, "message") and update.message:
            await update.message.reply_text(
                f"❌ {user_message}", reply_markup=keyboard, parse_mode="Markdown"
            )
        else:
            await context.bot.send_message(
                chat_id=uid,
                text=f"❌ {user_message}",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"Failed to send error message to user: {e}")


class ErrorHandling:
    """Decorator class for automatic error handling in async functions."""

    @staticmethod
    def safe(
        phase: str = "operation",
        user_message: Optional[str] = None,
        notify_admin: bool = True,
        default_return: Any = None,
    ):
        """
        Decorator for safe async function execution.

        Usage:
            @ErrorHandling.safe(phase="download")
            async def my_function():
                ...
        """

        def decorator(func):
            async def wrapper(*args, **kwargs):
                # Extract bot and context from args if available
                bot = None
                context = None
                update = None
                user_id = None
                task_id = None

                for arg in args:
                    if isinstance(arg, Update):
                        update = arg
                        if hasattr(arg, "effective_user") and arg.effective_user:
                            user_id = arg.effective_user.id
                    if isinstance(arg, ContextTypes.DEFAULT_TYPE):
                        context = arg

                # Try to get from kwargs
                context = kwargs.get("context", context)
                update = kwargs.get("update", update)

                if context:
                    bot = context.bot

                if (
                    update
                    and hasattr(update, "effective_user")
                    and update.effective_user
                ):
                    user_id = update.effective_user.id

                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    error_msg = user_message or get_user_error_message(e)

                    # Log the error
                    logger.error(
                        f"❌ Error in {func.__name__} [{phase}]: {e}", exc_info=True
                    )

                    # Notify admin if enabled
                    if notify_admin and bot:
                        await notify_admin(
                            bot=bot,
                            error=e,
                            context={"function": func.__name__, "phase": phase},
                            user_id=user_id,
                            task_id=task_id,
                            phase=phase,
                        )

                    # Try to inform user
                    if context and user_id:
                        try:
                            keyboard = InlineKeyboardMarkup(
                                [
                                    [
                                        InlineKeyboardButton(
                                            "🔄 Try Again", callback_data="retry_last"
                                        )
                                    ],
                                    [
                                        InlineKeyboardButton(
                                            "📞 Support", callback_data="us_support"
                                        )
                                    ],
                                ]
                            )
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=f"❌ {error_msg}",
                                reply_markup=keyboard,
                                parse_mode="Markdown",
                            )
                        except Exception:
                            pass

                    return default_return

            return wrapper

        return decorator


async def safe_execute(
    coro,
    bot,
    user_id: int,
    phase: str,
    task_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> Any:
    """
    Safely execute a coroutine with error handling.

    Returns the result on success, None on failure.
    Error is logged and admin is notified.
    """
    try:
        return await coro
    except Exception as e:
        await notify_admin(
            bot=bot,
            error=e,
            context=context or {},
            user_id=user_id,
            task_id=task_id,
            phase=phase,
        )
        logger.error(f"❌ Error in {phase} for user {user_id}: {e}", exc_info=True)
        return None


# Input validation helpers to prevent injection
def validate_filename(filename: str) -> tuple[bool, str]:
    """Validate filename for safe processing."""
    if not filename:
        return False, "Filename is required"

    # Remove null bytes and control characters
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)

    # Check for path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        return False, "Invalid characters in filename"

    # Check length
    if len(filename) > 255:
        return False, "Filename too long"

    # Allow only safe characters
    safe_pattern = r"^[a-zA-Z0-9_\-\s\.()]+$"
    if not re.match(safe_pattern, filename):
        return False, "Filename contains invalid characters"

    return True, "valid"


def validate_url(url: str) -> tuple[bool, str]:
    """Validate URL to prevent SSRF and injection."""
    if not url:
        return False, "URL is required"

    url_lower = url.lower().strip()

    # Check for allowed protocols
    if not url_lower.startswith(("http://", "https://", "ftp://")):
        return False, "Only HTTP/HTTPS/FTP URLs are allowed"

    # Block private IP ranges (basic SSRF prevention)
    private_patterns = [
        r"^10\.",  # 10.0.0.0/8
        r"^172\.(1[6-9]|2[0-9]|3[0-1])\.",  # 172.16.0.0/12
        r"^192\.168\.",  # 192.168.0.0/16
        r"^127\.",  # localhost
        r"^localhost",
        r"^0\.",  # 0.0.0.0
        r"^169\.254\.",  # link-local
    ]

    # Extract host from URL
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]

        for pattern in private_patterns:
            if re.match(pattern, host):
                return False, "Private URLs are not allowed"
    except Exception:
        return False, "Invalid URL format"

    # Block dangerous schemes
    dangerous_schemes = ["javascript", "data", "file", "ftp"]
    for scheme in dangerous_schemes:
        if url_lower.startswith(f"{scheme}:"):
            return False, f"{scheme}: URLs are not allowed"

    return True, "valid"


def validate_callback_data(data: str) -> tuple[bool, str]:
    """Validate callback data to prevent injection."""
    if not data:
        return False, "Data is required"

    # Only allow alphanumeric, underscore, hyphen
    if not re.match(r"^[a-zA-Z0-9_\-:]+$", data):
        return False, "Invalid callback data"

    # Limit length
    if len(data) > 200:
        return False, "Callback data too long"

    return True, "valid"


def validate_metadata_value(value: str) -> str:
    """Sanitize metadata value to prevent injection."""
    if not value:
        return ""

    # Remove potential injection characters
    value = re.sub(r'[<>{}\\$`"|;]', "", value)
    value = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", value)

    # Limit length
    return value.strip()[:500]
