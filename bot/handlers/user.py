import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import TelegramError
from bot.middleware import admin_only, rate_limit, is_admin
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
    get_user_position,  # Added
    get_task,  # Added
)
from bot.utils import (
    send_auto_delete_msg,
    log_info,
    log_error,
    log_user_update,
    validate_url,
)
from bot.services import create_or_update_storage_message, FFmpegService

# Circular imports moved inside functions:
# from bot.handlers.files import handle_url_input
# from bot.handlers.cloud import terabox_text_input
# from bot.handlers.settings import handle_config_edit_input
from bot.handlers.settings import ussettings_command
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


def paginate_keyboard(
    buttons: List, page: int, per_page: int = 4, prefix: str = "admin_page"
) -> InlineKeyboardMarkup:
    """Create paginated keyboard with 2x2 grid layout"""
    total = len(buttons)
    start = page * per_page
    end = start + per_page
    page_buttons = buttons[start:end]

    grid = []
    for i in range(0, len(page_buttons), 2):
        row = page_buttons[i : i + 2]
        grid.append([btn for btn in row])

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("⬅️ Previous", callback_data=f"{prefix}_{page - 1}")
        )
    if end < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}_{page + 1}"))

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
                plans_text += (
                    f"- Daily Limit: {plan_data.get('storage_per_day', 5)} GB\n"
                )
                plans_text += (
                    f"- Expiry: {plan_data.get('dump_expiry_days', 0)} days\n\n"
                )

        up_text = config.get("upgrade_text", "Not Set")
        if up_text and len(up_text) > 50:
            up_text = up_text[:47] + "..."

        plans_text += (
            f"⚡ **Universal Parallel**: `{config.get('parallel_global_limit', 5)}`\n"
            f"💎 **Upgrade Text**: `{up_text}`\n"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🆓 Free Plan", callback_data="edit_plan_free"
                    ),
                    InlineKeyboardButton(
                        "💎 Pro Plan", callback_data="edit_plan_premium"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📝 Edit Upgrade Text", callback_data="edit_upgrade_text"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⚡ Universal Parallel", callback_data="edit_parallel"
                    )
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_back")],
            ]
        )

        await update.callback_query.message.edit_text(
            plans_text, reply_markup=keyboard, parse_mode="Markdown"
        )
        await log_admin_action(update.effective_user.id, "opened_plans", {})
        logger.info("✅ Plans menu opened")

    except Exception as e:
        logger.error(f"❌ Error in plans menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


async def ask_channel_forward(
    update: Update, context: ContextTypes.DEFAULT_TYPE, channel_type: str
):
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
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "channel_forward"
        context.user_data["awaiting_channel_type"] = channel_type
        logger.info(
            f"✅ Waiting for {channel_type} input from admin {update.effective_user.id}"
        )
    except Exception as e:
        logger.error(f"❌ Error in ask_channel_forward: {e}", exc_info=True)


async def show_shorteners_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show link shorteners menu"""
    try:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ Add Shortener", callback_data="add_shortener"
                    )
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_back")],
            ]
        )
        await update.callback_query.message.edit_text(
            "🔗 **Link Shorteners**\n\nManage link shortener integrations.",
            reply_markup=keyboard,
            parse_mode="Markdown",
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
            parse_mode="Markdown",
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

    logger.info(f"🔔 CALLBACK RECEIVED: '{data}' from user {user_id}")

    try:
        admin_ids = get_admin_ids()

        # Unified admin check — covers both env-list IDs and DB role=admin
        _user_is_admin: bool = await is_admin(user_id)

        # Import needed handlers locally to avoid circularity
        from bot.handlers import (
            handle_admin_list_users,
            handle_admin_back,
            handle_admin_users,
            handle_admin_stats,
            show_config_menu,
            show_plans_menu,
            show_shorteners_menu,
            handle_admin_broadcast,
            handle_admin_rclone,
            handle_admin_terabox,
            handle_admin_filesize,
            handle_admin_logs,
            show_banned_users,
            handle_admin_chatbox,
            handle_admin_set_log_channel,
            handle_admin_set_dump_channel,
            handle_admin_set_storage_channel,
            handle_admin_set_force_sub_channel,
            handle_admin_fsub_add,
            handle_admin_fsub_manage,
            handle_admin_fsub_toggle,
            handle_admin_fsub_link,
            handle_admin_fsub_remove_confirm,
            handle_admin_fsub_remove,
            handle_admin_remove_log,
            handle_admin_remove_dump,
            handle_admin_remove_storage,
            handle_view_user,
            handle_unban_from_list,
            handle_admin_ban_user,
            handle_admin_find_user,
            handle_admin_unban_user,
            handle_admin_upgrade_user,
            handle_edit_start_message,
            handle_edit_watermark,
            handle_edit_support_contact,
            handle_edit_help_text,
            handle_edit_site_name,
            handle_edit_site_description,
            handle_edit_support_channel,
            handle_edit_parallel_limit,
            handle_edit_max_filesize,
            handle_edit_file_expiry,
            handle_edit_tos,
            handle_edit_upgrade_text,
            handle_edit_plan,
            handle_add_shortener,
            handle_edit_force_subs,
            handle_broadcast_compose,
            handle_broadcast_stats,
            handle_broadcast_cancel,
            handle_admin_add_rclone,
            handle_admin_add_rclone_wizard,
            handle_list_rclone_remotes,
            handle_test_rclone,
            handle_disable_rclone,
            handle_admin_rclone,
            handle_terabox_setup_key,
            handle_terabox_test,
            handle_terabox_stats,
            handle_terabox_disable,
            handle_set_max_filesize,
            handle_cleanup_old_files,
            handle_storage_stats,
            handle_admin_fsub_req_toggle,
            handle_us_dest_add,
            handle_us_dest_manage,
            handle_us_dest_remove_confirm,
            handle_us_dest_remove_do,
            handle_us_dest_caption_builder,
            handle_us_dest_cap_filename,
            handle_us_dest_cap_filesize,
            handle_us_dest_cap_url_label,
            handle_us_dest_cap_style,
            handle_us_dest_cap_reset,
            handle_us_dest_buttons,
            handle_us_dest_buttons_edit,
            handle_us_dest_buttons_clear,
            handle_us_dest_shortener_toggle,
            handle_us_dest_download_link_choice,
            handle_admin_delete_rclone_prompt,
            handle_admin_delete_rclone_confirm,
            handle_admin_rename_rclone_prompt,
            handle_view_rclone,
            handle_test_single_rclone,
            handle_rclone_edit_creds_prompt,
            rclone_users_callback,
            handle_toggle_rclone,
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
                await query.answer(
                    "⚠️ Task is already in progress or has expired.", show_alert=True
                )
                return

            # Set back to queued so worker picks it up
            await db.tasks.update_one(
                {"task_id": task_id},
                {"$set": {"status": "queued", "wait_responded_at": datetime.utcnow()}},
            )
            await query.message.edit_text(
                "✅ **Processing will start now!**\n\nPlease wait a moment...",
                parse_mode="Markdown",
            )
            await query.answer("🚀 Starting task...")
            logger.info(f"✅ User {user_id} clicked Start for task {task_id}")
            return

        # Admin-only callbacks check
        if (
            data.startswith("admin")
            or data.startswith("edit_")
            or data.startswith("broadcast_")
            or data.startswith("banned_")
            or data.startswith("listusers_")
            or data.startswith("unban_user_")
            or data.startswith("view_user_")
            or data.startswith("rclone_")
            or data.startswith("terabox_")
            or data.startswith("set_max_")
            or data.startswith("cleanup_")
            or data.startswith("storage_")
            or data.startswith("shortener_")
            or data.startswith("admin_fsub_")
            or data.startswith("upgrade_user_")
        ):
            if not _user_is_admin:
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
            elif data.startswith(
                ("edit_plan_", "edit_price_", "edit_daily_", "edit_expiry_")
            ):
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
            elif data.startswith("rclone_delete_prompt_"):
                await handle_admin_delete_rclone_prompt(update, context)
            elif data.startswith("rclone_delete_confirm_"):
                await handle_admin_delete_rclone_confirm(update, context)
            elif data.startswith("rclone_rename_prompt_"):
                await handle_admin_rename_rclone_prompt(update, context)
            elif data.startswith("view_rclone_"):
                await handle_view_rclone(update, context)
            elif data.startswith("test_single_rclone_"):
                await handle_test_single_rclone(update, context)
            elif data.startswith("rclone_edit_creds_"):
                await handle_rclone_edit_creds_prompt(update, context)
            elif data.startswith("rclone_users_"):
                await rclone_users_callback(update, context)
            elif data.startswith("toggle_rclone_"):
                await handle_toggle_rclone(update, context)

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
            elif data in (
                "filter_logs_user",
                "view_error_logs",
                "download_logs",
                "clear_old_logs",
            ):
                await query.message.reply_text(
                    "📋 Feature coming soon.", parse_mode="Markdown"
                )

            else:
                logger.warning(f"⚠️ Unknown admin callback: {data}")

        # Non-admin callbacks
        else:
            logger.info(f"🔔 Non-admin callback: {data}")
            if data == "us_destination":
                await handle_us_destination_button(update, context)
            elif data == "us_dest_add":
                await handle_us_dest_add(update, context)
            elif data.startswith("us_dest_manage_"):
                logger.info(f"🔔 Calling handle_us_dest_manage for: {data}")
                await handle_us_dest_manage(update, context)
            elif data.startswith("us_dest_shortener_"):
                await handle_us_dest_shortener_toggle(update, context)
            elif data.startswith("us_dest_dl_text_"):
                await handle_us_dest_download_link_choice(update, context)
            elif data.startswith("us_dest_caption_builder_"):
                await handle_us_dest_caption_builder(update, context)
            elif data.startswith("us_dest_cap_filename_"):
                await handle_us_dest_cap_filename(update, context)
            elif data.startswith("us_dest_cap_filesize_"):
                await handle_us_dest_cap_filesize(update, context)
            elif data.startswith("us_dest_cap_url_label_"):
                await handle_us_dest_cap_url_label(update, context)
            elif data.startswith("us_dest_cap_style_"):
                await handle_us_dest_cap_style(update, context)
            elif data.startswith("us_dest_cap_reset_"):
                await handle_us_dest_cap_reset(update, context)
            elif data.startswith("us_dest_buttons_"):
                if "_edit_" in data:
                    await handle_us_dest_buttons_edit(update, context)
                elif "_clear_" in data:
                    await handle_us_dest_buttons_clear(update, context)
                else:
                    await handle_us_dest_buttons(update, context)
            elif data.startswith("us_dest_caption_"):
                await handle_us_dest_caption_prompt(update, context)
            elif data.startswith("us_dest_remove_confirm_"):
                await handle_us_dest_remove_confirm(update, context)
            elif data.startswith("us_dest_remove_do_"):
                await handle_us_dest_remove_do(update, context)
            elif data.startswith("send_dest_"):
                await handle_send_to_destination(update, context)
            elif data.startswith("fwd_dest_"):
                await handle_forward_to_destination(update, context)
            elif data.startswith("refresh_q_"):
                task_id = data.replace("refresh_q_", "")
                from bot.database import get_task, get_user_position

                task = await get_task(task_id)
                if not task or task.get("status") != "queued":
                    await query.answer(
                        "Your task is no longer in the queue. It may be processing now.",
                        show_alert=True,
                    )
                    return

                pos = await get_user_position(user_id)
                if pos == 0:
                    await query.answer(
                        "Your turn is up! Processing will start momentarily.",
                        show_alert=True,
                    )
                else:
                    await query.answer(
                        f"Your current queue position is {pos + 1}", show_alert=True
                    )

            elif data.startswith("bypass_q_"):
                bypass_url = context.user_data.get("bypass_url")
                if not bypass_url:
                    await query.answer(
                        "Bypass link expired or invalid.", show_alert=True
                    )
                    return

                from bot.database import get_config

                config = await get_config() or {}
                # The user requested "ref teh help text in /admin"
                help_url = config.get(
                    "shorten_help_link",
                    config.get("help_text_url", "https://t.me/bot_paiyan_official"),
                )

                keyboard = [
                    [InlineKeyboardButton("Use me to proceed", url=bypass_url)],
                    [InlineKeyboardButton("How to use ?", url=help_url)],
                ]
                await query.edit_message_text(
                    "🔥 **Queue Bypass Activated**\n\nClick the button below to verify and instantly bypass the processing queue.",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )
            else:
                logger.warning(f"⚠️ UNHANDLED callback: {data}")

    except Exception as e:
        logger.error(f"❌ Error in callback_handler: {e}", exc_info=True)
        await query.answer("❌ Error processing request", show_alert=True)


async def generate_cloud_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str
) -> None:
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

                shortened = await LinkShortener.track_and_shorten(
                    file_id, user_id, raw_cloud_url
                )
                if shortened:
                    display_cloud_url = shortened
            except Exception as e:
                logger.error(f"Failed to apply tracking shortener: {e}")

        keyboard = [
            [
                InlineKeyboardButton("👁️ Watch", url=display_cloud_url),
                InlineKeyboardButton(
                    "📋 Copy Link", callback_data=f"copy_link_{file_id}"
                ),
            ],
            [
                InlineKeyboardButton("🔗 Share", callback_data=f"share_link_{file_id}"),
                InlineKeyboardButton("🔐 Lock", callback_data=f"lock_link_{file_id}"),
            ],
        ]

        link_msg = (
            f"☁️ **Cloud Link Generated**\n\n"
            f"`{display_cloud_url}`\n\n"
            f"⏰ Expires in 7 days\n"
            f"📊 Views: 0"
        )

        if update.message:
            await update.message.reply_text(
                link_msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
        elif update.callback_query:
            await update.callback_query.edit_message_text(
                link_msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )

        logger.info(f"✅ Cloud link generated: {file_id}")
    except Exception as e:
        logger.error(f"❌ Generate cloud link error: {e}")


async def copy_link_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Copy link to clipboard"""
    try:
        await update.callback_query.answer(
            "📋 Link copied to clipboard!", show_alert=True
        )
    except Exception as e:
        logger.error(f"❌ Copy link error: {e}")


async def lock_link_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Lock/unlock cloud link"""
    try:
        callback = update.callback_query
        await callback.answer()
        await callback.edit_message_text(
            "🔒 **Link Locked**\n\nThis link is now private.", parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Lock link error: {e}")


async def handle_us_destination_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Show user destination management menu"""
    try:
        query = update.callback_query
        user_id = update.effective_user.id
        await query.answer()

        # Clear any pending input state
        context.user_data.pop("awaiting", None)
        context.user_data.pop("awaiting_set_at", None)

        from bot.database import get_user_destinations

        destinations = await get_user_destinations(user_id)

        keyboard = []
        # [Add Channel]
        keyboard.append(
            [InlineKeyboardButton("➕ Add Channel", callback_data="us_dest_add")]
        )

        # [Configured Channel List - all clickable to manage]
        if destinations:
            for d in destinations:
                cid = d.get("id", "unknown")
                title = d.get("title") or str(cid)
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"📁 {title}", callback_data=f"us_dest_manage_{cid}"
                        )
                    ]
                )
        else:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "ℹ️ No destinations configured", callback_data="ignore"
                    )
                ]
            )

        # [Back]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="us_settings")])

        await query.message.edit_text(
            f"🎯 **Destination Management**\n\n"
            f"Here you can manage channels where files will be forwarded after processing.\n"
            f"You have `{len(destinations)}` configured.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
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
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"❌ Error in us_dest_add: {e}")


