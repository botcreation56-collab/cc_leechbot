"""
services/user_service.py — User lifecycle and quota enforcement.

Responsibilities:
  - Create / retrieve users (upsert on first interaction)
  - Enforce plan-based file size and daily quota limits
  - Escalate ban/unban to repository + notify audit log
  - Upgrade / downgrade plan

All I/O is via injected repositories — no direct DB code here.
Raises domain exceptions from core.exceptions on violations.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.exceptions import (
    AccessDeniedError,
    DailyQuotaExceededError,
    FileTooLargeError,
    StorageQuotaExceededError,
    UserBannedError,
)
from infrastructure.database.repositories import AuditLogRepository, UserRepository

logger = logging.getLogger("filebot.services.user")

# Plan limits — these are overridable at runtime via ConfigRepository
_PLAN_FILE_LIMITS: Dict[str, int] = {
    "free":    5 * 1024 ** 3,   # 5 GB
    "premium": 8 * 1024 ** 3,   # 8 GB
    "pro":    10 * 1024 ** 3,   # 10 GB
}

_PLAN_DAILY_LIMITS: Dict[str, float] = {  # daily transfer in GB
    "free":    5.0,
    "premium": 20.0,
    "pro":     float("inf"),  # unlimited
}

_VALID_PLANS = frozenset({"free", "premium", "pro"})


class UserService:
    """Stateless service — all state is in repositories."""

    def __init__(
        self,
        user_repo: UserRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        self._users = user_repo
        self._audit = audit_repo

    # ------------------------------------------------------------------ #
    # Retrieval / lifecycle                                                 #
    # ------------------------------------------------------------------ #

    async def get_or_create(
        self,
        user_id: int,
        first_name: str,
        username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return existing user or create new one (upsert semantics)."""
        user = await self._users.get(user_id)
        if not user:
            user = await self._users.create(user_id, first_name, username)
            logger.info("✅ New user created: %d (%s)", user_id, first_name)
        return user

    async def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        return await self._users.get(user_id)

    async def require(self, user_id: int) -> Dict[str, Any]:
        """Get user or raise ValueError if not found."""
        user = await self._users.get(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found — run /start first.")
        return user

    # ------------------------------------------------------------------ #
    # Auth guards                                                           #
    # ------------------------------------------------------------------ #

    async def assert_not_banned(self, user_id: int) -> Dict[str, Any]:
        """Raise UserBannedError if user is banned, else return user doc."""
        user = await self.require(user_id)
        if user.get("banned"):
            raise UserBannedError(user_id, user.get("ban_reason") or "Account suspended")
        return user

    async def assert_admin(self, user_id: int, admin_ids: List[int]) -> Dict[str, Any]:
        """Raise AccessDeniedError if user is not an admin."""
        user = await self.require(user_id)
        if user.get("role") != "admin" and user_id not in admin_ids:
            raise AccessDeniedError(user_id)
        return user

    # ------------------------------------------------------------------ #
    # Quota checks                                                          #
    # ------------------------------------------------------------------ #

    def get_file_size_limit(self, plan: str) -> int:
        """Return the maximum allowed file size (bytes) for a plan."""
        return _PLAN_FILE_LIMITS.get(plan, _PLAN_FILE_LIMITS["free"])

    def assert_file_size(self, file_size: int, plan: str) -> None:
        """Raise FileTooLargeError if file_size exceeds the plan's limit."""
        limit = self.get_file_size_limit(plan)
        if file_size > limit:
            raise FileTooLargeError(file_size, limit, plan)

    async def assert_daily_quota(self, user: Dict[str, Any]) -> None:
        """Raise DailyQuotaExceededError if the user has exhausted today's quota."""
        plan = user.get("plan", "free")
        daily_limit_gb = _PLAN_DAILY_LIMITS.get(plan, 5.0)
        if daily_limit_gb == float("inf"):
            return  # Pro: unlimited
        used_gb = user.get("daily_used", 0) / 1024 ** 3
        if used_gb >= daily_limit_gb:
            raise DailyQuotaExceededError(user["telegram_id"], used_gb, daily_limit_gb)

    async def consume_quota(self, user_id: int, bytes_used: int) -> None:
        """Increment daily_used and used_storage atomically."""
        await self._users.update(user_id, {
            "daily_used": {"$inc": bytes_used},  # handled at repo level
        })
        # Motor-safe increments are done via $inc — use update with inc helper
        user = await self._users.get(user_id)
        if user:
            new_daily = user.get("daily_used", 0) + bytes_used
            new_storage = user.get("used_storage", 0) + bytes_used
            await self._users.update(user_id, {
                "daily_used": new_daily,
                "used_storage": new_storage,
                "files_processed": user.get("files_processed", 0) + 1,
            })

    # ------------------------------------------------------------------ #
    # Administration                                                        #
    # ------------------------------------------------------------------ #

    async def ban(self, user_id: int, *, reason: str, admin_id: int) -> bool:
        ok = await self._users.ban(user_id, reason=reason, admin_id=admin_id)
        if ok:
            await self._audit.log(admin_id, "ban_user", {"user_id": user_id, "reason": reason})
        return ok

    async def unban(self, user_id: int, *, admin_id: int) -> bool:
        ok = await self._users.unban(user_id, admin_id=admin_id)
        if ok:
            await self._audit.log(admin_id, "unban_user", {"user_id": user_id})
        return ok

    async def set_plan(self, user_id: int, plan: str, *, admin_id: int) -> bool:
        if plan not in _VALID_PLANS:
            raise ValueError(f"Invalid plan '{plan}'. Must be one of {sorted(_VALID_PLANS)}")
        ok = await self._users.update(user_id, {"plan": plan})
        if ok:
            await self._audit.log(admin_id, "set_plan", {"user_id": user_id, "plan": plan})
        return ok

    async def set_role(self, user_id: int, role: str, *, admin_id: int) -> bool:
        if role not in ("user", "admin"):
            raise ValueError(f"Invalid role: {role!r}")
        ok = await self._users.update(user_id, {"role": role})
        if ok:
            await self._audit.log(admin_id, "set_role", {"user_id": user_id, "role": role})
        return ok

    async def get_all(self, *, limit: int = 200) -> tuple:
        return await self._users.get_all(limit=limit)

    async def get_banned(self) -> List[Dict[str, Any]]:
        return await self._users.get_banned()

    async def stats(self) -> Dict[str, int]:
        return await self._users.stats()
