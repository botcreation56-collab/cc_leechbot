"""
database/__init__.py — Consolidated Database Layer

All database functionality has been consolidated into this directory.
The following sub-modules are available:

Connection & Configuration:
    - connect_db, disconnect_db, get_db, get_db_context
    - DatabaseConnection (new DI-based class)

Repositories (new pattern - recommended):
    - UserRepository, TaskRepository, CloudFileRepository
    - OneTimeKeyRepository, ConfigRepository
    - AuditLogRepository, RcloneConfigRepository

Cache:
    - User/config caches with TTL support

Functions (legacy pattern - still supported):
    - Users: create_user, get_user, get_all_users, update_user, ban_user, etc.
    - Tasks: create_task, get_task, update_task, get_user_tasks, etc.
    - Config: get_config, set_config, update_config
    - Cloud: store_cloud_file_metadata, get_user_files, cleanup_old_cloud_files
    - Rclone: add_rclone_config, get_rclone_configs, upload_to_rclone, etc.
    - Broadcast: get_broadcasts, create_broadcast_draft, send_broadcast
    - Channels: get_channel_config, set_channel_config, get_force_sub_channels
    - Auth: create_one_time_key, verify_one_time_key
    - Security: log_admin_action, log_security_event, get_admin_stats
"""

from database.connection import (
    connect_db,
    disconnect_db,
    get_db,
    get_db_context,
    DatabaseConnection,
    MONGODB_TIMEOUT,
)
from database.repositories import (
    UserRepository,
    TaskRepository,
    CloudFileRepository,
    OneTimeKeyRepository,
    ConfigRepository,
    AuditLogRepository,
    RcloneConfigRepository,
)
from database.cache import (
    _user_cache,
    _config_cache,
    _get_cache_lock,
    _bust_user_cache,
    _bust_config_cache,
)
from database.users import (
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
from database.tasks import (
    create_task,
    get_task,
    update_task,
    get_user_tasks,
    complete_task,
    fail_task,
    get_user_position,
    get_active_task_count,
    cleanup_old_tasks,
)
from database.config import (
    get_config,
    get_config_sync,
    set_config,
    update_config,
    _get_from_settings,
    _initialize_config_from_settings,
)
from database.cloud import (
    store_cloud_file_metadata,
    get_user_files,
    get_user_cloud_files,
    cleanup_old_cloud_files,
    delete_expired_cloud_files,
    get_user_storage_path,
)
from database.rclone import (
    add_rclone_config,
    get_rclone_configs,
    get_rclone_config,
    pick_rclone_config_for_plan,
    increment_rclone_usage,
    upload_to_rclone,
    delete_from_rclone,
    update_rclone_config,
    delete_rclone_config,
    RCLONE_SUPPORTED_SERVICES,
)
from database.broadcast import (
    get_broadcasts,
    create_broadcast_draft,
    update_broadcast_draft,
    send_broadcast,
    create_broadcast_message,
)
from database.channels import (
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
    get_unique_chat_users,
)
from database.auth import (
    create_one_time_key,
    verify_one_time_key,
)
from database.security_log import (
    log_admin_action,
    log_security_event,
    add_action,
    get_admin_stats,
)
from database.migrations import (
    run_migrations,
    create_collections,
    create_indices,
)
from database.indices import (
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
    # Connection
    "connect_db",
    "disconnect_db",
    "get_db",
    "get_db_context",
    "DatabaseConnection",
    "MONGODB_TIMEOUT",
    # Repositories
    "UserRepository",
    "TaskRepository",
    "CloudFileRepository",
    "OneTimeKeyRepository",
    "ConfigRepository",
    "AuditLogRepository",
    "RcloneConfigRepository",
    # Cache
    "_user_cache",
    "_config_cache",
    "_get_cache_lock",
    "_bust_user_cache",
    "_bust_config_cache",
    # Users
    "create_user",
    "get_user",
    "get_all_users",
    "get_banned_users",
    "update_user",
    "ban_user",
    "unban_user",
    "set_user_role",
    "store_user_thumbnail",
    "get_user_destinations",
    "add_user_destination",
    "remove_user_destination",
    # Tasks
    "create_task",
    "get_task",
    "update_task",
    "get_user_tasks",
    "complete_task",
    "fail_task",
    "get_user_position",
    "get_active_task_count",
    "cleanup_old_tasks",
    # Config
    "get_config",
    "get_config_sync",
    "set_config",
    "update_config",
    "_get_from_settings",
    "_initialize_config_from_settings",
    # Cloud
    "store_cloud_file_metadata",
    "get_user_files",
    "get_user_cloud_files",
    "cleanup_old_cloud_files",
    "delete_expired_cloud_files",
    "get_user_storage_path",
    # Rclone
    "add_rclone_config",
    "get_rclone_configs",
    "get_rclone_config",
    "pick_rclone_config_for_plan",
    "increment_rclone_usage",
    "upload_to_rclone",
    "delete_from_rclone",
    "update_rclone_config",
    "delete_rclone_config",
    "RCLONE_SUPPORTED_SERVICES",
    # Broadcast
    "get_broadcasts",
    "create_broadcast_draft",
    "update_broadcast_draft",
    "send_broadcast",
    "create_broadcast_message",
    # Channels
    "get_channel_config",
    "get_channel_id",
    "get_channel_metadata",
    "get_force_sub_channels",
    "set_channel_config",
    "add_force_sub_channel",
    "remove_force_sub_channel",
    "update_force_sub_metadata",
    "remove_channel_config",
    "set_storage_channel",
    "set_dump_channel",
    "get_storage_channel",
    "get_dump_channel",
    "get_chatbox_messages",
    "add_chatbox_message",
    "get_unique_chat_users",
    # Auth
    "create_one_time_key",
    "verify_one_time_key",
    # Security
    "log_admin_action",
    "log_security_event",
    "add_action",
    "get_admin_stats",
    # Migrations
    "run_migrations",
    "create_collections",
    "create_indices",
    # Indices
    "USER_INDICES",
    "TASK_INDICES",
    "CLOUD_FILE_INDICES",
    "ONE_TIME_KEY_INDICES",
    "RCLONE_CONFIG_INDICES",
    "BROADCAST_INDICES",
    "SECURITY_LOG_INDICES",
    "get_all_indices",
]
