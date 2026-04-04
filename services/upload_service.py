"""
services/upload_service.py — Upload pipeline + stream link token generation.

Responsibilities:
  - Upload processed files to Telegram (via Pyrogram userbot or PTB bot)
  - Upload to Rclone cloud remotes as overflow / per-plan destination
  - Generate cryptographically secure one-time stream links
  - Generate thumbnails via FFmpeg

The service is stateless. All repositories are injected at construction.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional
import os

from core.exceptions import RcloneUploadError, TelegramUploadError
from core.security import TokenGenerator, sanitize_filename
from infrastructure.database.repositories import CloudFileRepository, OneTimeKeyRepository, RcloneConfigRepository
from services.media_service import _check_disk_space

logger = logging.getLogger("filebot.services.upload")

# Speed limits by plan (bytes/second). 0 == unlimited.
_PLAN_SPEED_LIMITS: Dict[str, int] = {
    "free":    153_600,   # ~150 KB/s → ~1.2 Mbps
    "premium": 512_000,   # ~500 KB/s ~4 Mbps
    "pro":     0,          # unlimited
}

_TWO_GB = 2 * 1024 ** 3


# ============================================================
# ThumbnailGenerator
# ============================================================

async def generate_thumbnail(file_path: str, output_path: str, at_second: float = 5.0) -> Optional[str]:
    """Extract a single frame from a video file and save as JPEG thumbnail.

    Returns the output path on success, None on failure (non-fatal).
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(at_second),
        "-i", file_path,
        "-frames:v", "1",
        "-vf", "scale=320:-1",
        output_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)
        if proc.returncode == 0 and Path(output_path).exists():
            return output_path
    except Exception as exc:
        logger.warning("Thumbnail generation failed: %s", exc)
    return None


# ============================================================
# UploadService
# ============================================================

