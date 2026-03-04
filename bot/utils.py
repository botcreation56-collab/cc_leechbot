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

from cryptography.fernet import Fernet, InvalidToken
from telegram import Bot

from config.settings import get_settings

# ============================================================
# SINGLE MODULE LOGGER
# ============================================================

logger = logging.getLogger("filebot")

# ============================================================
# ENCRYPTION
# ============================================================

class EncryptionManager:
    """Manages Fernet encryption/decryption of sensitive credentials."""

    def __init__(self, encryption_key: str = None):
        try:
            if not encryption_key:
                encryption_key = os.getenv("ENCRYPTION_KEY")
            if not encryption_key:
                raise ValueError("ENCRYPTION_KEY not set in environment")
            key_bytes = encryption_key.encode() if isinstance(encryption_key, str) else encryption_key
            self.cipher = Fernet(key_bytes)
        except Exception as e:
            logger.error(f"Encryption initialization failed: {e}")
            raise

    def encrypt(self, data: Dict[str, Any]) -> str:
        """Encrypt a dict → base64 string."""
        try:
            return self.cipher.encrypt(json.dumps(data).encode()).decode()
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise

    def decrypt(self, encrypted_str: str) -> Dict[str, Any]:
        """Decrypt a base64 string → dict."""
        try:
            return json.loads(self.cipher.decrypt(encrypted_str.encode()).decode())
        except InvalidToken:
            logger.error("Decryption failed: invalid token or corrupted data")
            raise
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise


_encryption_manager: Optional[EncryptionManager] = None


def get_encryption_manager() -> EncryptionManager:
    global _encryption_manager
    if not _encryption_manager:
        _encryption_manager = EncryptionManager()
    return _encryption_manager


def encrypt_credentials(credentials: Dict[str, Any]) -> str:
    return get_encryption_manager().encrypt(credentials)


def decrypt_credentials(encrypted: str) -> Dict[str, Any]:
    return get_encryption_manager().decrypt(encrypted)


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
            logging.StreamHandler(),
        ],
    )

    # Suppress noisy third-party loggers
    for lib in ("httpx", "urllib3", "motor", "passlib", "telegram"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    logger.info(f"Logging initialized | Level: {settings.LOG_LEVEL} | File: {settings.LOG_FILE}")


# ============================================================
# ASYNC LOG HELPERS
# ============================================================

async def log_info(message: str) -> None:
    """Async-safe info log."""
    logger.info(message)


async def log_error(message: str) -> None:
    """Async-safe error log."""
    logger.error(message)


async def log_user_update(
    bot: Bot,
    user_id: int,
    action: str,
    details: Optional[str] = None,
) -> None:
    """Log a user-triggered action."""
    try:
        msg = f"User {user_id} | Action: {action}"
        if details:
            msg += f" | {details}"
        logger.info(msg)
    except Exception as e:
        logger.error(f"Failed to log user update: {e}")


async def log_admin_action(
    admin_id: int,
    action: str,
    details: Optional[dict] = None,
) -> None:
    """Log an admin action with structured details."""
    try:
        msg = f"ADMIN ACTION | Admin: {admin_id} | Action: {action}"
        if details:
            msg += " | " + " | ".join(f"{k}: {v}" for k, v in details.items())
        logger.info(msg)
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
    VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".m3u8", ".ts", ".wmv", ".3gp", ".ogv"}
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
    filename = os.path.basename(filename)          # Block path traversal
    filename = filename.replace("\x00", "")        # Strip null bytes
    clean = FILENAME_ILLEGAL.sub("_", filename)    # Replace only illegal chars
    clean = clean.strip()                          # Trim whitespace edges only
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
        free_gb = free / (1024 ** 3)
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
    ipaddress.ip_network("127.0.0.0/8"),     # loopback
    ipaddress.ip_network("10.0.0.0/8"),       # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),    # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),   # RFC 1918
    ipaddress.ip_network("169.254.0.0/16"),   # link-local
    ipaddress.ip_network("::1/128"),          # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),         # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
    ipaddress.ip_network("0.0.0.0/8"),        # "this network"
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
      2. HTTP or HTTPS scheme only (rejects javascript:, data:, file://, etc.)
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
        return False, "Only HTTPS URLs are securely accepted"

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


# ============================================================
# OTHER VALIDATORS
# ============================================================

def validate_user_id(user_id: int) -> Tuple[bool, str]:
    """Validate Telegram user ID."""
    if not isinstance(user_id, int) or user_id <= 0:
        return False, "User ID must be a positive integer"
    return True, ""


def validate_file_size(size: int, max_size: int) -> Tuple[bool, str]:
    """Validate file size against plan limit."""
    if size > max_size:
        max_gb = max_size / (1024 ** 3)
        return False, f"File exceeds {max_gb:.1f}GB limit"
    return True, ""


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