async def handle_us_dest_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage a specific user destination"""
    try:
        logger.info(f"🔔 handle_us_dest_manage called")
        query = update.callback_query
        await query.answer()
        channel_id = int(query.data.replace("us_dest_manage_", ""))
        logger.info(f"🔔 Managing channel_id: {channel_id}")
        user_id = query.from_user.id

        # Clear any pending input state
        context.user_data.pop("awaiting", None)
        context.user_data.pop("awaiting_set_at", None)

        from bot.database import get_user_destinations, get_user, get_config

        destinations = await get_user_destinations(user_id)
        dest = next((d for d in destinations if d.get("id") == channel_id), None)

        if not dest:
            await query.answer("❌ Destination not found", show_alert=True)
            return

        # Get custom metadata for this channel
        user = await get_user(user_id)
        settings = user.get("settings", {})
        dest_metadata = settings.get("destination_metadata", {}).get(
            str(channel_id), {}
        )
        meta_title = dest_metadata.get("title", "Default name")
        meta_author = dest_metadata.get("author", "Default author")
        use_shortener = dest_metadata.get("use_shortener", False)
        download_link_text = dest_metadata.get("download_link_text", "Stream URL")
        caption_footer = dest_metadata.get("caption_footer", "")

        cap_filename_label = dest_metadata.get("cap_filename_label", "File name")
        cap_filesize_label = dest_metadata.get("cap_filesize_label", "File size")
        cap_url_label = dest_metadata.get("cap_url_label", "Stream URL")
        cap_style = dest_metadata.get("cap_style", "none")

        user_plan = user.get("plan", "free")
        plans_config = await get_config("plans") or {}
        plan_data = plans_config.get(user_plan, {})
        shortener_enabled_for_plan = plan_data.get("shortener_allowed", False)
        can_use_shortener = shortener_enabled_for_plan and user_plan != "free"

        title = dest.get("title") or str(channel_id)

        if not can_use_shortener:
            download_status = "⏸️ URL Shortener: Not Available"
        elif use_shortener:
            download_status = f"🔗 URL: {download_link_text}"
        else:
            download_status = "❌ URL Shortener: OFF"

        cap_preview_lines = []
        if cap_style == "bold":
            cap_preview_lines.append(f"<b>{cap_filename_label}: Test Video.mp4</b>")
        elif cap_style == "italic":
            cap_preview_lines.append(f"<i>{cap_filename_label}: Test Video.mp4</i>")
        elif cap_style == "bolditalic":
            cap_preview_lines.append(
                f"<b><i>{cap_filename_label}: Test Video.mp4</i></b>"
            )
        else:
            cap_preview_lines.append(f"{cap_filename_label}: Test Video.mp4")

        cap_preview_lines.append(f"{cap_filesize_label}: 1.2 GB")
        if use_shortener:
            cap_preview_lines.append(f"{cap_url_label}: https://link.com/abc")

        if caption_footer:
            cap_preview_lines.append(f"\n{caption_footer}")

        cap_preview = "\n".join(cap_preview_lines)

        caption_status = (
            f"📋 Footer: {caption_footer[:15]}..."
            if caption_footer
            else "📋 Footer: None"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"📁 {title}", callback_data="ignore")],
                [
                    InlineKeyboardButton(
                        f"📝 Name: {meta_title[:20]}",
                        callback_data=f"us_dest_meta_name_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"👤 Author: {meta_author[:20]}",
                        callback_data=f"us_dest_meta_auth_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        download_status,
                        callback_data=f"us_dest_shortener_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📝 Caption Builder",
                        callback_data=f"us_dest_caption_builder_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔘 Buttons",
                        callback_data=f"us_dest_buttons_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        caption_status,
                        callback_data=f"us_dest_caption_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🗑️ Remove",
                        callback_data=f"us_dest_remove_confirm_{channel_id}",
                    )
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="us_destination")],
            ]
        )

        try:
            await query.message.edit_text(
                f"🎯 **Destination Settings**\n\n"
                f"Channel: `{title}`\n"
                f"ID: `{channel_id}`\n\n"
                f"Preview:\n```{cap_preview}```\n\n"
                f"Configure how files are sent to this channel:",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        except Exception as edit_err:
            logger.warning(f"Edit failed, sending new message: {edit_err}")
            await query.message.reply_text(
                f"🎯 **Destination Settings**\n\n"
                f"Channel: `{title}`\n"
                f"ID: `{channel_id}`\n\n"
                f"Preview:\n```{cap_preview}```\n\n"
                f"Configure how files are sent to this channel:",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"❌ Error in us_dest_manage: {e}", exc_info=True)
        await query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


async def handle_us_dest_remove_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Removal confirmation for user destination"""
    try:
        query = update.callback_query
        await query.answer()
        channel_id = int(query.data.replace("us_dest_remove_confirm_", ""))

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Confirm Removal",
                        callback_data=f"us_dest_remove_do_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel", callback_data=f"us_dest_manage_{channel_id}"
                    )
                ],
            ]
        )
        await query.message.edit_text(
            "⚠️ **Remove Destination?**\n\nFiles will no longer be forwarded to this channel.",
            reply_markup=keyboard,
            parse_mode="Markdown",
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

        await handle_us_destination_button(
            update, context
        )  # Auto callback back to list
    except Exception as e:
        logger.error(f"❌ Error in us_dest_remove_do: {e}")
        await query.answer("❌ Error", show_alert=True)


async def handle_us_dest_meta_name_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Prompt user for custom destination name"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_meta_name_", "")
        context.user_data["awaiting"] = f"us_dest_meta_name_{channel_id}"
        await query.message.reply_text(
            "📝 **Send Custom Name**\n\n"
            "This name will be used as the file title when forwarding to this channel.\n"
            "Send the new name now or /cancel to abort.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_meta_name_prompt: {e}")


async def handle_us_dest_meta_auth_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Prompt user for custom destination author"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_meta_auth_", "")
        context.user_data["awaiting"] = f"us_dest_meta_auth_{channel_id}"
        await query.message.reply_text(
            "👤 **Send Custom Author**\n\n"
            "This author will be set in the file metadata when forwarding to this channel.\n"
            "Send the new author name now or /cancel to abort.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_meta_auth_prompt: {e}")


