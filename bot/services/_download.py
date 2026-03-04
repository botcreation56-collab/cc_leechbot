"""
bot/services/_download.py — Download engine: yt-dlp (analyze) + aria2c + Mega.nz.

Split from bot/services.py (originally concatenated with 5 other modules).
Zero logic changes; mid-file re-import of `from typing import ...` removed (was artifact of concatenation).
"""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from config.constants import (
    ARIA2C_PATH, YTDLP_PATH, ARIA2C_CONNECTIONS, ARIA2C_SPLITS,
    DOWNLOAD_TIMEOUT, YTDLP_TIMEOUT, DOWNLOADS_DIR,
)

logger = logging.getLogger("filebot.services.download")


class DownloadError(Exception):
    """Download related errors."""
    pass


async def analyze_url_with_ytdlp(url: str) -> Optional[Dict[str, Any]]:
    """
    Analyze URL using yt-dlp to get direct download URL.
    Does NOT download — only analyzes available formats.

    Returns:
        Dict with {direct_url, filename, ext, filesize, duration, meta} or None if failed.
    """
    try:
        if "youtube.com" in url.lower() or "youtu.be" in url.lower():
            raise DownloadError("YouTube downloads are strictly disabled per policy.")

        cmd = [
            YTDLP_PATH,
            "--dump-json",
            "-f", "bestvideo+bestaudio/best",
            "--no-warnings",
            "--proxy", "",
            "--max-downloads", "1",
            "--",  # SECURITY: Stop option parsing before URL
            url,
        ]

        logger.info(f"🔍 Analyzing URL with yt-dlp: {url[:50]}...")

        result = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=YTDLP_TIMEOUT,
        )

        stdout, stderr = await result.communicate()

        if result.returncode != 0:
            error_msg = stderr.decode(errors="ignore").strip()
            logger.error(f"❌ yt-dlp analysis failed: {error_msg}")
            raise DownloadError(f"URL analysis failed: {error_msg[:100]}")

        info = json.loads(stdout.decode())

        direct_url = info.get("url") or ""
        if not direct_url and "requested_formats" in info:
            formats = info["requested_formats"]
            if formats and "url" in formats:
                direct_url = formats["url"]

        filename = info.get("title", "video")[:200]
        ext = info.get("ext", "mp4")
        filesize = info.get("filesize") or info.get("filesize_approx") or 0
        duration = info.get("duration", 0)

        result_data = {
            "direct_url": direct_url,
            "filename": f"{filename}.{ext}",
            "ext": ext,
            "filesize": int(filesize),
            "duration": int(duration),
            "meta": info,
        }

        logger.info(f"✅ URL analyzed: {result_data['filename']} ({result_data['filesize']} bytes)")
        return result_data

    except asyncio.TimeoutError:
        logger.error("❌ yt-dlp timeout")
        raise DownloadError("Analysis timeout (5 min)")
    except json.JSONDecodeError:
        logger.error("❌ yt-dlp invalid JSON response")
        raise DownloadError("Invalid response from analyzer")
    except Exception as e:
        logger.error(f"❌ yt-dlp error: {e}")
        raise DownloadError(str(e)[:100])


