"""
database/indices.py — SHIM (do not delete)

Source moved to: infrastructure/database/_legacy_core/indices.py
"""
from infrastructure.database._legacy_core.indices import (
    USER_INDICES,
    TASK_INDICES,
    CLOUD_FILE_INDICES,
    ONE_TIME_KEY_INDICES,
    RCLONE_CONFIG_INDICES,
    BROADCAST_INDICES,
    SECURITY_LOG_INDICES,
    get_all_indices,
)

__all__ = [
    "USER_INDICES", "TASK_INDICES", "CLOUD_FILE_INDICES",
    "ONE_TIME_KEY_INDICES", "RCLONE_CONFIG_INDICES",
    "BROADCAST_INDICES", "SECURITY_LOG_INDICES", "get_all_indices",
]
