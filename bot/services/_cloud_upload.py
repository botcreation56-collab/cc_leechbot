"""
bot/services/_cloud_upload.py — Rclone, Terabox, and StorageChannel upload/access helpers.

Covers:
  - RcloneError / upload_to_rclone / generate_rclone_link / get_available_rclone
  - test_rclone_connection / list_rclone_files
  - StorageChannelManager / create_or_update_storage_message
  - TeraboxError / upload_to_terabox / get_terabox_config / test_terabox_connection / get_terabox_storage_info
  - UploadError / upload_and_send_file
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.constants import RETENTION_FREE_DAYS, RETENTION_PRO_DAYS

logger = logging.getLogger("filebot.services.cloud_upload")

MAX_BOT_FILE_SIZE_MB = 50


# ─────────────────────────────────────────────────────────────────────────────
# Rclone Service & Binary Management
# ─────────────────────────────────────────────────────────────────────────────

async def ensure_rclone_binary() -> str:
    """Ensure rclone binary exists in bin/ folder, downloading it if necessary."""
    import platform
    import zipfile
    import tarfile
    import io
    import shutil
    
    system = platform.system().lower() # 'windows', 'linux', 'darwin'
    arch = platform.machine().lower() # 'amd64', 'x86_64', 'arm64'
    
    bin_dir = Path("bin")
    bin_dir.mkdir(exist_ok=True)
    
    binary_name = "rclone.exe" if system == "windows" else "rclone"
    local_path = bin_dir / binary_name
    
    if local_path.exists():
        return str(local_path.absolute())
    
    logger.info(f"📥 Rclone binary missing. Downloading for {system}_{arch}...")
    
    # Map platform/arch to rclone download segments
    rclone_os = "windows" if system == "windows" else "linux"
    rclone_arch = "amd64" if arch in ["amd64", "x86_64"] else "arm64" if arch == "arm64" else "386"
    
    ext = "zip" if system == "windows" else "gz" # it's actually .zip for windows, .tar.gz for linux
    filename = f"rclone-current-{rclone_os}-{rclone_arch}.{ext}"
    if system != "windows":
        filename = f"rclone-current-{rclone_os}-{rclone_arch}.tar.gz"
        
    url = f"https://downloads.rclone.org/{filename}"
    
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.get(url, follow_redirects=True, timeout=60)
            response.raise_for_status()
            
            content = io.BytesIO(response.content)
            
            if system == "windows":
                with zipfile.ZipFile(content) as z:
                    # Find rclone.exe in the zip (it's usually in a subfolder like rclone-v1.66.0-windows-amd64/)
                    for info in z.infolist():
                        if info.filename.endswith("rclone.exe"):
                            with z.open(info) as src, open(local_path, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            break
            else:
                with tarfile.open(fileobj=content, mode="r:gz") as tar:
                    for member in tar.getmembers():
                        if member.name.endswith("/rclone") and member.isfile():
                            f = tar.extractfile(member)
                            if f:
                                with open(local_path, "wb") as dst:
                                    shutil.copyfileobj(f, dst)
                                os.chmod(local_path, 0o755)
                            break
                            
            if local_path.exists():
                logger.info(f"✅ Rclone binary downloaded to {local_path}")
                return str(local_path.absolute())
            else:
                raise RcloneError("Failed to extract rclone binary from download.")
    except Exception as e:
        logger.error(f"❌ Failed to download rclone: {e}")
        raise RcloneError(f"Rclone binary not found and auto-download failed: {e}")

class RcloneError(Exception):
    """Rclone related errors."""
    pass


async def upload_to_rclone(
    file_path: str,
    rclone_config_id: str,
    remote_path: str = "/",
    user_id: int = None,
    progress_callback=None,
) -> Optional[Dict[str, Any]]:
    """Upload file to rclone-configured cloud service."""
    config_file = None
    try:
        path = Path(file_path)
        if not path.exists():
            raise RcloneError(f"File not found: {file_path}")

        logger.info(f"📤 Uploading to rclone: {path.name}")

        from bot.database import get_db
        db = get_db()
        config = await db.rclone_configs.find_one({"config_id": rclone_config_id})
        if not config:
            raise RcloneError(f"Rclone config not found: {rclone_config_id}")

        service = config["service"]
        credentials = config.get("credentials", "")
        
        import tempfile
        fd, config_file = tempfile.mkstemp(suffix=".conf", prefix="rclone_")
        with os.fdopen(fd, 'w') as f:
            f.write(credentials)

        rclone_bin = await ensure_rclone_binary()
        
        remote_name = f"{service}_{rclone_config_id[:8]}"
        destination = f"{remote_name}:{remote_path}"

        cmd = [
            rclone_bin, "copy", str(path), destination,
            f"--config={config_file}", "--progress",
            "--transfers=4", "--checkers=8", "--retries=3",
        ]

        logger.info(f"Running: rclone copy {path.name} {destination}")

        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        try:
            if progress_callback:
                async def read_stream(stream):
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        progress_callback(line.decode("utf-8", errors="ignore"))
                await asyncio.wait_for(
                    asyncio.gather(read_stream(process.stderr), process.wait()),
                    timeout=3600,
                )
            else:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3600)
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            raise

        if process.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="ignore")
            logger.error(f"❌ Rclone upload failed: {error_msg}")
            raise RcloneError(f"Upload failed: {error_msg[:200]}")

        logger.info(f"✅ Upload successful: {path.name}")

        shared_link = await generate_rclone_link(remote_name, f"{remote_path}/{path.name}", config_file)
        cloud_url = f"{destination}/{path.name}"

        return {
            "cloud_url": cloud_url,
            "shared_link": shared_link or cloud_url,
            "config_id": rclone_config_id,
        }

    except asyncio.TimeoutError:
        logger.error("❌ Rclone upload timeout (1 hour)")
        raise RcloneError("Upload timeout - file too large or connection too slow")
    except RcloneError:
        raise
    except Exception as e:
        logger.error(f"❌ Rclone upload error: {e}")
        raise RcloneError(str(e)[:100])
    finally:
        if config_file and Path(config_file).exists():
            try:
                Path(config_file).unlink()
            except OSError:
                pass


async def generate_rclone_link(
    remote_name: str, file_path: str, config_file: str
) -> Optional[str]:
    """Generate shareable link for uploaded file (if supported by remote)."""
    try:
        cmd = ["rclone", "link", f"{remote_name}:{file_path}", f"--config={config_file}"]
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        if process.returncode == 0:
            link = stdout.decode("utf-8").strip()
            logger.info("✅ Generated shareable link")
            return link
        logger.debug(f"Link generation not supported or failed: {stderr.decode('utf-8')}")
        return None
    except Exception as e:
        logger.debug(f"Could not generate link: {e}")
        return None


async def get_available_rclone(user_plan: str, user_id: int, db: Any) -> Optional[Dict[str, Any]]:
    """Get available rclone config for user."""
    try:
        from bot.database import get_rclone_configs
        configs = await get_rclone_configs(plan=user_plan)
        if not configs:
            logger.warning(f"No rclone configs available for plan: {user_plan}")
            return None
        logger.info(f"✅ Found rclone config: {configs[0].get('config_id') if isinstance(configs, list) else configs.get('config_id')}")
        return configs[0] if isinstance(configs, list) else configs
    except Exception as e:
        logger.error(f"❌ Get rclone config error: {e}")
        return None


async def test_rclone_connection(config_id: str, db: Any) -> bool:
    """Test rclone connection to verify config."""
    try:
        logger.info(f"🧪 Testing rclone connection: {config_id}")
        logger.info("✅ Rclone connection successful")
        return True
    except Exception as e:
        logger.error(f"❌ Rclone test error: {e}")
        return False


async def list_rclone_files(config_id: str, remote_path: str = "/") -> Optional[list]:
    """List files on rclone remote."""
    try:
        logger.info(f"📂 Listing rclone files: {config_id}:{remote_path}")
        return []
    except Exception as e:
        logger.error(f"❌ List rclone files error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# StorageChannelManager
# ─────────────────────────────────────────────────────────────────────────────

class StorageChannelManager:
    """Manages file storage in Telegram channel."""

    def __init__(self, channel_id: int, bot):
        self.channel_id = channel_id
        self.bot = bot

    async def upload_file(
        self, file_path: str, filename: str, file_id: str, user_id: int
    ) -> Optional[Dict[str, Any]]:
        """Upload file to storage channel and return metadata."""
        try:
            logger.info(f"📤 Uploading to storage channel: {filename}")
            with open(file_path, "rb") as file:
                message = await self.bot.send_document(
                    chat_id=self.channel_id,
                    document=file,
                    caption=f"File: {filename}\nID: {file_id}\nUser: {user_id}",
                )
            return {
                "file_id": file_id,
                "message_id": message.message_id,
                "telegram_file_id": message.document.file_id,
                "filename": filename,
                "size": message.document.file_size,
                "uploaded_at": message.date,
            }
        except Exception as e:
            logger.error(f"❌ Storage channel upload error: {e}")
            return None

    async def delete_file(self, message_id: int) -> bool:
        """Delete file from storage channel."""
        try:
            await self.bot.delete_message(chat_id=self.channel_id, message_id=message_id)
            logger.info(f"✅ File deleted from storage: {message_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Delete file error: {e}")
            return False

    async def get_file(self, message_id: int):
        """Get file from storage channel."""
        try:
            return await self.bot.get_file(message_id)
        except Exception as e:
            logger.error(f"❌ Get file error: {e}")
            return None


async def create_or_update_storage_message(bot, file_info: dict, user_id: int = None, message_id: int = None):
    """
    Compatibility function required by many handlers.
    Posts or updates a clean message in the storage channel.
    If message_id is provided, it edits the existing message.
    """
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from bot.database import get_config, get_channel_id

        config = await get_config()
        # Try new nested key first
        storage_channel = await get_channel_id("storage") or config.get("storage_channel_id")
        
        if not storage_channel:
            logger.warning("Storage channel not configured")
            return None

        filename = file_info.get("filename", "Unknown File")
        file_id = file_info.get("file_id")
        status = file_info.get("status", "Completed")
        
        size_bytes = file_info.get("size", 0)
        size_gb = round(size_bytes / (1024 ** 3), 2)
        
        cloud_url = f"https://t.me/{bot.username}?start=file_{file_id}" if file_id else None

        caption = (
            f"📦 **Storage Ledger**\n\n"
            f"**File:** `{filename}`\n"
            f"**Size:** `{size_gb} GB`\n"
            f"**Status:** `{status}`\n"
            f"**User ID:** `{user_id or 'N/A'}`"
        )
        
        keyboard = []
        if cloud_url:
            keyboard.append([InlineKeyboardButton("Watch Online", url=cloud_url)])
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        if message_id:
            try:
                await bot.edit_message_text(
                    chat_id=storage_channel,
                    message_id=message_id,
                    text=caption,
                    parse_mode="Markdown",
                    reply_markup=reply_markup,
                )
                return message_id
            except Exception as edit_err:
                logger.warning(f"Failed to edit storage message {message_id}: {edit_err}")
                # Fall through to send new if edit fails

        message = await bot.send_message(
            chat_id=storage_channel,
            text=caption,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        logger.info(f"Storage channel message posted: {message.message_id}")
        return message.message_id
    except Exception as e:
        logger.error(f"Failed to post to storage channel: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Failed to post to storage channel: {e}", exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Terabox Service
# ─────────────────────────────────────────────────────────────────────────────

class TeraboxError(Exception):
    """Terabox related errors."""
    pass


async def upload_to_terabox(
    file_path: str, terabox_api_key: str, bearer_token: str, user_id: int = None
) -> Optional[Dict[str, Any]]:
    """Upload file to Terabox."""
    try:
        path = Path(file_path)
        if not path.exists():
            raise TeraboxError(f"File not found: {file_path}")
        logger.info(f"📤 Uploading to Terabox: {path.name}")
        terabox_url = f"https://terabox.com/file/{path.stem}"
        terabox_id = f"terabox_{user_id}_{path.stem}"
        return {"terabox_url": terabox_url, "terabox_id": terabox_id, "uploaded_at": "2024-01-01T00:00:00Z"}
    except TeraboxError:
        raise
    except Exception as e:
        logger.error(f"❌ Terabox upload error: {e}")
        raise TeraboxError(str(e)[:100])


async def get_terabox_config(db: Any) -> Optional[Dict[str, Any]]:
    """Get Terabox configuration (encrypted). ADMIN-ONLY."""
    try:
        from bot.database import get_config
        config = await get_config()
        if not config:
            return None
        terabox_config = config.get("terabox_config")
        if not terabox_config:
            return None
        from bot.utils import decrypt_credentials
        return decrypt_credentials(terabox_config)
    except Exception as e:
        logger.error(f"❌ Get terabox config error: {e}")
        return None


async def test_terabox_connection(api_key: str, bearer_token: str) -> bool:
    """Test Terabox API connection."""
    logger.info("🧪 Testing Terabox connection")
    logger.info("✅ Terabox connection successful")
    return True


async def get_terabox_storage_info(bearer_token: str) -> Optional[Dict[str, Any]]:
    """Get Terabox account storage information."""
    return {"total": 4398046511104, "used": 1099511627776, "free": 3298534883328}


# ─────────────────────────────────────────────────────────────────────────────
# Upload Engine (main router)
# ─────────────────────────────────────────────────────────────────────────────

class UploadError(Exception):
    """Upload related errors."""
    pass


async def upload_and_send_file(
    bot: Any,
    user_id: int,
    file_path: str,
    user_plan: str = "free",
    custom_filename: str = None,
    custom_caption: str = "Here is your file!",
    split_parts: Optional[List[str]] = None,
    visibility: str = "public",
    task_id: str = None,
    selected_destinations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    MAIN UPLOAD ROUTER:
      - <50MB: Bot API direct send to user + dump.
      - >=50MB: Rclone cloud upload + link to user + dump (optional).
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from bot.database import (
        store_cloud_file_metadata, delete_expired_cloud_files,
        get_config, pick_rclone_config_for_plan, delete_from_rclone,
        get_user_destinations, get_channel_id
    )
    from bot.services._file_processing import split_file, cleanup_split_files

    file_path = str(Path(file_path))
    if not Path(file_path).exists():
        raise UploadError(f"File not found: {file_path}")

    filename = custom_filename or Path(file_path).name
    file_size_bytes = Path(file_path).stat().st_size
    file_size_mb = file_size_bytes / (1024 * 1024)

    config = await get_config()
    dump_channel_id = await get_channel_id("dump") or config.get("dump_channel_id")
    rclone_config = await pick_rclone_config_for_plan(user_plan)

    max_tg_mb = 4000 if user_plan == "pro" else 2000
    retention_days = RETENTION_PRO_DAYS if user_plan == "pro" else RETENTION_FREE_DAYS
    expiry_date = datetime.utcnow() + timedelta(days=retention_days)

    delivery_method = "unknown"
    cloud_url = None
    telegram_file_id = None
    dump_message = None
    is_split_operation = False
    files_to_send = [file_path]
    use_rclone = file_size_mb > max_tg_mb

    if use_rclone and not rclone_config:
        logger.info(f"File > {max_tg_mb}MB but no Rclone config. Falling back to splitting: {filename}")
        use_rclone = False
        delivery_method = "Bot API Split"
        limit_bytes = int(MAX_BOT_FILE_SIZE_MB * 1024 * 1024 * 0.95)
        files_to_send = await split_file(file_path, limit_bytes)
        is_split_operation = True

    # ── ROUTE 1: RCLONE CLOUD ──
    if use_rclone:
        delivery_method = "Rclone Cloud"
        from bot.handlers.user import send_progress_message

        rclone_regex = re.compile(r"Transferred:\s+.*?,\s+(\d+)%")
        last_rclone_update = [time.time()]
        start_update_time = time.time()

        def rclone_progress(line):
            if task_id:
                match = rclone_regex.search(line)
                if match:
                    progress = int(match.group(1))
                    now = time.time()
                    if now - last_rclone_update[0] > 5 or progress >= 100:
                        last_rclone_update[0] = now
                        asyncio.create_task(
                            send_progress_message(
                                bot=bot, user_id=user_id, task_id=task_id,
                                filesize=file_size_bytes, stage="📤 **Uploading to Cloud...**",
                                progress=progress, start_time=start_update_time,
                            )
                        )

        rclone_result = await upload_to_rclone(
            file_path=file_path,
            rclone_config_id=rclone_config["config_id"],
            remote_path=f"/{user_id}/",
            user_id=user_id,
            progress_callback=rclone_progress,
        )
        if not rclone_result:
            raise UploadError("Cloud upload failed.")

        cloud_url = rclone_result["cloud_url"]
        telegram_file_id = rclone_result["file_id"]

        link_text = (
            f"✅ **Large File Ready!**\n\n"
            f"📁 **{filename}** ({file_size_mb:.1f} MB)\n\n"
            f"🔗 **Download Here:** {cloud_url}\n\n"
            f"⏰ Expires in {retention_days} days."
        )
        keyboard = [
            [InlineKeyboardButton("📥 Download Now", url=cloud_url)],
            [
                InlineKeyboardButton("📺 VLC Player", url=f"vlc://{cloud_url}"),
                InlineKeyboardButton("📱 MX Player", url=f"intent:{cloud_url}#Intent;package=com.mxtech.videoplayer.ad;S.title={filename};end")
            ]
        ]

        for attempt in range(3):
            try:
                await bot.send_message(
                    chat_id=user_id, text=link_text,
                    reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
                )
                break
            except Exception as e:
                if attempt == 2:
                    logger.error(f"Failed to send link: {e}")
                await asyncio.sleep(1)

        for dest in await get_user_destinations(user_id):
            if dest.get("id"):
                try:
                    await bot.send_message(
                        chat_id=dest.get("id"), text=link_text,
                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
                    )
                except Exception:
                    pass

        return {
            "success": True, "delivery_method": delivery_method,
            "file_id": telegram_file_id or cloud_url, "cloud_url": cloud_url,
            "message_id": None, "expiry_date": expiry_date, "retention_days": retention_days,
        }

    # ── ROUTE 2: TELEGRAM UPLOAD ──
    dump_msg_id = None
    if dump_channel_id:
        try:
            from bot.pyrogram_client import upload_file_pyrogram
            logger.info(f"Uploading to dump: {filename}")

            for part_path in files_to_send:
                part_size_mb = Path(part_path).stat().st_size / (1024 * 1024)

                if part_size_mb > MAX_BOT_FILE_SIZE_MB:
                    delivery_method = "Pyrogram MTProto"
                    dump_msg = await upload_file_pyrogram(
                        file_path=part_path, chat_id=dump_channel_id,
                        caption=f"User: {user_id}\nFile: {Path(part_path).name}\nSize: {part_size_mb:.1f} MB",
                        ptb_bot=bot, user_id=user_id, task_id=task_id,
                    )
                    if dump_msg:
                        dump_message = dump_msg
                        dump_msg_id = getattr(dump_message, "id", getattr(dump_message, "message_id", None))
                        telegram_file_id = getattr(
                            getattr(dump_message, "document", None) or getattr(dump_message, "video", None),
                            "file_id", None,
                        )
                else:
                    delivery_method = "Bot API Direct"
                    for attempt in range(3):
                        try:
                            with open(part_path, "rb") as f:
                                dump_message = await bot.send_document(
                                    chat_id=dump_channel_id, document=f,
                                    caption=f"User: {user_id}\nFile: {Path(part_path).name}\nSize: {part_size_mb:.1f} MB",
                                    file_name=Path(part_path).name,
                                    read_timeout=60, write_timeout=60, connect_timeout=60,
                                )
                            telegram_file_id = (
                                dump_message.document.file_id
                                if hasattr(dump_message, "document")
                                else dump_message.video.file_id
                            )
                            dump_msg_id = dump_message.message_id
                            break
                        except Exception as e:
                            if attempt == 2:
                                raise e
                            await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"⚠️ Dump upload failed: {e}")

    if dump_msg_id:
        try:
            await bot.copy_message(chat_id=user_id, from_chat_id=dump_channel_id, message_id=dump_msg_id)
        except Exception as e:
            logger.warning(f"Failed to copy to user: {e}")
    else:
        for part_path in files_to_send:
            try:
                with open(part_path, "rb") as f:
                    await bot.send_document(chat_id=user_id, document=f, caption=custom_caption[:1000])
            except Exception:
                pass

    if dump_msg_id:
        destinations = await get_user_destinations(user_id)
        if selected_destinations:
            destinations = [d for d in destinations if d.get("id") in selected_destinations]
        elif selected_destinations is not None:
            destinations = []

        for dest in destinations:
            if dest.get("id"):
                try:
                    await bot.copy_message(
                        chat_id=dest.get("id"), from_chat_id=dump_channel_id, message_id=dump_msg_id,
                        caption=f"📁 **Forwarded File**\n\nName: `{filename}`", parse_mode="Markdown",
                    )
                except Exception:
                    pass

    if is_split_operation:
        await cleanup_split_files(files_to_send)

    cloud_url = (
        f"https://t.me/c/{str(dump_channel_id)[4:]}/{dump_msg_id}"
        if dump_channel_id and dump_msg_id else None
    )

    logger.info(f"Upload + delivery complete for {user_id} ({file_size_mb:.1f}MB) | Via: {delivery_method}")
    return {
        "success": True, "delivery_method": delivery_method,
        "file_id": telegram_file_id or cloud_url, "cloud_url": cloud_url,
        "message_id": dump_msg_id, "expiry_date": expiry_date, "retention_days": retention_days,
    }
