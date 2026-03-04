"""
bot/services/__init__.py — Backward-compatible re-export shim.

ARCHITECTURE NOTE:
  bot/services/ is now a package split into 6 domain-focused sub-modules.
  This file re-exports every public symbol that was previously importable
  from the flat bot/services.py module, so all existing callers need zero changes.
"""

# ── Download Engine ──────────────────────────────────────────────────────────
from bot.services._download import (
    DownloadError,
    analyze_url_with_ytdlp,
    download_with_aria2c,
    download_from_mega,
    download_from_url,
    cleanup_old_downloads,
)

# ── FFmpeg ───────────────────────────────────────────────────────────────────
from bot.services._ffmpeg import (
    FFmpegService,
)

# ── File Processing ───────────────────────────────────────────────────────────
from bot.services._file_processing import (
    ProcessingError,
    validate_file_size,
    get_upload_engine_limit,
    should_split_file,
    split_file,
    cleanup_split_files,
    get_file_info,
    cleanup_temp_files,
)

# ── Link Shortener / OTP ─────────────────────────────────────────────────────
from bot.services._link_shortener import (
    CloudLinkGenerator,
    LinkShortener,
    OTPService,
    _otp_storage,
)

# ── Queue Worker ─────────────────────────────────────────────────────────────
from bot.services._queue_worker import (
    QueueWorker,
    run_broadcast_worker,
)

# ── Cloud Upload (Rclone, Terabox, Storage, Upload Engine) ───────────────────
from bot.services._cloud_upload import (
    RcloneError,
    upload_to_rclone,
    generate_rclone_link,
    get_available_rclone,
    test_rclone_connection,
    list_rclone_files,
    StorageChannelManager,
    create_or_update_storage_message,
    TeraboxError,
    upload_to_terabox,
    get_terabox_config,
    test_terabox_connection,
    get_terabox_storage_info,
    UploadError,
    upload_and_send_file,
    MAX_BOT_FILE_SIZE_MB,
)

__all__ = [
    # Download
    "DownloadError", "analyze_url_with_ytdlp", "download_with_aria2c",
    "download_from_mega", "download_from_url", "cleanup_old_downloads",
    # FFmpeg
    "FFmpegService",
    # File Processing
    "ProcessingError", "validate_file_size", "get_upload_engine_limit",
    "should_split_file", "split_file", "cleanup_split_files",
    "get_file_info", "cleanup_temp_files",
    # Links / OTP
    "CloudLinkGenerator", "LinkShortener", "OTPService", "_otp_storage",
    # Queue
    "QueueWorker", "run_broadcast_worker",
    # Cloud Upload
    "RcloneError", "upload_to_rclone", "generate_rclone_link",
    "get_available_rclone", "test_rclone_connection", "list_rclone_files",
    "StorageChannelManager", "create_or_update_storage_message",
    "TeraboxError", "upload_to_terabox", "get_terabox_config",
    "test_terabox_connection", "get_terabox_storage_info",
    "UploadError", "upload_and_send_file", "MAX_BOT_FILE_SIZE_MB",
]
