import json
import logging
import os
import sys
import time
from functools import wraps
from pathlib import Path
from contextlib import asynccontextmanager
import ipaddress

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

# ============================================
# LOGGING  (must be first)
# ============================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================
# PATH SETUP  (must happen before local imports)
# ============================================

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# ============================================
# LOCAL IMPORTS
# ============================================

from config.settings import get_settings, get_admin_ids, get_bot_token
from bot.database import init_db, close_db
from bot.middleware import error_handler, apply_ban_check

# ── User settings handlers ──────────────────────────────────────
from bot.handlers import (
    ussettings_command,
    settings_command,
    handle_us_metadata,
    handle_us_thumbnail,        # MessageHandler: receives the actual photo
    handle_us_thumbnail_menu,   # CallbackQueryHandler: shows prompt, sets awaiting
    handle_us_mode,
    handle_us_mode_video,
    handle_us_mode_document,
    handle_us_remove_confirm,
    handle_us_reset_confirm_yes,
    handle_us_myfiles,
    handle_us_plan,
    handle_us_prefix,
    handle_us_suffix,
    handle_us_visibility,
    handle_us_destination_button,
    go_back_to_settings,
    handle_subtitle_menu,
    handle_inject_sub,
    handle_meta_audio,
    handle_meta_video,
    handle_meta_subtitle,
    handle_rem_word,
    handle_rem_meta,
    handle_rem_inject,
    handle_callback_help,
    handle_callback_support,
)

# ── User command handlers ────────────────────────────────────────
from bot.handlers import (
    start_command,
    help_command,
    cancel_command,
    cancel_task_command,
    myfiles_command,
    stats_command,
    support_command,
    unknown_handler,
)

# ── Admin command + panel handlers ──────────────────────────────
from bot.handlers import (
    # Entry
    admin_command,
    handle_admin_back,

    # Main menu
    handle_admin_users,
    handle_admin_stats,
    show_config_menu,
    show_plans_menu,
    handle_admin_broadcast,
    handle_admin_rclone,
    handle_admin_filesize,
    handle_admin_bans,
    handle_admin_chatbox,
    handle_admin_terabox,
    handle_admin_logs,
    handle_admin_shorteners,

    # User management
    handle_admin_find_user,
    handle_admin_ban_user,
    handle_admin_unban_user,
    handle_admin_list_users,
    handle_admin_upgrade_user,
    show_banned_users,
    handle_unban_from_list,
    handle_view_user,

    # Broadcast
    handle_broadcast_compose,
    handle_broadcast_stats,
    handle_broadcast_cancel,

    # Rclone
    handle_admin_add_rclone,
    handle_admin_add_rclone_wizard,
    handle_list_rclone_remotes,
    handle_test_rclone,           # exported alias → handle_test_rclone_actual
    handle_disable_rclone,
    rclone_service_callback,
    rclone_plan_callback,
    rclone_users_callback,

    # Terabox
    handle_terabox_setup_key,
    handle_terabox_test,
    handle_terabox_stats,
    handle_terabox_disable,

    # File size / storage
    handle_set_max_filesize,
    handle_cleanup_old_files,
    handle_storage_stats,

    # Channel setup
    handle_admin_set_log_channel,
    handle_admin_set_dump_channel,
    handle_admin_set_storage_channel,
    handle_admin_set_force_sub_channel,

    # Force-sub management
    handle_admin_fsub_add,
    handle_admin_fsub_manage,
    handle_admin_fsub_toggle,
    handle_admin_fsub_link,
    handle_admin_fsub_remove_confirm,
    handle_admin_fsub_remove,

    # Channel removal
    handle_admin_remove_log,
    handle_admin_remove_dump,
    handle_admin_remove_storage,

    # Config edit fields
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
    handle_edit_plan,           # ← was missing (NameError on startup)

    # Forward handler (used in handle_forward_routing below)
    handle_admin_forwards,
    handle_user_destination_forward,
    admin_check_and_open,
)

# ── File, text, rclone, terabox, callback handlers / Wizard ──────
from bot.handlers import (
    handle_admin_input,
    handle_file_upload,
    handle_url_input,
    handle_text_input,
    callback_handler,
    rclone_command,
    rclone_text_input,
    terabox_command,
    terabox_text_input,
    WizardHandler,
)

# ── Web routers ─────────────────────────────────────────────────
from web.routes.auth import router as auth_router
from web.routes.admin_dashboard import router as dashboard_router
from web.routes.admin_users import router as users_router
from web.routes.admin_config import router as config_router
from web.routes.public import router as public_router

# ============================================
# SETTINGS & GLOBAL STATE
# ============================================

settings = get_settings()
bot_application: Application | None = None

# ============================================
# ENVIRONMENT HELPERS
# ============================================

def deduce_webhook_url() -> str:
    """Deduce the public base URL from platform environment variables."""
    if url := os.getenv("RENDER_EXTERNAL_URL"):
        return url
    if app_name := os.getenv("HEROKU_APP_NAME"):
        return f"https://{app_name}.herokuapp.com"
    if domain := os.getenv("RAILWAY_PUBLIC_DOMAIN"):
        return f"https://{domain}"
    if domain := os.getenv("KOYEB_PUBLIC_DOMAIN"):
        return f"https://{domain}"
    if domain := os.getenv("VERCEL_URL"):
        return f"https://{domain}"
    return ""


