import logging
import os
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
            get_ch_name("storage", "storage_channel_id")
        )

        # FIX: 'current_settings' was never defined. Build it here from config.
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
            f"🗄️ Storage Channel: `{storage_name}`"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Start Message", callback_data="edit_start_msg"),
             InlineKeyboardButton("💦 Watermark", callback_data="edit_watermark")],
            [InlineKeyboardButton("☎️ Support Contact", callback_data="edit_contact"),
             InlineKeyboardButton("📖 Help Text", callback_data="edit_help_text")],
            [InlineKeyboardButton("🏢 Site Name", callback_data="edit_site_name"),
             InlineKeyboardButton("📝 Description", callback_data="edit_site_desc")],
            [InlineKeyboardButton("🔗 Support Channel", callback_data="edit_support_channel"),
             InlineKeyboardButton("⚡ Parallel Limit", callback_data="edit_parallel")],
            [InlineKeyboardButton("📦 Max File Size", callback_data="edit_max_filesize"),
             InlineKeyboardButton("📅 File Expiry", callback_data="edit_file_expiry")],
            [InlineKeyboardButton("📢 Force Sub Channels", callback_data="admin_set_force_sub_channel")],
            [InlineKeyboardButton("📌 Set Log Channel", callback_data="admin_set_log_channel"),
             InlineKeyboardButton("💾 Set Dump Channel", callback_data="admin_set_dump_channel")],
            [InlineKeyboardButton("🗄️ Set Storage Channel", callback_data="admin_set_storage_channel")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
        ])

        await update.callback_query.message.edit_text(
            current_settings,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

        await log_admin_action(update.effective_user.id, "opened_config", {})
        logger.info("✅ Config menu opened")

    except Exception as e:
        logger.error(f"❌ Error in config menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_edit_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit /start command message"""
    try:
        await update.callback_query.message.reply_text(
            "💬 **Edit Start Message**\n\nSend the new /start message.\n\nSupports **Markdown** formatting.\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "edit_start_msg"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_edit_watermark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit watermark/caption text"""
    try:
        await update.callback_query.message.reply_text(
            "💦 **Edit Watermark Caption**\n\nSend the new watermark text.\n\nThis appears on downloaded files.\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "edit_watermark"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_edit_support_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit support contact details"""
    try:
        await update.callback_query.message.reply_text(
            "☎️ **Edit Support Contact**\n\nSend support details:\n- Email\n- Phone\n- Telegram ID\n- Support channel link\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "edit_contact"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_edit_help_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit help text"""
    try:
        await update.callback_query.message.reply_text(
            "📖 **Edit Help Text**\n\nSend the new help/guide text.\n\nShown when users click /help.\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "edit_help_text"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_edit_site_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit site name"""
    try:
        await update.callback_query.message.reply_text(
            "🏢 **Edit Site Name**\n\nSend your bot/service name:\n\nMax 50 characters\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "edit_site_name"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_edit_site_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit site description"""
    try:
        await update.callback_query.message.reply_text(
            "📝 **Edit Site Description**\n\nSend your bot/service description:\n\nMax 500 characters\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "edit_site_desc"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_edit_support_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit support channel link"""
    try:
        await update.callback_query.message.reply_text(
            "🔗 **Edit Support Channel**\n\nSend support channel link:\n\nFormat: `https://t.me/yourchannel`\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "edit_support_channel"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_edit_parallel_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit parallel processing limit"""
    try:
        config = await get_config() or {}
        current = config.get("parallel_global_limit", 5)
        await update.callback_query.message.reply_text(
            f"⚡ **Edit Parallel Processing Limit**\n\nCurrent: `{current}`\n\nSend new limit (1-50):\n\nHigher = faster but more resource usage\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "edit_parallel"
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
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "edit_max_filesize"
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
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "edit_file_expiry"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_edit_tos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit Terms of Service string"""
    try:
        await update.callback_query.message.reply_text(
            "📜 **Edit Terms of Service**\n\nSend the new Terms of Service text.\n\nYou can use HTML formatting tags like `<b>` and `<p>`.\n\nUse /cancel to abort.",
            parse_mode="Markdown"
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
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "edit_upgrade_text"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_edit_force_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for force sub management"""
    from bot.handlers.admin import handle_admin_set_force_sub_channel
    await handle_admin_set_force_sub_channel(update, context)

async def handle_edit_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit plan configuration"""
    try:
        query = update.callback_query
        await query.answer()

        plan_name = query.data.split("_")[-1]

        config = await get_config() or {}
        plans = config.get("plans", {})
        plan_data = plans.get(plan_name, {})

        # FIX: 'plan_text' was never defined. Build it here.
        plan_text = (
            f"⭐ **Edit {plan_name.upper()} Plan**\n\n"
            f"Price: ${plan_data.get('price', 0)}\n"
            f"Parallel: {plan_data.get('parallel', 1)}\n"
            f"Daily Limit: {plan_data.get('storage_per_day', 5)} GB\n"
            f"Expiry: {plan_data.get('dump_expiry_days', 0)} days"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Edit Price", callback_data=f"edit_price_{plan_name}")],
            [InlineKeyboardButton("⚡ Edit Parallel", callback_data=f"edit_plan_parallel_{plan_name}")],
            [InlineKeyboardButton("📦 Edit Daily Limit", callback_data=f"edit_daily_{plan_name}")],
            [InlineKeyboardButton("📅 Edit Expiry", callback_data=f"edit_expiry_{plan_name}")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_plans")]
        ])

        await query.message.edit_text(plan_text, reply_markup=keyboard, parse_mode="Markdown")
        await log_admin_action(update.effective_user.id, "opened_plan_edit", {"plan": plan_name})
        logger.info(f"✅ Plan edit menu opened for {plan_name}")

    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        try:
            await update.callback_query.answer(f"❌ Error", show_alert=True)
        except:
            pass

async def handle_config_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str):
    """Handle admin text input for all edit_* config state machine flows.
    Called from handle_text_input when awaiting starts with 'edit_' or is 'add_shortener'.
    """
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    # Map awaiting state → DB config key
    STATE_TO_CONFIG_KEY = {
        "edit_start_msg":       "start_message",
        "edit_watermark":       "watermark_text",
        "edit_contact":         "support_contact",
        "edit_help_text":       "help_text",
        "edit_site_name":       "site_name",
        "edit_site_desc":       "site_description",
        "edit_support_channel": "support_channel",
        "edit_parallel":        "parallel_limit",
        "edit_max_filesize":    "max_filesize_gb",
        "edit_file_expiry":     "file_expiry_days",
        "edit_tos":             "tos_text",
        "edit_upgrade_text":    "upgrade_text",
    }

    try:
        if state in STATE_TO_CONFIG_KEY:
            config_key = STATE_TO_CONFIG_KEY[state]

            # Type coercion for numeric fields
            if config_key in ("parallel_limit", "max_filesize_gb", "file_expiry_days"):
                try:
                    value = float(text)
                    if config_key in ("parallel_limit", "file_expiry_days"):
                        value = int(value)
                except ValueError:
                    await update.message.reply_text(
                        f"❌ **Invalid Value**\n\nPlease enter a number.",
                        parse_mode="Markdown"
                    )
                    return
            else:
                value = text

            from bot.database import set_config
            ok = await set_config({config_key: value})
            if ok:
                label = state.replace("edit_", "").replace("_", " ").title()
                await update.message.reply_text(
                    f"✅ **{label} Updated**\n\n"
                    f"New value: `{str(value)[:200]}`",
                    parse_mode="Markdown"
                )
                await log_admin_action(user_id, f"updated_config_{config_key}", {"value": str(value)[:100]})
            else:
                await update.message.reply_text("❌ Failed to save config. Please try again.")

        elif state == "add_shortener_api":
            if len(text) < 5:
                await update.message.reply_text("❌ API key seems too short. Try again.")
                return
            context.user_data["temp_shortener_api"] = text
            context.user_data["awaiting"] = "add_shortener_url"
            await update.message.reply_text(
                "🔗 **Step 2: Send Site Link**\n\n"
                "Example: `https://gplinks.com/`\n\n"
                "Use /cancel to abort.",
                parse_mode="Markdown"
            )
            return

        elif state == "add_shortener_url":
            api_key = context.user_data.pop("temp_shortener_api", None)
            if not api_key:
                await update.message.reply_text("❌ Missing API key. Please start over.")
                context.user_data.pop("awaiting", None)
                return

            domain = text.strip()
            if not domain.startswith("http"):
                domain = f"https://{domain}"

            from bot.database import set_config, get_config
            config = await get_config() or {}
            shorteners = config.get("shorteners", [])
            # Remove existing entry for same domain
            shorteners = [s for s in shorteners if s.get("domain") == domain]
            shorteners.append({"domain": domain, "api_key": api_key})
            ok = await set_config({"shorteners": shorteners})
            if ok:
                await update.message.reply_text(
                    f"✅ **Shortener Added**\n\nSite: `{domain}`",
                    parse_mode="Markdown"
                )
                await log_admin_action(user_id, "added_shortener", {"domain": domain})
            else:
                await update.message.reply_text("❌ Failed to save shortener.")
            
            context.user_data.pop("awaiting", None)

        else:
            logger.warning(f"Unhandled config edit state: {state}")
            await update.message.reply_text("❌ Unknown config state. Please try again from the admin menu.")
            context.user_data.pop("awaiting", None)

    except Exception as e:
        logger.error(f"❌ Error in handle_config_edit_input (state={state}): {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error saving config: {str(e)[:100]}")
        context.user_data.pop("awaiting", None)

async def handle_us_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user prefix settings"""
    try:
        user_id = update.effective_user.id
        text = update.message.text
        await update_user(user_id, {"prefix": text})
        await update.message.reply_text(f"✅ Prefix updated to: `{text}`", parse_mode="Markdown")
        context.user_data.pop("awaiting", None)
    except Exception as e:
        logger.error(f"❌ Error in handle_us_prefix: {e}")

async def handle_us_suffix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user suffix settings"""
    try:
        user_id = update.effective_user.id
        text = update.message.text
        await update_user(user_id, {"suffix": text})
        await update.message.reply_text(f"✅ Suffix updated to: `{text}`", parse_mode="Markdown")
        context.user_data.pop("awaiting", None)
    except Exception as e:
        logger.error(f"❌ Error in handle_us_suffix: {e}")

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /settings command - redirect to /ussettings"""
    try:
        await update.message.reply_text(
            "⚙️ **Settings**\n\n"
            "Use /ussettings to open your settings menu.",
            parse_mode="Markdown"
        )

        await log_info(f"✅ /settings used by {update.effective_user.id}")

    except Exception as e:
        logger.error(f"❌ Error in settings command: {e}", exc_info=True)
        await log_error(f"❌ Error in settings command: {str(e)}")
        await update.message.reply_text(
            "❌ Unable to open settings. Please try /ussettings instead.",
            parse_mode="Markdown"
        )

async def ussettings_command(update: Update, context: ContextTypes.DEFAULT_TYPE, photo_file_id: str = None) -> None:
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
            [InlineKeyboardButton("📁 Destination", callback_data="us_destination"),
                InlineKeyboardButton("👁️ Visibility", callback_data="us_visibility"),
            ],
            [InlineKeyboardButton("🗑️ Remove", callback_data="us_remove"),
             InlineKeyboardButton("📂 My Files", callback_data="us_myfiles")
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="us_close")],
        ]

        # Get user settings
        settings = user.get("settings", {})
        prefix = settings.get("prefix", "Not set")
        suffix = settings.get("suffix", "Not set")
        plan = user.get("plan", "free")

        prefix_display = f"{prefix[:10]}..." if len(prefix) > 10 else (prefix if prefix != "Not set" else "Not set")
        suffix_display = f"{suffix[:10]}..." if len(suffix) > 10 else (suffix if suffix != "Not set" else "Not set")

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
        if not show_photo_id and not is_callback and settings.get("thumbnail") == "custom":
            show_photo_id = settings.get("thumbnail_file_id")

        if show_photo_id:
            try:
                if is_callback:
                    await message.delete()
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=show_photo_id,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning(f"Could not send settings photo: {e}")
                # Fallback to text
                await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            if is_callback:
                try:
                    await message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
                except Exception:
                    await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

        logger.info(f"✅ Settings menu shown to {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in ussettings_command: {e}", exc_info=True)
        try:
            await update.effective_message.reply_text("❌ Error opening settings. Please try /ussettings again.")
        except:
            pass

async def handle_user_settings_text(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    
    try:
        user = await get_user(user_id)
        if not user:
            await update.message.reply_text("❌ User not found. Use /start first.")
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
                    parse_mode="Markdown"
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
                    parse_mode="Markdown"
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

        elif state in ("us_meta_title", "us_meta_author", "us_meta_year"):
            if len(text) > 100:
                await update.message.reply_text(
                    "❌ **Too Long**\n\n"
                    "Metadata field must be under 100 characters.",
                    parse_mode="Markdown"
                )
                return

            metadata = settings.get("metadata", {})
            tag_name = state.replace("us_meta_", "")
            metadata[tag_name] = text
            settings["metadata"] = metadata
            user["settings"] = settings
            
            await update_user(user_id, user)
            
            await update.message.reply_text(
                f"✅ **Default {tag_name.title()} Set**\n\n"
                f"{tag_name.title()}: `{text}`",
                parse_mode="Markdown"
            )
            
            logger.info(f"✅ User {user_id} set metadata {tag_name}: {text}")
        
# ============================================================
# HANDLE METADATA SUBTITLE
# ============================================================

        elif state == "us_meta_subtitle":
            text_lower = text.lower()
            
            if text_lower in ["none", "no", "disable", "off"]:
                metadata = settings.get("metadata", {})
                metadata["subtitle"] = "none"
                settings["metadata"] = metadata
                user["settings"] = settings
                await update_user(user_id, user)
                
                await update.message.reply_text(
                    "✅ **Subtitles Disabled**\n\n"
                    "Subtitles will not be processed.",
                    parse_mode="Markdown"
                )
            else:
                metadata = settings.get("metadata", {})
                metadata["subtitle"] = text
                settings["metadata"] = metadata
                user["settings"] = settings
                await update_user(user_id, user)
                
                await update.message.reply_text(
                    f"✅ **Subtitle Language Set**\n\n"
                    f"Subtitle: `{text}`\n\n"
                    f"Subtitles in this language will be processed.",
                    parse_mode="Markdown"
                )
            
            logger.info(f"✅ User {user_id} set subtitle: {text}")
        
# ============================================================
# HANDLE THUMBNAIL
# ============================================================

        elif state == "us_thumbnail":
            if text.lower() in ["auto", "automatic", "default"]:
                settings["thumbnail"] = "auto"
                user["settings"] = settings
                await update_user(user_id, user)
                
                await update.message.reply_text(
                    "✅ **Thumbnail Set to Auto**\n\n"
                    "Thumbnails will be generated automatically.",
                    parse_mode="Markdown"
                )
                
                logger.info(f"✅ User {user_id} set thumbnail to auto")
            else:
                await update.message.reply_text(
                    "❌ **Invalid Input**\n\n"
                    "Please send:\n"
                    "- An image (photo)\n"
                    "- Or type 'auto' for automatic thumbnails\n\n"
                    "Use /cancel to skip.",
                    parse_mode="Markdown"
                )
            
            logger.info(f"✅ User {user_id} added remove word: {text}")
        
        else:
            logger.warning(f"⚠️ Unknown user settings state: {state}")
            await update.message.reply_text(
                "❌ **Unknown Setting**\n\n"
                "Please try again from /ussettings menu.",
                parse_mode="Markdown"
            )
        
        # Clear awaiting state
        context.user_data.pop("awaiting", None)
        
    except Exception as e:
        logger.error(f"❌ Error handling user settings text: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ **Error Saving Settings**\n\n"
            f"Error: {str(e)[:100]}\n\n"
            f"Please try again or use /support.",
            parse_mode="Markdown"
        )
        context.user_data.pop("awaiting", None)

async def handle_us_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        user = await get_user(user_id)
        if not user:
            await query.message.reply_text("❌ User not found")
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

async def handle_us_mode_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        user = await get_user(user_id)
        if not user:
            await query.answer("❌ User not found", show_alert=True)
            return

        settings = user.get("settings", {})
        settings["mode"] = "video"
        user["settings"] = settings

        await update_user(user_id, user)

        await query.answer("✅ Mode set to VIDEO", show_alert=True)

        await log_user_update(context.bot, user_id, "set mode to video")
        logger.info(f"✅ User {user_id} set mode to VIDEO")

        # Return to settings menu
        await ussettings_command(update, context)

    except Exception as e:
        logger.error(f"❌ Error in handle_us_mode_video: {e}", exc_info=True)
        await query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_us_mode_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        user = await get_user(user_id)
        if not user:
            await query.answer("❌ User not found", show_alert=True)
            return

        settings = user.get("settings", {})
        settings["mode"] = "document"
        user["settings"] = settings

        await update_user(user_id, user)

        await query.answer("✅ Mode set to DOCUMENT", show_alert=True)

        await log_user_update(context.bot, user_id, "set mode to document")
        logger.info(f"✅ User {user_id} set mode to DOCUMENT")

        # Return to settings menu
        await ussettings_command(update, context)

    except Exception as e:
        logger.error(f"❌ Error in handle_us_mode_document: {e}", exc_info=True)
        await query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def go_back_to_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

async def handle_us_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: show prompt to set filename prefix"""
    try:
        query = update.callback_query
        await query.answer()
        await query.message.reply_text(
            "📝 **Set Filename Prefix**\n\n"
            "Send the text you want to add at the **beginning** of every filename.\n"
            "Example: `[HQ]` → `[HQ] video.mp4`\n\n"
            "Send d to disable prefix.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "us_prefix"
        logger.info(f"✅ Prefix prompt sent to {update.effective_user.id}")
    except Exception as e:
        logger.error(f"❌ Error in handle_us_prefix: {e}")
        await update.callback_query.answer("❌ Error", show_alert=True)

async def handle_us_suffix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: show prompt to set filename suffix"""
    try:
        query = update.callback_query
        await query.answer()
        await query.message.reply_text(
            "📝 **Set Filename Suffix**\n\n"
            "Send the text you want to add at the **end** of every filename (before extension).\n"
            "Example: `_1080p` → `video_1080p.mp4`\n\n"
            "Send d to disable suffix.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "us_suffix"
        logger.info(f"✅ Suffix prompt sent to {update.effective_user.id}")
    except Exception as e:
        logger.error(f"❌ Error in handle_us_suffix: {e}")
        await update.callback_query.answer("❌ Error", show_alert=True)