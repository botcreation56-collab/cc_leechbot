import logging
import os
import time as _time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import TelegramError
from bot.middleware import admin_only
from bot.database import (
    get_db,
    get_user,
    get_all_users,
    ban_user,
    unban_user,
    update_user,
    get_config,
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
)
from bot.utils import (
    send_auto_delete_msg,
    log_info,
    log_error,
    log_user_update,
    validate_url,
)
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


async def show_config_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Configuration menu with all settings"""
    try:
        from bot.handlers.admin import _require_channels_setup

        if not await _require_channels_setup(update, context):
            return
        config = await get_config() or {}

        # Helper to get channel name
        async def get_ch_name(nested_key, flat_key):
            # 1. Try nested schema
            ch = config.get("channels", {}).get(nested_key, {})
            ch_id = ch.get("id")
            ch_title = ch.get("metadata", {}).get("title")
            # 2. Fallback to flat schema
            if not ch_id:
                ch_id = config.get(flat_key)
            if not ch_id:
                return "Not set"
            # 3. Use title if we have it, else try to fetch
            if ch_title:
                return f"{ch_title} ({ch_id})"
            try:
                chat = await context.bot.get_chat(ch_id)
                return f"{chat.title} ({ch_id})"
            except:
                return str(ch_id)

        import asyncio

        log_name, dump_name, storage_name = await asyncio.gather(
            get_ch_name("log", "log_channel_id"),
            get_ch_name("dump", "dump_channel_id"),
            get_ch_name("storage", "storage_channel_id"),
        )

        # Check GDrive/Rclone status
        gdrive_status = "❌ Not configured"
        rclone_status = "❌ No remotes"
        try:
            from bot.database import get_rclone_configs

            remotes = await get_rclone_configs()
            if remotes:
                rclone_status = f"✅ {len(remotes)} remote(s)"
            try:
                from bot.services import GDriveService

                if await GDriveService.is_configured():
                    gdrive_status = "✅ Configured"
            except:
                pass
        except:
            pass

        current_settings = (
            f"⚙️ **Bot Configuration**\n\n"
            f"🏢 Site: `{config.get('site_name', 'Not set')}`\n"
            f"📝 Description: `{config.get('site_description', 'Not set')[:40]}`\n"
            f"☎️ Support: `{config.get('support_contact', 'Not set')}`\n"
            f"🔗 Support Channel: `{config.get('support_channel', 'Not set')}`\n"
            f"⚡ Parallel Limit: `{config.get('parallel_global_limit', 5)}`\n"
            f"📦 Max File Size: `{config.get('max_file_size_gb', 10)} GB`\n"
            f"📅 File Expiry: `{config.get('file_expiry_days', 7)} days`\n"
            f"📌 Log Channel: `{log_name}`\n"
            f"💾 Dump Channel: `{dump_name}`\n"
            f"🗄️ Storage Channel: `{storage_name}`\n\n"
            f"☁️ **Cloud Storage**\n"
            f"GDrive: {gdrive_status}\n"
            f"Rclone: {rclone_status}"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💬 Start Message", callback_data="edit_start_msg"
                    ),
                    InlineKeyboardButton(
                        "💦 Watermark", callback_data="edit_watermark"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "☎️ Support Contact", callback_data="edit_contact"
                    ),
                    InlineKeyboardButton(
                        "📖 Help Text", callback_data="edit_help_text"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🏢 Site Name", callback_data="edit_site_name"
                    ),
                    InlineKeyboardButton(
                        "📝 Description", callback_data="edit_site_desc"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔗 Support Channel", callback_data="edit_support_channel"
                    ),
                    InlineKeyboardButton(
                        "⚡ Parallel Limit", callback_data="edit_parallel"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📦 Max File Size", callback_data="edit_max_filesize"
                    ),
                    InlineKeyboardButton(
                        "📅 File Expiry", callback_data="edit_file_expiry"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📢 Force Sub Channels",
                        callback_data="admin_set_force_sub_channel",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📌 Set Log Channel", callback_data="admin_set_log_channel"
                    ),
                    InlineKeyboardButton(
                        "💾 Set Dump Channel", callback_data="admin_set_dump_channel"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🗄️ Set Storage Channel",
                        callback_data="admin_set_storage_channel",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "☁️ Cloud Storage Setup",
                        callback_data="admin_rclone",
                    )
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_back")],
            ]
        )

        await update.callback_query.message.edit_text(
            current_settings, reply_markup=keyboard, parse_mode="Markdown"
        )

        await log_admin_action(update.effective_user.id, "opened_config", {})
        logger.info("✅ Config menu opened")

    except Exception as e:
        logger.error(f"❌ Error in config menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


async def handle_edit_rclone_creds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: Start the admin-side global Rclone setup wizard (Hybrid Auth)."""
    try:
        query = update.callback_query
        await query.answer()

        context.user_data["awaiting"] = "edit_rclone_client_id"

        await query.message.reply_text(
            "📂 **Global Admin Google Drive Setup (Hybrid Auth)**\n\n"
            "This will create a global Rclone configuration usable by multiple users depending on their plan.\n\n"
            "**Step 1:** Please enter your Google **Client ID**.\n"
            "*(It usually ends with `apps.googleusercontent.com`)*\n\n"
            "Use /cancel to abort.",
            parse_mode="Markdown",
        )
        logger.info(
            f"✅ Admin Rclone hybrid auth setup started by {update.effective_user.id}"
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_edit_rclone_creds: {e}")
        await update.callback_query.answer("❌ Error starting setup", show_alert=True)


async def handle_edit_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit /start command message"""
    try:
        await update.callback_query.message.reply_text(
            "💬 **Edit Start Message**\n\nSend the new /start message.\n\nSupports **Markdown** formatting.\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_start_msg"
        context.user_data["awaiting_set_at"] = _time.time()
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_watermark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit watermark/caption text"""
    try:
        await update.callback_query.message.reply_text(
            "💦 **Edit Watermark Caption**\n\nSend the new watermark text.\n\nThis appears on downloaded files.\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_watermark"
        context.user_data["awaiting_set_at"] = _time.time()
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_support_contact(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Edit support contact details"""
    try:
        await update.callback_query.message.reply_text(
            "☎️ **Edit Support Contact**\n\nSend support details:\n- Email\n- Phone\n- Telegram ID\n- Support channel link\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_contact"
        context.user_data["awaiting_set_at"] = _time.time()
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_help_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit help text"""
    try:
        await update.callback_query.message.reply_text(
            "📖 **Edit Help Text**\n\nSend the new help/guide text.\n\nShown when users click /help.\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_help_text"
        context.user_data["awaiting_set_at"] = _time.time()
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_site_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit site name"""
    try:
        await update.callback_query.message.reply_text(
            "🏢 **Edit Site Name**\n\nSend your bot/service name:\n\nMax 50 characters\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_site_name"
        context.user_data["awaiting_set_at"] = _time.time()
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_site_description(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Edit site description"""
    try:
        await update.callback_query.message.reply_text(
            "📝 **Edit Site Description**\n\nSend your bot/service description:\n\nMax 500 characters\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_site_desc"
        context.user_data["awaiting_set_at"] = _time.time()
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_support_channel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Edit support channel link"""
    try:
        await update.callback_query.message.reply_text(
            "🔗 **Edit Support Channel**\n\nSend support channel link:\n\nFormat: `https://t.me/yourchannel`\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_support_channel"
        context.user_data["awaiting_set_at"] = _time.time()
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_parallel_limit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Edit parallel processing limit"""
    try:
        config = await get_config() or {}
        current = config.get("parallel_global_limit", 5)
        await update.callback_query.message.reply_text(
            f"⚡ **Edit Parallel Processing Limit**\n\nCurrent: `{current}`\n\nSend new limit (1-50):\n\nHigher = faster but more resource usage\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_parallel"
        context.user_data["awaiting_set_at"] = _time.time()
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_max_filesize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit maximum file size limit"""
    try:
        config = await get_config() or {}
        current = config.get("max_file_size_gb", 10)
        await update.callback_query.message.reply_text(
            f"📦 **Edit Max File Size**\n\nCurrent: `{current} GB`\n\nSend new limit (1-1000 GB):\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_max_filesize"
        context.user_data["awaiting_set_at"] = _time.time()
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_file_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit file expiry days"""
    try:
        config = await get_config() or {}
        current = config.get("file_expiry_days", 7)
        await update.callback_query.message.reply_text(
            f"📅 **Edit File Expiry**\n\nCurrent: `{current} days`\n\nSend new expiry (1-365 days):\n\nFiles older than this are auto-deleted\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_file_expiry"
        context.user_data["awaiting_set_at"] = _time.time()
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_tos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit Terms of Service string"""
    try:
        await update.callback_query.message.reply_text(
            "📜 **Edit Terms of Service**\n\nSend the new Terms of Service text.\n\nYou can use HTML formatting tags like `<b>` and `<p>`.\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_tos"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_upgrade_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit custom text for User Upgrades"""
    try:
        await update.callback_query.message.reply_text(
            "💎 **Edit Upgrade Text**\n\nSend the text shown when users attempt to upgrade and you want them to contact you.\n\nYou can use HTML formatting tags like `<b>` and `<p>`.\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_upgrade_text"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_updates_channel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Edit updates channel link"""
    try:
        await update.callback_query.message.reply_text(
            "📢 **Edit Updates Channel**\n\nSend your updates/news channel handle or link:\n\nFormat: `@yourchannel` or `https://t.me/yourchannel`\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "edit_updates_channel"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_edit_force_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for force sub management"""
    from bot.handlers.admin import handle_admin_set_force_sub_channel

    await handle_admin_set_force_sub_channel(update, context)


async def handle_edit_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit plan configuration - handles both menu and field prompts"""
    try:
        query = update.callback_query
        data = query.data
        await query.answer()

        parts = data.split("_")
        # data format: edit_plan_FREE, edit_price_FREE, edit_plan_parallel_FREE, etc.
        plan_name = parts[-1]
        field = parts[1] if parts[0] == "edit" else None

        config = await get_config() or {}
        plans = config.get("plans", {})
        plan_data = plans.get(plan_name, {})

        if field in ("price", "daily", "expiry") or "parallel" in data:
            # We want to prompt for a specific field
            labels = {
                "price": "💰 Plan Price ($)",
                "plan_parallel": "⚡ Parallel Tasks",
                "daily": "📦 Daily Limit (GB)",
                "expiry": "📅 Dump Expiry (Days)",
            }
            # Special case for parallel because it has 'plan' in parts[1]
            label_key = "plan_parallel" if "parallel" in data else field
            label = labels.get(label_key, field.title())

            # Map callback field to internal plan_data key
            key_map = {
                "price": "price",
                "plan_parallel": "parallel",
                "daily": "storage_per_day",
                "expiry": "dump_expiry_days",
            }
            internal_key = key_map.get(label_key)
            current = plan_data.get(internal_key, "Not Set")

            msg = await query.message.edit_text(
                f"{label}\n\n"
                f"Plan: **{plan_name.upper()}**\n"
                f"Current Value: `{current}`\n\n"
                f"Please send the new value below.\n\n"
                f"Use /cancel to abort.",
                parse_mode="Markdown",
            )
            context.user_data["prompt_msg_id"] = msg.message_id
            context.user_data["awaiting"] = (
                f"edit_plan_field_{plan_name}_{internal_key}"
            )
            context.user_data["awaiting_set_at"] = _time.time()
            return

        # Default: Show the plan menu
        plan_text = (
            f"⭐ **Edit {plan_name.upper()} Plan**\n\n"
            f"Price: ${plan_data.get('price', 0)}\n"
            f"Parallel: {plan_data.get('parallel', 1)}\n"
            f"Daily Limit: {plan_data.get('storage_per_day', 5)} GB\n"
            f"Expiry: {plan_data.get('dump_expiry_days', 0)} days"
        )

        rclone_allowed = plan_data.get("rclone_allowed", False)
        rclone_icon = "✅" if rclone_allowed else "❌"

        shortener_allowed = plan_data.get("shortener_allowed", False)
        shortener_icon = "✅" if shortener_allowed else "❌"

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💰 Edit Price", callback_data=f"edit_price_{plan_name}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⚡ Edit Parallel",
                        callback_data=f"edit_plan_parallel_{plan_name}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📦 Edit Daily Limit", callback_data=f"edit_daily_{plan_name}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📅 Edit Expiry", callback_data=f"edit_expiry_{plan_name}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"{rclone_icon} Rclone {'Allowed' if rclone_allowed else 'Denied'} — Toggle",
                        callback_data=f"toggle_rclone_{plan_name}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"{shortener_icon} Shortener {'Allowed' if shortener_allowed else 'Denied'} — Toggle",
                        callback_data=f"toggle_shortener_{plan_name}",
                    )
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_plans")],
            ]
        )

        await query.message.edit_text(
            plan_text, reply_markup=keyboard, parse_mode="Markdown"
        )
        await log_admin_action(
            update.effective_user.id, "opened_plan_edit", {"plan": plan_name}
        )
        logger.info(f"✅ Plan edit menu opened for {plan_name}")

    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        try:
            await update.callback_query.answer(f"❌ Error", show_alert=True)
        except:
            pass


async def handle_config_edit_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: str
):
    """Handle admin text input for all edit_* config state machine flows.
    Called from handle_text_input when awaiting starts with 'edit_' or is 'add_shortener'.
    """
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    # Map awaiting state → DB config key
    STATE_TO_CONFIG_KEY = {
        "edit_start_msg": "start_message",
        "edit_watermark": "watermark_text",
        "edit_contact": "support_contact",
        "edit_help_text": "help_text",
        "edit_site_name": "site_name",
        "edit_site_desc": "site_description",
        "edit_support_channel": "support_channel",
        "edit_parallel": "parallel_global_limit",
        "edit_max_filesize": "max_filesize_gb",
        "edit_file_expiry": "file_expiry_days",
        "edit_tos": "tos_text",
        "edit_upgrade_text": "upgrade_text",
        "edit_updates_channel": "updates_channel",
    }

    try:
        if state in STATE_TO_CONFIG_KEY:
            config_key = STATE_TO_CONFIG_KEY[state]

            # Type coercion for numeric fields
            if config_key in (
                "parallel_global_limit",
                "max_filesize_gb",
                "file_expiry_days",
            ):
                try:
                    value = float(text)
                    if config_key in ("parallel_global_limit", "file_expiry_days"):
                        value = int(value)
                        if config_key == "parallel_global_limit":
                            try:
                                from bot.services import FFmpegService, QueueWorker

                                FFmpegService.set_parallel_limit(value)
                                try:
                                    worker = QueueWorker.get_instance()
                                    worker.update_limit(value)
                                except Exception:
                                    pass  # Worker might not be running yet
                            except Exception as e:
                                logger.error(f"Failed to update service limits: {e}")
                except ValueError:
                    await send_auto_delete_msg(
                        context.bot,
                        update.effective_chat.id,
                        f"❌ **Invalid Value**\n\nPlease enter a number.",
                        parse_mode="Markdown",
                    )
                    return
            else:
                value = text

            from bot.database import set_config

            ok = await set_config({config_key: value})
            if ok:
                label = state.replace("edit_", "").replace("_", " ").title()
                await update.message.reply_text(
                    f"✅ **{label} Updated**\n\nNew value: `{str(value)[:200]}`",
                    parse_mode="Markdown",
                )
                await log_admin_action(
                    user_id, f"updated_config_{config_key}", {"value": str(value)[:100]}
                )
            else:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Failed to save config. Please try again.",
                    parse_mode="Markdown",
                )

        elif state.startswith("edit_plan_field_"):
            # Format: edit_plan_field_{plan_name}_{internal_key}
            parts = state.split("_")
            plan_name = parts[3]
            field_key = parts[4]

            # Numeric validation
            try:
                if field_key in (
                    "price",
                    "parallel",
                    "storage_per_day",
                    "dump_expiry_days",
                ):
                    value = float(text)
                    if field_key in ("parallel", "dump_expiry_days"):
                        value = int(value)
                else:
                    value = text
            except ValueError:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Please enter a valid number.",
                    parse_mode="Markdown",
                )
                return

            from bot.database import get_config, set_config

            config = await get_config() or {}
            plans = config.get("plans", {})
            if plan_name not in plans:
                plans[plan_name] = {}

            plans[plan_name][field_key] = value
            ok = await set_config({"plans": plans})

            if ok:
                await update.message.reply_text(
                    f"✅ **Plan Updated**\n\n"
                    f"Plan: `{plan_name.upper()}`\n"
                    f"Field: `{field_key}`\n"
                    f"New Value: `{value}`",
                    parse_mode="Markdown",
                )
                await log_admin_action(
                    user_id,
                    f"updated_plan_{plan_name}_{field_key}",
                    {"value": str(value)},
                )
            else:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Failed to save plan configuration.",
                    parse_mode="Markdown",
                )

            context.user_data.pop("awaiting", None)
            return

        elif state == "add_shortener_api":
            if len(text) < 5:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ API key seems too short. Try again.",
                    parse_mode="Markdown",
                )
                return
            context.user_data["temp_shortener_api"] = text
            context.user_data["awaiting"] = "add_shortener_url"
            await update.message.reply_text(
                "🔗 **Step 2: Send Site Link**\n\n"
                "Example: `https://gplinks.com/`\n\n"
                "Use /cancel to abort.",
                parse_mode="Markdown",
            )
            return

        elif state == "add_shortener_url":
            context.user_data["temp_shortener_domain"] = text.strip()
            context.user_data["awaiting"] = "add_shortener_tutorial"
            await update.message.reply_text(
                "🔗 **Step 3: Bypass Tutorial Link**\n\n"
                "Send a tutorial link for how to bypass the queue.\n"
                "This will be shown as 'How to bypass?' button.\n\n"
                "Example: `https://youtube.com/watch?v=example`\n"
                "Or leave empty if not needed.\n\n"
                "Use /cancel to abort.",
                parse_mode="Markdown",
            )
            return

        elif state == "add_shortener_tutorial":
            api_key = context.user_data.pop("temp_shortener_api", None)
            domain = context.user_data.pop("temp_shortener_domain", None)
            tutorial_link = text.strip() if text.strip() else None

            if not api_key or not domain:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Missing data. Please start over.",
                    parse_mode="Markdown",
                )
                context.user_data.pop("awaiting", None)
                return

            domain_url = domain.strip()
            if not domain_url.startswith("http"):
                domain_url = f"https://{domain_url}"

            from bot.database import set_config, get_config

            config = await get_config() or {}
            shorteners = config.get("shorteners", [])
            shorteners = [s for s in shorteners if s.get("domain") != domain_url]
            shorteners.append({"domain": domain_url, "api_key": api_key})
            ok = await set_config({"shorteners": shorteners})

            # Also save tutorial link globally for bypass
            if tutorial_link:
                await set_config({"shortener_tutorial_link": tutorial_link})

            if ok:
                tutorial_text = (
                    f"\nTutorial: `{tutorial_link}`" if tutorial_link else ""
                )
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    f"✅ **Shortener Added**\n\nSite: `{domain_url}`{tutorial_text}",
                    parse_mode="Markdown",
                )
                await log_admin_action(
                    user_id,
                    "added_shortener",
                    {"domain": domain_url, "tutorial": tutorial_link},
                )
            else:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Failed to save shortener.",
                    parse_mode="Markdown",
                )

            context.user_data.pop("awaiting", None)

        elif state == "edit_tutorial_link":
            tutorial_link = text.strip() if text.strip() else None

            ok = await set_config({"shortener_tutorial_link": tutorial_link})
            if ok:
                if tutorial_link:
                    await send_auto_delete_msg(
                        context.bot,
                        update.effective_chat.id,
                        f"✅ **Tutorial Link Updated**\n\n`{tutorial_link}`",
                        parse_mode="Markdown",
                    )
                else:
                    await send_auto_delete_msg(
                        context.bot,
                        update.effective_chat.id,
                        "✅ **Tutorial Link Removed**",
                        parse_mode="Markdown",
                    )
                await log_admin_action(
                    user_id,
                    "updated_tutorial_link",
                    {"tutorial_link": tutorial_link},
                )
            else:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Failed to save tutorial link.",
                    parse_mode="Markdown",
                )

            context.user_data.pop("awaiting", None)

        else:
            logger.warning(f"Unhandled config edit state: {state}")
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                "❌ Unknown config state. Please try again from the admin menu.",
                parse_mode="Markdown",
            )
            context.user_data.pop("awaiting", None)

    except Exception as e:
        logger.error(
            f"❌ Error in handle_config_edit_input (state={state}): {e}", exc_info=True
        )
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            f"❌ Error saving config: {str(e)[:100]}",
            parse_mode="Markdown",
        )
        context.user_data.pop("awaiting", None)


async def handle_us_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user prefix settings"""
    try:
        user_id = update.effective_user.id
        text = update.message.text
        await update_user(user_id, {"prefix": text})
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            f"✅ Prefix updated to: `{text}`",
            parse_mode="Markdown",
        )
        context.user_data.pop("awaiting", None)
    except Exception as e:
        logger.error(f"❌ Error in handle_us_prefix: {e}")


async def handle_us_suffix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user suffix settings"""
    try:
        user_id = update.effective_user.id
        text = update.message.text
        await update_user(user_id, {"suffix": text})
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            f"✅ Suffix updated to: `{text}`",
            parse_mode="Markdown",
        )
        context.user_data.pop("awaiting", None)
    except Exception as e:
        logger.error(f"❌ Error in handle_us_suffix: {e}")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /settings command - check fsub first, then redirect to /ussettings"""
    try:
        # Check force subscription first
        from bot.handlers.user import check_force_sub

        if not await check_force_sub(update, context):
            return

        await update.message.reply_text(
            "⚙️ **Settings**\n\nUse /ussettings to open your settings menu.",
            parse_mode="Markdown",
        )

        await log_info(f"✅ /settings used by {update.effective_user.id}")

    except Exception as e:
        logger.error(f"❌ Error in settings command: {e}", exc_info=True)
        await log_error(f"❌ Error in settings command: {str(e)}")
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            "❌ Unable to open settings. Please try /ussettings instead.",
            parse_mode="Markdown",
        )


async def ussettings_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, photo_file_id: str = None
) -> None:
    """User settings handler - displays settings menu"""
    try:
        # Check force subscription
        from bot.handlers import check_force_sub

        if not await check_force_sub(update, context):
            return

        user_id = update.effective_user.id

        # Determine if this is a callback or a command
        is_callback = False
        message = None

        if update.callback_query:
            is_callback = True
            message = update.callback_query.message
            # Don't answer here if calling from other handlers, usually they answer before.
            # But duplicate answer is harmless.
        else:
            message = update.message

        user = await get_user(user_id)

        if not user:
            text = "❌ **User Not Found**\n\nPlease use /start first."
            if is_callback:
                await message.edit_text(text, parse_mode="Markdown")
            else:
                await message.reply_text(text, parse_mode="Markdown")
            return

        # Create settings menu
        keyboard = [
            [
                InlineKeyboardButton("📝 Prefix", callback_data="us_prefix"),
                InlineKeyboardButton("📝 Suffix", callback_data="us_suffix"),
            ],
            [
                InlineKeyboardButton("🎵 Metadata", callback_data="us_metadata"),
                InlineKeyboardButton("🖼️ Thumbnail", callback_data="us_thumbnail"),
            ],
            [
                InlineKeyboardButton("💎 Plan", callback_data="us_plan"),
                InlineKeyboardButton("🎬 Mode", callback_data="us_mode"),
            ],
            [
                InlineKeyboardButton("📁 Destination", callback_data="us_destination"),
                InlineKeyboardButton("👁️ Visibility", callback_data="us_visibility"),
            ],
            [
                InlineKeyboardButton("🗑️ Remove", callback_data="us_remove"),
                InlineKeyboardButton("📂 My Files", callback_data="us_myfiles"),
            ],
            [
                InlineKeyboardButton(
                    "📢 Updates Channel", url="https://t.me/cc_leechbot"
                ),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="us_close")],
        ]

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
            keyboard[-2][
                0
            ].url = updates_url  # index -2 = Updates Channel row, -1 = Back

        # Get user settings
        settings = user.get("settings", {})
        prefix = settings.get("prefix", "Not set")
        suffix = settings.get("suffix", "Not set")
        plan = user.get("plan", "free")

        prefix_display = (
            f"{prefix[:10]}..."
            if len(prefix) > 10
            else (prefix if prefix != "Not set" else "Not set")
        )
        suffix_display = (
            f"{suffix[:10]}..."
            if len(suffix) > 10
            else (suffix if suffix != "Not set" else "Not set")
        )

        mode = settings.get("mode", "video").upper()
        visibility = settings.get("visibility", "public").upper()

        text = (
            f"⚙️ **Your Settings**\n\n"
            f"📝 Prefix: `{prefix_display}`\n"
            f"📝 Suffix: `{suffix_display}`\n"
            f"🎬 Mode: `{mode}`\n"
            f"👁️ Visibility: `{visibility}`\n\n"
            f"💎 Plan: `{plan.upper()}`"
        )

        reply_markup = InlineKeyboardMarkup(keyboard)

        # If a photo ID is provided or if the user has a custom thumbnail, show it.
        show_photo_id = photo_file_id
        if not show_photo_id and settings.get("thumbnail") == "custom":
            show_photo_id = settings.get("thumbnail_file_id")
            logger.info(f"🖼️ Using custom thumbnail: {show_photo_id}")

        if show_photo_id:
            try:
                # If already a photo message, edit it
                if is_callback and message.photo:
                    from telegram import InputMediaPhoto

                    logger.info(f"🎨 Editing media with thumbnail: {show_photo_id}")
                    await message.edit_media(
                        media=InputMediaPhoto(
                            media=show_photo_id, caption=text, parse_mode="Markdown"
                        ),
                        reply_markup=reply_markup,
                    )
                else:
                    # Not a photo message or not a callback -> send new or replace
                    if is_callback:
                        try:
                            await message.delete()
                        except:
                            pass

                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=show_photo_id,
                        caption=text,
                        reply_markup=reply_markup,
                        parse_mode="Markdown",
                    )
            except Exception as e:
                logger.warning(f"Could not update/send settings photo: {e}")
                # Fallback to text
                if is_callback:
                    await message.edit_text(
                        text, reply_markup=reply_markup, parse_mode="Markdown"
                    )
                else:
                    await message.reply_text(
                        text, reply_markup=reply_markup, parse_mode="Markdown"
                    )
        else:
            # No thumbnail -> Text Only
            if is_callback:
                if message.photo:
                    # Message was a photo but now we have no thumb? Delete and send text.
                    await message.delete()
                    await context.bot.send_message(
                        user_id, text, reply_markup=reply_markup, parse_mode="Markdown"
                    )
                else:
                    try:
                        await message.edit_text(
                            text, reply_markup=reply_markup, parse_mode="Markdown"
                        )
                    except Exception:
                        await message.reply_text(
                            text, reply_markup=reply_markup, parse_mode="Markdown"
                        )
            else:
                await message.reply_text(
                    text, reply_markup=reply_markup, parse_mode="Markdown"
                )

        logger.info(f"✅ Settings menu shown to {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in ussettings_command: {e}", exc_info=True)
        try:
            await update.effective_message.reply_text(
                "❌ Error opening settings. Please try /ussettings again."
            )
        except:
            pass


async def handle_user_settings_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: str
):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    try:
        user = await get_user(user_id)
        if not user:
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                "❌ User not found. Use /start first.",
                parse_mode="Markdown",
            )
            context.user_data.pop("awaiting", None)
            return

        settings = user.get("settings", {})

        # ============================================================
        # HANDLE PREFIX
        # ============================================================

        if state == "us_prefix":
            if len(text) > 160:
                await update.message.reply_text(
                    "❌ **Too Long**\n\n"
                    "Prefix must be under 160 characters.\n\n"
                    "Please send a shorter prefix or use /cancel to skip.",
                    parse_mode="Markdown",
                )
                return  # Don't clear awaiting — user needs to retry

            if text.lower() == "d":
                text = ""
                msg = "✅ **Prefix Disabled**\n\nNo prefix will be added to filenames."
            else:
                msg = f"✅ **Prefix Set**\n\nPrefix: `{text}`"

            await update_user(user_id, {"settings.prefix": text})
            await update.message.reply_text(msg, parse_mode="Markdown")
            await log_user_update(context.bot, user_id, f"set prefix: {text}")
            logger.info(f"✅ User {user_id} set prefix: {text}")

        elif state == "us_suffix":
            if len(text) > 160:
                await update.message.reply_text(
                    "❌ **Too Long**\n\n"
                    "Suffix must be under 160 characters.\n\n"
                    "Please send a shorter suffix or use /cancel to skip.",
                    parse_mode="Markdown",
                )
                return  # Don't clear awaiting — user needs to retry

            if text.lower() == "d":
                text = ""
                msg = "✅ **Suffix Disabled**\n\nNo suffix will be added to filenames."
            else:
                msg = f"✅ **Suffix Set**\n\nSuffix: `{text}`"

            await update_user(user_id, {"settings.suffix": text})
            await update.message.reply_text(msg, parse_mode="Markdown")
            await log_user_update(context.bot, user_id, f"set suffix: {text}")
            logger.info(f"✅ User {user_id} set suffix: {text}")

        # ============================================================
        # HANDLE METADATA
        # ============================================================

        elif state in (
            "us_meta_video",
            "us_meta_author",
            "us_meta_audio",
            "us_meta_subs",
        ):
            if len(text) > 100:
                await update.message.reply_text(
                    "❌ **Too Long**\n\nMetadata field must be under 100 characters.",
                    parse_mode="Markdown",
                )
                return

            tag_name = state.replace("us_meta_", "")
            # Mapping for track titles format: [Value] | [Language]
            await update_user(user_id, {f"settings.metadata.{tag_name}": text})

            label = {
                "video": "Video Title",
                "author": "Artist/Author",
                "audio": "Audio Track Label",
                "subs": "Subtitle Track Label",
            }.get(tag_name, tag_name.title())

            await update.message.reply_text(
                f"✅ **{label} Set**\n\nValue: `{text}`", parse_mode="Markdown"
            )

            logger.info(f"✅ User {user_id} set metadata {tag_name}: {text}")

        # ============================================================
        # HANDLE THUMBNAIL
        # ============================================================

        elif state == "us_rclone_service":
            import re

            client_id, client_secret = None, None
            text_cleaned = text.strip()

            # Check for generic property assignment `ID = "..."` `Secret = "..."`
            id_match = re.search(
                r'(?:Client[_\s]*ID|ID)\s*=\s*["\']?([^"\'\n\|]+)["\']?',
                text_cleaned,
                re.IGNORECASE,
            )
            sec_match = re.search(
                r'(?:Client[_\s]*Secret|Secret)\s*=\s*["\']?([^"\'\n\|]+)["\']?',
                text_cleaned,
                re.IGNORECASE,
            )

            if id_match and sec_match:
                client_id = id_match.group(1).strip()
                client_secret = sec_match.group(1).strip()
            # Check for original pipe delimiter `ID | Secret`
            elif "|" in text_cleaned:
                parts = [p.strip() for p in text_cleaned.split("|")]
                if len(parts) == 2:
                    client_id, client_secret = parts
            # Check for two lines generic assignment `ID \n Secret`
            else:
                lines = [l.strip() for l in text_cleaned.splitlines() if l.strip()]
                if len(lines) == 2:
                    client_id, client_secret = lines

            if not client_id or not client_secret:
                await update.message.reply_text(
                    "❌ **Invalid Format**\n\n"
                    "Please send your credentials like this:\n"
                    '`ID = "your_client_id_here"`\n'
                    '`Secret = "your_client_secret_here"`\n\n'
                    "*(Or just send them separated by a pipe `|`)*",
                    parse_mode="Markdown",
                )
                return
            user_id = update.effective_user.id

            # Deduced base URL from settings (filled in main.py)
            base_url = (settings.WEBHOOK_URL or "").replace("/webhook/telegram", "")
            if not base_url:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ **Error**: Webhook URL not configured. Contact admin.",
                    parse_mode="Markdown",
                )
                return

            auth_url = f"{base_url}/api/rclone/auth?user_id={user_id}&client_id={client_id}&client_secret={client_secret}"

            keyboard = [
                [InlineKeyboardButton("🔗 Authorize with Google", url=auth_url)]
            ]
            await update.message.reply_text(
                "✅ **Credentials Accepted!**\n\n"
                "Now, click the button below to authorize the bot to access your Google Drive.\n\n"
                "Once authorized, the bot will automatically setup your remote.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )

            # Clear state as we're moving to the web flow
            context.user_data.pop("awaiting", None)
            logger.info(f"✅ Rclone OAuth link generated for {user_id}")

        elif state == "us_thumbnail":
            if text.lower() in ["auto", "automatic", "default"]:
                await update_user(user_id, {"settings.thumbnail": "auto"})

                await update.message.reply_text(
                    "✅ **Thumbnail Set to Auto**\n\n"
                    "Thumbnails will be generated automatically.",
                    parse_mode="Markdown",
                )

                logger.info(f"✅ User {user_id} set thumbnail to auto")
            else:
                await update.message.reply_text(
                    "❌ **Invalid Input**\n\n"
                    "Please send:\n"
                    "- An image (photo)\n"
                    "- Or type 'auto' for automatic thumbnails\n\n"
                    "Use /cancel to skip.",
                    parse_mode="Markdown",
                )

            logger.info(f"✅ User {user_id} added remove word: {text}")

        else:
            logger.warning(f"⚠️ Unknown user settings state: {state}")
            await update.message.reply_text(
                "❌ **Unknown Setting**\n\nPlease try again from /ussettings menu.",
                parse_mode="Markdown",
            )

        # Clear awaiting state
        context.user_data.pop("awaiting", None)

    except Exception as e:
        logger.error(f"❌ Error handling user settings text: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ **Error Saving Settings**\n\n"
            f"Error: {str(e)[:100]}\n\n"
            f"Please try again or use /support.",
            parse_mode="Markdown",
        )
        context.user_data.pop("awaiting", None)


async def handle_us_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        user = await get_user(user_id)
        if not user:
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                "❌ User not found",
                parse_mode="Markdown",
            )
            return

        current_mode = user.get("settings", {}).get("mode", "video")

        keyboard = [
            [
                InlineKeyboardButton("🎬 VIDEO", callback_data="us_mode_video"),
                InlineKeyboardButton("📄 DOCUMENT", callback_data="us_mode_document"),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="us_back")],
        ]

        await query.message.edit_text(
            mode_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

        logger.info(f"✅ Mode menu opened for user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_us_mode: {e}", exc_info=True)
        await update.callback_query.answer("❌ Error", show_alert=True)


async def handle_us_mode_video(
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

        await update_user(user_id, {"settings.mode": "video"})

        await query.answer("✅ Mode set to VIDEO", show_alert=True)

        await log_user_update(context.bot, user_id, "set mode to video")
        logger.info(f"✅ User {user_id} set mode to VIDEO")

        # Return to settings menu
        await ussettings_command(update, context)

    except Exception as e:
        logger.error(f"❌ Error in handle_us_mode_video: {e}", exc_info=True)
        await query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


async def handle_us_mode_document(
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

        await update_user(user_id, {"settings.mode": "document"})

        await query.answer("✅ Mode set to DOCUMENT", show_alert=True)

        await log_user_update(context.bot, user_id, "set mode to document")
        logger.info(f"✅ User {user_id} set mode to DOCUMENT")

        # Return to settings menu
        await ussettings_command(update, context)

    except Exception as e:
        logger.error(f"❌ Error in handle_us_mode_document: {e}", exc_info=True)
        await query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


async def go_back_to_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        # Recreate the update object for command handler
        await ussettings_command(update, context)
        logger.info(f"✅ Back to settings for user {update.effective_user.id}")

    except Exception as e:
        logger.error(f"❌ Error in go_back_to_settings: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_us_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Close the settings menu by deleting the message"""
    try:
        query = update.callback_query
        await query.answer()
        await query.message.delete()
        logger.info(f"✅ Settings menu closed for {update.effective_user.id}")
    except Exception as e:
        logger.error(f"❌ Error closing settings: {e}")


async def handle_us_rclone_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: Start the user-side Rclone setup wizard.
    Asks the user to provide their own Google Client ID."""
    try:
        query = update.callback_query
        await query.answer()

        context.user_data["awaiting"] = "us_rclone_name"

        await query.message.reply_text(
            "📂 **Setup Google Drive (Hybrid Auth)**\n\n"
            "To connect your Google Drive safely, please generate your own Google Cloud app credentials.\n\n"
            "**Step 1:** Please enter a short **Name** for this drive (e.g. `MyMovies`).\n\n"
            "Use /cancel to abort.",
            parse_mode="Markdown",
        )
        logger.info(
            f"✅ Rclone hybrid auth setup started for {update.effective_user.id}"
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_us_rclone_service: {e}")
        await update.callback_query.answer("❌ Error starting setup", show_alert=True)


async def handle_us_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: show prompt to set filename prefix"""
    try:
        query = update.callback_query
        await query.answer()
        msg = await query.message.reply_text(
            "📝 **Set Filename Prefix**\n\n"
            "Send the text you want to add at the **beginning** of every filename.\n"
            "Example: `[HQ]` → `[HQ] video.mp4`\n\n"
            "Send d to disable prefix.",
            parse_mode="Markdown",
        )
        context.user_data["prompt_msg_id"] = msg.message_id
        context.user_data["awaiting"] = "us_prefix"
        context.user_data["awaiting_set_at"] = _time.time()
        logger.info(f"✅ Prefix prompt sent to {update.effective_user.id}")
    except Exception as e:
        logger.error(f"❌ Error in handle_us_prefix: {e}")
        await update.callback_query.answer("❌ Error", show_alert=True)


async def handle_us_suffix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: show prompt to set filename suffix"""
    try:
        query = update.callback_query
        await query.answer()
        msg = await query.message.reply_text(
            "📝 **Set Filename Suffix**\n\n"
            "Send the text you want to add at the **end** of every filename (before extension).\n"
            "Example: `_1080p` → `video_1080p.mp4`\n\n"
            "Send d to disable suffix.",
            parse_mode="Markdown",
        )
        context.user_data["prompt_msg_id"] = msg.message_id
        context.user_data["awaiting"] = "us_suffix"
        context.user_data["awaiting_set_at"] = _time.time()
        logger.info(f"✅ Suffix prompt sent to {update.effective_user.id}")
    except Exception as e:
        logger.error(f"❌ Error in handle_us_suffix: {e}")
        await update.callback_query.answer("❌ Error", show_alert=True)


async def handle_us_thumbnail_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: show the current custom thumbnail as a full photo"""
    try:
        query = update.callback_query
        user_id = update.effective_user.id
        from bot.database import get_user

        user = await get_user(user_id)
        thumb = user.get("settings", {}).get("thumbnail_file_id")

        if not thumb:
            await query.answer("❌ No custom thumbnail found", show_alert=True)
            return

        await query.answer()
        # We send it as a new message so they can save/forward it
        await context.bot.send_photo(
            chat_id=user_id,
            photo=thumb,
            caption="🖼️ **Current Custom Thumbnail**",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error in handle_us_thumbnail_view: {e}")
        await query.answer("❌ Error viewing thumbnail", show_alert=True)


async def handle_us_thumbnail_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Callback: confirmation for thumbnail deletion"""
    try:
        query = update.callback_query
        await query.answer()

        text = "⚠️ **Delete Thumbnail?**\n\nAre you sure you want to remove your custom thumbnail?"
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Yes, Delete", callback_data="us_thumbnail_delete_confirm"
                ),
                InlineKeyboardButton(
                    "🔙 No, Keep", callback_data="us_thumbnail"
                ),  # Corrected callback data
            ]
        ]
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error in handle_us_thumbnail_delete: {e}")


async def handle_us_thumbnail_delete_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Callback: actual deletion of thumbnail"""
    try:
        query = update.callback_query
        user_id = update.effective_user.id

        await update_user(
            user_id, {"settings.thumbnail": "auto", "settings.thumbnail_file_id": None}
        )

        await query.answer("✅ Thumbnail deleted", show_alert=True)
        await log_user_update(context.bot, user_id, "deleted custom thumbnail")

        # Go back to settings (via command to refresh UI)
        await ussettings_command(update, context)
    except Exception as e:
        logger.error(f"Error in handle_us_thumbnail_delete_confirm: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# USER: Activate newly-created Rclone remote as default upload destination
# ─────────────────────────────────────────────────────────────────────────────


async def handle_us_rclone_dest_activate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Callback: us_set_rclone_dest_<remote_name>
    Sets the user's upload destination to the newly created Google Drive remote.
    """
    try:
        query = update.callback_query
        await query.answer()

        remote_name = query.data.replace("us_set_rclone_dest_", "", 1)
        user_id = update.effective_user.id

        await update_user(
            user_id,
            {
                "settings.destination_type": "rclone",
                "settings.destination_remote": remote_name,
            },
        )

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"\u2705 **Destination Set!**\n\n"
            f"All your future uploads will be sent to:\n"
            f"\u2601\ufe0f Google Drive \u2192 `{remote_name}`\n\n"
            f"You can change this any time from /ussettings \u2192 \ud83d\udcc1 Destination.",
            parse_mode="Markdown",
        )
        logger.info(f"\u2705 User {user_id} set rclone destination to {remote_name}")
    except Exception as e:
        logger.error(
            f"\u274c Error in handle_us_rclone_dest_activate: {e}", exc_info=True
        )
        try:
            await update.callback_query.answer("\u274c Error", show_alert=True)
        except Exception:
            pass


# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# ADMIN: Rclone Plan Toggle & Global Credentials
# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


async def handle_toggle_plan_rclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle rclone_allowed on/off for a plan. Callback: toggle_rclone_<PLAN>"""
    try:
        query = update.callback_query
        await query.answer()

        plan_name = query.data.replace("toggle_rclone_", "")
        config = await get_config() or {}
        plans = config.get("plans", {})
        plan_data = plans.get(plan_name, {})

        # Flip the flag
        current = plan_data.get("rclone_allowed", False)
        plan_data["rclone_allowed"] = not current
        plans[plan_name] = plan_data

        from bot.database import set_config

        await set_config({"plans": plans})

        icon = "✅" if plan_data["rclone_allowed"] else "❌"
        await query.answer(
            f"{icon} Rclone {'enabled' if plan_data['rclone_allowed'] else 'disabled'} for {plan_name.upper()}",
            show_alert=True,
        )

        # Refresh the plan edit page so toggle reflects new state
        await handle_edit_plan(update, context)
        logger.info(
            f"✅ Admin toggled rclone for plan {plan_name}: {plan_data['rclone_allowed']}"
        )

    except Exception as e:
        logger.error(f"❌ Error in handle_toggle_plan_rclone: {e}", exc_info=True)
        await update.callback_query.answer("❌ Error", show_alert=True)


async def handle_toggle_plan_shortener(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Toggle shortener_allowed on/off for a plan. Callback: toggle_shortener_<PLAN>"""
    try:
        query = update.callback_query
        await query.answer()

        plan_name = query.data.replace("toggle_shortener_", "")
        plans_config = await get_config("plans") or {}
        plan_data = plans_config.get(plan_name, {})

        current = plan_data.get("shortener_allowed", False)
        plan_data["shortener_allowed"] = not current
        plans_config[plan_name] = plan_data

        ok = await set_config({"plans": plans_config})

        icon = "✅" if plan_data["shortener_allowed"] else "❌"
        await query.answer(
            f"{icon} Shortener {'enabled' if plan_data['shortener_allowed'] else 'disabled'} for {plan_name.upper()}",
            show_alert=True,
        )
        await log_admin_action(
            update.effective_user.id,
            "toggled_plan_shortener",
            {"plan": plan_name, "enabled": plan_data["shortener_allowed"]},
        )

        await handle_edit_plan(update, context)
        logger.info(
            f"✅ Admin toggled shortener for plan {plan_name}: {plan_data['shortener_allowed']}"
        )

    except Exception as e:
        logger.error(f"❌ Error in handle_toggle_plan_shortener: {e}", exc_info=True)
        await update.callback_query.answer("❌ Error", show_alert=True)


# ─────────────────────────────────────────────────────────────────────────────
# USER: Manual Rclone Config Input Handlers (replaces Web OAuth)
# ─────────────────────────────────────────────────────────────────────────────


async def handle_user_rclone_setup_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Handle text inputs for the manual user-side Google Drive setup wizard."""
    try:
        text = (update.message.text or "").strip()
        user_id = update.effective_user.id
        awaiting = context.user_data.get("awaiting")

        if awaiting == "us_rclone_name":
            import re

            clean_name = re.sub(r"[^a-zA-Z0-9_\-]", "", text)
            if not clean_name:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Invalid name. Use letters and numbers.",
                    parse_mode="Markdown",
                )
                return
            context.user_data["temp_rclone_name"] = clean_name
            context.user_data["awaiting"] = "us_rclone_client_id"
            await update.message.reply_text(
                "**Step 2:** Please enter your Google **Client ID**.\n"
                "*(It usually ends with `apps.googleusercontent.com`)*\n\n"
                "Use /cancel to abort.",
                parse_mode="Markdown",
            )
            return

        if awaiting == "us_rclone_client_id":
            if not text or len(text) < 10 or "." not in text:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Invalid Client ID. Please try again or /cancel.",
                    parse_mode="Markdown",
                )
                return
            context.user_data["temp_rclone_client_id"] = text
            context.user_data["awaiting"] = "us_rclone_client_secret"
            await update.message.reply_text(
                "**Step 3:** Please enter your Google **Client Secret**.\n\n"
                "Use /cancel to abort.",
                parse_mode="Markdown",
            )
            return

        if awaiting == "us_rclone_client_secret":
            if not text or len(text) < 5:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Invalid Client Secret. Please try again or /cancel.",
                    parse_mode="Markdown",
                )
                return

            client_id = context.user_data.get("temp_rclone_client_id")
            client_secret = text
            remote_name = context.user_data.get("temp_rclone_name", f"user_{user_id}")

            import json
            import base64
            from urllib.parse import quote
            from config.config import get_config
            import config.settings as app_settings

            config = await get_config() or {}

            state_data = {
                "u": user_id,
                "i": client_id,
                "s": client_secret,
                "n": remote_name,
            }
            state = base64.urlsafe_b64encode(json.dumps(state_data).encode()).decode()

            base_url = (
                (config.get("webhook_url") or "")
                .rstrip("/")
                .replace("/webhook/telegram", "")
            )
            if not base_url:
                base_url = (
                    (app_settings.WEBHOOK_URL or "")
                    .rstrip("/")
                    .replace("/webhook/telegram", "")
                )

            redirect_uri = f"{base_url}/api/rclone/callback"

            auth_url = (
                "https://accounts.google.com/o/oauth2/v2/auth"
                f"?client_id={quote(client_id)}"
                f"&redirect_uri={quote(redirect_uri)}"
                "&response_type=code"
                "&scope=https://www.googleapis.com/auth/drive"
                "&access_type=offline"
                "&prompt=consent"
                f"&state={state}"
            )

            keyboard = [
                [InlineKeyboardButton("🔗 Authorize with Google Drive", url=auth_url)]
            ]
            await update.message.reply_text(
                "**Step 4:** Connect Google Drive\n\n"
                "Click the button below to open your browser and authorize the bot to access your Google Drive using your custom application.\n\n"
                "Once authorized, you will see a success page and the bot will notify you.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )

            # Clear text wizard state, OAuth server takes over
            context.user_data.pop("awaiting", None)
            context.user_data.pop("temp_rclone_client_id", None)
            context.user_data.pop("temp_rclone_name", None)
            logger.info(
                f"✅ User {user_id} generated Hybrid OAuth link with custom credentials"
            )
            return

    except Exception as e:
        logger.error(f"❌ Error in handle_user_rclone_setup_step: {e}", exc_info=True)
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            "❌ Error processing configuration. Press /cancel and try again.",
            parse_mode="Markdown",
        )
        context.user_data.pop("awaiting", None)
