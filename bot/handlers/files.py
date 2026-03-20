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
from bot.utils import (
    send_auto_delete_msg,
    log_info,
    log_error,
    log_user_update,
    validate_url,
    validate_file_size,
)
from bot.services import create_or_update_storage_message, FFmpegService
from core.exceptions import DownloadError
from bot.handlers.settings import handle_edit_max_filesize
from pathlib import Path
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


async def handle_cleanup_old_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clean up old/expired files from database"""
    try:
        await update.callback_query.answer("⏳ Cleaning up...", show_alert=True)
        result = await cleanup_old_cloud_files()
        deleted = (
            result.get("deleted_count", 0) if result and result.get("success") else 0
        )
        stats_text = f"🗑️ **Cleanup Complete**\n\nDeleted: `{deleted}` expired files.\n"
        await update.callback_query.message.edit_text(
            stats_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="admin_filesize")]]
            ),
        )
        await log_admin_action(
            update.effective_user.id, "cleanup_files", {"deleted": deleted}
        )
        logger.info(f"✅ Cleanup done: {deleted} files removed")
    except Exception as e:
        logger.error(f"❌ Error in cleanup: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


async def handle_set_max_filesize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt admin to set max file size (registered as CallbackQueryHandler in main.py)"""
    # Delegates to the text-prompt handler
    await handle_edit_max_filesize(update, context)


async def handle_file_upload(
    update: Update, context: ContextTypes.DEFAULT_TYPE, resumed_file=False, file_id=None
):
    """Handle direct file uploads"""
    try:
        user_id = update.effective_user.id

        # 0️⃣ ✅ CHECK WIZARD INJECTION STATE (keyed per file to avoid race conditions)
        # Read file_unique_id first to scope the state lookup
        _inject_file_obj = (
            update.message.document or update.message.audio or update.message.video
        )
        _fuid = (
            getattr(_inject_file_obj, "file_unique_id", None)
            if _inject_file_obj
            else None
        )
        # Check both file-scoped key and legacy top-level key for backwards compat
        awaiting = context.user_data.get(f"awaiting_{_fuid}") if _fuid else None
        awaiting = awaiting or context.user_data.get("awaiting")
        if awaiting and awaiting.startswith("wiz_inject_"):
            # Handle Injection
            file_obj = (
                update.message.document or update.message.audio or update.message.video
            )
            if not file_obj:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Please send a valid file.",
                    parse_mode="Markdown",
                )
                return

            # Download
            wait_msg = await update.message.reply_text("📥 Receving injection file...")
            path = await (await file_obj.get_file()).download_to_drive()
            await wait_msg.delete()

            session = context.user_data.get("wizard")
            if not session:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ Session expired.",
                    parse_mode="Markdown",
                )
                return

            if awaiting == "wiz_inject_audio":
                session["injected_audio"].append(str(path))
                await update.message.reply_text(
                    "✅ Audio track added!",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Back to Audio", callback_data="wiz_menu_audio"
                                )
                            ]
                        ]
                    ),
                )
            elif awaiting == "wiz_inject_subs":
                session["injected_subs"].append(str(path))
                await update.message.reply_text(
                    "✅ Subtitle track added!",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Back to Subs", callback_data="wiz_menu_subs"
                                )
                            ]
                        ]
                    ),
                )

            # Clear both scoped and legacy state keys
            if _fuid:
                context.user_data.pop(f"awaiting_{_fuid}", None)
            context.user_data.pop("awaiting", None)
            return

        # 0.5️⃣ ✅ LOCK SESSION IF IN WIZARD
        in_wizard = context.user_data.get("wizard") is not None
        if in_wizard or awaiting:
            if update.message:
                msg = await update.message.reply_text(
                    "⚠️ **Active Session**\n\nPlease finish or cancel your curren action before sending a new file."
                )
                from bot.utils import send_auto_delete_msg, auto_delete_message
                import asyncio

                asyncio.create_task(
                    auto_delete_message(
                        context.bot, update.effective_chat.id, msg.message_id, 7
                    )
                )
            return

        # 1️⃣ Get user
        user = await get_user(user_id)
        if not user:
            if update.message:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ User not found.",
                    parse_mode="Markdown",
                )
            return

        if user.get("banned"):
            if update.message:
                await update.message.reply_text(
                    ERROR_MESSAGES.get("banned", "You are banned.")
                )
            return

        # 1.5️⃣ Check Force Sub
        if not resumed_file:
            # Prepare metadata for potential resume
            file_obj = (
                update.message.document or update.message.video or update.message.audio
            )
            if file_obj:
                p_data = {
                    "type": "file",
                    "file_id": file_obj.file_id,
                    "file_name": getattr(file_obj, "file_name", "file"),
                    "file_size": getattr(file_obj, "file_size", 0),
                }
                from bot.handlers.user import check_force_sub

                if not await check_force_sub(update, context, pending_data=p_data):
                    return

        # 2️⃣ ✅ CHECK RCLONE IS CONFIGURED
        config = await get_config()
        if not config:
            await update.message.reply_text(
                "🔧 **Bot Under Maintenance**\n\n"
                "Configuration not loaded.\n"
                "Please contact admin.",
                parse_mode="Markdown",
            )
            logger.warning(f"File upload rejected for {user_id}: rclone not enabled")
            return

        # Get file object
        file_obj = None
        if update.message:
            file_obj = (
                update.message.document or update.message.video or update.message.audio
            )

        # If resuming, we might have file_id but no message attachment in this update
        if not file_obj and file_id:
            # We can't get the full metadata from just file_id without a call
            # So we rely on user_data which was saved in handle_file_upload initially
            pending_data = context.user_data.get("pending_fsub_data", [])
            if not isinstance(pending_data, list):
                pending_data = [pending_data]
            pending = next(
                (p for p in pending_data if p and p.get("file_id") == file_id), None
            )

            if pending:
                # Mock a file object for the rest of the logic
                from dataclasses import dataclass

                @dataclass
                class MockFile:
                    file_id: str
                    file_name: str
                    file_size: int

                    async def get_file(self):
                        return await context.bot.get_file(self.file_id)

                file_obj = MockFile(
                    file_id=file_id,
                    file_name=pending.get("file_name", "file"),
                    file_size=pending.get("file_size", 0),
                )

        if not file_obj:
            return

        # 2.5️⃣ ✅ PREEMPTIVE SIZE CHECK
        file_size = getattr(file_obj, "file_size", 0)
        from bot.utils import send_auto_delete_msg, validate_file_size

        is_val, err = validate_file_size(file_size, user.get("plan", "free"))
        if not is_val:
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                f"❌ {err}\n\n{ERROR_MESSAGES.get('file_too_large', '')}",
                parse_mode="Markdown",
            )
            return

        filename = getattr(file_obj, "file_name", "file")

        # Apply word removal from user settings
        remove_words = user.get("settings", {}).get("remove_words", [])
        if remove_words:
            import re

            words_sorted = sorted(remove_words, key=len, reverse=True)
            for word in words_sorted:
                pattern = re.compile(re.escape(word), re.IGNORECASE)
                filename = pattern.sub("", filename)
            # Cleanup weird spaces left over by replacements
            filename = " ".join(filename.split())
            if not filename.strip() or filename == ".":
                filename = "file"

        from bot.utils import send_auto_delete_msg, sanitize_filename

        filename = sanitize_filename(filename)

        file_size = getattr(file_obj, "file_size", 0)
        file_unique_id = getattr(file_obj, "file_unique_id", None)

        # 3️⃣ ✅ CHECK FOR DUPLICATES (FINGERPRINTING)
        if file_unique_id:
            from bot.database import get_db

            db = get_db()
            existing_task = await db.tasks.find_one(
                {
                    "user_id": user_id,
                    "metadata.file_unique_id": file_unique_id,
                    "status": {"$in": ["pending", "processing"]},
                }
            )
            if existing_task:
                e_tid = existing_task.get("task_id", "unknown")
                await update.message.reply_text(
                    f"⚠️ **Wait inline!**\n\n"
                    f"This exact file is already in progress for you.\n"
                    f"Active Task ID: `{e_tid}`",
                    parse_mode="Markdown",
                )
                return

        # Create task
        metadata = {"file_unique_id": file_unique_id} if file_unique_id else {}
        task_id = await create_task(
            user_id, file_obj.file_id, "upload", metadata=metadata
        )

        if not task_id:
            await update.message.reply_text(
                ERROR_MESSAGES.get("database_error", "Database error")
            )
            return

        # 6️⃣ ✅ LAUNCH WIZARD (Optimized: No immediate download)
        await WizardHandler.start_wizard(
            update,
            context,
            file_path=None,
            file_id=file_obj.file_id,
            file_name=filename,  # Pass Safe Display Name
            file_size=file_size,
            task_id=task_id,
        )

        logger.info(f"✅ Wizard started for task without downloading: {task_id}")
        logger.info(f"✅ Wizard started for task: {task_id}")

    except Exception as e:
        logger.error(f"❌ File upload error: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ **Error Processing File**\n\n{str(e)[:100]}", parse_mode="Markdown"
        )