def validate_environment() -> None:
    """
    Validate required env vars.
    Logs problems but does NOT raise — server must bind its port regardless.
    """
    if not settings.WEBHOOK_URL:
        deduced = deduce_webhook_url()
        if deduced:
            settings.WEBHOOK_URL = deduced.rstrip("/") + "/webhook/telegram"
            logger.info(f"🔍 Deduced Webhook URL: {settings.WEBHOOK_URL}")

    required_vars = ["BOT_TOKEN", "MONGODB_URI"]
    missing = [v for v in required_vars if not getattr(settings, v, None)]
    if missing:
        # Log as critical but do NOT raise — let FastAPI bind the port first
        logger.critical(f"🚨 Missing required env vars: {', '.join(missing)}")
        return

    if not settings.WEBHOOK_URL:
        logger.warning("⚠️ WEBHOOK_URL not set — webhook cannot be auto-configured.")
    elif not settings.WEBHOOK_URL.startswith("https://"):
        logger.warning("⚠️ WEBHOOK_URL is not HTTPS — Telegram may reject it.")

    logger.info("✅ Environment validated")


def validate_production_secrets() -> None:
    """
    In production, require ENCRYPTION_KEY and JWT_SECRET to be explicitly set.
    Called only from __main__ CLI path (not from FastAPI startup).
    """
    is_production = os.getenv("ENVIRONMENT", "development").lower() == "production"
    if not is_production:
        logger.info("ℹ️ Development mode — secret validation skipped")
        return

    missing = []
    if not os.getenv("ENCRYPTION_KEY"):
        missing.append("ENCRYPTION_KEY")
    if not os.getenv("JWT_SECRET"):
        missing.append("JWT_SECRET")

    if missing:
        logger.critical(
            f"🚨 CRITICAL: Missing production secrets: {', '.join(missing)}\n"
            f"Auto-generated secrets will invalidate all sessions and encrypted data on restart!"
        )
        sys.exit(1)

    logger.info("✅ Production secrets validated")

# ============================================
# RATE LIMITER DECORATOR
# ============================================

def rate_limit_handler(max_calls: int = 10, time_window: int = 60):
    """Per-user rate limiter using real epoch time."""
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id
            now = time.time()
            last_reset = context.bot_data.get(f"rate_{user_id}_time", 0.0)
            count = context.bot_data.get(f"rate_{user_id}_count", 0)

            if (now - last_reset) > time_window:
                count = 0
                context.bot_data[f"rate_{user_id}_time"] = now
                context.bot_data[f"rate_{user_id}_count"] = 0

            if count >= max_calls:
                logger.warning(f"⚠️ Rate limit exceeded for user {user_id}")
                await update.message.reply_text("⏱️ Too many requests. Please wait.")
                return

            context.bot_data[f"rate_{user_id}_count"] = count + 1
            return await func(update, context)
        return wrapper
    return decorator

# ============================================
# FORWARD ROUTER  (defined here, not in handlers)
# ============================================

