"""
database/repositories.py — Repository pattern for all collections.

Design:
  - Each repository is a class that wraps a single Motor collection.
  - Repositories accept a Motor Database as constructor argument.
  - All public methods are async.
  - Business logic lives in services/. Repositories do only data access.
  - Caching (TTLCache) is scoped to each repository instance.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from cachetools import TTLCache
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger("filebot.db.repos")


def _utcnow() -> datetime:
    return datetime.utcnow()


def _to_str_id(doc: dict) -> dict:
    """Convert ObjectId _id to string for safe serialisation."""
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


class UserRepository:
    """CRUD for the users collection with TTL caching."""

    _CACHE_TTL = 120
    _CACHE_MAX = 2048

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db.users
        self._cache: TTLCache = TTLCache(maxsize=self._CACHE_MAX, ttl=self._CACHE_TTL)

    def _invalidate(self, user_id: int) -> None:
        self._cache.pop(user_id, None)

    async def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Return the user document or None. Cache-backed."""
        if user_id in self._cache:
            return self._cache[user_id]
        try:
            doc = await self._col.find_one({"telegram_id": user_id}, {"__v": 0})
            if doc:
                doc = _to_str_id(doc)
                self._cache[user_id] = doc
            return doc
        except Exception as exc:
            logger.error("UserRepository.get failed: %s", exc)
            return None

    async def get_all(
        self, *, limit: int = 200, skip: int = 0, banned: Optional[bool] = None
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Return (users_page, total_count)."""
        try:
            query: Dict[str, Any] = {}
            if banned is not None:
                query["banned"] = banned
            total = await self._col.count_documents(query)
            cursor = (
                self._col.find(query, {"__v": 0})
                .skip(skip)
                .limit(limit)
                .sort("created_at", DESCENDING)
            )
            docs = [_to_str_id(d) for d in await cursor.to_list(length=limit)]
            return docs, total
        except Exception as exc:
            logger.error("UserRepository.get_all failed: %s", exc)
            return [], 0

    async def get_banned(self, *, limit: int = 1000) -> List[Dict[str, Any]]:
        return (await self.get_all(limit=limit, banned=True))[0]

    async def create(
        self, user_id: int, first_name: str, username: Optional[str] = None
    ) -> Dict[str, Any]:
        """Upsert a user and return the final document."""
        self._invalidate(user_id)
        now = _utcnow()
        doc = {
            "telegram_id": user_id,
            "first_name": first_name[:64],
            "username": username,
            "role": "user",
            "plan": "free",
            "banned": False,
            "ban_reason": None,
            "files_processed": 0,
            "daily_used": 0,
            "used_storage": 0,
            "daily_limit": 5,
            "settings": {
                "mode": "video",
                "prefix": None,
                "suffix": None,
                "thumbnail": "auto",
                "thumbnail_file_id": None,
                "visibility": "public",
                "metadata": {},
                "destinations": [],
                "destination_metadata": {},
            },
            "created_at": now,
            "updated_at": now,
        }
        try:
            await self._col.update_one(
                {"telegram_id": user_id},
                {"$setOnInsert": doc},
                upsert=True,
            )
            return await self.get(user_id) or doc
        except Exception as exc:
            logger.error("UserRepository.create failed: %s", exc)
            return doc

    async def update(self, user_id: int, fields: Dict[str, Any]) -> bool:
        """Partial update."""
        self._invalidate(user_id)
        allowed_top_level = {
            "first_name",
            "username",
            "role",
            "plan",
            "banned",
            "ban_reason",
            "files_processed",
            "daily_used",
            "used_storage",
            "daily_limit",
            "settings",
            "priority_until",
            "updated_at",
        }
        safe = {k: v for k, v in fields.items() if k.split(".")[0] in allowed_top_level}
        if not safe:
            logger.warning(
                "UserRepository.update called with empty/disallowed fields for %d",
                user_id,
            )
            return False
        safe["updated_at"] = _utcnow()
        try:
            result = await self._col.update_one(
                {"telegram_id": user_id}, {"$set": safe}
            )
            return result.matched_count > 0
        except Exception as exc:
            logger.error("UserRepository.update failed: %s", exc)
            return False

    async def ban(
        self,
        user_id: int,
        *,
        reason: str = "Banned by admin",
        admin_id: Optional[int] = None,
    ) -> bool:
        self._invalidate(user_id)
        try:
            result = await self._col.update_one(
                {"telegram_id": user_id},
                {
                    "$set": {
                        "banned": True,
                        "ban_reason": reason,
                        "banned_by": admin_id,
                        "updated_at": _utcnow(),
                    }
                },
            )
            return result.matched_count > 0
        except Exception as exc:
            logger.error("UserRepository.ban failed: %s", exc)
            return False

    async def unban(self, user_id: int, *, admin_id: Optional[int] = None) -> bool:
        self._invalidate(user_id)
        try:
            result = await self._col.update_one(
                {"telegram_id": user_id},
                {
                    "$set": {
                        "banned": False,
                        "ban_reason": None,
                        "banned_by": None,
                        "updated_at": _utcnow(),
                    }
                },
            )
            return result.matched_count > 0
        except Exception as exc:
            logger.error("UserRepository.unban failed: %s", exc)
            return False

    async def add_destination(self, user_id: int, channel_id: int, title: str) -> bool:
        self._invalidate(user_id)
        try:
            result = await self._col.update_one(
                {
                    "telegram_id": user_id,
                    "settings.destinations.id": {"$ne": channel_id},
                },
                {
                    "$push": {
                        "settings.destinations": {"id": channel_id, "title": title}
                    },
                    "$set": {"updated_at": _utcnow()},
                },
            )
            return result.modified_count > 0
        except Exception as exc:
            logger.error("UserRepository.add_destination failed: %s", exc)
            return False

    async def remove_destination(self, user_id: int, channel_id: int) -> bool:
        self._invalidate(user_id)
        try:
            result = await self._col.update_one(
                {"telegram_id": user_id},
                {
                    "$pull": {"settings.destinations": {"id": channel_id}},
                    "$set": {"updated_at": _utcnow()},
                },
            )
            return result.modified_count > 0
        except Exception as exc:
            logger.error("UserRepository.remove_destination failed: %s", exc)
            return False

    async def stats(self) -> Dict[str, int]:
        """Return aggregate user stats for admin dashboard."""
        try:
            pipeline = [
                {
                    "$group": {
                        "_id": None,
                        "total": {"$sum": 1},
                        "banned": {"$sum": {"$cond": ["$banned", 1, 0]}},
                        "premium": {
                            "$sum": {"$cond": [{"$eq": ["$plan", "premium"]}, 1, 0]}
                        },
                        "pro": {"$sum": {"$cond": [{"$eq": ["$plan", "pro"]}, 1, 0]}},
                    }
                }
            ]
            res = await self._col.aggregate(pipeline).to_list(1)
            if not res:
                return {
                    "total": 0,
                    "banned": 0,
                    "active": 0,
                    "premium": 0,
                    "pro": 0,
                    "free": 0,
                }
            row = res[0]
            total = row["total"]
            banned = row["banned"]
            premium = row.get("premium", 0)
            pro = row.get("pro", 0)
            free = total - premium - pro
            return {
                "total_users": total,
                "banned_users": banned,
                "active_users": total - banned,
                "premium_users": premium,
                "pro_users": pro,
                "free_users": max(free, 0),
            }
        except Exception as exc:
            logger.error("UserRepository.stats failed: %s", exc)
            return {}


class TaskRepository:
    """CRUD for the tasks collection."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db.tasks

    async def create(
        self,
        user_id: int,
        file_id: str,
        task_type: str = "upload",
        metadata: Optional[Dict] = None,
        max_concurrent_per_user: int = 3,
    ) -> Tuple[bool, str]:
        """Create a task and return its string ID.

        Returns (success, task_id_or_error_message).
        Enforces concurrent task limits per user.
        """
        # Check concurrent tasks for this user
        active_statuses = [
            "pending",
            "queued",
            "downloading",
            "processing",
            "uploading",
        ]
        active_count = await self._col.count_documents(
            {"user_id": user_id, "status": {"$in": active_statuses}}
        )

        if active_count >= max_concurrent_per_user:
            logger.warning(
                "TaskRepository.create rejected: user %d has %d active tasks (max: %d)",
                user_id,
                active_count,
                max_concurrent_per_user,
            )
            return (
                False,
                f"Concurrent task limit reached ({max_concurrent_per_user}). Please wait.",
            )

        import uuid

        task_id = str(uuid.uuid4())
        doc = {
            "task_id": task_id,
            "user_id": user_id,
            "file_id": file_id,
            "task_type": task_type,
            "status": "pending",
            "metadata": metadata or {},
            "error_message": None,
            "created_at": _utcnow(),
            "updated_at": _utcnow(),
            "completed_at": None,
        }
        try:
            await self._col.insert_one(doc)
            return True, task_id
        except Exception as exc:
            logger.error("TaskRepository.create failed: %s", exc)
            return False, "Database error"

    async def update(self, task_id: str, fields: Dict[str, Any]) -> bool:
        fields["updated_at"] = _utcnow()
        if fields.get("status") in ("completed", "failed"):
            fields.setdefault("completed_at", _utcnow())
        try:
            result = await self._col.update_one({"task_id": task_id}, {"$set": fields})
            return result.matched_count > 0
        except Exception as exc:
            logger.error("TaskRepository.update failed: %s", exc)
            return False

    async def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        try:
            return _to_str_id(await self._col.find_one({"task_id": task_id}))
        except Exception as exc:
            logger.error("TaskRepository.get failed: %s", exc)
            return None

    async def get_user_tasks(
        self, user_id: int, *, limit: int = 20, exclude_terminal: bool = False
    ) -> List[Dict[str, Any]]:
        """Fetch user tasks, optionally excluding terminal states."""
        try:
            query = {"user_id": user_id}
            if exclude_terminal:
                query["status"] = {
                    "$nin": ["completed", "failed", "cancelled", "expired"]
                }
            cursor = self._col.find(query).sort("created_at", DESCENDING).limit(limit)
            return [_to_str_id(d) for d in await cursor.to_list(length=limit)]
        except Exception as exc:
            logger.error("TaskRepository.get_user_tasks failed: %s", exc)
            return []

    async def count_by_status(self) -> Dict[str, int]:
        """Return count per status for admin dashboard."""
        try:
            pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
            res = await self._col.aggregate(pipeline).to_list(20)
            return {row["_id"]: row["count"] for row in res}
        except Exception as exc:
            logger.error("TaskRepository.count_by_status failed: %s", exc)
            return {}


class CloudFileRepository:
    """CRUD for the cloud_files collection."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db.cloud_files

    async def save(self, data: Dict[str, Any]) -> bool:
        """Upsert a cloud file by file_id."""
        data.setdefault("created_at", _utcnow())
        data["updated_at"] = _utcnow()
        try:
            await self._col.update_one(
                {"file_id": data["file_id"]},
                {"$set": data},
                upsert=True,
            )
            return True
        except Exception as exc:
            logger.error("CloudFileRepository.save failed: %s", exc)
            return False

    async def get(self, file_id: str) -> Optional[Dict[str, Any]]:
        try:
            return _to_str_id(await self._col.find_one({"file_id": file_id}))
        except Exception as exc:
            logger.error("CloudFileRepository.get failed: %s", exc)
            return None

    async def get_user_files(
        self, user_id: int, *, limit: int = 50
    ) -> List[Dict[str, Any]]:
        try:
            cursor = (
                self._col.find({"user_id": user_id})
                .sort("created_at", DESCENDING)
                .limit(limit)
            )
            return [_to_str_id(d) for d in await cursor.to_list(length=limit)]
        except Exception as exc:
            logger.error("CloudFileRepository.get_user_files failed: %s", exc)
            return []

    async def delete(self, file_id: str) -> bool:
        try:
            result = await self._col.delete_one({"file_id": file_id})
            return result.deleted_count > 0
        except Exception as exc:
            logger.error("CloudFileRepository.delete failed: %s", exc)
            return False

    async def cleanup_expired(self) -> int:
        """Delete all files where expires_at is in the past."""
        try:
            result = await self._col.delete_many({"expires_at": {"$lt": _utcnow()}})
            return result.deleted_count
        except Exception as exc:
            logger.error("CloudFileRepository.cleanup_expired failed: %s", exc)
            return 0

    async def total_size_bytes(self) -> int:
        """Sum of all file sizes stored."""
        try:
            pipeline = [{"$group": {"_id": None, "total": {"$sum": "$file_size"}}}]
            res = await self._col.aggregate(pipeline).to_list(1)
            return int(res[0]["total"]) if res else 0
        except Exception as exc:
            logger.error("CloudFileRepository.total_size_bytes failed: %s", exc)
            return 0


class OneTimeKeyRepository:
    """One-time secure tokens for streaming authentication."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db.one_time_keys

    async def create(self, user_id: int, token: str, expires: datetime) -> bool:
        try:
            await self._col.insert_one(
                {
                    "user_id": user_id,
                    "otp": token,
                    "used": False,
                    "expires_at": expires,
                    "created_at": _utcnow(),
                }
            )
            return True
        except DuplicateKeyError:
            logger.warning("Duplicate OTK token for user %d", user_id)
            return False
        except Exception as exc:
            logger.error("OneTimeKeyRepository.create failed: %s", exc)
            return False

    async def consume(self, token: str) -> Optional[Dict[str, Any]]:
        """Find, validate, and atomically mark a token as used."""
        try:
            doc = await self._col.find_one_and_update(
                {"otp": token, "used": False, "expires_at": {"$gt": _utcnow()}},
                {"$set": {"used": True}},
                return_document=ReturnDocument.AFTER,
            )
            return _to_str_id(doc) if doc else None
        except Exception as exc:
            logger.error("OneTimeKeyRepository.consume failed: %s", exc)
            return None

    async def validate(self, token: str) -> Optional[Dict[str, Any]]:
        """Non-consuming lookup."""
        try:
            doc = await self._col.find_one(
                {"otp": token, "expires_at": {"$gt": _utcnow()}}
            )
            return _to_str_id(doc) if doc else None
        except Exception as exc:
            logger.error("OneTimeKeyRepository.validate failed: %s", exc)
            return None


class ConfigRepository:
    """Singleton-document bot runtime configuration with caching."""

    _CACHE_TTL = 60

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db.config
        self._cache: TTLCache = TTLCache(maxsize=4, ttl=self._CACHE_TTL)

    async def get(self) -> Dict[str, Any]:
        cached = self._cache.get("main")
        if cached is not None:
            return cached
        try:
            doc = await self._col.find_one({"_id": "main"}) or {}
            doc.pop("_id", None)
            self._cache["main"] = doc
            return doc
        except Exception as exc:
            logger.error("ConfigRepository.get failed: %s", exc)
            return {}

    async def update(self, fields: Dict[str, Any]) -> bool:
        self._cache.pop("main", None)
        try:
            result = await self._col.update_one(
                {"_id": "main"},
                {"$set": fields},
                upsert=True,
            )
            return result.modified_count > 0 or result.upserted_id is not None
        except Exception as exc:
            logger.error("ConfigRepository.update failed: %s", exc)
            return False


class AuditLogRepository:
    """Append-only structured admin audit trail."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db.audit_log

    async def log(
        self, admin_id: int, action: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        try:
            await self._col.insert_one(
                {
                    "admin_id": admin_id,
                    "action": action,
                    "details": details or {},
                    "timestamp": _utcnow(),
                }
            )
        except Exception as exc:
            logger.error("Audit log write failed: %s", exc)


class RcloneConfigRepository:
    """Encrypted Rclone credentials storage per plan/service."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db.rclone_configs

    async def add(self, config_data: Dict[str, Any]) -> str:
        """Insert a new rclone config. Returns the inserted string ID."""
        import uuid

        config_data["config_id"] = str(uuid.uuid4())
        config_data.setdefault("is_active", True)
        config_data.setdefault("created_at", _utcnow())
        try:
            result = await self._col.insert_one(config_data)
            return str(result.inserted_id)
        except Exception as exc:
            logger.error("RcloneConfigRepository.add failed: %s", exc)
            return ""

    async def list(self, *, is_active: Optional[bool] = None) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {}
        if is_active is not None:
            query["is_active"] = is_active
        try:
            cursor = self._col.find(query)
            return [_to_str_id(d) for d in await cursor.to_list(length=100)]
        except Exception as exc:
            logger.error("RcloneConfigRepository.list failed: %s", exc)
            return []

    async def pick_for_plan(self, plan: str) -> Optional[Dict[str, Any]]:
        """Pick the least-loaded active config for the given plan tier."""
        try:
            configs = await self.list(is_active=True)
            matches = [c for c in configs if c.get("plan", "free") == plan]
            if not matches:
                matches = configs
            if not matches:
                return None
            return min(matches, key=lambda c: c.get("current_users", 0))
        except Exception as exc:
            logger.error("RcloneConfigRepository.pick_for_plan failed: %s", exc)
            return None
