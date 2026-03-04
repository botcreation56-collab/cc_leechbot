"""
bot/database/_channels.py — Channel configuration storage (log, dump, storage, force_sub).

Covers:
  - get_channel_config / get_channel_id / get_channel_metadata
  - get_force_sub_channels / add_force_sub_channel / remove_force_sub_channel
  - update_force_sub_metadata
  - set_channel_config / set_storage_channel / set_dump_channel
  - remove_channel_config
  - get_chatbox_messages / add_chatbox_message
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from infrastructure.database._legacy_bot._config import get_config
from infrastructure.database._legacy_bot._connection import get_db
from infrastructure.database._legacy_bot._security_log import log_admin_action

logger = logging.getLogger("filebot.db.channels")


async def get_channel_config(channel_type: str) -> Optional[Any]:
    """
    Get channel configuration by type (log, dump, storage, force_sub).

    Returns nested structure with fallback to flat structure:
      - Single channels: {"id": -100123, "metadata": {...}}
      - force_sub:       [{"id": -100123, "metadata": {...}}, ...]
      - None if not configured
    """
    try:
        config = await get_config()

        channels = config.get("channels", {})
        if channels and channel_type in channels:
            logger.info(f"📡 Found {channel_type} in nested structure")
            return channels[channel_type]

        flat_key = f"{channel_type}_channel"
        channel_id = config.get(flat_key)
        if not channel_id:
            logger.info(f"⚠️ Channel {channel_type} not configured")
            return None

        logger.info(f"📡 Found {channel_type} in flat structure, converting...")

        if channel_type == "force_sub":
            if not isinstance(channel_id, list):
                channel_id = [channel_id] if channel_id else []
            metadata_dict = config.get(f"{flat_key}_metadata", {})
            return [
                {"id": ch_id, "metadata": metadata_dict.get(str(ch_id), {})}
                for ch_id in channel_id
            ]

        metadata = config.get(f"{flat_key}_metadata", {})
        return {"id": channel_id, "metadata": metadata}

    except Exception as e:
        logger.error(f"❌ Error getting channel config: {e}", exc_info=True)
        return None


async def get_channel_id(channel_type: str) -> Optional[int]:
    """Get channel ID by type."""
    try:
        channel_config = await get_channel_config(channel_type)
        if not channel_config:
            return None
        if isinstance(channel_config, dict):
            return channel_config.get("id")
        return None
    except Exception as e:
        logger.error(f"❌ Error getting channel ID: {e}", exc_info=True)
        return None


async def get_channel_metadata(
    channel_type: str, channel_id: Optional[int] = None
) -> Optional[Dict]:
    """Get channel metadata by type."""
    try:
        channel_config = await get_channel_config(channel_type)
        if not channel_config:
            return None
        if isinstance(channel_config, dict):
            return channel_config.get("metadata", {})
        if isinstance(channel_config, list) and channel_id:
            for ch in channel_config:
                if ch.get("id") == channel_id:
                    return ch.get("metadata", {})
        return None
    except Exception as e:
        logger.error(f"❌ Error getting channel metadata: {e}", exc_info=True)
        return None


async def get_force_sub_channels() -> List[Dict[str, Any]]:
    """Get all force subscribe channels."""
    try:
        force_config = await get_channel_config("force_sub")
        if not force_config:
            return []
        if isinstance(force_config, list):
            return force_config
        return [force_config] if force_config else []
    except Exception as e:
        logger.error(f"❌ Error getting force sub channels: {e}", exc_info=True)
        return []


async def set_channel_config(
    channel_type: str, channel_id: int, metadata: Dict[str, Any], admin_id: int = 0
) -> bool:
    """Set channel configuration in nested structure."""
    try:
        logger.info(f"💾 Setting {channel_type} channel: {channel_id}")
        db = get_db()
        result = await db.config.update_one(
            {"type": "global"},
            {"$set": {
                f"channels.{channel_type}": {"id": channel_id, "metadata": metadata},
                "updated_at": datetime.utcnow(),
            }},
            upsert=True,
        )
        if result.acknowledged:
            logger.info(f"✅ Channel {channel_type} saved to nested structure")
            if admin_id:
                await log_admin_action(admin_id, f"set_{channel_type}_channel", {
                    "channel_id": channel_id, "title": metadata.get("title"),
                })
            return True
        return False
    except Exception as e:
        logger.error(f"❌ Error setting channel config: {e}", exc_info=True)
        return False


async def add_force_sub_channel(
    channel_id: int, metadata: Dict[str, Any], admin_id: int = 0
) -> bool:
    """Add a force subscribe channel to the array."""
    try:
        logger.info(f"➕ Adding force sub channel: {channel_id}")
        db = get_db()
        existing = await get_force_sub_channels()

        if any(ch.get("id") == channel_id for ch in existing):
            logger.warning(f"⚠️ Channel {channel_id} already in force_sub list")
            return False

        existing.append({"id": channel_id, "metadata": metadata})
        result = await db.config.update_one(
            {"type": "global"},
            {"$set": {"channels.force_sub": existing, "updated_at": datetime.utcnow()}},
            upsert=True,
        )
        if result.acknowledged:
            logger.info(f"✅ Force sub channel added: {channel_id}")
            if admin_id:
                await log_admin_action(admin_id, "add_force_sub_channel", {
                    "channel_id": channel_id, "title": metadata.get("title"),
                })
            return True
        return False
    except Exception as e:
        logger.error(f"❌ Error adding force sub channel: {e}", exc_info=True)
        return False


async def remove_force_sub_channel(channel_id: int, admin_id: int = 0) -> bool:
    """Remove a force subscribe channel from the array."""
    try:
        logger.info(f"🗑️ Removing force sub channel: {channel_id}")
        db = get_db()
        existing = await get_force_sub_channels()
        filtered = [ch for ch in existing if ch.get("id") != channel_id]
        if len(filtered) == len(existing):
            logger.warning(f"⚠️ Channel {channel_id} not found in force_sub list")
            return False
        result = await db.config.update_one(
            {"type": "global"},
            {"$set": {"channels.force_sub": filtered, "updated_at": datetime.utcnow()}},
        )
        if result.acknowledged:
            logger.info(f"✅ Force sub channel removed: {channel_id}")
            if admin_id:
                await log_admin_action(admin_id, "remove_force_sub_channel", {"channel_id": channel_id})
            return True
        return False
    except Exception as e:
        logger.error(f"❌ Error removing force sub channel: {e}", exc_info=True)
        return False


async def update_force_sub_metadata(
    channel_id: int, updates: Dict[str, Any], admin_id: int = 0
) -> bool:
    """Update metadata for a specific force subscribe channel."""
    try:
        logger.info(f"🔄 Updating force sub metadata: {channel_id}")
        existing = await get_force_sub_channels()
        updated = False
        for ch in existing:
            if ch.get("id") == channel_id:
                ch["metadata"].update(updates)
                updated = True
                break
        if not updated:
            logger.warning(f"⚠️ Channel {channel_id} not found")
            return False
        db = get_db()
        result = await db.config.update_one(
            {"type": "global"},
            {"$set": {"channels.force_sub": existing, "updated_at": datetime.utcnow()}},
        )
        if result.acknowledged:
            logger.info(f"✅ Force sub metadata updated: {channel_id}")
            if admin_id:
                await log_admin_action(admin_id, "update_force_sub_metadata", {
                    "channel_id": channel_id, "updates": updates,
                })
            return True
        return False
    except Exception as e:
        logger.error(f"❌ Error updating force sub metadata: {e}", exc_info=True)
        return False


async def remove_channel_config(channel_type: str, admin_id: int = 0) -> bool:
    """Remove a channel configuration (log, dump, storage)."""
    try:
        logger.info(f"🗑️ Removing {channel_type} channel")
        db = get_db()
        result = await db.config.update_one(
            {"type": "global"},
            {"$unset": {f"channels.{channel_type}": ""}, "$set": {"updated_at": datetime.utcnow()}},
        )
        if result.acknowledged:
            logger.info(f"✅ {channel_type} channel removed")
            if admin_id:
                await log_admin_action(admin_id, f"remove_{channel_type}_channel", {})
            return True
        return False
    except Exception as e:
        logger.error(f"❌ Error removing channel: {e}", exc_info=True)
        return False


async def set_storage_channel(
    channel_id: int, metadata: Optional[Dict[str, Any]] = None, admin_id: int = 0
) -> bool:
    """Configure main storage channel."""
    return await set_channel_config("storage", channel_id, metadata or {}, admin_id)


async def set_dump_channel(
    channel_id: int, metadata: Optional[Dict[str, Any]] = None, admin_id: int = 0
) -> bool:
    """Configure dump channel for temporary files."""
    return await set_channel_config(
        "dump", channel_id, metadata or {"description": "Temporary file storage"}, admin_id
    )


async def get_storage_channel() -> Optional[Dict[str, Any]]:
    """Get configured storage channel."""
    try:
        return await get_channel_config("storage")
    except Exception as e:
        logger.error(f"❌ Get storage channel failed: {e}", exc_info=True)
        return None


async def get_dump_channel() -> Optional[Dict[str, Any]]:
    """Get configured dump channel."""
    try:
        return await get_channel_config("dump")
    except Exception as e:
        logger.error(f"❌ Get dump channel failed: {e}", exc_info=True)
        return None


# ============================================================
# CHATBOX
# ============================================================

async def get_chatbox_messages(
    user_id: Optional[int] = None, limit: int = 50
) -> List[Dict[str, Any]]:
    """Get chatbox messages."""
    try:
        db = get_db()
        filter_dict: Dict[str, Any] = {}
        if user_id is not None:
            filter_dict["user_id"] = user_id
        messages = await db.chatbox.find(filter_dict).sort("timestamp", -1).limit(limit).to_list(length=limit)
        logger.info(f"✅ Retrieved {len(messages) if messages else 0} chatbox messages")
        return messages if messages is not None else []
    except Exception as e:
        logger.error(f"❌ Get chatbox messages failed: {e}", exc_info=True)
        return []


async def add_chatbox_message(
    user_id: int, message: str, sender_type: str = "user"
) -> bool:
    """Add chatbox message."""
    try:
        db = get_db()
        await db.chatbox.insert_one({
            "user_id": user_id,
            "message": message,
            "sender_type": sender_type,
            "timestamp": datetime.utcnow(),
            "read": False,
        })
        logger.info(f"✅ Chatbox message added for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Add chatbox message failed: {e}", exc_info=True)
        return False
