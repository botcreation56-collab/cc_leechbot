"""
main.py — Composition Root & Application Bootstrap

This is the ONLY file that knows about all layers.
It wires dependencies together and starts the application.

Architecture:
  Infrastructure (DB, Storage) → injected into →
  Services → injected into →
  Presentation (Bot Handlers, FastAPI Routes)

NO business logic lives here — only dependency wiring and lifecycle management.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

# ============================================================
# LOGGING — must be first
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("filebot.main")

# ============================================================
# PATH SETUP — must happen before local imports
# ============================================================

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# ============================================================
# CONFIGURATION
# ============================================================

from config.settings import get_admin_ids, get_bot_token, get_settings

settings = get_settings()

# ============================================================
# DEPENDENCY GRAPH  — build at startup, share via app.state
# ============================================================

bot_application: Application | None = None


async def build_dependency_graph():
    """Create and wire all infrastructure and service objects.

    Returns a dict of named services for injection into handlers.
    """
    from infrastructure.database.connection import DatabaseConnection
    from infrastructure.database.repositories import (
        AuditLogRepository,
        CloudFileRepository,
        ConfigRepository,
        OneTimeKeyRepository,
        RcloneConfigRepository,
        TaskRepository,
        UserRepository,
    )
    from services.media_service import DownloadService, MediaProcessingService
    from services.upload_service import UploadService
    from services.user_service import UserService

    # --- infrastructure ---
    db_conn = DatabaseConnection(
        uri=str(settings.MONGODB_URI),
        db_name=getattr(settings, "MONGODB_DB", "filebot_production"),
        min_pool=5,
        max_pool=30,
    )
    await db_conn.connect()
    await db_conn.create_indexes()
    db = db_conn.db

    user_repo   = UserRepository(db)
    task_repo   = TaskRepository(db)
    cloud_repo  = CloudFileRepository(db)
    otk_repo    = OneTimeKeyRepository(db)
    config_repo = ConfigRepository(db)
    audit_repo  = AuditLogRepository(db)
    rclone_repo = RcloneConfigRepository(db)

    # --- services ---
    base_url = (settings.WEBHOOK_URL or "").replace("/webhook/telegram", "")
    user_svc    = UserService(user_repo, audit_repo)
    dl_svc      = DownloadService()
    media_svc   = MediaProcessingService()
    upload_svc  = UploadService(cloud_repo, otk_repo, rclone_repo, stream_base_url=base_url)

    # Backward-compat: inject the shared Motor DB handle into the old bot.database layer
    # so existing handlers that still call `get_db()` share the exact same connection pool.
    from infrastructure.database._legacy_bot._connection import _set_shared_db, init_db as _old_init_db
    _set_shared_db(db)
    await _old_init_db()

    return {
        "db_conn":    db_conn,
        "user_repo":  user_repo,
        "task_repo":  task_repo,
        "cloud_repo": cloud_repo,
        "otk_repo":   otk_repo,
        "config_repo": config_repo,
        "audit_repo": audit_repo,
        "rclone_repo": rclone_repo,
        "user_svc":   user_svc,
        "dl_svc":     dl_svc,
        "media_svc":  media_svc,
        "upload_svc": upload_svc,
    }


# ============================================================
# ENVIRONMENT HELPERS
# ============================================================

def deduce_webhook_url() -> str:
    """Detect public hostname from common PaaS environment variables."""
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
    """Check required settings and auto-deduce webhook URL where possible."""
    if not settings.WEBHOOK_URL:
        deduced = deduce_webhook_url()
        if deduced:
            settings.WEBHOOK_URL = deduced.rstrip("/") + "/webhook/telegram"
            logger.info("🔍 Deduced Webhook URL: %s", settings.WEBHOOK_URL)

    required = {"BOT_TOKEN", "MONGODB_URI"}
    missing = []
    for v in required:
        val = getattr(settings, v, None)
        if not val:
            missing.append(v)
        elif hasattr(val, "get_secret_value") and not val.get_secret_value():
            missing.append(v)

    if missing:
        logger.critical("🚨 Missing required env vars: %s. The bot will not function until these are set in Render.", ", ".join(missing))
        # We don't sys.exit(1) here so that uvicorn can bind to the port and Render doesn't mark the deploy as failed.

    if settings.ENVIRONMENT.lower() == "production":
        sec_missing = []
        if not os.getenv("ENCRYPTION_KEY"):
            sec_missing.append("ENCRYPTION_KEY")
        if not os.getenv("JWT_SECRET"):
            sec_missing.append("JWT_SECRET")
        if sec_missing:
            logger.warning(
                "⚠️ Missing production secrets: %s — "
                "auto-generated values will be used (sessions reset on restart). "
                "Set these in Render dashboard for persistence!",
                ", ".join(sec_missing),
            )

        if not os.getenv("WEBHOOK_SECRET"):
            logger.warning(
                "⚠️ WEBHOOK_SECRET not set — Telegram webhook is unprotected! "
                "Any server can POST fake updates. Set WEBHOOK_SECRET in Render dashboard."
            )

    logger.info("✅ Environment validated")


# ============================================================
# HANDLER REGISTRATION
# ============================================================

def setup_handlers(application: Application) -> None:
    """Register ALL bot handlers in correct priority order."""
    from bot.middleware import apply_ban_check, error_handler

    # ── User handlers ────────────────────────────────────────
    from bot.handlers import (
        start_command, help_command, stats_command, myfiles_command,
        support_command, cancel_command, settings_command, ussettings_command,
        go_back_to_settings, handle_us_close, handle_us_mode,
        handle_us_mode_video, handle_us_mode_document,
        handle_us_prefix, handle_us_suffix, handle_us_metadata,
        handle_meta_title, handle_meta_author, handle_meta_year,
        handle_us_thumbnail, handle_us_thumbnail_menu, handle_us_visibility,
        handle_us_destination_button, handle_us_remove_confirm,
        handle_us_reset_confirm_yes, handle_us_myfiles, handle_us_plan,
        handle_subtitle_menu, handle_inject_sub, handle_meta_audio,
        handle_meta_video, handle_meta_subtitle, handle_rem_word,
        handle_rem_meta, handle_rem_inject, handle_callback_help,
        handle_callback_support, cancel_task_command, unknown_handler,
        handle_start_support_chat,
    )

    # ── Admin handlers ───────────────────────────────────────
    from bot.handlers import (
        admin_check_and_open,
        admin_command,
        handle_admin_add_rclone,
        handle_admin_add_rclone_wizard,
        handle_admin_back,
        handle_admin_ban_user,
        handle_admin_bans,
        handle_admin_broadcast,
        handle_admin_chatbox,
        handle_admin_filesize,
        handle_admin_find_user,
        handle_admin_forwards,
        handle_admin_fsub_add,
        handle_admin_fsub_link,
        handle_admin_fsub_manage,
        handle_admin_fsub_remove,
        handle_admin_fsub_remove_confirm,
        handle_admin_fsub_req_toggle,
        handle_admin_fsub_toggle,
        handle_admin_list_users,
        handle_admin_logs_menu,
        handle_admin_logs,
        handle_admin_download_logs,
        handle_admin_clear_logs,
        handle_view_error_logs,
        handle_support_reply,
        handle_support_read,
        handle_admin_rclone,
        handle_admin_remove_dump,
        handle_admin_remove_log,
        handle_admin_remove_storage,
        handle_admin_set_dump_channel,
        handle_admin_set_force_sub_channel,
        handle_admin_set_log_channel,
        handle_admin_set_storage_channel,
        handle_admin_add_dump_channel,
        handle_admin_add_log_channel,
        handle_admin_add_storage_channel,
        handle_admin_shorteners,
        handle_admin_stats,
        handle_admin_terabox,
        handle_admin_unban_user,
        handle_admin_upgrade_user,
        handle_admin_users,
        handle_broadcast_cancel,
        handle_broadcast_compose,
        handle_broadcast_stats,
        handle_cleanup_old_files,
        handle_disable_rclone,
        handle_edit_file_expiry,
        handle_edit_help_text,
        handle_edit_max_filesize,
        handle_edit_parallel_limit,
        handle_edit_plan,
        handle_edit_site_description,
        handle_edit_site_name,
        handle_edit_start_message,
        handle_edit_support_channel,
        handle_edit_support_contact,
        handle_edit_watermark,
        handle_list_rclone_remotes,
        handle_set_max_filesize,
        handle_storage_stats,
        handle_terabox_disable,
        handle_terabox_setup_key,
        handle_terabox_stats,
        handle_terabox_test,
        handle_test_rclone,
        handle_test_single_rclone,
        handle_unban_from_list,
        handle_user_destination_forward,
        handle_view_rclone,
        handle_view_user,
        rclone_plan_callback,
        rclone_service_callback,
        rclone_users_callback,
        handle_toggle_rclone,
        show_banned_users,
        show_config_menu,
        show_plans_menu,
    )

    # ── File, text, wizard ───────────────────────────────────
    from bot.handlers import (
        WizardHandler,
        callback_handler,
        handle_admin_input,
        handle_file_upload,
        handle_text_input,
        handle_url_input,
        rclone_command,
        rclone_text_input,
        terabox_command,
        terabox_text_input,
    )

    try:
        logger.info("🔧 Registering bot handlers...")

        # 0. GLOBAL MIDDLEWARE
        application.add_handler(TypeHandler(Update, apply_ban_check), group=-1)

        # 1. COMMANDS
        application.add_handler(CommandHandler("start",      start_command))
        application.add_handler(CommandHandler("help",       help_command))
        application.add_handler(CommandHandler("stats",      stats_command))
        application.add_handler(CommandHandler("myfiles",    myfiles_command))
        application.add_handler(CommandHandler("support",    support_command))
        application.add_handler(CommandHandler("cancel",     cancel_command))
        application.add_handler(CommandHandler("settings",   settings_command))
        application.add_handler(CommandHandler("ussettings", ussettings_command))
        application.add_handler(CommandHandler("admin",      admin_command, filters=filters.ChatType.PRIVATE))
        application.add_handler(CommandHandler("rclone",     rclone_command))
        application.add_handler(CommandHandler("terabox",    terabox_command))
        application.add_handler(MessageHandler(
            filters.Regex(r"^/cancel_\S+") & filters.TEXT & filters.ChatType.PRIVATE,
            cancel_task_command,
        ))

        # 2. FORWARDED MESSAGES
        async def handle_forward_routing(update: Update, context) -> None:
            user_id = update.effective_user.id
            admin_ids = get_admin_ids()
            channel_type = context.user_data.get("awaiting_channel_type", "")
            awaiting = context.user_data.get("awaiting")
            if user_id in admin_ids and channel_type:
                await handle_admin_forwards(update, context)
                return
            if awaiting == "us_destination":
                await handle_user_destination_forward(update, context)
                return
            await update.message.reply_text(
                "ℹ️ *Forwarded Message*\n\n"
                "Not expecting a channel right now.\n"
                "• Admin: /admin → ⚙️ Config\n"
                "• User: /ussettings → 🎯 Destination",
                parse_mode="Markdown",
            )

        application.add_handler(MessageHandler(
            filters.FORWARDED & filters.ChatType.PRIVATE,
            handle_forward_routing,
        ))

        # 3. FILES & PHOTOS
        application.add_handler(MessageHandler(
            (filters.Document.ALL | filters.VIDEO | filters.AUDIO) & filters.ChatType.PRIVATE,
            handle_file_upload,
        ))
        application.add_handler(MessageHandler(
            filters.PHOTO & filters.ChatType.PRIVATE,
            handle_us_thumbnail,
        ))

        # 4. USER SETTINGS CALLBACKS
        for pattern, handler in [
            ("^us_metadata$",       handle_us_metadata),
            ("^us_thumbnail$",      handle_us_thumbnail_menu),
            ("^us_mode$",           handle_us_mode),
            ("^us_mode_video$",     handle_us_mode_video),
            ("^us_mode_document$",  handle_us_mode_document),
            ("^us_remove_confirm$", handle_us_remove_confirm),
            ("^us_reset_confirm_yes$", handle_us_reset_confirm_yes),
            ("^us_myfiles$",        handle_us_myfiles),
            ("^us_plan$",           handle_us_plan),
            ("^us_prefix$",         handle_us_prefix),
            ("^us_suffix$",         handle_us_suffix),
            ("^us_visibility$",     handle_us_visibility),
            ("^us_destination$",    handle_us_destination_button),
            ("^us_back$",           go_back_to_settings),
            ("^us_close$",          handle_us_close),
            ("^us_help$",           handle_callback_help),
            ("^us_support$",        handle_callback_support),
            ("^us_stats$",          stats_command),
            ("^us_settings$",       ussettings_command),
            ("^subtitle_menu$",     handle_subtitle_menu),
            ("^inject_sub$",        handle_inject_sub),
            ("^meta_audio$",        handle_meta_audio),
            ("^meta_video$",        handle_meta_video),
            ("^meta_subtitle$",     handle_meta_subtitle),
            ("^rem_word$",          handle_rem_word),
            ("^rem_meta$",          handle_rem_meta),
            ("^rem_inject$",        handle_rem_inject),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # Wizard callbacks
        application.add_handler(CallbackQueryHandler(WizardHandler.handle_callback, pattern="^wiz_"))
        async def _noop(u, c): await u.callback_query.answer()
        application.add_handler(CallbackQueryHandler(_noop, pattern="^ignore$"))

        # 5. ADMIN MAIN MENU
        for pattern, handler in [
            ("^admin_back$",      handle_admin_back),
            ("^admin_users$",     handle_admin_users),
            ("^admin_stats$",     handle_admin_stats),
            ("^admin_config$",    show_config_menu),
            ("^admin_plans$",     show_plans_menu),
            ("^admin_broadcast$", handle_admin_broadcast),
            ("^admin_rclone$",    handle_admin_rclone),
            ("^admin_filesize$",  handle_admin_filesize),
            ("^admin_bans$",      handle_admin_bans),
            ("^admin_chatbox$",   handle_admin_chatbox),
            ("^admin_terabox$",   handle_admin_terabox),
            ("^admin_logs$",      handle_admin_logs),
            ("^view_logs_",       handle_admin_logs),
            ("^admin_shorteners$",handle_admin_shorteners),
            ("^add_shortener$",   handle_admin_shorteners),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 6. ADMIN CONFIG EDIT
        for pattern, handler in [
            ("^edit_start_msg$",        handle_edit_start_message),
            ("^edit_watermark$",        handle_edit_watermark),
            ("^edit_contact$",          handle_edit_support_contact),
            ("^edit_help_text$",        handle_edit_help_text),
            ("^edit_site_name$",        handle_edit_site_name),
            ("^edit_site_desc$",        handle_edit_site_description),
            ("^edit_support_channel$",  handle_edit_support_channel),
            ("^edit_parallel$",         handle_edit_parallel_limit),
            ("^edit_max_filesize$",     handle_edit_max_filesize),
            ("^edit_file_expiry$",      handle_edit_file_expiry),
            ("^edit_plan_free$",        handle_edit_plan),
            ("^edit_plan_premium$",     handle_edit_plan),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 7. ADMIN USER MANAGEMENT
        for pattern, handler in [
            ("^admin_find_user$",    handle_admin_find_user),
            ("^admin_ban_user$",     handle_admin_ban_user),
            ("^admin_unban_user$",   handle_admin_unban_user),
            ("^admin_upgrade_user$", handle_admin_upgrade_user),
            ("^admin_list_users_",   handle_admin_list_users),
            ("^view_user_",          handle_view_user),
            ("^banned_page_",        show_banned_users),
            ("^unban_user_",         handle_unban_from_list),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 8. ADMIN BROADCAST
        for pattern, handler in [
            ("^broadcast_compose$",       handle_broadcast_compose),
            ("^broadcast_stats$",         handle_broadcast_stats),
            ("^broadcast_cancel_input$",  handle_broadcast_cancel),
            ("^broadcast_pending$",       handle_admin_broadcast),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 9. ADMIN RCLONE
        for pattern, handler in [
            ("^admin_add_rclone$",        handle_admin_add_rclone),
            ("^admin_add_rclone_wizard$", handle_admin_add_rclone_wizard),
            ("^list_rclone_remotes$",     handle_list_rclone_remotes),
            ("^view_rclone_",             handle_view_rclone),
            ("^test_single_rclone_",      handle_test_single_rclone),
            ("^toggle_rclone_",           handle_toggle_rclone),
            ("^test_rclone$",             handle_test_rclone),
            ("^disable_rclone$",          handle_disable_rclone),
            ("^rclone_plan_(free|pro)$",  rclone_plan_callback),
            ("^rclone_users_",            rclone_users_callback),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))
        application.add_handler(CallbackQueryHandler(
            rclone_service_callback,
            pattern="^rclone_(gdrive|onedrive|dropbox|mega|terabox|custom)$",
        ))

        # 10. ADMIN TERABOX
        for pattern, handler in [
            ("^terabox_setup_key$", handle_terabox_setup_key),
            ("^terabox_test$",      handle_terabox_test),
            ("^terabox_stats$",     handle_terabox_stats),
            ("^terabox_disable$",   handle_terabox_disable),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 11. ADMIN STORAGE
        for pattern, handler in [
            ("^set_max_filesize$",  handle_set_max_filesize),
            ("^cleanup_old_files$", handle_cleanup_old_files),
            ("^storage_stats$",     handle_storage_stats),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 12. ADMIN CHANNELS
        for pattern, handler in [
            ("^admin_set_log_channel$",       handle_admin_set_log_channel),
            ("^admin_set_dump_channel$",      handle_admin_set_dump_channel),
            ("^admin_set_storage_channel$",   handle_admin_set_storage_channel),
            ("^admin_add_log_channel$",       handle_admin_add_log_channel),
            ("^admin_add_dump_channel$",      handle_admin_add_dump_channel),
            ("^admin_add_storage_channel$",   handle_admin_add_storage_channel),
            ("^admin_set_force_sub_channel$", handle_admin_set_force_sub_channel),
            ("^admin_check_and_open$",        admin_check_and_open),
            ("^admin_fsub_add$",              handle_admin_fsub_add),
            ("^admin_fsub_manage_",           handle_admin_fsub_manage),
            ("^admin_fsub_toggle_",           handle_admin_fsub_toggle),
            ("^admin_fsub_link_",             handle_admin_fsub_link),
            ("^admin_fsub_req_toggle_",       handle_admin_fsub_req_toggle),
            ("^admin_fsub_remove_confirm_",   handle_admin_fsub_remove_confirm),
            ("^admin_fsub_remove_",           handle_admin_fsub_remove),
            ("^admin_remove_log$",            handle_admin_remove_log),
            ("^admin_remove_dump$",           handle_admin_remove_dump),
            ("^admin_remove_storage$",        handle_admin_remove_storage),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 13. USER METADATA
        for pattern, handler in [
            ("^meta_title$",    handle_meta_title),
            ("^meta_author$",   handle_meta_author),
            ("^meta_year$",     handle_meta_year),
            ("^meta_subtitle$", handle_meta_subtitle),
            ("^meta_video$",    handle_meta_video),
            ("^meta_audio$",    handle_meta_audio),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 13. TEXT INPUT (awaiting states)
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_text_input,
        ))

        # 14. ADMIN LOGS & CHAT
        for pattern, handler in [
            ("^view_admin_logs$",   handle_admin_logs_menu),
            ("^view_logs_",         handle_admin_logs),
            ("^download_logs$",     handle_admin_download_logs),
            ("^clear_old_logs$",    handle_admin_clear_logs),
            ("^view_error_logs$",   handle_view_error_logs),
            ("^admin_chatbox$",     handle_admin_chatbox),
            ("^support_reply_",     handle_support_reply),
            ("^support_read_",      handle_support_read),
            ("^start_support_chat$", handle_start_support_chat),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 15. FALLBACKS (must be last)
        application.add_handler(CallbackQueryHandler(callback_handler))
        application.add_handler(MessageHandler(filters.COMMAND, unknown_handler))
        application.add_error_handler(error_handler)

        logger.info("✅ All handlers registered")
    except Exception as exc:
        logger.critical("❌ Handler registration failed: %s", exc, exc_info=True)
        raise


# ============================================================
# BOT APPLICATION BUILDER
# ============================================================

async def build_bot_application(deps: dict) -> Application:
    """Create PTB Application and inject shared state."""
    logger.info("🤖 Building Telegram Application (webhook mode)…")

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

    # Initialise Pyrogram clients
    from bot.pyrogram_client import init_pyrogram
    pyrogram_ok = await init_pyrogram()

    # Share services + repos via bot_data — handlers read from here
    application.bot_data.update({
        "admin_ids":    get_admin_ids(),
        "deps":         deps,               # full DI container
        # convenience shortcuts
        "user_svc":     deps["user_svc"],
        "dl_svc":       deps["dl_svc"],
        "media_svc":    deps["media_svc"],
        "upload_svc":   deps["upload_svc"],
        "config_repo":  deps["config_repo"],
        "task_repo":    deps["task_repo"],
    })

    setup_handlers(application)

    logger.info("✅ Bot application built | webhook: %s", settings.WEBHOOK_URL)
    return application


async def configure_webhook(bot_token: str, webhook_url: str, secret: str, user_count: int) -> None:
    """Auto-scale and (re-)configure Telegram webhook."""
    extra_conns = (user_count // 1000) * 20
    needed_max = min(100, 40 + extra_conns)

    try:
        async with httpx.AsyncClient() as client:
            info = (await client.get(
                f"https://api.telegram.org/bot{bot_token}/getWebhookInfo", timeout=10
            )).json()

        current_url = info.get("result", {}).get("url", "")
        current_max = info.get("result", {}).get("max_connections", 40)

        if current_url == webhook_url and needed_max <= current_max:
            logger.info("✅ Webhook already configured correctly: %s", webhook_url)
            return

        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.telegram.org/bot{bot_token}/setWebhook",
                json={
                    "url": webhook_url,
                    "drop_pending_updates": False,
                    "allowed_updates": ["message", "callback_query", "inline_query"],
                    "max_connections": needed_max,
                    "secret_token": secret,
                },
                timeout=15,
            )
        logger.info("SetWebhook → %d: %s", r.status_code, r.text[:200])
    except Exception as exc:
        logger.error("❌ Webhook setup failed: %s", exc, exc_info=True)


async def cleanup_bot(application: Application, db_conn) -> None:
    """Gracefully stop PTB application and DB connection."""
    from bot.pyrogram_client import stop_pyrogram
    await application.stop()
    await application.shutdown()
    await stop_pyrogram()
    await db_conn.close()
    logger.info("🛑 Cleanup complete")


# ============================================================
# FASTAPI LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_application

    logger.info("🚀 FastAPI startup")
    validate_environment()

    try:
        deps = await build_dependency_graph()
        app.state.deps = deps

        bot_application = await build_bot_application(deps)
        app.state.bot = bot_application.bot

        # Start background workers
        try:
            import asyncio
            from bot.services import QueueWorker
            worker = QueueWorker(bot_application.bot)
            asyncio.create_task(worker.start())
            logger.info("🚀 QueueWorker started in background")
        except Exception as e:
            logger.error(f"❌ Failed to start QueueWorker: {e}")

        # Fetch bot identity for display
        try:
            me = await bot_application.bot.get_me()
            settings.BOT_USERNAME = me.username
            settings.BOT_LINK = f"https://t.me/{me.username}"
            logger.info("🤖 @%s ready", me.username)
        except Exception as exc:
            logger.warning("Could not fetch bot identity: %s", exc)

        # Configure webhook
        if settings.WEBHOOK_URL:
            user_count = await deps["user_repo"]._col.count_documents({})
            await configure_webhook(
                get_bot_token(),
                settings.WEBHOOK_URL,
                settings.WEBHOOK_SECRET or "",
                user_count,
            )

    except Exception as exc:
        logger.error("❌ Startup error: %s", exc, exc_info=True)
        logger.warning("⚠️ Degraded mode — bot unavailable, web serving continues")

    yield

    logger.info("🛑 FastAPI shutdown")
    if bot_application is not None:
        await cleanup_bot(bot_application, deps.get("db_conn"))


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(
    title="FileBot",
    version="2.0",
    description="Telegram FileBot — production-grade",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://localhost:3000",
        os.getenv("FRONTEND_URL", ""),
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# ── Web routers ───────────────────────────────────────────────
from web.routes.admin_config import router as config_router
from web.routes.admin_dashboard import router as dashboard_router
from web.routes.admin_logs import router as logs_router
from web.routes.admin_users import router as users_router
from web.routes.auth import router as auth_router
from web.routes.public import router as public_router
from web.routes.user_settings import router as user_settings_router

app.include_router(auth_router, tags=["Auth"], prefix="/api/auth")
app.include_router(auth_router, tags=["Auth"], prefix="/auth")  # auth.js uses /auth/request-code
app.include_router(public_router, tags=["Public"])
app.include_router(dashboard_router, tags=["AdminDashboard"], prefix="/api/admin")
app.include_router(users_router, tags=["AdminUsers"], prefix="/api/admin")
app.include_router(config_router, tags=["AdminConfig"], prefix="/api/admin")
app.include_router(user_settings_router, tags=["UserSettings"], prefix="/api/user")
app.include_router(logs_router, tags=["AdminLogs"], prefix="/api/admin")



# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health", include_in_schema=False)
@app.head("/health", include_in_schema=False)
async def health():
    deps = getattr(app.state, "deps", {})
    db_ok = False
    if db_conn := deps.get("db_conn"):
        try:
            await db_conn.db.command("ping")
            db_ok = True
        except Exception:
            pass
    status = "healthy" if db_ok else "degraded"
    
    # Debug: Check admin access stats or logs if needed
    admin_ids = get_admin_ids()
    
    return {
        "status": status,
        "bot_ready": bot_application is not None,
        "bot_username": settings.BOT_USERNAME,
        "bot_link": settings.BOT_LINK,
        "admin_count": len(admin_ids)
    }


# ============================================================
# TELEGRAM WEBHOOK RECEIVER
# ============================================================

WEBHOOK_PATH = "/webhook/telegram"


@app.post(WEBHOOK_PATH, include_in_schema=False)
async def telegram_webhook(request: Request):
    if bot_application is None:
        raise HTTPException(status_code=503, detail="Bot not initialised")

    # Validate Telegram secret header
    secret = settings.WEBHOOK_SECRET or ""
    if secret:
        incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if incoming != secret:
            raise HTTPException(status_code=403, detail="Invalid secret token")

    body = await request.body()
    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    update = Update.de_json(data, bot_application.bot)
    await bot_application.process_update(update)
    return JSONResponse({"ok": True})


# ── Stream file redirect (used by /api/stream handler) ───────
@app.get("/stream/{file_id}", include_in_schema=False)
async def stream_file(file_id: str):
    """Redirect to a fresh Telegram CDN URL for the given file_id.

    Telegram CDN URLs expire quickly — always generate fresh ones per request.
    """
    if bot_application is None:
        raise HTTPException(status_code=503, detail="Bot not available")
    try:
        from fastapi.responses import RedirectResponse
        tg_file = await bot_application.bot.get_file(file_id)
        return RedirectResponse(url=tg_file.file_path)
    except Exception as exc:
        logger.warning("Stream redirect failed for file_id=%s: %s", file_id, exc)
        raise HTTPException(status_code=404, detail="File not found or expired")


# ── Static files ─────────────────────────────────────────────
_static_dir = project_root / "web" / "static"
if _static_dir.exists():
    # Mount assets (CSS, JS) under /static
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")



    # Mount HTML pages under / so they are accessible as /login.html, /dashboard.html etc.
    _pages_dir = _static_dir / "pages"
    if _pages_dir.exists():
        app.mount("/", StaticFiles(directory=str(_pages_dir), html=True), name="pages")


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        reload=False,
        log_level="info",
    )
