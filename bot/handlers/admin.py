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
    get_force_sub_channels,
    update_force_sub_metadata,
    set_channel_config,
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

async def get_queue_stats() -> Dict[str, Any]:
    """Return basic queue/task statistics from DB."""
    try:
        db = get_db()
        pending = await db.tasks.count_documents({"status": "pending"})
        processing = await db.tasks.count_documents({"status": {"$in": ["downloading", "processing", "uploading"]}})
        completed_today = await db.tasks.count_documents({
            "status": "completed",
            "completed_at": {"$gte": datetime.utcnow().replace(hour=0, minute=0, second=0)}
        })
        failed = await db.tasks.count_documents({"status": "failed"})
        return {
            "pending": pending,
            "processing": processing,
            "completed_today": completed_today,
            "failed": failed,
        }
    except Exception as e:
        logger.error(f"❌ get_queue_stats error: {e}")
        return {"pending": 0, "processing": 0, "completed_today": 0, "failed": 0}


async def _require_channels_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Guard helper: returns True if all 3 global channels are configured.
    If any channel is missing, shows the setup screen in-place and returns False.
    Must be called from a callback_query handler.
    """
    from bot.database import get_config
    config = await get_config() or {}
    ch = config.get("channels", {})
    configured = {
        "log":     bool(ch.get("log")),
        "dump":    bool(ch.get("dump")),
        "storage": bool(ch.get("storage")),
    }
    if all(configured.values()):
        return True

    # Build setup screen
    def ch_label(key, icon, name):
        tick = "✅" if configured[key] else "⚠️"
        return InlineKeyboardButton(
            f"{tick} {icon} {name}",
            callback_data=f"admin_set_{key}_channel"
        )

    done_count = sum(configured.values())
    missing_names = [k.title() for k, v in configured.items() if not v]
    setup_keyboard = InlineKeyboardMarkup([
        [ch_label("log",     "📌", "Log Channel")],
        [ch_label("dump",    "💾", "Dump Channel")],
        [ch_label("storage", "🗄️", "Storage Channel")],
        [InlineKeyboardButton("▶ Open Panel",  callback_data="admin_check_and_open")],
        [InlineKeyboardButton("❌ Cancel",      callback_data="start")],
    ])
    setup_text = (
        "⚙️ **Admin Setup — Global Channels**\n\n"
        f"Progress: `{done_count}/3` channels configured\n\n"
        f"Still needed: {', '.join(missing_names)}\n\n"
        "Tap a ⚠️ channel to set it, then press **▶ Open Panel**."
    )
    query = update.callback_query
    if query:
        try:
            await query.message.edit_text(setup_text, reply_markup=setup_keyboard, parse_mode="Markdown")
        except Exception:
            await query.message.reply_text(setup_text, reply_markup=setup_keyboard, parse_mode="Markdown")
    return False

def get_admin_ids() -> List[int]:
    """Get admin IDs with proper error handling"""
    try:
        from config.settings import get_admin_ids as settings_get_admin_ids
        admin_ids = settings_get_admin_ids()
        if not admin_ids:
            logger.warning("⚠️ ADMIN_IDS is empty from settings")
            return []
        return admin_ids
    except Exception as e:
        logger.error(f"❌ Failed to get admin IDs: {e}")
        import os
        admin_str = os.getenv("ADMIN_IDS", "")
        try:
            return [int(x.strip()) for x in admin_str.split(",") if x.strip()]
        except:
            return []

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main admin panel entry point - Responsive"""
    try:
        if update.callback_query:
            is_callback = True
            message = update.callback_query.message
            user = update.effective_user
        else:
            is_callback = False
            message = update.message
            user = update.effective_user

        user_id = user.id

        from bot.database import get_user, get_config
        db_user = await get_user(user_id)

        if not db_user:
            text = "❌ User not found. Use /start first."
            if is_callback:
                await message.edit_text(text)
            else:
                await message.reply_text(text)
            return

        admin_ids = get_admin_ids()
        role = db_user.get("role", "user")

        if role != "admin" and user_id not in admin_ids:
            text = "🚫 **Access Denied**\n\nYou are not an admin."
            if is_callback:
                await message.edit_text(text, parse_mode="Markdown")
            else:
                await message.reply_text(text, parse_mode="Markdown")
            return

        # ── Channel setup guard ──────────────────────────────────────
        # Check nested config structure: channels.log / .dump / .storage
        config = await get_config() or {}
        ch = config.get("channels", {})

        configured = {
            "log":     bool(ch.get("log")),
            "dump":    bool(ch.get("dump")),
            "storage": bool(ch.get("storage")),
        }
        missing_any = not all(configured.values())

        if missing_any:
            # Build one button per channel — ✅ if set, ⚠️ if missing
            def ch_label(key, icon, name):
                tick = "✅" if configured[key] else "⚠️"
                return InlineKeyboardButton(
                    f"{tick} {icon} {name}",
                    callback_data=f"admin_set_{key}_channel"
                )

            setup_keyboard = InlineKeyboardMarkup([
                [ch_label("log",     "📌", "Log Channel")],
                [ch_label("dump",    "💾", "Dump Channel")],
                [ch_label("storage", "🗄️", "Storage Channel")],
                [InlineKeyboardButton("▶ Open Panel",  callback_data="admin_check_and_open")],
                [InlineKeyboardButton("❌ Cancel",      callback_data="start")],
            ])
            done_count = sum(configured.values())
            setup_text = (
                "⚙️ **Admin Setup — Global Channels**\n\n"
                f"Progress: `{done_count}/3` channels configured\n\n"
                "Forward a message from each channel to configure it.\n"
                "The bot must be an **admin** in every channel.\n\n"
                "Tap a channel button to set it up, then press **▶ Open Panel** when done."
            )
            if is_callback:
                await message.edit_text(setup_text, reply_markup=setup_keyboard, parse_mode="Markdown")
            else:
                await message.reply_text(setup_text, reply_markup=setup_keyboard, parse_mode="Markdown")
            return
        # ─────────────────────────────────────────────────────────────

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👥 Users", callback_data="admin_users"),
                InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")
            ],
            [
                InlineKeyboardButton("⚙️ Configuration", callback_data="admin_config"),
                InlineKeyboardButton("⭐ Plans", callback_data="admin_plans")
            ],
            [
                InlineKeyboardButton("🔗 Shorteners", callback_data="admin_shorteners"),
                InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")
            ],
            [
                InlineKeyboardButton("🚫 Banned Users", callback_data="admin_bans"),
                InlineKeyboardButton("💬 Chatbox", callback_data="admin_chatbox")
            ],
            [
                InlineKeyboardButton("🔧 Rclone", callback_data="admin_rclone"),
                InlineKeyboardButton("📦 Terabox", callback_data="admin_terabox")
            ],
            [
                InlineKeyboardButton("🗄️ File Size", callback_data="admin_filesize"),
                InlineKeyboardButton("📋 Logs", callback_data="admin_logs")
            ]
        ])

        # Build force-sub channel summary
        fsub_channels = config.get("channels", {}).get("force_sub", [])
        if fsub_channels:
            fsub_names = ", ".join(
                ch.get("metadata", {}).get("title") or ch.get("name") or str(ch.get("id", "?"))
                for ch in fsub_channels
                if isinstance(ch, dict)
            ) or "None"
        else:
            fsub_names = "Not set"

        admin_message = (
            f"👑 **Admin Panel**\n"
            f"User: [{user.first_name}](tg://user?id={user_id})\n"
            f"Role: `{role.upper()}`\n"
            f"📢 Force Sub: `{fsub_names}`"
        )

        if is_callback:
            await message.edit_text(admin_message, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await message.reply_text(admin_message, reply_markup=keyboard, parse_mode="Markdown")

        await log_admin_action(user_id, "opened_admin_panel", {})
        logger.info(f"✅ Admin panel opened by {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in admin command: {e}", exc_info=True)
        try:
            await update.effective_message.reply_text("❌ Error opening admin panel.")
        except:
            pass

async def admin_check_and_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: ▶ Open Panel on the setup screen.
    Re-checks channel config — if all 3 done → opens admin panel.
    If still missing → refreshes the setup screen with current status.
    """
    query = update.callback_query
    await query.answer()

    from bot.database import get_config
    config = await get_config() or {}
    ch = config.get("channels", {})

    configured = {
        "log":     bool(ch.get("log")),
        "dump":    bool(ch.get("dump")),
        "storage": bool(ch.get("storage")),
    }

    if all(configured.values()):
        # All configured → open the full admin panel
        await admin_command(update, context)
        return

    # Still missing — refresh the setup screen in-place
    def ch_label(key, icon, name):
        tick = "✅" if configured[key] else "⚠️"
        return InlineKeyboardButton(
            f"{tick} {icon} {name}",
            callback_data=f"admin_set_{key}_channel"
        )

    done_count = sum(configured.values())
    missing_names = [k.title() for k, v in configured.items() if not v]
    setup_keyboard = InlineKeyboardMarkup([
        [ch_label("log",     "📌", "Log Channel")],
        [ch_label("dump",    "💾", "Dump Channel")],
        [ch_label("storage", "🗄️", "Storage Channel")],
        [InlineKeyboardButton("▶ Open Panel",  callback_data="admin_check_and_open")],
        [InlineKeyboardButton("❌ Cancel",      callback_data="start")],
    ])
    await query.message.edit_text(
        f"⚙️ **Admin Setup — Global Channels**\n\n"
        f"Progress: `{done_count}/3` channels configured\n\n"
        f"Still needed: {', '.join(missing_names)}\n\n"
        "Tap a ⚠️ channel to set it, then press **▶ Open Panel**.",
        reply_markup=setup_keyboard,
        parse_mode="Markdown"
    )

async def handle_admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return to main admin menu"""
    await admin_command(update, context)

async def handle_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User management menu"""
    try:
        if not await _require_channels_setup(update, context):
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Find User", callback_data="admin_find_user")],
            [InlineKeyboardButton("📜 List All Users", callback_data="admin_list_users_0")],
            [InlineKeyboardButton("🔨 Ban User", callback_data="admin_ban_user")],
            [InlineKeyboardButton("🔓 Unban User", callback_data="admin_unban_user")],
            [InlineKeyboardButton("⬆️ Upgrade User", callback_data="admin_upgrade_user")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
        ])

        await update.callback_query.message.edit_text(
            "👥 **User Management**\n\nSelect an action:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

        await log_admin_action(update.effective_user.id, "opened_user_management", {})

    except Exception as e:
        logger.error(f"❌ Error in users menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_admin_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Show all users paginated"""
    try:
        # Extract page from callback_data if called from router
        if update.callback_query and update.callback_query.data:
            data = update.callback_query.data
            if data.startswith("admin_list_users_") or data.startswith("listusers_page_"):
                try:
                    page = int(data.split("_")[-1])
                except ValueError:
                    page = 0

        users, total = await get_all_users(limit=100)

        if not users:
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_users")]]
            await update.callback_query.message.edit_text(
                "✅ **No Users Yet**\n\nNo users registered in the system.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        # FIX: Build InlineKeyboardButton objects correctly
        buttons = []
        for user in users:
            name = user.get("first_name", "No Name")[:12]
            uid = user.get("telegram_id")
            plan = user.get("plan", "free").upper()
            status = "🚫" if user.get("banned") else "🟢"
            buttons.append(
                InlineKeyboardButton(
                    f"{status} {name} | {plan}",
                    callback_data=f"view_user_{uid}"
                )
            )

        keyboard = paginate_keyboard(buttons, page, per_page=6, prefix="listusers_page")

        keyboard_buttons = list(keyboard.inline_keyboard)
        keyboard_buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_users")])
        keyboard = InlineKeyboardMarkup(keyboard_buttons)

        await update.callback_query.message.edit_text(
            f"📜 **All Users** ({len(users)})\n\nClick to view details:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

        logger.info(f"✅ Listed {len(users)} users (page {page})")

    except Exception as e:
        logger.error(f"❌ Error listing users: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_view_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View detailed user information"""
    try:
        query = update.callback_query
        await query.answer()

        user_id = int(query.data.split("_")[-1])

        user = await get_user(user_id)
        if not user:
            await query.message.edit_text(
                f"❌ **User Not Found**\n\nUser ID: `{user_id}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back", callback_data="admin_list_users_0")
                ]])
            )
            return

        created_at = user.get("created_at", "Unknown")
        files_count = user.get("files_processed", 0)
        pending_tasks = user.get("pending_tasks", 0)

        # FIX: renamed variable from 'info' to 'text' to match edit_text call below
        text = (
            f"👤 **User Profile**\n\n"
            f"**Basic Info:**\n"
            f"- ID: `{user_id}`\n"
            f"- Name: {user.get('first_name', 'Unknown')}\n"
            f"- Username: @{user.get('username', 'None')}\n"
            f"- Plan: **{user.get('plan', 'free').upper()}**\n"
            f"- Status: {'🚫 Banned' if user.get('banned') else '✅ Active'}\n"
            f"- Role: **{user.get('role', 'user').upper()}**\n\n"
            f"**Usage Stats:**\n"
            f"- Files Processed: {files_count}\n"
            f"- Daily Used: {user.get('daily_used', 0)}/{user.get('daily_limit', 5)} GB\n"
            f"- Storage Used: {user.get('used_storage', 0) / (1024**3):.2f} GB\n"
            f"- Pending Tasks: {pending_tasks}\n"
            f"- Created: {created_at}\n\n"
            f"**Settings:**\n"
            f"- Prefix: `{user.get('settings', {}).get('prefix', 'None')}`\n"
            f"- Suffix: `{user.get('settings', {}).get('suffix', 'None')}`\n"
            f"- Mode: {user.get('settings', {}).get('mode', 'Video')}\n"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔨 Ban User", callback_data=f"admin_ban_user_{user_id}")],
            [InlineKeyboardButton("⬆️ Upgrade Plan", callback_data=f"upgrade_user_{user_id}")],
            [InlineKeyboardButton("📊 View Files", callback_data=f"view_files_{user_id}")],
            [InlineKeyboardButton("🗑️ Clear Storage", callback_data=f"clear_storage_{user_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_list_users_0")]
        ])

        # FIX: was 'info' (undefined), now correctly 'text'
        await query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

        await log_admin_action(update.effective_user.id, "viewed_user", {"user_id": user_id})
        logger.info(f"✅ Admin viewed user {user_id}")

    except Exception as e:
        logger.error(f"❌ Error viewing user: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot statistics"""
    try:
        if not await _require_channels_setup(update, context):
            return
        from bot.database import get_admin_stats
        
        db_stats = await get_admin_stats()
        queue_stats = await get_queue_stats()
        
        db = get_db()
        pipeline = [{"$group": {"_id": None, "total": {"$sum": "$daily_used"}}}]
        res = await db.users.aggregate(pipeline).to_list(1)
        today_processed = res[0]["total"] if res else 0

        stats_text = (
            f"📊 **Bot Statistics**\n\n"
            f"👥 Total Users: `{db_stats.get('total_users', 0)}`\n"
            f"✅ Active: `{db_stats.get('active_users', 0)}` | 🚫 Banned: `{db_stats.get('banned_users', 0)}`\n"
            f"💎 Premium: `{db_stats.get('premium_users', 0)}` | 🆓 Free: `{db_stats.get('free_users', 0)}`\n\n"
            f"⚙️ **Queue**\n"
            f"⏳ Pending: `{queue_stats['pending']}`\n"
            f"🔄 Processing: `{queue_stats['processing']}`\n"
            f"✅ Done Today: `{queue_stats['completed_today']}`\n"
            f"❌ Failed: `{queue_stats['failed']}`\n\n"
            f"📦 Today Processed: `{today_processed:.1f} GB`"
        )

        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]

        await update.callback_query.message.edit_text(
            stats_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        await log_admin_action(update.effective_user.id, "viewed_stats", {"total_users": total})
        logger.info(f"✅ Stats displayed: {total} users")

    except Exception as e:
        logger.error(f"❌ Error in stats: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_admin_bans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show banned users list"""
    try:
        await show_banned_users(update, context, page=0)
        await log_admin_action(update.effective_user.id, "viewed_banned_users", {})
    except Exception as e:
        logger.error(f"❌ Error showing banned users: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def show_banned_users(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Show paginated list of banned users"""
    try:
        # Extract page from callback if called from router
        if update.callback_query and update.callback_query.data:
            data = update.callback_query.data
            if data.startswith("banned_page_"):
                try:
                    page = int(data.split("_")[-1])
                except ValueError:
                    page = 0

        banned_users = await get_banned_users(limit=1000)

        if not banned_users:
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]
            await update.callback_query.message.edit_text(
                "✅ **No Banned Users**\n\nAll users are currently active!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        buttons = []
        for user in banned_users:
            name = user.get("first_name", "Unknown")[:15]
            uid = user.get("telegram_id")
            reason = user.get("ban_reason", "No reason")[:15]
            buttons.append(
                InlineKeyboardButton(
                    f"🚫 {name} - {reason}",
                    callback_data=f"unban_user_{uid}"
                )
            )

        keyboard = paginate_keyboard(buttons, page, per_page=6, prefix="banned_page")
        keyboard_buttons = list(keyboard.inline_keyboard)
        keyboard_buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_back")])
        keyboard = InlineKeyboardMarkup(keyboard_buttons)

        await update.callback_query.message.edit_text(
            f"🚫 **Banned Users** ({len(banned_users)} total)\n\nClick a user to unban them:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        logger.info(f"✅ Banned users list shown: {len(banned_users)} users (page {page})")

    except Exception as e:
        logger.error(f"❌ Error showing banned users: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_unban_from_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unban user directly from banned users list"""
    try:
        query = update.callback_query
        await query.answer()

        user_id = int(query.data.split("_")[-1])
        result = await unban_user(user_id, admin_id=update.effective_user.id)

        if result:
            await query.answer("✅ User unbanned!", show_alert=True)
            try:
                await context.bot.send_message(
                    user_id,
                    "✅ **You've Been Unbanned!**\n\nYou can now use the bot again.\n\nUse /start to continue.",
                    parse_mode="Markdown"
                )
            except:
                pass
            await show_banned_users(update, context, page=0)
            await log_admin_action(update.effective_user.id, "unbanned_user", {"user_id": user_id})
            logger.info(f"✅ Admin {update.effective_user.id} unbanned user {user_id}")
        else:
            await query.answer("❌ Failed to unban user", show_alert=True)

    except Exception as e:
        logger.error(f"❌ Error unbanning from list: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast menu"""
    try:
        if not await _require_channels_setup(update, context):
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✉️ Send Message", callback_data="broadcast_compose")],
            [InlineKeyboardButton("📊 View Stats", callback_data="broadcast_stats")],
            [InlineKeyboardButton("⏸️ Pending Broadcasts", callback_data="broadcast_pending")],
            [InlineKeyboardButton("❌ Cancel All", callback_data="broadcast_cancel_input")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
        ])
        broadcast_text = (
            "📢 **Broadcast Center**\n\n"
            "Send a message to all users.\n\n"
            "Use the buttons below to compose, view stats or cancel."
        )
        await update.callback_query.message.edit_text(
            broadcast_text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        logger.info(f"✅ Broadcast menu opened by {update.effective_user.id}")
    except Exception as e:
        logger.error(f"❌ Error in broadcast menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_broadcast_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show broadcast statistics"""
    try:
        config = await get_config() or {}
        broadcasts = config.get("broadcasts", [])

        total_broadcasts = len(broadcasts)
        active = len([b for b in broadcasts if b.get("status") == "pending"])
        completed = len([b for b in broadcasts if b.get("status") == "completed"])
        failed = total_broadcasts - active - completed

        stats_text = (
            f"📊 **Broadcast Statistics**\n\n"
            f"Total: `{total_broadcasts}`\n"
            f"✅ Completed: `{completed}`\n"
            f"⏳ Pending: `{active}`\n"
            f"❌ Failed: `{failed}`"
        )
        await update.callback_query.message.edit_text(
            stats_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_broadcast")]])
        )
        logger.info("✅ Broadcast stats shown")
    except Exception as e:
        logger.error(f"❌ Error showing broadcast stats: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_admin_filesize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """File size management menu"""
    try:
        if not await _require_channels_setup(update, context):
            return
        config = await get_config() or {}
        max_size = config.get("max_file_size_gb", 10)
        storage_used = config.get("storage_used_gb", 0)
        auto_cleanup = config.get('auto_cleanup', False)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📏 Set Max Size", callback_data="set_max_filesize")],
            [InlineKeyboardButton("🗑️ Cleanup Old Files", callback_data="cleanup_old_files")],
            [InlineKeyboardButton("📊 Storage Stats", callback_data="storage_stats")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
        ])

        filesize_text = (
            f"🗄️ **File Size Management**\n\n"
            f"Max upload: `{max_size} GB`\n"
            f"Storage used: `{storage_used:.2f} GB`\n"
            f"Auto-cleanup: `{'✅ On' if auto_cleanup else '❌ Off'}`"
        )
        await update.callback_query.message.edit_text(
            filesize_text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        await log_admin_action(update.effective_user.id, "opened_filesize", {})
        logger.info("✅ File size menu opened")
    except Exception as e:
        logger.error(f"❌ Error in filesize menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_storage_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show storage statistics"""
    try:
        db = get_db()
        pipeline = [{"$group": {"_id": None, "total": {"$sum": "$file_size"}}}]
        cursor = db.cloud_files.aggregate(pipeline)
        result = await cursor.to_list(length=1)
        total_bytes = result[0]["total"] if result else 0
        total_gb = total_bytes / (1024 ** 3)

        await update.callback_query.message.edit_text(
            f"📊 **Storage Statistics**\n\n"
            f"Total cloud files size: `{total_gb:.2f} GB`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_filesize")]])
        )
    except Exception as e:
        logger.error(f"❌ Error in storage stats: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_admin_find_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt for user ID to find"""
    try:
        await update.callback_query.message.reply_text(
            "🔍 **Find User**\n\nSend the User ID to search:\n\nExample: `123456789`\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "admin_find_user"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_admin_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt for user ID to ban"""
    try:
        await update.callback_query.message.reply_text(
            "🔨 **Ban User**\n\nSend the User ID to ban:\n\nExample: `123456789`\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "admin_ban_user"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_admin_unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt for user ID to unban"""
    try:
        await update.callback_query.message.reply_text(
            "🔓 **Unban User**\n\nSend the User ID to unban:\n\nExample: `123456789`\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "admin_unban_user"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_admin_upgrade_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt for user ID to upgrade"""
    try:
        await update.callback_query.message.reply_text(
            "⬆️ **Upgrade User Plan**\n\nSend the User ID and plan:\n\n"
            "Format: `123456789 premium`\n\nAvailable plans: free, premium, pro\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "admin_upgrade_user"
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_admin_rclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rclone configuration menu"""
    try:
        config = await get_config() or {}
        rclone_config = config.get("rclone_config", {})

        try:
            active_configs = await get_rclone_configs(is_active=True)
            active_count = len(active_configs) if active_configs else 0
        except Exception as rc_err:
            logger.error(f"Failed to get rclone configs: {rc_err}", exc_info=True)
            active_count = 0

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Rclone Config", callback_data="admin_add_rclone")],
            [InlineKeyboardButton("📋 List Remotes", callback_data="list_rclone_remotes")],
            [InlineKeyboardButton("🧪 Test Rclone", callback_data="test_rclone")],
            [InlineKeyboardButton("🚫 Disable Rclone", callback_data="disable_rclone")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
        ])

        rclone_status = "✅ Enabled" if rclone_config.get("enabled") else "❌ Disabled"
        rclone_text = (
            f"🔧 **Rclone Configuration**\n\n"
            f"Status: `{rclone_status}`\n"
            f"Active configs: `{active_count}`"
        )
        await update.callback_query.message.edit_text(
            rclone_text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        await log_admin_action(update.effective_user.id, "opened_rclone", {})
        logger.info(f"✅ Rclone menu opened")
    except Exception as e:
        logger.error(f"❌ Error in rclone menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_admin_add_rclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show existing rclone configs and option to add new"""
    try:
        existing_configs = await get_rclone_configs()
        keyboard = []

        if existing_configs:
            config_text = f"📋 **Existing Rclone Configurations** ({len(existing_configs)}):\n\n"
            for config in existing_configs:
                service = config.get('service', 'unknown').upper()
                plan = config.get('plan', 'free')
                max_users = config.get('max_users', 0)
                is_active = config.get('is_active', True)
                config_id = str(config.get('_id', ''))
                status_icon = "✅" if is_active else "❌"
                keyboard.append([InlineKeyboardButton(
                    f"{status_icon} {service} ({plan}) - {max_users} users",
                    callback_data=f"view_rclone_{config_id}"
                )])
            config_text += "\n👇 Select a config to view/edit, or add a new one:"
        else:
            config_text = "📋 **No Rclone Configurations Yet**\n\nClick below to add your first rclone config:"

        keyboard.append([InlineKeyboardButton("➕ Add New Rclone Config", callback_data="admin_add_rclone_wizard")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_rclone")])

        await update.callback_query.message.edit_text(
            config_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        logger.info(f"✅ Rclone list shown: {len(existing_configs) if existing_configs else 0} configs")
    except Exception as e:
        logger.error(f"❌ Error in rclone add handler: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_admin_add_rclone_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start rclone wizard"""
    try:
        services_text = ", ".join(f"`{s}`" for s in RCLONE_SUPPORTED_SERVICES)
        await update.callback_query.message.reply_text(
            f"🔧 **Rclone Config Wizard**\n\nAvailable clouds: {services_text}\n\n"
            f"Send the cloud name (e.g., `gdrive`, `onedrive`).\n\nUse /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data["rclone_step"] = "await_service"
        logger.info("✅ Rclone wizard started")
    except Exception as e:
        logger.error(f"❌ Error in rclone wizard: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_admin_terabox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terabox management menu"""
    try:
        config = await get_config() or {}
        terabox_config = config.get("terabox_config", {})
        status = "✅ Enabled" if terabox_config.get("enabled") else "❌ Disabled"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Setup API Key", callback_data="terabox_setup_key")],
            [InlineKeyboardButton("🧪 Test Connection", callback_data="terabox_test")],
            [InlineKeyboardButton("📊 Stats", callback_data="terabox_stats")],
            [InlineKeyboardButton("🚫 Disable", callback_data="terabox_disable")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
        ])
        await update.callback_query.message.edit_text(
            f"📦 **Terabox Configuration**\n\nStatus: `{status}`",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in terabox menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_terabox_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show terabox statistics"""
    try:
        await update.callback_query.message.edit_text(
            "📊 **Terabox Statistics**\n\nFeature coming soon.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_terabox")]])
        )
        await log_admin_action(update.effective_user.id, "viewed_terabox_stats", {})
    except Exception as e:
        logger.error(f"❌ Error showing terabox stats: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_admin_set_log_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt admin to set log channel"""
    from bot.handlers.user import ask_channel_forward
    await ask_channel_forward(update, context, "log_channel")

async def handle_admin_set_dump_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt admin to set dump channel"""
    from bot.handlers.user import ask_channel_forward
    await ask_channel_forward(update, context, "dump_channel")

async def handle_admin_set_storage_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt admin to set storage channel"""
    from bot.handlers.user import ask_channel_forward
    await ask_channel_forward(update, context, "storage_channel")

async def handle_admin_set_force_sub_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show force-sub channel management menu with requested buttons"""
    try:
        from bot.database import get_force_sub_channels
        fsub_channels = await get_force_sub_channels()

        keyboard = []
        # [Add Channel]
        keyboard.append([InlineKeyboardButton("➕ Add Channel", callback_data="admin_fsub_add")])
        
        # [Configured Channel List]
        if fsub_channels:
            for ch in fsub_channels:
                cid = ch.get("id", "unknown")
                metadata = ch.get("metadata", {})
                title = metadata.get("title") or ch.get("name") or str(cid)
                keyboard.append([InlineKeyboardButton(f"📁 {title}", callback_data=f"admin_fsub_manage_{cid}")])
        else:
            keyboard.append([InlineKeyboardButton("ℹ️ No channels configured", callback_data="ignore")])

        # [Cancel] [Back]
        keyboard.append([
            InlineKeyboardButton("❌ Cancel", callback_data="admin_config"),
            InlineKeyboardButton("🔙 Back", callback_data="admin_back")
        ])

        query = update.callback_query
        await query.message.edit_text(
            f"📢 **Force Subscribe Management**\n\n"
            f"Configure channels that users must join before using the bot.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in force sub menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_admin_fsub_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt admin to add a force-sub channel"""
    from bot.handlers.user import ask_channel_forward
    await ask_channel_forward(update, context, "force_sub_channel")

async def handle_admin_fsub_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage a specific force-sub channel with req_join toggle"""
    try:
        query = update.callback_query
        await query.answer()
        channel_id = int(query.data.replace("admin_fsub_manage_", ""))

        from bot.database import get_force_sub_channels
        channels = await get_force_sub_channels()
        channel = next((ch for ch in channels if ch.get("id") == channel_id), None)

        if not channel:
            await query.answer("❌ Channel not found", show_alert=True)
            return

        metadata = channel.get("metadata", {})
        title = metadata.get("title") or channel.get("name") or str(channel_id)
        req_join = metadata.get("req_join", False)
        
        # Toggle icon
        req_icon = "✅ Green Tick (Req Join)" if req_join else "❌ Red Cross (Direct Join)"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Channel: {title}", callback_data="ignore")],
            [InlineKeyboardButton(f"Req to Join: {req_icon}", callback_data=f"admin_fsub_req_toggle_{channel_id}")],
            [InlineKeyboardButton("🗑️ Remove Channel", callback_data=f"admin_fsub_remove_confirm_{channel_id}")],
            [
                InlineKeyboardButton("🔙 Back", callback_data="admin_set_force_sub_channel"),
                InlineKeyboardButton("✅ Apply", callback_data="admin_set_force_sub_channel")
            ]
        ])
        
        await query.message.edit_text(
            f"📢 **Manage Force Sub Channel**\n\n"
            f"Channel: `{title}`\n"
            f"ID: `{channel_id}`\n\n"
            f"• **Req to Join**: If enabled, users will see a 'Request to Join' button. If disabled, they must join directly.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in fsub manage: {e}", exc_info=True)
        await query.answer("❌ Error", show_alert=True)

async def handle_admin_fsub_req_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle the req_join metadata field"""
    try:
        query = update.callback_query
        channel_id = int(query.data.replace("admin_fsub_req_toggle_", ""))
        
        from bot.database import get_force_sub_channels, update_force_sub_metadata
        channels = await get_force_sub_channels()
        channel = next((ch for ch in channels if ch.get("id") == channel_id), None)
        
        if channel:
            current = channel.get("metadata", {}).get("req_join", False)
            await update_force_sub_metadata(channel_id, {"req_join": not current}, admin_id=update.effective_user.id)
            await query.answer("✅ Req to Join toggled", show_alert=False)
            await handle_admin_fsub_manage(update, context)
    except Exception as e:
        logger.error(f"❌ Error toggling req_join: {e}")
        await query.answer("❌ Error", show_alert=True)

async def handle_admin_fsub_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle force-sub channel enabled/disabled"""
    try:
        query = update.callback_query
        await query.answer()
        channel_id = query.data.replace("admin_fsub_toggle_", "")

        config = await get_config() or {}
        channels = config.get("force_sub_channels", [])
        for ch in channels:
            if str(ch.get("channel_id")) == str(channel_id):
                ch["enabled"] = not ch.get("enabled", True)
                new_status = "✅ Enabled" if ch["enabled"] else "❌ Disabled"
                await query.answer(f"Channel {new_status}", show_alert=True)
                break
        config["force_sub_channels"] = channels
        await update_config(config, admin_id=update.effective_user.id)
        await handle_admin_set_force_sub_channel(update, context)
    except Exception as e:
        logger.error(f"❌ Error toggling fsub: {e}", exc_info=True)

async def handle_admin_fsub_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get invite link for force-sub channel"""
    try:
        query = update.callback_query
        await query.answer()
        channel_id = query.data.replace("admin_fsub_link_", "")
        try:
            link = await context.bot.export_chat_invite_link(int(channel_id))
            await query.message.reply_text(f"🔗 Invite link: {link}")
        except Exception as le:
            await query.answer(f"❌ Could not get link: {str(le)[:50]}", show_alert=True)
    except Exception as e:
        logger.error(f"❌ Error in fsub link: {e}", exc_info=True)

async def handle_admin_fsub_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask confirmation to remove force-sub channel"""
    try:
        query = update.callback_query
        await query.answer()
        channel_id = query.data.replace("admin_fsub_remove_confirm_", "")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Remove", callback_data=f"admin_fsub_remove_{channel_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"admin_fsub_manage_{channel_id}")]
        ])
        await query.message.edit_text(
            f"⚠️ **Remove Force Sub Channel?**\n\nChannel ID: `{channel_id}`\n\nThis will stop forcing users to join this channel.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in fsub remove confirm: {e}", exc_info=True)

async def handle_admin_fsub_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove force-sub channel"""
    try:
        query = update.callback_query
        await query.answer()
        channel_id = query.data.replace("admin_fsub_remove_", "")

        config = await get_config() or {}
        channels = config.get("force_sub_channels", [])
        channels = [ch for ch in channels if str(ch.get("channel_id")) != str(channel_id)]
        config["force_sub_channels"] = channels
        await update_config(config, admin_id=update.effective_user.id)

        await query.answer("✅ Channel removed", show_alert=True)
        await handle_admin_set_force_sub_channel(update, context)
        await log_admin_action(update.effective_user.id, "removed_fsub_channel", {"channel_id": channel_id})
    except Exception as e:
        logger.error(f"❌ Error removing fsub: {e}", exc_info=True)

async def handle_admin_remove_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove log channel from config"""
    try:
        config = await get_config() or {}
        config.pop("log_channel_id", None)
        await update_config(config, admin_id=update.effective_user.id)
        await update.callback_query.answer("✅ Log channel removed", show_alert=True)
        from bot.handlers.settings import show_config_menu
        await show_config_menu(update, context)
    except Exception as e:
        logger.error(f"❌ Error removing log channel: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_admin_remove_dump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove dump channel from config"""
    try:
        config = await get_config() or {}
        config.pop("dump_channel_id", None)
        await update_config(config, admin_id=update.effective_user.id)
        await update.callback_query.answer("✅ Dump channel removed", show_alert=True)
        from bot.handlers.settings import show_config_menu
        await show_config_menu(update, context)
    except Exception as e:
        logger.error(f"❌ Error removing dump channel: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_admin_remove_storage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove storage channel from config"""
    try:
        config = await get_config() or {}
        config.pop("storage_channel_id", None)
        await update_config(config, admin_id=update.effective_user.id)
        await update.callback_query.answer("✅ Storage channel removed", show_alert=True)
        from bot.handlers.settings import show_config_menu
        await show_config_menu(update, context)
    except Exception as e:
        logger.error(f"❌ Error removing storage channel: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_admin_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin logs menu"""
    try:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 View Recent", callback_data="view_logs_0")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
        ])
        await update.callback_query.message.edit_text(
            "📋 **Admin Logs**\n\nView recent bot activity.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Error in logs menu: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

async def handle_admin_chatbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show chatbox/support messages"""
    try:
        messages = await get_chatbox_messages(limit=10)
        if not messages:
            text = "💬 **Chatbox**\n\nNo support messages yet."
        else:
            text = f"💬 **Chatbox** ({len(messages)} messages)\n\n"
            for msg in messages[:5]:
                uid = msg.get("user_id", "?")
                content = str(msg.get("message", ""))[:50]
                text += f"• `{uid}`: {content}\n"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]])
        await update.callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"❌ Error in chatbox: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error", show_alert=True)

def admin_required(func):
    """Decorator to check if user is admin"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        admin_ids = get_admin_ids()
        if user_id not in admin_ids:
            try:
                await update.message.reply_text(
                    "🚫 **Access Denied**\n\nThis command requires admin privileges.",
                    parse_mode="Markdown"
                )
            except:
                await update.callback_query.answer("🚫 Access Denied", show_alert=True)
            logger.warning(f"⚠️ Unauthorized admin access attempt: {user_id}")
            return
        return await func(update, context)
    return wrapper

async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str = None):
    """Handle admin text input based on state"""
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    try:
        if state == "admin_find_user":
            try:
                uid = int(text)
                user = await get_user(uid)
                if user:
                    user_info = (
                        f"👤 **User Found**\n\n"
                        f"ID: `{uid}`\n"
                        f"Name: {user.get('first_name')}\n"
                        f"Plan: {user.get('plan')}"
                    )
                    await update.message.reply_text(user_info, parse_mode="Markdown")
                else:
                    await update.message.reply_text("❌ User not found.")
            except Exception as e:
                logger.error(f"Error finding user: {e}")
                await update.message.reply_text("❌ Invalid User ID.")
            context.user_data.pop("awaiting", None)
            return

        # BUG-12 FIX: added missing admin action states
        if state == "admin_ban_user":
            try:
                uid = int(text.split()[0])
                reason = " ".join(text.split()[1:]) or "No reason"
                from bot.database import ban_user
                ok = await ban_user(uid, reason=reason, admin_id=user_id)
                if ok:
                    await update.message.reply_text(f"✅ User `{uid}` has been banned.\nReason: {reason}", parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"❌ Could not ban user `{uid}`. User may not exist.", parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ Invalid input. Send: `<user_id> <reason>`", parse_mode="Markdown")
            context.user_data.pop("awaiting", None)
            return

        if state == "admin_unban_user":
            try:
                uid = int(text)
                from bot.database import unban_user
                ok = await unban_user(uid, admin_id=user_id)
                if ok:
                    await update.message.reply_text(f"✅ User `{uid}` has been unbanned.", parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"❌ Could not unban user `{uid}`. User may not exist.", parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text("❌ Invalid User ID. Please send a number.", parse_mode="Markdown")
            context.user_data.pop("awaiting", None)
            return

        if state == "admin_upgrade_user":
            try:
                parts = text.split()
                uid = int(parts[0])
                plan = parts[1].lower() if len(parts) > 1 else "premium"
                ok = await update_user(uid, {"plan": plan})
                if ok:
                    await update.message.reply_text(f"✅ User `{uid}` upgraded to `{plan}`.", parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"❌ Could not upgrade user `{uid}`.", parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text("❌ Usage: `<user_id> [plan_name]`\nExample: `123456789 premium`", parse_mode="Markdown")
            context.user_data.pop("awaiting", None)
            return

        logger.warning(f"Unhandled admin input state: {state}")

    except Exception as e:
        logger.error(f"Error in handle_admin_input: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def get_broadcast_stats() -> Dict[str, Any]:
    """Get broadcast statistics"""
    try:
        db = get_db()
        total = await db.broadcasts.count_documents({})
        sent = await db.broadcasts.count_documents({"status": "completed"})
        draft = await db.broadcasts.count_documents({"status": "draft"})
        return {"total": total, "sent": sent, "draft": draft}
    except Exception as e:
        logger.error(f"Error getting broadcast stats: {e}")
        return {"total": 0, "sent": 0, "draft": 0}

async def handle_admin_forwards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin channel forward for global channel setup.
    Reads awaiting_channel_type from context.user_data, extracts the
    channel ID from the forwarded message, saves it to DB config,
    then clears the awaiting state.
    """
    try:
        msg = update.message
        channel_type = context.user_data.get("awaiting_channel_type", "")

        if not channel_type:
            await msg.reply_text("⚠️ No channel setup in progress. Use /admin → Config first.")
            return

        # Extract channel from forward_origin
        forward_origin = msg.forward_origin
        channel_id = None
        channel_title = "Unknown"

        if forward_origin and hasattr(forward_origin, "chat"):
            channel_id = forward_origin.chat.id
            channel_title = forward_origin.chat.title or str(channel_id)
        elif msg.forward_from_chat:
            # Fallback for older PTB versions
            channel_id = msg.forward_from_chat.id
            channel_title = msg.forward_from_chat.title or str(channel_id)

        if not channel_id:
            await msg.reply_text(
                "❌ **Could Not Read Channel**\n\n"
                "Please forward a message **directly from the channel** (not from a user).\n"
                "Make sure the bot is an admin in that channel first.",
                parse_mode="Markdown"
            )
            return

        # Nested DB structure: channels.log / channels.dump / channels.storage
        # (matches ensure_channel_schema nested format in database.py)
        TYPE_TO_NESTED_KEY = {
            "log_channel":     "channels.log",
            "dump_channel":    "channels.dump",
            "storage_channel": "channels.storage",
            "force_sub_channel": None,   # handled separately below
        }

        admin_id = update.effective_user.id
        from bot.database import set_config, get_config

        if channel_type == "force_sub_channel":
            # Add to nested channels.force_sub array
            config = await get_config() or {}
            fsub_list = config.get("channels", {}).get("force_sub", [])
            if not isinstance(fsub_list, list):
                fsub_list = []
            # Avoid duplicates
            if not any(
                (c.get("id") if isinstance(c, dict) else c) == channel_id
                for c in fsub_list
            ):
                fsub_list.append({"id": channel_id, "name": channel_title, "enabled": True})
                await set_config({"channels.force_sub": fsub_list})
            label = "Force Subscribe Channel"
        else:
            nested_key = TYPE_TO_NESTED_KEY.get(channel_type)
            if not nested_key:
                await msg.reply_text(f"❌ Unknown channel type: `{channel_type}`", parse_mode="Markdown")
                return
            # Save as nested object with id + metadata
            await set_config({nested_key: {"id": channel_id, "metadata": {"title": channel_title}}})
            label = channel_type.replace("_", " ").title()

        await log_admin_action(admin_id, f"set_{channel_type}", {
            "channel_id": channel_id, "title": channel_title
        })

        context.user_data.pop("awaiting_channel_type", None)

        await msg.reply_text(
            f"✅ **{label} Set!**\n\n"
            f"Channel: `{channel_title}`\n"
            f"ID: `{channel_id}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back to Config", callback_data="admin_config")]]
            )
        )
        logger.info(f"✅ {channel_type} set to {channel_id} by admin {admin_id}")

    except Exception as e:
        logger.error(f"❌ Error in handle_admin_forwards: {e}", exc_info=True)
        await update.message.reply_text("❌ Failed to save channel. Please try again.")

async def handle_admin_rclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show rclone management menu."""
    try:
        if not await _require_channels_setup(update, context):
            return
        query = update.callback_query or None
        if query:
            await query.answer()
            edit = query.message.edit_text
        else:
            edit = update.message.reply_text

        from bot.database import get_rclone_configs
        configs = await get_rclone_configs()
        count = len(configs) if configs else 0

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add New Remote", callback_data="admin_add_rclone_wizard")],
            [InlineKeyboardButton("📋 List Remotes", callback_data="list_rclone_remotes")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_back")],
        ])
        await edit(
            f"🔧 **Rclone Management**\n\n"
            f"Active remotes: `{count}`\n\n"
            "Add a remote to enable cloud storage for users.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ handle_admin_rclone: {e}", exc_info=True)

async def handle_admin_add_rclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias — shows the wizard start."""
    await handle_admin_add_rclone_wizard(update, context)

async def handle_admin_add_rclone_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1 of rclone wizard — choose the cloud service."""
    try:
        query = update.callback_query
        if query:
            await query.answer()
        msg = query.message if query else update.message

        context.user_data["rclone_wizard"] = {}  # reset wizard state

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("☁️ Google Drive", callback_data="rclone_gdrive"),
                InlineKeyboardButton("📁 OneDrive",     callback_data="rclone_onedrive"),
            ],
            [
                InlineKeyboardButton("📦 Dropbox", callback_data="rclone_dropbox"),
                InlineKeyboardButton("🌐 Mega",    callback_data="rclone_mega"),
            ],
            [
                InlineKeyboardButton("☁️ Amazon S3", callback_data="rclone_s3"),
                InlineKeyboardButton("🔧 Custom",   callback_data="rclone_custom"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_rclone")],
        ])
        await msg.reply_text(
            "🔧 **Add Rclone Remote — Step 1 / 4**\n\n"
            "Select the cloud storage service:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ handle_admin_add_rclone_wizard: {e}", exc_info=True)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command"""
    try:
        user_id = update.effective_user.id

        # Determine if called from message or callback
        is_callback = bool(update.callback_query)
        send_fn = (
            update.callback_query.message.edit_text
            if is_callback
            else update.message.reply_text
        )

        user = await get_user(user_id)

        if not user:
            await send_fn(
                "⚠️ **No Stats Available**\n\n"
                "Use /start first to initialize your account.",
                parse_mode="Markdown"
            )
            return  # ← was missing; fell through and crashed on stats_text

        # Build stats text from user document
        plan          = user.get("plan", "free").upper()
        files_done    = user.get("files_processed", 0)
        used_bytes    = user.get("used_storage", 0)
        limit_bytes   = user.get("storage_limit", 5 * 1024 ** 3)
        daily_used    = user.get("daily_used", 0)
        daily_limit   = user.get("daily_limit", 5)
        used_gb       = used_bytes / (1024 ** 3)
        limit_gb      = limit_bytes / (1024 ** 3)
        pct           = (used_gb / limit_gb * 100) if limit_gb > 0 else 0
        bar_filled    = int(pct / 10)
        bar           = "█" * bar_filled + "░" * (10 - bar_filled)
        plan_emoji    = "💎" if plan != "FREE" else "⭐"

        stats_text = (
            f"{plan_emoji} **Your Statistics**\n\n"
            f"📦 **Plan:** `{plan}`\n"
            f"✅ **Files Processed:** `{files_done}`\n\n"
            f"**Storage Usage:**\n"
            f"[{bar}] `{pct:.1f}%`\n"
            f"`{used_gb:.2f} GB` / `{limit_gb:.1f} GB`\n\n"
            f"**Daily Uploads:** `{daily_used}` / `{daily_limit}`"
        )

        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="start")]]

        await send_fn(
            stats_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        await log_info(f"✅ /stats used by {user_id}")

    except Exception as e:
        logger.error(f"❌ Error in stats command: {e}", exc_info=True)
        await log_error(f"❌ Error in stats command: {str(e)}")
        try:
            (update.message or update.callback_query.message).reply_text(
                "❌ Unable to fetch stats. Please try again.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

async def handle_admin_chatbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent chatbox messages (support inbox)"""
    try:
        query = update.callback_query
        await query.answer()
        messages = await get_chatbox_messages(limit=20)
        if not messages:
            text = "💬 **Chatbox** \n\nNo messages yet."
        else:
            lines = ["💬 **Recent Support Messages**\n"]
            for m in messages[:10]:
                uid = m.get("user_id", "?")
                sender = m.get("sender_type", "user")
                msg = str(m.get("message", ""))[:80]
                ts = m.get("timestamp", "").strftime("%m/%d %H:%M") if hasattr(m.get("timestamp"), "strftime") else ""
                lines.append(f"[{ts}] {'\ud83d\udc64' if sender == 'user' else '\ud83e\udd16'} {uid}: {msg}")
            text = "\n".join(lines)
        await query.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]])
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_admin_chatbox: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_admin_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent admin action logs"""
    try:
        query = update.callback_query
        await query.answer()

        # Extract page from callback data (view_logs_0, view_logs_1, etc.)
        page = 0
        if query.data and query.data.startswith("view_logs_"):
            try:
                page = int(query.data.split("_")[-1])
            except ValueError:
                page = 0

        db = get_db()
        skip = page * 10
        cursor = db.admin_logs.find({}).sort("timestamp", -1).skip(skip).limit(10)
        logs = await cursor.to_list(length=10)
        total = await db.admin_logs.count_documents({})

        if not logs:
            text = "📋 **Admin Logs**\n\nNo logs recorded yet."
        else:
            lines = [f"📋 **Admin Logs** (page {page + 1})\n"]
            for log in logs:
                admin = log.get("admin_id", "?")
                action = log.get("action", "unknown")
                ts = log.get("timestamp", "")
                ts_str = ts.strftime("%m/%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
                lines.append(f"● [{ts_str}] `{admin}` → {action}")
            text = "\n".join(lines)

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"view_logs_{page-1}"))
        if (page + 1) * 10 < total:
            nav.append(InlineKeyboardButton("Next ➡", callback_data=f"view_logs_{page+1}"))
        keyboard = [nav] if nav else []
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_back")])

        await query.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_admin_logs: {e}", exc_info=True)
        await update.callback_query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)

async def handle_admin_shorteners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortener management placeholder (future feature)"""
    try:
        query = update.callback_query
        await query.answer()
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]])
        await query.message.edit_text(
            "🔗 **URL Shorteners**\n\n"
            "Shortener integration is not yet configured.\n\n"
            "You can add API keys for services like:\n"
            "• bit.ly\n"
            "• cutt.ly\n"
            "• tinyurl\n\n"
            "_Feature coming soon._",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"❌ Error in handle_admin_shorteners: {e}")
        await update.callback_query.answer("❌ Error", show_alert=True)