async def handle_us_dest_meta_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process custom destination metadata input"""
    try:
        user_id = update.effective_user.id
        text = (update.message.text or "").strip()
        awaiting = context.user_data.get("awaiting", "")

        # Fix 4: Enforce length limit on custom destination metadata values
        MAX_META_LEN = 100
        if len(text) > MAX_META_LEN:
            await update.message.reply_text(
                f"❌ **Too Long** — max {MAX_META_LEN} characters.\n\n"
                "Please send a shorter value or /cancel to abort.",
                parse_mode="Markdown",
            )
            return  # Keep awaiting state so user can retry

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
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                f"✅ Custom name updated to: `{text}`",
                parse_mode="Markdown",
            )

        elif awaiting.startswith("us_dest_meta_auth_"):
            channel_id = awaiting.replace("us_dest_meta_auth_", "")
            if channel_id not in dest_metadata:
                dest_metadata[channel_id] = {}
            dest_metadata[channel_id]["author"] = text
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                f"✅ Custom author updated to: `{text}`",
                parse_mode="Markdown",
            )

        elif awaiting.startswith("us_dest_caption_"):
            channel_id = awaiting.replace("us_dest_caption_", "")
            if channel_id not in dest_metadata:
                dest_metadata[channel_id] = {}
            dest_metadata[channel_id]["caption_footer"] = text
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                f"✅ Caption footer updated:\n`{text}`",
                parse_mode="Markdown",
            )

        elif awaiting.startswith("us_dest_dl_custom_"):
            channel_id = awaiting.replace("us_dest_dl_custom_", "")
            if channel_id not in dest_metadata:
                dest_metadata[channel_id] = {}
            dest_metadata[channel_id]["use_shortener"] = True
            dest_metadata[channel_id]["download_link_text"] = text
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                f"✅ Download link text set to:\n`{text}`",
                parse_mode="Markdown",
            )

        elif awaiting.startswith("us_dest_cap_filename_"):
            channel_id = awaiting.replace("us_dest_cap_filename_", "")
            if channel_id not in dest_metadata:
                dest_metadata[channel_id] = {}
            dest_metadata[channel_id]["cap_filename_label"] = text
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                f"✅ Filename label set to:\n`{text}`",
                parse_mode="Markdown",
            )

        elif awaiting.startswith("us_dest_cap_filesize_"):
            channel_id = awaiting.replace("us_dest_cap_filesize_", "")
            if channel_id not in dest_metadata:
                dest_metadata[channel_id] = {}
            dest_metadata[channel_id]["cap_filesize_label"] = text
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                f"✅ Filesize label set to:\n`{text}`",
                parse_mode="Markdown",
            )

        elif awaiting.startswith("us_dest_cap_url_label_"):
            channel_id = awaiting.replace("us_dest_cap_url_label_", "")
            if channel_id not in dest_metadata:
                dest_metadata[channel_id] = {}
            dest_metadata[channel_id]["cap_url_label"] = text
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                f"✅ URL label set to:\n`{text}`",
                parse_mode="Markdown",
            )

        elif awaiting.startswith("us_dest_buttons_"):
            channel_id = awaiting.replace("us_dest_buttons_", "")
            if channel_id not in dest_metadata:
                dest_metadata[channel_id] = {}
            dest_metadata[channel_id]["cap_buttons"] = text
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                "✅ Buttons updated!",
                parse_mode="Markdown",
            )

        settings["destination_metadata"] = dest_metadata
        await update_user(user_id, {"settings": settings})

        context.user_data.pop("awaiting", None)
        context.user_data.pop("awaiting_set_at", None)

    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_meta_input: {e}")


async def handle_us_dest_shortener_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Show download link options for a destination"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_shortener_", "")
        user_id = query.from_user.id

        from bot.database import get_user, get_config

        user = await get_user(user_id)
        settings = user.get("settings", {})
        dest_metadata = settings.get("destination_metadata", {}).get(channel_id, {})
        current = dest_metadata.get("use_shortener", False)

        user_plan = user.get("plan", "free")
        plans_config = await get_config("plans") or {}
        plan_data = plans_config.get(user_plan, {})
        shortener_enabled_for_plan = plan_data.get("shortener_allowed", False)

        if not shortener_enabled_for_plan or user_plan == "free":
            await query.answer(
                "❌ Download links not available for your plan", show_alert=True
            )
            return

        if current:
            dest_metadata["use_shortener"] = False
            if "destination_metadata" not in settings:
                settings["destination_metadata"] = {}
            settings["destination_metadata"][channel_id] = dest_metadata
            from bot.database import update_user

            await update_user(user_id, {"settings": settings})
            await query.answer("❌ Download link disabled", show_alert=True)
            await handle_us_dest_manage(update, context)
            return

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔗 Download Link",
                        callback_data=f"us_dest_dl_text_{channel_id}_🔗 Download Link",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📥 Click to Download",
                        callback_data=f"us_dest_dl_text_{channel_id}_📥 Click to Download",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬇️ Download",
                        callback_data=f"us_dest_dl_text_{channel_id}_⬇️ Download",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔽 Get Link",
                        callback_data=f"us_dest_dl_text_{channel_id}_🔽 Get Link",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "💬 Custom Text...",
                        callback_data=f"us_dest_dl_text_{channel_id}_CUSTOM",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back", callback_data=f"us_dest_manage_{channel_id}"
                    )
                ],
            ]
        )

        await query.message.edit_text(
            "🔗 **How to Download Link?**\n\n"
            "Choose how the download URL appears in your caption.\n\n"
            "**📋 Options:**\n\n"
            "🔗 **Download Link** - Simple and clear\n"
            "📥 **Click to Download** - Action-oriented\n"
            "⬇️ **Download** - Short and direct\n"
            "🔽 **Get Link** - Minimal text\n"
            "💬 **Custom Text...** - Write your own\n\n"
            "**💡 Tip:** The URL label in Caption Builder sets\n"
            "the text BEFORE this link text (e.g., 'Stream URL: 🔗 Download Link')\n\n"
            "Tap an option to select:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_shortener_toggle: {e}")


async def handle_us_dest_download_link_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Handle download link text selection"""
    try:
        query = update.callback_query
        data = query.data.replace("us_dest_dl_text_", "")
        parts = data.split("_", 1)
        channel_id = parts[0]
        link_text = parts[1] if len(parts) > 1 else ""

        if link_text == "CUSTOM":
            context.user_data["awaiting"] = f"us_dest_dl_custom_{channel_id}"
            await query.message.reply_text(
                "✏️ **Custom Download Link Text**\n\n"
                "Send the text you want to use for the download link.\n"
                "Example: `📥 Download File Here`\n\n"
                "Send your custom text or /cancel to abort.",
                parse_mode="Markdown",
            )
            return

        from bot.database import get_user, update_user

        user = await get_user(query.from_user.id)
        settings = user.get("settings", {})
        dest_metadata = settings.get("destination_metadata", {}).get(channel_id, {})
        dest_metadata["use_shortener"] = True
        dest_metadata["download_link_text"] = link_text

        if "destination_metadata" not in settings:
            settings["destination_metadata"] = {}
        settings["destination_metadata"][channel_id] = dest_metadata
        await update_user(query.from_user.id, {"settings": settings})

        await query.answer(f"✅ Download link text set!", show_alert=True)
        await handle_us_dest_manage(update, context)

    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_download_link_choice: {e}")


async def handle_us_dest_caption_builder(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Show caption builder menu"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_caption_builder_", "")
        user_id = query.from_user.id

        from bot.database import get_user

        user = await get_user(user_id)
        settings = user.get("settings", {})
        dest_metadata = settings.get("destination_metadata", {}).get(channel_id, {})

        cap_filename_label = dest_metadata.get("cap_filename_label", "File name")
        cap_filesize_label = dest_metadata.get("cap_filesize_label", "File size")
        cap_url_label = dest_metadata.get("cap_url_label", "Stream URL")
        cap_style = dest_metadata.get("cap_style", "none")

        style_names = {
            "none": "Normal",
            "bold": "Bold",
            "italic": "Italic",
            "bolditalic": "Bold+Italic",
        }
        current_style = style_names.get(cap_style, "Normal")

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"📄 Filename: {cap_filename_label}",
                        callback_data=f"us_dest_cap_filename_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"📦 Filesize: {cap_filesize_label}",
                        callback_data=f"us_dest_cap_filesize_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"🔗 URL Label: {cap_url_label}",
                        callback_data=f"us_dest_cap_url_label_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"✨ Style: {current_style}",
                        callback_data=f"us_dest_cap_style_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔄 Reset to Default",
                        callback_data=f"us_dest_cap_reset_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back", callback_data=f"us_dest_manage_{channel_id}"
                    )
                ],
            ]
        )

        help_text = (
            "📝 **Caption Builder**\n\n"
            "Customize how your file caption looks:\n\n"
            "**How to use each option:**\n\n"
            "📄 **Filename Label**\n"
            "• Set the text shown before the file name\n"
            "• Examples: `Title`, `Movie`, `Episode`, `File`\n"
            "• Tap to change\n\n"
            "📦 **Filesize Label**\n"
            "• Set the text shown before the file size\n"
            "• Examples: `Size`, `GB`, `Duration`, `Length`\n"
            "• Tap to change\n\n"
            "🔗 **URL Label**\n"
            "• Set the text shown before the download link\n"
            "• Examples: `Download`, `Watch`, `Stream`, `Link`\n"
            "• Tap to change\n\n"
            "✨ **Style**\n"
            "• Cycles through: Normal → Bold → Italic → Bold+Italic\n"
            "• Applies HTML formatting to caption lines\n"
            "• HTML tags: `<b>bold</b>`, `<i>italic</i>`\n\n"
            "🔄 **Reset to Default**\n"
            "• Resets all labels to defaults"
        )

        await query.message.edit_text(
            help_text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_caption_builder: {e}")


async def handle_us_dest_cap_filename(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Prompt for filename label"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_cap_filename_", "")
        context.user_data["awaiting"] = f"us_dest_cap_filename_{channel_id}"
        await query.message.reply_text(
            "📄 **Filename Label**\n\n"
            "Set the label shown before the filename.\n"
            "Examples: `File name`, `Title`, `Movie`, `Video`\n\n"
            "Send your label or /cancel to abort.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_cap_filename: {e}")


async def handle_us_dest_cap_filesize(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Prompt for filesize label"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_cap_filesize_", "")
        context.user_data["awaiting"] = f"us_dest_cap_filesize_{channel_id}"
        await query.message.reply_text(
            "📦 **Filesize Label**\n\n"
            "Set the label shown before the file size.\n"
            "Examples: `Size`, `File size`, `GB`, `Duration`\n\n"
            "Send your label or /cancel to abort.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_cap_filesize: {e}")


async def handle_us_dest_cap_url_label(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Prompt for URL label"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_cap_url_label_", "")
        context.user_data["awaiting"] = f"us_dest_cap_url_label_{channel_id}"
        await query.message.reply_text(
            "🔗 **URL Label**\n\n"
            "Set the label shown before the stream/download URL.\n"
            "Examples: `Stream URL`, `Watch`, `Download Link`, `URL`\n\n"
            "Send your label or /cancel to abort.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_cap_url_label: {e}")


async def handle_us_dest_cap_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cycle through caption styles"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_cap_style_", "")
        user_id = query.from_user.id

        from bot.database import get_user, update_user

        user = await get_user(user_id)
        settings = user.get("settings", {})
        dest_metadata = settings.get("destination_metadata", {}).get(channel_id, {})

        current = dest_metadata.get("cap_style", "none")
        styles = ["none", "bold", "italic", "bolditalic"]
        next_idx = (styles.index(current) + 1) % len(styles)
        dest_metadata["cap_style"] = styles[next_idx]

        if "destination_metadata" not in settings:
            settings["destination_metadata"] = {}
        settings["destination_metadata"][channel_id] = dest_metadata
        await update_user(user_id, {"settings": settings})

        await handle_us_dest_caption_builder(update, context)

    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_cap_style: {e}")


