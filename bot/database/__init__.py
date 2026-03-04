"""
bot/database/__init__.py — SHIM (do not delete)

All source files have been moved to:
    infrastructure/database/_legacy_bot/

This shim re-exports every public symbol so that every existing caller:
    from bot.database import get_user, init_db, ...
continues to work with zero changes.
"""

# ── Connection lifecycle ────────────────────────────────────────────────────
from infrastructure.database._legacy_bot._connection import (
    init_db,
    close_db,
    get_db,
    create_indexes,
    ensure_channel_schema,
    migrate_flat_to_nested,
    MONGODB_TIMEOUT,
)

# ── Cache internals ─────────────────────────────────────────────────────────
from infrastructure.database._legacy_bot._cache import (
    _user_cache,
    _config_cache,
    _get_cache_lock,
    _bust_user_cache,
    _bust_config_cache,
)

# ── Users ───────────────────────────────────────────────────────────────────
from infrastructure.database._legacy_bot._users import (
    create_user,
    get_user,
    get_all_users,
    get_banned_users,
    update_user,
    ban_user,
    unban_user,
    set_user_role,
    store_user_thumbnail,
    get_user_destinations,
    add_user_destination,
    remove_user_destination,
)

# ── Config ──────────────────────────────────────────────────────────────────
from infrastructure.database._legacy_bot._config import (
    get_config,
    set_config,
    update_config,
    _get_from_settings,
    _initialize_config_from_settings,
)

# ── Tasks ───────────────────────────────────────────────────────────────────
from infrastructure.database._legacy_bot._tasks import (
    create_task,
    get_task,
    update_task,
    get_user_tasks,
    complete_task,
    fail_task,
    get_user_position,
    cleanup_old_tasks,
)

# ── Cloud files ─────────────────────────────────────────────────────────────
from infrastructure.database._legacy_bot._cloud import (
    store_cloud_file_metadata,
    get_user_files,
    get_user_cloud_files,
    cleanup_old_cloud_files,
    delete_expired_cloud_files,
    get_user_storage_path,
)

# ── Rclone ──────────────────────────────────────────────────────────────────
from infrastructure.database._legacy_bot._rclone import (
    add_rclone_config,
    get_rclone_configs,
    get_rclone_config,
    pick_rclone_config_for_plan,
    increment_rclone_usage,
    upload_to_rclone,
    delete_from_rclone,
    RCLONE_SUPPORTED_SERVICES,
)

# ── Broadcasts ──────────────────────────────────────────────────────────────
from infrastructure.database._legacy_bot._broadcast import (
    get_broadcasts,
    create_broadcast_draft,
    update_broadcast_draft,
    send_broadcast,
    create_broadcast_message,
)

# ── Security log & audit ────────────────────────────────────────────────────
from infrastructure.database._legacy_bot._security_log import (
    log_admin_action,
    log_security_event,
    add_action,
    get_admin_stats,
)

# ── One-time keys ────────────────────────────────────────────────────────────
from infrastructure.database._legacy_bot._auth import (
    create_one_time_key,
    verify_one_time_key,
)

# ── Channels ─────────────────────────────────────────────────────────────────
from infrastructure.database._legacy_bot._channels import (
    get_channel_config,
    get_channel_id,
    get_channel_metadata,
    get_force_sub_channels,
    set_channel_config,
    add_force_sub_channel,
    remove_force_sub_channel,
    update_force_sub_metadata,
    remove_channel_config,
    set_storage_channel,
    set_dump_channel,
    get_storage_channel,
    get_dump_channel,
    get_chatbox_messages,
    add_chatbox_message,
)

__all__ = [
    # Connection
    "init_db", "close_db", "get_db", "create_indexes",
    "ensure_channel_schema", "migrate_flat_to_nested", "MONGODB_TIMEOUT",
    # Cache
    "_user_cache", "_config_cache", "_get_cache_lock",
    "_bust_user_cache", "_bust_config_cache",
    # Users
    "create_user", "get_user", "get_all_users", "get_banned_users",
    "update_user", "ban_user", "unban_user", "set_user_role",
    "store_user_thumbnail", "get_user_destinations",
    "add_user_destination", "remove_user_destination",
    # Config
    "get_config", "set_config", "update_config",
    "_get_from_settings", "_initialize_config_from_settings",
    # Tasks
    "create_task", "get_task", "update_task", "get_user_tasks",
    "complete_task", "fail_task", "get_user_position", "cleanup_old_tasks",
    # Cloud
    "store_cloud_file_metadata", "get_user_files", "get_user_cloud_files",
    "cleanup_old_cloud_files", "delete_expired_cloud_files", "get_user_storage_path",
    # Rclone
    "add_rclone_config", "get_rclone_configs", "get_rclone_config",
    "pick_rclone_config_for_plan", "increment_rclone_usage",
    "upload_to_rclone", "delete_from_rclone", "RCLONE_SUPPORTED_SERVICES",
    # Broadcasts
    "get_broadcasts", "create_broadcast_draft", "update_broadcast_draft",
    "send_broadcast", "create_broadcast_message",
    # Security log
    "log_admin_action", "log_security_event", "add_action", "get_admin_stats",
    # Auth
    "create_one_time_key", "verify_one_time_key",
    # Channels
    "get_channel_config", "get_channel_id", "get_channel_metadata",
    "get_force_sub_channels", "set_channel_config",
    "add_force_sub_channel", "remove_force_sub_channel",
    "update_force_sub_metadata", "remove_channel_config",
    "set_storage_channel", "set_dump_channel",
    "get_storage_channel", "get_dump_channel",
    "get_chatbox_messages", "add_chatbox_message",
]
