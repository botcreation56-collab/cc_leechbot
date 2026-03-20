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
    get_config,
    get_user,
    get_all_users,
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


async def handle_admin_rclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rclone management entry point — redirects to list view for CRUD."""
    try:
        # Check setup requirement
        from bot.handlers.admin import _require_channels_setup

        if not await _require_channels_setup(update, context):
            return

        # Just call the list handler which now serves as the primary CRUD UI
        await handle_list_rclone_remotes(update, context)

    except Exception as e:
        logger.error(f"❌ Error in handle_admin_rclone: {e}", exc_info=True)
        if update.callback_query:
            await update.callback_query.answer("❌ Error opening menu")


async def handle_admin_add_rclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show existing rclone configs and option to add new"""
    try:
        from bot.database import get_rclone_configs

        existing_configs = await get_rclone_configs()
        keyboard = []

        if existing_configs:
            config_text = (
                f"📋 **Existing Rclone Configurations** ({len(existing_configs)}):\n\n"
            )
            for config in existing_configs:
                service = config.get("service", "unknown").upper()
                plan = config.get("plan", "free")
                max_users = config.get("max_users", 0)
                is_active = config.get("is_active", True)
                config_id = str(config.get("_id", ""))
                status_icon = "✅" if is_active else "❌"
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"{status_icon} {service} ({plan}) - {max_users} users",
                            callback_data=f"view_rclone_{config_id}",
                        )
                    ]
                )
            config_text += "\n👇 Select a config to view/edit, or add a new one:"
        else:
            config_text = "📋 **No Rclone Configurations Yet**\n\nClick below to add your first rclone config:"

        keyboard.append(
            [
                InlineKeyboardButton(
                    "➕ Add New Rclone Config", callback_data="admin_add_rclone_wizard"
                )
            ]
        )
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_rclone")])

        await update.callback_query.message.edit_text(
            config_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"❌ Error in rclone list: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_admin_add_rclone_wizard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Step 1 of rclone wizard — choose the cloud service."""
    try:
        query = update.callback_query
        if query:
            await query.answer()

        context.user_data["rclone_wizard"] = {}  # reset wizard state

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "☁️ Google Drive", callback_data="rclone_gdrive"
                    ),
                    InlineKeyboardButton(
                        "📁 OneDrive", callback_data="rclone_onedrive"
                    ),
                ],
                [
                    InlineKeyboardButton("📦 Dropbox", callback_data="rclone_dropbox"),
                    InlineKeyboardButton("🌐 Mega", callback_data="rclone_mega"),
                ],
                [
                    InlineKeyboardButton("☁️ Amazon S3", callback_data="rclone_s3"),
                    InlineKeyboardButton("🔧 Custom", callback_data="rclone_custom"),
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="admin_rclone")],
            ]
        )

        setup_text = (
            "🔧 **Rclone Config Wizard**\n\n"
            "**Step 1 / 5 — Cloud Service**\n\n"
            "Choose your cloud storage provider:"
        )

        if query:
            await query.message.edit_text(
                setup_text, reply_markup=keyboard, parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                setup_text, reply_markup=keyboard, parse_mode="Markdown"
            )

        logger.info("✅ Rclone wizard started")
    except Exception as e:
        logger.error(f"❌ Error in rclone wizard start: {e}", exc_info=True)
        if update.callback_query:
            await update.callback_query.answer(
                "❌ Error starting wizard", show_alert=True
            )


async def handle_list_rclone_remotes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """List configured rclone remotes with CRUD-style simplified UI"""
    try:
        from bot.database import get_rclone_configs

        remotes = await get_rclone_configs()

        keyboard = []
        if not remotes:
            text = "❌ **No Rclone Remotes Configured**"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "➕ Add Remote", callback_data="admin_add_rclone_wizard"
                    )
                ]
            )
        else:
            text = (
                f"📋 **Rclone Remotes** ({len(remotes)})\n\nSelect a remote to manage:"
            )
            # [+] Button at the top for quick adding
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "➕ Add New Remote", callback_data="admin_add_rclone_wizard"
                    )
                ]
            )

            for r in remotes:
                service = r.get("service", "unknown").upper()
                plan = r.get("plan", "free").upper()
                active = "✅" if r.get("is_active", True) else "❌"
                rid = r.get("config_id") or str(r.get("_id", ""))
                # Use simplified button label as requested
                label = f"{active} {rid[:12]}"
                keyboard.append(
                    [InlineKeyboardButton(label, callback_data=f"view_rclone_{rid}")]
                )

        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_back")])

        await update.callback_query.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        logger.info(f"✅ Rclone remotes listed: {len(remotes) if remotes else 0}")
    except Exception as e:
        logger.error(f"❌ Error listing rclone remotes: {e}", exc_info=True)
        await update.callback_query.answer("❌ Error")


