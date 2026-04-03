"""
bot/utils.py — Consolidated utility module.

Provides:
  - EncryptionManager  (Fernet encrypt/decrypt)
  - setup_logging()    (configure root logger)
  - Async log helpers  (log_info, log_error, log_user_update, log_admin_action)
  - File helpers       (format_bytes, format_seconds, sanitize_filename, etc.)
  - validate_url()     (single canonical version with full SSRF protection)
  - Other validators   (validate_user_id, validate_file_size, validate_filename, validate_email)

NOTE: MockDB / MockBot / MockContext / MockUpdate have been removed from this file.
      They belong in tests/ only. Import from there in test code.
"""

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from telegram import Bot
from telegram.error import TelegramError

from config.settings import get_settings

# ============================================================
# SINGLE MODULE LOGGER
# ============================================================

logger = logging.getLogger("filebot")


class TelegramLogHandler(logging.Handler):
    """Custom logging handler that sends logs to a Telegram channel."""

    _bot: Bot | None = None
    _channel_id: int | None = None
    _queue: asyncio.Queue = None
    _worker_task: asyncio.Task | None = None

    def __init__(self):
        super().__init__()
        self._queue = asyncio.Queue(maxsize=100)

    @classmethod
    async def initialize(cls, bot: Bot):
        """Initialize the handler with a bot instance."""
        cls._bot = bot
        from database import get_channel_id
        from config.settings import get_settings

        settings = get_settings()
        db_log_channel = await get_channel_id("log")
        cls._channel_id = db_log_channel if db_log_channel else settings.LOG_CHANNEL_ID

        if cls._channel_id and cls._worker_task is None:
            cls._worker_task = asyncio.create_task(cls._worker())

    @classmethod
    async def _worker(cls):
        """Background worker to process log messages."""
        while True:
            try:
                record = await cls._queue.get()
                if cls._bot and cls._channel_id:
                    await cls._send_message(record)
                cls._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    @classmethod
    async def _send_message(cls, record: logging.LogRecord):
        """Send formatted log message to Telegram."""
        try:
            emoji = {
                "DEBUG": "🔍",
                "INFO": "ℹ️",
                "WARNING": "⚠️",
                "ERROR": "❌",
                "CRITICAL": "🚨",
            }.get(record.levelname, "📝")

            timestamp = datetime.now().strftime("%H:%M:%S")
            message = (
                f"{emoji} *{record.levelname}*\n"
                f"`{timestamp}` | `{record.name}`\n"
                f"{record.getMessage()}"
            )

            await cls._bot.send_message(
                chat_id=cls._channel_id,
                text=message[:4096],
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except TelegramError:
            pass

    def emit(self, record: logging.LogRecord):
        """Emit a log record to the queue."""
        try:
            if not self._queue.full():
                self._queue.put_nowait(record)
        except Exception:
            pass


# ============================================================
# ENCRYPTION - Consolidated from core.security
# ============================================================
# Re-export from core.security to maintain backwards compatibility

from core.security import (
    encrypt_credentials,
    decrypt_credentials,
    encrypt_token,
    decrypt_token,
    TokenGenerator,
)

__all__ = [
    "encrypt_credentials",
    "decrypt_credentials",
    "encrypt_token",
    "decrypt_token",
    "TokenGenerator",
]


# ============================================================
# LOGGING SETUP
# ============================================================


def setup_logging() -> None:
    """Configure root logger (call once at startup)."""
    settings = get_settings()

    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(settings.LOG_FILE, encoding="utf-8"),
        ],
    )

    # Add Telegram log handler
    telegram_handler = TelegramLogHandler()
    telegram_handler.setLevel(logging.WARNING)  # Only send warnings/errors to channel
    logging.getLogger().addHandler(telegram_handler)

    # Suppress noisy third-party loggers
    for lib in ("httpx", "urllib3", "motor", "passlib", "telegram"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    logger.info(
        f"Logging initialized | Level: {settings.LOG_LEVEL} | File: {settings.LOG_FILE}"
    )


async def init_telegram_logging(bot) -> None:
    """Initialize Telegram log handler after bot is ready."""
    await TelegramLogHandler.initialize(bot)


# ============================================================
# ASYNC LOG HELPERS
# ============================================================


async def log_info(message: str) -> None:
    """Async-safe info log."""
    logger.info(message)


async def log_error(message: str) -> None:
    """Async-safe error log."""
    logger.error(message)


# Per-user lock for storage channel sync to prevent race conditions
_sync_locks: Dict[int, asyncio.Lock] = {}


async def _sync_user_profile_to_storage(bot: Bot, user_id: int) -> None:
    # Get or create lock for this user
    if user_id not in _sync_locks:
        _sync_locks[user_id] = asyncio.Lock()

    async with _sync_locks[user_id]:
        try:
            from database import get_user, update_user, get_storage_channel

            user = await get_user(user_id)
            if not user:
                return

            storage_channel = await get_storage_channel()
            if not storage_channel or not storage_channel.get("id"):
                return

            channel_id = storage_channel["id"]
            settings = user.get("settings", {})
            plan = user.get("plan", "free").upper()

            prefix = settings.get("prefix") or "None"
            suffix = settings.get("suffix") or "None"
            thumb_id = (
                settings.get("thumbnail_file_id")
                if settings.get("thumbnail") == "custom"
                else None
            )

            from database import get_user_destinations

            try:
                dests = await get_user_destinations(user_id)
                dest_count = len(dests) if dests else 0
            except:
                dest_count = 0

            profile_text = (
                f"👤 **User Profile Sync**\n"
                f"ID: `{user_id}`\n"
                f"Plan: `{plan}`\n\n"
                f"**Current Settings:**\n"
                f"Prefix: `{prefix}`\n"
                f"Suffix: `{suffix}`\n"
                f"Destinations: `{dest_count}`\n"
                f"Thumbnail: `{'Set ✅' if thumb_id else 'None ❌'}`\n\n"
                f"🕒 Last Update: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
            )

            msg_id = settings.get("storage_msg_id")

            # 🗑️ DELETE PREVIOUS MESSAGE IF IT EXISTS
            # This ensures only one "active" card exists in storage channel
            if msg_id:
                try:
                    await bot.delete_message(chat_id=channel_id, message_id=msg_id)
                    logger.info(
                        f"🗑️ Deleted old storage message {msg_id} for user {user_id}"
                    )
                except Exception as de:
                    logger.debug(f"Could not delete old storage msg {msg_id}: {de}")
                msg_id = None  # Reset to force resend below

            # If we have a thumbnail, we prefer sending as a photo
            try:
                if not msg_id:
                    if thumb_id:
                        new_msg = await bot.send_photo(
                            chat_id=channel_id,
                            photo=thumb_id,
                            caption=profile_text,
                            parse_mode="Markdown",
                        )
                    else:
                        new_msg = await bot.send_message(
                            chat_id=channel_id, text=profile_text, parse_mode="Markdown"
                        )

                    # Directly update DB with new MSG ID to avoid race conditions with old user objects
                    from database.connection import get_db

                    db = get_db()
                    await db.users.update_one(
                        {"telegram_id": user_id},
                        {"$set": {"settings.storage_msg_id": new_msg.message_id}},
                    )
            except Exception as e:
                logger.warning(f"⚠️ Storage sync partial failure: {e}")

        except Exception as e:
            logger.error(f"Failed to sync user {user_id} profile to storage: {e}")


async def log_user_update(
    bot: Bot,
    user_id: int,
    action: str,
    details: Optional[str] = None,
) -> None:
    """Log a user-triggered action to both local logger and Telegram log channel."""
    try:
        msg = f"User {user_id} | Action: {action}"
        if details:
            msg += f" | {details}"
        logger.info(msg)

        # Log to Telegram
        telegram_msg = f"👤 **USER ACTION**\nUser: `{user_id}`\nAction: `{action}`"
        if details:
            telegram_msg += f"\nDetails: `{details}`"

        asyncio.create_task(send_to_log_channel(bot, telegram_msg))

        # Sync profile to storage channel in background
        asyncio.create_task(_sync_user_profile_to_storage(bot, user_id))
    except Exception as e:
        logger.error(f"Failed to log user update: {e}")


async def log_admin_action(
    admin_id: int,
    action: str,
    details: Optional[dict] = None,
    bot: Optional[Bot] = None,
) -> None:
    """Log an admin action to local logger and optionally to Telegram log channel."""
    try:
        msg = f"ADMIN ACTION | Admin: {admin_id} | Action: {action}"
        if details:
            msg += " | " + " | ".join(f"{k}: {v}" for k, v in details.items())
        logger.info(msg)

        if bot:
            telegram_msg = (
                f"🛡️ **ADMIN ACTION**\nAdmin: `{admin_id}`\nAction: `{action}`"
            )
            if details:
                telegram_msg += "\n\n**Details:**\n"
                telegram_msg += "\n".join(f"- {k}: `{v}`" for k, v in details.items())

            asyncio.create_task(send_to_log_channel(bot, telegram_msg))

    except Exception as e:
        logger.error(f"Failed to log admin action for {admin_id}: {e}", exc_info=True)


# ============================================================
# FILE HELPERS
# ============================================================


def format_bytes(num_bytes: int) -> str:
    """Format bytes to human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024:
            return f"{num_bytes:.2f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f}PB"


def format_seconds(seconds: int) -> str:
    """Format seconds to h/m/s string."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def cleanup_temp_file(file_path: str) -> bool:
    """Delete a temporary file."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            return True
        return False
    except Exception as e:
        logger.warning(f"Failed to delete {file_path}: {e}")
        return False


def get_file_size(file_path: str) -> int:
    try:
        return os.path.getsize(file_path)
    except Exception:
        return 0


def get_file_extension(filename: str) -> str:
    return Path(filename).suffix.lower()


def get_file_name_without_ext(filename: str) -> str:
    return Path(filename).stem


def is_video_file(filename: str) -> bool:
    VIDEO_EXT = {
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".flv",
        ".webm",
        ".m3u8",
        ".ts",
        ".wmv",
        ".3gp",
        ".ogv",
    }
    return get_file_extension(filename) in VIDEO_EXT


def is_audio_file(filename: str) -> bool:
    AUDIO_EXT = {".mp3", ".aac", ".flac", ".wav", ".wma", ".ogg", ".m4a"}
    return get_file_extension(filename) in AUDIO_EXT


def get_expiry_date(days: int) -> datetime:
    return datetime.utcnow() + timedelta(days=days)


def is_expired(expiry_date: datetime) -> bool:
    return datetime.utcnow() > expiry_date


# ============================================================
# PATH SAFETY
# ============================================================

# Deny-list: only strip characters that are illegal on the filesystem.
# Linux forbids: / and null byte.
# We also strip Windows-unsafe chars for portability: \ : * ? " < > |
FILENAME_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00]')


def sanitize_filename(filename: str) -> str:
    """
    Preserve the filename exactly as the user gave it, stripping ONLY
    characters that are illegal on Linux/Windows filesystems.
    Keeps spaces, emojis, apostrophes, brackets, underscores, etc.
    Returns a safe filename string of max 255 characters.
    """
    if not filename:
        return "unknown_file"
    filename = os.path.basename(filename)  # Block path traversal
    filename = filename.replace("\x00", "")  # Strip null bytes
    clean = FILENAME_ILLEGAL.sub("_", filename)  # Replace only illegal chars
    clean = clean.strip()  # Trim whitespace edges only
    return clean[:255] or "unnamed_file"


def safe_path(base_dir: str, filename: str) -> str:
    """
    Resolve path and verify it stays within base_dir.
    Raises ValueError on path-traversal attempts (including %2e%2e URL-encoded forms).
    """
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    if base not in target.parents and base != target:
        raise ValueError(f"Path traversal blocked: {filename!r}")
    return str(target)


def check_disk_space(path: str, min_gb: float = 1.0) -> bool:
    """Return True if free disk space >= min_gb, False otherwise."""
    try:
        _, _, free = shutil.disk_usage(path)
        free_gb = free / (1024**3)
        if free_gb < min_gb:
            logger.critical(f"❌ LOW DISK SPACE: {free_gb:.2f}GB free (min {min_gb}GB)")
            return False
        return True
    except Exception as e:
        logger.error(f"Disk check failed: {e}")
        return True  # Fail open — don't block service on check failure


# ============================================================
# URL VALIDATION — single canonical version with SSRF protection
# ============================================================

# Blocks private / link-local / loopback ranges per RFC 1918 / RFC 4193 / RFC 5737
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("0.0.0.0/8"),  # "this network"
]

_BLOCKED_SCHEMES = {"javascript", "data", "file", "ftp", "gopher"}
_URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


def _is_private_ip(host: str) -> bool:
    """Return True if host resolves to any private/reserved IP."""
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        pass  # host is a domain — resolve it

    try:
        for _, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            addr = ipaddress.ip_address(sockaddr[0])
            if any(addr in net for net in _PRIVATE_NETWORKS):
                return True
    except Exception:
        pass  # DNS failure → treat as invalid but not private

    return False


def validate_url(url: str) -> Tuple[bool, str]:
    """
    Validate a URL for safety.
    Checks:
      1. Non-empty
      2. HTTP or HTTPS scheme only
      3. Matches URL regex
      4. Host does not resolve to a private/reserved IP (SSRF protection)

    Returns:
        (True, "")              → URL is safe to use
        (False, reason_str)     → URL is blocked, reason_str explains why
    """
    if not url or not url.strip():
        return False, "Empty URL"

    url = url.strip()

    # Scheme check — catches javascript:, data:, file:// etc.
    scheme = url.split(":")[0].lower()
    if scheme in _BLOCKED_SCHEMES:
        return False, f"Blocked scheme: {scheme}"

    if not url.lower().startswith("https://"):
        return False, "Only HTTPS URLs are accepted for security reasons"

    # Block YouTube/YouTu.be to avoid strikes
    blocked_domains = ["youtube.com", "youtu.be", "www.youtube.com"]
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if any(domain in host for domain in blocked_domains):
            return False, "YouTube links are not supported Right now"
    except:
        return False, "Invalid URL format"

    if not _URL_RE.match(url):
        return False, "Invalid URL format"

    # Extract hostname
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname or ""
    except Exception:
        return False, "Could not parse URL"

    if not host:
        return False, "No hostname in URL"

    if _is_private_ip(host):
        return False, f"SSRF blocked: {host} resolves to a private/reserved address"

    return True, ""


async def send_to_log_channel(
    bot: Bot, message: str, parse_mode: str = "Markdown"
) -> bool:
    """Send a message to the configured log channel."""
    try:
        from database import get_channel_id
        from config.settings import get_settings

        db_log_channel = await get_channel_id("log")
        settings = get_settings()
        log_channel_id = db_log_channel if db_log_channel else settings.LOG_CHANNEL_ID

        if log_channel_id:
            await bot.send_message(
                chat_id=log_channel_id,
                text=message,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to send to log channel: {e}")
        return False


async def auto_delete_message(bot: Bot, chat_id: int, message_id: int, delay: int = 5):
    """Wait for 'delay' seconds and then delete the message."""
    await asyncio.sleep(delay)
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id=chat_id, message_id=message_id)


async def send_auto_delete_msg(
    bot: Bot, chat_id: int, text: str, delay: int = 10, **kwargs
):
    """Sends a message and deletes it after `delay` seconds."""
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        asyncio.create_task(auto_delete_message(bot, chat_id, msg.message_id, delay))
        return msg
    except Exception as e:
        logger.error(f"Failed to send auto-delete message: {e}")
        return None


# ============================================================
# OTHER VALIDATORS
# ============================================================


def validate_user_id(user_id: int) -> Tuple[bool, str]:
    """Validate Telegram user ID."""
    if not isinstance(user_id, int) or user_id <= 0:
        return False, "User ID must be a positive integer"
    return True, ""


def validate_file_size(size: int, limit: Any) -> Tuple[bool, str]:
    """
    Validate file size against a numeric limit (int) or a plan name (str).
    Supported plans: free, premium, pro.
    """
    try:
        from config.constants import MAX_FILE_SIZE_FREE, MAX_FILE_SIZE_PRO

        # If limit is a string (plan name), resolve it to bytes
        if isinstance(limit, str):
            plan = limit.lower()
            if plan == "pro":
                max_bytes = MAX_FILE_SIZE_PRO
            elif plan == "premium":
                # Assuming premium is same as pro for size limits, or define specifically
                max_bytes = MAX_FILE_SIZE_PRO
            else:
                max_bytes = MAX_FILE_SIZE_FREE
        else:
            max_bytes = int(limit)

        if size > max_bytes:
            max_gb = max_bytes / (1024**3)
            return False, f"File exceeds {max_gb:.1f}GB limit"
        return True, ""
    except Exception as e:
        logger.error(f"File size validation error: {e}")
        return False, "Validation error"


def validate_filename(filename: str) -> Tuple[bool, str]:
    """Validate a filename for safety."""
    if not filename or len(filename) > 255:
        return False, "Invalid filename length"
    dangerous = ["..", "/", "\\", "\0"]
    if any(d in filename for d in dangerous):
        return False, "Filename contains dangerous characters"
    return True, ""


def validate_email(email: str) -> Tuple[bool, str]:
    """Validate email address format."""
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    if not re.match(pattern, email):
        return False, "Invalid email format"
    return True, ""
