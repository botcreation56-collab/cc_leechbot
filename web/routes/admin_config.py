"""
Admin configuration API routes
"""

import logging
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional

from bot.database import get_config, update_config
from web.routes.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


class ConfigRequest(BaseModel):
    """Config update request"""

    max_upload_size: Optional[int] = None
    retention_free_days: Optional[int] = None
    retention_pro_days: Optional[int] = None
    header_text: Optional[str] = None
    footer_text: Optional[str] = None
    start_message: Optional[str] = None
    help_text: Optional[str] = None
    support_contact: Optional[str] = None
    watermark: Optional[str] = None
    log_channel: Optional[int] = None
    dump_channel: Optional[int] = None
    storage_channel: Optional[int] = None
    force_sub_channel: Optional[int] = None


class ConfigResponse(BaseModel):
    """Config response"""

    max_upload_size: int
    retention_free_days: int
    retention_pro_days: int
    header_text: str
    footer_text: str
    start_message: str
    help_text: str
    support_contact: str
    watermark: str
    log_channel: Optional[int]
    dump_channel: Optional[int]
    storage_channel: Optional[int]
    force_sub_channel: Optional[int]


# ============================================================
# GET CONFIG
# ============================================================
@router.get("/config", response_model=ConfigResponse)
async def get_config_endpoint(admin_id: int = Depends(get_current_user)):
    """Get bot configuration"""
    try:
        from config.settings import get_admin_ids

        if admin_id not in get_admin_ids():
            raise HTTPException(status_code=403, detail="Not authorized")

        config = await get_config() or {}
        channels = config.get("channels", {})
        logger.info(f"✅ Config retrieved: admin={admin_id}")

        def get_nested_id(ch_val):
            if isinstance(ch_val, dict):
                return ch_val.get("id")
            return ch_val

        return ConfigResponse(
            max_upload_size=config.get("max_upload_size", 4294967296),
            retention_free_days=config.get("retention_free_days", 7),
            retention_pro_days=config.get("retention_pro_days", 28),
            header_text=config.get("header_text", ""),
            footer_text=config.get("footer_text", ""),
            start_message=config.get(
                "start_message", "Welcome to the File Processor Bot!"
            ),
            help_text=config.get("help_text", "Send a file to start."),
            support_contact=config.get("support_contact", "@admin"),
            watermark=config.get("watermark", ""),
            log_channel=get_nested_id(channels.get("log")) or config.get("log_channel"),
            dump_channel=get_nested_id(channels.get("dump"))
            or config.get("dump_channel"),
            storage_channel=get_nested_id(channels.get("storage"))
            or config.get("storage_channel"),
            force_sub_channel=get_nested_id(channels.get("force_sub"))
            or config.get("force_sub_channel"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Get config error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================
# UPDATE CONFIG
# ============================================================
@router.post("/config")
async def update_config_endpoint(
    request: ConfigRequest, admin_id: int = Depends(get_current_user)
):
    """Update bot configuration"""
    try:
        from config.settings import get_admin_ids

        if admin_id not in get_admin_ids():
            raise HTTPException(status_code=403, detail="Not authorized")

        updates = {}
        if request.max_upload_size is not None:
            updates["max_upload_size"] = request.max_upload_size
        if request.retention_free_days is not None:
            updates["retention_free_days"] = request.retention_free_days
        if request.retention_pro_days is not None:
            updates["retention_pro_days"] = request.retention_pro_days
        if request.header_text is not None:
            updates["header_text"] = request.header_text
        if request.footer_text is not None:
            updates["footer_text"] = request.footer_text
        if request.start_message is not None:
            updates["start_message"] = request.start_message
        if request.help_text is not None:
            updates["help_text"] = request.help_text
        if request.support_contact is not None:
            updates["support_contact"] = request.support_contact
        if request.watermark is not None:
            updates["watermark"] = request.watermark
        if request.log_channel is not None:
            updates["log_channel"] = request.log_channel
            updates["channels.log"] = {"id": request.log_channel}
        if request.dump_channel is not None:
            updates["dump_channel"] = request.dump_channel
            updates["channels.dump"] = {"id": request.dump_channel}
        if request.storage_channel is not None:
            updates["storage_channel"] = request.storage_channel
            updates["channels.storage"] = {"id": request.storage_channel}
        if request.force_sub_channel is not None:
            updates["force_sub_channel"] = request.force_sub_channel
            updates["channels.force_sub"] = {"id": request.force_sub_channel}

        if not updates:
            raise HTTPException(status_code=400, detail="No updates provided")

        success = await update_config(updates, admin_id)
        if not success:
            raise HTTPException(status_code=400, detail="Failed to update config")

        logger.info(f"✅ Config updated: admin={admin_id}")
        return {"status": "success", "message": "Configuration updated"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Update config error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================
# PLANS MANAGEMENT
# ============================================================


class PlanItem(BaseModel):
    price: int
    parallel: int
    storage_per_day: int
    dump_expiry_days: int
    max_file_size_gb: int = 5


class PlansUpdateRequest(BaseModel):
    free: Optional[PlanItem] = None
    pro: Optional[PlanItem] = None


@router.get("/plans")
async def get_plans_endpoint(admin_id: int = Depends(get_current_user)):
    try:
        from config.settings import get_admin_ids

        if admin_id not in get_admin_ids():
            raise HTTPException(status_code=403, detail="Not authorized")

        config = await get_config() or {}
        plans = config.get(
            "plans",
            {
                "free": {
                    "price": 0,
                    "parallel": 1,
                    "storage_per_day": 5,
                    "dump_expiry_days": 0,
                    "max_file_size_gb": 5,
                },
                "premium": {
                    "price": 499,
                    "parallel": 3,
                    "storage_per_day": 50,
                    "dump_expiry_days": 30,
                    "max_file_size_gb": 10,
                },
            },
        )

        # Ensure 'pro' key exists if UI expects it (or just return the whole plans dict)
        if "premium" in plans and "pro" not in plans:
            plans["pro"] = plans["premium"]

        return plans
    except Exception as e:
        logger.error(f"❌ Get plans error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/plans")
async def update_plans_endpoint(
    request: PlansUpdateRequest, admin_id: int = Depends(get_current_user)
):
    try:
        from config.settings import get_admin_ids

        if admin_id not in get_admin_ids():
            raise HTTPException(status_code=403, detail="Not authorized")

        config = await get_config() or {}
        plans = config.get("plans", {})

        if request.free:
            plans["free"] = request.free.dict()
        if request.pro:
            plans["premium"] = request.pro.dict()

        success = await update_config({"plans": plans}, admin_id)
        if not success:
            raise HTTPException(status_code=400, detail="Failed to update plans")

        return {"status": "success", "message": "Plans updated"}
    except Exception as e:
        logger.error(f"❌ Update plans error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================
# BROADCAST
# ============================================================


class BroadcastRequest(BaseModel):
    message: str


@router.post("/broadcast")
async def trigger_broadcast_endpoint(
    request: BroadcastRequest, admin_id: int = Depends(get_current_user)
):
    try:
        from config.settings import get_admin_ids

        if admin_id not in get_admin_ids():
            raise HTTPException(status_code=403, detail="Not authorized")

        from bot.database import create_broadcast_draft
        import asyncio
        from bot.services import run_broadcast_worker

        # We start the broadcast by creating a draft aimed at "all" users
        broadcast_id = await create_broadcast_draft(admin_id, request.message, "all")
        if not broadcast_id:
            raise HTTPException(
                status_code=500, detail="Failed to create broadcast draft in DB"
            )

        # Spin up the background worker (fire and forget)
        asyncio.create_task(run_broadcast_worker(broadcast_id))
        return {
            "status": "success",
            "message": "Global Broadcast initiated in the background!",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Broadcast trigger error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
