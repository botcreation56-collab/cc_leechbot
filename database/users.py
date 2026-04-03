"""
database/users.py — User CRUD operations (migrated to repository pattern).
"""
import logging
from .connection import get_db
from .repositories import UserRepository

logger = logging.getLogger("filebot.db.users")

async def create_user(user_id: int, first_name: str, username: str = ""):
    return await UserRepository(get_db()).create(user_id, first_name, username)

async def get_user(user_id: int):
    return await UserRepository(get_db()).get(user_id)

async def get_all_users(query=None, limit=100, banned_only=False, page=0):
    banned = True if banned_only else None
    return await UserRepository(get_db()).get_all(limit=limit, skip=page*limit, banned=banned)

async def get_banned_users(limit=1000):
    return await UserRepository(get_db()).get_banned(limit=limit)

async def update_user(user_id: int, updates: dict, admin_id=None):
    return await UserRepository(get_db()).update(user_id, updates)

async def ban_user(user_id: int, reason: str = "", admin_id=None):
    return await UserRepository(get_db()).ban(user_id, reason=reason, admin_id=admin_id)

async def unban_user(user_id: int, admin_id=None):
    return await UserRepository(get_db()).unban(user_id, admin_id=admin_id)

async def set_user_role(user_id: int, role: str):
    return await UserRepository(get_db()).update(user_id, {"role": role})

async def add_user_destination(user_id: int, channel_id: int, title: str):
    return await UserRepository(get_db()).add_destination(user_id, channel_id, title)

async def remove_user_destination(user_id: int, channel_id: int):
    return await UserRepository(get_db()).remove_destination(user_id, channel_id)

async def get_user_destinations(user_id: int):
    user = await UserRepository(get_db()).get(user_id)
    return user.get("settings", {}).get("destinations", []) if user else []

async def store_user_thumbnail(user_id: int, file_id: str, thumbnail_url: str, channel_id: int, admin_id=None):
    return await UserRepository(get_db()).update(user_id, {
        "settings.thumbnail_file_id": file_id,
        "settings.thumbnail": "custom",
        "settings.thumbnail_url": thumbnail_url
    })
