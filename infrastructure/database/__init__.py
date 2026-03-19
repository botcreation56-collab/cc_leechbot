"""
infrastructure/database/__init__.py — Backward compatibility shim.

All database functionality is now in database/. This shim re-exports from there.
"""

from database import (
    connect_db,
    disconnect_db,
    get_db,
    get_db_context,
    DatabaseConnection,
    MONGODB_TIMEOUT,
    UserRepository,
    TaskRepository,
    CloudFileRepository,
    OneTimeKeyRepository,
    ConfigRepository,
    AuditLogRepository,
    RcloneConfigRepository,
)

__all__ = [
    "connect_db",
    "disconnect_db",
    "get_db",
    "get_db_context",
    "DatabaseConnection",
    "MONGODB_TIMEOUT",
    "UserRepository",
    "TaskRepository",
    "CloudFileRepository",
    "OneTimeKeyRepository",
    "ConfigRepository",
    "AuditLogRepository",
    "RcloneConfigRepository",
]
