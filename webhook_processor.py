"""
webhook_processor.py — High-performance webhook processing with:
  - Async fire-and-forget processing (returns 200 immediately)
  - Per-user tier rate limiting (Free: 5/min, Pro: 30/min)
  - Batch update handling
  - Connection pooling reuse
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
from telegram import Update

logger = logging.getLogger("filebot.webhook")

FREE_TIER_RATE = 5
FREE_TIER_WINDOW = 60
PRO_TIER_RATE = 30
PRO_TIER_WINDOW = 60


@dataclass
class RateLimitEntry:
    count: int = 0
    window_start: float = field(default_factory=time.time)
    blocked_until: float = 0.0


class WebhookProcessor:
    _instance: Optional["WebhookProcessor"] = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized") and self._initialized:
            return
        self._rate_limits: dict[int, RateLimitEntry] = defaultdict(RateLimitEntry)
        self._update_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._worker_task: Optional[asyncio.Task] = None
        self._initialized = True
        self._running = False

    @classmethod
    def get_instance(cls) -> "WebhookProcessor":
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

    def _get_user_tier(self, user_id: int) -> str:
        try:
            from database import get_user

            user = asyncio.create_task(get_user(user_id))
        except Exception:
            return "free"
        return "pro" if user and user.get("plan") != "free" else "free"

    def _check_rate_limit(self, user_id: int, tier: str) -> tuple[bool, float]:
        entry = self._rate_limits[user_id]
        now = time.time()

        if now < entry.blocked_until:
            return False, entry.blocked_until - now

        rate = PRO_TIER_RATE if tier == "pro" else FREE_TIER_RATE
        window = PRO_TIER_WINDOW if tier == "pro" else FREE_TIER_WINDOW

        if now - entry.window_start >= window:
            entry.count = 0
            entry.window_start = now

        if entry.count >= rate:
            entry.blocked_until = entry.window_start + window
            return False, window - (now - entry.window_start)

        entry.count += 1
        return True, 0.0

    async def enqueue_update(self, update: Update) -> bool:
        if self._update_queue.full():
            logger.warning("Webhook queue full, dropping update")
            return False

        await self._update_queue.put(update)
        return True

    async def start_worker(self, bot_application):
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._process_loop(bot_application))
        logger.info("✅ Webhook processor worker started")

    async def stop_worker(self):
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def _process_loop(self, bot_application):
        batch: list[Update] = []
        batch_timeout = 0.05

        while self._running:
            try:
                try:
                    update = await asyncio.wait_for(
                        self._update_queue.get(), timeout=batch_timeout
                    )
                    batch.append(update)

                    while len(batch) < 10:
                        try:
                            update = await asyncio.wait_for(
                                self._update_queue.get(), timeout=0.01
                            )
                            batch.append(update)
                        except asyncio.TimeoutError:
                            break

                    for upd in batch:
                        asyncio.create_task(self._process_single(upd, bot_application))
                    batch.clear()

                except asyncio.TimeoutError:
                    if batch:
                        for upd in batch:
                            asyncio.create_task(
                                self._process_single(upd, bot_application)
                            )
                        batch.clear()

            except Exception as e:
                logger.error(f"Webhook process loop error: {e}")
                await asyncio.sleep(0.1)

    async def _process_single(self, update: Update, bot_application):
        try:
            user_id = update.effective_user.id if update.effective_user else 0
            tier = "free"

            if user_id:
                try:
                    from database import get_user

                    user = await get_user(user_id)
                    tier = "pro" if user and user.get("plan") != "free" else "free"
                except Exception:
                    pass

                allowed, wait_time = self._check_rate_limit(user_id, tier)
                if not allowed:
                    logger.warning(
                        f"Rate limited user {user_id} (tier={tier}), wait={wait_time:.1f}s"
                    )
                    return

            await bot_application.process_update(update)

        except Exception as e:
            logger.error(f"Error processing update: {e}")


_webhook_processor: Optional[WebhookProcessor] = None


def get_webhook_processor() -> WebhookProcessor:
    global _webhook_processor
    if _webhook_processor is None:
        _webhook_processor = WebhookProcessor()
    return _webhook_processor
