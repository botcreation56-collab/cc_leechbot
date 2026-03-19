"""
infrastructure/database/repositories.py — Backward compatibility shim.

All database functionality is now in database/repositories.py. This shim re-exports from there.
"""

from database.repositories import (
    UserRepository,
    TaskRepository,
    CloudFileRepository,
    OneTimeKeyRepository,
    ConfigRepository,
    AuditLogRepository,
    RcloneConfigRepository,
)

__all__ = [
    "UserRepository",
    "TaskRepository",
    "CloudFileRepository",
    "OneTimeKeyRepository",
    "ConfigRepository",
    "AuditLogRepository",
    "RcloneConfigRepository",
]
