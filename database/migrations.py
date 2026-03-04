"""
database/migrations.py — SHIM (do not delete)

Source moved to: infrastructure/database/_legacy_core/migrations.py
"""
from infrastructure.database._legacy_core.migrations import (
    run_migrations,
    create_collections,
    create_indices,
)

__all__ = ["run_migrations", "create_collections", "create_indices"]