async def process_file_task(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    task_id = job.data["task_id"]
    user_id = job.data["user_id"]
    file_obj = job.data.get("file_obj")  # Or download if URL

    # 1. Download (if URL) or get local path
    display_name = "file"
    if isinstance(file_obj, str):  # URL
        from bot.services import download_from_url

        download_result = await download_from_url(file_obj, user_id, task_id, get_db())
        file_path = download_result["file_path"]
        display_name = download_result[
            "filename"
        ]  # Get sanitized display name from downloader
    else:  # Direct file
        # It's a Telegram File object
        display_name = file_obj.file_name
        # Note: Direct files are downloaded in handle_file_upload, but here we might be reprocessing?
        # Actually, handle_file_upload starts WizardHandler directly.
        # This process_file_task seems to be for URL tasks or legacy flow.
        # If it's used for direct files, we need to handle it.
        # But based on code, this is primarily triggered for URLs or if handle_file_upload queues it.
        file_path = await (await file_obj.get_file()).download_to_drive()  # Temp path

    # 2. Upload + Send
    from bot.services import upload_and_send_file
    from bot.database import get_user, get_channel_id

    user = await get_user(user_id)
    config = await get_config()
    dump_channel = await get_channel_id("dump") or config.get("dump_channel_id")

    visibility = user.get("settings", {}).get("visibility", "public")

    result = await upload_and_send_file(
        bot=context.bot,
        user_id=user_id,
        file_path=file_path,
        user_plan=user.get("plan", "free"),
        custom_filename=display_name,  # ✅ Fixed: Use variable
        visibility=visibility,
        task_id=task_id,
    )

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"✅ File added to queue!\n\n"
            f"📄 File: {file_obj.file_name}\n"
            f"📦 Size: {file_obj.file_size / (1024**3):.2f} GB\n\n"
            f"⏳ Processing will start shortly..."
        ),
        parse_mode="Markdown",
    )

    # 3. Generate Link
    # 3. Generate Link with Secure Token
    from bot.database import create_one_time_key
    import secrets
    from datetime import datetime, timedelta

    dump_file_id = result[
        "file_id"
    ]  # file_id returned after send_video/send_document to dump

    # Generate a cryptographically secure 32-character token
    secure_token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=24)
    await create_one_time_key(user_id, secure_token, expires, purpose="stream")

    # Send user the verification link - this will set the cookie and redirect to clean stream url
    stream_url = (
        f"https://{settings.DOMAIN}/api/verify_link/{dump_file_id}/{secure_token}"
    )
    link_info = {"link": stream_url}

    # 4. Update Task & Storage Msg
    await update_task(task_id, {"status": "completed", "link": link_info["link"]})

    await create_or_update_storage_message(
        context.bot,
        {
            "file_id": result["file_id"],
            "filename": file_obj.file_name
            if hasattr(file_obj, "file_name")
            else display_name,
            "size": file_obj.file_size if hasattr(file_obj, "file_size") else 0,
        },
    )

    keyboard = []
    if stream_url:
        keyboard = [
            [
                InlineKeyboardButton("📺 VLC Player", url=f"vlc://{stream_url}"),
                InlineKeyboardButton(
                    "📱 MX Player",
                    url=f"intent:{stream_url}#Intent;package=com.mxtech.videoplayer.ad;S.title={display_name};end",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📤 Send to Destination", callback_data=f"send_dest_{dump_file_id}"
                )
            ],
        ]

    # 5. ✅ SEND RESULT TO USER PM
    await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"✅ **File Ready!**\n\n"
            f"🔗 Stream Link: {stream_url}\n\n"
            f"🆔 Task: `{task_id}`\n\n"
            f"⚠️ **Note**: If the file stream is not playable, please use an external player like MX Player, PlayIt, or VLC."
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )

    # Cleanup temp file
    Path(file_path).unlink(missing_ok=True)

    logger.info(f"✅ Full flow complete: {task_id}")


