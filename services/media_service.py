"""
services/media_service.py — Download engine + FFmpeg processing.

Responsibilities:
  - Analyse URLs via yt-dlp
  - Download via aria2c (multi-connection, retry logic)
  - Mega.nz download support
  - FFmpeg probe + remux / audio-subtitle injection
  - Temporary file cleanup

Raises domain exceptions from core.exceptions instead of bare exceptions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config.constants import (
    ARIA2C_CONNECTIONS,
    ARIA2C_PATH,
    ARIA2C_SPLITS,
    DOWNLOAD_TIMEOUT,
    DOWNLOADS_DIR,
    YTDLP_PATH,
    YTDLP_TIMEOUT,
)
from core.exceptions import DownloadError, FFmpegError, UnsupportedURLError
from core.security import sanitize_filename, safe_path

logger = logging.getLogger("filebot.services.media")

# ---------------------------------------------------------------------------
# Semaphore for concurrent FFmpeg processes (prevent resource saturation)
# ---------------------------------------------------------------------------
_FFMPEG_SEM = asyncio.Semaphore(3)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_disk_space(path: str, min_gb: float = 1.0) -> bool:
    try:
        _, _, free = shutil.disk_usage(path)
        free_gb = free / 1024**3
        if free_gb < min_gb:
            logger.critical(
                "❌ LOW DISK SPACE: %.2f GB free (min %.1f GB)", free_gb, min_gb
            )
            return False
        return True
    except Exception as exc:
        logger.error("Disk check failed: %s", exc)
        return True  # fail-open — don't block service on check failure


# ---------------------------------------------------------------------------
# DownloadService
# ---------------------------------------------------------------------------


class DownloadService:
    """Orchestrates URL analysis and binary download via aria2c / mega.py."""

    _BLOCKED_DOMAINS = frozenset({"youtube.com", "youtu.be"})

    def __init__(self, downloads_dir: Optional[Path] = None) -> None:
        self._dl_dir = downloads_dir or DOWNLOADS_DIR

    # ------------------------------------------------------------------ #
    # Public API                                                            #
    # ------------------------------------------------------------------ #

    async def analyse(self, url: str) -> Dict[str, Any]:
        """Use yt-dlp to retrieve metadata without downloading.

        Returns a dict: {direct_url, filename, ext, filesize, duration, meta}.
        Raises DownloadError or UnsupportedURLError on failure.
        """
        netloc = url.lower()
        if any(domain in netloc for domain in self._BLOCKED_DOMAINS):
            raise UnsupportedURLError(
                url, "YouTube downloads are strictly disabled per policy"
            )

        cmd = [
            YTDLP_PATH,
            "--dump-json",
            "-f",
            "bestvideo+bestaudio/best",
            "--no-warnings",
            "--",  # stop option parsing before URL — SECURITY CRITICAL
            url,
        ]

        logger.info("🔍 Analysing URL: %s…", url[:60])

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=YTDLP_TIMEOUT,
            )
            stdout, stderr = await proc.communicate()
        except asyncio.TimeoutError:
            raise DownloadError(f"URL analysis timed out after {YTDLP_TIMEOUT}s")
        except Exception as exc:
            raise DownloadError(f"yt-dlp subprocess error: {exc}") from exc

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore").strip()
            raise DownloadError(f"yt-dlp failed: {err[:200]}")

        try:
            info = json.loads(stdout.decode())
        except json.JSONDecodeError as exc:
            raise DownloadError("yt-dlp returned invalid JSON") from exc

        direct_url = info.get("url") or ""
        if not direct_url and "requested_formats" in info:
            fmts = info["requested_formats"]
            if fmts and "url" in fmts:
                direct_url = fmts["url"]

        title = sanitize_filename(info.get("title", "video")[:200])
        ext = info.get("ext", "mp4")
        filesize = int(info.get("filesize") or info.get("filesize_approx") or 0)

        return {
            "direct_url": direct_url,
            "filename": f"{title}.{ext}",
            "ext": ext,
            "filesize": filesize,
            "duration": int(info.get("duration") or 0),
            "meta": info,
        }

    async def download(
        self,
        url: str,
        user_id: int,
        task_id: str,
        progress_callback: Optional[Callable[[int], None]] = None,
        max_size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Analyse URL and download via aria2c with retries.

        Returns: {file_path, filename (display), filesize, duration, meta}.
        Raises DownloadError on unrecoverable errors.
        """
        if "mega.nz" in url.lower():
            return await self._download_mega(url, user_id, task_id)

        last_error: Optional[Exception] = None
        analysis: Optional[Dict[str, Any]] = None

        # Retry analysis up to 3 times with exponential backoff
        for attempt in range(3):
            try:
                analysis = await self.analyse(url)
                break
            except Exception as exc:
                last_error = exc
                logger.warning("⚠️ Analysis attempt %d/3 failed: %s", attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(2**attempt)

        if not analysis:
            raise DownloadError(f"URL analysis failed after 3 attempts: {last_error}")

        if max_size_bytes and analysis.get("filesize", 0) > max_size_bytes:
            raise DownloadError(
                f"File too large: {analysis['filesize'] / (1024**3):.2f}GB exceeds allowed maximum"
            )

        direct_url = analysis.get("direct_url")
        if not direct_url:
            raise DownloadError("yt-dlp returned no direct download URL")

        display_name = analysis["filename"]
        ext = analysis.get("ext", "mp4")
        internal_filename = f"{uuid.uuid4()}.{ext}"

        file_path: Optional[str] = None
        for attempt in range(3):
            try:
                file_path = await self._aria2c(
                    direct_url, internal_filename, user_id, task_id, progress_callback
                )
                if file_path:
                    break
            except Exception as exc:
                last_error = exc
                logger.warning("⚠️ Download attempt %d/3 failed: %s", attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(2**attempt)

        if not file_path:
            raise DownloadError(f"Download failed after 3 attempts: {last_error}")

        file_size = Path(file_path).stat().st_size
        return {
            "file_path": file_path,
            "filename": display_name,
            "filesize": file_size,
            "duration": analysis.get("duration", 0),
            "meta": analysis.get("meta", {}),
        }

    # ------------------------------------------------------------------ #
    # Private helpers                                                       #
    # ------------------------------------------------------------------ #

    async def _aria2c(
        self,
        url: str,
        filename: str,
        user_id: int,
        task_id: str,
        progress_callback: Optional[Callable[[int], None]],
    ) -> str:
        user_dir = self._dl_dir / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)

        dest = Path(safe_path(str(user_dir), filename))

        if not _check_disk_space(str(self._dl_dir), min_gb=1.0):
            raise DownloadError("Server disk space critically low")

        cmd = [
            ARIA2C_PATH,
            "-x",
            str(ARIA2C_CONNECTIONS),
            "-s",
            str(ARIA2C_SPLITS),
            "-k",
            "1M",
            "--file-allocation=none",
            "--continue=true",
            "--allow-overwrite=true",
            "--timeout=300",
            f"--dir={user_dir}",
            f"--out={filename}",
            url,
        ]

        logger.info("📥 aria2c download: %s", filename)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        last_reported = 0
        async for line_bytes in proc.stdout:
            line = line_bytes.decode(errors="ignore").strip()
            if "(" in line and "%)" in line:
                try:
                    pct_str = line.split("(")[1].split("%")[0].strip()
                    pct = min(int(float(pct_str)), 100)
                    if pct - last_reported >= 5 or pct == 100:
                        last_reported = pct
                        if progress_callback:
                            await progress_callback(pct)
                except (ValueError, IndexError):
                    pass

        await asyncio.wait_for(proc.wait(), timeout=DOWNLOAD_TIMEOUT)

        if proc.returncode != 0:
            raise DownloadError(f"aria2c exited with code {proc.returncode}")
        if not dest.exists():
            raise DownloadError("File not found after aria2c reported success")

        logger.info("✅ aria2c complete: %s (%d bytes)", filename, dest.stat().st_size)
        return str(dest)

    async def _download_mega(
        self, url: str, user_id: int, task_id: str
    ) -> Dict[str, Any]:
        """Download from Mega.nz using mega.py (executes in thread pool)."""
        user_dir = self._dl_dir / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)

        def _sync_dl():
            import mega  # optional dependency

            m = mega.Mega().login()
            return m.download_url(url, dest_path=str(user_dir))

        logger.info("📥 Mega.nz download: %s…", url[:60])
        try:
            downloaded = await asyncio.to_thread(_sync_dl)
        except Exception as exc:
            raise DownloadError(f"Mega.nz download failed: {exc}") from exc

        if not downloaded:
            raise DownloadError("Mega.nz download returned no file path")

        original = Path(downloaded)
        display_name = sanitize_filename(original.name)
        ext = original.suffix or ".bin"
        internal = user_dir / f"{uuid.uuid4()}{ext}"
        original.rename(internal)

        return {
            "file_path": str(internal),
            "filename": display_name,
            "filesize": internal.stat().st_size,
            "duration": 0,
            "meta": {},
        }

    @staticmethod
    async def cleanup_old(older_than_hours: int = 24) -> int:
        """Delete temp files older than N hours from user download directories."""
        import time

        def _sync():
            cutoff = time.time() - older_than_hours * 3600
            deleted = 0
            for user_dir in DOWNLOADS_DIR.iterdir():
                if not user_dir.is_dir():
                    continue
                for f in user_dir.iterdir():
                    try:
                        if f.stat().st_mtime < cutoff:
                            f.unlink()
                            deleted += 1
                    except Exception:
                        pass
            return deleted

        count = await asyncio.to_thread(_sync)
        if count:
            logger.info("🗑️ Cleaned up %d old temp files", count)
        return count