class UploadService:
    """Orchestrates file upload across Telegram and Rclone backends."""

    def __init__(
        self,
        cloud_repo: CloudFileRepository,
        otk_repo: OneTimeKeyRepository,
        rclone_repo: RcloneConfigRepository,
        stream_base_url: str,
    ) -> None:
        self._cloud = cloud_repo
        self._otk = otk_repo
        self._rclone = rclone_repo
        self._base_url = stream_base_url.rstrip("/")

    # ------------------------------------------------------------------ #
    # Telegram Upload                                                       #
    # ------------------------------------------------------------------ #

    async def upload_to_telegram(
        self,
        file_path: str,
        chat_id: int,
        caption: str,
        *,
        user_id: int,
        task_id: str,
        ptb_bot: Any,
        pyrogram_bot: Optional[Any] = None,
        pyrogram_user: Optional[Any] = None,
        thumb_path: Optional[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> str:
        """Upload a file to Telegram and return the Telegram file_id.

        Strategy:
          - > 2 GB AND pyrogram_user available → pyrogram userbot
          - <= 2 GB AND pyrogram_user available → pyrogram userbot (preferred for speed)
          - <= 2 GB, no pyrogram → PTB bot (falls back to chunked splitting if needed)

        Raises TelegramUploadError on failure.
        """
        path = Path(file_path)
        if not path.exists():
            raise TelegramUploadError(file_path, "File not found on disk")

        file_size = path.stat().st_size
        ext = path.suffix.lower()
        is_video = ext in {".mp4", ".mkv", ".avi", ".mov", ".webm"}

        # --- select client ---
        if file_size > _TWO_GB:
            if not pyrogram_user:
                raise TelegramUploadError(
                    path.name, "File > 2 GB requires a Pyrogram userbot session (USERBOT_SESSION not set)"
                )
            client = pyrogram_user
        else:
            client = pyrogram_user or pyrogram_bot

        if client is not None:
            return await self._pyrogram_upload(
                client=client,
                file_path=file_path,
                chat_id=chat_id,
                caption=caption,
                is_video=is_video,
                thumb_path=thumb_path,
                task_id=task_id,
                user_id=user_id,
                ptb_bot=ptb_bot,
                progress_callback=progress_callback,
            )

        # Fallback: PTB bot (limited to ~50 MB without local server)
        return await self._ptb_upload(ptb_bot, file_path, chat_id, caption, is_video, thumb_path)

    async def _pyrogram_upload(
        self,
        client: Any,
        file_path: str,
        chat_id: int,
        caption: str,
        is_video: bool,
        thumb_path: Optional[str],
        task_id: str,
        user_id: int,
        ptb_bot: Any,
        progress_callback: Optional[Callable[[int], None]],
    ) -> str:
        import time

        last_update = [time.time()]

        async def _progress(current: int, total: int) -> None:
            now = time.time()
            if now - last_update[0] >= 5.0 or current >= total:
                last_update[0] = now
                pct = int(current / total * 100)
                if progress_callback:
                    await progress_callback(pct)

        try:
            logger.info("📤 Pyrogram upload: %s", Path(file_path).name)
            if is_video:
                msg = await client.send_video(
                    chat_id=chat_id,
                    video=file_path,
                    caption=caption,
                    thumb=thumb_path,
                    progress=_progress,
                )
            else:
                msg = await client.send_document(
                    chat_id=chat_id,
                    document=file_path,
                    caption=caption,
                    thumb=thumb_path,
                    progress=_progress,
                )
            logger.info("✅ Pyrogram upload complete: %s", Path(file_path).name)
            doc = getattr(msg, "document", None) or getattr(msg, "video", None)
            return doc.file_id if doc else ""
        except Exception as exc:
            raise TelegramUploadError(Path(file_path).name, str(exc)) from exc

    async def _ptb_upload(
        self,
        bot: Any,
        file_path: str,
        chat_id: int,
        caption: str,
        is_video: bool,
        thumb_path: Optional[str],
    ) -> str:
        logger.info("📤 PTB upload: %s", Path(file_path).name)
        try:
            with open(file_path, "rb") as f:
                if is_video:
                    msg = await bot.send_video(chat_id, video=f, caption=caption, thumbnail=thumb_path)
                    doc = msg.video
                else:
                    msg = await bot.send_document(chat_id, document=f, caption=caption, thumbnail=thumb_path)
                    doc = msg.document
            return doc.file_id if doc else ""
        except Exception as exc:
            raise TelegramUploadError(Path(file_path).name, str(exc)) from exc

    # ------------------------------------------------------------------ #
    # Rclone Upload                                                         #
    # ------------------------------------------------------------------ #

    _rclone_semaphore: Optional[asyncio.Semaphore] = None

    @classmethod
    def _get_rclone_semaphore(cls) -> asyncio.Semaphore:
        if cls._rclone_semaphore is None:
            from config.settings import get_settings
            limit = int(os.getenv("PARALLEL_LIMIT", 5))
            cls._rclone_semaphore = asyncio.Semaphore(limit)
        return cls._rclone_semaphore

    async def upload_to_rclone(self, file_path: str, plan: str, display_name: str) -> str:
        """Upload via Rclone to the least-loaded config for the given plan.

        Returns the public cloud URL on success. Raises RcloneUploadError on failure.
        """
        async with self._get_rclone_semaphore():
            config = await self._rclone.pick_for_plan(plan)
            if not config:
                raise RcloneUploadError("none", file_path, "No active Rclone configuration found")

            remote = config.get("remote_name", "")
            remote_path = config.get("remote_path", "/uploads")
            safe_name = sanitize_filename(display_name)

            import tempfile
            import os
            from pathlib import Path
            from bot.utils import decrypt_credentials

            config_path = None
            try:
                # Bug Fix: Render/Docker containers lose /tmp or ~/.config files on restart.
                # So, we write the securely DB-stored credentials to a temp file dynamically
                # just for the duration of this rclone subprocess.
                enc_creds = config.get("credentials", "")
                
                credentials = ""
                if isinstance(enc_creds, dict) and "config" in enc_creds:
                    # It's an unencrypted dict (migration fallback if encryption failed)
                    credentials = enc_creds["config"]
                elif isinstance(enc_creds, str) and enc_creds.strip():
                    # It's an encrypted string
                    try:
                        decrypted = decrypt_credentials(enc_creds)
                        credentials = decrypted.get("config", "")
                    except Exception as e:
                        logger.error(f"Failed to decrypt rclone credentials: {e}")
                else:
                    credentials = str(enc_creds)

                fd, config_path = tempfile.mkstemp(suffix=".conf", prefix="rclone_")
                with os.fdopen(fd, 'w') as f:
                    f.write(credentials)

                cmd = [
                    "rclone", "copyto",
                    file_path,
                    f"{remote}:{remote_path}/{safe_name}",
                    "--progress",
                    "--config", config_path,
                ]

                logger.info("☁️ Rclone upload [PARALLEL SAFE] → %s:%s/%s", remote, remote_path, safe_name)
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)
                if proc.returncode != 0:
                    err = stderr.decode(errors="ignore").strip()
                    raise RcloneUploadError(remote, file_path, err[:300])
            except asyncio.TimeoutError:
                raise RcloneUploadError(remote, file_path, "Upload timed out after 1 hour")
            except RcloneUploadError:
                raise
            except Exception as exc:
                raise RcloneUploadError(remote, file_path, str(exc)) from exc
            finally:
                config_path_file = Path(config_path) if config_path else None
                if config_path_file and config_path_file.exists():
                    try:
                        config_path_file.unlink()
                    except OSError:
                        pass

            # Build public URL from remote config
            cdn_base = config.get("cdn_base_url", "")
            cloud_url = f"{cdn_base}/{safe_name}" if cdn_base else ""
            logger.info("✅ Rclone upload complete: %s", safe_name)
            return cloud_url

    # ------------------------------------------------------------------ #
    # Persistent storage record + secure link generation                   #
    # ------------------------------------------------------------------ #

    async def save_and_generate_link(
        self,
        *,
        file_id: str,
        user_id: int,
        filename: str,
        file_size: int,
        cloud_url: str = "",
        visibility: str = "public",
        plan: str = "free",
        token_ttl_hours: int = 24,
    ) -> Dict[str, str]:
        """Persist cloud file record and generate a secure stream link.

        Returns: {stream_link, token, expires_at_iso}.
        """
        from database import get_config
        config = await get_config() or {}
        if plan == "free":
            expiry_days = config.get("retention_free_days", 7)
        else:
            expiry_days = config.get("retention_pro_days", 28)
            
        expires_file = datetime.utcnow() + timedelta(days=expiry_days)

        # Save cloud file metadata
        await self._cloud.save({
            "file_id": file_id,
            "user_id": user_id,
            "filename": sanitize_filename(filename),
            "file_size": file_size,
            "cloud_url": cloud_url,
            "visibility": visibility,
            "expires_at": expires_file,
        })

        # Generate one-time auth token
        token = TokenGenerator.url_safe(32)
        token_expires = datetime.utcnow() + timedelta(hours=token_ttl_hours)
        await self._otk.create(user_id, token, token_expires)

        stream_link = f"{self._base_url}/api/verify_link/{file_id}/{token}"
        logger.info("✅ Stream link generated for file_id=%s, user=%d", file_id, user_id)

        return {
            "stream_link": stream_link,
            "token": token,
            "expires_at": token_expires.isoformat(),
        }

    # ------------------------------------------------------------------ #
    # Speed-limit helper for streaming                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_speed_limit(plan: str) -> int:
        """Return integer bytes/second speed limit for plan. 0 == unlimited."""
        return _PLAN_SPEED_LIMITS.get(plan, _PLAN_SPEED_LIMITS["free"])
