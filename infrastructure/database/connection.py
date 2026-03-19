"""
infrastructure/database/connection.py — Backward compatibility shim.

All database functionality is now in database/connection.py. This shim re-exports from there.
"""

from database.connection import (
    connect_db,
    disconnect_db,
    get_db,
    get_db_context,
    DatabaseConnection,
    MONGODB_TIMEOUT,
    _set_shared_db,
    init_db,
    create_indexes,
    ensure_channel_schema,
    migrate_flat_to_nested,
)

__all__ = [
    "connect_db",
    "disconnect_db",
    "get_db",
    "get_db_context",
    "DatabaseConnection",
    "MONGODB_TIMEOUT",
    "_set_shared_db",
    "init_db",
    "create_indexes",
    "ensure_channel_schema",
    "migrate_flat_to_nested",
]
