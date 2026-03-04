"""
Admin logs API routes
"""

import logging
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

from bot.database import get_db
from web.routes.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


class LogEntry(BaseModel):
    """Log entry"""
    timestamp: datetime
    level: str
    message: str
    user_id: Optional[int] = None


class LogResponse(BaseModel):
    """Log response"""
    total: int
    logs: List[LogEntry]


@router.get("/logs", response_model=LogResponse)
async def get_logs(
    skip: int = Query(0),
    limit: int = Query(50),
    level: Optional[str] = None,
    token: str = Query(...)
):
    """
    Get admin logs
    
    Args:
        skip: Skip N logs
        limit: Limit results
        level: Filter by level (INFO, WARNING, ERROR)
        token: JWT token
        
    Returns:
        Logs list
    """
    try:
        # Verify admin
        admin_id = await get_current_user(token)
        from config.settings import get_admin_ids
        
        admin_ids = get_admin_ids()
        if admin_id not in admin_ids:
            raise HTTPException(status_code=403, detail="Not authorized")
        
        # Get logs from database
        db = get_db()
        query = {}
        if level:
            query["level"] = level
        
        logs = await db.security_logs.find(query).skip(skip).limit(limit).to_list(limit)
        total = await db.security_logs.count_documents(query)
        
        logger.info(f"✅ Logs retrieved: {admin_id}")
        
        return LogResponse(total=total, logs=logs)
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Get logs error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
