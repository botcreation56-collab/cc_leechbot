"""
database/connection.py — SHIM (do not delete)

Source moved to: infrastructure/database/_legacy_core/connection.py
The canonical new-layer connection is: infrastructure.database.connection.DatabaseConnection

This shim preserves backward compatibility for any caller using:
    from database.connection import connect_db, get_db, ...
"""
from infrastructure.database._legacy_core.connection import (
    connect_db,
    disconnect_db,
    get_db,
    get_db_context,
)

__all__ = ["connect_db", "disconnect_db", "get_db", "get_db_context"]
