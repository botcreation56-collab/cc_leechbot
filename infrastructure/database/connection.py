"""
infrastructure/database/connection.py — Motor MongoDB lifecycle management.

Design decisions:
  - Single client created at startup and injected into repositories via DI.
  - All index creation is idempotent and done at startup.
  - Connection pool tuned for async workloads.
  - No global singletons exported — callers hold the Database object.
"""

from __future__ import annotations

import logging
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import ConnectionFailure, OperationFailure

from core.exceptions import DatabaseError

logger = logging.getLogger("filebot.db.connection")


class DatabaseConnection:
    """Manages the MongoDB Motor client lifecycle.

    Usage (composition root / main.py):
        conn = DatabaseConnection(settings.MONGODB_URI)
        await conn.connect()
        db = conn.db           # pass this into your repositories
        ...
        await conn.close()
    """

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

    # ------------------------------------------------------------------ #
    # Lifecycle                                                             #
    # ------------------------------------------------------------------ #

    async def connect(self) -> "DatabaseConnection":
        """Open connection and verify reachability. Raises ``DatabaseError`` on failure."""
        if self._client is not None:
            return self  # already connected

        try:
            self._client = AsyncIOMotorClient(
                self._uri,
                minPoolSize=self._min_pool,
                maxPoolSize=self._max_pool,
                connectTimeoutMS=self._connect_timeout_ms,
                serverSelectionTimeoutMS=self._server_selection_timeout_ms,
                # Require TLS in production; driver will validate server cert automatically
                # tls=True is implicit when URI uses mongodb+srv or tls=true parameter
            )
            # Verify connectivity immediately
            await self._client.admin.command("ping")
            self._db = self._client[self._db_name]
            logger.info("✅ MongoDB connected | database: %s", self._db_name)
        except (ConnectionFailure, OperationFailure) as exc:
            raise DatabaseError("connect", str(exc)) from exc
        except Exception as exc:
            raise DatabaseError("connect", f"Unexpected error: {exc}") from exc

        return self

    async def close(self) -> None:
        """Gracefully close the MongoDB connection pool."""
        if self._client:
            self._client.close()
            self._client = None
            self._db = None
            logger.info("🛑 MongoDB connection closed")

    @property
    def db(self) -> AsyncIOMotorDatabase:
        """Return the Motor Database handle. Raises if not connected."""
        if self._db is None:
            raise DatabaseError("db_access", "Database is not connected. Call connect() first.")
        return self._db

    # ------------------------------------------------------------------ #
    # Index Bootstrap                                                       #
    # ------------------------------------------------------------------ #

    async def create_indexes(self) -> None:
        """Ensure all necessary indexes exist. Idempotent — safe to run every startup."""
        db = self.db
        logger.info("🔧 Creating/verifying MongoDB indexes…")

        try:
            # users
            await db.users.create_index([("telegram_id", ASCENDING)], unique=True, name="users_telegram_id_uq")
            await db.users.create_index([("username", ASCENDING)], sparse=True, name="users_username")
            await db.users.create_index([("role", ASCENDING)], name="users_role")
            await db.users.create_index([("banned", ASCENDING)], name="users_banned")
            await db.users.create_index([("plan", ASCENDING)], name="users_plan")

            # tasks
            await db.tasks.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)], name="tasks_user_created")
            await db.tasks.create_index([("status", ASCENDING)], name="tasks_status")
            await db.tasks.create_index(
                [("created_at", ASCENDING)],
                expireAfterSeconds=7 * 24 * 3600,  # auto-delete after 7 days
                name="tasks_ttl",
            )

            # cloud_files
            await db.cloud_files.create_index([("file_id", ASCENDING)], unique=True, name="cloud_files_file_id_uq")
            await db.cloud_files.create_index([("user_id", ASCENDING)], name="cloud_files_user_id")
            await db.cloud_files.create_index([("visibility", ASCENDING)], name="cloud_files_visibility")
            await db.cloud_files.create_index(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0,
                sparse=True,
                name="cloud_files_expires_ttl",
            )

            # one_time_keys — TTL auto-expiration handled by MongoDB
            await db.one_time_keys.create_index([("otp", ASCENDING)], unique=True, name="otk_otp_uq")
            await db.one_time_keys.create_index([("user_id", ASCENDING)], name="otk_user_id")
            await db.one_time_keys.create_index(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0,
                name="otk_ttl",
            )

            # rclone_configs
            await db.rclone_configs.create_index([("plan", ASCENDING)], name="rclone_plan")
            await db.rclone_configs.create_index([("is_active", ASCENDING)], name="rclone_active")

            # audit_log
            await db.audit_log.create_index([("admin_id", ASCENDING), ("timestamp", DESCENDING)], name="audit_admin_ts")
            await db.audit_log.create_index(
                [("timestamp", ASCENDING)],
                expireAfterSeconds=90 * 24 * 3600,  # 90-day retention
                name="audit_ttl",
            )

            logger.info("✅ MongoDB indexes verified")
        except Exception as exc:
            # Non-fatal — application can run without optimal indexes, but warn loudly
            logger.error("⚠️  Index creation partially failed: %s", exc, exc_info=True)