async def handle_url_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, resumed_url=None
) -> None:
    """Handle URL input from user"""
    try:
        user_id = update.effective_user.id
        url = resumed_url or update.message.text.strip()

        # Validate URL
        from bot.utils import send_auto_delete_msg, validate_url

        is_valid, error_msg = validate_url(url)
        if not is_valid:
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                f"❌ {error_msg}",
                parse_mode="Markdown",
            )
            return

        # Get user
        user = await get_user(user_id)
        if not user:
            if update.message:
                await send_auto_delete_msg(
                    context.bot,
                    update.effective_chat.id,
                    "❌ User not found.",
                    parse_mode="Markdown",
                )
            return

        if user.get("banned"):
            if update.message:
                await update.message.reply_text(ERROR_MESSAGES["banned"])
            return

        # Check Force Sub
        from bot.handlers.user import check_force_sub

        if not resumed_url:
            if not await check_force_sub(
                update, context, pending_data={"type": "url", "url": url}
            ):
                return

        # Create task
        user = await get_user(user_id)
        task_id = await create_task(
            user_id=user_id,
            file_id="url_task",  # dummy file_id, we don’t have one
            task_type="url",
            metadata={"url": url},
        )

        if not task_id:
            await update.message.reply_text(ERROR_MESSAGES["database_error"])
            return

        # Send processing message
        processing_msg = await update.message.reply_text(
            f"🔄 Analyzing URL...\n\nTask ID: `{task_id}`", parse_mode="Markdown"
        )

        try:
            # Analyze URL with yt-dlp
            from bot.services import analyze_url_with_ytdlp

            analysis = await analyze_url_with_ytdlp(url)
            if not analysis:
                raise DownloadError("Analysis failed")

            filename = analysis["filename"]
            filesize = analysis["filesize"]

            # Validate size
            is_valid, error_msg = validate_file_size(filesize, user["plan"])
            if not is_valid:
                await processing_msg.edit_text(f"❌ {error_msg}")
                await update_task(
                    task_id, {"status": "failed", "error_message": error_msg}
                )
                return

            # Update task with file info
            await update_task(
                task_id,
                {
                    "status": "queued",
                    "file_data": {
                        "type": "url",
                        "url": url,
                        "filename": filename,
                        "size": filesize,
                    },
                },
            )

            # Edit message
            await processing_msg.edit_text(
                f"✅ URL processed!\n\n"
                f"File: {filename}\n"
                f"Size: {filesize / (1024**3):.2f}GB\n\n"
                f"Queued for processing..."
            )

            logger.info(f"✅ URL queued: {task_id} ({filename})")

            # ✅ TRIGGER BACKGROUND JOB
            if context.job_queue:
                context.job_queue.run_once(
                    process_file_task,
                    when=0,
                    data={"task_id": task_id, "user_id": user_id, "file_obj": url},
                )

        except Exception as e:
            logger.error(f"❌ URL analysis failed: {e}")
            await processing_msg.edit_text(f"❌ Error analysis: {str(e)[:100]}")
            await update_task(task_id, {"status": "failed", "error_message": str(e)})

    except Exception as e:
        logger.error(f"❌ Error in handle_url_input: {e}", exc_info=True)
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            f"❌ Error: {str(e)[:100]}",
            parse_mode="Markdown",
        )


async def handle_document_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads (files to process)"""
    try:
        user_id = update.effective_user.id
        awaiting = context.user_data.get("awaiting")

        logger.info(f"📄 Document received from user {user_id}")
        logger.info(f"   Awaiting state: {awaiting}")

        if awaiting:
            # ✅ ALLOW WIZARD INJECTIONS
            if awaiting.startswith("wiz_inject_"):
                # Let handle_file_upload deal with it (injections need file processing)
                logger.info("   Allowing wizard injection file pass-through")
                await handle_file_upload(update, context)  # MUST process it!
                return

        in_wizard = context.user_data.get("wizard") is not None
        if in_wizard or awaiting:
            logger.info(f"   User in awaiting/wizard state, blocking file")
            msg = await update.message.reply_text(
                "⚠️ **Active Session**\n\n"
                "Please finish or cancel your current operation first.\n\n"
                "Use /cancel to cancel current operation.",
                parse_mode="Markdown",
            )
            from bot.utils import send_auto_delete_msg, auto_delete_message
            import asyncio

            asyncio.create_task(
                auto_delete_message(
                    context.bot, update.effective_chat.id, msg.message_id, 10
                )
            )
            return  # ← FIX: MUST RETURN

        # No awaiting state — process as a normal file upload
        await handle_file_upload(update, context)

    except Exception as e:
        logger.error(f"❌ Error handling document: {e}", exc_info=True)


async def handle_wizard_text_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
):
    """Handle text input specifically for wizard actions like rename and metadata"""
    try:
        awaiting = context.user_data.get("awaiting")
        if awaiting == "wiz_rename" or (awaiting and awaiting.startswith("wiz_meta_")):
            session = context.user_data.get("wizard")
            if not session:
                await update.message.reply_text("⚠️ Wizard session expired.")
                context.user_data.pop("awaiting", None)
                return

            if text.startswith("-"):
                text = text.lstrip("-")

            if awaiting == "wiz_rename":
                from bot.utils import send_auto_delete_msg, sanitize_filename

                # Sanitize explicitly against command injection & path traversal
                safe_name = sanitize_filename(text)

                # Additional harden: Do not allow the new name to start with hyphens
                if safe_name.startswith("-"):
                    safe_name = safe_name.lstrip("-")

                session["rename"] = safe_name
                context.user_data.pop("awaiting", None)

                # Send back the wizard menu
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "🎵 Audio Tracks", callback_data="wiz_menu_audio"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📝 Subtitles", callback_data="wiz_menu_subs"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "✏️ Rename", callback_data="wiz_rename_prompt"
                        ),
                        InlineKeyboardButton(
                            "🏷️ Metadata", callback_data="wiz_menu_metadata"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "🚀 Proceed", callback_data="wiz_process_now"
                        )
                    ],
                    [InlineKeyboardButton("❌ Cancel", callback_data="wiz_cancel")],
                ]

                try:
                    await update.message.delete()
                except Exception:
                    pass

                await update.message.reply_text(
                    f"✅ **Renamed** to `{safe_name}`\n\n🎛️ **Editor Menu**\n\nSelect what you want to change:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )

            elif awaiting.startswith("wiz_meta_"):
                tag = awaiting.replace("wiz_meta_", "")
                if "custom_metadata" not in session:
                    session["custom_metadata"] = {}
                session["custom_metadata"][tag] = text
                context.user_data.pop("awaiting", None)

                keyboard = [
                    [
                        InlineKeyboardButton(
                            "🎬 Title", callback_data="wiz_meta_prompt_title"
                        ),
                        InlineKeyboardButton(
                            "👤 Artist", callback_data="wiz_meta_prompt_artist"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "📅 Year", callback_data="wiz_meta_prompt_date"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🔙 Back to Editor", callback_data="wiz_edit"
                        )
                    ],
                ]

                try:
                    await update.message.delete()
                except Exception:
                    pass

                await update.message.reply_text(
                    f"✅ **{tag.title()} set to:** `{text}`\n\n📝 **Metadata Editor**",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )

    except Exception as e:
        logger.error(f"❌ Error in wizard text input: {e}", exc_info=True)
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            f"❌ Error: {str(e)[:100]}",
            parse_mode="Markdown",
        )


async def myfiles_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /myfiles command - Direct access to user files"""
    try:
        user_id = update.effective_user.id

        files = await get_user_files(user_id)

        if not files:
            await update.message.reply_text(
                "📂 **My Files**\n\n"
                "You haven't processed any files yet.\n\n"
                "Send a file or URL to get started!",
                parse_mode="Markdown",
            )

        await log_info(f"✅ /myfiles used by {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in myfiles command: {e}", exc_info=True)
        await log_error(f"❌ Error in myfiles command: {str(e)}")
        await send_auto_delete_msg(
            context.bot,
            update.effective_chat.id,
            "❌ Unable to load files. Please try again.",
            parse_mode="Markdown",
        )


