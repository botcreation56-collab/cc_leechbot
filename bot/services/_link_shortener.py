"""
bot/services/_link_shortener.py — LinkShortener + CloudLinkGenerator + OTPService.

Three small helpers that were concatenated in the original flat file.
"""

import logging
import random
import string
import urllib.parse
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger("filebot.services.links")

# In-memory OTP storage: {user_id: {"code": "123456", "expires_at": datetime}}
# In production, use Redis.
_otp_storage: Dict[int, Dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# CloudLinkGenerator
# ─────────────────────────────────────────────────────────────────────────────

class CloudLinkGenerator:
    """Generates and manages cloud file links."""

    @staticmethod
    def generate_link(
        file_id: str,
        webhook_url: str,
        expiry_days: int = 7,
        password: Optional[str] = None,
    ) -> Dict[str, str]:
        """Generate shareable cloud link."""
        try:
            token = str(uuid.uuid4())[:16]
            link = f"{webhook_url}/watch/{file_id}?token={token}"
            return {
                "link": link,
                "token": token,
                "file_id": file_id,
                "created_at": datetime.utcnow().isoformat(),
                "expires_at": (datetime.utcnow() + timedelta(days=expiry_days)).isoformat(),
                "password": password,
                "views": 0,
            }
        except Exception as e:
            logger.error(f"❌ Generate link error: {e}")
            return {}

    @staticmethod
    async def shorten_link(long_link: str, shortener_service: str = "tinyurl") -> Optional[str]:
        """Shorten cloud link using external service."""
        try:
            import httpx
            if shortener_service == "tinyurl":
                encoded_url = urllib.parse.quote(long_link)
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"https://tinyurl.com/api-create.php?url={encoded_url}", timeout=5
                    )
                    if resp.status_code == 200:
                        return resp.text.strip()

            from config.settings import get_settings
            settings = get_settings()
            base_link = settings.BOT_LINK.replace("https://", "")
            return f"{base_link}?start={long_link[-8:]}"
        except Exception as e:
            logger.error(f"❌ Shorten link error: {e}")
            return None

    @staticmethod
    def validate_token(token: str, stored_token: str) -> bool:
        """Validate access token."""
        return token == stored_token


# ─────────────────────────────────────────────────────────────────────────────
# LinkShortener
# ─────────────────────────────────────────────────────────────────────────────

