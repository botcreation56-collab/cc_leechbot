"""
bot/services/_file_processing.py — File size validation, splitting, and cleanup utilities.

Split from bot/services.py (originally concatenated with 5 other modules).
Mid-file re-import of `from typing import ...` and `from datetime import ...` removed.
"""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.constants import (
    MAX_FILE_SIZE_FREE, MAX_FILE_SIZE_PRO,
    MAX_UPLOAD_SIZE_2GB, MAX_UPLOAD_SIZE_4GB,
    STREAM_CHUNK_SIZE, TEMP_DIR,
)

logger = logging.getLogger("filebot.services.file_processing")


class ProcessingError(Exception):
    """File processing errors."""
    pass


def validate_file_size(file_size: int, user_plan: str) -> tuple[bool, str]:
    """
    Validate file size against plan limits.

    Returns:
        Tuple (is_valid, error_message).
    """
    try:
        max_size = MAX_FILE_SIZE_PRO if user_plan == "pro" else MAX_FILE_SIZE_FREE
        if file_size > max_size:
            max_gb = max_size / (1024 ** 3)
            return False, f"❌ File exceeds {max_gb:.1f}GB limit for {user_plan} plan"
        return True, ""
    except Exception as e:
        logger.error(f"❌ File validation error: {e}")
        return False, "Validation error"


def get_upload_engine_limit(engine_size: str = "4gb") -> int:
    """Return current upload engine limit in bytes."""
    if engine_size == "2gb":
        return MAX_UPLOAD_SIZE_2GB
    return MAX_UPLOAD_SIZE_4GB


def should_split_file(file_size: int, engine_limit: int) -> bool:
    """Determine if file should be split for uploading."""
    return file_size > engine_limit


async def split_file(
    file_path: str, engine_limit: int, output_dir: Optional[str] = None
) -> List[str]:
    """
    Split large file into chunks for uploading.

    Returns:
        List of part file paths.
    """
    try:
        import aiofiles

        file_path = Path(file_path)
        if not file_path.exists():
            raise ProcessingError(f"File not found: {file_path}")

        file_size = file_path.stat().st_size

        if not output_dir:
            output_dir = Path(TEMP_DIR) / "splits" / file_path.stem
        else:
            output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"📂 Splitting {file_path.name} ({file_size} bytes)")

        part_files = []
        part_num = 1
        bytes_read = 0

        async with aiofiles.open(file_path, "rb") as src:
            while bytes_read < file_size:
                bytes_to_read = min(engine_limit, file_size - bytes_read)
                part_name = f"{file_path.stem}_part{part_num}{file_path.suffix}"
                part_path = output_dir / part_name
                logger.info(f"📝 Creating part {part_num}: {part_name} ({bytes_to_read} bytes)")

                async with aiofiles.open(part_path, "wb") as part_file:
                    while bytes_to_read > 0:
                        read_size = min(STREAM_CHUNK_SIZE, bytes_to_read)
                        data = await src.read(read_size)
                        if not data:
                            break
                        await part_file.write(data)
                        bytes_to_read -= len(data)
                        bytes_read += len(data)

                part_files.append(str(part_path))
                part_num += 1

        logger.info(f"✅ Split complete: {len(part_files)} parts")
        return part_files

    except ProcessingError:
        raise
    except Exception as e:
        logger.error(f"❌ File split error: {e}")
        raise ProcessingError(str(e)[:100])


async def cleanup_split_files(part_files: List[str]) -> int:
    """Clean up split part files. Returns number of files deleted."""
    try:
        deleted = 0
        for part_file in part_files:
            try:
                Path(part_file).unlink()
                deleted += 1
                logger.debug(f"Deleted: {part_file}")
            except Exception as e:
                logger.warning(f"Failed to delete {part_file}: {e}")
        logger.info(f"✅ Cleaned up {deleted} part files")
        return deleted
    except Exception as e:
        logger.error(f"❌ Cleanup error: {e}")
        return 0


async def get_file_info(file_path: str) -> Dict[str, Any]:
    """Get file information."""
    try:
        path = Path(file_path)
        if not path.exists():
            raise ProcessingError(f"File not found: {file_path}")
        stat = path.stat()
        return {
            "name": path.name,
            "size": stat.st_size,
            "extension": path.suffix,
            "created": datetime.fromtimestamp(stat.st_ctime),
            "modified": datetime.fromtimestamp(stat.st_mtime),
            "is_large": stat.st_size > MAX_UPLOAD_SIZE_4GB,
        }
    except ProcessingError:
        raise
    except Exception as e:
        logger.error(f"❌ Get file info error: {e}")
        raise ProcessingError(str(e)[:100])


async def cleanup_temp_files(older_than_hours: int = 12) -> int:
    """Clean up temporary files older than X hours."""
    try:
        cutoff_time = time.time() - (older_than_hours * 3600)
        deleted_count = 0
        temp_path = Path(TEMP_DIR)
        for item in temp_path.rglob("*"):
            try:
                if item.is_file() and item.stat().st_mtime < cutoff_time:
                    item.unlink()
                    deleted_count += 1
            except Exception as e:
                logger.debug(f"Failed to delete {item}: {e}")
        if deleted_count > 0:
            logger.info(f"✅ Cleaned up {deleted_count} temp files")
        return deleted_count
    except Exception as e:
        logger.error(f"❌ Temp cleanup error: {e}")
        return 0