async def handle_us_dest_cap_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset caption settings to defaults"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_cap_reset_", "")
        user_id = query.from_user.id

        from bot.database import get_user, update_user

        user = await get_user(user_id)
        settings = user.get("settings", {})
        dest_metadata = settings.get("destination_metadata", {}).get(channel_id, {})

        dest_metadata["cap_filename_label"] = "File name"
        dest_metadata["cap_filesize_label"] = "File size"
        dest_metadata["cap_url_label"] = "Stream URL"
        dest_metadata["cap_style"] = "none"

        settings["destination_metadata"][channel_id] = dest_metadata
        await update_user(user_id, {"settings": settings})

        await query.answer("✅ Caption reset to defaults", show_alert=True)
        await handle_us_dest_caption_builder(update, context)

    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_cap_reset: {e}")


async def handle_us_dest_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show buttons configuration menu"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_buttons_", "")
        user_id = query.from_user.id

        from bot.database import get_user

        user = await get_user(user_id)
        settings = user.get("settings", {})
        dest_metadata = settings.get("destination_metadata", {}).get(channel_id, {})
        cap_buttons = dest_metadata.get("cap_buttons", "")

        if cap_buttons:
            preview = cap_buttons[:100] + ("..." if len(cap_buttons) > 100 else "")
        else:
            preview = "No buttons set"

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✏️ Edit Buttons",
                        callback_data=f"us_dest_buttons_edit_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🗑️ Clear Buttons",
                        callback_data=f"us_dest_buttons_clear_{channel_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back", callback_data=f"us_dest_manage_{channel_id}"
                    )
                ],
            ]
        )

        help_text = (
            "🔘 **Button Configuration**\n\n"
            "Add clickable buttons below your file caption.\n\n"
            "**📋 Format Rules:**\n\n"
            "**Basic Button:**\n"
            "`[Button Text - https://example.com]`\n"
            "Creates one button on its own row.\n\n"
            "**Multiple Buttons (Same Row):**\n"
            "`[Btn1 - url1] | [Btn2 - url2]`\n"
            "Creates two buttons side by side.\n\n"
            "**Multiple Rows:**\n"
            "Each new line = new row of buttons.\n\n"
            "**📝 Example Configuration:**\n"
            "```\n"
            "[Watch Online - https://site.com/watch]\n"
            "[Download - https://site.com/dl] | [Mirror - https://site.com/mirror]\n"
            "[Join Channel - https://t.me/channel]\n"
            "[Subtitles - https://site.com/subs]\n"
            "```\n"
            "This creates 4 buttons in 3 rows.\n\n"
            "**⚠️ Tips:**\n"
            "• Use full URLs starting with `https://`\n"
            "• Keep button text short (max 30 chars)\n"
            "• URLs must be valid and accessible\n\n"
            f"**Current:**\n`{preview}`"
        )

        await query.message.edit_text(
            help_text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_buttons: {e}")


async def handle_us_dest_buttons_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Prompt for buttons configuration"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_buttons_edit_", "")
        context.user_data["awaiting"] = f"us_dest_buttons_{channel_id}"

        from bot.database import get_user

        user = await get_user(query.from_user.id)
        settings = user.get("settings", {})
        dest_metadata = settings.get("destination_metadata", {}).get(channel_id, {})
        current = dest_metadata.get("cap_buttons", "")

        current_text = f"\n\n**Current:**\n`{current}`" if current else ""

        await query.message.reply_text(
            "🔘 **Edit Buttons**\n\n"
            "**📋 How to Create Buttons:**\n\n"
            "**Single Button (one per row):**\n"
            "`[Watch - https://site.com/watch]`\n\n"
            "**Two Buttons Side by Side:**\n"
            "`[Download - https://dl.com] | [Mirror - https://mirror.com]`\n\n"
            "**Three Buttons Side by Side:**\n"
            "`[720p - url] | [1080p - url] | [4K - url]`\n\n"
            "**📝 Example (Copy & Modify):**\n"
            "```\n"
            "[Watch Online - https://stream.com/play]\n"
            "[Download HD - https://dl.com/file] | [Mirror - https://mirror.com]\n"
            "[Subtitles - https://subs.com/eng]\n"
            "```\n\n"
            "**⚠️ Rules:**\n"
            "• URL must start with `https://`\n"
            "• Button text: max 30 characters\n"
            "• Use `|` to put buttons on same row\n"
            "• Each new line = new button row\n\n"
            f"{current_text}\n\n"
            "Send your button configuration or /cancel to abort.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_buttons_edit: {e}")


async def handle_us_dest_buttons_clear(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Clear buttons configuration"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_buttons_clear_", "")
        user_id = query.from_user.id

        from bot.database import get_user, update_user

        user = await get_user(user_id)
        settings = user.get("settings", {})
        dest_metadata = settings.get("destination_metadata", {}).get(channel_id, {})
        dest_metadata["cap_buttons"] = ""

        settings["destination_metadata"][channel_id] = dest_metadata
        await update_user(user_id, {"settings": settings})

        await query.answer("✅ Buttons cleared", show_alert=True)
        await handle_us_dest_manage(update, context)

    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_buttons_clear: {e}")


async def handle_us_dest_caption_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Prompt user for caption footer text"""
    try:
        query = update.callback_query
        channel_id = query.data.replace("us_dest_caption_", "")
        context.user_data["awaiting"] = f"us_dest_caption_{channel_id}"

        user = await get_user(query.from_user.id)
        settings = user.get("settings", {})
        dest_metadata = settings.get("destination_metadata", {}).get(channel_id, {})
        current = dest_metadata.get("caption_footer", "")

        current_text = f"\n\nCurrent: `{current}`" if current else ""

        await query.message.reply_text(
            f"📋 **Set Caption Footer**\n\n"
            f"Add custom text below your file caption.{current_text}\n\n"
            f"**📝 Examples:**\n"
            f"• `Join our channel: @channelname`\n"
            f"• `Support: @admin_handle`\n"
            f"• `Powered by @YourBot`\n"
            f"• `Follow us on Instagram`\n\n"
            f"**💡 Tips:**\n"
            f"• Supports emoji and Telegram formatting\n"
            f"• Max 200 characters\n"
            f"• Will appear on a new line after the URL\n\n"
            f"Send your footer text or /cancel to abort.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_us_dest_caption_prompt: {e}")


async def handle_user_destination_forward(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Handle forwarded channel message for user destination setup"""
    try:
        msg = update.message
        forward_origin = msg.forward_origin
        user_id = update.effective_user.id

        channel_id = None
        channel_title = None

        if forward_origin and hasattr(forward_origin, "chat"):
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
                    f"Tap **Configure** to set up caption options, buttons, and download link settings.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "⚙️ Configure",
                                    callback_data=f"us_dest_manage_{channel_id}",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    "🔙 Back to List", callback_data="us_destination"
                                )
                            ],
                        ]
                    ),
                )
            else:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Failed to add destination. Maybe it's already there?",
                    parse_mode="Markdown",
                )

            context.user_data.pop("awaiting", None)
        else:
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                "❌ Could not read channel from this forward. Please forward a message directly from your channel.",
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"❌ Error in handle_user_destination_forward: {e}")
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            "❌ Error setting destination. Please try again.",
            parse_mode="Markdown",
        )


