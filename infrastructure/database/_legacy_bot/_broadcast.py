"""
bot/database/_broadcast.py — Broadcast draft lifecycle (create, update, send, query).

Note: get_broadcasts is the unified canonical version (draft_id-based).
The older create_broadcast_message function is preserved for compatibility.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from infrastructure.database._legacy_bot._connection import get_db
from infrastructure.database._legacy_bot._security_log import log_admin_action

logger = logging.getLogger("filebot.db.broadcast")


async def get_broadcasts(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get all broadcasts or filter by status."""
    try:
        db = get_db()
        query: Dict[str, Any] = {}
        if status:
            query["status"] = status
        cursor = db.broadcasts.find(query).sort("created_at", -1)
        result = await cursor.to_list(length=100)
        logger.info(f"✅ Retrieved broadcasts: status={status}, count={len(result) if result else 0}")
        return result if result is not None else []
    except Exception as e:
        logger.error(f"❌ get_broadcasts error: {e}", exc_info=True)
        return []


async def create_broadcast_draft(
    admin_id: int, message: str, target: str = "all"
) -> Optional[str]:
    """Create a new broadcast draft."""
    try:
        db = get_db()
        draft = {
            "draft_id": str(uuid.uuid4()),
            "admin_id": admin_id,
            "message": message,
            "target": target,
            "status": "draft",
            "created_at": datetime.utcnow().isoformat(),
            "sent_count": 0,
            "failed_count": 0,
        }
        await db.broadcasts.insert_one(draft)
        return draft["draft_id"]
    except Exception as e:
        logger.error(f"create_broadcast_draft error: {e}")
        return None


async def update_broadcast_draft(draft_id: str, updates: dict) -> bool:
    """Update draft (e.g. confirm send)."""
    try:
        db = get_db()
        await db.broadcasts.update_one({"draft_id": draft_id}, {"$set": updates})
        return True
    except Exception as e:
        logger.error(f"update_broadcast_draft error: {e}")
        return False


async def send_broadcast(draft_id: str) -> bool:
    """Mark broadcast as sent."""
    try:
        db = get_db()
        result = await db.broadcasts.update_one(
            {"draft_id": draft_id},
            {"$set": {"status": "sent", "sent_at": datetime.utcnow().isoformat()}},
        )
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"send_broadcast error: {e}")
        return False


async def create_broadcast_message(
    message_text: str, target_plan: str, admin_id: int
) -> Optional[str]:
    """Create broadcast message (legacy API, delegates to collection insert)."""
    try:
        db = get_db()
        broadcast_id = f"bcast_{uuid.uuid4().hex[:12]}"
        broadcast = {
            "broadcast_id": broadcast_id,
            "message_text": message_text,
            "target_plan": target_plan,
            "status": "pending",
            "created_by": admin_id,
            "created_at": datetime.utcnow(),
            "sent_count": 0,
            "failed_count": 0,
            "sent_at": None,
        }
        await db.broadcasts.insert_one(broadcast)
        await log_admin_action(
            admin_id,
            "broadcast_created",
            {"broadcast_id": broadcast_id, "target_plan": target_plan},
        )
        logger.info(f"✅ Broadcast created: {broadcast_id}")
        return broadcast_id
    except Exception as e:
        logger.error(f"❌ Create broadcast failed: {e}", exc_info=True)
        return None
