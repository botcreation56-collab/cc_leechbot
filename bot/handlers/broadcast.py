import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import TelegramError
from bot.middleware import admin_only
from database import (
    get_db,
    get_user,
    get_all_users,
    ban_user,
    unban_user,
    update_user,
    update_config,
    get_config,
    add_action,
    get_chatbox_messages,
    add_chatbox_message,
    get_banned_users,
    get_user_files,
    add_rclone_config,
    get_rclone_configs,
    pick_rclone_config_for_plan,
    cleanup_old_cloud_files,
    log_admin_action,
    create_broadcast_draft,
    get_user_cloud_files,
    get_user_tasks,
    get_admin_stats,
    create_task,
    update_task,
)
from bot.utils import log_info, log_error, log_user_update, validate_url
from bot.services import create_or_update_storage_message, FFmpegService
from config.constants import ERROR_MESSAGES, BROADCAST_RATE_LIMIT
from config.settings import get_settings, get_admin_ids
settings = get_settings()
ADMIN_IDS = get_admin_ids()
HELP_TEXT = """
🤖 **Welcome to File Processor Bot!**

I can help you process files (FFmpeg) and upload them to cloud storage.

**Available Commands:**
/start - Start the bot
/help - Show this message
/stats - View your usage statistics
/myfiles - Manage your uploaded files
/settings - Customize your behavior
/cancel - Cancel current operation

**Supported Files:**
Video (mp4, mkv, avi, etc.)
Audio (mp3, aac, flac, etc.)
Large files up to 10GB (Pro)

**How to use:**
1. Send me a direct file or a direct download URL.
2. Select your processing options.
3. Wait for the magic to happen!
"""
WELCOME_MSG = "👋 Welcome to File Processor Bot!"
STATS_TEXT = "📊 **Your Statistics**"
PROMPT_TEXT = "📝 **Send the new value below:**"
MODE_TEXT = "🎬 **Select Output Mode**"
prompt = PROMPT_TEXT
stats_text = STATS_TEXT
mode_text = MODE_TEXT
prompt_text = PROMPT_TEXT
logger = logging.getLogger(__name__)
RCLONE_SUPPORTED_SERVICES = ["gdrive", "onedrive", "dropbox", "mega", "s3", "box"]
logger.info("✅ Admin panel handlers loaded")
logger.info("✅ User commands module loaded successfully")
logger.info("User commands module loaded successfully")
logger.info("✅ User settings module loaded with all handlers")
WIZARD_TIMEOUT = 1200

async def handle_broadcast_compose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Compose broadcast message"""
    try:
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel_input")]]
        await update.callback_query.message.edit_text(
            "📝 **Compose Broadcast**\n\n"
            "Send the message to broadcast to all users.\n\n"
            "Supports _Markdown_.\n\n"
            "👇 **Send text below** or click Cancel.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "broadcast_message"
        logger.info(f"✅ Broadcast compose started")
    except Exception as e:
        logger.error(f"❌ Error in broadcast compose: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel pending broadcasts (triggered by broadcast_cancel_input callback)"""
    try:
        config = await get_config() or {}
        broadcasts = config.get("broadcasts", [])
        active = [b for b in broadcasts if b.get("status") == "pending"]

        if not active:
            await update.callback_query.answer("❌ No active broadcasts to cancel", show_alert=True)
            return

        for b in active:
            b["status"] = "cancelled"
        config["broadcasts"] = broadcasts
        await update_config(config, admin_id=update.effective_user.id)

        await update.callback_query.message.edit_text(
            f"✅ **Broadcast Cancelled**\n\n{len(active)} pending broadcasts have been cancelled.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_broadcast")]])
        )
        logger.info(f"✅ {len(active)} broadcasts cancelled by {update.effective_user.id}")
    except Exception as e:
        logger.error(f"❌ Error cancelling broadcasts: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def broadcast_edit_message_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Edit the broadcast message before sending"""
    try:
        callback = update.callback_query
        await callback.answer()
        context.user_data["broadcast_awaiting"] = "message"
        await callback.message.edit_text(
            "📝 **Edit Broadcast Message**\n\nPlease send the new message text.",
            parse_mode="Markdown"
        )
        logger.info("📝 Broadcast message edit initiated")
    except Exception as e:
        logger.error(f"❌ Broadcast edit error: {e}")
        try:
            await update.callback_query.answer("Error editing message", show_alert=True)
        except:
            pass

async def broadcast_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel broadcast operation and clean up"""
    try:
        callback = update.callback_query
        await callback.answer()
        admin_id = callback.from_user.id

        context.user_data.pop("broadcast_message", None)
        context.user_data.pop("broadcast_target", None)
        context.user_data.pop("broadcast_action", None)
        context.user_data.pop("awaiting", None)

        cancel_msg = "✅ **Broadcast Cancelled**\n\nAll pending operations have been cleared."

        await callback.message.edit_text(
            cancel_msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_broadcast")]])
        )

        await log_admin_action(admin_id, "broadcast_cancelled", {
            "timestamp": datetime.utcnow().isoformat()
        })
        logger.info(f"✅ Broadcast cancelled by admin {admin_id}")

    except Exception as e:
        logger.error(f"❌ Broadcast cancel error: {e}", exc_info=True)
        try:
            await update.callback_query.answer("Error cancelling broadcast", show_alert=True)
        except:
            pass

async def broadcast_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Route broadcast-related messages"""
    try:
        if update.callback_query:
            callback_data = update.callback_query.data
            if callback_data == "broadcast_cancel_input":
                await handle_broadcast_cancel(update, context)
            elif callback_data == "broadcast_cancel":
                await broadcast_cancel_callback(update, context)
            elif callback_data == "broadcast_edit_message":
                await broadcast_edit_message_callback(update, context)
            return True

        if update.message and update.message.text:
            awaiting = context.user_data.get("awaiting")
            if awaiting == "broadcast_message":
                await handle_broadcast_message_input(update, context)
                return True

        return False
    except Exception as e:
        logger.error(f"❌ Broadcast router error: {e}", exc_info=True)
        return False

async def handle_broadcast_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for broadcast message"""
    try:
        user_id = update.effective_user.id
        text = update.message.text
        # Save to draft
        await create_broadcast_draft(user_id, text)
        await update.message.reply_text("✅ Broadcast draft saved. Use /admin → Broadcast to send.")
        context.user_data.pop("awaiting", None)
    except Exception as e:
        logger.error(f"❌ Error in handle_broadcast_message_input: {e}")
        await update.message.reply_text("❌ Could not save broadcast draft. Please try again.")

async def handle_broadcast_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for broadcast message"""
    try:
        user_id = update.effective_user.id
        text = update.message.text
        # Save to draft
        await create_broadcast_draft(user_id, text)
        await update.message.reply_text("✅ Broadcast draft saved. Use /admin → Broadcast to send.")
        context.user_data.pop("awaiting", None)
    except Exception as e:
        logger.error(f"❌ Error in handle_broadcast_message_input: {e}")
        await update.message.reply_text("❌ Could not save broadcast draft. Please try again.")