"""
Admin dashboard API routes
"""

import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from typing import List, Optional

from bot.database import (
    get_db, get_all_users, get_user, get_rclone_configs, get_config
)
from web.routes.auth import get_current_user, get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter()


class DashboardStats(BaseModel):
    """Dashboard statistics"""
    total_users: int
    free_users: int
    pro_users: int
    banned_users: int
    rclone_configs: int
    terabox_enabled: bool


class UserStats(BaseModel):
    """User statistics"""
    user_id: int
    plan: str
    storage_used: float
    storage_limit: float
    files_count: int
    banned: bool




@router.get("/dashboard", response_model=DashboardStats)
async def get_dashboard(admin_id: int = Depends(get_current_admin)):
    """Get dashboard statistics using aggregation pipeline."""
    try:
        db = get_db()
        
        # ✅ USE AGGREGATION PIPELINE (NO LOADING INTO MEMORY)
        pipeline = [
            {
                "$facet": {
                    "total": [{"$count": "count"}],
                    "by_plan": [
                        {"$group": {
                            "_id": "$plan",
                            "count": {"$sum": 1}
                        }},
                        {"$match": {"_id": {"$in": ["free", "pro"]}}}
                    ],
                    "banned": [
                        {"$match": {"banned": True}},
                        {"$count": "count"}
                    ]
                }
            }
        ]
        
        result = await db.users.aggregate(pipeline).to_list(1)
        data = result[0] if result else {}
        
        # Extract counts
        total_count = data.get("total", [{}])[0].get("count", 0) if data.get("total") else 0
        
        plan_counts = {item["_id"]: item["count"] 
                      for item in data.get("by_plan", [])}
        free_users = plan_counts.get("free", 0)
        pro_users = plan_counts.get("pro", 0)
        
        banned_list = data.get("banned", [])
        banned_count = banned_list[0].get("count", 0) if banned_list else 0
        
        # Get rclone configs and terabox status
        rclone_configs = await db.rclone_configs.count_documents({})
        config = await db.config.find_one({})
        terabox_enabled = bool(config and config.get("terabox_config"))
        
        logger.info(f"✅ Dashboard stats retrieved: admin_id={admin_id}, total_users={total_count}")
        
        return DashboardStats(
            total_users=total_count,
            free_users=free_users,
            pro_users=pro_users,
            banned_users=banned_count,
            rclone_configs=rclone_configs,
            terabox_enabled=terabox_enabled
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Dashboard error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")



@router.get("/stats/{user_id}", response_model=UserStats)
async def get_user_stats(user_id: int, admin_id: int = Depends(get_current_admin)):
    """Get user statistics."""
    try:
        db = get_db()
        
        # Get user from database — field is telegram_id, not user_id
        user = await db.users.find_one({"telegram_id": user_id})
        if not user:
            logger.warning(f"❌ User not found: telegram_id={user_id}")
            raise HTTPException(status_code=404, detail="User not found")
        
        # ✅ QUERY FILE COUNT FROM DATABASE
        files_count = await db.files.count_documents({"user_id": user_id})
        
        # ✅ CALCULATE ACTUAL STORAGE USED
        storage_pipeline = [
            {"$match": {"user_id": user_id}},
            {"$group": {
                "_id": None,
                "total_size": {"$sum": "$file_size"}
            }}
        ]
        storage_result = await db.files.aggregate(storage_pipeline).to_list(1)
        # storage_result is a list (aggregate returns list of docs)
        storage_used_bytes = storage_result[0].get("total_size", 0) if storage_result and isinstance(storage_result[0], dict) else 0
        storage_used = storage_used_bytes / (1024 ** 3)  # Convert to GB
        
        storage_limit = user.get("storage_limit", 0) / (1024 ** 3)
        
        logger.info(f"✅ User stats retrieved: user_id={user_id}, files={files_count}, storage={storage_used:.2f}GB")
        
        return UserStats(
            user_id=user_id,
            plan=user.get("plan", "free"),
            storage_used=storage_used,
            storage_limit=storage_limit,
            files_count=files_count,  # ✅ ACTUAL COUNT
            banned=user.get("banned", False)
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ User stats error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

