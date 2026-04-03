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
from typing import Optional

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

print(f"🚀 main.py: Module loaded. CWD={os.getcwd()}", flush=True)
print(f"🚀 main.py: Python version={sys.version}", flush=True)

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
    if domain := os.getenv("DOMAIN"):
        if not domain.startswith(("http://", "https://")):
            return f"https://{domain}"
        return domain
    return ""


def validate_environment() -> None:
    """Check required settings and log environment info."""
    logger.info("🔍 ENV BOT_TOKEN set: %s", bool(settings.BOT_TOKEN))
    logger.info("🔍 ENV WEBHOOK_URL set: %s", bool(settings.WEBHOOK_URL))
    logger.info(
        "🔍 ENV RENDER_EXTERNAL_URL: %s", os.getenv("RENDER_EXTERNAL_URL") or "None"
    )

    # Auto-deduce webhook if requested OR if running on Render/with a DOMAIN
    if not settings.WEBHOOK_URL:
        if (
            os.getenv("USE_WEBHOOK") == "true"
            or os.getenv("RENDER_SERVICE_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or os.getenv("DOMAIN")
        ):
            deduced = deduce_webhook_url()
            if deduced:
                settings.WEBHOOK_URL = deduced.rstrip("/") + "/webhook/telegram"
                logger.info("🔍 Auto webhook URL: %s", settings.WEBHOOK_URL)

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
        # NOTE: ^view_logs_ and ^admin_chatbox$ are already registered in Section 5.
        # Only unique patterns belong here to avoid silent duplicate registrations.
        for pattern, handler in [
            ("^view_admin_logs$", handle_admin_logs_menu),
            ("^download_logs$", handle_admin_download_logs),
            ("^clear_old_logs$", handle_admin_clear_logs),
            ("^view_error_logs$", handle_view_error_logs),
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

    logger.info("DEBUG: Creating JobQueue")
    job_queue = JobQueue()

    logger.info("DEBUG: Building Application")
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
    logger.info("DEBUG: Application built")

    job_queue.set_application(application)

    logger.info("DEBUG: Initializing application")
    await application.initialize()
    logger.info("DEBUG: Starting application")
    await application.start()
    logger.info("DEBUG: Application started")

    # Cache bot username immediately after initialize (getMe already called internally)
    try:
        me = await application.bot.get_me()
        if me and me.username:
            settings.BOT_USERNAME = me.username
            settings.BOT_LINK = f"https://t.me/{me.username}"
            logger.info("DEBUG: Bot username cached from initialize: @%s", me.username)
    except Exception as e:
        logger.warning("DEBUG: Could not cache bot username: %s", e)

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
    logger.info("DEBUG: bot_data updated")

    logger.info("DEBUG: Setting up handlers")
    setup_handlers(application)
    logger.info("DEBUG: Handlers set up")

    logger.info("✅ Bot application built | webhook: %s", settings.WEBHOOK_URL)
    return application


def get_safe_secret(raw_secret: str) -> Optional[str]:
    """Sanitize the webhook secret to only allowed chars (A-Z, a-z, 0-9, _, -)."""
    if not raw_secret:
        return None
    import re

    safe = re.sub(r"[^a-zA-Z0-9_-]", "", str(raw_secret))
    return safe if safe else None


def _compute_webhook_max_connections(user_count: Optional[int]) -> int:
    """Compute webhook max_connections based on active parallel user count.

    Strategy:
      - Base floor: 40  (Telegram minimum recommended)
      - Scale: +2 connections per active user
      - Hard ceiling: 100 (Telegram maximum)
    """
    if not user_count or user_count <= 0:
        return 40
    scaled = 40 + (user_count * 2)
    return min(scaled, 100)


async def configure_webhook(
    bot, webhook_url: str, secret: str, user_count: Optional[int] = None
) -> bool:
    """Set (or update) the Telegram webhook.

    max_connections is computed dynamically from *user_count* so that
    Telegram allocates more delivery workers as parallel load grows.
    Call this again with the new user_count whenever the concurrency
    level changes — Telegram will update without dropping pending updates.
    """
    try:
        # --- 1. Secret Protector ---
        # If the secret mistakenly matches the URL, it means it's misconfigured in Render.
        # We must ignore it to avoid Telegram API hangs.
        if secret and (
            "http://" in secret.lower()
            or "https://" in secret.lower()
            or secret.strip() == webhook_url.strip()
        ):
            logger.error(
                "🚨 CRITICAL: WEBHOOK_SECRET is set to a URL! This is a misconfiguration."
            )
            logger.error(
                "👉 FIX: Change WEBHOOK_SECRET in Render to a random string (e.g. 'my_secret_123'), NOT your bot URL."
            )
            logger.warning("⚠️ Proceeding WITHOUT secret_token protection to avoid hang...")
            secret = ""

        # --- 2. Sanitization ---
        import re

        safe_secret = get_safe_secret(secret)
        if secret and not safe_secret:
            logger.warning(
                "⚠️ WEBHOOK_SECRET was entirely invalid! Disabling protection."
            )
        elif safe_secret and safe_secret != secret:
            logger.warning(f"⚠️ WEBHOOK_SECRET cleaned: {secret} -> {safe_secret}")

        # --- 3. Dynamic max_connections scaling ---
        max_conn = _compute_webhook_max_connections(user_count)

        logger.info(
            "configure_webhook: Attempting bot.set_webhook | url=%s | max_conn=%d | secret_len=%d",
            webhook_url,
            max_conn,
            len(safe_secret) if safe_secret else 0,
        )

        # --- 4. API Call with Internal Timeout ---
        # We use a 12s internal timeout to fail-fast if api.telegram.org is slow/blocked.
        await asyncio.wait_for(
            bot.set_webhook(
                url=webhook_url,
                max_connections=max_conn,
                secret_token=safe_secret,
                drop_pending_updates=False,
            ),
            timeout=12.0,
        )
        logger.info(
            "✅ configure_webhook: bot.set_webhook COMPLETED (max_connections=%d)", max_conn
        )
        return True
    except Exception as exc:
        logger.error("❌ Webhook failed: %s", exc)
        return False


async def update_webhook_capacity(bot, user_count: int) -> bool:
    """Re-configure webhook max_connections when concurrency changes.

    Call this whenever the number of parallel users increases or decreases
    so Telegram adjusts its delivery capacity accordingly.
    """
    if not settings.WEBHOOK_URL:
        return False  # Polling mode — nothing to update
    return await configure_webhook(
        bot,
        settings.WEBHOOK_URL,
        settings.WEBHOOK_SECRET or "",
        user_count=user_count,
    )


# ============================================================
# CLEANUP
# ============================================================


async def cleanup_bot(application: Application, db_conn) -> None:
    """Gracefully stop PTB application and DB connection."""
    from bot.pyrogram_client import stop_pyrogram
    from bot.services import QueueWorker

    try:
        worker = QueueWorker.get_instance()
        await worker.stop()
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        if "QueueWorker NOT initialized" not in str(e):
            logger.warning(f"⚠️ Could not stop QueueWorker: {e}")

    await application.stop()
    await application.shutdown()
    await stop_pyrogram()
    if db_conn:
        await db_conn.close()
    import logging
    logger = logging.getLogger(__name__)
    logger.info("🛑 Cleanup complete")


# ============================================================
# FASTAPI LIFESPAN
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan - port binds IMMEDIATELY, all heavy work runs in background."""
    global bot_application

    logger.info("🚀 Starting up (port will bind immediately)...")

    validate_environment()

    asyncio.create_task(_full_startup(app))

    yield  # PORT BINDS HERE - Render health check will succeed

    logger.info("🛑 Shutting down...")
    if bot_application:
        try:
            db_conn = getattr(app.state, "deps", {}).get("db_conn")
            await cleanup_bot(bot_application, db_conn)
        except Exception as e:
            logger.error(f"❌ Cleanup error: {e}")
    logger.info("🛑 Shutdown complete")


async def _full_startup(app: FastAPI):
    """Run ALL startup tasks in background after port binds."""
    import traceback as tb

    logger.info("🚀 Full startup beginning...")

    # Step 1: Build dependency graph (DB connection)
    try:
        logger.info("[STARTUP-1] Connecting to database...")
        deps = await asyncio.wait_for(build_dependency_graph(), timeout=30.0)
        app.state.deps = deps
        logger.info("[STARTUP-1] ✅ Dependencies ready (DB connected and shared)")
    except asyncio.TimeoutError:
        logger.error("[STARTUP-1] ❌ DB connection TIMED OUT after 30s")
        return
    except Exception as e:
        logger.error(f"[STARTUP-1] ❌ Dependencies failed: {e}\n{tb.format_exc()}")
        return

    # Step 2: Build bot application (handlers)
    try:
        logger.info("[STARTUP-2] Building bot application...")
        bot_app = await asyncio.wait_for(build_bot_application(deps), timeout=45.0)
        
        logger.info("[STARTUP-2] bot_app variable received. Storing in app.state...")
        app.state.bot = bot_app.bot
        global bot_application
        bot_application = bot_app
        
        logger.info("[STARTUP-2] ✅ Bot application built, initialized, and started.")

        # Process any updates that arrived during startup
        if _pending_updates:
            logger.info(
                f"[STARTUP-2] 📥 Processing {_pending_updates.__len__()} queued updates..."
            )
            for data in _pending_updates:
                try:
                    update = Update.de_json(data, bot_application.bot)
                    asyncio.create_task(bot_application.process_update(update))
                except Exception:
                    pass
            _pending_updates.clear()
    except asyncio.TimeoutError:
        logger.error("[STARTUP-2] ❌ Bot app build TIMED OUT after 45s")
        return
    except Exception as e:
        logger.error(f"[STARTUP-2] ❌ Bot app failed: {e}\n{tb.format_exc()}")
        return

    # Step 3: Start QueueWorker
    try:
        logger.info("[STARTUP-3] Starting QueueWorker...")
        logger.info("[STARTUP-3] DEBUG: Importing QueueWorker")
        from bot.services import QueueWorker

        logger.info("[STARTUP-3] DEBUG: Creating QueueWorker instance")
        worker = QueueWorker(bot_application.bot)
        logger.info("[STARTUP-3] DEBUG: Starting QueueWorker synchronously...")
        await worker.start()
        logger.info("[STARTUP-3] ✅ QueueWorker started")
    except Exception as e:
        logger.warning(f"[STARTUP-3] ⚠️ QueueWorker: {e}")

    # Bot username was cached during application.initialize() in build_bot_application
    logger.info("🤖 @%s ready | Webhook URL: %s", settings.BOT_USERNAME or "unknown", settings.WEBHOOK_URL or "POLLING")

    # Step 4: Configure webhook OR start long polling
    logger.info("[STARTUP-4] 🔧 Starting Webhook/polling setup...")
    webhook_success = False
    if settings.WEBHOOK_URL:
        try:
            logger.info("[STARTUP-4] 🔧 Setting webhook...")
            webhook_success = await asyncio.wait_for(
                configure_webhook(
                    bot_application.bot,
                    settings.WEBHOOK_URL,
                    settings.WEBHOOK_SECRET or "",
                    None,
                ),
                timeout=30.0,
            )
            if webhook_success:
                logger.info("[STARTUP-4] ✅ Webhook configured")
            else:
                logger.warning("[STARTUP-4] ⚠️ Webhook setup failed, falling back to polling...")
        except Exception as e:
            logger.error(f"[STARTUP-4] ❌ Webhook error: {e}")

    if not webhook_success:
        updater = getattr(bot_application, "updater", None)
        if updater is not None:
            try:
                logger.info("[STARTUP-4] 🔧 Starting long polling as fallback...")
                await updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES,
                    close_loop=False,
                )
                logger.info("[STARTUP-4] ✅ Long polling started")
            except Exception as e:
                logger.error(f"[STARTUP-4] ❌ Polling fallback error: {e}")
        else:
            logger.warning(
                "[STARTUP-4] ⚠️ No Updater available (webhook-mode build) and webhook failed. "
                "Bot will process updates only via incoming webhook POSTs."
            )

    # Run remaining startup tasks in background
    async def startup_remaining():
        # Step 5: Initialize Pyrogram
        logger.info("[STARTUP-5] 🔧 Starting Pyrogram clients...")
        try:
            from bot.pyrogram_client import init_pyrogram

            success = await asyncio.wait_for(init_pyrogram(), timeout=30.0)
            if success:
                logger.info("[STARTUP-5] ✅ Pyrogram started")
            else:
                logger.info("[STARTUP-5] ⚠️ Pyrogram initialization skipped or failed")
        except asyncio.TimeoutError:
            logger.error("[STARTUP-5] ❌ Pyrogram TIMED OUT")
        except Exception as e:
            logger.warning(f"[STARTUP-5] ⚠️ Pyrogram error: {e}")

        # Step 6: Rclone setup
        logger.info("[STARTUP-6] 🔧 Checking rclone binary...")
        try:
            from bot.services._cloud_upload import ensure_rclone_binary

            path = await asyncio.wait_for(ensure_rclone_binary(), timeout=60.0)
            if path:
                logger.info("[STARTUP-6] ✅ Rclone ready")
            else:
                logger.info("[STARTUP-6] ⚠️ Rclone not configured")
        except asyncio.TimeoutError:
            logger.error("[STARTUP-6] ❌ Rclone TIMED OUT")
        except Exception as e:
            logger.warning(f"[STARTUP-6] ⚠️ Rclone error: {e}")

        logger.info("🎉 ALL STARTUP COMPLETE - Bot is fully operational!")

    asyncio.create_task(startup_remaining())


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

    db_ok = True
    db_status = "connected"
    try:
        if db_conn:
            await asyncio.wait_for(db_conn.db.command("ping"), timeout=1.0)
        else:
            db_ok = False
            db_status = "disconnected"
    except Exception as e:
        db_ok = False
        db_status = f"ping_failed: {str(e)[:50]}"

    return {
        "status": "healthy" if db_ok else "degraded",
        "bot_ready": bot_application is not None,
        "bot_username": settings.BOT_USERNAME or "unknown",
        "bot_link": settings.BOT_LINK or f"https://t.me/{settings.BOT_USERNAME}",
        "db_status": db_status,
    }


# ============================================================
# TELEGRAM WEBHOOK RECEIVER
# ============================================================

WEBHOOK_PATH = "/webhook/telegram"

_pending_updates: list = []


@app.get(WEBHOOK_PATH, include_in_schema=False)
async def telegram_webhook_get(request: Request):
    """Telegram webhook verification - must return 200 for setWebhook to work."""
    return JSONResponse({"ok": True})


@app.post(WEBHOOK_PATH, include_in_schema=False)
async def telegram_webhook(request: Request):
    if bot_application is None:
        body = await request.body()
        try:
            data = json.loads(body)
            update_id = data.get("update_id", "?")
            logger.info(f"📥 Queued update during startup: {update_id}")
            _pending_updates.append(data)
            if len(_pending_updates) > 100:
                _pending_updates.pop(0)
        except Exception:
            pass
        return JSONResponse({"ok": True})

    if not settings.WEBHOOK_SECRET:
        logger.critical("🚨 WEBHOOK_SECRET not configured in settings!")
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    safe_expected = get_safe_secret(settings.WEBHOOK_SECRET)
    incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

    if not safe_expected or incoming != safe_expected:
        logger.warning(
            f"🚫 Webhook rejected: secret mismatch (expected_safe_len={len(safe_expected) if safe_expected else 0})"
        )
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
