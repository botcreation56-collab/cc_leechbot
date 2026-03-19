"""
database/auth.py — One-time key (OTK) operations for web authentication.
"""

import logging
from datetime import datetime
from typing import Optional

from database.connection import get_db

logger = logging.getLogger("filebot.db.auth")


async def create_one_time_key(
    user_id: int, key: str, expires_at: datetime, purpose: str = "stream"
) -> bool:
    """Create one-time key for web auth."""
    try:
        db = get_db()
        await db.one_time_keys.delete_many({"user_id": user_id, "purpose": purpose})

        key_doc = {
            "user_id": user_id,
            "otp": key,
            "expires_at": expires_at,
            "used": False,
            "purpose": purpose,
            "created_at": datetime.utcnow(),
        }

        await db.one_time_keys.insert_one(key_doc)
        logger.info(f"✅ One-time key created for user: {user_id} (purpose={purpose})")
        return True

    except Exception as e:
        logger.error(f"❌ Create one-time key failed: {e}", exc_info=True)
        return False


async def verify_one_time_key(user_id: int, key: str) -> bool:
    """Verify and mark one-time key as used."""
    try:
        db = get_db()
        key_doc = await db.one_time_keys.find_one(
            {
                "user_id": user_id,
                "otp": key,
                "used": False,
                "expires_at": {"$gt": datetime.utcnow()},
            }
        )

        if key_doc is None:
            logger.warning(f"❌ Invalid or expired key for user: {user_id}")
            return False

        await db.one_time_keys.update_one(
            {"_id": key_doc["_id"]},
            {"$set": {"used": True}},
        )
        logger.info(f"✅ One-time key verified for user: {user_id}")
        return True

    except Exception as e:
        logger.error(f"❌ Verify one-time key failed: {e}", exc_info=True)
        return False
