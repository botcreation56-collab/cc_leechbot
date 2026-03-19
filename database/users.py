"""
database/users.py — User CRUD operations.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from database.connection import get_db
from database.cache import _get_cache_lock, _bust_user_cache, _user_cache
from database.security_log import log_admin_action

logger = logging.getLogger("filebot.db.users")


async def create_user(
    user_id: int, first_name: str, username: str = ""
) -> Dict[str, Any]:
    """Create a new user in MongoDB with complete profile."""
    try:
        logger.info(f"🔄 create_user: user_id={user_id}, first_name={first_name}")
        db = get_db()
        if db is None:
            logger.error("❌ get_db() returned None - database not initialized")
            return {}

        now = datetime.utcnow()

        existing = await db.users.find_one({"telegram_id": user_id})
        if existing:
            try:
                from config.settings import get_admin_ids

                if user_id in get_admin_ids() and existing.get("role") != "admin":
                    await db.users.update_one(
                        {"telegram_id": user_id}, {"$set": {"role": "admin"}}
                    )
                    existing["role"] = "admin"
                    logger.info(
                        f"⬆️ Promoted existing user {user_id} to admin based on settings"
                    )
            except Exception:
                pass
            return existing

        try:
            from config.settings import get_admin_ids

            is_admin = user_id in get_admin_ids()
        except Exception:
            is_admin = False

        user_doc = {
            "telegram_id": user_id,
            "first_name": first_name or "User",
            "username": username or "",
            "plan": "free",
            "storage_limit": 5 * 1024 * 1024 * 1024,
            "used_storage": 0,
            "daily_limit": 5,
            "daily_used": 0,
            "parallel_slots": 1,
            "banned": False,
            "ban_reason": None,
            "notifications_enabled": True,
            "role": "admin" if is_admin else "user",
            "settings": {
                "prefix": "",
                "suffix": "",
                "mode": "video",
                "metadata": {},
                "destination_channel": None,
                "destination_metadata": {},
                "remove_words": [],
                "thumbnail": "auto",
            },
            "files_processed": 0,
            "created_at": now,
            "updated_at": now,
            "last_activity": now,
        }

        await db.users.insert_one(user_doc)
        logger.info(f"✅ User created successfully: {user_id}")
        return user_doc

    except Exception as e:
        logger.error(f"❌ create_user FAILED for {user_id}: {e}", exc_info=True)
        raise


async def get_user(user_id: int) -> dict | None:
    """Fetch user by telegram_id with async-safe 60s TTLCache."""
    async with _get_cache_lock():
        if user_id in _user_cache:
            return _user_cache[user_id]

    try:
        db = get_db()
        if db is None:
            logger.error(f"❌ get_db() returned None during get_user({user_id})")
            return None

        user = await db.users.find_one({"telegram_id": user_id})

        if user:
            async with _get_cache_lock():
                _user_cache[user_id] = user

        return user

    except Exception as e:
        logger.error(f"❌ get_user({user_id}) failed: {e}", exc_info=True)
        raise


async def get_all_users(
    query: Dict[str, Any] = None,
    limit: int = 100,
    banned_only: bool = False,
    page: int = 0,
) -> tuple[List[Dict], int]:
    """Fetch all users with pagination and optional filters."""
    try:
        db = get_db()
        if query is None:
            query = {}
        if banned_only:
            query["banned"] = True

        skip = page * limit
        users = (
            await db.users.find(query)
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
            .to_list(length=None)
        )
        if not isinstance(users, list):
            users = list(users) if users else []

        total = await db.users.count_documents(query)
        logger.debug(f"Fetched {len(users)} users (page {page}, total {total})")
        return (users, total)

    except Exception as e:
        logger.error(f"❌ get_all_users failed: {e}")
        return ([], 0)


async def get_banned_users(limit: int = 1000) -> List[Dict[str, Any]]:
    """Get all banned users."""
    try:
        db = get_db()
        users = await (
            db.users.find({"banned": True})
            .sort("banned_at", -1)
            .limit(limit)
            .to_list(length=limit)
        )
        logger.info(f"✅ Retrieved {len(users) if users else 0} banned users")
        return users if users is not None else []
    except Exception as e:
        logger.error(f"❌ Get banned users failed: {e}", exc_info=True)
        return []


async def update_user(
    user_id: int, updates: Dict[str, Any], admin_id: Optional[int] = None
) -> bool:
    """Update user document — supports nested field updates with dot notation."""
    try:
        db = get_db()

        async with _get_cache_lock():
            _bust_user_cache(user_id)

        updates["updated_at"] = datetime.utcnow()
        logger.debug(f"📝 Updating user {user_id}: {list(updates.keys())[:5]}")

        if "_id" in updates:
            del updates["_id"]

        ALLOWED_USER_KEYS = {
            "first_name",
            "username",
            "plan",
            "storage_limit",
            "used_storage",
            "daily_limit",
            "daily_used",
            "parallel_slots",
            "banned",
            "ban_reason",
            "banned_at",
            "banned_by",
            "notifications_enabled",
            "role",
            "files_processed",
            "settings",
            "validity_from",
            "validity_to",
            "requested_fsub",
            "storage_msg_id",
        }

        for key in list(updates.keys()):
            base_key = key.split(".")[0]
            if base_key not in ALLOWED_USER_KEYS:
                logger.warning(f"⚠️ Blocked unpermitted update key: {key}")
                del updates[key]

        if not updates:
            logger.info(f"ℹ️ User {user_id} - no valid changes provided")
            return True

        result = await db.users.update_one({"telegram_id": user_id}, {"$set": updates})
        logger.info(
            f"   MongoDB result: matched={result.matched_count}, modified={result.modified_count}"
        )

        if admin_id is not None:
            try:
                await log_admin_action(admin_id, "user_updated", {"user_id": user_id})
            except Exception as e:
                logger.warning(f"⚠️ Failed to log admin action: {e}")

        if result.matched_count == 0:
            logger.error(f"❌ User {user_id} not found in database")
            return False

        logger.info(f"✅ User {user_id} updated successfully")
        return True

    except Exception as e:
        logger.error(f"❌ Update user {user_id} failed: {e}", exc_info=True)
        return False


async def ban_user(
    user_id: int, reason: str = "", admin_id: Optional[int] = None
) -> bool:
    """Ban a user."""
    try:
        db = get_db()
        result = await db.users.update_one(
            {"telegram_id": user_id},
            {
                "$set": {
                    "banned": True,
                    "ban_reason": reason,
                    "banned_at": datetime.utcnow(),
                    "banned_by": admin_id,
                }
            },
        )
        if admin_id:
            await log_admin_action(
                admin_id, "user_banned", {"user_id": user_id, "reason": reason}
            )
        _bust_user_cache(user_id)
        logger.info(f"✅ User banned: {user_id}")
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"❌ Ban user failed: {e}", exc_info=True)
        return False


async def unban_user(user_id: int, admin_id: Optional[int] = None) -> bool:
    """Unban a user."""
    try:
        db = get_db()
        result = await db.users.update_one(
            {"telegram_id": user_id},
            {
                "$set": {
                    "banned": False,
                    "ban_reason": None,
                    "banned_at": None,
                    "banned_by": None,
                }
            },
        )
        if admin_id:
            await log_admin_action(admin_id, "user_unbanned", {"user_id": user_id})
        _bust_user_cache(user_id)
        logger.info(f"✅ User unbanned: {user_id}")
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"❌ Unban user failed: {e}", exc_info=True)
        return False


async def set_user_role(user_id: int, role: str) -> bool:
    """Set user role to 'user' or 'admin'."""
    try:
        if role not in ("user", "admin"):
            raise ValueError(f"Invalid role: {role}")
        db = get_db()
        result = await db.users.update_one(
            {"telegram_id": user_id},
            {"$set": {"role": role, "updated_at": datetime.utcnow()}},
        )
        _bust_user_cache(user_id)
        logger.info(f"✅ Role set to {role} for user {user_id}")
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"❌ Set user role failed: {e}", exc_info=True)
        return False


async def store_user_thumbnail(
    user_id: int,
    file_id: str,
    thumbnail_url: str,
    channel_id: int,
    admin_id: Optional[int] = None,
) -> bool:
    """Store user thumbnail in designated channel and update MongoDB."""
    try:
        logger.info(f"📸 Storing thumbnail for user {user_id}")
        db = get_db()
        now = datetime.utcnow()
        thumbnail_data = {
            "file_id": file_id,
            "url": thumbnail_url,
            "channel_id": channel_id,
            "uploaded_at": now,
        }
        update_data = {
            "settings.thumbnail_file_id": file_id,
            "settings.thumbnail": "custom",
            "settings.thumbnail_url": thumbnail_url,
            "updated_at": now,
        }
        result = await db.users.update_one(
            {"telegram_id": user_id},
            {"$set": update_data, "$push": {"storage.thumbnails": thumbnail_data}},
        )
        if result.matched_count > 0:
            logger.info(f"✅ Thumbnail stored for user {user_id}: {thumbnail_url}")
            if admin_id:
                await log_admin_action(
                    admin_id,
                    "user_thumbnail_updated",
                    {"user_id": user_id, "thumbnail_url": thumbnail_url},
                )
            return True
        logger.error(f"❌ User {user_id} not found")
        return False
    except Exception as e:
        logger.error(
            f"❌ Store thumbnail failed for user {user_id}: {e}", exc_info=True
        )
        return False


async def get_user_destinations(user_id: int) -> List[Dict[str, Any]]:
    """Get all destination channels for a user."""
    try:
        user = await get_user(user_id)
        if not user:
            return []
        return user.get("settings", {}).get("destinations", [])
    except Exception as e:
        logger.error(f"❌ Error getting user destinations: {e}")
        return []


async def add_user_destination(user_id: int, channel_id: int, title: str) -> bool:
    """Add a destination channel for a user."""
    try:
        user = await get_user(user_id)
        if not user:
            return False
        settings = user.get("settings", {})
        destinations = settings.get("destinations", [])
        if any(d.get("id") == channel_id for d in destinations):
            return True
        destinations.append(
            {"id": channel_id, "title": title, "added_at": datetime.utcnow()}
        )
        await update_user(user_id, {"settings.destinations": destinations})
        return True
    except Exception as e:
        logger.error(f"❌ Error adding user destination: {e}")
        return False


async def remove_user_destination(user_id: int, channel_id: int) -> bool:
    """Remove a destination channel for a user."""
    try:
        user = await get_user(user_id)
        if not user:
            return False
        settings = user.get("settings", {})
        destinations = settings.get("destinations", [])
        new_destinations = [d for d in destinations if d.get("id") != channel_id]
        if len(new_destinations) == len(destinations):
            return False
        await update_user(user_id, {"settings.destinations": new_destinations})
        return True
    except Exception as e:
        logger.error(f"❌ Error removing user destination: {e}")
        return False
