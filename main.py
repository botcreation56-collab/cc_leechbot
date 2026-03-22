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

# Webhook status tracker
_webhook_status = {
    "configured": False,
    "url": None,
    "error": None,
    "last_check": None,
}


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

    # All database indexing and migrations will be moved to the end of _startup_tasks
    # to prevent startup hangs during the critical Render health check window.

    db = db_conn.db

    user_repo = UserRepository(db)
    task_repo = TaskRepository(db)
    cloud_repo = CloudFileRepository(db)
    otk_repo = OneTimeKeyRepository(db)
    config_repo = ConfigRepository(db)
    audit_repo = AuditLogRepository(db)
    rclone_repo = RcloneConfigRepository(db)

    # --- services ---
    base_url = (settings.WEBHOOK_URL or "").replace("/webhook/telegram", "")
    user_svc = UserService(user_repo, audit_repo)
    dl_svc = DownloadService()
    media_svc = MediaProcessingService()
    upload_svc = UploadService(
        cloud_repo, otk_repo, rclone_repo, stream_base_url=base_url
    )

    # Backward-compat: inject the shared Motor DB handle into the old bot.database layer
    # so existing handlers that still call `get_db()` share the exact same connection pool.
    from database.connection import (
        _set_shared_db,
        ensure_channel_schema,
        migrate_flat_to_nested,
    )

    _set_shared_db(db)
    # Background migrations to avoid blocking port binding / startup
    asyncio.create_task(ensure_channel_schema(db))
    asyncio.create_task(migrate_flat_to_nested(db))

    return {
        "db_conn": db_conn,
        "user_repo": user_repo,
        "task_repo": task_repo,
        "cloud_repo": cloud_repo,
        "otk_repo": otk_repo,
        "config_repo": config_repo,
        "audit_repo": audit_repo,
        "rclone_repo": rclone_repo,
        "user_svc": user_svc,
        "dl_svc": dl_svc,
        "media_svc": media_svc,
        "upload_svc": upload_svc,
    }


# ============================================================
# ENVIRONMENT HELPERS
# ============================================================


