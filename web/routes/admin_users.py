"""
Admin user management API routes
"""

import logging
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from typing import List, Optional

from bot.database import (
    get_db, get_user, update_user, ban_user, unban_user, get_all_users
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


class UpdateUserRequest(BaseModel):
    """Update user request"""
    plan: Optional[str] = None
    parallel_slots: Optional[int] = None
    storage_limit_gb: Optional[float] = None
    validity_from: Optional[str] = None
    validity_to: Optional[str] = None



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
    admin_id: int = Depends(get_current_admin)
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
            user_responses.append(UserResponse(
                telegram_id=user.get("telegram_id"),
                first_name=user.get("first_name", ""),
                username=user.get("username"),
                plan=user.get("plan", "free"),
                banned=user.get("banned", False),
                parallel_slots=user.get("parallel_slots"),
                storage_limit=user.get("storage_limit"),
                validity_from=user.get("validity_from"),
                validity_to=user.get("validity_to")
            ))
        
        logger.info(f"✅ Users list retrieved: {len(users)} total")
        
        return UserListResponse(
            total=len(users),
            users=user_responses
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ List users error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/users/{user_id}/ban")
async def ban_user_endpoint(user_id: int, request: BanUserRequest, admin_id: int = Depends(get_current_admin)):
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
        success = await update_user(user_id, {
            "plan": "pro",
            "storage_limit": 10 * 1024 * 1024 * 1024,  # 10GB
            "daily_limit": None,
            "parallel_slots": 5
        }, admin_id)
        
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
async def update_user_endpoint(user_id: int, request: UpdateUserRequest, admin_id: int = Depends(get_current_admin)):
    """
    Update a user
    """
    try:
        db = get_db()
        updates = {}
        if request.plan is not None:
            updates['plan'] = request.plan
        if request.parallel_slots is not None:
            updates['parallel_slots'] = request.parallel_slots
        if request.storage_limit_gb is not None:
            updates['storage_limit'] = int(request.storage_limit_gb * 1024 * 1024 * 1024)
        if request.validity_from is not None:
            updates['validity_from'] = request.validity_from
        if request.validity_to is not None:
            updates['validity_to'] = request.validity_to

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

