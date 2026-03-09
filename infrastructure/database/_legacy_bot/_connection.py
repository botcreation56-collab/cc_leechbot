"""
bot/database/_connection.py — MongoDB connection lifecycle (CONSOLIDATED).

IMPORTANT: This module no longer opens a second connection pool.
Instead, it shares the single AsyncIOMotorDatabase created by
infrastructure.database.connection.DatabaseConnection (via main.py).

Public API (unchanged — all callers continue to work):
  init_db()   → registers the shared DB handle
  close_db()  → no-op (lifecycle owned by DatabaseConnection)
  get_db()    → returns the shared DB handle
"""

import logging
from typing import Optional
import datetime
from datetime import datetime as dt_class # Fallback for `datetime.utcnow()`
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("filebot.db.connection")

MONGODB_TIMEOUT = 30  # seconds (kept for backward compatiblity)

# Shared DB handle — set by _set_shared_db() on startup, never a separate pool.
_db: Optional[AsyncIOMotorDatabase] = None


def _set_shared_db(db: AsyncIOMotorDatabase) -> None:
    """Called once by main.py after DatabaseConnection.connect() to share the pool."""
    global _db
    _db = db
    logger.info("✅ Legacy DB layer is now using the shared MongoDB connection pool")


async def init_db() -> AsyncIOMotorDatabase:
    """
    No-op initialiser retained for backward compatibility.
    The real connection is established via infrastructure.database.connection.DatabaseConnection
    and injected with _set_shared_db() before this is called by any handler.
    """
    if _db is None:
        raise RuntimeError(
            "Shared DB not set. Call _set_shared_db(db) from main.py after "
            "DatabaseConnection.connect() before starting the bot."
        )
    await _run_migrations(_db)
    logger.info("✅ init_db() ack — using shared pool, no second client opened")
    return _db


async def _run_migrations(db: AsyncIOMotorDatabase) -> None:
    """Run one-time startup migrations (schema ensures)."""
    try:
        await ensure_channel_schema(db)
        await migrate_flat_to_nested(db)
    except Exception as e:
        logger.warning("⚠️ Migration step failed (non-fatal): %s", e)


async def close_db() -> None:
    """
    No-op — lifecycle is owned by DatabaseConnection in infrastructure layer.
    Kept for backward compatibility.
    """
    logger.info("ℹ️ close_db() called — lifecycle delegated to DatabaseConnection")


def get_db() -> AsyncIOMotorDatabase:
    """Return the shared database instance."""
    if _db is None:
        raise RuntimeError("Database not initialized. Ensure _set_shared_db() was called at startup.")
    return _db



async def _safe_create_index(collection, keys, **kwargs) -> None:
    """Create an index, silently skipping if an equivalent one already exists.

    MongoDB error code 85 (IndexOptionsConflict) means the new DatabaseConnection
    layer already created this index with an explicit name.  We skip it rather
    than crashing so the legacy layer stays compatible with the new layer.
    """
    from pymongo.errors import OperationFailure
    try:
        await collection.create_index(keys, **kwargs)
    except OperationFailure as exc:
        if exc.code == 85:          # IndexOptionsConflict — already exists, safe to skip
            logger.debug("⏭ Index already exists (skipping): %s %s", keys, exc.details)
        else:
            raise


async def create_indexes(db: AsyncIOMotorDatabase) -> None:
    """Create all necessary MongoDB indexes.

    Each call is wrapped via _safe_create_index so that indexes already created
    by the new DatabaseConnection layer (with explicit names) are skipped rather
    than causing an IndexOptionsConflict crash.
    """
    logger.info("🔄 Creating database indexes with Motor syntax...")

    await _safe_create_index(db.users, [("telegram_id", 1)], unique=True)
    await _safe_create_index(db.users, [("plan", 1)])
    await _safe_create_index(db.users, [("banned", 1)])
    await _safe_create_index(db.users, [("created_at", -1)])
    await _safe_create_index(db.users, [("role", 1)])
    logger.info("✅ Users collection indexes created")

    await _safe_create_index(db.tasks, [("task_id", 1)], unique=True)
    await _safe_create_index(db.tasks, [("user_id", 1)])
    await _safe_create_index(db.tasks, [("status", 1)])
    logger.info("✅ Tasks collection indexes created")

    await _safe_create_index(db.cloud_files, [("user_id", 1)])
    await _safe_create_index(db.cloud_files, [("expiry_date", 1)])
    logger.info("✅ Cloud files collection indexes created")

    await _safe_create_index(db.one_time_keys, [("user_id", 1)])
    await _safe_create_index(db.one_time_keys, [("expires_at", 1)], expireAfterSeconds=0)

    await _safe_create_index(db.admin_logs, [("admin_id", 1)])
    await _safe_create_index(db.admin_logs, [("user_id", 1)])
    await _safe_create_index(db.admin_logs, [("timestamp", -1)])

    await _safe_create_index(db.rclone_configs, [("service", 1)])
    await _safe_create_index(db.rclone_configs, [("plan", 1)])

    await _safe_create_index(db.config, [("type", 1)], unique=True)

    await _safe_create_index(db.broadcasts, [("status", 1)])
    await _safe_create_index(db.broadcasts, [("created_at", -1)])

    await _safe_create_index(db.chatbox, [("user_id", 1)])
    await _safe_create_index(db.chatbox, [("timestamp", -1)])

    await _safe_create_index(db.actions, [("admin_id", 1)])
    await _safe_create_index(db.actions, [("timestamp", -1)])

    await _safe_create_index(db.sessions, [("token", 1)], unique=True)
    await _safe_create_index(db.sessions, [("expires_at", 1)])
    await _safe_create_index(db.sessions, [("user_id", 1)])

    logger.info("✅ All database indexes created successfully")


async def ensure_channel_schema(db: AsyncIOMotorDatabase) -> None:
    """Ensure global config document exists with proper channel structure."""
    try:
        logger.info("🔄 Ensuring global config document exists...")
        existing = await db.config.find_one({"type": "global"})

        default_channels = {"log": None, "dump": None, "storage": None, "force_sub": []}

        if not existing:
            logger.info("📝 Creating initial global config document with nested channel structure")
            await db.config.insert_one({
                "type": "global",
                "channels": default_channels,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            })
            logger.info("✅ Global config document created")
        else:
            # Fix: If channels is missing OR explicitly null, reset to default structure
            if not existing.get("channels"):
                logger.info("📝 Fixing 'channels' structure (null or missing) in global config")
                await db.config.update_one(
                    {"type": "global"},
                    {"$set": {
                        "channels": default_channels,
                        "updated_at": datetime.utcnow()
                    }},
                )
                logger.info("✅ Channels structure repaired")
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
