"""
bot/database/_connection.py — MongoDB connection lifecycle and index creation.

Responsibilities:
  - init_db()      : connect + create indexes + run migrations
  - close_db()     : graceful disconnect
  - get_db()       : return cached AsyncIOMotorDatabase instance
  - create_indexes : all collection index definitions
  - ensure_channel_schema / migrate_flat_to_nested : startup migrations
"""

import logging
import os
from datetime import datetime
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

logger = logging.getLogger("filebot.db.connection")

MONGODB_TIMEOUT = 30  # seconds

_db_client: Optional[AsyncIOMotorClient] = None
_db: Optional[AsyncIOMotorDatabase] = None


async def init_db() -> AsyncIOMotorDatabase:
    """Initialize MongoDB connection and create indexes."""
    global _db_client, _db

    try:
        mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
        db_name = os.getenv("MONGODB_DB", "filebot_production")

        max_pool_size = int(os.getenv("MONGODB_MAX_POOL_SIZE", "200"))
        min_pool_size = int(os.getenv("MONGODB_MIN_POOL_SIZE", "20"))

        _db_client = AsyncIOMotorClient(
            mongo_uri,
            maxPoolSize=max_pool_size,
            minPoolSize=min_pool_size,
            maxIdleTimeMS=45000,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=10000,
            retryWrites=True,
        )

        await _db_client.admin.command("ping")
        _db = _db_client[db_name]

        await create_indexes(_db)
        await ensure_channel_schema(_db)
        await migrate_flat_to_nested(_db)

        logger.info(f"✅ MongoDB connected: {db_name}")
        return _db

    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}", exc_info=True)
        raise


async def close_db() -> None:
    """Close MongoDB connection."""
    global _db_client
    if _db_client is not None:
        _db_client.close()
        logger.info("✅ MongoDB connection closed")


def get_db() -> AsyncIOMotorDatabase:
    """Return database instance (must call init_db first)."""
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db


async def create_indexes(db: AsyncIOMotorDatabase) -> None:
    """Create all necessary MongoDB indexes."""
    try:
        logger.info("🔄 Creating database indexes with Motor syntax...")

        await db.users.create_index([("telegram_id", 1)], unique=True)
        await db.users.create_index([("plan", 1)])
        await db.users.create_index([("banned", 1)])
        await db.users.create_index([("created_at", -1)])
        await db.users.create_index([("role", 1)])
        logger.info("✅ Users collection indexes created")

        await db.tasks.create_index([("task_id", 1)], unique=True)
        await db.tasks.create_index([("user_id", 1)])
        await db.tasks.create_index([("status", 1)])
        logger.info("✅ Tasks collection indexes created")

        await db.cloud_files.create_index([("user_id", 1)])
        await db.cloud_files.create_index([("expiry_date", 1)])
        logger.info("✅ Cloud files collection indexes created")

        await db.one_time_keys.create_index([("user_id", 1)])
        await db.one_time_keys.create_index(
            [("expires_at", 1)],
            expireAfterSeconds=0,
        )

        await db.admin_logs.create_index([("admin_id", 1)])
        await db.admin_logs.create_index([("user_id", 1)])
        await db.admin_logs.create_index([("timestamp", -1)])

        await db.rclone_configs.create_index([("service", 1)])
        await db.rclone_configs.create_index([("plan", 1)])

        await db.config.create_index([("type", 1)], unique=True)

        await db.broadcasts.create_index([("status", 1)])
        await db.broadcasts.create_index([("created_at", -1)])

        await db.chatbox.create_index([("user_id", 1)])
        await db.chatbox.create_index([("timestamp", -1)])

        await db.actions.create_index([("admin_id", 1)])
        await db.actions.create_index([("timestamp", -1)])

        await db.sessions.create_index([("token", 1)], unique=True)
        await db.sessions.create_index([("expires_at", 1)])
        await db.sessions.create_index([("user_id", 1)])
        logger.info("✅ All database indexes created successfully")

    except Exception as e:
        logger.error(f"❌ Index creation failed: {e}", exc_info=True)
        raise


