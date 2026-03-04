import os
from pathlib import Path

# ============================================================
# PATHS
# All paths are Path objects — consistent type, no mid-file reassignment.
# Directories are created here at import time so the rest of the app
# can safely assume they exist (harmless if already present).
# ============================================================
PROJECT_ROOT = Path(__file__).parent.parent
TEMP_DIR     = Path(os.getenv("TEMP_DIR", "/tmp/filebot"))
LOG_DIR      = PROJECT_ROOT / "logs"
DOWNLOADS_DIR = TEMP_DIR / "downloads"

# Ensure required runtime directories exist
TEMP_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ============================================
# FILE SIZE LIMITS (in bytes)
# ============================================
# Plan-based maximums (what user can upload/process)
MAX_FILE_SIZE_FREE = int(os.getenv("MAX_FILE_SIZE_FREE", 5 * 1024 * 1024 * 1024))  # 5GB
MAX_FILE_SIZE_PRO = int(os.getenv("MAX_FILE_SIZE_PRO", 10 * 1024 * 1024 * 1024))  # 10GB

# Upload engine limit (admin-configurable: 2GB or 4GB)
# This is the max size per Telegram upload (when splitting needed)
MAX_UPLOAD_SIZE_2GB = 2 * 1024 * 1024 * 1024  # 2GB
MAX_UPLOAD_SIZE_4GB = 4 * 1024 * 1024 * 1024  # 4GB
MAX_UPLOAD_SIZE_DEFAULT = int(os.getenv("MAX_UPLOAD_SIZE", MAX_UPLOAD_SIZE_4GB))

# Streaming chunk size (8MB for better performance)
STREAM_CHUNK_SIZE = 8 * 1024 * 1024

# ============================================
# RETENTION POLICY (days)
# ============================================
RETENTION_FREE_DAYS = int(os.getenv("RETENTION_FREE_DAYS", 7))
RETENTION_PRO_DAYS = int(os.getenv("RETENTION_PRO_DAYS", 28))

# ============================================
# USER QUOTAS
# ============================================
USER_DAILY_LIMIT_FREE = 5
USER_DAILY_LIMIT_PRO = None  # Unlimited

PARALLEL_SLOTS_FREE = 1
PARALLEL_SLOTS_PRO = 5

STORAGE_LIMIT_FREE = MAX_FILE_SIZE_FREE * 4  # 20GB
STORAGE_LIMIT_PRO = MAX_FILE_SIZE_PRO * 20  # 200GB

# ============================================
# DOWNLOAD ENGINE
# ============================================
ARIA2C_PATH = os.getenv("ARIA2C_PATH", "aria2c")
YTDLP_PATH = os.getenv("YTDLP_PATH", "yt-dlp")

# aria2c parameters for 10x speed
ARIA2C_CONNECTIONS = 16  # Max connections
ARIA2C_SPLITS = 16  # Split into 16 parts
ARIA2C_PIECE_LENGTH = 1024 * 1024  # 1MB piece

# ============================================
# DOWNLOAD TIMEOUTS (seconds)
# ============================================
DOWNLOAD_TIMEOUT = 3600  # 1 hour
YTDLP_TIMEOUT = 300  # 5 minutes
MONGODB_TIMEOUT = 10  # 10 seconds

# ============================================
# QUEUE SETTINGS
# ============================================
QUEUE_PROCESS_LIMIT = 3  # Process max 3 tasks simultaneously
QUEUE_CHECK_INTERVAL = 5  # Check queue every 5 seconds
STUCK_TASK_TIMEOUT = 3600  # Tasks older than 1 hour are "stuck"

# ============================================
# CLEANUP SETTINGS
# ============================================
CLEANUP_INTERVAL = 6 * 3600  # Run cleanup every 6 hours
IDLE_FILE_CLEANUP_HOURS = 24  # Delete if not in queue/uploaded after 24h

# ============================================
# BROADCAST SETTINGS
# ============================================
BROADCAST_RATE_LIMIT = 0.05  # 50ms between messages (20 msgs/sec max)
BROADCAST_BATCH_SIZE = 100  # Send in batches of 100

