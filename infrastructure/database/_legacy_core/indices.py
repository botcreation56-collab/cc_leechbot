"""
Index definitions for MongoDB collections
Optimizes query performance
"""

# User collection indices
USER_INDICES = [
    ("telegram_id", {"unique": True}),
    ("plan", {}),
    ("banned", {}),
    ("role", {}),  # New: Index on role for faster queries
    ("created_at", {})
]

# Task collection indices
TASK_INDICES = [
    ("user_id", {}),
    ("status", {}),
    ("created_at", {}),
    ("file_id", {})
]

# Cloud files collection indices
CLOUD_FILE_INDICES = [
    ("file_id", {"unique": True}),
    ("user_id", {}),
    ("expiry_date", {}),
    ("cloud_type", {})
]

# One-time keys collection indices
ONE_TIME_KEY_INDICES = [
    ("user_id", {}),
    ("expires_at", {}),
    ("used", {})
]

# Rclone configs collection indices
RCLONE_CONFIG_INDICES = [
    ("service", {}),
    ("plan", {}),
    ("created_by", {})
]

# Broadcast collection indices
BROADCAST_INDICES = [
    ("created_by", {}),
    ("created_at", {}),
    ("status", {})
]

# Security logs collection indices
SECURITY_LOG_INDICES = [
    ("user_id", {}),
    ("event_type", {}),
    ("created_at", {}),
    ("severity", {})
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
        "security_logs": SECURITY_LOG_INDICES
    }

