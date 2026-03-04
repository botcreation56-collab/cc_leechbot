"""
bot/models.py — Pydantic V2 data models.

Consolidated from: broadcast.py, cloud_file.py, config.py, rclone_config.py, task.py, user.py
Single import block, all Pydantic V1 deprecations removed.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_config


# ============================================================
# BROADCAST
# ============================================================

class Broadcast(BaseModel):
    """Broadcast message model."""

    broadcast_id: str
    created_by: int
    message: str
    target: str          # "all" | "free" | "pro"
    status: str = "draft"  # "draft" | "sending" | "completed"
    sent_count: int = 0
    failed_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    sent_at: Optional[datetime] = None


# ============================================================
# CLOUD FILE
# ============================================================

class CloudFile(BaseModel):
    """Cloud file metadata."""

    file_id: str
    user_id: int
    filename: str
    file_size: int
    cloud_type: str         # "telegram" | "gdrive" | "onedrive" | "terabox"
    cloud_url: str
    shared_link: Optional[str] = None
    password: Optional[str] = None
    views: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expiry_date: datetime
    is_deleted: bool = False


# ============================================================
# BOT CONFIGURATION
# ============================================================

class BotConfig(BaseModel):
    """Bot configuration model."""

    max_upload_size: int = 4_294_967_296        # 4 GB
    retention_free_days: int = 7
    retention_pro_days: int = 28
    header_text: str = "📥 Upload files and get cloud links"
    footer_text: str = "Powered by FileBot"
    maintenance_mode: bool = False
    emergency_message: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================
# RCLONE CONFIGURATION
# ============================================================

class RcloneConfig(BaseModel):
    """Rclone configuration model."""

    config_id: str
    service: str                    # "gdrive" | "onedrive" | "dropbox" | "mega" | "s3"
    plan: str = Field(default="free", pattern=r"^(free|pro)$")  # Pydantic V2 pattern=
    max_users: int
    created_by: int
    created_at: datetime = Field(default_factory=datetime.utcnow)
    credentials_encrypted: str
    is_active: bool = True


# ============================================================
# TASK
# ============================================================

class TaskStatus(str, Enum):
    """Task lifecycle states."""
    PENDING     = "pending"
    DOWNLOADING = "downloading"
    PROCESSING  = "processing"
    UPLOADING   = "uploading"
    COMPLETED   = "completed"
    FAILED      = "failed"
    CANCELLED   = "cancelled"


class Task(BaseModel):
    """File processing task model."""

    task_id: str
    user_id: int
    file_url: str
    filename: str
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    file_size: int = 0
    cloud_url: Optional[str] = None


# ============================================================
# USER
# ============================================================

class UserSettings(BaseModel):
    """Embedded user settings sub-document."""
    prefix: str = ""
    suffix: str = ""
    mode: str = "video"
    metadata: dict = Field(default_factory=dict)
    destination_channel: Optional[int] = None
    destination_metadata: dict = Field(default_factory=dict)
    remove_words: List[str] = Field(default_factory=list)
    thumbnail: str = "auto"
    thumbnail_file_id: Optional[str] = None


class User(BaseModel):
    """User model — Pydantic V2 compatible."""

    model_config = model_config(arbitrary_types_allowed=True)

    telegram_id: int = Field(..., gt=0)
    first_name: str
    username: Optional[str] = None
    plan: str = Field(default="free", pattern=r"^(free|pro|premium)$")  # V2 pattern=
    banned: bool = False
    ban_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    storage_limit: int = Field(default=5_368_709_120)   # 5 GB
    used_storage: int = 0
    daily_limit: int = 5
    daily_used: int = 0
    parallel_slots: int = 1
    notifications_enabled: bool = True
    settings: UserSettings = Field(default_factory=UserSettings)
    files_processed: int = 0

    @field_validator("first_name")  # Pydantic V2 field_validator
    @classmethod
    def validate_first_name(cls, v: str) -> str:
        if not v or len(v) > 100:
            raise ValueError("first_name must be 1–100 characters")
        return v
