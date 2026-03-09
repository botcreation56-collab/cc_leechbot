import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import TelegramError
from bot.middleware import admin_only, rate_limit
from bot.database import (
    get_db,
    get_user,
    get_all_users,
    get_config,
    create_user,
    ban_user,
    unban_user,
    update_user,
    update_config,
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
    get_user_position, # Added
    get_task, # Added
)
from bot.utils import log_info, log_error, log_user_update, validate_url
from bot.services import create_or_update_storage_message, FFmpegService
from bot.handlers.files import handle_url_input
from bot.handlers.cloud import terabox_text_input
from bot.handlers.settings import handle_config_edit_input, ussettings_command
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

def paginate_keyboard(buttons: List, page: int, per_page: int = 4, prefix: str = "admin_page") -> InlineKeyboardMarkup:
    """Create paginated keyboard with 2x2 grid layout"""
    total = len(buttons)
    start = page * per_page
    end = start + per_page
    page_buttons = buttons[start:end]

    grid = []
    for i in range(0, len(page_buttons), 2):
        row = page_buttons[i:i+2]
        grid.append([btn for btn in row])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"{prefix}_{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}_{page+1}"))

    if nav:
        grid.append(nav)

    return InlineKeyboardMarkup(grid)

async def show_plans_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plan management menu"""
    try:
        config = await get_config() or {}
        plans = config.get("plans", {})

        plans_text = "⭐ **Plan Management**\n\n"
        if plans:
            for plan_name, plan_data in plans.items():
                plans_text += f"**{plan_name.upper()}**\n"
                plans_text += f"- Parallel: {plan_data.get('parallel', 1)}\n"
                plans_text += f"- Daily Limit: {plan_data.get('storage_per_day', 5)} GB\n"
                plans_text += f"- Expiry: {plan_data.get('dump_expiry_days', 0)} days\n\n"
        
        up_text = config.get("upgrade_text", "Not Set")
        if up_text and len(up_text) > 50:
            up_text = up_text[:47] + "..."
        
        plans_text += (
            f"⚡ **Universal Parallel**: `{config.get('parallel_global_limit', 5)}`\n"
            f"💎 **Upgrade Text**: `{up_text}`\n"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🆓 Free Plan", callback_data="edit_plan_free"),
                InlineKeyboardButton("💎 Pro Plan", callback_data="edit_plan_premium")
            ],
            [InlineKeyboardButton("📝 Edit Upgrade Text", callback_data="edit_upgrade_text")],
            [InlineKeyboardButton("⚡ Universal Parallel", callback_data="edit_parallel")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
        ])

        await update.callback_query.message.edit_text(
            plans_text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        await log_admin_action(update.effective_user.id, "opened_plans", {})
        logger.info("✅ Plans menu opened")

    except Exception as e:
        logger.error(f"❌ Error in plans menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def ask_channel_forward(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_type: str):
    """Generic: prompt admin to forward a message from the target channel."""
    labels = {
        "log_channel": "📌 Log Channel",
        "dump_channel": "💾 Dump Channel",
        "storage_channel": "🗄️ Storage Channel",
        "force_sub_channel": "📢 Force Subscribe Channel",
    }
    label = labels.get(channel_type, channel_type)
    try:
        await update.callback_query.message.reply_text(
            f"📡 **Set {label}**\n\n"
            f"Forward any message from your **{label}** to this chat.\n"
            f"Or simply **send the Channel ID** (e.g. `-100123...`).\n\n"
            f"The bot must be an admin in that channel.\n\n"
            f"Use /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "channel_forward"
        context.user_data["awaiting_channel_type"] = channel_type
        logger.info(f"✅ Waiting for {channel_type} input from admin {update.effective_user.id}")
    except Exception as e:
        logger.error(f"❌ Error in ask_channel_forward: {e}", exc_info=True)

async def show_shorteners_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show link shorteners menu"""
    try:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Shortener", callback_data="add_shortener")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
        ])
        await update.callback_query.message.edit_text(
            "🔗 **Link Shorteners**\n\nManage link shortener integrations.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in shorteners menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_add_shortener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add new link shortener"""
    try:
        await update.callback_query.message.reply_text(
            "🔗 **Add Link Shortener**\n\n"
            "Step 1: **Send your API key** for the shortener.\n\n"
            "Use /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "add_shortener_api"
        logger.info("✅ Add shortener API prompt shown")
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


@rate_limit
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fallback callback query router for any buttons not caught by
    dedicated CallbackQueryHandlers registered in main.py setup_handlers().
    All functions are now local — no circular imports.
    """
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = update.effective_user.id

    try:
        admin_ids = get_admin_ids()

        # Import needed handlers locally to avoid circularity
        from bot.handlers import (
            handle_admin_list_users, handle_admin_back, handle_admin_users, handle_admin_stats, show_config_menu,
            show_plans_menu, show_shorteners_menu, handle_admin_broadcast, handle_admin_rclone,
            handle_admin_terabox, handle_admin_filesize, handle_admin_logs, show_banned_users,
            handle_admin_chatbox, handle_admin_set_log_channel, handle_admin_set_dump_channel,
            handle_admin_set_storage_channel, handle_admin_set_force_sub_channel, handle_admin_fsub_add,
            handle_admin_fsub_manage, handle_admin_fsub_toggle, handle_admin_fsub_link,
            handle_admin_fsub_remove_confirm, handle_admin_fsub_remove, handle_admin_remove_log,
            handle_admin_remove_dump, handle_admin_remove_storage, handle_view_user,
            handle_unban_from_list, handle_admin_ban_user, handle_admin_find_user,
            handle_admin_unban_user, handle_admin_upgrade_user, handle_edit_start_message,
            handle_edit_watermark, handle_edit_support_contact, handle_edit_help_text,
            handle_edit_site_name, handle_edit_site_description, handle_edit_support_channel,
            handle_edit_parallel_limit, handle_edit_max_filesize, handle_edit_file_expiry,
            handle_edit_tos, handle_edit_upgrade_text,
            handle_edit_plan, handle_add_shortener, handle_edit_force_subs,
            handle_broadcast_compose, handle_broadcast_stats, handle_broadcast_cancel,
            handle_admin_add_rclone, handle_admin_add_rclone_wizard, handle_list_rclone_remotes,
            handle_test_rclone, handle_disable_rclone, handle_admin_rclone,
            handle_terabox_setup_key, handle_terabox_test, handle_terabox_stats,
            handle_terabox_disable, handle_set_max_filesize, handle_cleanup_old_files,
            handle_storage_stats, handle_admin_fsub_req_toggle, handle_us_dest_add,
            handle_us_dest_manage, handle_us_dest_remove_confirm, handle_us_dest_remove_do
        )

        # Admin-only callbacks
        if data.startswith("queue_start_"):
            task_id = data.replace("queue_start_", "")
            db = get_db()
            task = await db.tasks.find_one({"task_id": task_id})
            if not task:
                await query.answer("❌ Task not found.", show_alert=True)
                return
            
            if task.get("status") != "waiting_user_input":
                await query.answer("⚠️ Task is already in progress or has expired.", show_alert=True)
                return
            
            # Set back to queued so worker picks it up
            await db.tasks.update_one(
                {"task_id": task_id},
                {"$set": {"status": "queued", "wait_responded_at": datetime.utcnow()}}
            )
            await query.message.edit_text("✅ **Processing will start now!**\n\nPlease wait a moment...", parse_mode="Markdown")
            await query.answer("🚀 Starting task...")
            logger.info(f"✅ User {user_id} clicked Start for task {task_id}")
            return

        # Admin-only callbacks check
        if data.startswith("admin") or data.startswith("edit_") or data.startswith("broadcast_") \
                or data.startswith("banned_") or data.startswith("listusers_") \
                or data.startswith("unban_user_") or data.startswith("view_user_") \
                or data.startswith("rclone_") or data.startswith("terabox_") \
                or data.startswith("set_max_") or data.startswith("cleanup_") \
                or data.startswith("storage_") or data.startswith("shortener_") \
                or data.startswith("admin_fsub_") or data.startswith("upgrade_user_"):

            if user_id not in admin_ids:
                await query.answer("❌ Unauthorized. Admin only.", show_alert=True)
                return

            # ── Main menu ──
            if data == "admin_back":
                await handle_admin_back(update, context)
            elif data == "admin_users":
                await handle_admin_users(update, context)
            elif data == "admin_stats":
                await handle_admin_stats(update, context)
            elif data == "admin_config":
                await show_config_menu(update, context)
            elif data == "admin_plans":
                await show_plans_menu(update, context)
            elif data == "admin_shorteners":
                await show_shorteners_menu(update, context)
            elif data == "admin_broadcast":
                await handle_admin_broadcast(update, context)
            elif data == "admin_rclone":
                await handle_admin_rclone(update, context)
            elif data == "admin_terabox":
                await handle_admin_terabox(update, context)
            elif data == "admin_filesize":
                await handle_admin_filesize(update, context)
            elif data == "admin_logs":
                await handle_admin_logs(update, context)
            elif data == "admin_bans":
                await show_banned_users(update, context, 0)
            elif data == "admin_chatbox":
                await handle_admin_chatbox(update, context)

            # ── Channel setup ──
            elif data == "admin_set_log_channel":
                await handle_admin_set_log_channel(update, context)
            elif data == "admin_set_dump_channel":
                await handle_admin_set_dump_channel(update, context)
            elif data == "admin_set_storage_channel":
                await handle_admin_set_storage_channel(update, context)
            elif data == "admin_set_force_sub_channel":
                await handle_admin_set_force_sub_channel(update, context)

            # ── Force-sub management ──
            elif data == "admin_fsub_add":
                await handle_admin_fsub_add(update, context)
            elif data.startswith("admin_fsub_manage_"):
                await handle_admin_fsub_manage(update, context)
            elif data.startswith("admin_fsub_toggle_"):
                await handle_admin_fsub_toggle(update, context)
            elif data.startswith("admin_fsub_req_toggle_"):
                from bot.handlers.admin import handle_admin_fsub_req_toggle
                await handle_admin_fsub_req_toggle(update, context)
            elif data.startswith("admin_fsub_link_"):
                await handle_admin_fsub_link(update, context)
            elif data.startswith("admin_fsub_remove_confirm_"):
                await handle_admin_fsub_remove_confirm(update, context)
            elif data.startswith("admin_fsub_remove_"):
                await handle_admin_fsub_remove(update, context)

            # ── Channel removal ──
            elif data == "admin_remove_log":
                await handle_admin_remove_log(update, context)
            elif data == "admin_remove_dump":
                await handle_admin_remove_dump(update, context)
            elif data == "admin_remove_storage":
                await handle_admin_remove_storage(update, context)

            # ── Paginated lists ──
            elif data.startswith("banned_page_"):
                await show_banned_users(update, context)
            elif data == "admin_list_users_0" or data.startswith("listusers_page_"):
                await handle_admin_list_users(update, context)
            elif data.startswith("shortener_page_"):
                await show_shorteners_menu(update, context)

            # ── User-specific actions ──
            elif data.startswith("view_user_"):
                await handle_view_user(update, context)
            elif data.startswith("unban_user_"):
                await handle_unban_from_list(update, context)
            elif data.startswith("admin_ban_user_"):
                context.user_data["ban_user_id"] = int(data.split("_")[-1])
                await handle_admin_ban_user(update, context)
            elif data == "admin_find_user":
                await handle_admin_find_user(update, context)
            elif data == "admin_ban_user":
                await handle_admin_ban_user(update, context)
            elif data == "admin_unban_user":
                await handle_admin_unban_user(update, context)
            elif data == "admin_upgrade_user":
                await handle_admin_upgrade_user(update, context)
            elif data.startswith("upgrade_user_"):
                await handle_admin_upgrade_user(update, context)

            # ── Edit config prompts ──
            elif data == "edit_start_msg":
                await handle_edit_start_message(update, context)
            elif data == "edit_watermark":
                await handle_edit_watermark(update, context)
            elif data == "edit_contact":
                await handle_edit_support_contact(update, context)
            elif data == "edit_help_text":
                await handle_edit_help_text(update, context)
            elif data == "edit_tos":
                await handle_edit_tos(update, context)
            elif data == "edit_upgrade_text":
                await handle_edit_upgrade_text(update, context)
            elif data == "edit_site_name":
                await handle_edit_site_name(update, context)
            elif data == "edit_site_desc":
                await handle_edit_site_description(update, context)
            elif data == "edit_support_channel":
                await handle_edit_support_channel(update, context)
            elif data == "edit_parallel":
                await handle_edit_parallel_limit(update, context)
            elif data == "edit_max_filesize":
                await handle_edit_max_filesize(update, context)
            elif data == "edit_file_expiry":
                await handle_edit_file_expiry(update, context)
            elif data.startswith(("edit_plan_", "edit_price_", "edit_daily_", "edit_expiry_")):
                await handle_edit_plan(update, context)
            elif data == "add_shortener":
                await handle_add_shortener(update, context)
            elif data.startswith("edit_shortener_"):
                await handle_add_shortener(update, context)
            elif data == "edit_force_subs":
                await handle_edit_force_subs(update, context)

            # ── Broadcast ──
            elif data == "broadcast_compose":
                await handle_broadcast_compose(update, context)
            elif data == "broadcast_stats":
                await handle_broadcast_stats(update, context)
            elif data == "broadcast_cancel_input":
                await handle_broadcast_cancel(update, context)
            elif data == "broadcast_pending":
                await handle_admin_broadcast(update, context)

            # ── Rclone ──
            elif data == "admin_add_rclone":
                await handle_admin_add_rclone(update, context)
            elif data == "admin_add_rclone_wizard":
                await handle_admin_add_rclone_wizard(update, context)
            elif data == "list_rclone_remotes":
                await handle_list_rclone_remotes(update, context)
            elif data == "test_rclone":
                await handle_test_rclone(update, context)
            elif data == "disable_rclone":
                await handle_disable_rclone(update, context)
            elif data == "configure_rclone":
                await handle_admin_rclone(update, context)

            # ── Terabox ──
            elif data == "terabox_setup_key":
                await handle_terabox_setup_key(update, context)
            elif data == "terabox_test":
                await handle_terabox_test(update, context)
            elif data == "terabox_stats":
                await handle_terabox_stats(update, context)
            elif data == "terabox_disable":
                await handle_terabox_disable(update, context)

            # ── File size / storage ──
            elif data == "set_max_filesize":
                await handle_set_max_filesize(update, context)
            elif data == "cleanup_old_files":
                await handle_cleanup_old_files(update, context)
            elif data == "storage_stats":
                await handle_storage_stats(update, context)

            # ── Logs ──
            elif data == "view_logs_0" or data.startswith("logs_page_"):
                await handle_admin_logs(update, context)
            elif data in ("filter_logs_user", "view_error_logs", "download_logs", "clear_old_logs"):
                await query.message.reply_text("📋 Feature coming soon.", parse_mode="Markdown")

            else:
                logger.warning(f"⚠️ Unknown admin callback: {data}")

        # Non-admin callbacks
        else:
            if data == "us_destination":
                await handle_us_destination_button(update, context)
            elif data == "us_dest_add":
                await handle_us_dest_add(update, context)
            elif data.startswith("us_dest_manage_"):
                await handle_us_dest_manage(update, context)
            elif data.startswith("us_dest_remove_confirm_"):
                await handle_us_dest_remove_confirm(update, context)
            elif data.startswith("us_dest_remove_do_"):
                await handle_us_dest_remove_do(update, context)
            elif data.startswith("refresh_q_"):
                task_id = data.replace("refresh_q_", "")
                from bot.database import get_task, get_user_position
                task = await get_task(task_id)
                if not task or task.get("status") != "queued":
                    await query.answer("Your task is no longer in the queue. It may be processing now.", show_alert=True)
                    return
                
                pos = await get_user_position(user_id)
                if pos == 0:
                    await query.answer("Your turn is up! Processing will start momentarily.", show_alert=True)
                else:
                    await query.answer(f"Your current queue position is {pos + 1}", show_alert=True)
                    
            elif data.startswith("bypass_q_"):
                bypass_url = context.user_data.get("bypass_url")
                if not bypass_url:
                    await query.answer("Bypass link expired or invalid.", show_alert=True)
                    return
                
                from bot.database import get_config
                config = await get_config() or {}
                # The user requested "ref teh help text in /admin"
                help_url = config.get("shorten_help_link", config.get("help_text_url", "https://t.me/bot_paiyan_official"))

                keyboard = [
                    [InlineKeyboardButton("Use me to proceed", url=bypass_url)],
                    [InlineKeyboardButton("How to use ?", url=help_url)]
                ]
                await query.edit_message_text(
                    "🔥 **Queue Bypass Activated**\n\nClick the button below to verify and instantly bypass the processing queue.",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
            else:
                logger.debug(f"Non-admin callback not handled by fallback: {data}")

    except Exception as e:
        logger.error(f"❌ Error in callback_handler: {e}", exc_info=True)
        await query.answer("❌ Error processing request", show_alert=True)

async def generate_cloud_link(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str) -> None:
    """Generate cloud link for file"""
    try:
        user_id = update.effective_user.id
        from bot.database import get_user
        user = await get_user(user_id)
        plan = user.get("plan", "free") if user else "free"
        
        _settings = get_settings()
        raw_cloud_url = f"{_settings.WEBHOOK_URL}/watch/{file_id}"
        display_cloud_url = raw_cloud_url
        
        # 🔗 Apply Link Shortener exclusively for normal/free users
        if plan != "pro":
            try:
                from bot.services import LinkShortener
                shortened = await LinkShortener.track_and_shorten(file_id, user_id, raw_cloud_url)
                if shortened:
                    display_cloud_url = shortened
            except Exception as e:
                logger.error(f"Failed to apply tracking shortener: {e}")

        keyboard = [
            [InlineKeyboardButton("👁️ Watch", url=display_cloud_url),
             InlineKeyboardButton("📋 Copy Link", callback_data=f"copy_link_{file_id}")],
            [InlineKeyboardButton("🔗 Share", callback_data=f"share_link_{file_id}"),
             InlineKeyboardButton("🔐 Lock", callback_data=f"lock_link_{file_id}")]
        ]

        link_msg = (
            f"☁️ **Cloud Link Generated**\n\n"
            f"`{display_cloud_url}`\n\n"
            f"⏰ Expires in 7 days\n"
            f"📊 Views: 0"
        )

        if update.message:
            await update.message.reply_text(link_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.edit_message_text(link_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

        logger.info(f"✅ Cloud link generated: {file_id}")
    except Exception as e:
        logger.error(f"❌ Generate cloud link error: {e}")

async def copy_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Copy link to clipboard"""
    try:
        await update.callback_query.answer("📋 Link copied to clipboard!", show_alert=True)
    except Exception as e:
        logger.error(f"❌ Copy link error: {e}")

async def lock_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lock/unlock cloud link"""
    try:
        callback = update.callback_query
        await callback.answer()
        await callback.edit_message_text("🔒 **Link Locked**\n\nThis link is now private.", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"❌ Lock link error: {e}")



async def handle_us_destination_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user destination management menu"""
    try:
        query = update.callback_query
        user_id = update.effective_user.id
        await query.answer()

        from bot.database import get_user_destinations
        destinations = await get_user_destinations(user_id)

        keyboard = []
        # [Add Channel]
        keyboard.append([InlineKeyboardButton("➕ Add Channel", callback_data="us_dest_add")])
        
        # [Configured Channel List]
        if destinations:
            for d in destinations:
                cid = d.get("id", "unknown")
                title = d.get("title") or str(cid)
                keyboard.append([InlineKeyboardButton(f"📁 {title}", callback_data=f"us_dest_manage_{cid}")])
        else:
            keyboard.append([InlineKeyboardButton("ℹ️ No destinations configured", callback_data="ignore")])

        # [Cancel] [Back]
        keyboard.append([
            InlineKeyboardButton("❌ Cancel", callback_data="start"),
            InlineKeyboardButton("🔙 Back", callback_data="us_settings")
        ])

        await query.message.edit_text(
            f"🎯 **Destination Management**\n\n"
            f"Here you can manage channels where files will be forwarded after processing.\n"
            f"You have `{len(destinations)}` configured.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in user destination menu: {e}", exc_info=True)
        await query.answer("❌ Error", show_alert=True)

async def handle_us_dest_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt user to add a destination by forwarding a message"""
    try:
        query = update.callback_query
        await query.answer()
        context.user_data["awaiting"] = "us_dest_forward"
        await query.message.edit_text(
            "➕ **Add Destination Channel**\n\n"
            "1. Add the bot as an **Admin** in your channel.\n"
            "2. **Forward any message** from that channel here.\n\n"
            "Use /cancel to abort.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in us_dest_add: {e}")

async def handle_us_dest_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage a specific user destination"""
    try:
        query = update.callback_query
        await query.answer()
        channel_id = int(query.data.replace("us_dest_manage_", ""))
        user_id = query.from_user.id

        from bot.database import get_user_destinations, get_user
        destinations = await get_user_destinations(user_id)
        dest = next((d for d in destinations if d.get("id") == channel_id), None)

        if not dest:
            await query.answer("❌ Destination not found", show_alert=True)
            return
            
        # Get custom metadata for this channel
        user = await get_user(user_id)
        settings = user.get("settings", {})
        dest_metadata = settings.get("destination_metadata", {}).get(str(channel_id), {})
        meta_title = dest_metadata.get("title", "Default name")
        meta_author = dest_metadata.get("author", "Default author")

        title = dest.get("title") or str(channel_id)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Channel: {title}", callback_data="ignore")],
            [InlineKeyboardButton(f"📝 Custom Name: {meta_title[:15]}", callback_data=f"us_dest_meta_name_{channel_id}")],
            [InlineKeyboardButton(f"👤 Custom Author: {meta_author[:15]}", callback_data=f"us_dest_meta_auth_{channel_id}")],
            [InlineKeyboardButton("🗑️ Remove Destination", callback_data=f"us_dest_remove_confirm_{channel_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="us_destination")]
        ])
        
        await query.message.edit_text(
            f"🎯 **Manage Destination**\n\n"
            f"Channel: `{title}`\n"
            f"ID: `{channel_id}`\n\n"
            f"You can configure specific metadata for files forwarded to this channel.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in us_dest_manage: {e}")

async def handle_us_dest_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removal confirmation for user destination"""
    try:
        query = update.callback_query
        await query.answer()
        channel_id = int(query.data.replace("us_dest_remove_confirm_", ""))
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Removal", callback_data=f"us_dest_remove_do_{channel_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"us_dest_manage_{channel_id}")]
        ])
        await query.message.edit_text(
            "⚠️ **Remove Destination?**\n\nFiles will no longer be forwarded to this channel.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in us_dest_remove_confirm: {e}")

async def handle_us_dest_remove_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Actually remove the destination"""
    try:
        query = update.callback_query
        channel_id = int(query.data.replace("us_dest_remove_do_", ""))
        user_id = query.from_user.id
        
        from bot.database import remove_user_destination
        success = await remove_user_destination(user_id, channel_id)
        
        if success:
            await query.answer("✅ Channel removed successfully", show_alert=True)
        else:
            await query.answer("❌ Failed to remove or not found", show_alert=True)
            
        await handle_us_destination_button(update, context) # Auto callback back to list
    except Exception as e:
        logger.error(f"❌ Error in us_dest_remove_do: {e}")
        await query.answer("❌ Error", show_alert=True)

async def handle_us_dest_meta_name_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt user for custom destination name"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_meta_name_", "")
        context.user_data["awaiting"] = f"us_dest_meta_name_{channel_id}"
        await query.message.reply_text(
            "📝 **Send Custom Name**\n\n"
            "This name will be used as the file title when forwarding to this channel.\n"
            "Send the new name now or /cancel to abort.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_meta_name_prompt: {e}")

async def handle_us_dest_meta_auth_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt user for custom destination author"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_meta_auth_", "")
        context.user_data["awaiting"] = f"us_dest_meta_auth_{channel_id}"
        await query.message.reply_text(
            "👤 **Send Custom Author**\n\n"
            "This author will be set in the file metadata when forwarding to this channel.\n"
            "Send the new author name now or /cancel to abort.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_meta_auth_prompt: {e}")