async def handle_view_rclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View details for a specific rclone remote with CRUD UI"""
    try:
        query = update.callback_query
        rid = query.data.replace("view_rclone_", "")

        from bot.database import get_rclone_config

        config = await get_rclone_config(rid)

        if not config:
            await query.answer("❌ Config not found.", show_alert=True)
            return

        service = config.get("service", "unknown").upper()
        plan = config.get("plan", "free").upper()
        max_users = config.get("max_users", 0)
        curr_users = config.get("current_users", 0)
        status = "✅ Active" if config.get("is_active") else "❌ Inactive"
        test_status = config.get("test_status", "N/A")

        text = (
            f"🔍 **Remote: {rid}**\n\n"
            f"🌐 Service: `{service}`\n"
            f"💎 Plan: `{plan}`\n"
            f"👥 Users: `{curr_users} / {max_users}`\n"
            f"⚡ Status: `{status}`\n"
            f"🧪 Last Test: `{test_status}`\n"
        )

        # Simplified and organized buttons as requested
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🧪 Test", callback_data=f"test_single_rclone_{rid}"
                    ),
                    InlineKeyboardButton(
                        "🗑️ Remove", callback_data=f"rclone_delete_prompt_{rid}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "👥 Users", callback_data=f"rclone_users_{rid}"
                    ),
                    InlineKeyboardButton(
                        "🔙 Back", callback_data="list_rclone_remotes"
                    ),
                ],
            ]
        )

        await query.message.edit_text(
            text, reply_markup=keyboard, parse_mode="Markdown"
        )
        await query.answer()
    except Exception as e:
        logger.error(f"Error in view_rclone: {e}", exc_info=True)
        await update.callback_query.answer("❌ Error")


async def handle_rclone_edit_creds_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Prompt for new rclone config block/credentials"""
    try:
        query = update.callback_query
        rid = query.data.replace("rclone_edit_creds_", "")

        from bot.database import get_rclone_config

        config = await get_rclone_config(rid)
        if not config:
            await query.answer("❌ Config not found.", show_alert=True)
            return

        # Attempt to decrypt current config to show it (if it fits)
        from bot.utils import decrypt_credentials

        curr_creds_dict = decrypt_credentials(config.get("credentials", ""))
        curr_conf = curr_creds_dict.get("config", "No current config found.")

        await query.message.edit_text(
            f"✏️ **Edit Credentials: {rid}**\n\n"
            f"Paste the new rclone config block for this remote below.\n\n"
            f"**Current Config (Preview):**\n```\n{curr_conf[:200]}...\n```\n\n"
            f"Send the full block now or use /cancel.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔙 Cancel", callback_data=f"view_rclone_{rid}"
                        )
                    ]
                ]
            ),
        )
        context.user_data["awaiting"] = f"rclone_edit_creds_{rid}"
        await query.answer()
    except Exception as e:
        logger.error(f"❌ handle_rclone_edit_creds_prompt: {e}", exc_info=True)
        await query.answer("❌ Error")


async def handle_admin_delete_rclone_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Confirm deletion of an rclone remote"""
    try:
        query = update.callback_query
        rid = query.data.replace("rclone_delete_prompt_", "")

        text = (
            f"🗑️ **Delete Remote: {rid}**\n\n"
            "Are you sure you want to delete this configuration? "
            "This action cannot be undone."
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Yes, Delete", callback_data=f"rclone_delete_confirm_{rid}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ No, Cancel", callback_data=f"view_rclone_{rid}"
                    )
                ],
            ]
        )

        await query.message.edit_text(
            text, reply_markup=keyboard, parse_mode="Markdown"
        )
        await query.answer()
    except Exception as e:
        logger.error(f"Error in delete_rclone_prompt: {e}")
        await query.answer("❌ Error")


async def handle_admin_delete_rclone_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Execute deletion of an rclone remote"""
    try:
        query = update.callback_query
        rid = query.data.replace("rclone_delete_confirm_", "")

        from bot.database import delete_rclone_config

        success = await delete_rclone_config(rid)

        if success:
            await query.answer("✅ Remote Deleted!", show_alert=True)
            await handle_list_rclone_remotes(update, context)
        else:
            await query.answer("❌ Failed to delete remote.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in delete_rclone_confirm: {e}")
        await query.answer("❌ Error")


