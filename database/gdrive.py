"""
database/gdrive.py — Google Drive configuration storage.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from database.connection import get_db

logger = logging.getLogger("filebot.db.gdrive")


async def save_gdrive_config(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    root_folder_id: str = None,
    admin_id: int = 0,
) -> bool:
    """Save or update GDrive configuration (encrypted)."""
    try:
        from bot.utils import encrypt_credentials

        db = get_db()

        encrypted_creds = encrypt_credentials(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            }
        )

        config_doc = {
            "type": "gdrive",
            "client_id": encrypted_creds,
            "root_folder_id": root_folder_id or "",
            "updated_at": datetime.utcnow(),
            "updated_by": admin_id,
            "is_active": True,
        }

        await db.gdrive_config.update_one(
            {"type": "gdrive"},
            {"$set": config_doc},
            upsert=True,
        )

        logger.info("✅ GDrive config saved")
        return True

    except Exception as e:
        logger.error(f"❌ Save GDrive config failed: {e}", exc_info=True)
        return False


async def get_gdrive_config() -> Optional[Dict[str, Any]]:
    """Get GDrive configuration (decrypted)."""
    try:
        from bot.utils import decrypt_credentials

        db = get_db()
        config = await db.gdrive_config.find_one({"type": "gdrive", "is_active": True})

        if not config:
            logger.info("GDrive: No config found in database")
            return None

        encrypted_creds = config.get("client_id")
        if not encrypted_creds:
            return None

        try:
            creds_dict = decrypt_credentials(encrypted_creds)
        except Exception as e:
            logger.error(f"Failed to decrypt GDrive credentials: {e}")
            return None

        return {
            "client_id": creds_dict.get("client_id"),
            "client_secret": creds_dict.get("client_secret"),
            "refresh_token": creds_dict.get("refresh_token"),
            "root_folder_id": config.get("root_folder_id", ""),
            "updated_at": config.get("updated_at"),
        }

    except Exception as e:
        logger.error(f"❌ Get GDrive config failed: {e}", exc_info=True)
        return None


async def delete_gdrive_config() -> bool:
    """Delete GDrive configuration."""
    try:
        db = get_db()
        result = await db.gdrive_config.delete_one({"type": "gdrive"})
        logger.info(f"✅ GDrive config deleted: {result.deleted_count}")
        return result.deleted_count > 0
    except Exception as e:
        logger.error(f"❌ Delete GDrive config failed: {e}")
        return False


async def get_gdrive_folder_ids() -> Optional[Dict[str, str]]:
    """Get stored GDrive folder IDs (temp, free, pro)."""
    try:
        db = get_db()
        config = await db.gdrive_config.find_one({"type": "gdrive"})

        if not config:
            return None

        return {
            "temp": config.get("folder_temp"),
            "free": config.get("folder_free"),
            "pro": config.get("folder_pro"),
            "root": config.get("root_folder_id"),
        }

    except Exception as e:
        logger.error(f"❌ Get GDrive folder IDs failed: {e}")
        return None


async def save_gdrive_folder_ids(
    temp_id: str = None,
    free_id: str = None,
    pro_id: str = None,
) -> bool:
    """Save GDrive folder IDs after setup."""
    try:
        db = get_db()

        updates = {}
        if temp_id:
            updates["folder_temp"] = temp_id
        if free_id:
            updates["folder_free"] = free_id
        if pro_id:
            updates["folder_pro"] = pro_id

        if updates:
            await db.gdrive_config.update_one(
                {"type": "gdrive"},
                {"$set": {**updates, "folders_created_at": datetime.utcnow()}},
            )

        logger.info("✅ GDrive folder IDs saved")
        return True

    except Exception as e:
        logger.error(f"❌ Save GDrive folder IDs failed: {e}")
        return False