async def handle_us_dest_meta_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process custom destination metadata input"""
    try:
        user_id = update.effective_user.id
        text = update.message.text
        awaiting = context.user_data.get("awaiting", "")
        
        user = await get_user(user_id)
        if not user:
            return
            
        settings = user.get("settings", {})
        dest_metadata = settings.get("destination_metadata", {})
        
        if awaiting.startswith("us_dest_meta_name_"):
            channel_id = awaiting.replace("us_dest_meta_name_", "")
            if channel_id not in dest_metadata:
                dest_metadata[channel_id] = {}
            dest_metadata[channel_id]["title"] = text
            await update.message.reply_text(f"✅ Custom name updated to: `{text}`", parse_mode="Markdown")
            
        elif awaiting.startswith("us_dest_meta_auth_"):
            channel_id = awaiting.replace("us_dest_meta_auth_", "")
            if channel_id not in dest_metadata:
                dest_metadata[channel_id] = {}
            dest_metadata[channel_id]["author"] = text
            await update.message.reply_text(f"✅ Custom author updated to: `{text}`", parse_mode="Markdown")
            
        settings["destination_metadata"] = dest_metadata
        await update_user(user_id, {"settings": settings})
        
        context.user_data.pop("awaiting", None)
        
    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_meta_input: {e}")



async def handle_user_destination_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle forwarded channel message for user destination setup"""
    try:
        msg = update.message
        forward_origin = msg.forward_origin
        user_id = update.effective_user.id
        
        channel_id = None
        channel_title = None
        
        if forward_origin and hasattr(forward_origin, 'chat'):
            channel_id = forward_origin.chat.id
            channel_title = forward_origin.chat.title or str(channel_id)
        elif msg.forward_from_chat:
             channel_id = msg.forward_from_chat.id
             channel_title = msg.forward_from_chat.title or str(channel_id)
             
        if channel_id:
            from bot.database import add_user_destination
            success = await add_user_destination(user_id, channel_id, channel_title)
            
            if success:
                await msg.reply_text(
                    f"✅ **Destination Added!**\n\n"
                    f"Channel: `{channel_title}`\n\n"
                    f"Processed files can now be forwarded there.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="us_destination")]])
                )
            else:
                await msg.reply_text("❌ Failed to add destination. Maybe it's already there?")
            
            context.user_data.pop("awaiting", None)
        else:
            await msg.reply_text("❌ Could not read channel from this forward. Please forward a message directly from your channel.")
    except Exception as e:
        logger.error(f"❌ Error in handle_user_destination_forward: {e}")
        await update.message.reply_text("❌ Error setting destination. Please try again.")

