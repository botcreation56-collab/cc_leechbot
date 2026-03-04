"""
infrastructure/database/__init__.py

Single entry point for the entire database layer.

New DDD-compliant layer (use these going forward):
  - DatabaseConnection  → infrastructure.database.connection
  - UserRepository      → infrastructure.database.repositories
  - TaskRepository      → infrastructure.database.repositories
  - CloudFileRepository → infrastructure.database.repositories
  - ConfigRepository    → infrastructure.database.repositories
  - AuditLogRepository  → infrastructure.database.repositories
  - RcloneConfigRepository → infrastructure.database.repositories

Legacy layer (preserved for backward compatibility):
  - infrastructure.database._legacy_bot.*   (was: bot/database/)
  - infrastructure.database._legacy_core.*  (was: database/)

Cache bridge (cross-layer cache invalidation):
  - infrastructure.database.cache_bridge
"""

from infrastructure.database.connection import DatabaseConnection
from infrastructure.database.repositories import (
    UserRepository,
    TaskRepository,
    CloudFileRepository,
    OneTimeKeyRepository,
    ConfigRepository,
    AuditLogRepository,
    RcloneConfigRepository,
)
from infrastructure.database.cache_bridge import (
    bust_user_cache,
    bust_config_cache,
    register_user_repo,
    register_config_repo,
)

__all__ = [
    # Connection
    "DatabaseConnection",
    # Repositories
    "UserRepository",
    "TaskRepository",
    "CloudFileRepository",
    "OneTimeKeyRepository",
    "ConfigRepository",
    "AuditLogRepository",
    "RcloneConfigRepository",
    # Cache bridge
    "bust_user_cache",
    "bust_config_cache",
    "register_user_repo",
    "register_config_repo",
]