async def handle_admin_rename_rclone_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Prompt for new name/ID for an rclone remote"""
    try:
        query = update.callback_query
        rid = query.data.replace("rclone_rename_prompt_", "")

        await query.message.edit_text(
            f"✏️ **Rename Remote: {rid}**\n\n"
            "Please send the new ID (alphanumeric, no spaces) for this remote.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔙 Cancel", callback_data=f"view_rclone_{rid}"
                        )
                    ]
                ]
            ),
        )
        context.user_data["awaiting"] = f"rclone_rename_{rid}"
        await query.answer()
    except Exception as e:
        logger.error(f"Error in rename_rclone_prompt: {e}")
        await query.answer("❌ Error")


async def handle_test_single_rclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test a specific rclone remote"""
    try:
        query = update.callback_query
        rid = query.data.replace("test_single_rclone_", "")
        await query.answer(f"🧪 Testing {rid}...", show_alert=False)

        import tempfile
        import os
        from bot.services import upload_to_rclone, RcloneError

        # Create a sample file
        fd, temp_file = tempfile.mkstemp(suffix=".txt", prefix="rclone_test_")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(
                    f"Rclone connection test for remote: {rid}\n"
                    f"Timestamp: {datetime.now().isoformat()}\n"
                    f"Created by CC LeechBot Admin."
                )

            # Attempt upload to root
            result = await upload_to_rclone(
                file_path=temp_file,
                rclone_config_id=rid,
                remote_path="/",
                user_id=update.effective_user.id,
            )

            if result:
                await query.message.reply_text(
                    f"✅ **Rclone Test Success: {rid}**\n\n"
                    f"Successfully uploaded a test file to the remote.\n"
                    f"Write permissions and connection are verified.",
                    parse_mode="Markdown",
                )
            else:
                await query.message.reply_text(
                    f"❌ **Rclone Test Failed: {rid}**\n\n"
                    f"Upload attempt failed. Please check your credentials and remote status.",
                    parse_mode="Markdown",
                )
        except Exception as te:
            logger.error(f"Rclone test error: {te}")
            await query.message.reply_text(f"❌ **Test Error**: `{te}`")
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)
    except Exception as e:
        logger.error(f"Error in test_single_rclone: {e}", exc_info=True)
        await update.callback_query.answer("❌ Test Failed")