class LinkShortener:
    """Handles URL shortening using configured external providers."""

    @staticmethod
    async def shorten_url(long_url: str) -> Optional[str]:
        """Shorten a URL using the active shortener from config, or TinyURL fallback."""
        try:
            import aiohttp
            from bot.database import get_config
            from bot.services._link_shortener import CloudLinkGenerator

            # Default fallback behaviour
            async def _fallback():
                logger.info("Using TinyURL fallback for LinkShortener")
                return await CloudLinkGenerator.shorten_link(long_url, "tinyurl")

            config = await get_config()
            if not config:
                return await _fallback()

            shorteners = config.get("link_shorteners", [])
            active_shortener = next((s for s in shorteners if s.get("active")), None)

            if not active_shortener:
                return await _fallback()

            domain = active_shortener.get("domain")
            api_key = active_shortener.get("api_key")
            if not domain or not api_key:
                return await _fallback()

            encoded_url = urllib.parse.quote(long_url)
            api_url = f"https://{domain}/api?api={api_key}&url={encoded_url}"

            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if "shortenedUrl" in data:
                            return data["shortenedUrl"]
                        logger.warning(f"⚠️ Unknown response format from {domain}: {data}")
                    else:
                        logger.error(f"❌ Shortener API failed: {response.status}")
            
            # If the API request failed or returned unknown format, also fallback
            return await _fallback()
            
        except Exception as e:
            logger.error(f"❌ Shortener error: {e}")
            from bot.services._link_shortener import CloudLinkGenerator
            return await CloudLinkGenerator.shorten_link(long_url, "tinyurl")

    @staticmethod
    async def track_and_shorten(file_id: str, user_id: int, master_url: str) -> Optional[str]:
        """
        Duplicate the master URL for tracking, shorten it, and log the relationship to MongoDB.
        """
        try:
            import uuid
            from datetime import datetime
            
            # 1. Create duplicate tracking URL
            hash_id = uuid.uuid4().hex[:6]
            separator = "&" if "?" in master_url else "?"
            duplicate_url = f"{master_url}{separator}ref=user_{user_id}&hash={hash_id}"
            
            # 2. Shorten the duplicate URL
            shortened_url = await LinkShortener.shorten_url(duplicate_url)
            if not shortened_url:
                return None
                
            # 3. Detect which service was used
            from bot.database import get_config
            config = await get_config()
            service_domain = "tinyurl.com"
            if config:
                shorteners = config.get("link_shorteners", [])
                active_shortener = next((s for s in shorteners if s.get("active")), None)
                if active_shortener and active_shortener.get("domain"):
                    service_domain = active_shortener.get("domain")
                    
            if service_domain not in shortened_url and "tinyurl" in shortened_url:
                service_domain = "tinyurl.com"

            # 4. Save to MongoDB
            from bot.database import get_db
            db = get_db()
            
            tracking_doc = {
                "file_id": file_id,
                "user_id": user_id,
                "urls": {
                    "master_url": master_url,
                    "duplicate_url": duplicate_url,
                    "shortened_url": shortened_url
                },
                "shortener_service": service_domain,
                "status": {
                    "is_active": True,
                    "clicks": 0,
                    "last_checked_available": datetime.utcnow()
                },
                "created_at": datetime.utcnow()
            }
            
            await db.tracked_links.insert_one(tracking_doc)
            logger.info(f"✅ Tracked new link for user {user_id} -> {service_domain}")
            
            return shortened_url
        except Exception as e:
            logger.error(f"❌ Error in track_and_shorten: {e}")
            return await LinkShortener.shorten_url(master_url)

    @staticmethod
    async def get_verification_link(user_id: int) -> str:
        """Generate a verification link for a user to skip the queue."""
        from config.settings import get_settings
        settings = get_settings()
        verify_base = f"https://{settings.DOMAIN}/verify?user={user_id}"
        short_url = await LinkShortener.shorten_url(verify_base)
        return short_url or verify_base

    @staticmethod
    def is_short_link(url: str) -> bool:
        """Check if a URL looks like it came from a known shortener."""
        return False  # TODO: Implement if needed


# ─────────────────────────────────────────────────────────────────────────────
# OTPService
# ─────────────────────────────────────────────────────────────────────────────

class OTPService:
    """Manages One-Time Passwords for Web Authentication via Telegram."""

    @staticmethod
    def generate_otp(length: int = 6) -> str:
        """Generate numeric OTP."""
        return "".join(random.choices(string.digits, k=length))

    @staticmethod
    async def create_and_send_otp(bot, user_id: int) -> bool:
        """Generates OTP and sends it to the user's Telegram DM."""
        try:
            code = OTPService.generate_otp()
            expires_at = datetime.utcnow() + timedelta(minutes=5)
            _otp_storage[user_id] = {"code": code, "expires_at": expires_at}
            msg_text = (
                f"🔐 **Login Verification**\n\n"
                f"Your OTP Code is: `{code}`\n\n"
                f"⏱️ Expires in 5 minutes.\n"
                f"⚠️ Do not share this code."
            )
            await bot.send_message(chat_id=user_id, text=msg_text, parse_mode="Markdown")
            logger.info(f"OTP sent to {user_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to send OTP to {user_id}: {e}")
            return False

    @staticmethod
    def verify_otp(user_id: int, code: str) -> bool:
        """Verify the provided OTP."""
        record = _otp_storage.get(user_id)
        if not record:
            return False
        if datetime.utcnow() > record["expires_at"]:
            del _otp_storage[user_id]
            return False
        if record["code"] == code:
            del _otp_storage[user_id]
            return True
        return False
