"""
bot/services/_queue_worker.py — Background task queue processor.
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Set

from bot.database import get_db, update_task

logger = logging.getLogger("filebot.services.queue")


class QueueWorker:
    _instance: 'QueueWorker' = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(QueueWorker, cls).__new__(cls)
        return cls._instance

    def __init__(self, bot=None):
        if hasattr(self, '_initialized'): return
        self.bot = bot
        self.running = False
        from bot.database import get_config_sync
        config = get_config_sync() or {}
        self.limit = int(config.get("parallel_global_limit") or os.getenv("PARALLEL_LIMIT", 5))
        self.semaphore = asyncio.Semaphore(self.limit)
        self.sleep_interval = 2
        self.active_tasks: Set[asyncio.Task] = set()
        self._initialized = True

    @classmethod
    def get_instance(cls) -> 'QueueWorker':
        if not cls._instance:
            raise RuntimeError("QueueWorker NOT initialized! Call QueueWorker(bot) first.")
        return cls._instance

    def update_limit(self, new_limit: int):
        """Update the global parallel limit at runtime."""
        self.limit = new_limit
        self.semaphore = asyncio.Semaphore(new_limit)
        logger.info(f"⚡ QueueWorker limit updated to: {new_limit}")

    async def start(self):
        """Start the worker loop."""
        self.running = True
        logger.info(f"🚀 Queue Worker Started. Limit: {self.limit}")
        await self.recover_stale_tasks()
        asyncio.create_task(self._loop())

    async def stop(self):
        """Stop the worker gracefully."""
        self.running = False
        logger.info("🛑 Queue Worker Stopping... Waiting for active tasks.")
        if self.active_tasks:
            try:
                await asyncio.wait(self.active_tasks, timeout=30)
            except asyncio.TimeoutError:
                logger.warning("⚠️ Some tasks did not finish in time.")
        logger.info("✅ Queue Worker Stopped.")

    async def recover_stale_tasks(self):
        """Reset tasks that were 'processing' or 'waiting_user_input' when the bot crashed."""
        try:
            db = get_db()
            result = await db.tasks.update_many(
                {"status": {"$in": ["processing", "waiting_user_input"]}},
                {"$set": {"status": "queued", "recovered": True}},
            )
            if result.modified_count > 0:
                logger.warning(f"🔄 Recovered {result.modified_count} stale/waiting tasks.")
        except Exception as e:
            logger.error(f"❌ Failed to recover stale tasks: {e}")

    async def _loop(self):
        while self.running:
            try:
                db = get_db()
                
                # 1. Cleanup expired waiting tasks (120s timeout)
                now = datetime.utcnow()
                expired_threshold = 120
                expired_tasks = await db.tasks.find({
                    "status": "waiting_user_input",
                    "wait_started_at": {"$lt": datetime.fromtimestamp(time.time() - expired_threshold)}
                }).to_list(length=None)
                
                for et in expired_tasks:
                    tid = et.get("task_id")
                    uid = et.get("user_id")
                    await update_task(tid, {"status": "expired", "error": "User response timeout (120s)"})
                    try:
                        await self.bot.send_message(
                            chat_id=uid,
                            text="⏰ **Wait Window Expired**\n\nYour turn in the queue has expired because you didn't click 'Start' within 120 seconds. Please resend the file if you wish to try again.",
                            parse_mode="Markdown"
                        )
                    except: pass
                    logger.info(f"⏰ Task {tid} expired due to timeout.")

                # 2. If queue is full (parallel limit reached), ONLY pull Pro tasks (Priority > 0)
                if self.semaphore.locked():
                    pro_task = await db.tasks.find_one_and_update(
                        {"status": "queued", "priority": {"$gt": 0}},
                        {"$set": {"status": "processing", "started_at": datetime.utcnow()}},
                        sort=[("priority", -1), ("created_at", 1)],
                    )
                    if pro_task:
                        t = asyncio.create_task(self._process_task_safely(pro_task, bypass_semaphore=True))
                        self.active_tasks.add(t)
                        t.add_done_callback(self.active_tasks.discard)
                        continue

                    await asyncio.sleep(1)
                    continue

                # 3. Normal pull (respects priority)
                task = await db.tasks.find({"status": "queued"}).sort([("priority", -1), ("created_at", 1)]).to_list(length=1)
                if not task:
                    await asyncio.sleep(self.sleep_interval)
                    continue
                
                task = task[0]
                user_id = task.get("user_id")
                from bot.database import get_user, get_config, get_active_task_count
                user = await get_user(user_id)
                plan_name = user.get("plan", "free").lower()
                is_pro = plan_name != "free"
                
                # Check per-user parallel limit from plan
                plans_config = await get_config("plans") or {}
                plan_limit = plans_config.get(plan_name, {}).get("parallel", 1)
                user_active_count = await get_active_task_count(user_id)
                
                if user_active_count >= plan_limit:
                    # User already at their limit, skip this task for now
                    # (In a real system we might want to prioritize others, 
                    # but for now we'll just sleep a bit to avoid CPU spin)
                    await asyncio.sleep(1)
                    continue

                if is_pro:
                    # Pro: Immediately process
                    task = await db.tasks.find_one_and_update(
                        {"task_id": task["task_id"], "status": "queued"},
                        {"$set": {"status": "processing", "started_at": datetime.utcnow()}}
                    )
                    if task:
                        t = asyncio.create_task(self._process_task_safely(task, bypass_semaphore=False))
                        self.active_tasks.add(t)
                        t.add_done_callback(self.active_tasks.discard)
                else:
                    # Free User: Enter wait state
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                    await db.tasks.update_one(
                        {"task_id": task["task_id"]},
                        {"$set": {"status": "waiting_user_input", "wait_started_at": datetime.utcnow()}}
                    )
                    try:
                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton("🚀 Start My Task", callback_data=f"queue_start_{task['task_id']}")
                        ]])
                        await self.bot.send_message(
                            chat_id=user_id,
                            text=(
                                "🎉 **It's Your Turn!**\n\n"
                                "Your file is ready to be processed.\n"
                                "Please click the button below within **120 seconds** to start.\n\n"
                                "If you don't respond, your turn will be skipped."
                            ),
                            reply_markup=keyboard,
                            parse_mode="Markdown"
                        )
                        logger.info(f"📢 User {user_id} notified for task {task['task_id']}. Waiting 120s.")
                    except Exception as e:
                        logger.error(f"Failed to notify free user {user_id}: {e}")
                        # If notify fails, just mark it back to queued or fail it?
                        # Let's mark it as queued so it doesn't get stuck in waiting
                        await db.tasks.update_one({"task_id": task["task_id"]}, {"$set": {"status": "queued"}})

                if not hasattr(self, "_last_cleanup"):
                    self._last_cleanup = 0
                if time.time() - self._last_cleanup > 3600:
                    from bot.services._download import cleanup_old_downloads
                    asyncio.create_task(cleanup_old_downloads(older_than_hours=6))
                    self._last_cleanup = time.time()

            except Exception as e:
                logger.error(f"Queue Loop Error: {e}")
                await asyncio.sleep(5)

    async def _process_task_safely(self, task: Any, bypass_semaphore: bool = False):
        """Wrapper to run task with semaphore and error handling."""
        task_id = task.get("task_id")
        user_id = task.get("user_id")
        retry_count = task.get("retry_count", 0)
        MAX_RETRIES = 3

        if not bypass_semaphore:
            await self.semaphore.acquire()
            
        try:
            try:
                logger.info(f"▶️ Starting Task {task_id} (Attempt {retry_count + 1}) [Pro Bypass: {bypass_semaphore}]")

                try:
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=f"🏗️ **Processing Started!**\n\nTask ID: `{task_id}`\nYour file is now being processed.",
                        parse_mode="Markdown",
                    )
                except Exception as msg_err:
                    logger.warning(f"Could not notify user {user_id}: {msg_err}")

                task_type = task.get("type") or task.get("task_type")
                if task_type in ["file", "upload"]:
                    if "session" in task:
                        from bot.handlers.files import WizardHandler
                        await WizardHandler.process_session_background(self.bot, user_id, task["session"])
                    else:
                        from bot.handlers import execute_processing_flow_by_task
                        await execute_processing_flow_by_task(self.bot, task)

                await update_task(task_id, {"status": "completed", "completed_at": datetime.utcnow()})

            except Exception as e:
                logger.error(f"Task {task_id} Failed: {e}", exc_info=True)

                if retry_count < MAX_RETRIES:
                    logger.info(f"🔄 Retrying Task {task_id} (Attempt {retry_count + 1}/{MAX_RETRIES})")
                    await update_task(task_id, {
                        "status": "queued",
                        "retry_count": retry_count + 1,
                        "last_error": str(e),
                    })
                else:
                    await update_task(task_id, {"status": "failed", "error": str(e)})
                    try:
                        await self.bot.send_message(
                            chat_id=user_id,
                            text=f"❌ **Task Failed** (Max Retries Exceeded)\n\nError: `{str(e)[:100]}`",
                        )
                    except Exception:
                        pass
        finally:
            if not bypass_semaphore:
                self.semaphore.release()


# ─────────────────────────────────────────────────────────────────────────────
# run_broadcast_worker — standalone coroutine (used by web/routes/admin_config)
# ─────────────────────────────────────────────────────────────────────────────

async def run_broadcast_worker(broadcast_id: str) -> None:
    """
    Execute a broadcast draft identified by `broadcast_id`.

    Fetches the draft from MongoDB, iterates over all active (non-banned) users
    and sends the message. Updates the broadcast status to 'completed' on finish.

    Called from the web route: asyncio.create_task(run_broadcast_worker(broadcast_id))
    """
    import asyncio
    from datetime import datetime as _dt
    from bot.database import get_db

    logger.info(f"📢 Broadcast worker started: {broadcast_id}")
    db = get_db()

    try:
        draft = await db.broadcasts.find_one({"broadcast_id": broadcast_id})
        if not draft:
            logger.error(f"❌ Broadcast draft not found: {broadcast_id}")
            return

        message_text = draft.get("message", "")
        target = draft.get("target", "all")

        # Fetch target users
        query: dict = {"banned": {"$ne": True}}
        if target != "all":
            query["plan"] = target

        users = await db.users.find(query, {"telegram_id": 1}).to_list(length=None)

        # Resolve the bot instance from the running application
        try:
            from main import bot_application
            bot = bot_application.bot if bot_application else None
        except Exception:
            bot = None

        if not bot:
            logger.error("❌ run_broadcast_worker: bot not available yet")
            await db.broadcasts.update_one(
                {"broadcast_id": broadcast_id},
                {"$set": {"status": "failed", "error": "Bot not available", "finished_at": _dt.utcnow()}},
            )
            return

        sent = 0
        failed = 0

        for user in users:
            uid = user.get("telegram_id")
            if not uid:
                continue
            try:
                await bot.send_message(
                    chat_id=uid,
                    text=message_text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                sent += 1
            except Exception as e:
                logger.debug(f"Broadcast send failed for {uid}: {e}")
                failed += 1
            # Rate limit: 20 msgs/sec (Telegram global limit)
            await asyncio.sleep(0.05)

        await db.broadcasts.update_one(
            {"broadcast_id": broadcast_id},
            {"$set": {
                "status": "completed",
                "sent": sent,
                "failed": failed,
                "finished_at": _dt.utcnow(),
            }},
        )
        logger.info(f"✅ Broadcast {broadcast_id} complete: {sent} sent, {failed} failed")

    except Exception as e:
        logger.error(f"❌ Broadcast worker error [{broadcast_id}]: {e}", exc_info=True)
        try:
            from datetime import datetime as _dt2
            await db.broadcasts.update_one(
                {"broadcast_id": broadcast_id},
                {"$set": {"status": "failed", "error": str(e)[:200], "finished_at": _dt2.utcnow()}},
            )
        except Exception:
            pass