def get_progress_bar(progress: int) -> str:
    """
    3-color hill-style progress bar with emojis:
    """
    if progress < 0:
        progress = 0
    if progress >= 100:
        return "🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴 **100%**"

    if progress <= 30:
        block = "⚪"
    elif progress <= 69:
        block = "🟠"
    else:
        block = "🔴"

    level = min(progress // 10, 9)
    bar = block * 10
    return f"{bar} **{progress:>3}%**"

def format_eta(seconds: int) -> str:
    if seconds <= 0: return "0s"
    if seconds < 60: return f"{int(seconds)}s"
    m, s = divmod(int(seconds), 60)
    if m < 60: return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"

async def send_progress_message(
    bot, 
    user_id, 
    task_id, 
    filesize, 
    stage=None, 
    progress=None, 
    dump_channel=None,
    start_time=None
):
    """
    Send or update progress message in user PM + dump channel.
    """
    import time
    
    # Thread-safe storage
    if not hasattr(bot, "progress_data"):
        bot.progress_data = {}
    progress_data = bot.progress_data

    # Update current task state
    task_info = progress_data.setdefault(task_id, {})
    if stage is not None:
        task_info["stage"] = stage
    if progress is not None:
        task_info["progress"] = progress
    if start_time is not None:
        task_info["start_time"] = start_time
    elif "start_time" not in task_info:
        task_info["start_time"] = time.time()

    # Get current values
    current_stage = task_info.get("stage", "🚀 Starting...")
    current_progress = task_info.get("progress", 0)
    task_start_time = task_info.get("start_time", time.time())

    # Calculate ETA
    elapsed_time = time.time() - task_start_time
    eta_text = "⏳ **ETA:** Calculating..."
    speed_text = "📊 **Speed:** Calculating..."
    if current_progress > 0 and current_progress <= 100:
        eta_seconds = (elapsed_time / current_progress) * (100 - current_progress)
        eta_text = f"⏳ **ETA:** {format_eta(eta_seconds)}"
        
        # Approximate speed based on file size and elapsed time if size > 0
        if filesize > 0 and elapsed_time > 0:
            bytes_done = (current_progress / 100) * filesize
            speed_mb = (bytes_done / 1024 / 1024) / elapsed_time
            speed_text = f"⚡ **Speed:** {speed_mb:.1f} MB/s"

    # Generate beautiful progress bar
    progress_bar = get_progress_bar(current_progress)
    size_text = f"{filesize / (1024 * 1024):.1f} MB" if filesize > 0 else "---"

    message_text = (
        f"{current_stage}\n\n"
        f"{progress_bar}\n"
        f"📦 **Size:** {size_text}\n"
        f"{speed_text}\n"
        f"{eta_text}\n\n"
        f"🆔 **Task:** `{task_id}`"
    )

    keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_progress_{task_id}")]]

    if current_progress >= 100:
        message_text += "\n\n✅ **Processing Complete!** 🎉"
        keyboard = []  # Remove button when done

    try:
        # ——— Update User PM ———
        if "user_progress_msg_id" not in task_info:
            msg = await bot.send_message(
                chat_id=user_id,
                text=message_text,
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
                parse_mode="Markdown"
            )
            task_info["user_progress_msg_id"] = msg.message_id
            task_info["user_id"] = user_id
        else:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=task_info["user_progress_msg_id"],
                text=message_text,
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
                parse_mode="Markdown"
            )

        # ——— Update Dump Channel (optional) ———
        if dump_channel:
            dump_text = f"📤 **DUMP LOG**\n{message_text}"
            if "dump_progress_msg_id" not in task_info:
                msg = await bot.send_message(
                    chat_id=dump_channel,
                    text=dump_text,
                    parse_mode="Markdown"
                )
                task_info["dump_progress_msg_id"] = msg.message_id
            else:
                await bot.edit_message_text(
                    chat_id=dump_channel,
                    message_id=task_info["dump_progress_msg_id"],
                    text=dump_text,
                    parse_mode="Markdown"
                )

        # Save state
        bot.progress_data[task_id] = task_info

        if current_progress >= 100:
            logger.info(f"Task {task_id} completed — deleting progress message 🗑️")
            try:
                await bot.delete_message(chat_id=user_id, message_id=task_info["user_progress_msg_id"])
                # Also delete dump progress if exists
                if "dump_progress_msg_id" in task_info:
                    await bot.delete_message(chat_id=dump_channel, message_id=task_info["dump_progress_msg_id"])
            except Exception as e:
                logger.warning(f"Failed to delete progress message: {e}")
            
            # Clean up memory
            if task_id in bot.progress_data:
                del bot.progress_data[task_id]
            return

    except Exception as e:
        logger.error(f"send_progress_message failed for task {task_id}: {e}", exc_info=True)
        # Silent fallback — never crash the bot
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"⚠️ Warning: Progress update failed — but your file is still processing!\n🆔 Task: `{task_id}`",
                parse_mode="Markdown"
            )
        except:
            pass

