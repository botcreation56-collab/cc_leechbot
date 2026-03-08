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
    """
    Background worker that accepts 'queued' tasks from MongoDB
    and processes them within the PARALLEL_LIMIT.
    """

    def __init__(self, bot):
        self.bot = bot
        self.running = False
        self.limit = int(os.getenv("PARALLEL_LIMIT", 5))
        self.semaphore = asyncio.Semaphore(self.limit)
        self.sleep_interval = 2
        self.active_tasks: Set[asyncio.Task] = set()

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
        """Reset tasks that were 'processing' when the bot crashed."""
        try:
            db = get_db()
            result = await db.tasks.update_many(
                {"status": "processing"},
                {"$set": {"status": "queued", "recovered": True}},
            )
            if result.modified_count > 0:
                logger.warning(f"🔄 Recovered {result.modified_count} stale tasks.")
        except Exception as e:
            logger.error(f"❌ Failed to recover stale tasks: {e}")

    async def _loop(self):
        while self.running:
            try:
                db = get_db()
                
                # If queue is full (parallel limit reached), ONLY pull Pro tasks (Priority > 0)
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

                # Normal pull: respects priority (Pro goes first)
                task = await db.tasks.find_one_and_update(
                    {"status": "queued"},
                    {"$set": {"status": "processing", "started_at": datetime.utcnow()}},
                    sort=[("priority", -1), ("created_at", 1)],
                )

                if task:
                    t = asyncio.create_task(self._process_task_safely(task, bypass_semaphore=False))
                    self.active_tasks.add(t)
                    t.add_done_callback(self.active_tasks.discard)
                else:
                    await asyncio.sleep(self.sleep_interval)

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