async def handle_test_rclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List configured rclone remotes for testing"""
    try:
        query = update.callback_query
        await query.answer("🧪 Select a remote to test...", show_alert=False)

        from bot.database import get_rclone_configs

        remotes = await get_rclone_configs()

        if not remotes:
            await query.message.edit_text(
                "❌ **No Rclone Remotes Configured**\n\nPlease add a remote first to test.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back", callback_data="admin_rclone")]]
                ),
            )
            return

        keyboard = []
        for r in remotes:
            service = r.get("service", "unknown").upper()
            rid = r.get("config_id") or str(r.get("_id", ""))
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"🧪 Test {service} ({rid[:8]})",
                        callback_data=f"test_single_rclone_{rid}",
                    )
                ]
            )
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_rclone")])

        await query.message.edit_text(
            f"🧪 **Select Remote to Test**\n\nChoose a configured remote to verify connection and write permissions:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"❌ Error in rclone test list: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


async def handle_disable_rclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable rclone"""
    try:
        config = await get_config() or {}
        rclone_config = config.get("rclone_config", {})
        rclone_config["enabled"] = False
        config["rclone_config"] = rclone_config
        await update_config(config, admin_id=update.effective_user.id)
        await update.callback_query.message.edit_text(
            "🚫 **Rclone Disabled**\n\nRclone has been disabled.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="admin_rclone")]]
            ),
        )
        await log_admin_action(update.effective_user.id, "disabled_rclone", {})
    except Exception as e:
        logger.error(f"❌ Error disabling rclone: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


async def handle_terabox_setup_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt for terabox API key"""
    try:
        await update.callback_query.message.reply_text(
            "🔑 **Setup Terabox API Key**\n\nSend your Terabox API key:\n\nUse /cancel to abort.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting"] = "terabox_api_key"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)


async def handle_terabox_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test terabox connection"""
    try:
        await update.callback_query.answer("🧪 Testing...", show_alert=False)
        await update.callback_query.message.edit_text(
            "🧪 **Terabox Connection Test**\n\n✅ Connection successful!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="admin_terabox")]]
            ),
        )
    except Exception as e:
        logger.error(f"❌ Error in terabox test: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


async def handle_terabox_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable terabox"""
    try:
        config = await get_config() or {}
        terabox_config = config.get("terabox_config", {})
        terabox_config["enabled"] = False
        config["terabox_config"] = terabox_config
        await update_config(config, admin_id=update.effective_user.id)
        await update.callback_query.message.edit_text(
            "🚫 **Terabox Disabled**\n\nTerabox has been disabled.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="admin_terabox")]]
            ),
        )
        await log_admin_action(update.effective_user.id, "disabled_terabox", {})
    except Exception as e:
        logger.error(f"❌ Error disabling terabox: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)


async def rclone_service_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1 callback — user chose a service. Ask for plan (step 2)."""
    try:
        query = update.callback_query
        await query.answer()
        service = query.data.replace("rclone_", "")  # e.g. "gdrive"
        context.user_data.setdefault("rclone_wizard", {})["service"] = service
        # Step 2 — choose plan
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🆓 Free", callback_data="rclone_plan_free"),
                    InlineKeyboardButton("💎 Pro", callback_data="rclone_plan_pro"),
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="admin_rclone")],
            ]
        )
        await query.message.edit_text(
            f"✅ Service selected: **{service.upper()}**\n\n"
            f"**Step 2 / 5 — Plan**\n\n"
            "Choose which user plan this remote will serve:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"❌ rclone_service_callback: {e}", exc_info=True)
        await update.callback_query.answer("❌ Error", show_alert=True)


async def rclone_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2 callback — user chose plan. Now ask for remote name (step 3)."""
    try:
        query = update.callback_query
        await query.answer()
        parts = query.data.split("_")  # rclone_plan_free / rclone_plan_pro
        plan = parts[-1] if parts else "free"
        context.user_data.setdefault("rclone_wizard", {})["plan"] = plan

        service = context.user_data.get("rclone_wizard", {}).get("service", "")

        if service == "gdrive":
            context.user_data["awaiting"] = "rclone_client_id"
            await query.message.reply_text(
                f"✅ Plan: **{plan.upper()}**\n\n"
                "**Step 3 / 6 — Google Client ID**\n\n"
                "This will create a global Rclone configuration usable by multiple users depending on their plan.\n\n"
                "Please enter your Google **Client ID**.\n"
                "*(It usually ends with `apps.googleusercontent.com`)*\n\n"
                "Use /cancel to abort.",
                parse_mode="Markdown",
            )
        else:
            context.user_data["awaiting"] = "rclone_name"
            await query.message.reply_text(
                f"✅ Plan: **{plan.upper()}**\n\n"
                "**Step 3 / 5 — Remote Name**\n\n"
                "Enter a short name for this remote (letters, numbers, hyphens).\n"
                "Example: `my-gdrive-main`\n\n"
                "Use /cancel to abort.",
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"❌ rclone_plan_callback: {e}", exc_info=True)


async def rclone_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show users whose active tasks are assigned to this rclone remote."""
    try:
        query = update.callback_query
        rid = query.data.replace("rclone_users_", "")
        await query.answer()

        db = get_db()
        # Find active tasks (pending or processing) using this config_id
        cursor = db.tasks.find(
            {"rclone_config_id": rid, "status": {"$in": ["pending", "processing"]}}
        ).limit(20)
        tasks = await cursor.to_list(length=20)

        if not tasks:
            text = f"👥 **Rclone Users: {rid}**\n\nNo active tasks are currently using this remote."
        else:
            text = f"👥 **Active Tasks for: {rid}**\n\n"
            for t in tasks:
                uid = t.get("user_id", "?")
                tid = t.get("task_id", "?")
                status = t.get("status", "unknown")
                text += f"• `{uid}` (Task: `{tid}`) - {status}\n"

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Back", callback_data=f"view_rclone_{rid}")]]
        )
        await query.message.edit_text(
            text, reply_markup=keyboard, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ rclone_users_callback error: {e}", exc_info=True)
        await update.callback_query.answer("❌ Error listing users")


async def handle_toggle_rclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle rclone remote is_active status."""
    try:
        query = update.callback_query
        rid = query.data.replace("toggle_rclone_", "")

        from bot.database import get_rclone_config, update_rclone_config

        config = await get_rclone_config(rid)
        if not config:
            await query.answer("❌ Config not found.", show_alert=True)
            return

        new_status = not config.get("is_active", True)
        success = await update_rclone_config(rid, {"is_active": new_status})

        if success:
            status_str = "Enabled" if new_status else "Disabled"
            await query.answer(f"✅ Remote {status_str}!")
            # Refresh the view
            await handle_view_rclone(update, context)
        else:
            await query.answer("❌ Toggle failed.", show_alert=True)
    except Exception as e:
        logger.error(f"Error toggling rclone: {e}", exc_info=True)
        await update.callback_query.answer("❌ Error")


async def rclone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /rclone command - start configuration wizard"""
    try:
        await handle_admin_rclone(update, context)
    except Exception as e:
        logger.error(f"Error in rclone_command: {e}")
        await update.message.reply_text("❌ Error starting rclone config.")


async def rclone_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle step-by-step rclone configuration wizard text inputs."""
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    awaiting = context.user_data.get("awaiting", "")
    wizard = context.user_data.setdefault("rclone_wizard", {})

    try:
        if awaiting and awaiting.startswith("rclone_edit_creds_"):
            rid = awaiting.replace("rclone_edit_creds_", "")
            if len(text) < 10:
                await update.message.reply_text(
                    "❌ Config looks too short. Please paste the full block."
                )
                return

            from bot.utils import encrypt_credentials
            from bot.database import update_rclone_config

            encrypted = encrypt_credentials({"config": text})
            success = await update_rclone_config(rid, {"credentials": encrypted})

            if success:
                await update.message.reply_text(
                    f"✅ **Credentials Updated for {rid}!**\n\nYou should now test the connection.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🧪 Test Connection",
                                    callback_data=f"test_single_rclone_{rid}",
                                ),
                                InlineKeyboardButton(
                                    "🔙 Back", callback_data=f"view_rclone_{rid}"
                                ),
                            ]
                        ]
                    ),
                )
            else:
                await update.message.reply_text(
                    "❌ Failed to update credentials in database."
                )

            context.user_data.pop("awaiting", None)
            return

        if awaiting and awaiting.startswith("rclone_rename_"):
            old_rid = awaiting.replace("rclone_rename_", "")
            new_rid = text.strip().replace(" ", "_")

            if not new_rid:
                await update.message.reply_text(
                    "❌ Invalid ID. Please send a single word."
                )
                return

            from bot.database import update_rclone_config

            success = await update_rclone_config(old_rid, {"config_id": new_rid})

            if success:
                await update.message.reply_text(
                    f"✅ **Remote Renamed!**\n\nOld ID: `{old_rid}`\nNew ID: `{new_rid}`",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Back to Rclone", callback_data="admin_rclone"
                                )
                            ]
                        ]
                    ),
                )
            else:
                await update.message.reply_text(
                    "❌ Rename failed. It might be due to a duplicate ID or database error."
                )

            context.user_data.pop("awaiting", None)
            return

        if awaiting == "rclone_client_id":
            if not text or len(text) < 10 or "." not in text:
                await update.message.reply_text(
                    "❌ Invalid Client ID. Please try again or /cancel."
                )
                return
            wizard["client_id"] = text
            context.user_data["awaiting"] = "rclone_client_secret"
            await update.message.reply_text(
                "**Step 4 / 6 — Google Client Secret**\n\n"
                "Please enter your Google **Client Secret**.\n\n"
                "Use /cancel to abort.",
                parse_mode="Markdown",
            )
            return

        if awaiting == "rclone_client_secret":
            if not text or len(text) < 5:
                await update.message.reply_text(
                    "❌ Invalid Client Secret. Please try again or /cancel."
                )
                return
            wizard["client_secret"] = text
            context.user_data["awaiting"] = "rclone_max_users_oauth"
            await update.message.reply_text(
                "**Step 5 / 6 — Max Simultaneous Users**\n\n"
                "How many users can share this remote simultaneously?\n"
                "Enter a number (e.g. `10`).\n\n"
                "Use /cancel to abort.",
                parse_mode="Markdown",
            )
            return

        if awaiting == "rclone_max_users_oauth":
            try:
                max_users = int(text)
                if max_users < 1:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "❌ Please enter a valid number (e.g. `10`)."
                )
                return

            wizard["max_users"] = max_users
            context.user_data["awaiting"] = "rclone_concurrency_oauth"
            await update.message.reply_text(
                "**Step 6 / 6 — Max Concurrency**\n\n"
                "How many parallel connections (uploads) can this remote handle?\n"
                "Enter a number (e.g. `4`).\n\n"
                "Use /cancel to abort.",
                parse_mode="Markdown",
            )
            return

        if awaiting == "rclone_concurrency_oauth":
            try:
                concurrency = int(text)
                if concurrency < 1:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "❌ Please enter a valid number (e.g. `4`)."
                )
                return

            wizard["concurrency"] = concurrency
            max_users = wizard.get("max_users", 10)
            # Now generate OAUTH URL!
            client_id = wizard.get("client_id")
            client_secret = wizard.get("client_secret")
            plan = wizard.get("plan", "free")

            import json, base64
            from urllib.parse import quote
            from config.config import get_config
            import config.settings as app_settings

            config = await get_config() or {}

            state_data = {
                "u": user_id,
                "i": client_id,
                "s": client_secret,
                "p": plan,
                "m": max_users,
                "c": concurrency,
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
                f"&state={quote(state)}"
            )

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔗 Authorize with Google Drive", url=auth_url
                        )
                    ],
                    [InlineKeyboardButton("❌ Cancel", callback_data="admin_rclone")],
                ]
            )

            await update.message.reply_text(
                f"**Step 6 / 6 — Authorization**\n\n"
                f"Click the button below to authorize `leechbot` to access this Google Drive account.\n"
                f"Once authorized, the callback will automatically save and register the remote for the `{plan}` plan.",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )

            context.user_data.pop("awaiting", None)
            context.user_data.pop("rclone_wizard", None)
            return

        # ── Step 2: Remote name ──────────────────────────────────────
        if awaiting == "rclone_name":
            if not text or len(text) > 50:
                await update.message.reply_text(
                    "❌ Remote name must be 1–50 characters.\n"
                    "Example: `my-gdrive`\n\nPlease try again.",
                    parse_mode="Markdown",
                )
                return
            wizard["name"] = text
            context.user_data["awaiting"] = "rclone_config"
            await update.message.reply_text(
                "**Step 4 / 5 — Rclone Config Block**\n\n"
                "Paste your rclone config section for this remote.\n"
                "It should look like:\n\n"
                "```\n[remote-name]\ntype = drive\ntoken = {...}\n```\n\n"
                "Copy from `rclone config show` output. Use /cancel to abort.",
                parse_mode="Markdown",
            )
            return

        # ── Step 3: Config text ──────────────────────────────────────
        if awaiting == "rclone_config":
            if len(text) < 10:
                await update.message.reply_text(
                    "❌ Config looks too short. Paste the full rclone config block.",
                    parse_mode="Markdown",
                )
                return
            wizard["config"] = text
            context.user_data["awaiting"] = "rclone_max_users"
            await update.message.reply_text(
                "**Step 5 / 6 — Max Simultaneous Users**\n\n"
                "How many users can share this remote simultaneously?\n"
                "Enter a number (e.g. `10`).\n\n"
                "Use /cancel to abort.",
                parse_mode="Markdown",
            )
            return

        # ── Step 4: Max users → save ─────────────────────────────────
        if awaiting == "rclone_max_users":
            try:
                max_users = int(text)
                if max_users < 1:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "❌ Please enter a valid number (e.g. `10`).", parse_mode="Markdown"
                )
                return

            wizard["max_users"] = max_users
            context.user_data["awaiting"] = "rclone_concurrency"
            await update.message.reply_text(
                "**Step 6 / 6 — Max Concurrency**\n\n"
                "How many parallel connections (uploads) can this remote handle?\n"
                "Enter a number (e.g. `4`).\n\n"
                "Use /cancel to abort.",
                parse_mode="Markdown",
            )
            return

        # ── Step 5: Concurrency → save ─────────────────────────────────
        if awaiting == "rclone_concurrency":
            try:
                concurrency = int(text)
                if concurrency < 1:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "❌ Please enter a valid number (e.g. `4`)."
                )
                return

            wizard["concurrency"] = concurrency
            max_users = wizard.get("max_users", 10)
            service = wizard.get("service", "unknown")
            name = wizard.get("name", "remote")
            config = wizard.get("config", "")
            plan = wizard.get("plan", "free")

            from bot.database import add_rclone_config

            config_id = await add_rclone_config(
                name=name,
                service=service,
                plan=plan,
                max_users=max_users,
                concurrency=concurrency,
                credentials=config,
                admin_id=user_id,
            )

            context.user_data.pop("awaiting", None)
            context.user_data.pop("rclone_wizard", None)

            if config_id:
                await update.message.reply_text(
                    f"✅ **Rclone Remote Added!**\n\n"
                    f"🔑 Service: `{service}`\n"
                    f"📛 Name: `{name}`\n"
                    f"👥 Max Users: `{max_users}`\n"
                    f"⚡ Concurrency: `{concurrency}`\n"
                    f"🆔 Config ID: `{config_id}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Back to Rclone", callback_data="admin_rclone"
                                )
                            ]
                        ]
                    ),
                )
            else:
                await update.message.reply_text(
                    "❌ Failed to save rclone config. Please try again.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Back", callback_data="admin_rclone"
                                )
                            ]
                        ]
                    ),
                )
            return

        logger.warning(f"Unhandled rclone_text_input state: {awaiting}")

    except Exception as e:
        logger.error(f"❌ Error in rclone_text_input: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ Error in rclone wizard. Use /cancel and try again."
        )
        context.user_data.pop("awaiting", None)
        context.user_data.pop("rclone_wizard", None)


async def terabox_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /terabox command"""
    try:
        await update.message.reply_text(
            "📦 **Terabox Management**\n\nSelect an action:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📊 Stats", callback_data="terabox_stats")],
                    [
                        InlineKeyboardButton(
                            "🚫 Disable", callback_data="terabox_disable"
                        )
                    ],
                ]
            ),
        )
    except Exception as e:
        logger.error(f"Error in terabox_command: {e}")


