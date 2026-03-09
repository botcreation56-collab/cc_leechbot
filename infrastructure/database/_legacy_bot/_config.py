"""
bot/database/_config.py — Global bot configuration (read/write with TTLCache).
"""

import logging
import traceback
from datetime import datetime
from typing import Any, Dict, Optional

from infrastructure.database._legacy_bot._cache import _get_cache_lock, _bust_config_cache, _config_cache
from infrastructure.database._legacy_bot._connection import get_db
from infrastructure.database._legacy_bot._security_log import log_admin_action

logger = logging.getLogger("filebot.db.config")


def _get_from_settings(key: str) -> Any:
    """Get value from settings.py as fallback for missing DB config keys."""
    try:
        from config.settings import get_settings
        settings = get_settings()
        key_mapping = {
            "log_channel": "LOG_CHANNEL_ID",
            "dump_channel": "DUMP_CHANNEL_ID",
            "storage_channel": "STORAGE_CHANNEL_ID",
            "force_sub_channel": "FORCE_SUB_CHANNELS",
        }
        settings_key = key_mapping.get(key, key.upper())
        value = getattr(settings, settings_key, None)
        logger.info(f"🔍 Settings fallback: {key} -> {settings_key} = {value}")
        return value
    except Exception as e:
        logger.warning(f"⚠️ Could not get from settings: {e}")
        return None


async def _initialize_config_from_settings() -> Dict[str, Any]:
    """
    Initialize config document in MongoDB from settings.py values.
    Called when no config exists in database.
    Uses upsert so safe to call even if ensure_channel_schema() already inserted the doc.
    """
    try:
        from config.settings import get_settings, get_force_sub_channels
        settings = get_settings()

        logger.info("🔄 Initializing config from settings.py")
        initial_fields: Dict[str, Any] = {}

        if settings.LOG_CHANNEL_ID:
            initial_fields["log_channel"] = settings.LOG_CHANNEL_ID
        if settings.DUMP_CHANNEL_ID:
            initial_fields["dump_channel"] = settings.DUMP_CHANNEL_ID
        if settings.STORAGE_CHANNEL_ID:
            initial_fields["storage_channel"] = settings.STORAGE_CHANNEL_ID

        force_channels = get_force_sub_channels()
        if force_channels:
            initial_fields["force_sub_channel"] = force_channels

        db = get_db()
        await db.config.update_one(
            {"type": "global"},
            {"$setOnInsert": {
                "type": "global",
                "created_at": datetime.utcnow(),
                "initialized_from": "settings.py",
                **initial_fields,
            }},
            upsert=True,
        )

        config_doc = await db.config.find_one({"type": "global"}, {"_id": 0}) or {}
        logger.info(f"✅ Config ready with {len(config_doc)} keys")
        return config_doc

    except Exception as e:
        logger.error(f"❌ Failed to initialize config: {e}", exc_info=True)
        return {"type": "global", "created_at": datetime.utcnow()}


async def get_config(key: Optional[str] = None) -> Any:
    """
    Fetch global bot configuration from 'config' collection with 30s TTLCache.

    Args:
        key: Optional specific key to return. If None, returns full config dict.
    """
    async with _get_cache_lock():
        if "global" in _config_cache:
            config_doc = _config_cache["global"]
            if key:
                return config_doc.get(key, _get_from_settings(key))
            return config_doc

    try:
        db = get_db()
        config_doc = await db.config.find_one({"type": "global"}, {"_id": 0})
        if not config_doc:
            logger.warning("⚠️ No global config in DB — initializing from settings.py")
            config_doc = await _initialize_config_from_settings()
        config_doc = config_doc or {}

        async with _get_cache_lock():
            _config_cache["global"] = config_doc

    except Exception as e:
        logger.error(f"❌ get_config DB read failed: {e}", exc_info=True)
        if key:
            return _get_from_settings(key)
        return {}

    if key:
        value = config_doc.get(key)
        if value is None:
            value = _get_from_settings(key)
        return value

    return config_doc


async def set_config(updates: Dict[str, Any], upsert: bool = True) -> bool:
    """
    Update or insert global bot configuration in MongoDB.

    Args:
        updates: Dict of key-value pairs to set (e.g., {"log_channel": -123}).
        upsert:  If True, create if doesn't exist.
    """
    async with _get_cache_lock():
        _bust_config_cache()

    try:
        db = get_db()
        updates_to_set = {k: v for k, v in updates.items() if k != "type"}

        logger.info(f"💾 Filtered updates (without 'type'): {updates_to_set}")
        logger.info("💾 Attempting update with filter: {'type': 'global'}")

        result = await db.config.update_one(
            {"type": "global"},
            {
                "$set": updates_to_set,
                "$setOnInsert": {"type": "global", "created_at": datetime.utcnow()},
            },
            upsert=upsert,
        )

        logger.info(f"💾 MongoDB Update Result:")
        logger.info(f"   - acknowledged: {result.acknowledged}")
        logger.info(f"   - matched_count: {result.matched_count}")
        logger.info(f"   - modified_count: {result.modified_count}")
        logger.info(f"   - upserted_id: {result.upserted_id}")

        if result.acknowledged:
            logger.info("✅ Config update acknowledged by MongoDB")
            verification = await db.config.find_one({"type": "global"})
            for k, v in verification.items():
                if k != "_id":
                    logger.info(f"     {k}: {v}")

            all_updated = all(
                verification.get(k) == v for k, v in updates_to_set.items()
            )
            if all_updated:
                logger.info("✅ All updates verified in database")
                return True
            else:
                logger.error("❌ Some updates not found in verification")
                for k, v in updates_to_set.items():
                    db_val = verification.get(k)
                    if db_val != v:
                        logger.error(f"   Mismatch: {k} - Expected: {v}, Got: {db_val}")
                return False
        else:
            logger.error("❌ Update not acknowledged by MongoDB")
            return False

    except Exception as e:
        logger.error(f"❌ set_config failed: {e}", exc_info=True)
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False


async def update_config(updates: Dict[str, Any], admin_id: int = 0) -> bool:
    """Alias for set_config (legacy support). Logs admin action."""
    logger.info("🔍 update_config called")
    logger.info(f"   Admin ID: {admin_id}")
    logger.info(f"   Updates: {updates}")

    success = await set_config(updates, upsert=True)

    if success and admin_id:
        try:
            await log_admin_action(admin_id, "config_updated", {"keys": list(updates.keys())})
            logger.info(f"✅ Admin action logged for {admin_id}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to log admin action: {e}")

    logger.info(f"🔍 update_config returning: {success}")
    return success


def get_config_sync(key: Optional[str] = None) -> Any:
    """
    Synchronous version of get_config.
    ONLY checks TTLCache or settings.py fallback.
    Does NOT trigger a database read.
    """
    if "global" in _config_cache:
        config_doc = _config_cache["global"]
        if key:
            return config_doc.get(key, _get_from_settings(key))
        return config_doc

    if key:
        return _get_from_settings(key)
    return {}
