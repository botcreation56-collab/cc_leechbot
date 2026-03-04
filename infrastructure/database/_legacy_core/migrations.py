"""
Database schema migrations - run on startup
Creates collections and indices
"""

import logging
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)



async def run_migrations(db: AsyncIOMotorDatabase):
    """Run all database migrations"""
    logger.info("🔄 Running database migrations...")
    await create_collections(db)
    await create_indices(db)
    
    # Verify setup
    logger.info("✅ Migrations complete")


async def create_collections(db: AsyncIOMotorDatabase):
    """Create necessary collections"""
    collections = [
        "users",
        "tasks",
        "cloud_files",
        "config",
        "one_time_keys",
        "rclone_configs",
        "broadcasts",
        "chatbox",
        "admin_logs",
        "actions",
    ]

    existing = await db.list_collection_names()
    for collection in collections:
        if collection not in existing:
            await db.create_collection(collection)
            logger.info(f"✅ Created collection: {collection}")
        else:
            logger.info(f"⏭️  Collection already exists: {collection}")


async def create_indices(db: AsyncIOMotorDatabase):
    """Create database indices for performance"""
    
    try:
        # Users collection - ✅ FIX: Use telegram_id as unique key
        await db.users.create_index("telegram_id", unique=True)
        await db.users.create_index("plan")
        await db.users.create_index("banned")
        await db.users.create_index("created_at")
        await db.users.create_index("role")
        logger.info("✅ Users indices created")

        # Tasks collection
        await db.tasks.create_index("task_id", unique=True)
        await db.tasks.create_index("user_id")
        await db.tasks.create_index("status")
        await db.tasks.create_index([("user_id", 1), ("status", 1)])
        logger.info("✅ Tasks indices created")

        # Cloud files
        await db.cloud_files.create_index("file_id", unique=True)
        await db.cloud_files.create_index("user_id")
        await db.cloud_files.create_index("expiry_date")
        logger.info("✅ Cloud files indices created")

        # One-time keys
        await db.one_time_keys.create_index("user_id")
        await db.one_time_keys.create_index("expires_at")
        logger.info("✅ One-time keys indices created")

        # Admin logs
        await db.admin_logs.create_index("admin_id")
        await db.admin_logs.create_index("user_id")
        await db.admin_logs.create_index("timestamp")
        logger.info("✅ Admin logs indices created")

        # Rclone configs
        await db.rclone_configs.create_index("service")
        await db.rclone_configs.create_index("plan")
        logger.info("✅ Rclone configs indices created")

        # Config
        await db.config.create_index("type", unique=True)
        logger.info("✅ Config indices created")

        # Broadcasts
        await db.broadcasts.create_index("status")
        await db.broadcasts.create_index("created_at")
        logger.info("✅ Broadcasts indices created")

        # Chatbox
        await db.chatbox.create_index("user_id")
        await db.chatbox.create_index("timestamp")
        logger.info("✅ Chatbox indices created")

        # Actions
        await db.actions.create_index("admin_id")
        await db.actions.create_index("timestamp")
        logger.info("✅ Actions indices created")

        logger.info("✅ All database indices created successfully")

    except Exception as e:
        logger.error(f"❌ Index creation failed: {e}", exc_info=True)
        raise