# ---------------------------------------------------------------------------
# FFmpeg Service
# ---------------------------------------------------------------------------


class MediaProcessingService:
    """Wraps FFmpeg for probing and remuxing operations."""

    @staticmethod
    async def probe(file_path: str) -> Dict[str, Any]:
        """Run ffprobe and return structured audio + subtitle track info."""
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            file_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                return {"audio": [], "subtitle": []}
            data = json.loads(stdout.decode())
        except Exception as exc:
            logger.warning("ffprobe failed: %s", exc)
            return {"audio": [], "subtitle": []}

        audio, subtitle = [], []
        for s in data.get("streams", []):
            codec_type = s.get("codec_type")
            idx = s.get("index", 0)
            lang = s.get("tags", {}).get("language", "und")
            title = s.get("tags", {}).get("title", "")
            codec = s.get("codec_name", "")
            entry = {"index": idx, "language": lang, "title": title, "codec": codec}
            if codec_type == "audio":
                audio.append(entry)
            elif codec_type == "subtitle":
                subtitle.append(entry)

        return {"audio": audio, "subtitle": subtitle}

    @staticmethod
    async def process(
        input_path: str,
        output_path: str,
        selected_audio: Optional[List[int]] = None,
        selected_subs: Optional[List[int]] = None,
        injected_audio: Optional[List[str]] = None,
        injected_subs: Optional[List[str]] = None,
        metadata: Optional[Dict[str, str]] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> str:
        """Remux / copy streams with optional track selection and injection.

        Returns the output_path on success. Raises FFmpegError on failure.
        """
        async with _FFMPEG_SEM:
            probe = await MediaProcessingService.probe(input_path)
            available_audio = probe.get("audio", [])
            available_subs = probe.get("subtitle", [])

            valid_audio_indices = {s["index"] for s in available_audio}
            valid_sub_indices = {s["index"] for s in available_subs}

            safe_audio = []
            if selected_audio is not None:
                for idx in selected_audio:
                    if idx in valid_audio_indices:
                        safe_audio.append(idx)
                    else:
                        logger.warning(
                            "FFmpeg: Audio stream index %d not found, skipping", idx
                        )
                selected_audio = safe_audio if safe_audio else None

            safe_subs = []
            if selected_subs is not None:
                for idx in selected_subs:
                    if idx in valid_sub_indices:
                        safe_subs.append(idx)
                    else:
                        logger.warning(
                            "FFmpeg: Subtitle stream index %d not found, skipping", idx
                        )
                selected_subs = safe_subs if safe_subs else None

            if not _check_disk_space(str(Path(output_path).parent), min_gb=2.0):
                raise FFmpegError("ffmpeg", 1, "Insufficient disk space for processing")

            cmd = ["ffmpeg", "-y", "-i", input_path]

            # Inject extra audio sources
            for src in injected_audio or []:
                cmd += ["-i", src]
            # Inject extra subtitle sources
            for src in injected_subs or []:
                cmd += ["-i", src]

            # Map selected streams
            if selected_audio is not None:
                for idx in selected_audio:
                    cmd += ["-map", f"0:{idx}"]
            else:
                cmd += ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?"]

            if selected_subs is not None:
                for idx in selected_subs:
                    cmd += ["-map", f"0:{idx}"]

            # Map injected streams (they are inputs 1, 2, 3…)
            for i, _ in enumerate(injected_audio or [], start=1):
                cmd += ["-map", f"{i}:a"]
            for j, _ in enumerate(
                injected_subs or [], start=len(injected_audio or []) + 1
            ):
                cmd += ["-map", f"{j}:s"]

            # Copy all streams — no re-encode
            cmd += ["-c", "copy"]

            # Metadata injection
            for key, val in (metadata or {}).items():
                cmd += ["-metadata", f"{key}={val}"]

            cmd += ["-progress", "pipe:1", output_path]
            logger.info(
                "⚙️ FFmpeg: %s → %s", Path(input_path).name, Path(output_path).name
            )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Probe duration for progress calculation
            probe = await MediaProcessingService.probe(input_path)
            duration_s: float = 1.0  # fallback

            async for line_bytes in proc.stdout:
                line = line_bytes.decode(errors="ignore").strip()
                if line.startswith("out_time_ms="):
                    try:
                        elapsed_ms = int(line.split("=")[1])
                        if duration_s > 0 and progress_callback:
                            pct = min(
                                int(elapsed_ms / 1_000_000 / duration_s * 100), 99
                            )
                            await progress_callback(pct)
                    except (ValueError, IndexError):
                        pass

            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)
            if proc.returncode != 0:
                err = stderr.decode(errors="ignore").strip()
                raise FFmpegError(" ".join(cmd[:4]), proc.returncode, err)

            if progress_callback:
                await progress_callback(100)
            logger.info("✅ FFmpeg complete: %s", Path(output_path).name)
            return output_path
