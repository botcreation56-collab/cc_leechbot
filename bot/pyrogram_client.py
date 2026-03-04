import asyncio
import logging
import os
from pyrogram import Client
from pyrogram.types import Message
from typing import Optional, Any
from pathlib import Path
from config.settings import get_settings
import time

logger = logging.getLogger(__name__)

# Global instances
app: Optional[Client] = None
user_app: Optional[Client] = None

def get_pyrogram_apps():
    """Returns the (bot_app, user_app) instances"""
    return app, user_app

async def init_pyrogram():
    """Starts the pyrogram clients"""
    global app, user_app
    settings = get_settings()

    api_id = settings.API_ID
    api_hash = settings.API_HASH
    bot_token = settings.BOT_TOKEN.get_secret_value()
    user_session = settings.USERBOT_SESSION

    if not api_id or not api_hash:
        logger.warning("⚠️ API_ID and API_HASH not found. Pyrogram client disabled.")
        return False

    try:
        # Bot token client (useful for generic API functions > 50MB, up to 2GB via local server, or stream extraction)
        app = Client(
            "filebot_pyro",
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
        )
        await app.start()
        logger.info("✅ Pyrogram Bot Client Started")

        # Premium Userbot client (Required for uploading > 2GB)
        if user_session:
            user_app = Client(
                "filebot_user",
                api_id=api_id,
                api_hash=api_hash,
                session_string=user_session
            )
            await user_app.start()
            logger.info("✅ Pyrogram Userbot Client Started (Premium Uploads Enabled)")
        else:
            logger.info("ℹ️ No USERBOT_SESSION found. Files > 2GB will be bounced to Rclone.")

        return True
    except Exception as e:
        logger.error(f"❌ Failed to start Pyrogram: {e}")
        return False

async def stop_pyrogram():
    global app, user_app
    if app:
        await app.stop()
    if user_app:
        await user_app.stop()
    logger.info("🛑 Pyrogram clients stopped")

async def upload_file_pyrogram(
    file_path: str,
    chat_id: int,
    caption: str,
    ptb_bot: Any,
    user_id: int,
    task_id: str,
    thumb_path: str = None
) -> Optional[Message]:
    """Upload large files using Pyrogram Userbot"""
    global user_app, app
    path = Path(file_path)
    
    if not path.exists():
        logger.error(f"File not found: {file_path}")
        return None

    file_size = path.stat().st_size
    client_to_use = None

    # Determine client based on size and availability
    if file_size > 2 * 1024 * 1024 * 1024:
        if user_app:
            client_to_use = user_app
        else:
            logger.error("File > 2GB but no user_app session loaded.")
            return None
    else:
        # Standard Bot Client or Userbot
        client_to_use = user_app if user_app else app

    if not client_to_use:
         logger.error("No suitable Pyrogram client available for upload.")
         return None

    last_update_time = [time.time()]

    async def pyrogram_progress(current, total):
        from bot.handlers.user import send_progress_message
        now = time.time()
        
        # Throttle updates to exactly 5 seconds OR 100% completion
        if now - last_update_time[0] >= 5.0 or current >= total:
            last_update_time[0] = now
            progress_pct = int((current / total) * 100)
            asyncio.create_task(
                send_progress_message(
                    bot=ptb_bot,
                    user_id=user_id,
                    task_id=task_id,
                    filesize=total,
                    stage="📤 **Uploading via Telegram...**",
                    progress=progress_pct
                )
            )

    try:
        logger.info(f"Uploading {path.name} ({file_size/1024/1024:.2f} MB) via Pyrogram")
        
        # Decide if video or document (video based on extension)
        ext = path.suffix.lower()
        if ext in [".mp4", ".mkv", ".avi", ".mov"]:
            msg = await client_to_use.send_video(
                chat_id=chat_id,
                video=file_path,
                caption=caption,
                thumb=thumb_path if thumb_path else None,
                progress=pyrogram_progress
            )
        else:
            msg = await client_to_use.send_document(
                chat_id=chat_id,
                document=file_path,
                caption=caption,
                thumb=thumb_path if thumb_path else None,
                progress=pyrogram_progress
            )
            
        logger.info(f"✅ Pyrogram upload successful: {path.name}")
        return msg

    except Exception as e:
        logger.error(f"❌ Pyrogram upload failed: {e}", exc_info=True)
        return None
