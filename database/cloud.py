"""
database/cloud.py — Cloud file metadata storage and cleanup.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

from database.connection import get_db

logger = logging.getLogger("filebot.db.cloud")


async def store_cloud_file_metadata(
    user_id: int,
    file_id: str,
    cloud_type: str,
    cloud_url: str,
    retention_days: int,
    expiry_date: datetime,
    filename: str = "",
    file_size: int = 0,
    visibility: str = "public",
) -> bool:
    """Store cloud file metadata in the cloud_files collection."""
    try:
        db = get_db()
        doc = {
            "user_id": user_id,
            "file_id": file_id,
            "cloud_type": cloud_type,
            "cloud_url": cloud_url,
            "retention_days": retention_days,
            "expiry_date": expiry_date,
            "filename": filename,
            "file_size": file_size,
            "visibility": visibility,
            "created_at": datetime.utcnow(),
        }
        await db.cloud_files.insert_one(doc)
        logger.info(f"✅ Cloud file metadata stored: {file_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Store cloud file failed: {e}", exc_info=True)
        return False


async def get_user_files(user_id: int, limit: int = 100) -> List[Dict]:
    """Fetch user's cloud files (limited for performance)."""
    try:
        db = get_db()
        files = await (
            db.cloud_files.find({"user_id": user_id})
            .sort("created_at", -1)
            .limit(limit)
            .to_list(None)
        )
        logger.debug(f"Fetched {len(files)} files for user {user_id}")
        return files
    except Exception as e:
        logger.error(f"❌ get_user_files({user_id}) failed: {e}")
        return []


async def get_user_cloud_files(user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    """Alias for get_user_files."""
    return await get_user_files(user_id, limit)


async def cleanup_old_cloud_files() -> Dict[str, int]:
    """Remove expired cloud file metadata and their tracking URLs from database."""
    try:
        db = get_db()
        now = datetime.utcnow()

        expired = await db.cloud_files.find(
            {"expiry_date": {"$lt": now}}, {"file_id": 1}
        ).to_list(None)
        expired_ids = [doc["file_id"] for doc in expired if "file_id" in doc]

        if not expired_ids:
            return {"deleted_count": 0, "errors": 0}

        result = await db.cloud_files.delete_many({"file_id": {"$in": expired_ids}})
        deleted_count = result.deleted_count

        await db.tracked_links.delete_many({"file_id": {"$in": expired_ids}})

        logger.info(
            f"Cleanup completed: {deleted_count} expired cloud files and tracked links removed"
        )
        return {"deleted_count": deleted_count, "errors": 0}
    except Exception as e:
        logger.error(f"Cleanup old cloud files failed: {e}", exc_info=True)
        return {"deleted_count": 0, "errors": 1}


async def delete_expired_cloud_files() -> int:
    """Delete all expired cloud files and tracked links, return count removed."""
    try:
        db = get_db()
        now = datetime.utcnow()

        expired = await db.cloud_files.find(
            {"expiry_date": {"$lt": now}}, {"file_id": 1}
        ).to_list(None)
        expired_ids = [doc["file_id"] for doc in expired if "file_id" in doc]
        count = len(expired_ids)

        if count > 0:
            await db.cloud_files.delete_many({"file_id": {"$in": expired_ids}})
            await db.tracked_links.delete_many({"file_id": {"$in": expired_ids}})
            logger.info(f"✅ Deleted {count} expired files and tracked links")

        return count
    except Exception as e:
        logger.error(f"❌ Delete expired failed: {e}")
        return 0


async def get_user_storage_path(user_id: int) -> str:
    """Generate user-specific storage path for organizing files."""
    return f"pic/user_{user_id}/"