# ============================================
# PAGINATION
# ============================================
USERS_PER_PAGE = 10
FILES_PER_PAGE = 15
LOGS_PER_PAGE = 20

# ============================================
# SECURITY
# ============================================
ONE_TIME_KEY_LENGTH = 16
ONE_TIME_KEY_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*"
ONE_TIME_KEY_EXPIRY_HOURS = 1  # Expire after 1 hour
JWT_EXPIRY_HOURS = 24

# ============================================
# RCLONE SERVICES
# ============================================
RCLONE_SERVICES = {
    "gdrive": {
        "name": "Google Drive",
        "guide_url": "https://rclone.org/drive/",
        "requires_fields": ["client_id", "client_secret"]
    },
    "onedrive": {
        "name": "OneDrive",
        "guide_url": "https://rclone.org/onedrive/",
        "requires_fields": ["client_id", "client_secret"]
    },
    "dropbox": {
        "name": "Dropbox",
        "guide_url": "https://rclone.org/dropbox/",
        "requires_fields": ["app_key", "app_secret"]
    },
    "mega": {
        "name": "Mega",
        "guide_url": "https://rclone.org/mega/",
        "requires_fields": ["username", "password"]
    },
    "terabox": {
        "name": "Terabox",
        "guide_url": "https://terabox.com/",
        "requires_fields": ["api_key", "bearer_token"],
        "admin_only": True
    },
    "custom": {
        "name": "Custom Remote",
        "guide_url": "https://rclone.org/",
        "requires_fields": ["config"]
    }
}

# ============================================
# PLAYER REDIRECT SCHEMES
# ============================================
PLAYER_SCHEMES = {
    "vlc": "vlc://",
    "mx": "intent:{url}#Intent;package=com.mxtech.videoplayer.pro;end",
    "playit": "playit://"
}

# ============================================
# LOGGING
# ============================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", str(LOG_DIR / "bot.log"))

# ============================================
# ERROR MESSAGES
# ============================================
ERROR_MESSAGES = {
    "file_too_large": "❌ File size exceeds limit for your plan.\n\nFree: 5GB | Pro: 10GB",
    "download_failed": "❌ Download failed. Please check the URL and try again.",
    "processing_failed": "❌ File processing failed. Please contact support.",
    "upload_failed": "❌ Upload failed. Please try again in 5 minutes.",
    "unauthorized": "⛔ Unauthorized access.",
    "banned": "🚫 Your account has been banned.",
    "quota_exceeded": "⚠️ Daily limit reached. Please try tomorrow.",
    "storage_full": "⚠️ Storage full. Delete some files or upgrade your plan.",
    "invalid_url": "❌ Invalid URL. Please check and try again.",
    "database_error": "❌ Database error. Please try again.",
    "timeout": "⏱️ Operation timed out. Please try again.",
}

# ============================================
# SUCCESS MESSAGES
# ============================================
SUCCESS_MESSAGES = {
    "file_added_to_queue": "✅ File added to queue! Position: {position}",
    "file_processing": "🔄 Processing your file... {progress}%",
    "file_completed": "✅ File processed successfully!",
    "file_deleted": "✅ File deleted from storage.",
    "settings_updated": "✅ Settings updated successfully!",
}

# ============================================
# STATUS ICONS
# ============================================
STATUS_ICONS = {
    "queued": "⏳",
    "downloading": "📥",
    "processing": "⚙️",
    "uploading": "📤",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "⛔"
}

# ============================================
# VALIDATION
# ============================================
ALLOWED_VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", 
    ".webm", ".m4v", ".3gp", ".m3u8", ".ts", ".mpg", ".mpeg"
}

ALLOWED_AUDIO_EXTENSIONS = {
    ".mp3", ".aac", ".flac", ".wav", ".m4a", ".opus",
    ".wma", ".alac", ".ogg", ".aiff", ".ape"
}

BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".ps1", ".dll", ".so", ".sh"
}

BANNED_KEYWORDS = {
    "magnet:", "torrent:", "bitcoin", "gambling"
}