async def ensure_channel_schema(db: AsyncIOMotorDatabase) -> None:
    """Ensure global config document exists with proper channel structure."""
    try:
        logger.info("🔄 Ensuring global config document exists...")
        existing = await db.config.find_one({"type": "global"})

        if not existing:
            logger.info("📝 Creating initial global config document with nested channel structure")
            await db.config.insert_one({
                "type": "global",
                "channels": {"log": None, "dump": None, "storage": None, "force_sub": []},
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            })
            logger.info("✅ Global config document created")
        else:
            if "channels" not in existing:
                logger.info("📝 Adding 'channels' structure to existing global config")
                await db.config.update_one(
                    {"type": "global"},
                    {"$set": {"channels": {
                        "log": existing.get("channels", {}).get("log"),
                        "dump": existing.get("channels", {}).get("dump"),
                        "storage": existing.get("channels", {}).get("storage"),
                        "force_sub": existing.get("channels", {}).get("force_sub", []),
                    }}},
                )
                logger.info("✅ Channels structure added to global config")
            else:
                logger.info("✅ Global config document already has proper structure")

    except Exception as e:
        logger.error(f"❌ Error ensuring channel schema: {e}", exc_info=True)
        raise


async def migrate_flat_to_nested(db: AsyncIOMotorDatabase) -> None:
    """Migrate legacy flat channel configuration to nested structure."""
    try:
        logger.info("🔄 Checking for legacy flat channel configs...")
        config = await db.config.find_one({"type": "global"})
        if not config:
            logger.info("⚠️ No global config found, skipping migration")
            return

        updates: dict = {}
        migrated_any = False

        for channel_type in ["log", "dump", "storage"]:
            flat_key = f"{channel_type}_channel"
            flat_id = config.get(flat_key)
            nested_val = config.get("channels", {}).get(channel_type)

            if flat_id and not nested_val:
                logger.info(f"📋 Migrating {channel_type}_channel: {flat_id}")
                metadata = config.get(f"{flat_key}_metadata", {})
                updates[f"channels.{channel_type}"] = {"id": flat_id, "metadata": metadata}
                updates[f"$unset.{flat_key}"] = ""
                updates[f"$unset.{flat_key}_metadata"] = ""
                migrated_any = True

        flat_force_sub = config.get("force_sub_channel")
        nested_force_sub = config.get("channels", {}).get("force_sub")

        if flat_force_sub and not nested_force_sub:
            logger.info("📋 Migrating force_sub_channel")
            if not isinstance(flat_force_sub, list):
                flat_force_sub = [flat_force_sub]
            metadata_dict = config.get("force_sub_channel_metadata", {})
            force_sub_array = [
                {"id": ch_id, "metadata": metadata_dict.get(str(ch_id), {})}
                for ch_id in flat_force_sub
            ]
            updates["channels.force_sub"] = force_sub_array
            updates["$unset.force_sub_channel"] = ""
            updates["$unset.force_sub_channel_metadata"] = ""
            migrated_any = True

        if migrated_any:
            logger.info("💾 Applying migration updates...")
            set_updates = {k: v for k, v in updates.items() if not k.startswith("$unset")}
            unset_keys = {k.replace("$unset.", ""): v for k, v in updates.items() if k.startswith("$unset")}
            update_doc: dict = {}
            if set_updates:
                update_doc["$set"] = set_updates
                update_doc["$set"]["updated_at"] = datetime.utcnow()
            if unset_keys:
                update_doc["$unset"] = unset_keys
            result = await db.config.update_one({"type": "global"}, update_doc)
            if result.modified_count > 0:
                logger.info(f"✅ Migration completed: {result.modified_count} document(s) updated")
            else:
                logger.warning("⚠️ Migration ran but no documents were modified")
        else:
            logger.info("✅ No legacy flat configs to migrate")

    except Exception as e:
        logger.error(f"❌ Error during migration: {e}", exc_info=True)
        # Don't raise — migration failure must not prevent startup
