"""
MongoDB connection manager - shared by bot & web
Handles connection pooling and initialization
"""

import logging
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from contextlib import asynccontextmanager

from config.settings import get_settings

logger = logging.getLogger(__name__)

# Global database instance
_db: AsyncIOMotorDatabase = None
_client: AsyncIOMotorClient = None


async def connect_db() -> AsyncIOMotorDatabase:
    global _db, _client
    try:
        settings = get_settings()
        _client = AsyncIOMotorClient(settings.MONGODB_URI)
        _db = _client[settings.MONGODB_DB]
        await _db.command("ping")
        logger.info(f"✅ Connected to MongoDB: {settings.MONGODB_DB}")
        return _db
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        raise RuntimeError("DB connection failed")  # Raise to halt bot if DB fails


async def disconnect_db():
    """Disconnect from MongoDB"""
    global _db, _client
    
    try:
        if _client:
            _client.close()
        logger.info("✅ Disconnected from MongoDB")
    except Exception as e:
        logger.error(f"❌ Disconnect error: {e}")


def get_db() -> AsyncIOMotorDatabase:
    """Get current database instance"""
    if not _db:
        raise RuntimeError("Database not connected. Call connect_db() first.")
    return _db


@asynccontextmanager
async def get_db_context():
    """Context manager for database operations"""
    db = get_db()
    try:
        yield db
    except Exception as e:
        logger.error(f"❌ Database operation error: {e}")
        raise

