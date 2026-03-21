"""
Admin user management API routes
"""

import logging
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from typing import List, Optional

from bot.database import (
    get_db,
    get_user,
    update_user,
    ban_user,
    unban_user,
    get_all_users,
)
from web.routes.auth import get_current_user, get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter()


class UserResponse(BaseModel):
    """User response"""

    telegram_id: int
    first_name: str
    username: Optional[str]
    plan: str
    banned: bool
    parallel_slots: Optional[int] = None
    storage_limit: Optional[int] = None
    validity_from: Optional[str] = None
    validity_to: Optional[str] = None
    joined_at: Optional[str] = None
    last_active: Optional[str] = None
    files_count: Optional[int] = 0
    storage_used_gb: Optional[float] = 0.0
    daily_limit_mb: Optional[int] = None
    is_premium: bool = False
    priority_until: Optional[str] = None
    metadata_author: Optional[str] = None
    default_mode: Optional[str] = "video"


class UpdateUserRequest(BaseModel):
    """Update user request"""

    plan: Optional[str] = None
    parallel_slots: Optional[int] = None
    storage_limit_gb: Optional[float] = None
    daily_limit_mb: Optional[int] = None
    validity_from: Optional[str] = None
    validity_to: Optional[str] = None
    default_mode: Optional[str] = None
    metadata_author: Optional[str] = None


class UserListResponse(BaseModel):
    """User list response"""

    total: int
    users: List[UserResponse]


class BanUserRequest(BaseModel):
    """Ban user request"""

    user_id: int
    reason: str


@router.get("/users", response_model=UserListResponse)
async def list_users(
    skip: int = Query(0),
    limit: int = Query(10),
    admin_id: int = Depends(get_current_admin),
):
    """
    List all users with pagination
    """
    try:
        db = get_db()

        # Calculate page from skip/limit for the database layer
        db_page = skip // limit if limit > 0 else 0

        users, total = await get_all_users(limit=limit, page=db_page)

        # No Python-side array slicing needed (fully offloaded to MongoDB)
        paginated = users

        user_responses = []
        for user in paginated:
            telegram_id = user.get("telegram_id")

            # Get user's file count and storage
            files_count = 0
            storage_used_bytes = 0
            try:
                files_count = await db.files.count_documents({"user_id": telegram_id})
                storage_result = await db.files.aggregate(
                    [
                        {"$match": {"user_id": telegram_id}},
                        {"$group": {"_id": None, "total_size": {"$sum": "$file_size"}}},
                    ]
                ).to_list(1)
                if storage_result:
                    storage_used_bytes = storage_result[0].get("total_size", 0) or 0
            except Exception as e:
                logger.warning(
                    f"⚠️ Could not get file stats for user {telegram_id}: {e}"
                )

            storage_used_gb = round(storage_used_bytes / (1024**3), 2)

            user_responses.append(
                UserResponse(
                    telegram_id=telegram_id,
                    first_name=user.get("first_name", ""),
                    username=user.get("username"),
                    plan=user.get("plan", "free"),
                    banned=user.get("banned", False),
                    parallel_slots=user.get("parallel_slots"),
                    storage_limit=user.get("storage_limit"),
                    validity_from=user.get("validity_from"),
                    validity_to=user.get("validity_to"),
                    joined_at=user.get("created_at").isoformat()
                    if user.get("created_at")
                    else None,
                    last_active=user.get("last_active").isoformat()
                    if user.get("last_active")
                    else None,
                    files_count=files_count,
                    storage_used_gb=storage_used_gb,
                    daily_limit_mb=user.get("daily_limit_mb"),
                    is_premium=user.get("is_premium", False),
                    priority_until=user.get("priority_until"),
                    metadata_author=user.get("metadata_author"),
                    default_mode=user.get("settings", {}).get("default_mode", "video")
                    if isinstance(user.get("settings"), dict)
                    else "video",
                )
            )

        logger.info(f"✅ Users list retrieved: {len(users)} total")

        return UserListResponse(total=len(users), users=user_responses)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ List users error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/users/{user_id}/ban")
async def ban_user_endpoint(
    user_id: int, request: BanUserRequest, admin_id: int = Depends(get_current_admin)
):
    """
    Ban a user
    """
    try:
        # Ban user
        db = get_db()
        success = await ban_user(user_id, request.reason, admin_id)

        if not success:
            raise HTTPException(status_code=400, detail="Failed to ban user")

        logger.info(f"✅ User banned: {user_id}")

        return {"status": "success", "message": "User banned"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Ban user error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/users/{user_id}/unban")
async def unban_user_endpoint(user_id: int, admin_id: int = Depends(get_current_admin)):
    """
    Unban a user
    """
    try:
        # Unban user
        db = get_db()
        success = await unban_user(user_id, admin_id)

        if not success:
            raise HTTPException(status_code=400, detail="Failed to unban user")

        logger.info(f"✅ User unbanned: {user_id}")

        return {"status": "success", "message": "User unbanned"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Unban user error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/users/{user_id}/upgrade")
async def upgrade_user(user_id: int, admin_id: int = Depends(get_current_admin)):
    """
    Upgrade user to pro
    """
    try:
        # Upgrade user
        db = get_db()
        success = await update_user(
            user_id,
            {
                "plan": "pro",
                "storage_limit": 10 * 1024 * 1024 * 1024,  # 10GB
                "daily_limit": None,
                "parallel_slots": 5,
            },
            admin_id,
        )

        if not success:
            raise HTTPException(status_code=400, detail="Failed to upgrade user")

        logger.info(f"✅ User upgraded to pro: {user_id}")

        return {"status": "success", "message": "User upgraded to pro"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Upgrade user error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/users/{user_id}/update")
async def update_user_endpoint(
    user_id: int, request: UpdateUserRequest, admin_id: int = Depends(get_current_admin)
):
    """
    Update a user
    """
    try:
        db = get_db()
        updates = {}
        if request.plan is not None:
            updates["plan"] = request.plan
        if request.parallel_slots is not None:
            updates["parallel_slots"] = request.parallel_slots
        if request.storage_limit_gb is not None:
            updates["storage_limit"] = int(
                request.storage_limit_gb * 1024 * 1024 * 1024
            )
        if request.daily_limit_mb is not None:
            updates["daily_limit_mb"] = request.daily_limit_mb
        if request.validity_from is not None:
            updates["validity_from"] = request.validity_from
        if request.validity_to is not None:
            updates["validity_to"] = request.validity_to
        if request.default_mode is not None:
            updates["settings.default_mode"] = request.default_mode
        if request.metadata_author is not None:
            updates["metadata_author"] = request.metadata_author

        success = await update_user(user_id, updates, admin_id)

        if not success:
            raise HTTPException(status_code=400, detail="Failed to update user")

        logger.info(f"✅ User updated: {user_id}")
        return {"status": "success", "message": "User updated"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Update user error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
