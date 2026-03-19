"""
database/indices.py — Index definitions for MongoDB collections.
"""

USER_INDICES = [
    ("telegram_id", {"unique": True}),
    ("plan", {}),
    ("banned", {}),
    ("role", {}),
    ("created_at", {}),
]

TASK_INDICES = [("user_id", {}), ("status", {}), ("created_at", {}), ("file_id", {})]

CLOUD_FILE_INDICES = [
    ("file_id", {"unique": True}),
    ("user_id", {}),
    ("expiry_date", {}),
    ("cloud_type", {}),
]

ONE_TIME_KEY_INDICES = [("user_id", {}), ("expires_at", {}), ("used", {})]

RCLONE_CONFIG_INDICES = [("service", {}), ("plan", {}), ("created_by", {})]

BROADCAST_INDICES = [("created_by", {}), ("created_at", {}), ("status", {})]

SECURITY_LOG_INDICES = [
    ("user_id", {}),
    ("event_type", {}),
    ("created_at", {}),
    ("severity", {}),
]


def get_all_indices():
    """Get all index definitions"""
    return {
        "users": USER_INDICES,
        "tasks": TASK_INDICES,
        "cloud_files": CLOUD_FILE_INDICES,
        "one_time_keys": ONE_TIME_KEY_INDICES,
        "rclone_configs": RCLONE_CONFIG_INDICES,
        "broadcasts": BROADCAST_INDICES,
        "security_logs": SECURITY_LOG_INDICES,
    }
