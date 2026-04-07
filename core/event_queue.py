"""
event_queue.py — Event-driven queue using MongoDB Change Streams

Instead of polling the database every X seconds, we use MongoDB's
change streams to react immediately when tasks are created or updated.

This reduces:
- Database load (no constant queries)
- Latency (instant reaction vs polling delay)
- Resource usage (idle when no activity)

Requirements:
- MongoDB 3.6+ with replica set or sharded cluster
- For standalone MongoDB: Must enable replica set mode

Fallback: If change streams are not supported, falls back to polling.
"""

import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional

from database import get_db

logger = logging.getLogger("filebot.event_queue")


class EventQueue:
    """
    Event-driven task queue using MongoDB Change Streams.

    Listens for changes to the 'tasks' collection and triggers
    callbacks when specific events occur.

    Supported Events:
    - INSERT: New task created
    - UPDATE: Task status changed
    - REPLACE: Task replaced
    - DELETE: Task deleted

    Usage:
        eq = EventQueue()

        @eq.on_insert("tasks")
        async def handle_new_task(change):
            task = change.get("fullDocument")
            await process_task(task)

        await eq.start()
    """

    _instance: Optional["EventQueue"] = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized") and self._initialized:
            return

        self._handlers: dict[str, list[Callable]] = {
            "insert": [],
            "update": [],
            "replace": [],
            "delete": [],
        }
        self._running = False
        self._stream_task: Optional[asyncio.Task] = None
        self._change_stream = None
        self._fallback_mode = False
        self._initialized = True
        self._fallback_interval = 1.0  # seconds between fallback polls

    @classmethod
    def get_instance(cls) -> "EventQueue":
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

    def on_insert(self, collection: str = "tasks"):
        """Decorator to handle insert events."""

        def decorator(func: Callable):
            key = f"insert:{collection}"
            if key not in self._handlers:
                self._handlers[key] = []
            self._handlers[key].append(func)
            return func

        return decorator

    def on_update(self, collection: str = "tasks"):
        """Decorator to handle update events."""

        def decorator(func: Callable):
            key = f"update:{collection}"
            if key not in self._handlers:
                self._handlers[key] = []
            self._handlers[key].append(func)
            return func

        return decorator

    def _get_handlers(self, operation_type: str, collection: str) -> list[Callable]:
        """Get handlers for a specific operation and collection."""
        specific_key = f"{operation_type}:{collection}"
        generic_key = operation_type
        handlers = []
        handlers.extend(self._handlers.get(specific_key, []))
        handlers.extend(self._handlers.get(generic_key, []))
        return handlers

    async def _setup_change_stream(self):
        """Set up the MongoDB change stream."""
        try:
            db = get_db()

            pipeline = [
                {
                    "$match": {
                        "operationType": {
                            "$in": ["insert", "update", "replace", "delete"]
                        },
                        "ns.coll": "tasks",
                    }
                }
            ]

            self._change_stream = db.tasks.watch(pipeline, full_document="updateLookup")
            logger.info("✅ MongoDB Change Stream initialized for 'tasks' collection")
            return True

        except Exception as e:
            error_msg = str(e).lower()

            if "replica set" in error_msg or "not supported" in error_msg:
                logger.warning(
                    "⚠️ Change streams require MongoDB replica set. "
                    "Falling back to polling mode."
                )
            elif "not authorized" in error_msg:
                logger.warning(
                    "⚠️ Insufficient permissions for change streams. "
                    "Falling back to polling mode."
                )
            else:
                logger.warning(f"⚠️ Could not initialize change stream: {e}")

            self._fallback_mode = True
            return False

    async def _poll_fallback(self):
        """Fallback polling mechanism when change streams are unavailable."""
        logger.info(
            f"🔄 Event Queue running in FALLBACK mode (poll every {self._fallback_interval}s)"
        )

        last_check = datetime.utcnow()

        while self._running:
            try:
                db = get_db()

                new_tasks = await db.tasks.find(
                    {"status": "queued", "created_at": {"$gt": last_check}}
                ).to_list(length=100)

                for task in new_tasks:
                    handlers = self._get_handlers("insert", "tasks")
                    for handler in handlers:
                        try:
                            await handler(
                                {"operationType": "insert", "fullDocument": task}
                            )
                        except Exception as e:
                            logger.error(f"Handler error in fallback poll: {e}")

                last_check = datetime.utcnow()
                await asyncio.sleep(self._fallback_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fallback poll error: {e}")
                await asyncio.sleep(5)

    async def _stream_loop(self):
        """Main loop processing change stream events."""

        async def process_changes():
            async with self._change_stream as stream:
                async for change in stream:
                    if not self._running:
                        break

                    operation = change.get("operationType")
                    handlers = self._get_handlers(operation, "tasks")

                    for handler in handlers:
                        try:
                            await handler(change)
                        except Exception as e:
                            logger.error(f"Handler error for {operation}: {e}")

        try:
            await process_changes()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Change stream error: {e}")
            if not self._fallback_mode:
                self._fallback_mode = True
                logger.info("Switching to fallback polling mode")
                await self._poll_fallback()

    async def start(self):
        """Start the event queue."""
        if self._running:
            return

        self._running = True

        if await self._setup_change_stream():
            self._stream_task = asyncio.create_task(self._stream_loop())
        else:
            self._stream_task = asyncio.create_task(self._poll_fallback())

        logger.info("✅ EventQueue started")

    async def stop(self):
        """Stop the event queue."""
        self._running = False

        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass

        if self._change_stream:
            try:
                self._change_stream.close()
            except Exception:
                pass

        logger.info("🛑 EventQueue stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_fallback_mode(self) -> bool:
        return self._fallback_mode


# Singleton accessor
def get_event_queue() -> EventQueue:
    return EventQueue.get_instance()


# Convenience decorator functions
def on_task_insert(func: Callable):
    """Decorator for task insert events."""
    eq = get_event_queue()
    return eq.on_insert("tasks")(func)


def on_task_update(func: Callable):
    """Decorator for task update events."""
    eq = get_event_queue()
    return eq.on_update("tasks")(func)
