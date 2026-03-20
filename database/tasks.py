"""
database/tasks.py — Task lifecycle management.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from database.connection import get_db
from database.users import get_user

logger = logging.getLogger("filebot.db.tasks")


async def create_task(
    user_id: int,
    file_id: str,
    task_type: str = "upload",
    metadata: Optional[Dict] = None,
) -> str:
    """Create a new task for file processing/queueing."""
    try:
        db = get_db()
        task_id = str(uuid.uuid4())
        now = datetime.utcnow()

        user = await get_user(user_id)
        plan = user.get("plan", "free") if user else "free"
        priority = 10 if plan == "pro" else 0

        task_doc: Dict[str, Any] = {
            "task_id": task_id,
            "user_id": user_id,
            "file_id": file_id,
            "type": task_type,
            "status": "pending",
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now,
            "progress": 0,
            "error": None,
            "plan": plan,
            "priority": priority,
        }

        await db.tasks.insert_one(task_doc)
        logger.info(f"✅ Task created: {task_id} for user {user_id}, type: {task_type}")
        return task_id

    except Exception as e:
        logger.error(f"❌ create_task({user_id}, {file_id}) failed: {e}", exc_info=True)
        return ""


async def get_task(task_id: str) -> Optional[Dict]:
    """Fetch a single task by ID."""
    try:
        db = get_db()
        task = await db.tasks.find_one({"task_id": task_id})
        if task:
            logger.debug(f"✅ Task fetched: {task_id}")
        return task
    except Exception as e:
        logger.error(f"❌ get_task({task_id}) failed: {e}")
        return None


async def update_task(task_id: str, updates: Dict[str, Any]) -> bool:
    """Update task status, progress, etc."""
    try:
        db = get_db()
        updates["updated_at"] = datetime.utcnow()
        result = await db.tasks.update_one({"task_id": task_id}, {"$set": updates})
        if result.modified_count > 0:
            logger.info(
                f"✅ Task updated: {task_id} -> {updates.get('status', 'unknown')}"
            )
            return True
        logger.warning(f"⚠️ Task not found for update: {task_id}")
        return False
    except Exception as e:
        logger.error(f"❌ update_task({task_id}) failed: {e}", exc_info=True)
        return False


async def get_user_tasks(
    user_id: int,
    status: Optional[str] = None,
    limit: int = 20,
    exclude_terminal: bool = False,
) -> List[Dict]:
    """Fetch user's tasks with optional status filter.

    Args:
        user_id: The user ID
        status: Filter by specific status (optional)
        limit: Maximum number of tasks to return
        exclude_terminal: If True, exclude completed/failed/cancelled/expired tasks
    """
    try:
        db = get_db()
        query = {"user_id": user_id}
        if status:
            query["status"] = status
        elif exclude_terminal:
            # Exclude terminal states by default when showing "active" tasks
            query["status"] = {"$nin": ["completed", "failed", "cancelled", "expired"]}
        tasks = (
            await db.tasks.find(query).sort("created_at", -1).limit(limit).to_list(None)
        )
        logger.debug(f"Fetched {len(tasks)} tasks for user {user_id}")
        return tasks
    except Exception as e:
        logger.error(f"❌ get_user_tasks({user_id}) failed: {e}")
        return []


async def complete_task(task_id: str, result: Optional[Dict] = None) -> bool:
    """Mark task as completed with optional result data."""
    updates = {"status": "completed", "progress": 100}
    if result:
        updates["result"] = result
    return await update_task(task_id, updates)


async def fail_task(task_id: str, error_msg: str) -> bool:
    """Mark task as failed with error."""
    return await update_task(
        task_id, {"status": "failed", "error": error_msg, "progress": 0}
    )


async def get_active_task_count(user_id: int) -> int:
    """Count tasks currently in 'processing' status for a specific user."""
    try:
        db = get_db()
        count = await db.tasks.count_documents(
            {
                "user_id": user_id,
                "status": "processing",
            }
        )
        return count
    except Exception as e:
        logger.error(f"❌ get_active_task_count({user_id}) failed: {e}")
        return 0


async def get_user_position(user_id: int) -> int:
    """Get user's approximate queue position based on queued tasks ahead."""
    try:
        db = get_db()
        # Count only queued tasks (not cancelled, not completed, not failed)
        ahead_count = await db.tasks.count_documents(
            {
                "user_id": {"$ne": user_id},
                "status": "queued",
            }
        )
        position = ahead_count + 1
        logger.debug(f"Queue position for {user_id}: {position}")
        return position
    except Exception as e:
        logger.error(f"❌ get_user_position({user_id}) failed: {e}")
        return 0


async def cleanup_old_tasks(days_old: int = 30) -> Dict[str, int]:
    """Remove completed/failed tasks older than days_old."""
    try:
        db = get_db()
        cutoff = datetime.utcnow() - timedelta(days=days_old)
        result = await db.tasks.delete_many(
            {
                "status": {"$in": ["completed", "failed"]},
                "updated_at": {"$lt": cutoff},
            }
        )
        logger.info(f"✅ Cleaned {result.deleted_count} old tasks")
        return {"deleted_count": result.deleted_count}
    except Exception as e:
        logger.error(f"❌ cleanup_old_tasks failed: {e}")
        return {"deleted_count": 0}
