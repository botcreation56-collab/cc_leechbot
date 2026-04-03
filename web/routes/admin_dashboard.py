"""
Admin dashboard API routes
"""

import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from typing import List, Optional

from database import (
    UserRepository,
    TaskRepository,
    CloudFileRepository,
    RcloneConfigRepository,
    ConfigRepository,
)
from web.routes.auth import get_current_user, get_current_admin
from fastapi import Request

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
async def get_dashboard(request: Request, admin_id: int = Depends(get_current_admin)):
    """Get dashboard statistics using repositories."""
    try:
        deps = request.app.state.deps
        user_repo: UserRepository = deps["user_repo"]
        rclone_repo: RcloneConfigRepository = deps["rclone_repo"]
        config_repo: ConfigRepository = deps["config_repo"]

        # ✅ USE REPOSITORY STATS
        user_stats = await user_repo.stats()

        total_users = user_stats.get("total_users", 0)
        free_users = user_stats.get("free_users", 0)
        pro_users = user_stats.get("pro_users", 0)
        banned_users = user_stats.get("banned_users", 0)

        # Get rclone configs and terabox status
        rclone_count = await rclone_repo.count()
        config = await config_repo.get()
        terabox_enabled = bool(config and config.get("terabox_config"))

        logger.info(
            f"✅ Dashboard stats retrieved via repositories: admin_id={admin_id}, total_users={total_users}"
        )

        return DashboardStats(
            total_users=total_users,
            free_users=free_users,
            pro_users=pro_users,
            banned_users=banned_users,
            rclone_configs=rclone_count,
            terabox_enabled=terabox_enabled,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Dashboard error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/stats/{user_id}", response_model=UserStats)
async def get_user_stats(
    request: Request, user_id: int, admin_id: int = Depends(get_current_admin)
):
    """Get user statistics using repositories."""
    try:
        deps = request.app.state.deps
        user_repo: UserRepository = deps["user_repo"]
        cloud_repo: CloudFileRepository = deps["cloud_repo"]

        # Get user from database
        user = await user_repo.get(user_id)
        if not user:
            logger.warning(f"❌ User not found: telegram_id={user_id}")
            raise HTTPException(status_code=404, detail="User not found")

        # ✅ QUERY STORAGE STATS FROM REPOSITORY
        storage_stats = await cloud_repo.get_user_storage_stats(user_id)
        files_count = storage_stats["count"]
        storage_used_bytes = storage_stats["total_size"]
        storage_used = storage_used_bytes / (1024**3)  # Convert to GB

        storage_limit = user.get("daily_limit", 0) / (1024**3)

        logger.info(
            f"✅ User stats retrieved via repositories: user_id={user_id}, files={files_count}, storage={storage_used:.2f}GB"
        )

        return UserStats(
            user_id=user_id,
            plan=user.get("plan", "free"),
            storage_used=storage_used,
            storage_limit=storage_limit,
            files_count=files_count,
            banned=user.get("banned", False),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ User stats error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