async def finalize_progress(bot, task_id, success=True, result_text="", reply_markup=None):
    progress_data = getattr(bot, "progress_data", {})
    if task_id not in progress_data:
        return
    progress_info = progress_data[task_id]

    # User PM
    try:
        final_text = (
            f"✅ **Processing Complete!**\n\n{result_text}"
            if success
            else f"❌ **Processing Failed**\n\n{result_text}"
        )
        await bot.edit_message_text(
            chat_id=progress_info["user_id"],
            message_id=progress_info["progress_msg_id"],
            text=final_text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    except Exception:
        pass

    # Dump channel
    try:
        if "dump_progress_msg_id" in progress_info:
            final_dump_text = (
                "✅ **Processing Complete**\n\nFile processed successfully!"
                if success
                else "❌ **Processing Failed**\n\nCheck user PM for details."
            )
            await bot.edit_message_text(
                chat_id=progress_info["dump_channel"],
                message_id=progress_info["dump_progress_msg_id"],
                text=final_dump_text,
                parse_mode="Markdown",
            )
    except Exception:
        pass

    del bot.progress_data[task_id]

@rate_limit
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle general text input based on awaiting state"""
    try:
        user_id = update.effective_user.id
        text = (update.message.text or "").strip()
        awaiting = context.user_data.get("awaiting")
        
        if not awaiting:
            # If no awaiting state, check if it's a URL
            if validate_url(text)[0]:
                if not await check_force_sub(update, context):
                    return
                await handle_url_input(update, context)
            return

        logger.info(f"📩 Text input from {user_id} in state: {awaiting}")

        # Routing logic
        if awaiting == "channel_forward":
            from bot.handlers.admin import handle_admin_forwards
            await handle_admin_forwards(update, context)
            return

        if awaiting.startswith("us_dest_meta_name_") or awaiting.startswith("us_dest_meta_auth_"):
            await handle_us_dest_meta_input(update, context)
            return

        if awaiting.startswith("us_"):
            from bot.handlers import handle_user_settings_text
            await handle_user_settings_text(update, context, awaiting)
            return

        if awaiting.startswith("admin_"):
            from bot.handlers import handle_admin_input
            await handle_admin_input(update, context, awaiting)
            return

        if awaiting == "broadcast_message":
            from bot.handlers import handle_broadcast_message_input
            await handle_broadcast_message_input(update, context)
            return

        if awaiting.startswith("rclone_"):
            from bot.handlers.cloud import rclone_text_input
            await rclone_text_input(update, context)
            return

        if awaiting.startswith("terabox_"):
            await terabox_text_input(update, context)
            return

        # BUG-10 FIX: edit_* config states were being discarded (fell through to warning)
        if awaiting.startswith("edit_") or awaiting.startswith("add_shortener"):
            await handle_config_edit_input(update, context, awaiting)
            return

        if awaiting == "wiz_rename" or (awaiting and awaiting.startswith("wiz_meta_")):
            from bot.handlers.files import handle_wizard_text_input
            await handle_wizard_text_input(update, context, text)
            return

        # Fallback for unexpected states
        logger.warning(f"⚠️ Unhandled text input state: {awaiting}")

        if awaiting == "support_message":
            from infrastructure.database._legacy_bot._channels import add_chatbox_message
            success = await add_chatbox_message(user_id, text, sender_type="user")
            if success:
                await update.message.reply_text(
                    "✅ **Message Sent!**\n\nThe admin has been notified. Please wait for a reply.",
                    parse_mode="Markdown"
                )
                context.user_data.pop("awaiting", None)
                
                # Notify admins
                from config.settings import get_admin_ids
                for aid in get_admin_ids():
                    try:
                        await context.bot.send_message(
                            aid,
                            f"💬 **New Support Message**\n\n"
                            f"User: `{user_id}`\n"
                            f"Message: {text}\n\n"
                            f"Reply using /admin -> Chatbox",
                            parse_mode="Markdown"
                        )
                    except: pass
            else:
                await update.message.reply_text("❌ Failed to send message. Please try again later.")
            return

    except Exception as e:
        logger.error(f"❌ Error in handle_text_input: {e}", exc_info=True)
        await update.message.reply_text(f"❌ **Error Processing Input**\n\n{str(e)[:100]}")
    finally:
        # Optional: Clear state if it's a one-shot input
        # context.user_data.pop("awaiting", None)
        pass

async def handle_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo uploads (primarily for custom thumbnails)"""
    try:
        user_id = update.effective_user.id
        awaiting = context.user_data.get("awaiting")
        
        logger.info(f"🖼️ Photo received from user {user_id}")
        logger.info(f"   Awaiting state: {awaiting}")
        
        if awaiting == "us_thumbnail":
            logger.info(f"   → Routing to thumbnail handler")
            from bot.handlers import handle_us_thumbnail
            await handle_us_thumbnail(update, context)
            return
        
        logger.debug(f"   No handler for photo in state: {awaiting}")
        
    except Exception as e:
        logger.error(f"❌ Error handling photo: {e}", exc_info=True)

async def check_force_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user has joined all required force-subscription channels.
    Returns True if user can proceed, False if channels must be joined first.
    """
    try:
        from bot.database import get_force_sub_channels

        force_channels = await get_force_sub_channels()

        if not force_channels:
            return True

        user_id = update.effective_user.id
        not_joined = []

        for channel in force_channels:
            channel_id = channel.get("id")
            if not channel_id:
                continue

            try:
                member = await context.bot.get_chat_member(channel_id, user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    not_joined.append(channel)
            except TelegramError as e:
                await log_error(f"Error checking membership for {channel_id}: {str(e)}")
                continue

        if not_joined:
            keyboard = []

            for channel in not_joined:
                if isinstance(channel, dict):
                    channel_id = channel.get("id")
                    metadata = channel.get("metadata", {})
                    channel_name = metadata.get("title") or channel.get("name") or "Channel"
                    req_join = metadata.get("req_join", False)
                    invite_link = channel.get("invite_link", "")
                else:
                    channel_id = channel
                    channel_name = "Channel"
                    req_join = False
                    invite_link = ""

                if not invite_link:
                    try:
                        # If req_join is True, create a link that requires approval
                        link = await context.bot.create_chat_invite_link(
                            channel_id,
                            creates_join_request=req_join,
                            name=f"FSub_{user_id}"
                        )
                        invite_link = link.invite_link
                    except TelegramError as e:
                        logger.error(f"Error creating invite link for {channel_id}: {e}")

                if invite_link:
                    label = f"✨ Request to Join {channel_name}" if req_join else f"✨ Join {channel_name}"
                    keyboard.append([InlineKeyboardButton(label, url=invite_link)])

            keyboard.append([InlineKeyboardButton("✅ I Joined, Continue", callback_data="check_subscription")])

            if keyboard:
                msg = None
                if update.message:
                    msg = update.message
                elif update.callback_query:
                    msg = update.callback_query.message
                if msg:
                    await msg.reply_text(
                        "⚠️ **Subscription Required**\n\n"
                        "To use this bot, you must join our channels first.\n\n"
                        "👇 Click the button(s) below to join:",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode="Markdown"
                    )

            return False

        return True

    except Exception as e:
        await log_error(f"Error in force sub check: {str(e)}")
        return True

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - user registration and welcome"""
    try:
        user_id = update.effective_user.id
        username = update.effective_user.username or "Unknown"
        first_name = update.effective_user.first_name or "User"
        
        # DEBUG: Log immediately to confirm handler trigger
        logger.info(f"⚡ /start request received from {user_id} - Handler Triggered")
        
        logger.info(f"🔍 /start called for user: {user_id}")
        
        # Check for deep link parameters
        if context.args:
            arg = context.args[0]
            if arg == "id":
                 # Auto-register user when they request their ID
                 user = await get_user(user_id)
                 if not user:
                     await create_user(user_id, first_name, username)
                     logger.info(f"🆕 New user registered via /start id: {user_id} ({first_name})")
                     await log_user_update(context.bot, user_id, "registered via start ID")
                     
                 await update.message.reply_text(f"`{user_id}`", parse_mode="Markdown")
                 return
            elif arg.startswith("bypass_"):
                 from bot.database import get_db
                 db = get_db()
                 bypass_token = arg
                 task = await db.tasks.find_one_and_update(
                     {"wizard_bypass_token": bypass_token, "status": "queued"},
                     {"$set": {"priority": 100, "wizard_bypass_token": None}}
                 )
                 if task:
                     await update.message.reply_text(
                         "🚀 **Queue Bypassed!**\n\nYour file has been moved to the front of the queue and will begin processing momentarily.",
                         parse_mode="Markdown"
                     )
                 else:
                     await update.message.reply_text(
                         "❌ **Invalid or Expired Token**\n\nThis bypass link is no longer valid or has already been used.",
                         parse_mode="Markdown"
                     )
                 return
             
        # Fetch or create user in the database
        user = await get_user(user_id)
        if not user:
            user = await create_user(user_id, first_name, username)
            logger.info(f"🆕 New user registered: {user_id} ({first_name})")
            await log_user_update(context.bot, user_id, "registered")
        user_plan = user.get("plan", "free") if user else "free"
        
        keyboard = [
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="us_settings"),
                InlineKeyboardButton("📊 Stats", callback_data="us_stats")
            ],
            [
                InlineKeyboardButton("📂 My Files", callback_data="us_myfiles"),
                InlineKeyboardButton("💬 Support", callback_data="us_support")
            ],
            [InlineKeyboardButton("📚 Help Guide", callback_data="us_help")]
        ]
        
        await update.message.reply_text(
            WELCOME_MSG,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        
        # Log user action
        await log_user_update(context.bot, user_id, "started bot")
        logger.info(f"✅ /start by {user_id} ({username}) - Plan: {user_plan}")
        
    except Exception as e:
        logger.error(f"❌ Error in start command: {str(e)}", exc_info=True)
        await log_error(f"❌ Error in start command: {str(e)}")
        try:
            await update.message.reply_text(
                "❌ **Error**\n\n"
                "Something went wrong. Please try /start again.\n\n"
                "If this persists, use /support.",
                parse_mode="Markdown"
            )
        except:
            pass

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    try:
        config = await get_config()
        help_text = config.get("help_text", HELP_TEXT) if config else HELP_TEXT
        
        await update.message.reply_text(help_text, parse_mode="Markdown")
        await log_info(f"✅ /support used by {update.effective_user.id}")

    except Exception as e:
        logger.error(f"❌ Error in support command: {e}", exc_info=True)
        await log_error(f"❌ Error in support command: {str(e)}")
        await update.message.reply_text(
            "❌ Unable to load support info. Please try again.",
            parse_mode="Markdown"
        )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel — context-aware cancel of current awaiting state"""
    try:
        user_id = update.effective_user.id

        # Map every awaiting-state key → (what was being done, where to go back)
        STATE_MESSAGES = {
            "us_prefix":            ("✏️ Prefix edit",              "/ussettings"),
            "us_suffix":            ("✏️ Suffix edit",              "/ussettings"),
            "us_remove_word":       ("🗑️ Word removal",             "/ussettings"),
            "us_meta_author":       ("🏷️ Metadata author edit",     "/ussettings"),
            "us_meta_subtitle":     ("🎞️ Subtitle injection",       "/ussettings"),
            "us_destination":       ("🎯 Destination channel setup", "/ussettings"),
            "wiz_inject_audio":     ("🎵 Audio injection wizard",   "/ussettings"),
            "wiz_inject_subs":      ("🎞️ Subtitle injection wizard","/ussettings"),
            "wiz_rename":           ("✏️ Rename wizard",            "/ussettings"),
            "broadcast_message":    ("📢 Broadcast compose",        "/admin → Broadcast"),
            "admin_find_user":      ("🔍 Find user",                "/admin → Users"),
            "admin_ban_user":       ("🔨 Ban user",                 "/admin → Users"),
            "admin_unban_user":     ("🔓 Unban user",               "/admin → Users"),
            "admin_upgrade_user":   ("⬆️ Upgrade user",            "/admin → Users"),
            "edit_start_msg":       ("💬 Start message edit",       "/admin → Config"),
            "edit_watermark":       ("💦 Watermark edit",           "/admin → Config"),
            "edit_contact":         ("☎️ Support contact edit",     "/admin → Config"),
            "edit_help_text":       ("📖 Help text edit",           "/admin → Config"),
            "edit_site_name":       ("🏢 Site name edit",           "/admin → Config"),
            "edit_site_desc":       ("📝 Description edit",         "/admin → Config"),
            "edit_support_channel": ("🔗 Support channel edit",     "/admin → Config"),
            "edit_parallel":        ("⚡ Parallel limit edit",      "/admin → Config"),
            "edit_max_filesize":    ("📦 Max file size edit",       "/admin → Config"),
            "edit_file_expiry":     ("📅 File expiry edit",         "/admin → Config"),
            "add_shortener":        ("🔗 Shortener setup",          "/admin → Shorteners"),
        }

        awaiting = context.user_data.get("awaiting") or context.user_data.get("awaiting_channel_type")

        if awaiting and awaiting in STATE_MESSAGES:
            what, where = STATE_MESSAGES[awaiting]
            msg = (
                f"✅ **{what} cancelled.**\n\n"
                f"Nothing was saved.\n\n"
                f"Return via {where}."
            )
        elif awaiting:
            msg = "✅ **Current operation cancelled.**\n\nNothing was saved."
        else:
            msg = (
                "ℹ️ **Nothing to cancel.**\n\n"
                "You are not in the middle of any setup.\n\n"
                "**Quick links:**\n"
                "• /start — Home\n"
                "• /ussettings — Your settings\n"
                "• /myfiles — Your files\n"
                "• /cancelTask\\_ID — Cancel a running task"
            )

        # Clear all awaiting / wizard / rclone state keys
        clear = [k for k in context.user_data
                 if k in ("awaiting", "broadcast_awaiting")
                 or k.startswith(("awaiting_", "rclone_", "wiz_"))]
        for k in clear:
            context.user_data.pop(k, None)

        await update.message.reply_text(msg, parse_mode="Markdown")
        logger.info(f"✅ /cancel by {user_id} — was awaiting: {awaiting or 'nothing'}")

    except Exception as e:
        logger.error(f"❌ Error in cancel_command: {e}", exc_info=True)

async def cancel_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel_<taskid> — cancel a specific running processing task"""
    try:
        import re
        user_id = update.effective_user.id
        text = (update.message.text or "").strip()

        # Accept: /cancel_abc123  or  /cancel abc123
        match = re.match(r"^/cancel[_\s]+(.+)$", text, re.IGNORECASE)
        if not match:
            await update.message.reply_text(
                "⚠️ **Usage:** `/cancel_<task_id>`\n\n"
                "Example: `/cancel_abc123`\n\n"
                "Use /myfiles to see your task IDs.",
                parse_mode="Markdown"
            )
            return

        task_id = match.group(1).strip()

        tasks = await get_user_tasks(user_id) or []
        task = next((t for t in tasks if str(t.get("task_id", "")) == task_id), None)

        if not task:
            await update.message.reply_text(
                f"❌ **Task not found:** `{task_id}`\n\n"
                f"It may have already finished, or the ID is wrong.\n"
                f"Use /myfiles to see active tasks.",
                parse_mode="Markdown"
            )
            return

        status = task.get("status", "unknown")
        if status in ("completed", "failed", "cancelled"):
            await update.message.reply_text(
                f"ℹ️ **Task Already {status.title()}**\n\n"
                f"`{task_id}` is already `{status}` — no action needed.",
                parse_mode="Markdown"
            )
            return

        # Write cancellation flag to DB
        db = context.application.bot_data.get("db")
        if db is not None:
            await db.tasks.update_one(
                {"task_id": task_id, "user_id": user_id},
                {"$set": {"status": "cancelled"}}
            )

        filename = task.get("filename", task_id)
        await update.message.reply_text(
            f"✅ **Task Cancelled**\n\n"
            f"📄 File: `{filename}`\n"
            f"🆔 ID: `{task_id}`\n\n"
            f"If the task is actively processing it will stop at the next safe checkpoint.",
            parse_mode="Markdown"
        )
        logger.info(f"✅ Task {task_id} cancelled by user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in cancel_task_command: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ Could not cancel the task. Please try again.",
            parse_mode="Markdown"
        )

from bot.database import get_config
async def unknown_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unknown commands sent by users"""
    try:
        if not update.message or not update.message.text:
            return

        user_id = update.effective_user.id
        username = update.effective_user.username or "Unknown"
        command = update.message.text

        logger.warning(f"⚠️ Unknown command from {user_id} (@{username}): {command}")

        # Log the unknown command
        try:
            await log_info(f"Unknown command: {command} by user {user_id}")
        except:
            pass

        # Get list of available commands
        available_commands = [
            "/start - Initialize your account",
            "/help - Show help message",
            "/stats - View your statistics",
            "/support - Get support",
            "/myfiles - View your files",
            "/ussettings - Open settings menu",
            "/cancel - Cancel current operation",
        ]

        # Add admin commands if user is admin
        if user_id in ADMIN_IDS:
            available_commands.extend([
                "/admin - Open admin panel",
                "/rclone - Configure rclone",
                "/terabox - Configure terabox",
            ])

        # Build response message
        commands_list = "\n".join(available_commands)

        # Get updates channel from config for the URL
        config = await get_config() or {}
        updates_ch = config.get("updates_channel")
        if updates_ch:
            if not updates_ch.startswith("http") and not updates_ch.startswith("@"):
                updates_ch = f"@{updates_ch}"
            if updates_ch.startswith("@"):
                updates_url = f"https://t.me/{updates_ch[1:]}"
            else:
                updates_url = updates_ch
        else:
            updates_url = f"https://t.me/{settings.BOT_USERNAME or 'cc_leechbot'}"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Updates Channel", url=updates_url)]
        ])

        await update.message.reply_text(
            f"❓ **Unknown command:** `{command}`\n\n"
            "I'm sorry, I don't recognize that command. "
            "Here are the available commands:\n\n"
            f"{commands_list}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in unknown_handler: {e}", exc_info=True)

async def handle_callback_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle support button callback"""
    try:
        await support_command(update, context)
    except Exception as e:
        logger.error(f"❌ Error in callback support: {e}")
        await update.callback_query.answer("❌ Error loading support", show_alert=True)

async def handle_callback_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle help button callback"""
    try:
        await help_command(update, context)
    except Exception as e:
        logger.error(f"❌ Error in callback help: {e}")
        await update.callback_query.answer("❌ Error loading help", show_alert=True)

async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /support command"""
    try:
        config = await get_config()
        support_channel = config.get("support_channel", "") if config else ""
        contact_details = config.get("contact_details", "No contact details available") if config else "No contact details available"

        keyboard = []
        if support_channel:
            keyboard.append([InlineKeyboardButton("💬 Get Support (Channel)", url=support_channel)])
        
        keyboard.append([InlineKeyboardButton("💬 Bot Support (Chat)", callback_data="start_support_chat")])

        await update.message.reply_text(
            f"💁 **Need Help?**\n\n{contact_details}\n\n"
            f"Click below to get support or chat with us directly:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
            
        await log_info(f"✅ /support used by {update.effective_user.id}")

    except Exception as e:
        logger.error(f"Error in support command: {e}", exc_info=True)
        await log_error(f"❌ Error in support command: {str(e)}")
        await update.message.reply_text(
            "❌ Unable to load support info. Please try again.",
            parse_mode="Markdown"
        )

async def handle_subtitle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        keyboard = [
            [InlineKeyboardButton("✅ Inject", callback_data="us_inject_sub")],
            [InlineKeyboardButton("🚫 None", callback_data="us_none_sub")],
            [InlineKeyboardButton("🔙 Back", callback_data="us_metadata")],
        ]

        await query.message.edit_text(
            "📖 **Subtitle Options**\n\n"
            "Choose how to handle subtitles:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        logger.info(f"Subtitle menu opened for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_subtitle_menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_inject_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        user = await get_user(user_id)

        if user:
            await update_user(user_id, {"settings.metadata.subtitle": "Inject"})
            await update.callback_query.answer("✅ Subtitles will be injected", show_alert=True)
            logger.info(f"Subtitles injection enabled for user {user_id}")
        else:
            await update.callback_query.answer("❌ User not found", show_alert=True)

    except Exception as e:
        logger.error(f"❌ Error in handle_inject_sub: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_us_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        user = await get_user(user_id)
        if not user:
            await query.answer("❌ User not found", show_alert=True)
            return

        # Show confirmation dialog
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes, Reset All", callback_data="us_reset_confirm_yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="us_remove")
            ]
        ]

        await query.message.edit_text(
            "⚠️ **Reset All Settings?**\n\n"
            "This will reset:\n"
            "- Prefix & Suffix\n"
            "- Metadata settings\n"
            "- Send mode\n"
            "- Destination channel\n"
            "- All custom settings\n\n"
            "**This action cannot be undone!**",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        logger.info(f"✅ Reset confirmation shown for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_us_remove_confirm: {e}", exc_info=True)
        await query.answer("❌ Error", show_alert=True)

async def handle_us_reset_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        user = await get_user(user_id)
        if not user:
            await query.answer("❌ User not found", show_alert=True)
            return

        # Reset all settings to defaults
        user["settings"] = {
            "prefix": "",
            "suffix": "",
            "mode": "video",
            "metadata": {},
            "destination_channel": None,
            "destination_metadata": {},
            "remove_words": [],
            "thumbnail": "auto"
        }

        await update_user(user_id, user)

        await query.answer("✅ All settings reset to defaults!", show_alert=True)

        await log_user_update(context.bot, user_id, "reset all settings to defaults")
        logger.info(f"✅ All settings reset for user {user_id}")

        # Return to settings menu
        await ussettings_command(update, context)

    except Exception as e:
        logger.error(f"❌ Error in handle_us_reset_confirm_yes: {e}", exc_info=True)
        await query.answer("❌ Error", show_alert=True)

async def handle_meta_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        await query.message.reply_text("🎬 **Set Default Title**\n\nSend the text you want as the default title for all files.", parse_mode="Markdown")
        context.user_data["awaiting"] = "us_meta_title"

        logger.info(f"✅ Title metadata input awaiting for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_meta_title: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_meta_author(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        await query.message.reply_text("👤 **Set Default Author / Artist**\n\nSend the text you want as the default author/artist:", parse_mode="Markdown")
        context.user_data["awaiting"] = "us_meta_author"

        logger.info(f"✅ Author metadata input awaiting for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_meta_author: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_meta_year(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        await query.message.reply_text("📅 **Set Default Year**\n\nSend the year you want as the default year:", parse_mode="Markdown")
        context.user_data["awaiting"] = "us_meta_year"

        logger.info(f"✅ Year metadata input awaiting for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_meta_year: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_meta_subtitle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        await query.message.reply_text("📖 **Set Default Subtitle**\n\nTo disable subtitles, type `none`.\nOtherwise, send the subtitle language you want to extract/inject automatically.", parse_mode="Markdown")
        context.user_data["awaiting"] = "us_meta_subtitle"

        logger.info(f"✅ Subtitle metadata input awaiting for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_meta_subtitle: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_meta_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        context.user_data["metadata_type"] = "video"

        keyboard = [
            [InlineKeyboardButton("🔙 Back", callback_data="us_metadata")],
        ]

        await query.message.edit_text(
            "🎬 **Video Metadata**\n\n"
            "Video metadata will be preserved automatically.\n\n"
            "Settings saved.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        logger.info(f"✅ Video metadata menu opened for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_meta_video: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_meta_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        context.user_data["metadata_type"] = "audio"

        keyboard = [
            [InlineKeyboardButton("🔙 Back", callback_data="us_metadata")],
        ]

        await query.message.edit_text(
            "🎵 **Audio Metadata**\n\n"
            "Audio metadata will be preserved automatically.\n\n"
            "Settings saved.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        logger.info(f"✅ Audio metadata menu opened for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_meta_audio: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_us_visibility(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        user = await get_user(user_id)
        if not user:
             return

        # Check Plan
        plan = user.get("plan", "free")
        if plan == "free":
            await query.message.reply_text(
                "💎 **Premium Feature**\n\n"
                "Only **Pro Users** can set default file visibility to Private.\n\n"
                "Free users' files are always Public.\n"
                "Use /support to upgrade.",
                parse_mode="Markdown"
            )
        
        # Refresh menu
        await ussettings_command(update, context)

    except Exception as e:
        logger.error(f"❌ Error in handle_us_visibility: {e}", exc_info=True)
        await update.callback_query.answer("❌ Error", show_alert=True)

async def handle_us_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user plan / storage info"""
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        user = await get_user(user_id)
        if not user:
            await query.answer("❌ User not found", show_alert=True)
            return

        plan = user.get("plan", "free")
        storage_limit = user.get("storage_limit", 5 * 1024 * 1024 * 1024)
        used_storage = user.get("used_storage", 0)
        daily_limit = user.get("daily_limit", 5)
        daily_used = user.get("daily_used", 0)

        storage_limit_gb = storage_limit / (1024 ** 3)
        used_storage_gb = used_storage / (1024 ** 3)
        percentage = (used_storage_gb / storage_limit_gb * 100) if storage_limit_gb > 0 else 0

        plan_emoji = "⭐" if plan == "free" else "💎"
        bar_filled = int(percentage / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)

        keyboard = [
            [InlineKeyboardButton("💬 Upgrade Plan", callback_data="us_support")],
            [InlineKeyboardButton("🔙 Back", callback_data="us_back")],
        ]

        await query.message.edit_text(
            f"{plan_emoji} **Your Plan: {plan.upper()}**\n\n"
            f"**Storage:**\n"
            f"[{bar}] {percentage:.1f}%\n"
            f"Used: `{used_storage_gb:.2f} GB` / `{storage_limit_gb:.1f} GB`\n\n"
            f"**Daily Uploads:**\n"
            f"Used today: `{daily_used}` / `{daily_limit}`\n\n"
            f"{'💎 Upgrade to Pro for more storage & unlimited uploads!' if plan == 'free' else '✅ You are on the Pro plan.'}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

        logger.info(f"✅ Plan info shown to user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_us_plan: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_rem_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        await query.message.reply_text(prompt, parse_mode="Markdown")
        context.user_data["awaiting"] = "us_remove_word"

        logger.info(f"✅ Remove word input awaiting for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_rem_word: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_rem_meta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()
        
        user = await get_user(user_id)
        if user:
            await update_user(user_id, {"settings.metadata": {}})
            await query.answer("✅ All metadata cleared!", show_alert=True)
            
            await log_user_update(context.bot, user_id, "cleared metadata")
            logger.info(f"✅ Metadata cleared for user {user_id}")
            
            # Return to settings menu
            await ussettings_command(update, context)
        else:
            await query.answer("❌ User not found", show_alert=True)

    except Exception as e:
        logger.error(f"❌ Error in handle_rem_meta: {e}", exc_info=True)
        await query.answer(f"❌ Error", show_alert=True)

async def handle_rem_inject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()
        
        user = await get_user(user_id)
        if user:
            await update_user(user_id, {"settings.inject": None})
            await query.answer("✅ Injection settings cleared!", show_alert=True)
            
            await log_user_update(context.bot, user_id, "cleared injection settings")
            logger.info(f"✅ Injection settings cleared for user {user_id}")
            
            # Return to settings menu
            await ussettings_command(update, context)
        else:
            await query.answer("❌ User not found", show_alert=True)

    except Exception as e:
        logger.error(f"❌ Error in handle_rem_inject: {e}", exc_info=True)
        await query.answer(f"❌ Error", show_alert=True)

async def handle_us_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the remove/reset settings menu"""
    try:
        query = update.callback_query
        await query.answer()
        keyboard = [
            [InlineKeyboardButton("🗑️ Remove Word", callback_data="rem_word")],
            [InlineKeyboardButton("📋 Clear Metadata", callback_data="rem_meta")],
            [InlineKeyboardButton("❌ Reset All Settings", callback_data="us_remove_confirm")],
            [InlineKeyboardButton("🔙 Back", callback_data="us_back")],
        ]
        await query.message.edit_text(
            "🗑️ **Remove Settings**\n\nChoose what to remove:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_us_remove: {e}")
        await update.callback_query.answer("❌ Error", show_alert=True)

async def handle_us_destination_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: prompt user to forward a message from their destination channel"""
    try:
        query = update.callback_query
        await query.answer()
        await query.message.reply_text(
            "📡 **Set Destination Channel**\n\n"
            "Forward any message from your **destination channel** here.\n\n"
            "Make sure the bot is an **admin** in that channel first.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "us_destination"
        logger.info(f"✅ Destination prompt sent to {update.effective_user.id}")
    except Exception as e:
        logger.error(f"❌ Error in handle_us_destination_button: {e}")
        await update.callback_query.answer("❌ Error", show_alert=True)

async def handle_us_reset_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset all user settings to defaults after confirmation"""
    try:
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        await update_user(user_id, {
            "settings": {
                "prefix": "",
                "suffix": "",
                "mode": "video",
                "metadata": {},
                "destination_channel": None,
                "remove_words": [],
                "thumbnail": "auto"
            }
        })
        await query.answer("✅ All settings reset to defaults!", show_alert=True)
        await ussettings_command(update, context)
        logger.info(f"✅ Settings reset for user {user_id}")
    except Exception as e:
        logger.error(f"❌ Error in handle_us_reset_confirm_yes: {e}")
        await update.callback_query.answer("❌ Error", show_alert=True)

async def handle_callback_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help text as callback (for us_help button in /start menu)"""
    try:
        query = update.callback_query
        await query.answer()
        await query.message.edit_text(
            HELP_TEXT,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="start")]])
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_callback_help: {e}")
        try:
            await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")
        except: pass

async def handle_callback_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show support info as callback (for us_support button)"""
    try:
        query = update.callback_query
        await query.answer()
        config = await get_config() or {}
        
        # Check for custom upgrade text
        upgrade_text = config.get("upgrade_text")
        if upgrade_text:
            text = upgrade_text.replace("<p>", "\n\n").replace("</p>", "").strip()
            parse_mode = "HTML"
        else:
            support_channel = config.get("support_channel") or config.get("channels", {}).get("support")
            contact = config.get("support_contact", "")
            text = (
                "💬 **Support & Upgrades**\n\n"
                f"{'Channel: ' + support_channel if support_channel else ''}"
                f"{'\\n' if support_channel and contact else ''}"
                f"{'Contact: ' + contact if contact else ''}"
            ).strip() or "💬 **Support**\n\nPlease contact the admin for help."
            parse_mode = "Markdown"
            
        keyboard = []
        keyboard.append([InlineKeyboardButton("💬 Bot Support (Chat)", callback_data="start_support_chat")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="start")])

        await query.message.edit_text(
            text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_callback_support: {e}")
        await update.callback_query.answer("❌ Error", show_alert=True)



async def handle_start_support_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start chat with support - prompt for first message"""
    try:
        query = update.callback_query
        await query.answer()
        
        await query.message.edit_text(
            "💬 **Support Chat**\n\n"
            "Please send your message or question below.\n"
            "An admin will reply to you as soon as possible.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="support")]])
        )
        context.user_data["awaiting"] = "support_message"
        logger.info(f"✅ User {update.effective_user.id} started support chat")
    except Exception as e:
        logger.error(f"❌ Error in handle_start_support_chat: {e}")
        await update.callback_query.answer("❌ Error initiating chat", show_alert=True)
