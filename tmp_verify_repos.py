import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent))

# Set dummy env vars for config
os.environ["MONGODB_URI"] = "mongodb://localhost:27017"
os.environ["BOT_TOKEN"] = "123:abc"

try:
    from database.connection import get_db
    from database.repositories import TaskRepository, CloudFileRepository, UserRepository
    from config.settings import get_settings
except ImportError as e:
    print(f"Import Error: {e}")
    sys.exit(1)

async def verify():
    # Mock connection setup
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient("mongodb://localhost:27017")
    db = client["filebot_production"]
    
    task_repo = TaskRepository(db)
    cloud_repo = CloudFileRepository(db)
    user_repo = UserRepository(db)
    
    user_id = 999999999
    
    print("--- 1. Testing TaskRepository.create ---")
    try:
        # Test free user
        success, tid = await task_repo.create(user_id, "test_file_free", plan="free", max_concurrent_per_user=100)
        task = await db.tasks.find_one({"task_id": tid})
        print(f"Free Task Priority: {task.get('priority')} (Expected: 0)")
        
        # Test pro user
        success_pro, tid_pro = await task_repo.create(user_id, "test_file_pro", plan="pro", max_concurrent_per_user=100)
        task_pro = await db.tasks.find_one({"task_id": tid_pro})
        print(f"Pro Task Priority: {task_pro.get('priority')} (Expected: 10)")
    except Exception as e:
        print(f"Task Repo Test Failed: {e}")
    
    print("\n--- 2. Testing CloudFileRepository.get_user_storage_stats ---")
    try:
        # Insert dummy cloud file
        await db.cloud_files.insert_one({
            "user_id": user_id,
            "file_id": "verify_file_1",
            "file_size": 1024 * 1024 * 10, # 10MB
            "created_at": datetime.utcnow()
        })
        
        stats = await cloud_repo.get_user_storage_stats(user_id)
        print(f"User Storage Stats: {stats}")
    except Exception as e:
        print(f"Cloud Repo Test Failed: {e}")
    
    # Cleanup
    await db.tasks.delete_many({"user_id": user_id})
    await db.cloud_files.delete_many({"user_id": user_id})
    print("\n✅ Verification script completed.")

if __name__ == "__main__":
    asyncio.run(verify())