async def handle_send_to_destination(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Show list of destinations to forward a file to."""
    query = update.callback_query
    await query.answer()
    file_id = query.data.replace("send_dest_", "")
    user_id = query.from_user.id

    from bot.database import get_user_destinations

    destinations = await get_user_destinations(user_id)

    if not destinations:
        await query.message.reply_text(
            "ℹ️ **No Destinations Set!**\n\n"
            "You haven't configured any custom destinations.\n"
            "Please go to `Settings > Custom Destinations` to add one, or use the button below.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "➕ Add Destination", callback_data="us_dest_add"
                        )
                    ]
                ]
            ),
            parse_mode="Markdown",
        )
        return

    context.user_data[f"fwd_file_{file_id[-10:]}"] = file_id

    keyboard = []
    for d in destinations:
        cid = d.get("id")
        title = d.get("title") or str(cid)
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"➡️ {title}", callback_data=f"fwd_dest_{cid}_{file_id[-10:]}"
                )
            ]
        )

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="wiz_cancel")])

    await query.message.reply_text(
        "📤 **Select Destination:**\n\nChoose where to forward this file:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_forward_to_destination(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Forward file to selected destination with permanent link (token-based)"""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    if len(parts) < 4:
        return

    cid = parts[2]
    short_id = parts[3]
    full_file_id = context.user_data.get(f"fwd_file_{short_id}")

    if not full_file_id:
        await query.answer("❌ File reference expired.", show_alert=True)
        return

    user_id = query.from_user.id
    meta = context.user_data.get(f"fwd_meta_{short_id}", {})
    filename = meta.get("filename", "")
    size = meta.get("size", 0)

    # Get destination-specific settings
    from bot.database import get_user, get_config
    from datetime import datetime, timedelta
    import secrets
    import re

    user = await get_user(user_id)
    settings = user.get("settings", {})
    dest_metadata = settings.get("destination_metadata", {}).get(str(cid), {})

    # Check if user can generate links (only paid plans)
    user_plan = user.get("plan", "free")
    plans_config = await get_config("plans") or {}
    plan_data = plans_config.get(user_plan, {})
    can_generate_link = (
        plan_data.get("shortener_allowed", False) and user_plan != "free"
    )

    # Caption builder settings
    custom_title = dest_metadata.get("title", filename) if filename else "File"
    caption_footer = dest_metadata.get("caption_footer", "")
    cap_filename_label = dest_metadata.get("cap_filename_label", "File name")
    cap_filesize_label = dest_metadata.get("cap_filesize_label", "File size")
    cap_url_label = dest_metadata.get("cap_url_label", "Stream URL")
    cap_style = dest_metadata.get("cap_style", "none")
    cap_buttons = dest_metadata.get("cap_buttons", "")

    use_shortener = dest_metadata.get("use_shortener", False) and can_generate_link
    download_link_text = dest_metadata.get("download_link_text", "Stream URL")

    # Build caption with HTML style
    def apply_style(text):
        if cap_style == "bold":
            return f"<b>{text}</b>"
        elif cap_style == "italic":
            return f"<i>{text}</i>"
        elif cap_style == "bolditalic":
            return f"<b><i>{text}</i></b>"
        return text

    caption_lines = []
    caption_lines.append(apply_style(f"{cap_filename_label}: {custom_title}"))
    if size > 0:
        from bot.utils import format_bytes

        caption_lines.append(apply_style(f"{cap_filesize_label}: {format_bytes(size)}"))

    # Parse buttons from cap_buttons
    keyboard_rows = []
    if cap_buttons:
        lines = cap_buttons.strip().split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Check for same-line buttons with |
            if "|" in line:
                parts_btns = line.split("|")
                row = []
                for btn in parts_btns:
                    btn = btn.strip()
                    match = re.match(r"\[(.+?)\s*-\s*(https?://[^\]]+)\]", btn)
                    if match:
                        row.append(
                            InlineKeyboardButton(match.group(1), url=match.group(2))
                        )
                if row:
                    keyboard_rows.append(row)
            else:
                match = re.match(r"\[(.+?)\s*-\s*(https?://[^\]]+)\]", line)
                if match:
                    keyboard_rows.append(
                        [InlineKeyboardButton(match.group(1), url=match.group(2))]
                    )

    try:
        # Build initial caption for sending
        initial_caption = "\n\n".join(caption_lines)

        # STEP 1: Send file to destination
        sent_msg = None
        reply_markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

        try:
            sent_msg = await context.bot.send_document(
                chat_id=cid,
                document=full_file_id,
                caption=initial_caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except Exception:
            try:
                sent_msg = await context.bot.send_video(
                    chat_id=cid,
                    video=full_file_id,
                    caption=initial_caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            except Exception as e:
                await query.message.edit_text(f"❌ Failed to reach destination: {e}")
                return

        # If free user or no shortener enabled, we're done
        if not can_generate_link or not use_shortener:
            if caption_footer:
                caption_lines.append(f"\n{caption_footer}")
                try:
                    await context.bot.edit_message_caption(
                        chat_id=cid,
                        message_id=sent_msg.message_id,
                        caption="\n\n".join(caption_lines),
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning(f"Could not add caption footer: {e}")
            await query.message.edit_text(f"✅ Sent to destination successfully!")
            return

        # STEP 2: Get message_id and create token-based link (paid users only)
        msg_id = sent_msg.message_id if sent_msg else None
        if not msg_id:
            await query.message.edit_text("❌ Failed to get message info")
            return

        dest_token = secrets.token_urlsafe(32)

        from bot.database import get_db

        db = get_db()

        dest_link_doc = {
            "token": dest_token,
            "user_id": user_id,
            "dest_chat_id": str(cid),
            "dest_msg_id": msg_id,
            "file_id": full_file_id,
            "filename": custom_title,
            "created_at": datetime.utcnow(),
            "expires_at": datetime.utcnow() + timedelta(days=365),
            "used": False,
        }
        await db.dest_links.insert_one(dest_link_doc)

        from config.settings import get_settings

        s = get_settings()
        dest_link = f"https://{s.DOMAIN}/dest/{dest_token}"

        # Apply shortener if enabled
        final_link = dest_link
        if use_shortener:
            try:
                from bot.services._link_shortener import LinkShortener

                final_link = await LinkShortener.shorten_url(dest_link)
                if not final_link:
                    final_link = dest_link
            except Exception as e:
                logger.warning(f"Shortener failed: {e}")
                final_link = dest_link

        # Add URL to caption
        caption_lines.append(apply_style(f"{download_link_text}: {final_link}"))
        if caption_footer:
            caption_lines.append(f"\n{caption_footer}")

        final_caption = "\n\n".join(caption_lines)

        # Rebuild keyboard with URL button added
        keyboard_rows.append(
            [InlineKeyboardButton(f"{download_link_text}", url=final_link)]
        )
        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        try:
            await context.bot.edit_message_caption(
                chat_id=cid,
                message_id=msg_id,
                caption=final_caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.warning(f"Could not edit caption to add link: {e}")

        await query.message.edit_text(f"✅ Sent to destination successfully!")

    except Exception as e:
        logger.error(f"Error sending to dest: {e}")
        await query.message.edit_text("❌ Error sending to destination.")

    except Exception as e:
        logger.error(f"Error sending to dest: {e}")
        await query.message.edit_text("❌ Error sending to destination.")


STAGE_INFO = {
    "init": {
        "emoji": "🚀",
        "name": "Initializing",
        "desc": "Setting up your file for processing...",
        "step": 1,
        "total": 5,
    },
    "download": {
        "emoji": "📥",
        "name": "Downloading",
        "desc": "Getting your file from Telegram servers to our server...",
        "step": 2,
        "total": 5,
    },
    "probe": {
        "emoji": "🔍",
        "name": "Analyzing",
        "desc": "Reading video info, audio tracks, subtitles...",
        "step": 3,
        "total": 5,
    },
    "ffmpeg": {
        "emoji": "🎬",
        "name": "Processing",
        "desc": "Applying your choices: subtitles, audio, metadata, watermark...",
        "step": 4,
        "total": 5,
    },
    "upload": {
        "emoji": "📤",
        "name": "Uploading",
        "desc": "Sending your processed file back to you via Telegram...",
        "step": 5,
        "total": 5,
    },
    "complete": {
        "emoji": "✅",
        "name": "Complete",
        "desc": "Your file is ready! Check below 👇",
        "step": 5,
        "total": 5,
    },
    "cloud_upload": {
        "emoji": "☁️",
        "name": "Cloud Upload",
        "desc": "Uploading to cloud storage for large file delivery...",
        "step": 4,
        "total": 5,
    },
    "cloud_wait": {
        "emoji": "⏳",
        "name": "Waiting for Slot",
        "desc": "Cloud servers are busy. Waiting for a free slot...",
        "step": 4,
        "total": 5,
    },
    "rclone": {
        "emoji": "🔄",
        "name": "Cloud Transfer",
        "desc": "Transferring file to cloud storage destination...",
        "step": 4,
        "total": 5,
    },
    "rclone": {
        "emoji": "🔄",
        "name": "Cloud Transfer",
        "desc": "Transferring file to cloud storage...",
        "step": 4,
        "total": 5,
    },
}


def get_progress_bar(progress: int) -> str:
    """Generate a visual progress bar with percentage."""
    if progress < 0:
        progress = 0
    if progress >= 100:
        return "████████████████████ **100%**"

    filled = int(progress / 5)
    empty = 20 - filled
    bar = "█" * filled + "░" * empty
    return f"`{bar}` **{progress}%**"


def format_eta(seconds: int) -> str:
    """Format ETA in human-readable format."""
    if seconds <= 0:
        return "Calculating..."
    if seconds < 60:
        return f"~{int(seconds)}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"~{m}m {s}s"
    h, m = divmod(m, 60)
    return f"~{h}h {m}m"


async def send_progress_message(
    bot,
    user_id,
    task_id,
    filesize,
    stage=None,
    progress=None,
    dump_channel=None,
    start_time=None,
):
    """
    Send or update progress message with user-friendly step indicators.
    """
    import time

    if not hasattr(bot, "progress_data"):
        bot.progress_data = {}
    progress_data = bot.progress_data

    task_info = progress_data.setdefault(task_id, {})
    if stage is not None:
        task_info["stage"] = stage
    if progress is not None:
        task_info["progress"] = progress
    if start_time is not None:
        task_info["start_time"] = start_time
    elif "start_time" not in task_info:
        task_info["start_time"] = time.time()

    current_stage_key = task_info.get("stage", "init")
    current_progress = task_info.get("progress", 0)
    task_start_time = task_info.get("start_time", time.time())

    stage_info = STAGE_INFO.get(current_stage_key, STAGE_INFO["init"])

    elapsed_time = time.time() - task_start_time

    speed_text = ""
    eta_text = ""

    if current_progress > 5 and current_progress <= 100:
        eta_seconds = (elapsed_time / current_progress) * (100 - current_progress)
        eta_text = f"⏳ **ETA:** {format_eta(eta_seconds)}"

        if filesize > 0 and elapsed_time > 0:
            bytes_done = (current_progress / 100) * filesize
            speed_mb = (bytes_done / 1024 / 1024) / elapsed_time
            speed_text = f"⚡ **Speed:** {speed_mb:.1f} MB/s"

    progress_bar = get_progress_bar(current_progress)
    size_text = f"{filesize / (1024 * 1024):.1f} MB" if filesize > 0 else "---"

    step_text = f"**Step {stage_info['step']}/{stage_info['total']}**"

    message_text = (
        f"{stage_info['emoji']} **{stage_info['name']}**\n"
        f"_{stage_info['desc']}_\n\n"
        f"{step_text}\n\n"
        f"{progress_bar}\n\n"
        f"📦 **Size:** {size_text}\n"
    )

    if speed_text:
        message_text += f"{speed_text}\n"
    if eta_text:
        message_text += f"{eta_text}\n"

    keyboard = [
        [
            InlineKeyboardButton(
                "🔄 Refresh", callback_data=f"refresh_progress_{task_id}"
            )
        ]
    ]

    if current_progress >= 100 or current_stage_key == "complete":
        message_text += "\n\n✅ **Processing Complete!** 🎉"
        keyboard = []

    try:
        if "user_progress_msg_id" not in task_info:
            msg = await bot.send_message(
                chat_id=user_id,
                text=message_text,
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
                parse_mode="Markdown",
            )
            task_info["user_progress_msg_id"] = msg.message_id
            task_info["user_id"] = user_id
        else:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=task_info["user_progress_msg_id"],
                text=message_text,
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
                parse_mode="Markdown",
            )

        # ——— Update Dump Channel (optional) ———
        if dump_channel:
            dump_text = f"📤 **DUMP LOG**\n{message_text}"
            if "dump_progress_msg_id" not in task_info:
                msg = await bot.send_message(
                    chat_id=dump_channel, text=dump_text, parse_mode="Markdown"
                )
                task_info["dump_progress_msg_id"] = msg.message_id
            else:
                await bot.edit_message_text(
                    chat_id=dump_channel,
                    message_id=task_info["dump_progress_msg_id"],
                    text=dump_text,
                    parse_mode="Markdown",
                )

        # Save state
        bot.progress_data[task_id] = task_info

        if current_progress >= 100:
            logger.info(f"Task {task_id} completed — deleting progress message 🗑️")
            try:
                await bot.delete_message(
                    chat_id=user_id, message_id=task_info["user_progress_msg_id"]
                )
                # Also delete dump progress if exists
                if "dump_progress_msg_id" in task_info:
                    await bot.delete_message(
                        chat_id=dump_channel,
                        message_id=task_info["dump_progress_msg_id"],
                    )
            except Exception as e:
                logger.warning(f"Failed to delete progress message: {e}")

            # Clean up memory
            if task_id in bot.progress_data:
                del bot.progress_data[task_id]
            return

    except Exception as e:
        logger.error(
            f"send_progress_message failed for task {task_id}: {e}", exc_info=True
        )
        # Silent fallback — never crash the bot
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"⚠️ Warning: Progress update failed — but your file is still processing!\n🆔 Task: `{task_id}`",
                parse_mode="Markdown",
            )
        except:
            pass


async def finalize_progress(
    bot, task_id, success=True, result_text="", reply_markup=None
):
    """Finalize progress tracking and clean up session."""
    try:
        from bot.database import update_task

        status = "completed" if success else "failed"
        await update_task(task_id, {"status": status, "result": result_text})

        # We need user_id to clear session. Usually task has it.
        from bot.database import get_task

        task = await get_task(task_id)
        if task:
            user_id = task.get("user_id")
            # We'll rely on the caller or a helper to clear context.user_data
            # since we don't have 'context' here.
            # But we can at least log it.
            logger.info(f"✅ Task {task_id} finalized as {status}")

    except Exception as e:
        logger.error(f"Error finalizing progress: {e}")


async def clear_user_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wipe all temporary user state to prevent session leak/spam."""
    if not context or not context.user_data:
        return

    if update and update.effective_user:
        user_id = update.effective_user.id
    else:
        user_id = "unknown"
    keys_to_clear = [
        "awaiting",
        "wizard",
        "bypass_url",
        "queued_task",
        "processing_lock",
        "current_task_id",
        "awaiting_channel_type",
    ]
    # Also clear any dynamic 'rclone_' or 'wiz_' keys
    dynamic_keys = [
        k for k in context.user_data if k.startswith(("rclone_", "wiz_", "edit_"))
    ]
    keys_to_clear.extend(dynamic_keys)

    count = 0
    for key in keys_to_clear:
        if context.user_data.pop(key, None) is not None:
            count += 1

    if count > 0:
        logger.info(f"🧹 Session cleared for {user_id} ({count} keys removed)")


async def handle_check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback for '🔍 Check Status' button. Retriggers force-sub check & refreshes UI."""
    query = update.callback_query
    await query.answer("🔄 Refreshing status...")

    # Rerun check_force_sub. This will automatically edit the message
    # since we have fsub_msg_id in user_data.
    if await check_force_sub(update, context):
        # Verified — show success message
        try:
            await query.edit_message_text(
                "✅ **Verified!**\n\nProcessing your request...",
                parse_mode="Markdown",
            )
        except Exception:
            pass

        # Auto-vanish after 3 seconds
        async def vanish_message():
            await asyncio.sleep(3)
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=query.message.message_id,
                )
            except Exception:
                pass

        asyncio.create_task(vanish_message())

        # RESUME LOGIC
        pending_queue = context.user_data.pop("pending_fsub_data", [])
        if not isinstance(pending_queue, list):
            pending_queue = [pending_queue] if pending_queue else []

        context.user_data.pop("fsub_msg_id", None)

        if pending_queue:
            logger.info(
                f"🔄 Resuming {len(pending_queue)} pending tasks for {update.effective_user.id}"
            )
            for pending in pending_queue:
                if pending["type"] == "url":
                    from bot.handlers.files import handle_url_input

                    await handle_url_input(update, context, resumed_url=pending["url"])
                elif pending["type"] == "file":
                    from bot.handlers.files import handle_file_upload

                    await handle_file_upload(
                        update,
                        context,
                        resumed_file=True,
                        file_id=pending.get("file_id"),
                    )
                await asyncio.sleep(0.5)
        else:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="✅ **All Set!**\n\nSend me a file or link to get started.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    else:
        # Still not joined/requested — check_force_sub already edited the message in‑place.
        pass


async def finalize_progress(
    bot, task_id, success=True, result_text="", reply_markup=None
):
    """Finalize progress tracking and clean up session, notifying both User and Dump."""
    try:
        from bot.database import update_task

        status = "completed" if success else "failed"
        await update_task(task_id, {"status": status, "result": result_text})

        progress_data = getattr(bot, "progress_data", {})
        if task_id not in progress_data:
            return

        progress_info = progress_data[task_id]

        # 1. Update User PM
        user_id = progress_info.get("user_id")
        user_msg_id = progress_info.get("progress_msg_id") or progress_info.get(
            "user_progress_msg_id"
        )

        if user_id and user_msg_id:
            try:
                final_text = (
                    f"✅ **Processing Complete!**\n\n`{result_text}`"
                    if success
                    else f"❌ **Processing Failed**\n\n`{result_text}`"
                )
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=user_msg_id,
                    text=final_text,
                    reply_markup=reply_markup,  # Erases buttons by default if reply_markup is None
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning(f"Failed to edit user progress message: {e}")

        # 2. Update Dump Channel
        dump_ch = progress_info.get("dump_channel")
        dump_msg_id = progress_info.get("dump_progress_msg_id")

        if dump_ch and dump_msg_id:
            try:
                final_dump_text = (
                    "✅ **Processing Complete**\n\nFile processed successfully!"
                    if success
                    else "❌ **Processing Failed**\n\nCheck user PM for details."
                )
                await bot.edit_message_text(
                    chat_id=dump_ch,
                    message_id=dump_msg_id,
                    text=final_dump_text,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning(f"Failed to edit dump progress message: {e}")

        # Cleanup Memory
        if task_id in bot.progress_data:
            del bot.progress_data[task_id]
        logger.info(f"✅ Task {task_id} finalized as {status}")

    except Exception as e:
        logger.error(f"Error finalizing progress: {e}")


@rate_limit
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle general text input based on awaiting state"""
    import time as _time

    try:
        user_id = update.effective_user.id
        text = (update.message.text or "").strip()
        awaiting = context.user_data.get("awaiting")

        # UI CLEANUP: Delete previous prompt if it exists
        prompt_msg_id = context.user_data.pop("prompt_msg_id", None)
        if prompt_msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=user_id, message_id=prompt_msg_id
                )
            except Exception as e:
                logger.debug(f"Prompt delete skipped: {e}")

        if not awaiting:
            # If no awaiting state, check if it's a URL
            from bot.utils import send_auto_delete_msg, validate_url

            if validate_url(text)[0]:
                if not await check_force_sub(
                    update, context, pending_data={"type": "url", "url": text}
                ):
                    return
                from bot.handlers.files import handle_url_input

                await handle_url_input(update, context)
            return

        # Fix 7: Check awaiting state TTL (30 minutes)
        AWAITING_TTL = 1800  # 30 minutes in seconds
        awaiting_set_at = context.user_data.get("awaiting_set_at", 0)
        if awaiting_set_at and (_time.time() - awaiting_set_at) > AWAITING_TTL:
            logger.info(f"⏰ Awaiting state '{awaiting}' expired for user {user_id}")
            context.user_data.pop("awaiting", None)
            context.user_data.pop("awaiting_set_at", None)
            await update.message.reply_text(
                "⏰ **Session Expired**\n\nYour previous action timed out after 30 minutes.\n"
                "Please start again from the menu.",
                parse_mode="Markdown",
            )
            return

        logger.info(f"📩 Text input from {user_id} in state: {awaiting}")

        # Routing logic
        if awaiting == "channel_forward":
            from bot.handlers.admin import handle_admin_forwards

            await handle_admin_forwards(update, context)
            return

        if awaiting.startswith("us_dest_meta_name_") or awaiting.startswith(
            "us_dest_meta_auth_"
        ):
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
            from bot.handlers.settings import handle_config_edit_input

            await handle_config_edit_input(update, context, awaiting)
            return

        if awaiting.startswith("us_rclone_"):
            from bot.handlers.settings import handle_user_rclone_setup_step

            await handle_user_rclone_setup_step(update, context)
            return

        if awaiting == "wiz_rename" or (awaiting and awaiting.startswith("wiz_meta_")):
            from bot.handlers.files import handle_wizard_text_input

            await handle_wizard_text_input(update, context, text)
            return

        # Fallback for unexpected states
        logger.warning(f"⚠️ Unhandled text input state: {awaiting}")

        if awaiting == "support_message":
            from bot.database import add_chatbox_message

            success = await add_chatbox_message(user_id, text, sender_type="user")
            if success:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "✅ **Message Sent!**\n\nThe admin has been notified. Please wait for a reply.",
                    parse_mode="Markdown",
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
                            parse_mode="Markdown",
                        )
                    except:
                        pass
            else:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Failed to send message. Please try again later.",
                    parse_mode="Markdown",
                )
            return

    except Exception as e:
        logger.error(f"❌ Error in handle_text_input: {e}", exc_info=True)
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            f"❌ **Error Processing Input**\n\n{str(e)[:100]}",
            parse_mode="Markdown",
        )
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


async def check_force_sub(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pending_data: dict = None
) -> bool:
    """Check if user has joined all required force-subscription channels.
    Returns True if user can proceed, False if channels must be joined first.
    If False, saves pending_data to resume later.
    """
    try:
        from bot.database import get_force_sub_channels

        force_channels = await get_force_sub_channels()

        if not force_channels:
            logger.info("FSub: No force channels configured, allowing user")
            return True

        user_id = update.effective_user.id
        from bot.database import get_user

        user = await get_user(user_id)
        requested = user.get("requested_fsub", []) if user else []

        logger.info(
            f"FSub: Checking {len(force_channels)} channels for user {user_id}: {force_channels}"
        )

        not_joined = []

        for channel in force_channels:
            channel_id = channel.get("id")
            if not channel_id:
                logger.warning("FSub: Channel missing ID, skipping")
                continue

            # 0. Verify bot is admin in the channel
            try:
                bot_member = await context.bot.get_chat_member(
                    channel_id, context.bot.id
                )
                if bot_member.status not in ["administrator", "creator"]:
                    logger.warning(f"FSub: Bot not admin in {channel_id}, skipping")
                    # DON'T SKIP - still show button
                else:
                    logger.info(f"FSub: Bot IS admin in {channel_id}")
            except TelegramError as e:
                logger.warning(f"FSub: Cannot check bot admin for {channel_id}: {e}")
                # DON'T SKIP - still show button

            # 1. Check if user is member
            is_member = False
            is_requested = False
            try:
                member = await context.bot.get_chat_member(channel_id, user_id)
                if member.status in ["member", "administrator", "creator"]:
                    is_member = True
                    logger.info(f"FSub: User {user_id} IS member of {channel_id}")
                elif member.status == "left":
                    logger.info(f"FSub: User {user_id} has LEFT {channel_id}")
                elif member.status == "kicked":
                    logger.info(f"FSub: User {user_id} is BANNED in {channel_id}")
                else:
                    logger.info(
                        f"FSub: User {user_id} status in {channel_id}: {member.status}"
                    )
            except TelegramError as e:
                logger.warning(
                    f"FSub: Cannot check user membership for {channel_id}: {e}"
                )

            # 2. Check if user requested (for req_join channels)
            req_join = channel.get("metadata", {}).get("req_join", False)

            # Check both DB record and Telegram API for pending requests
            is_requested = int(channel_id) in [int(c) for c in requested]

            # Also check via Telegram API for pending join requests
            if req_join and not is_member:
                try:
                    from telegram.error import BadRequest

                    # Try to get chat and check for pending requests
                    chat = await context.bot.get_chat(channel_id)
                    # If we can get chat info, the bot has access
                    logger.info(
                        f"FSub: Bot has access to {channel_id} for req_join check"
                    )
                except TelegramError as e:
                    logger.warning(
                        f"FSub: Cannot access {channel_id} for req_join: {e}"
                    )

            # Allow if: member OR (requested for req_join mode)
            if is_member or (is_requested and req_join):
                logger.info(
                    f"FSub: User {user_id} allowed via member={is_member} or requested={is_requested}"
                )
                continue

            not_joined.append(channel)

        if not_joined:
            # SAVE FOR RESUME (List-based queue to prevent overwrite)
            if pending_data:
                if "pending_fsub_data" not in context.user_data:
                    context.user_data["pending_fsub_data"] = []

                # Check for duplicates in queue
                queue = context.user_data["pending_fsub_data"]
                if not isinstance(queue, list):
                    queue = [queue]

                if pending_data not in queue:
                    queue.append(pending_data)
                    context.user_data["pending_fsub_data"] = queue
                    logger.info(
                        f"💾 Added task to FSub queue for {user_id} (Total: {len(queue)})"
                    )

            keyboard = []

            for channel in not_joined:
                if isinstance(channel, dict):
                    channel_id = channel.get("id")
                    metadata = channel.get("metadata", {})
                    channel_name = (
                        metadata.get("title") or channel.get("name") or "Channel"
                    )
                    req_join = metadata.get("req_join", False)
                    invite_link = channel.get("invite_link", "")
                else:
                    channel_id = channel
                    channel_name = "Channel"
                    req_join = False
                    invite_link = ""

                if not invite_link:
                    try:
                        # First try to export existing link
                        try:
                            link = await context.bot.export_chat_invite_link(channel_id)
                            invite_link = link
                        except Exception:
                            # If export fails, create new link
                            if req_join:
                                # For req_join: create link that creates join request
                                link = await context.bot.create_chat_invite_link(
                                    channel_id,
                                    creates_join_request=True,
                                    name=f"FSub_{user_id}",
                                )
                            else:
                                # For normal join: create unlimited link
                                link = await context.bot.create_chat_invite_link(
                                    channel_id,
                                    name=f"FSub_{user_id}",
                                    member_limit=0,
                                )
                            invite_link = link.invite_link
                    except TelegramError as e:
                        logger.warning(f"Cannot create link for {channel_id}: {e}")
                        invite_link = None

                if invite_link:
                    label = (
                        f"✨ Request to Join {channel_name}"
                        if req_join
                        else f"✨ Join {channel_name}"
                    )
                    keyboard.append([InlineKeyboardButton(label, url=invite_link)])
                else:
                    # Show button anyway with join chat link
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                f"✨ {channel_name}",
                                url=f"https://t.me/c/{str(channel_id).replace('-100', '')}/1",
                            )
                        ]
                    )

            keyboard.append(
                [
                    InlineKeyboardButton(
                        "✅ I'm Joined, Continue...", callback_data="check_subscription"
                    )
                ]
            )

            if keyboard:
                # Build channel list text
                channel_list = "\n".join(
                    [
                        f"  • {ch.get('metadata', {}).get('title') or 'Channel'}"
                        for ch in not_joined
                    ]
                )

                fsub_text = (
                    "📢 **Subscription Required**\n\n"
                    "You must join the following channels first:\n\n"
                    f"{channel_list}\n\n"
                    "👇 Tap each button above to join, then click **Check Status**"
                )
                reply_markup = InlineKeyboardMarkup(keyboard)

                # Try to EDIT the existing fsub message
                existing_msg_id = context.user_data.get("fsub_msg_id")
                edited = False

                if existing_msg_id:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=user_id,
                            message_id=existing_msg_id,
                            text=fsub_text,
                            reply_markup=reply_markup,
                            parse_mode="Markdown",
                        )
                        edited = True
                    except Exception:
                        pass

                if not edited:
                    msg = None
                    if update.message:
                        msg = update.message
                    elif update.callback_query:
                        msg = update.callback_query.message

                    if msg:
                        sent = await msg.reply_text(
                            fsub_text, reply_markup=reply_markup, parse_mode="Markdown"
                        )
                        context.user_data["fsub_msg_id"] = sent.message_id

            return False

        return True

    except Exception as e:
        logger.error(f"Error in force sub check: {str(e)}")
        return True


async def handle_chat_join_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Track join requests in DB and revoke the unique link used. Auto-approve logic."""
    try:
        request = update.chat_join_request
        user_id = request.from_user.id
        chat_id = request.chat.id
        invite_link_obj = request.invite_link
        invite_link_str = invite_link_obj.invite_link if invite_link_obj else None

        logger.info(f"👋 Join request from {user_id} for chat {chat_id}")

        # Track that this user has a pending join request
        from bot.database import get_user, update_user

        user = await get_user(user_id)
        requested = user.get("requested_fsub", [])
        if int(chat_id) not in [int(c) for c in requested]:
            requested.append(int(chat_id))
            await update_user(user_id, {"requested_fsub": requested})
            logger.info(
                f"📝 User {user_id} added to requested_fsub for channel {chat_id}"
            )

        # Revoke the invite link used if it was a custom one-time link
        if invite_link_str:
            try:
                await context.bot.revoke_chat_invite_link(chat_id, invite_link_str)
                logger.info(
                    f"🗑️ Revoked invite link {invite_link_str} after request from user {user_id}"
                )
            except Exception as e:
                logger.warning(f"Could not revoke link {invite_link_str}: {e}")

        # AUTO-RESUME LOGIC: Check if all FS requirements are now met (joined or requested)
        if await check_force_sub(update, context):
            # All requirements satisfied!

            # 1. DELETE the force-sub prompt message if it exists
            fsub_msg_id = context.user_data.pop("fsub_msg_id", None)
            if fsub_msg_id:
                try:
                    await context.bot.delete_message(
                        chat_id=user_id, message_id=fsub_msg_id
                    )
                except:
                    pass

            # 2. RESUME pending tasks
            pending_queue = context.user_data.pop("pending_fsub_data", [])
            if not isinstance(pending_queue, list):
                pending_queue = [pending_queue] if pending_queue else []

            if pending_queue:
                logger.info(
                    f"🚀 Auto-resuming {len(pending_queue)} tasks for {user_id} after join request approval"
                )
                for pending in pending_queue:
                    if pending["type"] == "url":
                        from bot.handlers.files import handle_url_input

                        await handle_url_input(
                            update, context, resumed_url=pending["url"]
                        )
                    elif pending["type"] == "file":
                        from bot.handlers.files import handle_file_upload

                        await handle_file_upload(
                            update,
                            context,
                            resumed_file=True,
                            file_id=pending.get("file_id"),
                        )
                    await asyncio.sleep(0.5)
            else:
                # Notify the user they are cleared
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="✅ **Force Subscribe Released!**\n\nYou have requested to join all required channels. You can now use the bot freely.",
                        parse_mode="Markdown",
                    )
                except:
                    pass
        else:
            # Not all joined yet — notify them about this specific request
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="✅ **Join Request Received!**\n\nYour request has been logged. Please make sure you have requested to join **ALL** required channels listed in the bot.",
                    parse_mode="Markdown",
                )
            except:
                pass

    except Exception as e:
        logger.error(f"❌ Error in handle_chat_join_request: {e}", exc_info=True)


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
                    logger.info(
                        f"🆕 New user registered via /start id: {user_id} ({first_name})"
                    )
                    await log_user_update(
                        context.bot, user_id, "registered via start ID"
                    )

                await update.message.reply_text(f"`{user_id}`", parse_mode="Markdown")
                return
            elif arg.startswith("bypass_"):
                from bot.database import get_db

                db = get_db()
                bypass_token = arg
                task = await db.tasks.find_one_and_update(
                    {"wizard_bypass_token": bypass_token, "status": "queued"},
                    {"$set": {"priority": 100, "wizard_bypass_token": None}},
                )
                if task:
                    await update.message.reply_text(
                        "🚀 **Queue Bypassed!**\n\nYour file has been moved to the front of the queue and will begin processing momentarily.",
                        parse_mode="Markdown",
                    )
                else:
                    await send_auto_delete_msg(
                        context.bot,
                        update.effective_chat.id,
                        "❌ **Invalid or Expired Token**\n\nThis bypass link is no longer valid or has already been used.",
                        parse_mode="Markdown",
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
                InlineKeyboardButton("📊 Stats", callback_data="us_stats"),
            ],
            [
                InlineKeyboardButton("📂 My Files", callback_data="us_myfiles"),
                InlineKeyboardButton("💬 Support", callback_data="us_support"),
            ],
            [InlineKeyboardButton("📚 Help Guide", callback_data="us_help")],
        ]

        await update.message.reply_text(
            WELCOME_MSG,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
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
                parse_mode="Markdown",
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
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            "❌ Unable to load support info. Please try again.",
            parse_mode="Markdown",
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel — context-aware cancel of current awaiting state"""
    try:
        user_id = update.effective_user.id
        text = update.message.text.strip()

        # Handle /cancelTask_ID or /cancel Task_ID
        task_id = None
        if text.lower().startswith("/cancel") and len(text) > 7:
            # Extract Task_ID (could be /cancelTask_ID or /cancel Task_ID)
            raw_id = text[7:].strip()
            if raw_id:
                task_id = raw_id

        if task_id:
            from bot.database import get_task, fail_task

            task = await get_task(task_id)

            if not task:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    f"❌ **Task Not Found**\n\nTask ID: `{task_id}`",
                    parse_mode="Markdown",
                )
                return

            # Check ownership (unless admin)
            from config.settings import get_admin_ids

            if task.get("user_id") != user_id and user_id not in get_admin_ids():
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ **Access Denied**\n\nYou can only cancel your own tasks.",
                    parse_mode="Markdown",
                )
                return

            if task.get("status") in ["completed", "failed"]:
                await update.message.reply_text(
                    f"ℹ️ **Task Already Finished**\n\nStatus: `{task.get('status')}`",
                    parse_mode="Markdown",
                )
                return

            # Mark as failed/cancelled
            await fail_task(task_id, "Cancelled by user")

            # TODO: Signal the worker if it's currently processing
            # For now, worker will check status periodically or fail on next step

            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                f"✅ **Task Cancelled**\n\nTask `{task_id}` has been marked as cancelled.",
                parse_mode="Markdown",
            )
            logger.info(f"🛑 Task {task_id} cancelled by user {user_id}")
            return

        # Map every awaiting-state key → (what was being done, where to go back)
        STATE_MESSAGES = {
            "us_prefix": ("✏️ Prefix edit", "/ussettings"),
            "us_suffix": ("✏️ Suffix edit", "/ussettings"),
            "us_remove_word": ("🗑️ Word removal", "/ussettings"),
            "us_meta_author": ("🏷️ Metadata author edit", "/ussettings"),
            "us_meta_subs": ("🎞️ Subtitle injection", "/ussettings"),
            "us_destination": ("🎯 Destination channel setup", "/ussettings"),
            "wiz_inject_audio": ("🎵 Audio injection wizard", "/ussettings"),
            "wiz_inject_subs": ("🎞️ Subtitle injection wizard", "/ussettings"),
            "wiz_rename": ("✏️ Rename wizard", "/ussettings"),
            "broadcast_message": ("📢 Broadcast compose", "/admin → Broadcast"),
            "admin_find_user": ("🔍 Find user", "/admin → Users"),
            "admin_ban_user": ("🔨 Ban user", "/admin → Users"),
            "admin_unban_user": ("🔓 Unban user", "/admin → Users"),
            "admin_upgrade_user": ("⬆️ Upgrade user", "/admin → Users"),
            "edit_start_msg": ("💬 Start message edit", "/admin → Config"),
            "edit_watermark": ("💦 Watermark edit", "/admin → Config"),
            "edit_contact": ("☎️ Support contact edit", "/admin → Config"),
            "edit_help_text": ("📖 Help text edit", "/admin → Config"),
            "edit_site_name": ("🏢 Site name edit", "/admin → Config"),
            "edit_site_desc": ("📝 Description edit", "/admin → Config"),
            "edit_support_channel": ("🔗 Support channel edit", "/admin → Config"),
            "edit_parallel": ("⚡ Parallel limit edit", "/admin → Config"),
            "edit_max_filesize": ("📦 Max file size edit", "/admin → Config"),
            "edit_file_expiry": ("📅 File expiry edit", "/admin → Config"),
            "add_shortener": ("🔗 Shortener setup", "/admin → Shorteners"),
        }

        awaiting = context.user_data.get("awaiting") or context.user_data.get(
            "awaiting_channel_type"
        )

        if awaiting and awaiting in STATE_MESSAGES:
            what, where = STATE_MESSAGES[awaiting]
            msg = (
                f"✅ The **{what}** has been cancelled.\n\n"
                f"Nothing was saved.\n\n"
                f"Return via {where}."
            )
        elif awaiting:
            msg = (
                "✅ The **current operation** has been cancelled.\n\nNothing was saved."
            )
        else:
            msg = (
                "ℹ️ **Nothing to cancel.**\n\n"
                "You are not in the middle of any setup.\n\n"
                "**Quick links:**\n"
                "• /start — Home\n"
                "• /ussettings — Your settings\n"
                "• /myfiles — Your files\n"
                "• `/cancelTask_ID` — Cancel a running task"
            )

        # Clear all awaiting / wizard / rclone state keys
        # Clear state using helper
        await clear_user_session(update, context)

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
                parse_mode="Markdown",
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
                parse_mode="Markdown",
            )
            return

        status = task.get("status", "unknown")
        if status in ("completed", "failed", "cancelled"):
            await update.message.reply_text(
                f"ℹ️ **Task Already {status.title()}**\n\n"
                f"`{task_id}` is already `{status}` — no action needed.",
                parse_mode="Markdown",
            )
            return

        # Write cancellation flag to DB
        db = context.application.bot_data.get("db")
        if db is not None:
            await db.tasks.update_one(
                {"task_id": task_id, "user_id": user_id},
                {"$set": {"status": "cancelled"}},
            )

        filename = task.get("filename", task_id)
        await update.message.reply_text(
            f"✅ **Task Cancelled**\n\n"
            f"📄 File: `{filename}`\n"
            f"🆔 ID: `{task_id}`\n\n"
            f"If the task is actively processing it will stop at the next safe checkpoint.",
            parse_mode="Markdown",
        )
        logger.info(f"✅ Task {task_id} cancelled by user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in cancel_task_command: {e}", exc_info=True)
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            "❌ Could not cancel the task. Please try again.",
            parse_mode="Markdown",
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
            available_commands.extend(
                [
                    "/admin - Open admin panel",
                    "/rclone - Configure rclone",
                    "/terabox - Configure terabox",
                ]
            )

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

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📢 Join Updates Channel", url=updates_url)]]
        )

        await update.message.reply_text(
            f"❓ **Unknown command:** `{command}`\n\n"
            "I'm sorry, I don't recognize that command. "
            "Here are the available commands:\n\n"
            f"{commands_list}",
            reply_markup=keyboard,
            parse_mode="Markdown",
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
        contact_details = (
            config.get("contact_details", "No contact details available")
            if config
            else "No contact details available"
        )

        keyboard = []
        if support_channel:
            keyboard.append(
                [InlineKeyboardButton("💬 Get Support (Channel)", url=support_channel)]
            )

        keyboard.append(
            [
                InlineKeyboardButton(
                    "💬 Bot Support (Chat)", callback_data="start_support_chat"
                )
            ]
        )

        await update.message.reply_text(
            f"💁 **Need Help?**\n\n{contact_details}\n\n"
            f"Click below to get support or chat with us directly:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

        await log_info(f"✅ /support used by {update.effective_user.id}")

    except Exception as e:
        logger.error(f"Error in support command: {e}", exc_info=True)
        await log_error(f"❌ Error in support command: {str(e)}")
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            "❌ Unable to load support info. Please try again.",
            parse_mode="Markdown",
        )


async def handle_subtitle_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
            "📖 **Subtitle Options**\n\nChoose how to handle subtitles:",
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
            await update.callback_query.answer(
                "✅ Subtitles will be injected", show_alert=True
            )
            logger.info(f"Subtitles injection enabled for user {user_id}")
        else:
            await update.callback_query.answer("❌ User not found", show_alert=True)

    except Exception as e:
        logger.error(f"❌ Error in handle_inject_sub: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


async def handle_us_remove_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
                InlineKeyboardButton(
                    "✅ Yes, Reset All", callback_data="us_reset_confirm_yes"
                ),
                InlineKeyboardButton("❌ Cancel", callback_data="us_remove"),
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
            parse_mode="Markdown",
        )

        logger.info(f"✅ Reset confirmation shown for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_us_remove_confirm: {e}", exc_info=True)
        await query.answer("❌ Error", show_alert=True)


async def handle_us_reset_confirm_yes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
            "thumbnail": "auto",
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


async def handle_meta_author(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        msg = await query.message.reply_text(
            "👤 **Set Global Author / Artist**\n\nSend the text you want as the artist/author tag for all files:",
            parse_mode="Markdown",
        )
        context.user_data["prompt_msg_id"] = msg.message_id
        context.user_data["awaiting"] = "us_meta_author"

    except Exception as e:
        logger.error(f"❌ Error in handle_meta_author: {e}", exc_info=True)


async def handle_meta_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        msg = await query.message.reply_text(
            "🎬 **Set Video Title**\n\nSend the title you want for the video track:",
            parse_mode="Markdown",
        )
        context.user_data["prompt_msg_id"] = msg.message_id
        context.user_data["awaiting"] = "us_meta_video"

    except Exception as e:
        logger.error(f"❌ Error in handle_meta_video: {e}", exc_info=True)


async def handle_meta_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        msg = await query.message.reply_text(
            "🎵 **Set Audio Label**\n\nSend the text you want for audio tracks.\nResult will be: `[Text] | [Language]`",
            parse_mode="Markdown",
        )
        context.user_data["prompt_msg_id"] = msg.message_id
        context.user_data["awaiting"] = "us_meta_audio"

    except Exception as e:
        logger.error(f"❌ Error in handle_meta_audio: {e}", exc_info=True)


async def handle_meta_subs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        msg = await query.message.reply_text(
            "📝 **Set Subtitle Label**\n\nSend the text you want for subtitle tracks.\nResult will be: `[Text] | [Language]`",
            parse_mode="Markdown",
        )
        context.user_data["prompt_msg_id"] = msg.message_id
        context.user_data["awaiting"] = "us_meta_subs"

    except Exception as e:
        logger.error(f"❌ Error in handle_meta_subs: {e}", exc_info=True)

        logger.info(f"✅ Audio metadata menu opened for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_meta_audio: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_us_visibility(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
                parse_mode="Markdown",
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

        storage_limit_gb = storage_limit / (1024**3)
        used_storage_gb = used_storage / (1024**3)
        percentage = (
            (used_storage_gb / storage_limit_gb * 100) if storage_limit_gb > 0 else 0
        )

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
            [
                InlineKeyboardButton(
                    "❌ Reset All Settings", callback_data="us_remove_confirm"
                )
            ],
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


async def handle_us_rclone_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: prompt user for GDrive API credentials to create a service"""
    try:
        query = update.callback_query
        await query.answer()

        text = (
            "🛠️ **Create Rclone Service (GDrive)**\n\n"
            "To use your own Google Drive service, please provide your API credentials in this format:\n\n"
            "`Client_ID | Client_Secret | Refresh_Token` \n\n"
            "**How to get these?**\n"
            "1. Go to [Google Cloud Console](https://console.cloud.google.com/)\n"
            "2. Create a project and 'OAuth client ID'\n"
            "3. Get the Refresh Token using `rclone authorize drive` on your PC.\n\n"
            "**Warning:** Credentials will be shown once and deleted after 5 minutes."
        )

        await query.message.edit_text(
            text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="us_settings")]]
            ),
        )
        context.user_data["awaiting"] = "us_rclone_service"
        logger.info(f"✅ Rclone service prompt sent to {update.effective_user.id}")
    except Exception as e:
        logger.error(f"❌ Error in handle_us_rclone_service: {e}")
        await query.answer("❌ Error", show_alert=True)


