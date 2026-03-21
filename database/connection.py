"""
database/connection.py — MongoDB connection management.

Provides two connection patterns:
1. Legacy pattern: connect_db(), get_db() — global singleton
2. New pattern: DatabaseConnection class — DI-based (recommended)

Both patterns share the same underlying Motor client.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import ConnectionFailure, OperationFailure

logger = logging.getLogger("filebot.db.connection")

MONGODB_TIMEOUT = 10000

_db: Optional[AsyncIOMotorDatabase] = None
_client: Optional[AsyncIOMotorClient] = None


class DatabaseConnection:
    """Manages the MongoDB Motor client lifecycle."""

    def __init__(
        self,
        uri: str,
        db_name: str = "filebot",
        min_pool: int = 5,
        max_pool: int = 30,
        connect_timeout_ms: int = 5_000,
        server_selection_timeout_ms: int = 10_000,
    ) -> None:
        self._uri = uri
        self._db_name = db_name
        self._min_pool = min_pool
        self._max_pool = max_pool
        self._connect_timeout_ms = connect_timeout_ms
        self._server_selection_timeout_ms = server_selection_timeout_ms
        self._client: Optional[AsyncIOMotorClient] = None
        self._db: Optional[AsyncIOMotorDatabase] = None

    async def connect(self) -> "DatabaseConnection":
        """Open connection and verify reachability."""
        if self._client is not None:
            return self

        try:
            self._client = AsyncIOMotorClient(
                self._uri,
                minPoolSize=self._min_pool,
                maxPoolSize=self._max_pool,
                connectTimeoutMS=self._connect_timeout_ms,
                serverSelectionTimeoutMS=self._server_selection_timeout_ms,
            )
            await self._client.admin.command("ping")
            self._db = self._client[self._db_name]
            logger.info("✅ MongoDB connected | database: %s", self._db_name)
        except (ConnectionFailure, OperationFailure) as exc:
            logger.error("❌ MongoDB connection failed: %s", exc)
            raise

        return self

    async def close(self, timeout: float = 5.0) -> None:
        """Gracefully close the MongoDB connection pool.

        Args:
            timeout: Maximum seconds to wait for pending operations.
        """
        if self._client:
            try:
                self._client.close(timeout=timeout)
                logger.info("🛑 MongoDB connection closed gracefully")
            except Exception as e:
                logger.warning(f"MongoDB close error (forced): {e}")
                self._client = None
                self._db = None

    @property
    def db(self) -> AsyncIOMotorDatabase:
        """Return the Motor Database handle."""
        if self._db is None:
            raise RuntimeError("Database is not connected. Call connect() first.")
        return self._db

    async def create_indexes(self) -> None:
        """Ensure all necessary indexes exist. Idempotent."""
        db = self.db
        logger.info("🔧 Creating/verifying MongoDB indexes…")

        try:
            await db.users.create_index(
                [("telegram_id", ASCENDING)], unique=True, name="users_telegram_id_uq"
            )
            await db.users.create_index(
                [("username", ASCENDING)], sparse=True, name="users_username"
            )
            await db.users.create_index([("role", ASCENDING)], name="users_role")
            await db.users.create_index([("banned", ASCENDING)], name="users_banned")
            await db.users.create_index([("plan", ASCENDING)], name="users_plan")

            await db.tasks.create_index(
                [("user_id", ASCENDING), ("created_at", DESCENDING)],
                name="tasks_user_created",
            )
            await db.tasks.create_index([("status", ASCENDING)], name="tasks_status")
            await db.tasks.create_index(
                [("created_at", ASCENDING)],
                expireAfterSeconds=7 * 24 * 3600,
                name="tasks_ttl",
            )

            await db.cloud_files.create_index(
                [("file_id", ASCENDING)], unique=True, name="cloud_files_file_id_uq"
            )
            await db.cloud_files.create_index(
                [("user_id", ASCENDING)], name="cloud_files_user_id"
            )
            await db.cloud_files.create_index(
                [("visibility", ASCENDING)], name="cloud_files_visibility"
            )
            await db.cloud_files.create_index(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0,
                sparse=True,
                name="cloud_files_expires_ttl",
            )

            await db.one_time_keys.create_index(
                [("otp", ASCENDING)], unique=True, name="otk_otp_uq"
            )
            await db.one_time_keys.create_index(
                [("user_id", ASCENDING)], name="otk_user_id"
            )
            await db.one_time_keys.create_index(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0,
                name="otk_ttl",
            )

            await db.sessions.create_index(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0,
                name="sessions_ttl",
            )
            await db.sessions.create_index(
                [("token_hash", ASCENDING)],
                unique=True,
                name="sessions_token_hash_uq",
            )

            await db.rate_limits.create_index(
                [("key", ASCENDING), ("window_start", ASCENDING)],
                name="rate_limits_key_window",
            )
            await db.rate_limits.create_index(
                [("last_seen", ASCENDING)],
                expireAfterSeconds=3600,
                name="rate_limits_ttl",
            )

            await db.rclone_configs.create_index(
                [("plan", ASCENDING)], name="rclone_plan"
            )
            await db.rclone_configs.create_index(
                [("is_active", ASCENDING)], name="rclone_active"
            )

            await db.audit_log.create_index(
                [("admin_id", ASCENDING), ("timestamp", DESCENDING)],
                name="audit_admin_ts",
            )
            await db.audit_log.create_index(
                [("timestamp", ASCENDING)],
                expireAfterSeconds=90 * 24 * 3600,
                name="audit_ttl",
            )

            logger.info("✅ MongoDB indexes verified")
        except Exception as exc:
            logger.error("⚠️  Index creation partially failed: %s", exc, exc_info=True)


async def connect_db() -> AsyncIOMotorDatabase:
    """Legacy: Connect to MongoDB and return database instance."""
    global _db, _client

    if _db is not None:
        return _db

    try:
        from config.settings import get_settings

        settings = get_settings()
        _client = AsyncIOMotorClient(settings.MONGODB_URI)
        _db = _client[settings.MONGODB_DB]
        await _db.command("ping")
        logger.info(f"✅ Connected to MongoDB: {settings.MONGODB_DB}")
        return _db
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        raise RuntimeError("DB connection failed")


async def disconnect_db() -> None:
    """Legacy: Disconnect from MongoDB."""
    global _db, _client

    try:
        if _client:
            _client.close()
            _db = None
            _client = None
        logger.info("✅ Disconnected from MongoDB")
    except Exception as e:
        logger.error(f"❌ Disconnect error: {e}")


def get_db() -> AsyncIOMotorDatabase:
    """Legacy: Get current database instance."""
    if _db is not None:
        return _db
    raise RuntimeError("Database not connected. Call connect_db() first.")


def _set_shared_db(db: AsyncIOMotorDatabase) -> None:
    """Internal: Set the shared database instance (used by main.py to inject DI connection)."""
    global _db
    _db = db


@asynccontextmanager
async def get_db_context():
    """Context manager for database operations."""
    db = get_db()
    try:
        yield db
    except Exception as e:
        logger.error(f"❌ Database operation error: {e}")
        raise


async def init_db() -> None:
    """Legacy: Initialize database connection and indexes."""
    await connect_db()
    db = get_db()

    collections = [
        "users",
        "tasks",
        "cloud_files",
        "config",
        "one_time_keys",
        "dest_links",
        "rclone_configs",
        "broadcasts",
        "chatbox",
        "admin_logs",
        "actions",
    ]
    existing = await db.list_collection_names()
    for collection in collections:
        if collection not in existing:
            await db.create_collection(collection)
            logger.info(f"✅ Created collection: {collection}")

    await create_indexes(db)
    logger.info("✅ Database initialized")


async def create_indexes(db: AsyncIOMotorDatabase) -> None:
    """Legacy: Create database indexes for performance."""
    try:
        await db.users.create_index("telegram_id", unique=True)
        await db.users.create_index("plan")
        await db.users.create_index("banned")
        await db.users.create_index("created_at")
        await db.users.create_index("role")

        await db.tasks.create_index("task_id", unique=True)
        await db.tasks.create_index("user_id")
        await db.tasks.create_index("status")
        await db.tasks.create_index([("user_id", 1), ("status", 1)])

        await db.cloud_files.create_index("file_id", unique=True)
        await db.cloud_files.create_index("user_id")
        await db.cloud_files.create_index("expiry_date")

        await db.one_time_keys.create_index("user_id")
        await db.one_time_keys.create_index("expires_at")

        await db.dest_links.create_index("token", unique=True)
        await db.dest_links.create_index("user_id")
        await db.dest_links.create_index("dest_chat_id")

        await db.admin_logs.create_index("admin_id")
        await db.admin_logs.create_index("user_id")
        await db.admin_logs.create_index("timestamp")

        await db.rclone_configs.create_index("service")
        await db.rclone_configs.create_index("plan")

        await db.config.create_index("type", unique=True)

        await db.broadcasts.create_index("status")
        await db.broadcasts.create_index("created_at")

        await db.chatbox.create_index("user_id")
        await db.chatbox.create_index("timestamp")

        await db.actions.create_index("admin_id")
        await db.actions.create_index("timestamp")

        logger.info("✅ All database indices created successfully")
    except Exception as e:
        logger.error(f"❌ Index creation failed: {e}", exc_info=True)
        raise


async def ensure_channel_schema(db: AsyncIOMotorDatabase) -> None:
    """Legacy: Ensure channel schema exists in config collection."""
    import datetime

    existing = await db.config.find_one({"type": "global"})
    if not existing:
        await db.config.insert_one(
            {
                "type": "global",
                "channels": {},
                "created_at": datetime.datetime.utcnow(),
            }
        )
        logger.info("✅ Channel schema initialized")


async def migrate_flat_to_nested(db: AsyncIOMotorDatabase) -> None:
    """Legacy: Migrate flat channel config to nested structure."""
    config = await db.config.find_one({"type": "global"})
    if not config:
        return

    if "channels" in config:
        return

    channels = {}
    for key in ["log_channel", "dump_channel", "storage_channel", "force_sub_channel"]:
        if key in config:
            channels[key.replace("_channel", "")] = {"id": config[key], "metadata": {}}

    if channels:
        await db.config.update_one({"type": "global"}, {"$set": {"channels": channels}})
        logger.info("✅ Migrated flat config to nested structure")