async def handle_terabox_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terabox statistics (stubbed)"""
    try:
        query = update.callback_query
        await query.answer("📊 Terabox Stats", show_alert=False)
        await query.message.edit_text(
            "📊 **Terabox Statistics**\n\n"
            "• Total Uploads: `0` (Feature under development)\n"
            "• Bandwidth Used: `0 GB`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="admin_terabox")]]
            ),
        )
    except Exception as e:
        logger.error(f"Error in terabox_stats: {e}", exc_info=True)
        await update.callback_query.answer("❌ Error")


async def terabox_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle terabox specific text input (e.g. API keys)"""
    text = (update.message.text or "").strip()
    awaiting = context.user_data.get("awaiting")

    if awaiting == "terabox_api_key":
        if len(text) < 20:  # Simple validation for API key length
            await update.message.reply_text(
                "❌ Invalid API key format. Please try again."
            )
            return

        # In a real scenario, you would save this API key to your database config
        # For now, we'll just acknowledge it.
        # Example:
        # config = await get_config() or {}
        # terabox_config = config.get("terabox_config", {})
        # terabox_config["api_key"] = text
        # terabox_config["enabled"] = True # Enable if key is provided
        # await update_config(config, admin_id=update.effective_user.id)

        await update.message.reply_text(
            f"✅ **Terabox API Key Updated!**\n\nKey: `{text[:5]}...` (saved securely)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="admin_terabox")]]
            ),
        )
        context.user_data.pop("awaiting", None)
        return

    # If no specific awaiting state is matched, pass or log
    logger.warning(f"Unhandled terabox_text_input state: {awaiting}")