async def handle_us_reset_confirm_yes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Reset all user settings to defaults after confirmation"""
    try:
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        await update_user(
            user_id,
            {
                "settings": {
                    "prefix": "",
                    "suffix": "",
                    "mode": "video",
                    "metadata": {},
                    "destination_channel": None,
                    "remove_words": [],
                    "thumbnail": "auto",
                }
            },
        )
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
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="start")]]
            ),
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_callback_help: {e}")
        try:
            await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")
        except:
            pass


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
            support_channel = config.get("support_channel") or config.get(
                "channels", {}
            ).get("support")
            contact = config.get("support_contact", "")
            text = (
                "💬 **Support & Upgrades**\n\n"
                f"{'Channel: ' + support_channel if support_channel else ''}"
                f"{'\\n' if support_channel and contact else ''}"
                f"{'Contact: ' + contact if contact else ''}"
            ).strip() or "💬 **Support**\n\nPlease contact the admin for help."
            parse_mode = "Markdown"

        keyboard = []
        keyboard.append(
            [
                InlineKeyboardButton(
                    "💬 Bot Support (Chat)", callback_data="start_support_chat"
                )
            ]
        )
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="start")])

        await query.message.edit_text(
            text, parse_mode=parse_mode, reply_markup=InlineKeyboardMarkup(keyboard)
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
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="support")]]
            ),
        )
        context.user_data["awaiting"] = "support_message"
        logger.info(f"✅ User {update.effective_user.id} started support chat")
    except Exception as e:
        logger.error(f"❌ Error in handle_start_support_chat: {e}")
        await update.callback_query.answer("❌ Error initiating chat", show_alert=True)
