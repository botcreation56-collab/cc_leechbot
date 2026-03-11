"""
bot/database/_auth.py — One-time key (OTK) operations for web authentication.
"""

import logging
from datetime import datetime
from typing import Optional

from infrastructure.database._legacy_bot._connection import get_db

logger = logging.getLogger("filebot.db.auth")


async def create_one_time_key(user_id: int, key: str, expires_at: datetime, purpose: str = "stream") -> bool:
    """Create one-time key for web auth.
    
    Args:
        user_id: Telegram user ID.
        key: The OTP value to store.
        expires_at: Expiry timestamp.
        purpose: Token namespace. Use 'magic_login', 'stream', or 'priority_verify'.
                 Tokens are only valid for the endpoint that matches their purpose.
    """
    try:
        db = get_db()
        # Only delete old keys of the SAME purpose — avoids wiping stream tokens
        # when a magic-login token is issued for the same user.
        await db.one_time_keys.delete_many({"user_id": user_id, "purpose": purpose})

        key_doc = {
            "user_id": user_id,
            "otp": key,       # Store as "otp" — must match verify_one_time_key query
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
        key_doc = await db.one_time_keys.find_one({
            "user_id": user_id,
            "otp": key,
            "used": False,
            "expires_at": {"$gt": datetime.utcnow()},
        })

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
