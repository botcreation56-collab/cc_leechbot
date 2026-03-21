"""
User settings API routes
"""

import logging
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

from bot.database import get_db, get_user, update_user, get_config
from web.routes.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


class UserSettingsUpdate(BaseModel):
    """User settings update request"""

    metadata_author: Optional[str] = None
    metadata_title: Optional[str] = None
    default_mode: Optional[str] = None


class DestinationAdd(BaseModel):
    """Destination add request"""

    channel_id: int
    title: str


@router.get("/")
async def get_user_settings(user_id: int = Depends(get_current_user)):
    """Get current user settings"""
    try:
        user = await get_user(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        settings = user.get("settings", {})

        from bot.database import get_user_destinations

        destinations = await get_user_destinations(user_id)

        dest_metadata = settings.get("destination_metadata", {})
        enhanced_destinations = []
        for dest in destinations:
            cid = str(dest.get("id"))
            meta = dest_metadata.get(cid, {})
            enhanced_destinations.append(
                {
                    "id": dest.get("id"),
                    "title": dest.get("title") or cid,
                    "custom_name": meta.get("title", ""),
                    "custom_author": meta.get("author", ""),
                }
            )

        return {
            "metadata_author": user.get("metadata_author", ""),
            "metadata_title": user.get("metadata_title", ""),
            "default_mode": settings.get("default_mode", "video"),
            "destinations": enhanced_destinations,
            "plan": user.get("plan", "free"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Get settings error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/stats")
async def get_user_stats(user_id: int = Depends(get_current_user)):
    """Get current user stats with lazy loading support"""
    try:
        db = get_db()
        user = await get_user(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        from config.settings import get_settings

        settings = get_settings()

        plan = user.get("plan", "free")
        plan_config = (await get_config()).get("plans", {}).get(plan, {})

        storage_limit = plan_config.get("storage_per_day", 5) * 1024 * 1024 * 1024
        max_file_size = plan_config.get("max_file_size_gb", 5) * 1024 * 1024 * 1024

        total_files = await db.cloud_files.count_documents({"user_id": user_id})
        storage_used = user.get("used_storage", 0) or 0

        return {
            "total_files": total_files,
            "storage_used": storage_used,
            "storage_limit": storage_limit,
            "storage_used_mb": round(storage_used / (1024 * 1024), 2),
            "storage_limit_gb": plan_config.get("storage_per_day", 5),
            "max_file_size_gb": plan_config.get("max_file_size_gb", 5),
            "plan": plan,
            "parallel_slots": user.get(
                "parallel_slots", plan_config.get("parallel", 1)
            ),
            "daily_used": user.get("daily_used", 0),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Get stats error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/")
async def update_user_settings(
    request: UserSettingsUpdate, user_id: int = Depends(get_current_user)
):
    """Update current user settings"""
    try:
        user = await get_user(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        updates = {}
        if request.metadata_author is not None:
            updates["metadata_author"] = request.metadata_author
        if request.metadata_title is not None:
            updates["metadata_title"] = request.metadata_title

        if request.default_mode is not None:
            settings = user.get("settings", {})
            settings["default_mode"] = request.default_mode
            updates["settings"] = settings

        if updates:
            await update_user(user_id, updates)

        return {"status": "success", "message": "Settings updated"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Update settings error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/destinations")
async def add_destination(
    request: DestinationAdd, user_id: int = Depends(get_current_user)
):
    """Add a new destination channel"""
    try:
        from bot.database import add_user_destination

        success = await add_user_destination(user_id, request.channel_id, request.title)

        if success:
            return {"status": "success", "message": "Destination added"}
        else:
            raise HTTPException(status_code=400, detail="Failed to add destination")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Add destination error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/myfiles")
async def get_my_files(user_id: int = Depends(get_current_user)):
    """Get current user's uploaded files — called by api.js getMyFiles()."""
    try:
        db = get_db()
        files_cursor = (
            db.cloud_files.find({"user_id": user_id}).sort("created_at", -1).limit(50)
        )
        files = await files_cursor.to_list(length=50)

        return [
            {
                "file_id": f.get("file_id"),
                "filename": f.get("filename", f.get("file_id", "Unknown")),
                "file_size": f.get("file_size", 0),
                "created_at": f.get("created_at").isoformat()
                if f.get("created_at")
                else None,
                "status": "active" if f.get("status") != "expired" else "expired",
            }
            for f in files
        ]
    except Exception as e:
        logger.error(f"❌ Get my files error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/destinations/{channel_id}")
async def remove_destination(channel_id: int, user_id: int = Depends(get_current_user)):
    """Remove a destination channel"""
    try:
        from bot.database import remove_user_destination

        success = await remove_user_destination(user_id, channel_id)

        if success:
            return {"status": "success", "message": "Destination removed"}
        else:
            raise HTTPException(status_code=400, detail="Failed to remove destination")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Remove destination error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
