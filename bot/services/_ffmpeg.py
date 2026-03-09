"""
bot/services/_ffmpeg.py — FFmpegService: media probing + processing with concurrency control.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("filebot.services.ffmpeg")


class FFmpegService:
    """Handles media probing and processing using FFmpeg/FFprobe."""

    @staticmethod
    async def probe_file(file_path: str) -> Dict[str, Any]:
        """
        Probe file to get dynamic track information.
        Returns dict with 'audio' and 'subtitle' lists.
        """
        try:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-show_entries", "stream=index,codec_name,codec_type,tags",
                "-of", "json",
                "--",  # SECURITY: Stop option parsing before path
                os.path.abspath(file_path),
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.error(f"FFprobe failed: {stderr.decode()}")
                return {"audio": [], "subtitle": []}

            data = json.loads(stdout.decode())
            streams = data.get("streams", [])

            audio_tracks = []
            sub_tracks = []

            for s in streams:
                codec_type = s.get("codec_type")
                tags = s.get("tags", {})
                
                # Language detection priority: tags.language -> tags.LANGUAGE -> tags.handler_name (3-char)
                lang = tags.get("language") or tags.get("LANGUAGE") or tags.get("handler_name", "und")
                if len(lang) > 3: # handler_name can be long, but langs are usually 3
                    lang = lang[:3].lower()

                title = tags.get("title") or lang or s.get("codec_name", "Unknown")

                track_info = {
                    "index": s.get("index"),
                    "codec": s.get("codec_name"),
                    "language": lang,
                    "title": title,
                }

                if codec_type == "audio":
                    audio_tracks.append(track_info)
                elif codec_type == "subtitle":
                    sub_tracks.append(track_info)

            return {"audio": audio_tracks, "subtitle": sub_tracks}

        except Exception as e:
            logger.error(f"Probe error: {e}", exc_info=True)
            return {"audio": [], "subtitle": []}

    # Concurrency Control — lazy-init so semaphore is created inside the running event loop.
    _semaphore: Optional[asyncio.Semaphore] = None
    _semaphore_limit: int = int(os.getenv("PARALLEL_LIMIT", 5))

    @classmethod
    def _get_semaphore(cls) -> asyncio.Semaphore:
        """Return semaphore, creating it lazily on first access."""
        if cls._semaphore is None:
            cls._semaphore = asyncio.Semaphore(cls._semaphore_limit)
        return cls._semaphore

    @classmethod
    def set_parallel_limit(cls, limit: int) -> None:
        """Update semaphore limit at runtime (admin config change)."""
        cls._semaphore_limit = limit
        cls._semaphore = asyncio.Semaphore(limit)

    @staticmethod
    async def _run_command(
        cmd: List[str], timeout: int = 3600, progress_callback=None
    ) -> bool:
        """
        Run subprocess with timeout and safety checks.
        If progress_callback is provided, stream stderr to it.
        """
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
                        timeout=timeout,
                    )
                else:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=timeout
                    )

            except asyncio.TimeoutError:
                logger.error(f"❌ Subprocess timed out after {timeout}s! Killing process.")
                try:
                    process.kill()
                    # P1 FIX: Always await wait() after kill() to reap the zombie process
                    await process.wait()
                except Exception as kill_err:
                    logger.error(f"Failed to kill process: {kill_err}")
                raise TimeoutError("Processing timed out")

            if process.returncode != 0:
                if not progress_callback:
                    logger.error(f"Subprocess failed: {stderr.decode('utf-8', errors='ignore')}")
                else:
                    logger.error(f"Subprocess failed (return code {process.returncode})")
                return False

            return True

        except Exception as e:
            logger.error(f"Confirmation failed: {e}")
            if process:
                try:
                    process.kill()
                    # P1 FIX: Always reap zombie process after kill
                    await process.wait()
                except Exception as cleanup_err:
                    logger.error(f"Failed to terminate process: {cleanup_err}")
            return False

    @classmethod
    async def process_media(
        cls,
        input_path: str,
        output_path: str,
        selected_audio_indexes: List[int],
        selected_sub_indexes: List[int],
        injected_audio: List[str] = None,
        injected_subs: List[str] = None,
        new_filename: str = None,
        custom_metadata: Dict[str, str] = None,
        progress_callback=None,
    ) -> bool:
        """
        Process media file: map selected streams and inject new ones.
        Protected by semaphore to prevent server overload.
        """
        from bot.utils import check_disk_space

        if not check_disk_space(os.path.dirname(output_path), min_gb=0.5):
            logger.error("❌ Not enough disk space to process media")
            return False

        async with cls._get_semaphore():
            try:
                cmd = ["ffmpeg", "-y", "-i", os.path.abspath(input_path)]

                input_count = 1
                maps = ["-map", "0:v"]
                for idx in selected_audio_indexes:
                    maps.extend(["-map", f"0:{idx}"])
                for idx in selected_sub_indexes:
                    maps.extend(["-map", f"0:{idx}"])

                if injected_audio:
                    for a_path in injected_audio:
                        cmd.extend(["-i", os.path.abspath(a_path)])
                        maps.extend(["-map", f"{input_count}:a"])
                        input_count += 1

                if injected_subs:
                    for s_path in injected_subs:
                        cmd.extend(["-i", os.path.abspath(s_path)])
                        maps.extend(["-map", f"{input_count}:s"])
                        input_count += 1

                cmd.extend(maps)
                
                # Apply custom metadata tags natively to the container
                if custom_metadata:
                    for k, v in custom_metadata.items():
                        cmd.extend(["-metadata", f"{k}={v}"])

                cmd.extend(["-c", "copy"])
                cmd.extend(["--", os.path.abspath(output_path)])

                logger.info(f"Running FFmpeg: {cmd}")
                return await cls._run_command(cmd, progress_callback=progress_callback)

            except Exception as e:
                logger.error(f"FFmpeg error: {e}", exc_info=True)
                return False