def deduce_webhook_url() -> str:
    """Detect public hostname from common PaaS environment variables."""
    # Render uses RENDER_SERVICE_URL (not RENDER_EXTERNAL_URL)
    if url := os.getenv("RENDER_SERVICE_URL"):
        return url
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
    print(
        f"🔍 validate_environment: Initial WEBHOOK_URL = '{settings.WEBHOOK_URL}'",
        flush=True,
    )
    print(
        f"🔍 validate_environment: RENDER_EXTERNAL_URL = '{os.getenv('RENDER_EXTERNAL_URL')}'",
        flush=True,
    )
    print(
        f"🔍 validate_environment: RENDER_SERVICE_URL = '{os.getenv('RENDER_SERVICE_URL')}'",
        flush=True,
    )

    if not settings.WEBHOOK_URL:
        deduced = deduce_webhook_url()
        print(f"🔍 validate_environment: Deduced URL = '{deduced}'", flush=True)
        if deduced:
            settings.WEBHOOK_URL = deduced.rstrip("/") + "/webhook/telegram"
            logger.info("🔍 Deduced Webhook URL: %s", settings.WEBHOOK_URL)
            print(
                f"🔍 validate_environment: Set WEBHOOK_URL = '{settings.WEBHOOK_URL}'",
                flush=True,
            )

    required = {"BOT_TOKEN", "MONGODB_URI"}
    missing = []
    for v in required:
        val = getattr(settings, v, None)
        if not val:
            missing.append(v)
        elif hasattr(val, "get_secret_value") and not val.get_secret_value():
            missing.append(v)

    if missing:
        logger.critical(
            "🚨 Missing required env vars: %s. The bot will not function until these are set in Render.",
            ", ".join(missing),
        )
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
        start_command,
        help_command,
        stats_command,
        myfiles_command,
        support_command,
        cancel_command,
        settings_command,
        ussettings_command,
        go_back_to_settings,
        handle_us_close,
        handle_us_mode,
        handle_us_mode_video,
        handle_us_mode_document,
        handle_us_prefix,
        handle_us_suffix,
        handle_us_metadata,
        handle_meta_author,
        handle_us_thumbnail,
        handle_us_thumbnail_menu,
        handle_us_visibility,
        handle_us_destination_button,
        handle_us_dest_add,
        handle_us_dest_manage,
        handle_us_dest_caption_builder,
        handle_us_dest_cap_edit,
        handle_us_dest_cap_stream_btn,
        handle_us_dest_cap_style,
        handle_us_dest_cap_reset,
        handle_us_dest_buttons,
        handle_us_dest_shortener_toggle,
        handle_us_dest_download_link_choice,
        handle_us_dest_caption_prompt,
        handle_us_dest_meta_name_prompt,
        handle_us_dest_meta_auth_prompt,
        handle_us_dest_remove_confirm,
        handle_us_dest_remove_do,
        handle_us_remove_confirm,
        handle_us_reset_confirm_yes,
        handle_us_myfiles,
        handle_us_plan,
        handle_subtitle_menu,
        handle_inject_sub,
        handle_meta_audio,
        handle_meta_video,
        handle_meta_subs,
        handle_rem_word,
        handle_rem_meta,
        handle_rem_inject,
        handle_callback_help,
        handle_callback_support,
        cancel_task_command,
        unknown_handler,
        handle_start_support_chat,
        handle_chat_join_request,
        handle_check_subscription,
        handle_us_thumbnail_view,
        handle_us_thumbnail_delete,
        handle_us_thumbnail_delete_confirm,
        handle_us_rclone_service,
        handle_toggle_plan_rclone,
        handle_toggle_plan_shortener,
        handle_us_rclone_dest_activate,
        handle_add_shortener,
        handle_edit_tutorial_link,
        callback_handler,
        handle_bypass_queue,
        handle_refresh_queue,
        handle_refresh_progress,
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
        handle_add_shortener,
        handle_edit_tutorial_link,
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
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("stats", stats_command))
        application.add_handler(CommandHandler("myfiles", myfiles_command))
        application.add_handler(CommandHandler("support", support_command))
        application.add_handler(CommandHandler("cancel", cancel_command))
        application.add_handler(CommandHandler("settings", settings_command))
        application.add_handler(CommandHandler("ussettings", ussettings_command))
        application.add_handler(
            CommandHandler("admin", admin_command, filters=filters.ChatType.PRIVATE)
        )
        application.add_handler(CommandHandler("rclone", rclone_command))
        application.add_handler(CommandHandler("terabox", terabox_command))
        application.add_handler(
            MessageHandler(
                filters.Regex(r"^/cancel_\S+")
                & filters.TEXT
                & filters.ChatType.PRIVATE,
                cancel_task_command,
            )
        )

        # 2. FORWARDED MESSAGES
        async def handle_forward_routing(update: Update, context) -> None:
            user_id = update.effective_user.id
            admin_ids = get_admin_ids()
            channel_type = context.user_data.get("awaiting_channel_type", "")
            awaiting = context.user_data.get("awaiting")
            if user_id in admin_ids and channel_type:
                await handle_admin_forwards(update, context)
                return
            if awaiting in ("us_destination", "us_dest_forward"):
                await handle_user_destination_forward(update, context)
                return
            await update.message.reply_text(
                "ℹ️ *Forwarded Message*\n\n"
                "Not expecting a channel right now.\n"
                "• Admin: /admin → ⚙️ Config\n"
                "• User: /ussettings → 🎯 Destination",
                parse_mode="Markdown",
            )

        application.add_handler(
            MessageHandler(
                filters.FORWARDED & filters.ChatType.PRIVATE,
                handle_forward_routing,
            )
        )

        # 3. FILES & PHOTOS
        application.add_handler(
            MessageHandler(
                (filters.Document.ALL | filters.VIDEO | filters.AUDIO)
                & filters.ChatType.PRIVATE,
                handle_file_upload,
            )
        )
        application.add_handler(
            MessageHandler(
                filters.PHOTO & filters.ChatType.PRIVATE,
                handle_us_thumbnail,
            )
        )

        # 4. USER SETTINGS & DESTINATION CALLBACKS
        for pattern, handler in [
            ("^us_metadata$", handle_us_metadata),
            ("^us_thumbnail$", handle_us_thumbnail_menu),
            ("^us_thumbnail_view$", handle_us_thumbnail_view),
            ("^us_thumbnail_delete$", handle_us_thumbnail_delete),
            ("^us_thumbnail_delete_confirm$", handle_us_thumbnail_delete_confirm),
            ("^us_mode$", handle_us_mode),
            ("^us_mode_video$", handle_us_mode_video),
            ("^us_mode_document$", handle_us_mode_document),
            ("^us_remove_confirm$", handle_us_remove_confirm),
            ("^us_reset_confirm_yes$", handle_us_reset_confirm_yes),
            ("^us_myfiles$", handle_us_myfiles),
            ("^us_plan$", handle_us_plan),
            ("^us_prefix$", handle_us_prefix),
            ("^us_suffix$", handle_us_suffix),
            ("^us_visibility$", handle_us_visibility),
            ("^us_destination$", handle_us_destination_button),
            ("^us_dest_add$", handle_us_dest_add),
            ("^us_dest_manage_", handle_us_dest_manage),
            ("^us_dest_caption_builder_", handle_us_dest_caption_builder),
            ("^us_dest_cap_edit_", handle_us_dest_cap_edit),
            ("^us_dest_cap_stream_btn_", handle_us_dest_cap_stream_btn),
            ("^us_dest_cap_style_", handle_us_dest_cap_style),
            ("^us_dest_cap_reset_", handle_us_dest_cap_reset),
            ("^us_dest_buttons_", handle_us_dest_buttons),
            ("^us_dest_shortener_", handle_us_dest_shortener_toggle),
            ("^us_dest_dl_text_", handle_us_dest_download_link_choice),
            ("^us_dest_caption_", handle_us_dest_caption_prompt),
            ("^us_dest_meta_name_", handle_us_dest_meta_name_prompt),
            ("^us_dest_meta_auth_", handle_us_dest_meta_auth_prompt),
            ("^us_dest_remove_confirm_", handle_us_dest_remove_confirm),
            ("^us_dest_remove_do_", handle_us_dest_remove_do),
            ("^us_rclone_service$", handle_us_rclone_service),
            (
                "^us_set_rclone_dest_",
                handle_us_rclone_dest_activate,
            ),
            ("^us_back$", go_back_to_settings),
            ("^us_close$", handle_us_close),
            ("^us_help$", handle_callback_help),
            ("^us_support$", handle_callback_support),
            ("^us_stats$", stats_command),
            ("^us_settings$", ussettings_command),
            ("^subtitle_menu$", handle_subtitle_menu),
            ("^inject_sub$", handle_inject_sub),
            ("^meta_audio$", handle_meta_audio),
            ("^meta_video$", handle_meta_video),
            ("^meta_author$", handle_meta_author),
            ("^meta_subs$", handle_meta_subs),
            ("^rem_word$", handle_rem_word),
            ("^rem_meta$", handle_rem_meta),
            ("^rem_inject$", handle_rem_inject),
            # Queue & bypass - use dedicated handlers
            ("^queue_start_", callback_handler),
            ("^refresh_q_", handle_refresh_queue),
            ("^bypass_q_", handle_bypass_queue),
            ("^refresh_progress_", handle_refresh_progress),
            # File forwarding
            ("^send_dest_", callback_handler),
            ("^fwd_dest_", callback_handler),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # Wizard callbacks
        application.add_handler(
            CallbackQueryHandler(WizardHandler.handle_callback, pattern="^wiz_")
        )

        async def _noop(u, c):
            await u.callback_query.answer()

        application.add_handler(CallbackQueryHandler(_noop, pattern="^ignore$"))

        # 5. ADMIN MAIN MENU
        for pattern, handler in [
            ("^admin_back$", handle_admin_back),
            ("^admin_users$", handle_admin_users),
            ("^admin_stats$", handle_admin_stats),
            ("^admin_config$", show_config_menu),
            ("^admin_plans$", show_plans_menu),
            ("^admin_broadcast$", handle_admin_broadcast),
            ("^admin_rclone$", handle_admin_rclone),
            ("^admin_filesize$", handle_admin_filesize),
            ("^admin_bans$", handle_admin_bans),
            ("^admin_chatbox$", handle_admin_chatbox),
            ("^admin_terabox$", handle_admin_terabox),
            ("^admin_logs$", handle_admin_logs),
            ("^view_logs_", handle_admin_logs),
            ("^admin_shorteners$", handle_admin_shorteners),
            ("^add_shortener$", handle_add_shortener),
            ("^edit_tutorial_link$", handle_edit_tutorial_link),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 6. ADMIN CONFIG EDIT
        for pattern, handler in [
            ("^edit_start_msg$", handle_edit_start_message),
            ("^edit_watermark$", handle_edit_watermark),
            ("^edit_contact$", handle_edit_support_contact),
            ("^edit_help_text$", handle_edit_help_text),
            ("^edit_site_name$", handle_edit_site_name),
            ("^edit_site_desc$", handle_edit_site_description),
            ("^edit_support_channel$", handle_edit_support_channel),
            ("^edit_parallel$", handle_edit_parallel_limit),
            ("^edit_max_filesize$", handle_edit_max_filesize),
            ("^edit_file_expiry$", handle_edit_file_expiry),
            ("^edit_plan_free$", handle_edit_plan),
            ("^edit_plan_premium$", handle_edit_plan),
            ("^edit_shortener_", handle_add_shortener),
            (
                "^toggle_plan_rclone_",
                handle_toggle_plan_rclone,
            ),
            (
                "^toggle_shortener_",
                handle_toggle_plan_shortener,
            ),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 7. ADMIN USER MANAGEMENT
        for pattern, handler in [
            ("^admin_find_user$", handle_admin_find_user),
            ("^admin_ban_user$", handle_admin_ban_user),
            ("^admin_unban_user$", handle_admin_unban_user),
            ("^admin_upgrade_user$", handle_admin_upgrade_user),
            ("^admin_list_users_", handle_admin_list_users),
            ("^view_user_", handle_view_user),
            ("^banned_page_", show_banned_users),
            ("^unban_user_", handle_unban_from_list),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 8. ADMIN BROADCAST
        for pattern, handler in [
            ("^broadcast_compose$", handle_broadcast_compose),
            ("^broadcast_stats$", handle_broadcast_stats),
            ("^broadcast_cancel_input$", handle_broadcast_cancel),
            ("^broadcast_pending$", handle_admin_broadcast),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 9. ADMIN RCLONE
        for pattern, handler in [
            ("^admin_add_rclone$", handle_admin_add_rclone),
            ("^admin_add_rclone_wizard$", handle_admin_add_rclone_wizard),
            ("^list_rclone_remotes$", handle_list_rclone_remotes),
            ("^view_rclone_", handle_view_rclone),
            ("^test_single_rclone_", handle_test_single_rclone),
            ("^toggle_rclone_", handle_toggle_rclone),
            ("^test_rclone$", handle_test_rclone),
            ("^disable_rclone$", handle_disable_rclone),
            ("^rclone_plan_(free|pro)$", rclone_plan_callback),
            ("^rclone_users_", rclone_users_callback),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))
        application.add_handler(
            CallbackQueryHandler(
                rclone_service_callback,
                pattern="^rclone_(gdrive|onedrive|dropbox|mega|terabox|custom)$",
            )
        )

        # 10. ADMIN TERABOX
        for pattern, handler in [
            ("^terabox_setup_key$", handle_terabox_setup_key),
            ("^terabox_test$", handle_terabox_test),
            ("^terabox_stats$", handle_terabox_stats),
            ("^terabox_disable$", handle_terabox_disable),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 11. ADMIN STORAGE
        for pattern, handler in [
            ("^set_max_filesize$", handle_set_max_filesize),
            ("^cleanup_old_files$", handle_cleanup_old_files),
            ("^storage_stats$", handle_storage_stats),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 12. ADMIN CHANNELS
        for pattern, handler in [
            ("^admin_set_log_channel$", handle_admin_set_log_channel),
            ("^admin_set_dump_channel$", handle_admin_set_dump_channel),
            ("^admin_set_storage_channel$", handle_admin_set_storage_channel),
            ("^admin_add_log_channel$", handle_admin_add_log_channel),
            ("^admin_add_dump_channel$", handle_admin_add_dump_channel),
            ("^admin_add_storage_channel$", handle_admin_add_storage_channel),
            ("^admin_set_force_sub_channel$", handle_admin_set_force_sub_channel),
            ("^admin_check_and_open$", admin_check_and_open),
            ("^admin_fsub_add$", handle_admin_fsub_add),
            ("^admin_fsub_manage_", handle_admin_fsub_manage),
            ("^admin_fsub_toggle_", handle_admin_fsub_toggle),
            ("^admin_fsub_link_", handle_admin_fsub_link),
            ("^admin_fsub_req_toggle_", handle_admin_fsub_req_toggle),
            ("^admin_fsub_remove_confirm_", handle_admin_fsub_remove_confirm),
            ("^admin_fsub_remove_", handle_admin_fsub_remove),
            ("^admin_remove_log$", handle_admin_remove_log),
            ("^admin_remove_dump$", handle_admin_remove_dump),
            ("^admin_remove_storage$", handle_admin_remove_storage),
            ("^check_subscription$", handle_check_subscription),
        ]:
            application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

        # 12.5. JOIN REQUESTS
        from telegram.ext import ChatJoinRequestHandler

        application.add_handler(ChatJoinRequestHandler(handle_chat_join_request))

        # 13. TEXT INPUT (awaiting states)
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                handle_text_input,
            )
        )

        # 14. ADMIN LOGS & CHAT
        for pattern, handler in [
            ("^view_admin_logs$", handle_admin_logs_menu),
            ("^view_logs_", handle_admin_logs),
            ("^download_logs$", handle_admin_download_logs),
            ("^clear_old_logs$", handle_admin_clear_logs),
            ("^view_error_logs$", handle_view_error_logs),
            ("^admin_chatbox$", handle_admin_chatbox),
            ("^support_reply_", handle_support_reply),
            ("^support_read_", handle_support_read),
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

    from telegram.ext import JobQueue

    job_queue = JobQueue()

    application = (
        Application.builder()
        .token(get_bot_token())
        .connect_timeout(10.0)
        .read_timeout(10.0)
        .write_timeout(10.0)
        .pool_timeout(10.0)
        .job_queue(job_queue)
        .build()
    )

    job_queue.set_application(application)

    await application.initialize()
    await application.start()

    # Initialize Telegram log handler (non-blocking)
    try:
        from bot.utils import init_telegram_logging

        await init_telegram_logging(application.bot)
    except Exception as e:
        logger.warning(f"⚠️ Telegram log handler init failed: {e}")

    # Initialize Pyrogram clients (non-blocking, background task)
    import asyncio as _asyncio

    # Pyrogram clients are now started ONLY in _startup_tasks to ensure singleton execution

    # Share services + repos via bot_data — handlers read from here
    application.bot_data.update(
        {
            "admin_ids": get_admin_ids(),
            "deps": deps,  # full DI container
            # convenience shortcuts
            "user_svc": deps["user_svc"],
            "dl_svc": deps["dl_svc"],
            "media_svc": deps["media_svc"],
            "upload_svc": deps["upload_svc"],
            "config_repo": deps["config_repo"],
            "task_repo": deps["task_repo"],
        }
    )

    setup_handlers(application)

    logger.info("✅ Bot application built | webhook: %s", settings.WEBHOOK_URL)
    return application


async def configure_webhook(
    bot_token: str, webhook_url: str, secret: str, user_count: Optional[int] = None
) -> None:
    """Auto-scale and (re-)configure Telegram webhook."""
    global _webhook_status

    if user_count is None:
        needed_max = 40
    else:
        extra_conns = (user_count // 1000) * 20
        needed_max = min(100, 40 + extra_conns)

    try:
        print(f"🔧 configure_webhook: Checking current webhook info...", flush=True)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0)
        ) as client:
            print(
                f"🔧 configure_webhook: Making request to Telegram API...", flush=True
            )
            info = (
                await client.get(
                    f"https://api.telegram.org/bot{bot_token}/getWebhookInfo",
                )
            ).json()
            print(f"🔧 configure_webhook: Got response from Telegram API", flush=True)

        current_url = info.get("result", {}).get("url", "")
        current_max = info.get("result", {}).get("max_connections", 40)
        print(
            f"🔧 configure_webhook: Current URL = {current_url}, Current max = {current_max}",
            flush=True,
        )

        if current_url == webhook_url and needed_max <= current_max:
            logger.info("✅ Webhook already configured correctly: %s", webhook_url)
            print(
                "✅ configure_webhook: Webhook already configured correctly", flush=True
            )
            _webhook_status["configured"] = True
            _webhook_status["url"] = webhook_url
            return

        print(f"🔧 configure_webhook: Setting webhook to {webhook_url}...", flush=True)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0)
        ) as client:
            print(
                f"🔧 configure_webhook: Sending setWebhook request to Telegram...",
                flush=True,
            )
            r = await client.post(
                f"https://api.telegram.org/bot{bot_token}/setWebhook",
                json={
                    "url": webhook_url,
                    "drop_pending_updates": False,
                    "allowed_updates": [
                        "message",
                        "callback_query",
                        "inline_query",
                        "chat_join_request",
                    ],
                    "max_connections": needed_max,
                    "secret_token": secret,
                },
            )
        print(
            f"🔧 configure_webhook: SetWebhook response = {r.status_code}, {r.text[:100]}",
            flush=True,
        )
        logger.info("SetWebhook → %d: %s", r.status_code, r.text[:200])

        # Update status (correctly set based on response)
        _webhook_status["url"] = webhook_url

        # Check if webhook was set successfully
        is_ok = r.status_code == 200 or r.json().get("result", False)
        if is_ok:
            _webhook_status["configured"] = True
            print("✅ configure_webhook: SUCCESS - Webhook configured!", flush=True)
        else:
            _webhook_status["configured"] = False
            _webhook_status["error"] = r.text[:200]
            print(
                f"⚠️ configure_webhook: Webhook may not be configured. Response: {r.text[:100]}",
                flush=True,
            )

    except Exception as exc:
        logger.error("❌ Webhook setup failed: %s", exc, exc_info=True)
        print(f"❌ configure_webhook: Webhook setup failed: {exc}", flush=True)
        _webhook_status["configured"] = False
        _webhook_status["error"] = str(exc)[:200]


# ============================================================
# BACKGROUND SETUP HELPERS
# ============================================================


async def _init_pyrogram_bg():
    """Background Pyrogram initialization."""
    try:
        from bot.pyrogram_client import init_pyrogram

        logger.info("🔧 Starting Pyrogram clients in background...")
        ok = await init_pyrogram()
        if ok:
            logger.info("✅ Pyrogram clients ready")
        else:
            logger.info("ℹ️ Pyrogram not configured")
    except Exception as e:
        logger.warning(f"⚠️ Pyrogram init failed: {e}")


async def _setup_rclone_bg():
    """Background Rclone setup."""
    try:
        from bot.services._cloud_upload import ensure_rclone_binary

        print("🔧 Checking Rclone status...", flush=True)
        path = await ensure_rclone_binary()
        if path:
            print("✅ Rclone ready", flush=True)
        else:
            print("⚠️ Rclone not configured", flush=True)
    except Exception as e:
        print(f"❌ Rclone background error: {e}", flush=True)


async def _setup_webhook_bg():
    """Background Webhook/Polling setup. HIGH PRIORITY."""
    global _webhook_status

    print("🔧 _setup_webhook_bg: Task started", flush=True)
    _webhook_status["url"] = settings.WEBHOOK_URL
    _webhook_status["configured"] = False
    _webhook_status["error"] = None

    try:
        # Give the server minimal time to bind
        await asyncio.sleep(0.5)
        print("🔧 _setup_webhook_bg: After initial sleep (0.5s)", flush=True)

        from bot.database import get_config
        print("🔧 _setup_webhook_bg: Fetching config from database...", flush=True)
        config = await get_config() or {}
        print(f"🔧 _setup_webhook_bg: Config loaded ({len(config)} keys)", flush=True)
        if bot_application is None:
            logger.error("❌ Bot application not initialized when setting up webhook!")
            print("❌ _setup_webhook_bg: bot_application is None!", flush=True)
            _webhook_status["error"] = "Bot application not initialized"
            return

        if settings.WEBHOOK_URL:
            print(
                f"🔧 _setup_webhook_bg: Configuring webhook to {settings.WEBHOOK_URL}...",
                flush=True,
            )
            await configure_webhook(
                get_bot_token(),
                settings.WEBHOOK_URL,
                settings.WEBHOOK_SECRET or "",
                None,
            )
            print(f"✅ _setup_webhook_bg: Webhook configured successfully", flush=True)
        else:
            # Fallback to webhook with self URL for local development
            print("⚠️ WEBHOOK_URL not set, attempting to auto-detect...", flush=True)

            # Try to deduce the webhook URL
            detected_url = None
            if os.getenv("RENDER_EXTERNAL_URL"):
                detected_url = f"{os.getenv('RENDER_EXTERNAL_URL')}/webhook/telegram"
            elif os.getenv("RENDER_SERVICE_URL"):
                detected_url = f"{os.getenv('RENDER_SERVICE_URL')}/webhook/telegram"

            if detected_url:
                print(f"🔧 Auto-detected webhook URL: {detected_url}", flush=True)
                await configure_webhook(
                    get_bot_token(), detected_url, settings.WEBHOOK_SECRET or "", None
                )
                print("✅ Auto-configured webhook", flush=True)
            else:
                # Last resort: use polling with PTB's built-in webserver
                print("⚠️ No webhook URL found, starting polling...", flush=True)

                # In PTB, when no webhook URL is set and we want to receive updates,
                # we need to ensure the application is properly started
                if bot_application and hasattr(bot_application, "update_queue"):
                    logger.info(
                        "Bot application ready for updates via webhook receiver"
                    )
                    print(
                        "✅ Bot ready - updates will arrive via webhook endpoint",
                        flush=True,
                    )
                else:
                    logger.error(
                        "❌ Bot application not properly initialized for updates"
                    )
                    print("❌ Bot not ready for updates!", flush=True)

    except Exception as e:
        logger.error(f"❌ Webhook setup error: {e}", exc_info=True)
        print(f"❌ _setup_webhook_bg: Error: {e}", flush=True)
        import traceback

        print(f"   Full traceback:\n{traceback.format_exc()}", flush=True)
    finally:
        print("🔧 _setup_webhook_bg: Function completed", flush=True)


async def _background_indexing_job(db_conn):
    """Heavy indexing job deferred much further."""
    try:
        # Wait 30 seconds before starting heavy indexing
        # to ensure the web layer and health checks are stable
        await asyncio.sleep(30)
        logger.info("🔧 Starting background index building...")
        await db_conn.create_indexes()
        logger.info("✅ Background index building complete")
    except Exception as e:
        logger.warning(f"⚠️ Indexing job failed: {e}")


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
    """Lifespan - port binds immediately, startup runs in background."""
    global bot_application

    logger.info("🚀 Starting up...")
    print("🚀 Lifespan: Starting initialization...", flush=True)

    try:
        validate_environment()
        print("✅ Lifespan: Environment validated", flush=True)
    except Exception as e:
        logger.error(f"❌ Lifespan: Environment validation failed: {e}")
        print(f"❌ Lifespan: Environment validation failed: {e}", flush=True)

    loop = asyncio.get_running_loop()
    loop.create_task(_startup_tasks(app))
    print("✅ Lifespan: Startup task created", flush=True)

    yield  # PORT BINDS HERE
    print("🛑 Lifespan: Shutdown initiated", flush=True)

    logger.info("🛑 FastAPI shutting down...")
    if bot_application:
        try:
            await cleanup_bot(bot_application, app.state.deps.get("db_conn"))
        except Exception as e:
            logger.error(f"❌ Cleanup error: {e}")
            print(f"❌ Lifespan cleanup error: {e}", flush=True)
    print("🛑 Lifespan: Shutdown complete", flush=True)


async def _startup_tasks(app: FastAPI):
    """Run ALL startup tasks in background (port already bound)."""
    global bot_application
    import traceback as tb
    import sys

    logger.info("🚀 Running startup tasks...")
    print("🔧 _startup_tasks: Beginning startup sequence...", flush=True)

    try:
        validate_environment()
        logger.info("✅ Environment validated")
        print("✅ _startup_tasks: Environment validated", flush=True)
    except Exception as e:
        logger.warning(f"⚠️ Env validation: {e}")
        print(f"⚠️ _startup_tasks: Env validation warning: {e}", flush=True)

    print("🔧 _startup_tasks: Building dependency graph...", flush=True)
    try:
        deps = await build_dependency_graph()
        app.state.deps = deps
        logger.info("✅ Dependencies ready")
        print("✅ _startup_tasks: Dependencies ready", flush=True)
    except Exception as e:
        logger.error(f"❌ Dependencies failed: {e}\n{tb.format_exc()}")
        print(f"❌ _startup_tasks: Dependencies failed: {e}", flush=True)
        print(f"   Traceback: {tb.format_exc()}", flush=True)
        return

    print("🔧 _startup_tasks: Building bot application...", flush=True)
    try:
        bot_app = await build_bot_application(deps)
        app.state.bot = bot_app.bot
        bot_application = bot_app
        print("✅ _startup_tasks: Bot application built", flush=True)
    except Exception as e:
        logger.error(f"❌ Bot app failed: {e}\n{tb.format_exc()}")
        print(f"❌ _startup_tasks: Bot app failed: {e}", flush=True)
        return

    logger.info("✅ Bot application built and ready!")
    print("✅ _startup_tasks: Bot application built and ready!", flush=True)
    sys.stdout.flush()

    try:
        from bot.services import QueueWorker
        print("🔧 _startup_tasks: Initializing QueueWorker...", flush=True)
        worker = QueueWorker(bot_application.bot)
        print("✅ _startup_tasks: QueueWorker initialized", flush=True)

        print("🔧 _startup_tasks: Starting QueueWorker background task...", flush=True)
        asyncio.create_task(worker.start())
        print("✅ _startup_tasks: QueueWorker task created", flush=True)
    except Exception as e:
        logger.warning(f"⚠️ QueueWorker: {e}")
        print(f"⚠️ _startup_tasks: QueueWorker error: {e}", flush=True)
        print(f"   Full traceback:\n{tb.format_exc()}", flush=True)

    print("🔧 _startup_tasks: About to create webhook task...", flush=True)
    sys.stdout.flush()

    # Fire off all background tasks (Singleton pattern)
    # 1. Webhook first (Priority 1)
    print("🔧 _startup_tasks: Creating webhook setup task...", flush=True)
    sys.stdout.flush()
    webhook_task = asyncio.create_task(_setup_webhook_bg())
    print(
        f"✅ _startup_tasks: Webhook task created (id={id(webhook_task)})", flush=True
    )
    sys.stdout.flush()

    # 2. Rclone (Priority 2)
    asyncio.create_task(_setup_rclone_bg())

    # 3. Pyrogram (Priority 3 - heavy, so we delay it slightly)
    async def _init_pyrogram_delayed():
        await asyncio.sleep(5)
        await _init_pyrogram_bg()

    asyncio.create_task(_init_pyrogram_delayed())

    # 4. Heavy indexing (Priority 4 - absolute last)
    asyncio.create_task(_background_indexing_job(deps["db_conn"]))

    sys.stdout.flush()
    sys.stderr.flush()

    print("🎉 All startup tasks complete! Service is now online.", flush=True)
    logger.info(
        "🎉 All startup tasks complete! (Server is online, finishing setup in background...)"
    )
    print("📋 Startup Summary:", flush=True)
    print(f"   - Bot ready: {bot_application is not None}", flush=True)
    print(f"   - Webhook URL: {settings.WEBHOOK_URL or 'Not configured'}", flush=True)
    print(f"   - WEBHOOK_SECRET set: {bool(settings.WEBHOOK_SECRET)}", flush=True)
    print(f"   - Database: {deps.get('db_conn') is not None}", flush=True)
    print(
        "🔔 NOTE: Webhook setup is running in background - check logs for 'configure_webhook' or 'Webhook configured'",
        flush=True,
    )
    sys.stdout.flush()
    sys.stderr.flush()


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

# ── Security Headers ──────────────────────────────────────────
from web.utils.security_headers import SecurityHeadersMiddleware

app.add_middleware(SecurityHeadersMiddleware)


# ── Activity & Error Logging Middleware ───────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    import time

    start_time = time.time()
    try:
        response = await call_next(request)
        process_time = (time.time() - start_time) * 1000
        # Only log successes if not static assets to avoid noise
        if not request.url.path.startswith("/static"):
            logger.info(
                f"🌐 {request.method} {request.url.path} - {response.status_code} ({process_time:.2f}ms)"
            )
        return response
    except Exception as e:
        import traceback

        process_time = (time.time() - start_time) * 1000
        logger.error(
            f"💥 REQUEST FAILED: {request.method} {request.url.path}\nError: {e}\n{traceback.format_exc()}"
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error", "error": str(e)},
        )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback

    logger.error(f"🔥 UNHANDLED ERROR: {exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500, content={"detail": "Something went wrong", "error": str(exc)}
    )


# ── CORS ─────────────────────────────────────────────────────
# Build allow_origins safely - never include empty strings
_frontend_url = os.getenv("FRONTEND_URL", "").strip()
_allow_origins = ["https://localhost:3000", "http://localhost:3000"]
if _frontend_url and _frontend_url.startswith(("http://", "https://")):
    _allow_origins.append(_frontend_url)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
    expose_headers=["X-CSRF-Token"],
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
app.include_router(
    auth_router, tags=["Auth"], prefix="/auth"
)  # auth.js uses /auth/request-code
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
    """Simplified health check to prevent Render timeouts during indexing."""
    deps = getattr(app.state, "deps", {})
    db_conn = deps.get("db_conn")

    if not db_conn:
        return JSONResponse(
            {"status": "starting", "db": "disconnected"}, status_code=200
        )

    # During the first 5 minutes of startup, don't block on a Ping
    # if the database might be busy building indexes.
    db_ok = True
    try:
        # 1s timeout is plenty for a healthy DB.
        # If it takes longer, we'll just report 'busy' but return 200 to keep Render happy.
        await asyncio.wait_for(db_conn.db.command("ping"), timeout=1.0)
    except Exception:
        # Fail the ping but don't fail the health check yet during startup
        db_ok = False

    status = "healthy" if db_ok else "indexing_or_busy"

    return {
        "status": status,
        "bot_ready": bot_application is not None,
        "bot_username": settings.BOT_USERNAME,
        "db_connected": True,
        "webhook_configured": _webhook_status.get("configured", False),
        "webhook_url": _webhook_status.get("url") or settings.WEBHOOK_URL or None,
        "webhook_error": _webhook_status.get("error"),
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
    if not secret:
        logger.critical(
            "🚨 WEBHOOK_SECRET is not configured! Rejecting webhook request."
        )
        raise HTTPException(
            status_code=500,
            detail="Webhook secret not configured. Set WEBHOOK_SECRET environment variable.",
        )

    incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if incoming != secret:
        logger.warning(f"🚫 Webhook rejected: invalid secret token")
        raise HTTPException(status_code=403, detail="Invalid secret token")

    body = await request.body()
    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    update = Update.de_json(data, bot_application.bot)
    logger.info(f"📥 Received webhook update (id={update.update_id})")
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
