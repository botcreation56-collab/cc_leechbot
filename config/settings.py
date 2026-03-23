import os
from typing import List, Optional
from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings
from pydantic import Field, SecretStr


class Settings(BaseSettings):
    """Main application settings - environment-only fields.

    Only BOT_TOKEN, ADMIN_IDS, and MONGODB_URI are truly required from env.
    All other runtime config (channels, limits, flags, etc.) should be
    read from Mongo via get_config().
    """

    _is_frozen: bool = False
    _domain_cache: Optional[str] = None

    def freeze(self):
        """Freeze the settings object to prevent further mutation."""
        self._is_frozen = True

    def __setattr__(self, name, value):
        if getattr(self, "_is_frozen", False) and name not in ["_is_frozen"]:
            raise TypeError(f"Settings are frozen. Cannot mutate '{name}'.")
        super().__setattr__(name, value)

    # ============================================
    # TELEGRAM BOT (REQUIRED FROM ENV)
    # ============================================
    BOT_TOKEN: SecretStr = Field(
        default=SecretStr(""), description="Telegram bot token from BotFather"
    )
    ADMIN_IDS: str = Field(
        default="123456789",
        description="Admin user IDs (comma-separated)",
    )
    BOT_USERNAME: str = Field(
        default="filebot",
        description="Bot username",
    )
    BOT_LINK: str = Field(
        default="",
        description="Bot deep link (e.g. https://t.me/MyBot)",
    )

    # ============================================
    # PYROGRAM / USERBOT (FOR LARGE UPLOADS > 2GB)
    # ============================================
    API_ID: Optional[int] = Field(
        default=None,
        description="Telegram API ID from my.telegram.org",
    )
    API_HASH: Optional[str] = Field(
        default=None,
        description="Telegram API Hash from my.telegram.org",
    )
    USERBOT_SESSION: Optional[str] = Field(
        default=None,
        description="Pyrogram Session String for premium large file uploads",
    )

    # ============================================
    # MONGODB (REQUIRED FROM ENV)
    # ============================================
    MONGODB_URI: str = Field(
        default="mongodb://localhost:27017",
        description="MongoDB connection URI",
    )
    MONGODB_DB: str = Field(
        default="filebot_production",
        description="Database name",
    )

    # ============================================
    # CHANNELS (OPTIONAL AT STARTUP – OVERRIDDEN BY MONGO CONFIG)
    # ============================================
    DUMP_CHANNEL_ID: Optional[int] = Field(
        default=None,
        description="Dump channel ID (negative, optional at startup)",
    )
    STORAGE_CHANNEL_ID: Optional[int] = Field(
        default=None,
        description="Storage channel ID (negative, optional at startup)",
    )
    LOG_CHANNEL_ID: Optional[int] = Field(
        default=None,
        description="Log channel ID (optional at startup)",
    )
    FORCE_SUB_CHANNELS: str = Field(
        default="",
        description="Force subscription channels (comma-separated)",
    )

    # ============================================
    # FILE SIZE LIMITS (DEFAULTS, CAN BE OVERRIDDEN BY MONGO)
    # ============================================
    MAX_UPLOAD_SIZE: int = Field(
        default=4294967296,
        description="Max upload size (4GB)",
    )
    MAX_FILE_SIZE_FREE: int = Field(
        default=5368709120,
        description="Max file size for free users (5GB)",
    )
    MAX_FILE_SIZE_PRO: int = Field(
        default=10737418240,
        description="Max file size for pro users (10GB)",
    )

    # ============================================
    # RETENTION (DEFAULTS, CAN BE OVERRIDDEN BY MONGO)
    # ============================================
    RETENTION_FREE_DAYS: int = Field(
        default=7,
        description="Free user retention days",
    )
    RETENTION_PRO_DAYS: int = Field(
        default=28,
        description="Pro user retention days",
    )

    # ============================================
    # WEBHOOK
    # ============================================
    WEBHOOK_URL: str = Field(
        default="",
        description="Webhook URL for watch pages",
    )
    WEBHOOK_SECRET: str = Field(
        default="",
        description="Secret token for Telegram webhook validation",
    )

    # ============================================
    # ENCRYPTION & SECURITY (OPTIONAL - generate if not set)
    # ============================================
    ENCRYPTION_KEY: str = Field(
        default="",
        description="Fernet encryption key (auto-generated if empty)",
    )
    JWT_SECRET: str = Field(
        default="",
        description="JWT secret for web auth (auto-generated if empty)",
    )

    # ============================================
    # DOWNLOAD ENGINE
    # ============================================
    ARIA2C_PATH: str = Field(
        default="aria2c",
        description="aria2c binary path",
    )
    YTDLP_PATH: str = Field(
        default="yt-dlp",
        description="yt-dlp binary path",
    )

    # ============================================
    # TEMPORARY FILES
    # ============================================
    TEMP_DIR: str = Field(
        default="/tmp/filebot",
        description="Temporary directory",
    )
    LOG_DIR: str = Field(
        default="logs",
        description="Log directory",
    )

    # ============================================
    # LOGGING
    # ============================================
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Logging level",
    )
    LOG_FILE: str = Field(
        default="logs/bot.log",
        description="Log file path",
    )

    # ============================================
    # WEB ADMIN
    # ============================================
    WEB_HOST: str = Field(
        default="0.0.0.0",
        description="Web server host",
    )
    WEB_PORT: int = Field(
        default=8000,
        description="Web server port",
    )

    # ============================================
    # API RATE LIMITS
    # ============================================
    RATE_LIMIT_REQUESTS: int = Field(
        default=60,
        description="Rate limit requests",
    )
    RATE_LIMIT_WINDOW: int = Field(
        default=60,
        description="Rate limit window (seconds)",
    )

    # ============================================
    # FEATURE FLAGS (DEFAULTS, CAN BE OVERRIDDEN BY MONGO)
    # ============================================
    ENABLE_RCLONE: bool = Field(
        default=True,
        description="Enable rclone support",
    )
    ENABLE_TERABOX: bool = Field(
        default=True,
        description="Enable terabox support",
    )

    # ============================================
    # ENVIRONMENT
    # ============================================
    ENVIRONMENT: str = Field(
        default="development",
        description="Environment (development or production)",
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


# ============================================
# GLOBAL SETTINGS INSTANCE
# ============================================

_settings: Optional[Settings] = None


def get_bot_token() -> str:
    """Return the bot token safely."""
    settings = get_settings()
    return settings.BOT_TOKEN.get_secret_value()


def get_domain() -> str:
    """Extract domain from WEBHOOK_URL for generating links."""
    settings = get_settings()
    webhook_url = settings.WEBHOOK_URL.rstrip("/")

    # Remove protocol
    if webhook_url.startswith("https://"):
        domain = webhook_url[8:]
    elif webhook_url.startswith("http://"):
        domain = webhook_url[7:]
    else:
        domain = webhook_url

    # Remove path if any
    if "/" in domain:
        domain = domain.split("/")[0]

    return domain


def get_settings() -> Settings:
    """Get or create global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()

        # Check for required secrets in production
        environment = os.getenv("ENVIRONMENT", "").lower()
        is_production = environment == "production"

        if not _settings.WEBHOOK_SECRET:
            if is_production:
                raise ValueError(
                    "WEBHOOK_SECRET must be set in production. "
                    'Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
                )
            import secrets

            _settings.WEBHOOK_SECRET = (
                secrets.token_urlsafe(32).replace("_", "").replace("-", "")
            )
            print(f"\n{'=' * 60}")
            print(f"⚠️ WEBHOOK_SECRET auto-generated for current session (dev only).")
            print(f"{'=' * 60}\n")

        if is_production:
            if not _settings.ENCRYPTION_KEY:
                print(f"\n{'=' * 60}")
                print(f"⚠️ WARNING: ENCRYPTION_KEY not set. Auto-generating...")
                print(f"{'=' * 60}\n")
                try:
                    from cryptography.fernet import Fernet

                    _settings.ENCRYPTION_KEY = Fernet.generate_key().decode()
                    print(
                        f"Generated ENCRYPTION_KEY (will reset on restart): {_settings.ENCRYPTION_KEY}"
                    )
                except Exception as e:
                    raise RuntimeError(f"Failed to generate ENCRYPTION_KEY: {e}")

            if not _settings.JWT_SECRET:
                print(f"\n{'=' * 60}")
                print(f"⚠️ WARNING: JWT_SECRET not set. Auto-generating...")
                print(f"{'=' * 60}\n")
                import secrets as _secrets

                _settings.JWT_SECRET = _secrets.token_urlsafe(32)
                print(
                    f"Generated JWT_SECRET (will reset on restart): {_settings.JWT_SECRET}"
                )

        # Auto-generate only in development mode with explicit warnings
        if not _settings.ENCRYPTION_KEY or _settings.ENCRYPTION_KEY == "":
            if is_production:
                print(f"\n{'=' * 60}")
                print(
                    f"⚠️ WARNING: ENCRYPTION_KEY not set in production. Using fallback."
                )
                print(f"   User sessions may reset on restart.")
                print(f"{'=' * 60}\n")
            try:
                from cryptography.fernet import Fernet

                _settings.ENCRYPTION_KEY = Fernet.generate_key().decode()
                _warn_generated("ENCRYPTION_KEY", _settings.ENCRYPTION_KEY)
            except Exception as e:
                print(f"!!! Failed to generate ENCRYPTION_KEY: {e} !!!")

        if not _settings.JWT_SECRET or _settings.JWT_SECRET == "":
            if is_production:
                print(f"\n{'=' * 60}")
                print(f"⚠️ WARNING: JWT_SECRET not set in production. Using fallback.")
                print(f"   Web sessions may reset on restart.")
                print(f"{'=' * 60}\n")
            import secrets as _secrets

            _settings.JWT_SECRET = _secrets.token_urlsafe(32)
            _warn_generated("JWT_SECRET", _settings.JWT_SECRET)

    return _settings


def _warn_generated(name: str, value: str) -> None:
    """Print a loud warning when a secret is auto-generated."""
    border = "=" * 60
    print(f"\n{border}")
    print(f"!!! AUTO-GENERATED {name} — NOT PERSISTENT ACROSS RESTARTS !!!")
    print(f"   Copy this into your Render environment variables:")
    print(f"   {name}={value}")
    print(f"{border}\n")


def get_admin_ids() -> List[int]:
    """Get list of admin IDs from comma-separated string."""
    settings = get_settings()
    try:
        if isinstance(settings.ADMIN_IDS, str):
            return [int(x.strip()) for x in settings.ADMIN_IDS.split(",") if x.strip()]
        elif isinstance(settings.ADMIN_IDS, list):
            return settings.ADMIN_IDS
        else:
            return [int(settings.ADMIN_IDS)]
    except Exception as e:
        print(f"⚠️ Error parsing ADMIN_IDS: {e}. Using default.")
        return [123456789]


def get_force_sub_channels() -> List[int]:
    """Get list of force subscription channels from comma-separated string."""
    settings = get_settings()
    try:
        if not settings.FORCE_SUB_CHANNELS or settings.FORCE_SUB_CHANNELS == "":
            return []

        if isinstance(settings.FORCE_SUB_CHANNELS, str):
            return [
                int(x.strip())
                for x in settings.FORCE_SUB_CHANNELS.split(",")
                if x.strip()
            ]
        elif isinstance(settings.FORCE_SUB_CHANNELS, list):
            return settings.FORCE_SUB_CHANNELS
        else:
            return []
    except Exception as e:
        print(f"⚠️ Error parsing FORCE_SUB_CHANNELS: {e}")
        return []
