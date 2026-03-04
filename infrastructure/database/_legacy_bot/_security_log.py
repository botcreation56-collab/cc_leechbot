"""
bot/database/_security_log.py — Audit trail and security event logging.

All writes go to dedicated collections and are non-blocking / fail-safe.
Callers are never disrupted by logging failures.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from infrastructure.database._legacy_bot._connection import get_db

logger = logging.getLogger("filebot.db.security_log")


async def log_admin_action(
    admin_id: int, action: str, details: Optional[Dict] = None
) -> bool:
    """Log admin action for audit trail."""
    try:
        db = get_db()
        await db.admin_logs.insert_one({
            "admin_id": admin_id,
            "action": action,
            "details": details or {},
            "timestamp": datetime.utcnow(),
        })
        return True
    except Exception as e:
        logger.error(f"❌ Log admin action failed: {e}", exc_info=True)
        return False


async def log_security_event(
    user_id: int,
    event_type: str,
    severity: str = "medium",
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Write a structured security event to the security_logs collection.
    Non-blocking: silently absorbs all database errors so callers are not disrupted.

    Args:
        user_id:    Telegram user ID associated with the event.
        event_type: Short slug describing what happened (e.g. "unauthorized_admin_access").
        severity:   One of "low", "medium", "high", "critical".
        details:    Optional extra context to store alongside the event.
    """
    try:
        db = get_db()
        if db is None:
            return
        await db.security_logs.insert_one({
            "user_id": user_id,
            "event_type": event_type,
            "severity": severity,
            "details": details or {},
            "timestamp": datetime.utcnow(),
        })
    except Exception as exc:
        # Security logging must NEVER crash the caller
        logger.debug("log_security_event failed (non-fatal): %s", exc)


async def add_action(
    admin_id: int, action_type: str, target_id: Optional[int] = None, details: str = ""
) -> bool:
    """Log generic action for audit trail."""
    try:
        db = get_db()
        await db.actions.insert_one({
            "admin_id": admin_id,
            "action_type": action_type,
            "target_id": target_id,
            "details": details,
            "timestamp": datetime.utcnow(),
        })
        logger.info(f"✅ Action logged: {action_type} by admin {admin_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Add action failed: {e}", exc_info=True)
        return False


async def get_admin_stats() -> Dict[str, Any]:
    """Get comprehensive bot statistics."""
    try:
        db = get_db()
        total_users = await db.users.count_documents({})
        banned_users = await db.users.count_documents({"banned": True})
        premium_users = await db.users.count_documents({"plan": "premium"})
        pending_tasks = await db.tasks.count_documents({"status": "queued"})
        processing_tasks = await db.tasks.count_documents({"status": "processing"})
        completed_tasks = await db.tasks.count_documents({"status": "completed"})
        rclone_configs = await db.rclone_configs.count_documents({"is_active": True})
        pending_broadcasts = await db.broadcasts.count_documents({"status": "pending"})

        stats = {
            "total_users": total_users,
            "active_users": total_users - banned_users,
            "banned_users": banned_users,
            "premium_users": premium_users,
            "free_users": total_users - premium_users,
            "pending_tasks": pending_tasks,
            "processing_tasks": processing_tasks,
            "completed_tasks": completed_tasks,
            "rclone_configs": rclone_configs,
            "pending_broadcasts": pending_broadcasts,
        }
        logger.info("✅ Admin stats compiled")
        return stats

    except Exception as e:
        logger.error(f"❌ Get admin stats failed: {e}", exc_info=True)
        return {}