async def handle_us_thumbnail_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: show thumbnail options or prompt"""
    try:
        query = update.callback_query
        user_id = update.effective_user.id
        from bot.database import get_user

        user = await get_user(user_id)
        settings = user.get("settings", {})
        thumb = settings.get("thumbnail_file_id")

        await query.answer()
        context.user_data["awaiting"] = "us_thumbnail"

        if thumb:
            # Sub-menu for existing thumbnail
            text = "🖼️ **Custom Thumbnail Set**\n\nYou already have a custom thumbnail. What would you like to do?"
            keyboard = [
                [
                    InlineKeyboardButton(
                        "👁️ View Thumb", callback_data="us_thumbnail_view"
                    ),
                    InlineKeyboardButton(
                        "🗑️ Delete Thumb", callback_data="us_thumbnail_delete"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🖼️ Set New (Send Photo)", callback_data="ignore"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back to Settings", callback_data="go_back_to_settings"
                    )
                ],
            ]
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            # Simple prompt
            text = "🖼️ **Set Custom Thumbnail**\n\nSend me a photo to use as your thumbnail.\n\nOr type `auto` to use auto-generated thumbnails."
            keyboard = [
                [InlineKeyboardButton("🔙 Back", callback_data="go_back_to_settings")]
            ]
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
            )

    except Exception as e:
        logger.error(f"Error in handle_us_thumbnail_menu: {e}", exc_info=True)


async def handle_us_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """MessageHandler: receive the actual photo"""
    try:
        awaiting = context.user_data.get("awaiting")
        if awaiting != "us_thumbnail":
            return

        user_id = update.effective_user.id
        photo = update.message.photo[-1]

        # 1. Size Validation (Limit to 5MB)
        if photo.file_size > 5 * 1024 * 1024:
            await send_auto_delete_msg(
                context.bot,
                update.effective_chat.id,
                "❌ **Too Large**: Thumbnail must be under 5MB.",
                parse_mode="Markdown",
            )
            return

        # 2. Get User (Create if missing)
        user = await get_user(user_id)
        if not user:
            from bot.database import create_user

            username = update.effective_user.username or "Unknown"
            first_name = update.effective_user.first_name or "User"
            user = await create_user(user_id, first_name, username)
            logger.info(f"🆕 User {user_id} created during thumbnail setup")

        # 3. Backup to Storage Channel (Availability Check)
        from bot.database import get_storage_channel, get_dump_channel

        storage_config = await get_storage_channel()
        dump_config = await get_dump_channel()

        storage_channel = (storage_config.get("id") if storage_config else None) or (
            dump_config.get("id") if dump_config else None
        )

        final_file_id = photo.file_id

        if storage_channel:
            try:
                # Send to storage to make it persistent
                backup_msg = await context.bot.send_photo(
                    chat_id=storage_channel,
                    photo=photo.file_id,
                    caption=f"🖼️ #Thumbnail\n👤 User: `{user_id}`\n📅 {datetime.now()}",
                    parse_mode="Markdown",
                )
                final_file_id = backup_msg.photo[-1].file_id
                logger.info(f"✅ Thumbnail backed up to channel {storage_channel}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to backup thumbnail: {e}")
                # Fallback to original ID (might expire strictly speaking, but usually works for bots)

        # 4. Save to DB
        await update_user(
            user_id,
            {
                "settings.thumbnail_file_id": final_file_id,
                "settings.thumbnail": "custom",
            },
        )

        context.user_data.pop("awaiting", None)

        await log_user_update(context.bot, user_id, "set custom thumbnail")
        logger.info(f"✅ User {user_id} set custom thumbnail: {final_file_id}")

        # Delete user's photo message to keep chat clean
        try:
            await update.message.delete()
        except:
            pass

        # Return to settings menu in-place
        from bot.handlers import ussettings_command

        await ussettings_command(update, context, photo_file_id=final_file_id)

    except Exception as e:
        logger.error(f"❌ Thumbnail upload error: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ **Upload Failed**\n\nCouldn't save thumbnail. Please try again.",
            parse_mode="Markdown",
        )


async def handle_us_metadata(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show metadata settings sub-menu"""
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        user = await get_user(user_id)
        if not user:
            await query.answer("❌ User not found", show_alert=True)
            return

        settings_data = user.get("settings", {})
        metadata = settings_data.get("metadata", {})

        m_title = metadata.get("video", "Default")
        m_author = metadata.get("author", "Default")
        m_audio = metadata.get("audio", "Default")
        m_subs = metadata.get("subs", "Default")

        text = (
            "🏷️ **Media Metadata Settings**\n\n"
            "Customize the global tags applied to your processed files.\n"
            "Track titles are applied as: `[Value] | [Language]`\n\n"
            f"🎬 **Video Title:** `{m_title}`\n"
            f"👤 **Artist/Author:** `{m_author}`\n"
            f"🎵 **Audio Track:** `{m_audio}`\n"
            f"📝 **Subtitle Track:** `{m_subs}`"
        )

        keyboard = [
            [
                InlineKeyboardButton("🎬 Video", callback_data="meta_video"),
                InlineKeyboardButton("� Author", callback_data="meta_author"),
            ],
            [
                InlineKeyboardButton("🎵 Audio", callback_data="meta_audio"),
                InlineKeyboardButton("📝 Subs", callback_data="meta_subs"),
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back to Settings", callback_data="go_back_to_settings"
                )
            ],
        ]

        await query.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )

        logger.info(f"✅ Metadata menu shown to user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_us_metadata: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_us_myfiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        query = update.callback_query
        await query.answer()

        files = await get_user_files(user_id)

        if not files:
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="us_back")]]
            await query.message.edit_text(
                "📂 **My Files**\n\n"
                "No files yet.\n\n"
                "Upload some files to see them here!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            return

        files_text = "📂 **My Files**\n\n"
        for i, file in enumerate(files[:10], 1):
            filename = file.get("name", "Unknown")[:30]
            size = file.get("size", 0)
            size_mb = size / (1024 * 1024)
            status = file.get("status", "unknown")
            files_text += f"{i}. `{filename}`\n"
            files_text += f"   • Size: {size_mb:.2f} MB | Status: {status}\n"

        if len(files) > 10:
            files_text += f"\n... and {len(files) - 10} more files"

        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="us_back")]]
        await query.message.edit_text(
            files_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

        logger.info(f"✅ Files shown to user {user_id}: {len(files)} files")

    except Exception as e:
        logger.error(f"❌ Error in handle_us_myfiles: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


class WizardHandler:
    @staticmethod
    async def start_wizard(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        file_path: Optional[str],
        file_id: Optional[str],
        file_name: str,
        file_size: int,
        task_id: str,
    ):
        user_id = update.effective_user.id

        from bot.database import get_user

        user = await get_user(user_id)
        user_settings = user.get("settings", {}) if user else {}
        global_metadata = user_settings.get("metadata", {})

        # Initialize Session
        context.user_data["wizard"] = {
            "task_id": task_id,
            "file_path": file_path,  # Could be None if deferred
            "file_id": file_id,  # Store file_id to download later
            "original_name": file_name,
            "file_size": file_size,
            "selected_audio": {},  # {index: True/False}
            "selected_subs": {},
            "injected_audio": [],
            "injected_subs": [],
            "rename": None,
            "custom_metadata": global_metadata.copy(),
            "tracks_probed": False,
        }

        size_mb = file_size / (1024 * 1024)

        text = (
            f"📁 **File Received**\n\n"
            f"📝 Name: `{file_name}`\n"
            f"💾 Size: `{size_mb:.1f} MB`\n\n"
            f"Select an action:"
        )

        keyboard = [
            [InlineKeyboardButton("✏️ Edit File", callback_data="wiz_edit")],
            [InlineKeyboardButton("🚀 Proceed", callback_data="wiz_process_now")],
        ]

        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )

    @staticmethod
    async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            query = update.callback_query
            data = query.data
            user_id = query.from_user.id

            if "wizard" not in context.user_data:
                await query.answer(
                    "⚠️ Session expired. Please re-upload.", show_alert=True
                )
                return

            session = context.user_data["wizard"]

            # ============================================================
            # MENU: EDIT SELECTION
            # ============================================================

            if data == "wiz_edit":
                await query.answer()

                try:
                    # Probe if not done
                    if not session.get("tracks_probed"):
                        if not session.get("file_path"):
                            await query.edit_message_text(
                                "📥 **Downloading for inspection...**"
                            )
                            import uuid
                            from pathlib import Path
                            from config.constants import DOWNLOADS_DIR

                            ext = (
                                Path(session.get("original_name", "file")).suffix
                                or ".tmp"
                            )
                            internal_filename = f"{uuid.uuid4()}{ext}"
                            user_dl_dir = DOWNLOADS_DIR / str(user_id)
                            user_dl_dir.mkdir(parents=True, exist_ok=True)
                            internal_path = user_dl_dir / internal_filename

                            file_id = session.get("file_id")
                            if not file_id:
                                await query.edit_message_text(
                                    "❌ Missing file reference."
                                )
                                return

                            file_obj = await context.bot.get_file(file_id)
                            file_path = await file_obj.download_to_drive(
                                custom_path=internal_path
                            )
                            session["file_path"] = str(file_path)

                        await query.edit_message_text("🔄 **Analyzing File Tracks...**")
                        probe_data = await FFmpegService.probe_file(
                            session["file_path"]
                        )
                        session["audio_tracks"] = probe_data["audio"]  # list of dicts
                        session["sub_tracks"] = probe_data["subtitle"]
                        session["tracks_probed"] = True

                        # Default Select All Original
                        for t in session["audio_tracks"]:
                            session["selected_audio"][t["index"]] = True
                        for t in session["sub_tracks"]:
                            session["selected_subs"][t["index"]] = True
                except Exception as probe_error:
                    logger.error(f"Probe failed: {probe_error}")
                    await query.edit_message_text(
                        "❌ **Analysis Failed**\n\nIs the file corrupted?"
                    )
                    return

                keyboard = [
                    [
                        InlineKeyboardButton(
                            "🎵 Audio Tracks", callback_data="wiz_menu_audio"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📝 Subtitles", callback_data="wiz_menu_subs"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "✏️ Rename", callback_data="wiz_rename_prompt"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🚀 Proceed", callback_data="wiz_process_now"
                        )
                    ],
                    [InlineKeyboardButton("❌ Cancel", callback_data="wiz_cancel")],
                ]

                await query.edit_message_text(
                    "🎛️ **Editor Menu**\n\nSelect what you want to change:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )

            # ============================================================
            # AUDIO MENU
            # ============================================================

            elif data == "wiz_menu_audio":
                await WizardHandler.render_track_menu(query, session, "audio")

            elif data.startswith("wiz_toggle_audio_"):
                idx = int(data.split("_")[-1])
                # Toggle
                current = session["selected_audio"].get(idx, False)
                session["selected_audio"][idx] = not current
                await WizardHandler.render_track_menu(query, session, "audio")

            elif data == "wiz_inject_audio_prompt":
                await query.edit_message_text(
                    "🎵 **Inject Audio**\n\n"
                    "Send me the audio file you want to add.\n\n"
                    "Supported: mp3, m4a, aac, wav",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Back", callback_data="wiz_menu_audio"
                                )
                            ]
                        ]
                    ),
                )
                context.user_data["awaiting"] = "wiz_inject_audio"

            # ============================================================
            # SUBTITLE MENU
            # ============================================================

            elif data == "wiz_menu_subs":
                await WizardHandler.render_track_menu(query, session, "subs")

            elif data.startswith("wiz_toggle_subs_"):
                idx = int(data.split("_")[-1])
                # Toggle
                current = session["selected_subs"].get(idx, False)
                session["selected_subs"][idx] = not current
                await WizardHandler.render_track_menu(query, session, "subs")

            elif data == "wiz_inject_subs_prompt":
                await query.edit_message_text(
                    "📝 **Inject Subtitle**\n\n"
                    "Send me the subtitle file (.srt) you want to add.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Back", callback_data="wiz_menu_subs"
                                )
                            ]
                        ]
                    ),
                )
                context.user_data["awaiting"] = "wiz_inject_subs"

            # ============================================================
            # RENAME
            # ============================================================

            elif data == "wiz_rename_prompt":
                current = session.get("rename") or session["original_name"]
                await query.edit_message_text(
                    f"✏️ **Rename File**\n\n"
                    f"Current: `{current}`\n\n"
                    f"Send me the new filename.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔙 Back", callback_data="wiz_edit")]]
                    ),
                )
                context.user_data["awaiting"] = "wiz_rename"

            # ============================================================
            # METADATA
            # ============================================================

            elif data == "wiz_menu_metadata":
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "🎬 Title", callback_data="wiz_meta_prompt_title"
                        ),
                        InlineKeyboardButton(
                            "👤 Artist", callback_data="wiz_meta_prompt_artist"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "📅 Year", callback_data="wiz_meta_prompt_date"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🔙 Back to Editor", callback_data="wiz_edit"
                        )
                    ],
                ]
                await query.edit_message_text(
                    "📝 **Metadata Editor**\n\nCustomize how your file appears in media players:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )

            elif data.startswith("wiz_meta_prompt_"):
                tag = data.replace("wiz_meta_prompt_", "")
                current = session.get("custom_metadata", {}).get(tag, "Not Set")
                await query.edit_message_text(
                    f"📝 **Edit {tag.title()}**\n\nCurrent: `{current}`\n\nSend me the new text you want to use for this tag.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Back", callback_data="wiz_menu_metadata"
                                )
                            ]
                        ]
                    ),
                    parse_mode="Markdown",
                )
                context.user_data["awaiting"] = f"wiz_meta_{tag}"

            # ============================================================
            # PROCESS
            # ============================================================

            elif data == "wiz_process_now":
                if context.user_data.get("processing_lock"):
                    await query.answer("⚠️ Processing already started!", show_alert=True)
                    return

                context.user_data["processing_lock"] = True
                try:
                    await query.answer("🚀 Starting Processing...")
                    await WizardHandler.execute_processing_flow(
                        update, context, session
                    )
                finally:
                    context.user_data.pop("processing_lock", None)

            elif data == "wiz_cancel":
                if context.user_data.get("processing_lock"):
                    await query.answer(
                        "⚠️ Cannot cancel while processing is initializing.",
                        show_alert=True,
                    )
                    return
                await query.answer("Cancelled")
                await query.message.delete()
                context.user_data.pop("wizard", None)

        except Exception as e:
            logger.error(f"Wizard Handler Error: {e}", exc_info=True)
            try:
                await update.callback_query.answer("❌ Error occurred", show_alert=True)
            except:
                pass

    @staticmethod
    async def render_track_menu(query, session, track_type):
        if track_type == "audio":
            tracks = session.get("audio_tracks", [])
            selected = session["selected_audio"]
            toggle_prefix = "wiz_toggle_audio_"
            title = "🎵 Audio Tracks"
            inject_btn = InlineKeyboardButton(
                "➕ Inject Audio", callback_data="wiz_inject_audio_prompt"
            )
        else:
            tracks = session.get("sub_tracks", [])
            selected = session["selected_subs"]
            toggle_prefix = "wiz_toggle_subs_"
            title = "📝 Subtitle Tracks"
            inject_btn = InlineKeyboardButton(
                "➕ Inject Subtitle", callback_data="wiz_inject_subs_prompt"
            )

        keyboard = []

        if not tracks:
            keyboard.append(
                [InlineKeyboardButton("❌ No Tracks Found", callback_data="ignore")]
            )
        else:
            for t in tracks:
                idx = t["index"]
                lang = t.get("language", "und")
                name = t.get("title", "Unknown")
                is_sel = selected.get(idx, False)

                icon = "✅" if is_sel else "❌"

                # cleaner display: "✅ Tamil | Surround 5.1" instead of "✅ tam | tam"
                from bot.services import FFmpegService

                lang_display = FFmpegService.get_language_name(lang)

                if name == lang_display or name.lower() == "unknown":
                    display_text = lang_display
                else:
                    display_text = f"{lang_display} | {name}"

                text = f"{icon} {display_text}"
                keyboard.append(
                    [InlineKeyboardButton(text, callback_data=f"{toggle_prefix}{idx}")]
                )

        keyboard.append([inject_btn])
        keyboard.append(
            [InlineKeyboardButton("🔙 Done / Back", callback_data="wiz_edit")]
        )

        try:
            await query.edit_message_text(
                f"**{title}**\n\nSelect tracks to keep:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
        except:
            pass  # Ignore "same message" error

    @staticmethod
    async def process_session_background(
        bot, user_id: int, session: Dict[str, Any], query=None
    ):
        """Core processing logic decoupled from UI callbacks, runnable by QueueWorker."""
        task_id = session.get("task_id")
        logger.info(
            f"process_session_background: STARTING task_id={task_id}, user_id={user_id}"
        )

        try:
            # 0. Post early ledger update
            from bot.services import create_or_update_storage_message

            custom_name = session.get("custom_name", "Untitled")
            logger.info(
                f"process_session_background: Posting storage message for {task_id}"
            )
            ledger_msg_id = await create_or_update_storage_message(
                bot,
                {
                    "filename": custom_name,
                    "status": "🏗️ Processing (Wizard)...",
                    "size": session.get("file_size", 0),
                },
                user_id=user_id,
            )
            logger.info(
                f"process_session_background: Storage message done, ledger_id={ledger_msg_id}"
            )

            # --- PROGRESS BAR FIX ---
            # Register message ID and send initial progress (0%) immediately
            if not hasattr(bot, "progress_data"):
                bot.progress_data = {}
            task_info = bot.progress_data.setdefault(task_id, {})
            task_info["user_id"] = user_id

            if query:
                try:
                    await query.answer()
                except:
                    pass
                task_info["user_progress_msg_id"] = query.message.message_id
            else:
                # If no query (automatic background process), send a new status message
                initial_msg = await bot.send_message(
                    user_id, "🚀 **Initializing Processing...**", parse_mode="Markdown"
                )
                task_info["user_progress_msg_id"] = initial_msg.message_id

            # Show early progress bar (0% / Initializing)
            from bot.handlers.user import send_progress_message

            await send_progress_message(
                bot,
                user_id,
                task_id,
                filesize=session.get("file_size", 0),
                stage="🚀 Initializing...",
                progress=0,
            )

            # --- JIT DOWNLOAD IF DEFERRED ---
            if not session.get("file_path"):
                await send_progress_message(
                    bot,
                    user_id,
                    task_id,
                    filesize=session.get("file_size", 0),
                    stage="📥 Downloading file...",
                    progress=10,
                )
                import uuid
                from pathlib import Path
                from config.constants import DOWNLOADS_DIR

                ext = Path(session.get("original_name", "file")).suffix or ".tmp"
                internal_filename = f"{uuid.uuid4()}{ext}"
                user_dl_dir = DOWNLOADS_DIR / str(user_id)
                user_dl_dir.mkdir(parents=True, exist_ok=True)
                internal_path = user_dl_dir / internal_filename

                file_id = session.get("file_id")
                if not file_id:
                    raise RuntimeError("Missing file reference. Task failed.")

                file_obj = await bot.get_file(file_id)
                file_path = await file_obj.download_to_drive(custom_path=internal_path)
                session["file_path"] = str(file_path)

            # 2. Get Selections
            audio_indices = [
                idx
                for idx, selected in session.get("selected_audio", {}).items()
                if selected
            ]
            sub_indices = [
                idx
                for idx, selected in session.get("selected_subs", {}).items()
                if selected
            ]

            # 3. Handle Renaming / Output Path
            from pathlib import Path

            input_path = session["file_path"]
            original_ext = Path(input_path).suffix

            custom_name = session.get("rename") or session.get("original_name")
            if not custom_name.endswith(original_ext):
                custom_name += original_ext

            from config.constants import DOWNLOADS_DIR

            output_filename = f"processed_{Path(input_path).name}"
            output_path = str(Path(input_path).parent / output_filename)

            # 4. Process Media (FFmpeg)
            from bot.services import FFmpegService

            msg_text = "🎬 **Processing Media...**\n\nThis may take a few minutes depending on size."

            logger.info(
                f"process_session_background: {task_id} - Sending processing message"
            )

            # ALWAYS edit the message we registered, never send a new one if we have a msg_id
            target_msg_id = task_info.get("user_progress_msg_id")
            if target_msg_id:
                try:
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=target_msg_id,
                        text=msg_text,
                        parse_mode="Markdown",
                    )
                except:
                    await bot.send_message(user_id, msg_text, parse_mode="Markdown")
            else:
                await bot.send_message(user_id, msg_text, parse_mode="Markdown")

            logger.info(
                f"process_session_background: {task_id} - Calling FFmpegService.process_media"
            )

            # Progress Tracking Setup
            try:
                probe_data = await FFmpegService.probe_file(input_path)
                duration = probe_data.get("duration", 0.0)
            except:
                duration = 0.0

            file_size = session.get("file_size", 0)
            import re, time, asyncio
            from bot.handlers.user import send_progress_message

            time_regex = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
            last_update = [time.time()]
            start_time = time.time()

            def ffmpeg_progress(line):
                match = time_regex.search(line)
                if match and duration > 0:
                    h, m, s = map(float, match.groups())
                    current_sec = h * 3600 + m * 60 + s
                    progress = min(int((current_sec / duration) * 100), 100)

                    now = time.time()
                    if now - last_update[0] > 5 or progress >= 100:
                        last_update[0] = now
                        asyncio.create_task(
                            send_progress_message(
                                bot=bot,
                                user_id=user_id,
                                task_id=task_id,
                                filesize=file_size,
                                stage="🎬 **Processing Media (FFmpeg)...**",
                                progress=progress,
                                start_time=start_time,
                            )
                        )

            success = await FFmpegService.process_media(
                input_path=input_path,
                output_path=output_path,
                selected_audio_indexes=audio_indices,
                selected_sub_indexes=sub_indices,
                injected_audio=session.get("injected_audio"),
                injected_subs=session.get("injected_subs"),
                new_filename=custom_name,
                custom_metadata=session.get("custom_metadata"),
                progress_callback=ffmpeg_progress,
                all_audio_tracks=session.get("audio_tracks"),
                all_sub_tracks=session.get("sub_tracks"),
            )

            logger.info(
                f"process_session_background: {task_id} - FFmpeg completed, success={success}"
            )

            if not success:
                raise RuntimeError("Media processing failed.")

            # 5. Upload and Send
            logger.info(f"process_session_background: {task_id} - Uploading file")
            if query:
                await query.edit_message_text("📤 **Uploading File...**")
            else:
                await bot.send_message(user_id, "📤 **Uploading File...**")

            from bot.database import get_user, get_config
            from bot.services import upload_and_send_file

            user = await get_user(user_id)
            user_plan = user.get("plan", "free")
            visibility = user.get("settings", {}).get("visibility", "public")

            upload_result = await upload_and_send_file(
                bot=bot,
                user_id=user_id,
                file_path=output_path,
                user_plan=user_plan,
                custom_filename=custom_name,
                visibility=visibility,
                task_id=task_id,
            )

            if not upload_result:
                raise RuntimeError("Upload failed.")

            dump_file_id = upload_result.get("file_id")

            # 5.5 Update card in storage channel
            await create_or_update_storage_message(
                bot,
                {
                    "file_id": dump_file_id,
                    "filename": custom_name,
                    "static_size": session.get("file_size", 0),
                    "status": "✅ Completed",
                },
                user_id=user_id,
                message_id=ledger_msg_id,
            )

            # Store metadata for destination forward
            if dump_file_id:
                context.user_data[f"fwd_meta_{dump_file_id[-10:]}"] = {
                    "filename": custom_name,
                    "size": session.get("file_size", 0),
                }

            # 6. Generate Stream Link with Shortener Token logic if needed
            from bot.database import create_one_time_key
            import secrets
            from datetime import datetime, timedelta

            secure_token = secrets.token_urlsafe(32)
            expires = datetime.utcnow() + timedelta(hours=24)
            await create_one_time_key(user_id, secure_token, expires, purpose="stream")

            stream_url = (
                f"https://{settings.DOMAIN}/api/verify_link/{dump_file_id}/{secure_token}"
                if dump_file_id
                else None
            )

            final_text = f"✅ **Processing Complete!**\n\n📄 File: `{custom_name}`\n"
            keyboard = []
            if stream_url:
                final_text += f"🔗 Stream Link: {stream_url}\n\n⚠️ **Note**: If the file stream is not playable, please use an external player like MX Player or VLC."
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "📺 VLC Player", url=f"vlc://{stream_url}"
                        ),
                        InlineKeyboardButton(
                            "📱 MX Player",
                            url=f"intent:{stream_url}#Intent;package=com.mxtech.videoplayer.ad;S.title={custom_name};end",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "📤 Send to Destination",
                            callback_data=f"send_dest_{dump_file_id}",
                        )
                    ],
                ]

            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

            if query:
                # Clear state using helper before finishing UI
                from bot.handlers.user import clear_user_session

                await clear_user_session(
                    None, context
                )  # Pass update=None as we have context

                await query.edit_message_text(
                    final_text, parse_mode="Markdown", reply_markup=reply_markup
                )
                # Silently delete the wizard panel after a short grace period
                import asyncio

                async def _cleanup_msg():
                    await asyncio.sleep(3)
                    try:
                        await query.message.delete()
                    except Exception:
                        pass

                asyncio.create_task(_cleanup_msg())
            else:
                await bot.send_message(
                    user_id,
                    text=final_text,
                    parse_mode="Markdown",
                    reply_markup=reply_markup,
                )

            # 7. Cleanup
            Path(input_path).unlink(missing_ok=True)
            Path(output_path).unlink(missing_ok=True)

            logger.info(f"✅ Wizard flow complete for user {user_id}: {custom_name}")

        except Exception as e:
            logger.error(f"❌ Error in wizard processing: {e}", exc_info=True)
            err_msg = f"❌ **Processing Failed**\n\nError: {str(e)[:100]}"
            if query:
                try:
                    await query.edit_message_text(err_msg, parse_mode="Markdown")
                except Exception:
                    try:
                        await bot.send_message(
                            user_id, text=err_msg, parse_mode="Markdown"
                        )
                    except Exception:
                        pass
            else:
                try:
                    await bot.send_message(user_id, text=err_msg, parse_mode="Markdown")
                except Exception:
                    pass

    @staticmethod
    async def execute_processing_flow(
        update: Update, context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]
    ):
        """
        Triggered when user hits 'Proceed' in Wizard.
        Places user in Queue if Free and Queue full, otherwise processes immediately.
        """
        user_id = update.effective_user.id
        task_id = session.get("task_id")
        query = update.callback_query

        try:
            from bot.database import get_user, update_task, get_user_position
            from bot.services._queue_worker import QueueWorker
            from bot.services._ffmpeg import FFmpegService
            from config.settings import get_settings
            import uuid

            logger.info(
                f"execute_processing_flow: task_id={task_id}, user_id={user_id}"
            )

            user = await get_user(user_id)
            if not user:
                await query.answer("❌ User not found", show_alert=True)
                return

            plan_name = user.get("plan", "free").lower()

            from bot.database import get_config, get_active_task_count

            plans_config = await get_config("plans") or {}
            plan_limit = plans_config.get(plan_name, {}).get("parallel", 1)

            user_active_count = await get_active_task_count(user_id)

            db_position = await get_user_position(user_id)

            is_at_plan_limit = user_active_count >= plan_limit
            global_busy = db_position > 0 or FFmpegService._get_semaphore().locked()

            logger.info(
                f"execute_processing_flow: plan={plan_name}, at_limit={is_at_plan_limit}, busy={global_busy}, pos={db_position}"
            )

            if is_at_plan_limit or (global_busy and plan_name != "pro"):
                position = db_position + 1
                verify_token = f"bypass_{uuid.uuid4().hex[:8]}"
                await update_task(
                    task_id,
                    {
                        "status": "queued",
                        "session": session,
                        "wizard_bypass_token": verify_token,
                    },
                )

                logger.info(
                    f"execute_processing_flow: task {task_id} QUEUED at position {position}"
                )

                bot_username = context.bot.username
                bypass_url = f"https://{settings.DOMAIN}/queue-bypassed?token={verify_token}&bot={bot_username}"
                context.user_data["bypass_url"] = bypass_url

                bot_api_url = user.get("settings", {}).get("shorten_api_url")
                bot_api_key = user.get("settings", {}).get("shorten_api_key")

                keyboard = [
                    [
                        InlineKeyboardButton(
                            "Bypass the queue", callback_data=f"bypass_q_{task_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Refresh (to know the queue no)",
                            callback_data=f"refresh_q_{task_id}",
                        )
                    ],
                ]

                await query.edit_message_text(
                    f"The bot is processing many file now your position `{position}`,\n"
                    f"you'll get notification when your turn come",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )

                context.user_data["queued_task"] = task_id
                context.user_data["bypass_url"] = bypass_url
                context.user_data.pop("wizard", None)
                return

            await update_task(task_id, {"status": "processing"})

            import asyncio

            asyncio.create_task(
                WizardHandler.process_session_background(
                    context.bot, user_id, session, query
                )
            )

        except Exception as e:
            logger.error(f"❌ execute_processing_flow error: {e}", exc_info=True)
            try:
                await query.edit_message_text(
                    f"❌ **Processing Failed**\n\nError: `{str(e)[:150]}`",
                    parse_mode="Markdown",
                )
            except Exception:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"❌ **Processing Failed**\n\nError: `{str(e)[:150]}`",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass


