"""
bot/services/_ffmpeg.py — FFmpegService: media probing + processing with concurrency control.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("filebot.services.ffmpeg")


class FFmpegService:
    """Handles media probing and processing using FFmpeg/FFprobe."""

    @classmethod
    async def probe_file(cls, file_path: str) -> Dict[str, Any]:
        """
        Probe file to get dynamic track information.
        Returns dict with 'audio' and 'subtitle' lists.
        """
        try:
            # Validate input path
            if not file_path or not isinstance(file_path, str):
                logger.warning("Invalid file_path provided to probe_file")
                return {"audio": [], "subtitle": []}

            # Security: Check for path traversal
            if (
                ".." in file_path
                or file_path.startswith("/etc")
                or file_path.startswith("/root")
            ):
                logger.warning(f"Blocked suspicious path: {file_path}")
                return {"audio": [], "subtitle": []}

            # Handle URLs vs local paths
            target_path = file_path
            if not file_path.startswith(("http://", "https://")):
                target_path = os.path.abspath(file_path)

                # Check if file exists
                if not os.path.exists(target_path):
                    logger.warning(f"File does not exist: {target_path}")
                    return {"audio": [], "subtitle": []}

            cmd = [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_name,codec_type:stream_tags=language,handler_name,title:format_tags=:format=duration",
                "-of",
                "json",
                "--",  # SECURITY: Stop option parsing before path
                target_path,
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                err_msg = stderr.decode(errors="ignore").strip()
                logger.error(f"FFprobe failed for {file_path}: {err_msg[:100]}")
                return {"audio": [], "subtitle": []}

            try:
                data = json.loads(stdout.decode())
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON from ffprobe: {e}")
                return {"audio": [], "subtitle": []}

            streams = data.get("streams", [])

            audio_tracks = []
            sub_tracks = []

            for s in streams:
                codec_type = s.get("codec_type")
                tags = s.get("tags", {})

                # Language detection priority: tags.language -> tags.LANGUAGE -> tags.handler_name
                lang = tags.get("language") or tags.get("LANGUAGE") or ""

                # If lang is missing or 'und', try to extract from handler_name
                if not lang or lang.lower() == "und":
                    hn = tags.get("handler_name", "").strip()
                    if hn:
                        hn_low = hn.lower()
                        # 1. handler_name is exactly a 3-letter ISO code we KNOW about
                        if len(hn_low) == 3 and hn_low in cls.ISO_639_2_MAP:
                            lang = hn_low
                        # 2. handler_name is exactly a 2-letter ISO code we KNOW about
                        elif len(hn_low) == 2 and hn_low in cls.ISO_639_2_MAP:
                            lang = hn_low
                        # 3. handler_name CONTAINS a known language name (e.g. "Tamil Audio")
                        else:
                            for code, name in cls.ISO_639_2_MAP.items():
                                if name.lower() in hn_low:
                                    lang = code
                                    break

                # If lang is still missing/und, try to extract from title
                if not lang or lang.lower() == "und":
                    title_low = tags.get("title", "").lower()
                    if title_low:
                        for code, name in cls.ISO_639_2_MAP.items():
                            if name.lower() in title_low:
                                lang = code
                                break

                lang = lang or "und"
                if len(lang) > 3:
                    lang = lang[:3].lower()

                # Title logic: Prefer user-friendly title, then lang name, then codec
                title = tags.get("title")
                lang_name = cls.get_language_name(lang)

                if not title or title.lower() in ["und", "undetermined", lang.lower()]:
                    if lang_name != "Unknown":
                        title = lang_name
                    else:
                        # Fallback: show codec name capitalised (e.g. 'AAC', 'DTS', 'EAC3')
                        title = (s.get("codec_name") or "Unknown").upper()

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

            return {
                "audio": audio_tracks,
                "subtitle": sub_tracks,
                "duration": float(data.get("format", {}).get("duration", 0)),
                "format": data.get("format", {}),
            }

        except Exception as e:
            logger.error(f"Probe error for {file_path}: {e}", exc_info=True)
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
                logger.error(
                    f"❌ Subprocess timed out after {timeout}s! Killing process."
                )
                try:
                    process.kill()
                    # P1 FIX: Always await wait() after kill() to reap the zombie process
                    await process.wait()
                except Exception as kill_err:
                    logger.error(f"Failed to kill process: {kill_err}")
                raise TimeoutError("Processing timed out")

            if process.returncode != 0:
                if not progress_callback:
                    logger.error(
                        f"Subprocess failed: {stderr.decode('utf-8', errors='ignore')}"
                    )
                else:
                    logger.error(
                        f"Subprocess failed (return code {process.returncode})"
                    )
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

    ISO_639_2_MAP = {
        # 3-letter codes
        "tam": "Tamil",
        "tel": "Telugu",
        "hin": "Hindi",
        "kan": "Kannada",
        "mal": "Malayalam",
        "eng": "English",
        "ben": "Bengali",
        "guj": "Gujarati",
        "mar": "Marathi",
        "pan": "Punjabi",
        "urd": "Urdu",
        "ori": "Oriya",
        "asm": "Assamese",
        "san": "Sanskrit",
        "jpn": "Japanese",
        "kor": "Korean",
        "chi": "Chinese",
        "zho": "Chinese",
        "fra": "French",
        "fre": "French",
        "ger": "German",
        "deu": "German",
        "spa": "Spanish",
        "rus": "Russian",
        "por": "Portuguese",
        "ita": "Italian",
        "ara": "Arabic",
        "tur": "Turkish",
        "vie": "Vietnamese",
        "tha": "Thai",
        "ind": "Indonesian",
        "pol": "Polish",
        "und": "Unknown",
        # 2-letter codes
        "ta": "Tamil",
        "te": "Telugu",
        "hi": "Hindi",
        "kn": "Kannada",
        "ml": "Malayalam",
        "en": "English",
        "bn": "Bengali",
        "gu": "Gujarati",
        "mr": "Marathi",
        "pa": "Punjabi",
        "ur": "Urdu",
        "or": "Oriya",
        "as": "Assamese",
        "sa": "Sanskrit",
        "ja": "Japanese",
        "ko": "Korean",
        "zh": "Chinese",
        "fr": "French",
        "de": "German",
        "es": "Spanish",
        "ru": "Russian",
        "pt": "Portuguese",
        "it": "Italian",
        "ar": "Arabic",
        "tr": "Turkish",
        "vi": "Vietnamese",
        "th": "Thai",
        "id": "Indonesian",
        "pl": "Polish",
    }

    @classmethod
    def get_language_name(cls, code: str) -> str:
        """Map ISO code to full name."""
        if not code:
            return "Unknown"
        code = code.lower()
        return cls.ISO_639_2_MAP.get(code, code.capitalize())

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
        all_audio_tracks: List[Dict] = None,
        all_sub_tracks: List[Dict] = None,
    ) -> bool:
        """
        Process media file: map selected streams and apply dynamic track labeling.
        Returns True on success, False on failure.
        Cleans up partial output file on failure.
        """
        from bot.utils import check_disk_space
        from bot.utils.error_handler import validate_filename, validate_metadata_value

        # Validate input path
        if not input_path or not isinstance(input_path, str):
            logger.error("Invalid input_path provided")
            return False

        # Security check for input path
        if (
            ".." in input_path
            or input_path.startswith("/etc")
            or input_path.startswith("/root")
        ):
            logger.warning(f"Blocked suspicious input path: {input_path}")
            return False

        # Check input file exists
        abs_input = os.path.abspath(input_path)
        if not os.path.exists(abs_input):
            logger.error(f"Input file does not exist: {abs_input}")
            return False

        if not check_disk_space(os.path.dirname(output_path), min_gb=0.5):
            logger.error("❌ Not enough disk space to process media")
            return False

        # Validate and sanitize metadata values
        if custom_metadata:
            custom_metadata = {
                k: validate_metadata_value(v)
                for k, v in custom_metadata.items()
                if v and isinstance(v, str)
            }

        async with cls._get_semaphore():
            process = None
            try:
                cmd = ["ffmpeg", "-y", "-i", abs_input]

                input_count = 1
                maps = ["-map", "0:v"]

                # Metadata list for streams
                stream_metadata = []

                # Audio Mapping
                for i, idx in enumerate(selected_audio_indexes or []):
                    if not isinstance(idx, int):
                        continue
                    maps.extend(["-map", f"0:{idx}"])
                    user_val = validate_metadata_value(
                        (custom_metadata or {}).get("audio", "Default")
                    )

                    # Find language for this track
                    lang_name = "Unknown"
                    if all_audio_tracks:
                        track = next(
                            (t for t in all_audio_tracks if t.get("index") == idx), None
                        )
                        if track:
                            lang_name = cls.get_language_name(
                                track.get("language", "und")
                            )

                    # Apply label with sanitized values
                    safe_title = f"{user_val} | {lang_name}"[:200]  # Limit title length
                    stream_metadata.extend(
                        ["-metadata:s:a:" + str(i), f"title={safe_title}"]
                    )

                # Subtitle Mapping
                for i, idx in enumerate(selected_sub_indexes or []):
                    if not isinstance(idx, int):
                        continue
                    maps.extend(["-map", f"0:{idx}"])
                    user_val = validate_metadata_value(
                        (custom_metadata or {}).get("subs", "Default")
                    )

                    lang_name = "Unknown"
                    if all_sub_tracks:
                        track = next(
                            (t for t in all_sub_tracks if t.get("index") == idx), None
                        )
                        if track:
                            lang_name = cls.get_language_name(
                                track.get("language", "und")
                            )

                    safe_title = f"{user_val} | {lang_name}"[:200]
                    stream_metadata.extend(
                        ["-metadata:s:s:" + str(i), f"title={safe_title}"]
                    )

                # Injected Audio - validate paths
                if injected_audio:
                    for a_path in injected_audio:
                        if os.path.exists(a_path):
                            cmd.extend(["-i", os.path.abspath(a_path)])
                            maps.extend(["-map", f"{input_count}:a"])
                            input_count += 1
                        else:
                            logger.warning(
                                f"Injected audio file not found, skipping: {a_path}"
                            )

                # Injected Subs - validate paths
                if injected_subs:
                    for s_path in injected_subs:
                        if os.path.exists(s_path):
                            cmd.extend(["-i", os.path.abspath(s_path)])
                            maps.extend(["-map", f"{input_count}:s"])
                            input_count += 1
                        else:
                            logger.warning(
                                f"Injected subtitle file not found, skipping: {s_path}"
                            )

                cmd.extend(maps)

                # Global Metadata - sanitize values
                if custom_metadata:
                    # Video title
                    if "video" in custom_metadata:
                        safe_title = validate_metadata_value(custom_metadata["video"])[
                            :200
                        ]
                        cmd.extend(["-metadata", f"title={safe_title}"])

                    # Author/Artist
                    if "author" in custom_metadata:
                        safe_author = validate_metadata_value(
                            custom_metadata["author"]
                        )[:200]
                        cmd.extend(["-metadata", f"artist={safe_author}"])
                        cmd.extend(["-metadata", f"author={safe_author}"])

                # Apply stream labels
                cmd.extend(stream_metadata)

                cmd.extend(["-c", "copy"])
                abs_output = os.path.abspath(output_path)
                cmd.extend(["--", abs_output])

                logger.info(
                    f"Running FFmpeg: {' '.join(cmd[:6])}... [truncated for security]"
                )

                result = await cls._run_command(
                    cmd, progress_callback=progress_callback
                )

                if not result:
                    # Clean up partial output file
                    if os.path.exists(abs_output):
                        try:
                            os.remove(abs_output)
                            logger.info(f"Cleaned up partial output: {abs_output}")
                        except Exception as cleanup_err:
                            logger.warning(
                                f"Failed to cleanup partial output: {cleanup_err}"
                            )
                    return False

                return True

            except Exception as e:
                logger.error(f"FFmpeg error: {e}", exc_info=True)

                # Clean up partial output file
                try:
                    abs_output = os.path.abspath(output_path)
                    if os.path.exists(abs_output):
                        os.remove(abs_output)
                        logger.info(
                            f"Cleaned up partial output after error: {abs_output}"
                        )
                except Exception as cleanup_err:
                    logger.warning(f"Failed to cleanup partial output: {cleanup_err}")

                return False

