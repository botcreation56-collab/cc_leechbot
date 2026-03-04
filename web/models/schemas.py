"""
Pydantic schemas for FastAPI endpoints
"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ============================================
# Authentication Schemas
# ============================================

class RequestCodeRequest(BaseModel):
    """Request one-time code"""
    user_id: int = Field(..., gt=0)


class VerifyCodeRequest(BaseModel):
    """Verify one-time code"""
    user_id: int = Field(..., gt=0)
    code: str = Field(..., min_length=10)


class AuthResponse(BaseModel):
    """Authentication response"""
    status: str
    message: str
    token: Optional[str] = None


# ============================================
# User Schemas
# ============================================

class UserBase(BaseModel):
    """Base user schema"""
    telegram_id: int
    first_name: str
    username: Optional[str] = None
    plan: str = "free"


class UserCreate(UserBase):
    """Create user"""
    pass


class UserUpdate(BaseModel):
    """Update user"""
    plan: Optional[str] = None
    banned: Optional[bool] = None
    ban_reason: Optional[str] = None


class UserResponse(BaseModel):
    """User response"""
    telegram_id: int
    first_name: str
    username: Optional[str]
    plan: str
    banned: bool
    created_at: datetime


class UserListResponse(BaseModel):
    """User list response"""
    total: int
    users: List[UserResponse]


# ============================================
# Dashboard Schemas
# ============================================

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


# ============================================
# Config Schemas
# ============================================

class ConfigRequest(BaseModel):
    """Config update request"""
    max_upload_size: Optional[int] = None
    retention_free_days: Optional[int] = None
    retention_pro_days: Optional[int] = None
    header_text: Optional[str] = None
    footer_text: Optional[str] = None


class ConfigResponse(BaseModel):
    """Config response"""
    max_upload_size: int
    retention_free_days: int
    retention_pro_days: int
    header_text: str
    footer_text: str


# ============================================
# Broadcast Schemas
# ============================================

class BroadcastRequest(BaseModel):
    """Broadcast message request"""
    target: str = Field(..., regex=r"^(all|free|pro)$")
    message: str = Field(..., min_length=10)


class BroadcastResponse(BaseModel):
    """Broadcast response"""
    status: str
    sent: int
    failed: int
    total: int


# ============================================
# Error Response
# ============================================

class ErrorResponse(BaseModel):
    """Error response"""
    status: str = "error"
    message: str
    code: int