async def execute_processing_flow_by_task(bot, task: dict) -> None:
    """
    Entry point called by QueueWorker when it picks up a queued file task.

    Design note:
    - Direct file uploads go through WizardHandler immediately in handle_file_upload()
      and never reach the QueueWorker under the current flow.
    - This function handles tasks that were queued via create_task() from URL downloads
      or any future flow that decouples intake from processing.

    Args:
        bot: The running telegram.Bot instance
        task: MongoDB task document with at minimum:
              {task_id, user_id, file_id|url, task_type, ...}
    """
    task_id = task.get("task_id", "unknown")
    user_id = task.get("user_id")

    try:
        logger.info(
            f"▶️ execute_processing_flow_by_task: task_id={task_id} user_id={user_id}"
        )

        # Get user settings for processing preferences
        from bot.database import (
            get_user as _get_user,
            get_config as _get_config,
            update_task as _update_task,
        )

        user = await _get_user(user_id)
        config = await _get_config()

        if not user:
            logger.error(f"❌ Task {task_id}: user {user_id} not found in DB")
            await _update_task(task_id, {"status": "failed", "error": "User not found"})
            return

        # Determine what to process
        task_url = task.get("url") or task.get("metadata", {}).get("url")
        task_file_id = task.get("file_id")

        if task_url:
            # URL download task — use downloader service
            from bot.services import download_from_url

            db = get_db()
            download_result = await download_from_url(
                url=task_url,
                user_id=user_id,
                task_id=task_id,
                db=db,
            )
            if not download_result or not download_result.get("file_path"):
                raise RuntimeError("Download failed — no file_path returned")

            file_path = download_result["file_path"]
            display_name = download_result.get("filename", "download")

        elif task_file_id:
            # Telegram file — download to disk first
            import uuid
            from pathlib import Path as _Path
            from config.constants import DOWNLOADS_DIR

            tg_file = await bot.get_file(task_file_id)
            ext = _Path(task.get("filename", "file")).suffix or ".tmp"
            dest = DOWNLOADS_DIR / str(user_id) / f"{uuid.uuid4()}{ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            await (await tg_file.get_file()).download_to_drive(custom_path=dest)
            file_path = str(dest)
            display_name = task.get("filename", "file")

        else:
            raise RuntimeError("Task has neither url nor file_id — cannot process")

        # Post early card to storage channel (Ledger)
        ledger_msg_id = await create_or_update_storage_message(
            bot,
            {
                "filename": display_name,
                "status": "🏗️ Processing...",
                "size": task.get("file_size", 0),
            },
            user_id=user_id,
        )

        # Upload + send using shared service
        from bot.services import upload_and_send_file

        user_plan = user.get("plan", "free")
        visibility = user.get("settings", {}).get("visibility", "public")

        result = await upload_and_send_file(
            bot=bot,
            user_id=user_id,
            file_path=file_path,
            user_plan=user_plan,
            custom_filename=display_name,
            visibility=visibility,
            task_id=task.get("id") or task_id,
        )

        if not result:
            raise RuntimeError("upload_and_send_file returned None")

        # Generate stream link
        # Generate stream link with Secure Token
        from bot.database import create_one_time_key
        import secrets
        from datetime import datetime, timedelta

        dump_file_id = result.get("file_id", task_file_id or "")
        stream_url = ""

        if dump_file_id:
            secure_token = secrets.token_urlsafe(32)
            expires = datetime.utcnow() + timedelta(hours=24)
            await create_one_time_key(user_id, secure_token, expires, purpose="stream")
            stream_url = f"https://{settings.DOMAIN}/api/verify_link/{dump_file_id}/{secure_token}"

        # Update card in storage channel
        await create_or_update_storage_message(
            bot,
            {
                "file_id": dump_file_id,
                "filename": display_name,
                "static_size": result.get("file_size", 0),
                "status": "✅ Completed",
            },
            user_id=user_id,
            message_id=ledger_msg_id,
        )

        # Notify user
        try:
            notification_text = (
                f"✅ **File Ready!**\n\n"
                f"📄 File: `{display_name}`\n"
                + (f"🔗 Stream: {stream_url}\n\n" if stream_url else "\n")
                + f"🆔 Task: `{task_id}`\n\n"
                + f"⚠️ **Note**: If the file stream is not playable, please use an external player like MX Player, PlayIt, or VLC. Alternatively, use the Download button. Download speed restrictions remain unchanged."
            )

            await bot.send_message(
                chat_id=user_id,
                text=notification_text,
                parse_mode="Markdown",
            )
        except Exception as notify_err:
            logger.warning(f"Could not notify user {user_id}: {notify_err}")

        # Cleanup temp file if it came from download
        try:
            from pathlib import Path as _Path2

            _Path2(file_path).unlink(missing_ok=True)
        except Exception:
            pass

        logger.info(f"✅ execute_processing_flow_by_task done: {task_id}")

    except Exception as e:
        logger.error(
            f"❌ execute_processing_flow_by_task failed for {task_id}: {e}",
            exc_info=True,
        )
        raise
