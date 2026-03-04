"""
bot/database/_rclone.py — Rclone remote storage operations (config CRUD + upload/delete).

Note: upload_to_rclone / delete_from_rclone use asyncio subprocesses (non-blocking).
"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from infrastructure.database._legacy_bot._connection import get_db
from infrastructure.database._legacy_bot._security_log import log_admin_action

logger = logging.getLogger("filebot.db.rclone")

RCLONE_SUPPORTED_SERVICES = ["gdrive", "onedrive", "dropbox", "mega", "s3"]


async def add_rclone_config(
    service: str,
    plan: str,
    max_users: int,
    credentials: str,
    admin_id: int,
) -> Optional[str]:
    """Add rclone configuration to MongoDB."""
    try:
        db = get_db()

        if service.lower() not in RCLONE_SUPPORTED_SERVICES:
            logger.error(f"❌ Unsupported service: {service}")
            return None

        config_id = f"rclone_{service}_{plan}_{uuid.uuid4().hex[:8]}"

        rclone_config = {
            "config_id": config_id,
            "service": service.lower(),
            "plan": plan,
            "max_users": max_users,
            "current_users": 0,
            "credentials": credentials,
            "created_at": datetime.utcnow(),
            "created_by": admin_id,
            "is_active": True,
            "test_status": "pending",
            "last_tested": None,
            "error_count": 0,
        }

        await db.rclone_configs.insert_one(rclone_config)
        await log_admin_action(
            admin_id,
            "rclone_config_added",
            {"config_id": config_id, "service": service, "plan": plan},
        )
        logger.info(f"✅ Rclone config added: {config_id}")
        return config_id

    except Exception as e:
        logger.error(f"❌ Add rclone config failed: {e}", exc_info=True)
        return None


async def get_rclone_configs(
    service: Optional[str] = None,
    plan: Optional[str] = None,
    is_active: bool = True,
) -> List[Dict[str, Any]]:
    """Get rclone configurations with optional filters."""
    try:
        db = get_db()
        filter_dict: Dict[str, Any] = {"is_active": is_active}
        if service is not None:
            filter_dict["service"] = service.lower()
        if plan is not None:
            filter_dict["plan"] = plan

        configs = await db.rclone_configs.find(filter_dict).to_list(length=100)
        logger.info(f"✅ Retrieved {len(configs) if configs else 0} rclone configs")
        return configs if configs is not None else []

    except Exception as e:
        logger.error(f"❌ Get rclone configs failed: {e}", exc_info=True)
        return []


async def get_rclone_config(config_id: str = None) -> Optional[Dict[str, Any]]:
    """Get a specific rclone config by ID, or the first active config."""
    try:
        db = get_db()
        if config_id:
            return await db.rclone_configs.find_one({"config_id": config_id})
        return await db.rclone_configs.find_one({"is_active": True})
    except Exception as e:
        logger.error(f"❌ get_rclone_config failed: {e}", exc_info=True)
        return None


async def pick_rclone_config_for_plan(plan: str) -> Optional[Dict[str, Any]]:
    """Pick best rclone config for a plan (load balancing by current_users)."""
    try:
        db = get_db()
        configs = await (
            db.rclone_configs.find({"is_active": True, "plan": plan})
            .sort("current_users", 1)
            .limit(1)
            .to_list(length=1)
        )
        if configs:
            logger.info(f"✅ Selected rclone config: {configs[0].get('service')}")
            return configs[0]
        logger.warning(f"⚠️ No active rclone config for plan: {plan}")
        return None
    except Exception as e:
        logger.error(f"❌ Pick rclone config failed: {e}", exc_info=True)
        return None


async def increment_rclone_usage(config_id: str, delta: int = 1) -> bool:
    """Increase/decrease rclone config usage counter."""
    try:
        db = get_db()
        result = await db.rclone_configs.update_one(
            {"config_id": config_id},
            {"$inc": {"current_users": delta}},
        )
        if result.modified_count > 0:
            logger.info(f"✅ Rclone usage updated: {config_id} by {delta}")
            return True
        logger.warning(f"⚠️ Config not found: {config_id}")
        return False
    except Exception as e:
        logger.error(f"❌ Increment rclone usage failed: {e}", exc_info=True)
        return False


async def upload_to_rclone(
    file_path: str, remote_name: str, folder_path: str = "/"
) -> Optional[Dict[str, Any]]:
    """
    Upload file to rclone remote and return shareable link.
    Uses asyncio.create_subprocess_exec (non-blocking).
    """
    try:
        from pathlib import Path as _Path
        remote_path = f"{remote_name}:{folder_path}"
        full_remote = f"{remote_path}/{_Path(file_path).name}"

        cmd = ["rclone", "copy", file_path, full_remote, "--progress", "--transfers=1"]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=1800)

        if process.returncode != 0:
            logger.error(f"Rclone upload failed: {stderr.decode(errors='ignore')[:300]}")
            return None

        cloud_url = f"{remote_path} (access via rclone)"
        if remote_name == "gdrive":
            link_proc = await asyncio.create_subprocess_exec(
                "rclone", "link", full_remote,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            link_out, _ = await asyncio.wait_for(link_proc.communicate(), timeout=30)
            if link_proc.returncode == 0:
                cloud_url = link_out.decode().strip()

        logger.info(f"✅ Rclone upload complete → {cloud_url}")
        return {
            "success": True,
            "cloud_url": cloud_url,
            "file_id": full_remote,
            "uploaded_at": datetime.utcnow(),
        }

    except asyncio.TimeoutError:
        logger.error("❌ Rclone upload timed out (30 min)")
        return None
    except Exception as e:
        logger.error(f"❌ Rclone upload error: {e}")
        return None


async def delete_from_rclone(remote_name: str, file_id: str) -> bool:
    """Delete expired file from rclone remote (non-blocking)."""
    try:
        process = await asyncio.create_subprocess_exec(
            "rclone", "delete", file_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(process.communicate(), timeout=60)
        return process.returncode == 0
    except Exception as e:
        logger.error(f"❌ Rclone delete error: {e}")
        return False