async def download_with_aria2c(
    url: str,
    filename: str,
    user_id: int,
    task_id: str,
    db: Optional[Any] = None,
    progress_callback=None,
) -> Optional[str]:
    """
    Download file using aria2c with multi-connection (10x speed).

    Returns:
        Local file path or None if failed.
    """
    try:
        user_dir = DOWNLOADS_DIR / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)

        from bot.utils import safe_path, check_disk_space
        dest_path = Path(safe_path(str(user_dir), filename))

        if not check_disk_space(str(DOWNLOADS_DIR), min_gb=1.0):
            raise DownloadError("Server disk full")

        logger.info(f"📥 Starting aria2c download: {filename}")

        cmd = [
            ARIA2C_PATH,
            "-x", str(ARIA2C_CONNECTIONS),
            "-s", str(ARIA2C_SPLITS),
            "-k", "1M",
            "--file-allocation=none",
            "--continue=true",
            "--allow-overwrite=true",
            "--timeout=300",
            f"--dir={user_dir}",
            f"--out={filename}",
            url,
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        last_progress_update = 0
        try:
            async for line_bytes in process.stdout:
                try:
                    # TOCTOU Mitigation: hard limit 10GB check dynamically during chunked downloads
                    if dest_path.exists() and dest_path.stat().st_size > 10 * 1024 * 1024 * 1024:
                        raise DownloadError("File exceeds maximum allowed system size limit (10GB)")

                    line = line_bytes.decode(errors="ignore").strip()
                    if "MiB" in line or "GiB" in line:
                        if "(" in line and "%)" in line:
                            try:
                                paren_split = line.split("(")
                                if len(paren_split) >= 2:
                                    percent_str = paren_split[1].split("%")[0].strip()
                                    progress = min(int(float(percent_str)), 100)
                                if db and (progress - last_progress_update >= 5 or progress == 100):
                                    await db.tasks.update_one(
                                        {"task_id": task_id},
                                        {"$set": {"progress": progress}},
                                        max_time_ms=5000,
                                    )
                                    last_progress_update = progress
                                    logger.info(f"📊 Download progress: {progress}%")
                                if progress_callback:
                                    await progress_callback(progress)
                            except (ValueError, IndexError):
                                pass
                except DownloadError:
                    raise
                except Exception as e:
                    logger.debug(f"Progress parse error: {e}")

            await asyncio.wait_for(process.wait(), timeout=DOWNLOAD_TIMEOUT)

            if process.returncode != 0:
                logger.error(f"❌ aria2c failed with code {process.returncode}")
                raise DownloadError("Download failed")

        except asyncio.TimeoutError:
            logger.error("❌ Download timeout")
            raise DownloadError("Download timeout (60 min)")
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                    logger.warning("🧟 Killed orphaned aria2c zombie process successfully.")
                except Exception as e:
                    logger.error(f"Failed to kill aria2c zombie: {e}")

        if not dest_path.exists():
            logger.error(f"❌ Downloaded file not found: {dest_path}")
            raise DownloadError("File not found after download")

        file_size = dest_path.stat().st_size
        logger.info(f"✅ Download complete: {filename} ({file_size} bytes)")
        return str(dest_path)

    except asyncio.TimeoutError:
        logger.error("❌ Download timeout")
        raise DownloadError("Download timeout (60 min)")
    except Exception as e:
        logger.error(f"❌ Download error: {e}")
        raise DownloadError(str(e)[:100])


async def download_from_mega(
    url: str,
    user_id: int,
    task_id: str,
    db: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """
    Download from Mega.nz using mega.py.

    P0 FIX: asyncio.to_thread is wrapped in asyncio.wait_for with a hard
    DOWNLOAD_TIMEOUT cap. Without this, a stalled Mega session occupies a
    thread permanently, exhausting the ThreadPoolExecutor.
    """
    user_dir = DOWNLOADS_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    downloaded_file = None

    logger.info(f"📥 Starting Mega download: {url}")

    # Pass credentials to subprocess if admin has them
    from bot.database import get_db
    mega_email = ""
    mega_password = ""
    if user_id in settings.ADMIN_IDS or user_id == int(str(settings.ADMIN_IDS).split(",")[0] if isinstance(settings.ADMIN_IDS, str) else settings.ADMIN_IDS[0]):
        try:
            db_inst = get_db()
            mega_conf = await db_inst.rclone_configs.find_one({"user_id": user_id, "service": "mega"})
            if mega_conf and "credentials" in mega_conf:
                # Basic parsing of rclone conf format for mega: user = ... \n pass = ...
                creds = mega_conf["credentials"]
                for line in creds.splitlines():
                    if line.strip().startswith("user ="):
                        mega_email = line.split("=", 1)[1].strip()
                    elif line.strip().startswith("pass ="):
                        mega_password = line.split("=", 1)[1].strip()
                        # Rclone obscures mega passwords, mega.py needs plaintext.
                        # Attempt to reveal it using rclone.
                        try:
                            # Using python's subprocess since this is a quick synchronous-like operation
                            # but we do it async so we don't block.
                            reveal_proc = await asyncio.create_subprocess_exec(
                                "rclone", "reveal", mega_password,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE
                            )
                            stdout, _ = await asyncio.wait_for(reveal_proc.communicate(), timeout=5.0)
                            if reveal_proc.returncode == 0:
                                mega_password = stdout.decode().strip()
                        except Exception as e:
                            logger.warning(f"Could not reveal mega password: {e}")
        except Exception as e:
            logger.warning(f"Could not load Mega credentials for admin: {e}")

    script = f'''
import sys
import os
try:
    from mega import Mega
    m = Mega()
    email = os.environ.get("MEGA_EMAIL", "").strip()
    password = os.environ.get("MEGA_PASSWORD", "").strip()
    
    if email and password:
        m_login = m.login(email, password)
    else:
        m_login = m.login()
        
    res = m_login.download_url("{url}", dest_path="{user_dir}")
    print(f"MEGA_FILE_PATH={{res}}")
except Exception as e:
    print(str(e), file=sys.stderr)
    raise
'''
    
    env = os.environ.copy()
    if mega_email and mega_password:
        env["MEGA_EMAIL"] = mega_email
        env["MEGA_PASSWORD"] = mega_password

    process = await asyncio.create_subprocess_exec(
        "python", "-c", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env
    )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DOWNLOAD_TIMEOUT)
        
        if process.returncode != 0:
            error_msg = stderr.decode('utf-8', errors='ignore').strip()
            logger.error(f"❌ Mega download error: {error_msg}")
            raise DownloadError(f"Mega download failed: {error_msg[:100]}")
            
        stdout_str = stdout.decode('utf-8', errors='ignore')
        downloaded_file = None
        for line in stdout_str.splitlines():
            if line.startswith("MEGA_FILE_PATH="):
                downloaded_file = line.split("=", 1)[1].strip()
                
    except asyncio.TimeoutError:
        logger.error("❌ Mega download timed out after %ss", DOWNLOAD_TIMEOUT)
        try:
            process.kill()
            await process.wait()
            logger.warning("🧟 Killed orphaned Mega worker process successfully.")
        except Exception as e:
            logger.error(f"Failed to kill Mega zombie: {e}")
            
        try:
            for f in user_dir.iterdir():
                if f.is_file() and f.stat().st_mtime > (asyncio.get_event_loop().time() - DOWNLOAD_TIMEOUT - 60):
                    f.unlink(missing_ok=True)
        except Exception:
            pass
        raise DownloadError(f"Mega download timed out after {DOWNLOAD_TIMEOUT}s")
    except Exception as e:
        logger.error(f"❌ Mega download error: {e}")
        try:
            process.kill()
            await process.wait()
        except:
            pass
        raise DownloadError(f"Mega download failed: {str(e)[:100]}")

    if not downloaded_file:
        raise DownloadError("Mega download returned no file.")

    try:
        from bot.utils import sanitize_filename
        original_path = Path(downloaded_file)
        display_filename = sanitize_filename(original_path.name)
        ext = original_path.suffix or ".bin"
        internal_filename = f"{uuid.uuid4()}{ext}"
        dest_path = user_dir / internal_filename
        original_path.rename(dest_path)
        file_size = dest_path.stat().st_size

        logger.info(f"✅ Mega Download complete: {display_filename} ({file_size} bytes)")

        if db:
            await db.tasks.update_one(
                {"task_id": task_id},
                {"$set": {"progress": 100}},
                max_time_ms=5000,
            )

        return {
            "file_path": str(dest_path),
            "filename": display_filename,
            "filesize": file_size,
            "duration": 0,
            "meta": {},
        }
    except Exception as e:
        logger.error(f"❌ Mega post-processing error: {e}")
        raise DownloadError(f"Mega download failed: {str(e)[:100]}")


async def download_from_url(
    url: str,
    user_id: int,
    task_id: str,
    db: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """
    Main download function: analyze + download.
    Includes retry logic (3 attempts) with partial-file cleanup on failure.
    Uses UUID for internal filename (S001).
    """
    last_error = None

    if "mega.nz" in url.lower():
        return await download_from_mega(url, user_id, task_id, db)

    analysis = None
    for attempt in range(3):
        try:
            analysis = await analyze_url_with_ytdlp(url)
            if analysis:
                break
        except Exception as e:
            last_error = e
            logger.warning(f"⚠️ Analysis attempt {attempt + 1}/3 failed: {e}")
            await asyncio.sleep(2 * (attempt + 1))

    if not analysis:
        raise DownloadError(f"URL analysis failed after 3 attempts: {last_error}")

    direct_url = analysis["direct_url"]

    from bot.utils import sanitize_filename, check_disk_space, safe_path
    raw_name = analysis.get("filename", "video.mp4")
    display_filename = sanitize_filename(raw_name)

    ext = analysis.get("ext", "mp4")
    internal_filename = f"{uuid.uuid4()}.{ext}"

    size = analysis.get("filesize", 0)
    MAX_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
    if size > MAX_SIZE:
        raise DownloadError(f"File too large ({size / (1024**3):.2f}GB). Max 2GB.")

    if not direct_url:
        raise DownloadError("No download URL found")

    file_path = None
    for attempt in range(3):
        potential_path = DOWNLOADS_DIR / str(user_id) / internal_filename
        try:
            file_path = await download_with_aria2c(
                direct_url, internal_filename, user_id, task_id, db
            )
            if file_path:
                break
        except Exception as e:
            last_error = e
            logger.warning(f"⚠️ Download attempt {attempt + 1}/3 failed: {e}")
            # P2 FIX: Remove partial file and aria2c control file before retrying
            for stale in [potential_path, potential_path.with_suffix(potential_path.suffix + ".aria2")]:
                try:
                    stale.unlink(missing_ok=True)
                except Exception:
                    pass
            await asyncio.sleep(2 * (attempt + 1))

    if not file_path:
        raise DownloadError(f"Download failed after 3 attempts: {last_error}")

    file_size = Path(file_path).stat().st_size if file_path else 0

    return {
        "file_path": file_path,
        "filename": display_filename,
        "filesize": file_size,
        "duration": analysis.get("duration", 0),
        "meta": analysis.get("meta", {}),
    }


async def cleanup_old_downloads(older_than_hours: int = 24):
    """Clean up temporary download files older than X hours (non-blocking)."""
    def _sync_cleanup():
        import time
        cutoff_time = time.time() - (older_than_hours * 3600)
        deleted_count = 0
        for user_dir in DOWNLOADS_DIR.iterdir():
            if not user_dir.is_dir():
                continue
            for file in user_dir.iterdir():
                try:
                    if file.stat().st_mtime < cutoff_time:
                        file.unlink()
                        deleted_count += 1
                except Exception as e:
                    logger.debug(f"Failed to delete {file}: {e}")
        return deleted_count

    try:
        deleted_count = await asyncio.to_thread(_sync_cleanup)
        if deleted_count > 0:
            logger.info(f"✅ Cleaned up {deleted_count} old download files")
        return deleted_count
    except Exception as e:
        logger.error(f"❌ Cleanup failed: {e}")
        return 0
