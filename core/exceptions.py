"""
core/exceptions.py — Canonical domain exception hierarchy.

All domain-specific errors should subclass one of these base classes.
The presentation layer (handlers, API routes) catches these and maps
them to user-facing messages or HTTP status codes WITHOUT any business
logic leaking into the response layer.

Hierarchy:
    FileBotError                  (base — never raise directly)
      ├── AuthError
      │     ├── UserBannedError
      │     └── AccessDeniedError
      ├── ValidationError
      │     ├── FileTooLargeError
      │     ├── InvalidURLError
      │     └── InvalidFilenameError
      ├── QuotaError
      │     ├── DailyQuotaExceededError
      │     └── StorageQuotaExceededError
      ├── DownloadError
      │     └── UnsupportedURLError
      ├── ProcessingError
      │     └── FFmpegError
      ├── UploadError
      │     ├── TelegramUploadError
      │     └── RcloneUploadError
      └── InfrastructureError
            ├── DatabaseError
            └── ConfigurationError
"""

from __future__ import annotations

from typing import Any, Optional


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class FileBotError(Exception):
    """Root exception for all FileBot domain errors."""

    def __init__(self, message: str, *, code: Optional[str] = None, context: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.__class__.__name__
        self.context: dict[str, Any] = context or {}

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        ctx = f", context={self.context}" if self.context else ""
        return f"{self.__class__.__name__}(message={self.message!r}, code={self.code!r}{ctx})"


# ---------------------------------------------------------------------------
# Auth / Access
# ---------------------------------------------------------------------------

class AuthError(FileBotError):
    """Base for authentication and authorisation failures."""


class UserBannedError(AuthError):
    """Raised when a banned user attempts any protected action."""

    def __init__(self, user_id: int, reason: str = "Account suspended") -> None:
        super().__init__(f"User {user_id} is banned: {reason}", context={"user_id": user_id, "reason": reason})
        self.user_id = user_id
        self.reason = reason


class AccessDeniedError(AuthError):
    """Raised when a user lacks permissions for an operation."""

    def __init__(self, user_id: int, required_role: str = "admin") -> None:
        super().__init__(
            f"Access denied for user {user_id}. Required role: {required_role}",
            context={"user_id": user_id, "required_role": required_role},
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class ValidationError(FileBotError):
    """Base for input validation failures."""


class FileTooLargeError(ValidationError):
    """Raised when a file exceeds the plan's allowed size."""

    def __init__(self, actual_bytes: int, limit_bytes: int, plan: str = "free") -> None:
        actual_gb = actual_bytes / 1024 ** 3
        limit_gb = limit_bytes / 1024 ** 3
        super().__init__(
            f"File is {actual_gb:.2f} GB but your {plan.upper()} plan allows {limit_gb:.1f} GB.",
            context={"actual_bytes": actual_bytes, "limit_bytes": limit_bytes, "plan": plan},
        )
        self.actual_bytes = actual_bytes
        self.limit_bytes = limit_bytes


class InvalidURLError(ValidationError):
    """Raised when a URL fails SSRF/scheme/format validation."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"Invalid URL: {reason}", context={"url": url, "reason": reason})
        self.url = url
        self.reason = reason


class InvalidFilenameError(ValidationError):
    """Raised when a filename contains dangerous characters or exceeds limits."""

    def __init__(self, filename: str, reason: str) -> None:
        super().__init__(f"Invalid filename: {reason}", context={"filename": filename, "reason": reason})


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------

class QuotaError(FileBotError):
    """Base for quota / limit violations."""


class DailyQuotaExceededError(QuotaError):
    """Raised when a user's daily processing quota is exhausted."""

    def __init__(self, user_id: int, used_gb: float, limit_gb: float) -> None:
        super().__init__(
            f"Daily quota exceeded. Used {used_gb:.1f} GB / {limit_gb:.1f} GB.",
            context={"user_id": user_id, "used_gb": used_gb, "limit_gb": limit_gb},
        )


class StorageQuotaExceededError(QuotaError):
    """Raised when a user's total cloud storage quota is exhausted."""

    def __init__(self, user_id: int, used_gb: float, limit_gb: float) -> None:
        super().__init__(
            f"Storage quota exceeded. Used {used_gb:.1f} GB / {limit_gb:.1f} GB.",
            context={"user_id": user_id, "used_gb": used_gb, "limit_gb": limit_gb},
        )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

class DownloadError(FileBotError):
    """Base for download-pipeline failures."""


class UnsupportedURLError(DownloadError):
    """Raised when yt-dlp or aria2c cannot handle the provided URL."""

    def __init__(self, url: str, reason: str = "Unsupported source") -> None:
        super().__init__(f"Cannot download URL: {reason}", context={"url": url, "reason": reason})


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

class ProcessingError(FileBotError):
    """Base for media processing failures."""


class FFmpegError(ProcessingError):
    """Raised when FFmpeg exits with a non-zero return code."""

    def __init__(self, command: str, returncode: int, stderr: str) -> None:
        super().__init__(
            f"FFmpeg failed (exit {returncode}): {stderr[:200]}",
            context={"returncode": returncode, "stderr": stderr},
        )
        self.returncode = returncode
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

class UploadError(FileBotError):
    """Base for upload-pipeline failures."""


class TelegramUploadError(UploadError):
    """Raised when a Pyrogram / PTB upload to Telegram fails."""

    def __init__(self, file: str, reason: str) -> None:
        super().__init__(f"Telegram upload failed for '{file}': {reason}", context={"file": file, "reason": reason})


class RcloneUploadError(UploadError):
    """Raised when an Rclone upload to a cloud remote fails."""

    def __init__(self, remote: str, file: str, reason: str) -> None:
        super().__init__(
            f"Rclone upload of '{file}' to '{remote}' failed: {reason}",
            context={"remote": remote, "file": file, "reason": reason},
        )


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

class InfrastructureError(FileBotError):
    """Base for underlying infrastructure / external-system failures."""


class DatabaseError(InfrastructureError):
    """Raised when a MongoDB operation fails in an unrecoverable way."""

    def __init__(self, operation: str, reason: str) -> None:
        super().__init__(f"Database error during '{operation}': {reason}", context={"operation": operation})


class ConfigurationError(InfrastructureError):
    """Raised when a required configuration value is missing or invalid at startup."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"Configuration error — {field}: {reason}", context={"field": field, "reason": reason})
        self.field = field