async def handle_forward_routing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Route forwarded messages:
      Route 1 — Admin + awaiting_channel_type  → global channel setup
      Route 2 — User  + awaiting=us_destination → user destination channel
      Route 3 — No match                        → helpful feedback message
    """
    try:
        user_id = update.effective_user.id
        msg = update.message
        admin_ids = get_admin_ids()

        forward_origin = msg.forward_origin
        if forward_origin and hasattr(forward_origin, "chat"):
            forward_info = f"from {forward_origin.chat.title}"
        elif forward_origin:
            forward_info = f"type: {type(forward_origin).__name__}"
        else:
            forward_info = "no forward info"

        logger.info(f"📨 Forward received: user={user_id}, {forward_info}")

        # ── Route 1: Admin global channel setup ──
        is_admin = user_id in admin_ids
        channel_type = context.user_data.get("awaiting_channel_type", "")

        if is_admin and channel_type:
            logger.info(f"🔀 Admin forward → channel setup: {channel_type}")
            try:
                await handle_admin_forwards(update, context)
                logger.info(f"✅ Admin forward handled: {channel_type}")
            except Exception as e:
                logger.error(f"❌ Admin forward handler failed: {e}", exc_info=True)
                await msg.reply_text(
                    "❌ **Setup Failed**\n\nCould not process channel. Please try again.",
                    parse_mode="Markdown",
                )
            return

        # ── Route 2: User destination channel ──
        awaiting = context.user_data.get("awaiting")
        if awaiting == "us_destination":
            logger.info(f"🔀 User forward → destination setup: {user_id}")
            try:
                await handle_user_destination_forward(update, context)
                logger.info("✅ User destination forward handled")
            except Exception as e:
                logger.error(f"❌ User destination handler failed: {e}", exc_info=True)
                await msg.reply_text(
                    "❌ **Setup Failed**\n\nCould not set destination. Use /cancel to abort.",
                    parse_mode="Markdown",
                )
            return

        # ── Route 3: No matching state ──
        logger.warning(
            f"⚠️ Unhandled forward | user={user_id} | "
            f"admin_state={channel_type or 'None'} | user_state={awaiting or 'None'}"
        )
        await msg.reply_text(
            "ℹ️ **Forwarded Message Received**\n\n"
            "I'm not currently expecting a forwarded channel.\n\n"
            "• Admin: /admin → ⚙️ Config\n"
            "• User: /ussettings → 🎯 Destination\n\n"
            "Then forward your channel message.",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"❌ Critical error in forward routing: {e}", exc_info=True)
        try:
            await update.message.reply_text("❌ An unexpected error occurred. Please try again.")
        except Exception:
            pass

# ============================================
# HANDLER REGISTRATION
# ============================================

def setup_handlers(application: Application) -> None:
    """Register ALL bot handlers in correct priority order."""
    try:
        logger.info("🔧 Registering bot handlers...")

        # ===================================================================
        # 0. GLOBAL MIDDLEWARE (Group -1)
        # ===================================================================
        application.add_handler(TypeHandler(Update, apply_ban_check), group=-1)

        # ===================================================================
        # 1. COMMAND HANDLERS
        # ===================================================================
        application.add_handler(CommandHandler("start",      start_command))
        application.add_handler(CommandHandler("help",       help_command))
        application.add_handler(CommandHandler("stats",      stats_command))
        application.add_handler(CommandHandler("myfiles",    myfiles_command))
        application.add_handler(CommandHandler("support",    support_command))
        application.add_handler(CommandHandler("cancel",     cancel_command))
        application.add_handler(CommandHandler("settings",   settings_command))
        application.add_handler(CommandHandler("ussettings", ussettings_command))
        application.add_handler(CommandHandler("admin",      admin_command,  filters=filters.ChatType.PRIVATE))
        application.add_handler(CommandHandler("rclone",     rclone_command))
        application.add_handler(CommandHandler("terabox",    terabox_command))

        # /cancel_<taskid> pattern
        application.add_handler(MessageHandler(
            filters.Regex(r"^/cancel_\S+") & filters.TEXT & filters.ChatType.PRIVATE,
            cancel_task_command,
        ))

        # ===================================================================
        # 2. FORWARDED MESSAGES
        # ===================================================================
        application.add_handler(MessageHandler(
            filters.FORWARDED & filters.ChatType.PRIVATE,
            handle_forward_routing,
        ))
        logger.info("✅ Forward router registered")

        # ===================================================================
        # 3. FILE & PHOTO HANDLERS
        # ===================================================================
        application.add_handler(MessageHandler(
            (filters.Document.ALL | filters.VIDEO | filters.AUDIO) & filters.ChatType.PRIVATE,
            handle_file_upload,
        ))
        # Photo handler — receives thumbnail image when awaiting == "us_thumbnail"
        application.add_handler(MessageHandler(
            filters.PHOTO & filters.ChatType.PRIVATE,
            handle_us_thumbnail,
        ))

        # ===================================================================
        # 4. USER SETTINGS CALLBACKS
        # ===================================================================
        application.add_handler(CallbackQueryHandler(handle_us_metadata,         pattern="^us_metadata$"))
        application.add_handler(CallbackQueryHandler(handle_us_thumbnail_menu,    pattern="^us_thumbnail$"))  # shows prompt
        application.add_handler(CallbackQueryHandler(handle_us_mode,             pattern="^us_mode$"))
        application.add_handler(CallbackQueryHandler(handle_us_mode_video,       pattern="^us_mode_video$"))
        application.add_handler(CallbackQueryHandler(handle_us_mode_document,    pattern="^us_mode_document$"))
        application.add_handler(CallbackQueryHandler(handle_us_remove_confirm,   pattern="^us_remove_confirm$"))
        application.add_handler(CallbackQueryHandler(handle_us_reset_confirm_yes,pattern="^us_reset_confirm_yes$"))
        application.add_handler(CallbackQueryHandler(handle_us_myfiles,          pattern="^us_myfiles$"))
        application.add_handler(CallbackQueryHandler(handle_us_plan,             pattern="^us_plan$"))
        application.add_handler(CallbackQueryHandler(handle_us_prefix,           pattern="^us_prefix$"))
        application.add_handler(CallbackQueryHandler(handle_us_suffix,           pattern="^us_suffix$"))
        application.add_handler(CallbackQueryHandler(handle_us_visibility,       pattern="^us_visibility$"))
        application.add_handler(CallbackQueryHandler(handle_us_destination_button, pattern="^us_destination$"))
        application.add_handler(CallbackQueryHandler(go_back_to_settings,        pattern="^us_back$"))
        application.add_handler(CallbackQueryHandler(handle_callback_help,       pattern="^us_help$"))
        application.add_handler(CallbackQueryHandler(handle_callback_support,    pattern="^us_support$"))
        application.add_handler(CallbackQueryHandler(stats_command,              pattern="^us_stats$"))
        application.add_handler(CallbackQueryHandler(ussettings_command,         pattern="^us_settings$"))

        # Metadata sub-menu
        application.add_handler(CallbackQueryHandler(handle_subtitle_menu, pattern="^subtitle_menu$"))
        application.add_handler(CallbackQueryHandler(handle_inject_sub,    pattern="^inject_sub$"))
        application.add_handler(CallbackQueryHandler(handle_meta_audio,    pattern="^meta_audio$"))
        application.add_handler(CallbackQueryHandler(handle_meta_video,    pattern="^meta_video$"))
        application.add_handler(CallbackQueryHandler(handle_meta_subtitle, pattern="^meta_subtitle$"))

        # Remove menu
        application.add_handler(CallbackQueryHandler(handle_rem_word,   pattern="^rem_word$"))
        application.add_handler(CallbackQueryHandler(handle_rem_meta,   pattern="^rem_meta$"))
        application.add_handler(CallbackQueryHandler(handle_rem_inject, pattern="^rem_inject$"))

        # Wizard (file editor flow — wiz_* callbacks)
        application.add_handler(CallbackQueryHandler(WizardHandler.handle_callback, pattern="^wiz_"))

        # No-op (ignore button — used in track lists when no tracks available)
        async def _noop(u, c): await u.callback_query.answer()
        application.add_handler(CallbackQueryHandler(_noop, pattern="^ignore$"))

        # ===================================================================
        # 5. ADMIN PANEL — MAIN MENU
        # ===================================================================
        application.add_handler(CallbackQueryHandler(handle_admin_back,        pattern="^admin_back$"))
        application.add_handler(CallbackQueryHandler(handle_admin_users,       pattern="^admin_users$"))
        application.add_handler(CallbackQueryHandler(handle_admin_stats,       pattern="^admin_stats$"))
        application.add_handler(CallbackQueryHandler(show_config_menu,         pattern="^admin_config$"))
        application.add_handler(CallbackQueryHandler(show_plans_menu,          pattern="^admin_plans$"))
        application.add_handler(CallbackQueryHandler(handle_admin_broadcast,   pattern="^admin_broadcast$"))
        application.add_handler(CallbackQueryHandler(handle_admin_rclone,      pattern="^admin_rclone$"))
        application.add_handler(CallbackQueryHandler(handle_admin_filesize,    pattern="^admin_filesize$"))
        application.add_handler(CallbackQueryHandler(handle_admin_bans,        pattern="^admin_bans$"))
        application.add_handler(CallbackQueryHandler(handle_admin_chatbox,     pattern="^admin_chatbox$"))
        application.add_handler(CallbackQueryHandler(handle_admin_terabox,     pattern="^admin_terabox$"))
        application.add_handler(CallbackQueryHandler(handle_admin_logs,        pattern="^admin_logs$"))
        application.add_handler(CallbackQueryHandler(handle_admin_logs,        pattern="^view_logs_"))
        application.add_handler(CallbackQueryHandler(handle_admin_shorteners,  pattern="^admin_shorteners$"))
        application.add_handler(CallbackQueryHandler(handle_admin_shorteners,  pattern="^add_shortener$"))

        # ===================================================================
        # 6. ADMIN — CONFIG EDIT BUTTONS
        # ===================================================================
        application.add_handler(CallbackQueryHandler(handle_edit_start_message,    pattern="^edit_start_msg$"))
        application.add_handler(CallbackQueryHandler(handle_edit_watermark,        pattern="^edit_watermark$"))
        application.add_handler(CallbackQueryHandler(handle_edit_support_contact,  pattern="^edit_contact$"))
        application.add_handler(CallbackQueryHandler(handle_edit_help_text,        pattern="^edit_help_text$"))
        application.add_handler(CallbackQueryHandler(handle_edit_site_name,        pattern="^edit_site_name$"))
        application.add_handler(CallbackQueryHandler(handle_edit_site_description, pattern="^edit_site_desc$"))
        application.add_handler(CallbackQueryHandler(handle_edit_support_channel,  pattern="^edit_support_channel$"))
        application.add_handler(CallbackQueryHandler(handle_edit_parallel_limit,   pattern="^edit_parallel$"))
        application.add_handler(CallbackQueryHandler(handle_edit_max_filesize,     pattern="^edit_max_filesize$"))
        application.add_handler(CallbackQueryHandler(handle_edit_file_expiry,      pattern="^edit_file_expiry$"))

        # ===================================================================
        # 7. ADMIN — USER MANAGEMENT
        # ===================================================================
        application.add_handler(CallbackQueryHandler(handle_admin_find_user,    pattern="^admin_find_user$"))
        application.add_handler(CallbackQueryHandler(handle_admin_ban_user,     pattern="^admin_ban_user$"))
        application.add_handler(CallbackQueryHandler(handle_admin_unban_user,   pattern="^admin_unban_user$"))
        application.add_handler(CallbackQueryHandler(handle_admin_upgrade_user, pattern="^admin_upgrade_user$"))
        application.add_handler(CallbackQueryHandler(handle_admin_list_users,   pattern="^admin_list_users_"))
        application.add_handler(CallbackQueryHandler(handle_view_user,          pattern="^view_user_"))
        application.add_handler(CallbackQueryHandler(show_banned_users,         pattern="^banned_page_"))
        application.add_handler(CallbackQueryHandler(handle_unban_from_list,    pattern="^unban_user_"))

        # ===================================================================
        # 8. ADMIN — BROADCAST
        # ===================================================================
        application.add_handler(CallbackQueryHandler(handle_broadcast_compose, pattern="^broadcast_compose$"))
        application.add_handler(CallbackQueryHandler(handle_broadcast_stats,   pattern="^broadcast_stats$"))
        application.add_handler(CallbackQueryHandler(handle_broadcast_cancel,  pattern="^broadcast_cancel_input$"))
        application.add_handler(CallbackQueryHandler(handle_admin_broadcast,   pattern="^broadcast_pending$"))

        # ===================================================================
        # 9. ADMIN — RCLONE
        # ===================================================================
        application.add_handler(CallbackQueryHandler(handle_admin_add_rclone,        pattern="^admin_add_rclone$"))
        application.add_handler(CallbackQueryHandler(handle_admin_add_rclone_wizard, pattern="^admin_add_rclone_wizard$"))
        application.add_handler(CallbackQueryHandler(handle_list_rclone_remotes,     pattern="^list_rclone_remotes$"))
        application.add_handler(CallbackQueryHandler(handle_test_rclone,             pattern="^test_rclone$"))
        application.add_handler(CallbackQueryHandler(handle_disable_rclone,          pattern="^disable_rclone$"))
        application.add_handler(CallbackQueryHandler(
            rclone_service_callback,
            pattern="^rclone_(gdrive|onedrive|dropbox|mega|terabox|custom)$",
        ))
        application.add_handler(CallbackQueryHandler(rclone_plan_callback,  pattern="^rclone_plan_(free|pro)$"))
        application.add_handler(CallbackQueryHandler(rclone_users_callback, pattern="^rclone_users_"))

        # ===================================================================
        # 10. ADMIN — TERABOX
        # ===================================================================
        application.add_handler(CallbackQueryHandler(handle_terabox_setup_key, pattern="^terabox_setup_key$"))
        application.add_handler(CallbackQueryHandler(handle_terabox_test,      pattern="^terabox_test$"))
        application.add_handler(CallbackQueryHandler(handle_terabox_stats,     pattern="^terabox_stats$"))
        application.add_handler(CallbackQueryHandler(handle_terabox_disable,   pattern="^terabox_disable$"))

        # ===================================================================
        # 11. ADMIN — FILE SIZE & STORAGE
        # ===================================================================
        application.add_handler(CallbackQueryHandler(handle_set_max_filesize,  pattern="^set_max_filesize$"))
        application.add_handler(CallbackQueryHandler(handle_cleanup_old_files, pattern="^cleanup_old_files$"))
        application.add_handler(CallbackQueryHandler(handle_storage_stats,     pattern="^storage_stats$"))

        # ===================================================================
        # 12. ADMIN — PLANS
        # ===================================================================
        # BUG-9 FIX: edit_plan_free/premium were routed to show_plans_menu (wrong)
        application.add_handler(CallbackQueryHandler(handle_edit_plan, pattern="^edit_plan_free$"))
        application.add_handler(CallbackQueryHandler(handle_edit_plan, pattern="^edit_plan_premium$"))

        # ===================================================================
        # 13. ADMIN — CHANNEL SETUP
        # ===================================================================
        application.add_handler(CallbackQueryHandler(handle_admin_set_log_channel,        pattern="^admin_set_log_channel$"))
        application.add_handler(CallbackQueryHandler(handle_admin_set_dump_channel,       pattern="^admin_set_dump_channel$"))
        application.add_handler(CallbackQueryHandler(handle_admin_set_storage_channel,    pattern="^admin_set_storage_channel$"))
        application.add_handler(CallbackQueryHandler(handle_admin_set_force_sub_channel,  pattern="^admin_set_force_sub_channel$"))
        application.add_handler(CallbackQueryHandler(admin_check_and_open,                pattern="^admin_check_and_open$"))

        # ===================================================================
        # 14. ADMIN — FORCE-SUB MANAGEMENT
        # ===================================================================
        application.add_handler(CallbackQueryHandler(handle_admin_fsub_add,            pattern="^admin_fsub_add$"))
        application.add_handler(CallbackQueryHandler(handle_admin_fsub_manage,         pattern="^admin_fsub_manage_"))
        application.add_handler(CallbackQueryHandler(handle_admin_fsub_toggle,         pattern="^admin_fsub_toggle_"))
        application.add_handler(CallbackQueryHandler(handle_admin_fsub_link,           pattern="^admin_fsub_link_"))
        application.add_handler(CallbackQueryHandler(handle_admin_fsub_remove_confirm, pattern="^admin_fsub_remove_confirm_"))
        application.add_handler(CallbackQueryHandler(handle_admin_fsub_remove,         pattern="^admin_fsub_remove_"))

        # ===================================================================
        # 15. ADMIN — CHANNEL REMOVAL
        # ===================================================================
        application.add_handler(CallbackQueryHandler(handle_admin_remove_log,     pattern="^admin_remove_log$"))
        application.add_handler(CallbackQueryHandler(handle_admin_remove_dump,    pattern="^admin_remove_dump$"))
        application.add_handler(CallbackQueryHandler(handle_admin_remove_storage, pattern="^admin_remove_storage$"))

        # ===================================================================
        # 16. TEXT INPUT  (awaiting states — must come after specific callbacks)
        # ===================================================================
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_text_input,
        ))

        # ===================================================================
        # 17. FALLBACK HANDLERS  — MUST BE LAST
        # ===================================================================
        application.add_handler(CallbackQueryHandler(callback_handler))
        application.add_handler(MessageHandler(filters.COMMAND, unknown_handler))
        application.add_error_handler(error_handler)

        logger.info("✅ All handlers registered successfully")

    except Exception as e:
        logger.error(f"❌ CRITICAL: Handler registration failed: {e}", exc_info=True)
        raise

# ============================================
# BOT APPLICATION BUILDER
# ============================================

async def build_bot_application() -> Application:
    """Create and fully initialize PTB Application for webhook mode."""
    logger.info("🤖 Building Telegram Application (webhook mode)...")

    try:
        logger.info("📡 Initializing database...")
        db = await init_db()
        logger.info("✅ Database initialized")

        application = (
            Application.builder()
            .token(get_bot_token())
            .connect_timeout(10.0)
            .read_timeout(10.0)
            .write_timeout(10.0)
            .pool_timeout(10.0)
            .build()
        )

        await application.initialize()
        await application.start()

        # Share state across all handlers
        application.bot_data["admin_ids"]    = settings.ADMIN_IDS
        application.bot_data["dump_channel"] = settings.DUMP_CHANNEL_ID
        application.bot_data["db"]           = db
        application.bot_data["settings"]     = settings

        setup_handlers(application)

        logger.info("✅ Bot application built | webhook: %s", settings.WEBHOOK_URL)
        return application

    except Exception as e:
        logger.error(f"❌ Failed to build bot application: {e}", exc_info=True)
        raise


async def cleanup_bot_application(application: Application) -> None:
    """Gracefully stop PTB app and close DB."""
    logger.info("🛑 Cleaning up bot application...")
    try:
        await application.stop()
        await application.shutdown()
        db = application.bot_data.get("db")
        if db is not None:
            await close_db()
        logger.info("✅ Bot cleaned up")
    except Exception as e:
        logger.error("❌ Cleanup error: %s", e, exc_info=True)

# ============================================
# FASTAPI LIFESPAN
# ============================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Initialize bot, start queue worker, configure Telegram webhook.
    """
    global bot_application
    logger.info("🚀 FastAPI startup — initializing Telegram bot")

    try:
        validate_environment()

        try:
            bot_application = await build_bot_application()
        except Exception as e:
            logger.error(f"❌ Bot build failed: {e}", exc_info=True)
            logger.warning("⚠️ Bot unavailable — server continuing in degraded mode")
        else:
            # Start task queue worker
            try:
                from bot.services import QueueWorker
                queue_worker = QueueWorker(bot_application.bot)
                await queue_worker.start()
                bot_application.bot_data["queue_worker"] = queue_worker
                logger.info("✅ Queue worker started")
            except Exception as e:
                logger.error(f"❌ Queue worker failed to start: {e}", exc_info=True)

            # Expose bot to FastAPI state
            app.state.bot = bot_application.bot

            # Fetch and cache bot identity
            try:
                bot_me = await bot_application.bot.get_me()
                settings.BOT_USERNAME = bot_me.username
                settings.BOT_LINK = f"https://t.me/{bot_me.username}"
                logger.info(f"🤖 Bot: @{settings.BOT_USERNAME} | {settings.BOT_LINK}")
            except Exception as e:
                logger.warning(f"⚠️ Could not fetch bot identity: {e}")

            # Configure Telegram webhook asynchronously
            try:
                bot_token   = get_bot_token()
                webhook_url = (settings.WEBHOOK_URL or "").strip()

                if not bot_token or not webhook_url:
                    logger.warning("⚠️ BOT_TOKEN or WEBHOOK_URL missing — entering long-poll mode.")
                    try:
                        async with httpx.AsyncClient() as client:
                            del_resp = await client.post(
                                f"https://api.telegram.org/bot{bot_token}/deleteWebhook",
                                json={"drop_pending_updates": False},
                                timeout=10,
                            )
                        logger.info(f"🔧 deleteWebhook → {del_resp.status_code}: {del_resp.text}")
                    except Exception as dw_err:
                        logger.warning(f"⚠️ deleteWebhook failed: {dw_err}")

                    import asyncio
                    async def _poll_forever():
                        offset = 0
                        logger.info("📡 Long-poll loop started")
                        while True:
                            try:
                                poll_resp = await bot_application.bot.get_updates(
                                    offset=offset, timeout=30,
                                    allowed_updates=["message", "callback_query", "inline_query"],
                                )
                                for upd in poll_resp:
                                    await bot_application.process_update(upd)
                                    offset = upd.update_id + 1
                            except Exception as poll_err:
                                logger.warning(f"⚠️ Poll error: {poll_err}")
                                await asyncio.sleep(3)

                    asyncio.create_task(_poll_forever())
                    logger.info("✅ Long-poll task created")
                else:
                    api_get = f"https://api.telegram.org/bot{bot_token}/getWebhookInfo"
                    async with httpx.AsyncClient() as client:
                        r = await client.get(api_get, timeout=10)
                        info = r.json()
                        current_url = info.get("result", {}).get("url", "")
                        current_max = info.get("result", {}).get("max_connections", 40)

                    db = bot_application.bot_data.get("db")
                    user_count = await db.users.count_documents({}) if db is not None else 0
                    extra_conns  = (user_count // 1000) * 20
                    needed_max   = min(100, 40 + extra_conns)
                    url_changed  = current_url != webhook_url
                    scale_needed = needed_max > current_max

                    if url_changed or scale_needed:
                        reasons = ["URL mismatch"] if url_changed else []
                        if scale_needed: reasons.append(f"connections {current_max}→{needed_max}")
                        logger.info(f"🔄 Updating webhook ({', '.join(reasons)})\n🔗 URL: {webhook_url}")
                        
                        api_set = f"https://api.telegram.org/bot{bot_token}/setWebhook"
                        payload = {
                            "url": webhook_url,
                            "drop_pending_updates": False,
                            "allowed_updates": ["message", "callback_query", "inline_query"],
                            "max_connections": needed_max,
                            "secret_token": settings.WEBHOOK_SECRET,
                        }
                        async with httpx.AsyncClient() as client:
                            r2 = await client.post(api_set, json=payload, timeout=15)
                        logger.info(f"SetWebhook → {r2.status_code}: {r2.text}")
                    else:
                        logger.info(f"✅ Webhook already correct: {webhook_url}")

            except Exception as e:
                logger.error(f"❌ Webhook setup failed: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"❌ Startup error: {e}", exc_info=True)
    
    yield  # Yield execution to FastAPI running state

    logger.info("🛑 FastAPI shutdown — cleaning up")
    if bot_application is not None:
        worker = bot_application.bot_data.get("queue_worker")
        if worker:
            await worker.stop()
        await cleanup_bot_application(bot_application)

# Override FastAPI app creation to use lifespan
app = FastAPI(
    title="FileBot",
    version="1.0",
    description="Telegram FileBot + Web Admin + Public API",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan
)

# ============================================
# CORS
# ============================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://localhost:3000",
        "https://localhost:8080",
        os.getenv("FRONTEND_URL", "https://ccs-ffmpeg-bot.onrender.com"),
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# ============================================
# STATIC FILES
# ============================================

static_dir = project_root / "web" / "static"

if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    logger.info(f"✅ Static files mounted: {static_dir}")
else:
    logger.warning(f"⚠️ Static directory not found: {static_dir}")

# ============================================
# WEB ROUTERS
# ============================================

app.include_router(auth_router,      prefix="/api/auth",  tags=["Authentication"])
app.include_router(dashboard_router, prefix="/api/admin", tags=["Admin Dashboard"])
app.include_router(users_router,     prefix="/api/admin", tags=["Admin Users"])
app.include_router(config_router,    prefix="/api/admin", tags=["Admin Config"])
app.include_router(public_router,                         tags=["Public"])

# Startup and Shutdown logic are now managed by the FastAPI lifespan manager (defined above).

# ============================================
# TELEGRAM WEBHOOK ENDPOINT
# ============================================

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """
    Receive Telegram updates with strict security enforcement.
    """
    try:
        # Enforce WEBHOOK_SECRET via X-Telegram-Bot-Api-Secret-Token header
        if settings.WEBHOOK_SECRET:
            secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if secret != settings.WEBHOOK_SECRET:
                logger.warning("🚨 Unauthorized webhook access attempt (Invalid Secret)")
                return JSONResponse({"status": "forbidden"}, status_code=403)

        # Enforce Telegram IP Allowlist Check
        # Using Telegram's known CIDR blocks: 149.154.160.0/20 and 91.108.4.0/22
        client_ip = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
        try:
            ip = ipaddress.ip_address(client_ip)
            is_valid_ip = any(
                ip in ipaddress.ip_network(cidr) 
                for cidr in ["149.154.160.0/20", "91.108.4.0/22"]
            )
            # If IPv6 or outside standard scope, log it but rely heavily on Secret Token
            # Still strictly rejecting clear mismatches
            if not is_valid_ip and settings.ENVIRONMENT.lower() == "production":
                logger.warning(f"🚨 Webhook request from untrusted IP: {client_ip}")
                return JSONResponse({"status": "forbidden"}, status_code=403)
        except ValueError:
            logger.warning(f"🚨 Invalid IP address on webhook request: {client_ip}")
            return JSONResponse({"status": "forbidden"}, status_code=403)

        if bot_application is None:
            logger.error("❌ Webhook hit but bot_application is None (startup failed?)")
            return JSONResponse({"status": "error", "detail": "Bot not initialized"}, status_code=503)

        data = await request.json()
        logger.info("📨 Webhook update received") # Sanitized log output

        if not bot_application.running:
            logger.error("❌ bot_application.running=False — attempting restart")
            await bot_application.start()

        update = Update.de_json(data, bot_application.bot)
        await bot_application.process_update(update)

        return {"status": "ok"}

    except Exception as e:
        logger.error("Webhook error: %s", e, exc_info=True)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

# ============================================
# STREAMING ENDPOINT  (dump channel → browser)
# ============================================

@app.get("/stream/{file_id}")
async def stream_telegram_file(file_id: str, request: Request):
    """
    Stream a Telegram file directly to the browser using its file_id.
    Harden endpoint: only authenticated requests or valid signed links should stream files.
    For now, stripping BOT_TOKEN out of API logs.
    """
    bot_token = get_bot_token()

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://api.telegram.org/bot{bot_token}/getFile",
            params={"file_id": file_id},
            timeout=10,
        )
        data = r.json()
        if not data.get("ok"):
            return JSONResponse({"error": "File not found or inaccessible"}, status_code=404)

        file_path    = data["result"]["file_path"]
        telegram_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"

    ext = file_path.rsplit(".", 1)[-1].lower()
    content_types = {
        "mp4": "video/mp4",
        "mkv": "video/x-matroska",
        "webm": "video/webm",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "pdf": "application/pdf",
    }
    media_type = content_types.get(ext, "application/octet-stream")

    async def stream_gen():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", telegram_url, timeout=120) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk

    return StreamingResponse(
        stream_gen(),
        media_type=media_type,
        headers={"Accept-Ranges": "bytes", "Content-Disposition": "inline"},
    )

# ============================================
# HEALTH CHECK
# ============================================

@app.get("/health")
async def health_check():
    """Expose bot and config state — useful for diagnosing Render deploys."""
    return {
        "status":       "ok" if (bot_application and bot_application.running) else "degraded",
        "bot_running":  bot_application.running if bot_application else False,
        "bot_username": getattr(settings, "BOT_USERNAME", None),
        "webhook_url":  getattr(settings, "WEBHOOK_URL", None),
        "bot_link":     getattr(settings, "BOT_LINK", None),
    }

# ============================================
# STATIC PAGE ROUTES
# ============================================

@app.get("/login.html", response_class=FileResponse)
async def serve_login():
    login_path = static_dir / "pages" / "login.html"
    if login_path.exists():
        return FileResponse(str(login_path))
    return JSONResponse({"error": "Login page not found"}, status_code=404)


@app.get("/dashboard.html", response_class=FileResponse)
async def serve_dashboard():
    """
    Serve dashboard page.
    Auth is handled client-side — JS checks localStorage for 'filebot_token'
    and redirects to login if missing.
    """
    dashboard_path = static_dir / "pages" / "dashboard.html"
    if dashboard_path.exists():
        return FileResponse(str(dashboard_path))
    return JSONResponse({"error": "Dashboard not found"}, status_code=404)


@app.get("/admin.html", response_class=FileResponse)
async def serve_admin():
    """Serve admin panel page — role check is enforced by the API (/api/admin/dashboard returns 403 for non-admins)."""
    admin_path = static_dir / "pages" / "admin.html"
    if admin_path.exists():
        return FileResponse(str(admin_path))
    return JSONResponse({"error": "Admin page not found"}, status_code=404)



@app.api_route("/", methods=["GET", "HEAD"])
async def serve_root():
    """Serve login page at root."""
    login_path = static_dir / "pages" / "login.html"
    if login_path.exists():
        return FileResponse(str(login_path))
    return {"status": "ok", "bot": getattr(settings, "BOT_USERNAME", "unknown")}


@app.get("/{path:path}")
async def serve_page(path: str):
    """Serve arbitrary static pages and files safely."""
    import urllib.parse
    
    # Path traversal protection - decode to catch %2e%2e
    decoded_path = urllib.parse.unquote(path)
    if ".." in decoded_path or decoded_path.startswith("/") or "%2e%2e" in path.lower():
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    try:
        resolved_static_target = (static_dir / decoded_path).resolve()
        # Verify that the generated path resides inside the static_dir
        if not str(resolved_static_target).startswith(str(static_dir.resolve())):
            return JSONResponse({"error": "Invalid path context"}, status_code=400)
    except Exception:
         return JSONResponse({"error": "Invalid path"}, status_code=400)

    for candidate in [
        static_dir / "pages" / decoded_path,
        static_dir / "pages" / f"{decoded_path}.html",
        static_dir / decoded_path,
    ]:
        if candidate.exists() and candidate.is_file():
            return FileResponse(str(candidate))

    return JSONResponse({"error": "Not found"}, status_code=404)

# ============================================
# CLI HELPERS
# ============================================

def set_webhook():
    """CLI: manually push webhook URL to Telegram."""
    bot_token   = settings.BOT_TOKEN
    webhook_url = (settings.WEBHOOK_URL or "").strip()

    if not bot_token:
        print("❌ BOT_TOKEN missing")
        return
    if not webhook_url:
        print("❌ WEBHOOK_URL missing")
        return

    print(f"➡️  Setting webhook → {webhook_url}")
    resp = httpx.post(
        f"https://api.telegram.org/bot{bot_token}/setWebhook",
        json={"url": webhook_url, "drop_pending_updates": False},
        timeout=15,
    )
    print(f"Status:   {resp.status_code}")
    print(f"Response: {resp.text}")


def get_webhook_info():
    """CLI: print current Telegram webhook info."""
    bot_token = settings.BOT_TOKEN
    if not bot_token:
        print("❌ BOT_TOKEN missing")
        return
    resp = httpx.get(
        f"https://api.telegram.org/bot{bot_token}/getWebhookInfo",
        timeout=10,
    )
    print(f"Status:   {resp.status_code}")
    print(f"Response: {resp.text}")

# ============================================
# MAIN ENTRY POINT
# ============================================

if __name__ == "__main__":
    import uvicorn

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "set-webhook":
            set_webhook()
        elif cmd == "get-webhook":
            get_webhook_info()
        else:
            print("Unknown command. Use: set-webhook | get-webhook")
    else:
        validate_production_secrets()
        uvicorn.run(
            "main:app",
            host="0.0.0.0",
            port=int(os.getenv("PORT", "8000")),
            reload=os.getenv("DEBUG", "").lower() == "true",  # fixed: bool("False") was always True
        )
