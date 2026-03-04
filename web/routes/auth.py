
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
import logging
from datetime import datetime, timedelta
import uuid
import secrets
import hashlib
from typing import Optional

from bot.database import (
    get_user, 
    create_one_time_key,
    get_db
)
from web.utils.rate_limiter import RateLimiter

router = APIRouter()
logger = logging.getLogger(__name__)

# Constants
SESSION_TIMEOUT_HOURS = 24

# Models
class LoginRequest(BaseModel):
    telegram_id: int = Field(..., alias='user_id')
    
    class Config:
        populate_by_name = True

class VerifyTokenRequest(BaseModel):
    token: str

# -------------------------------------------------------------
# 1. REQUEST MAGIC LINK
# -------------------------------------------------------------
@router.post("/request-login-link", dependencies=[Depends(RateLimiter(times=3, seconds=60))])
async def request_magic_link(req: LoginRequest, request: Request):
    """
    Generate a Magic Link and send via Telegram Bot.
    """
    try:
        user_id = req.telegram_id
        logger.info(f"🔍 Magic link request for user: {user_id}")
        
        # Check if user exists
        user = await get_user(user_id)
        if not user:
             logger.error(f"❌ User {user_id} not found in database")
             raise HTTPException(status_code=404, detail="User not found. Start bot first.")
        
        logger.info(f"✅ User {user_id} found in database")

        # Generate Magic Token (UUID)
        magic_token = str(uuid.uuid4())
        expires_at = datetime.utcnow() + timedelta(minutes=10)
        logger.info(f"🔑 Generated token for user {user_id} (expires: {expires_at})")
        
        # Store in DB 
        logger.info(f"💾 Attempting to store token in database...")
        success = await create_one_time_key(user_id, magic_token, expires_at)
        
        if not success:
            logger.error(f"❌ create_one_time_key returned False for user {user_id}")
            raise HTTPException(status_code=500, detail="Database Error")
        
        logger.info(f"✅ Token stored successfully in database")

        # Send via Bot
        bot = None
        if hasattr(request.app.state, "bot"):
            bot = request.app.state.bot
        
        if not bot:
             logger.error("❌ Bot instance not found in app.state")
             raise HTTPException(status_code=503, detail="Bot service not ready")
        
        logger.info(f"🤖 Bot instance found, preparing to send message")

        # Construct Link
        scheme = request.url.scheme
        netloc = request.url.netloc
        
        # Prefer HTTPS if not localhost
        if "localhost" not in netloc and scheme == "http":
            scheme = "https"
            
        origin = f"{scheme}://{netloc}"
        
        files_link = f"{origin}/login.html?magic_token={magic_token}"
        logger.info(f"🔗 Sending magic link to user {user_id}")

        await bot.send_message(
            chat_id=user_id,
            text=(
                "🔐 **Web Login Request**\n\n"
                "Click the link below to sign in:\n"
                f"👉 [Login to FileBot]({files_link})\n\n"
                "⚠️ Expires in 10 minutes.\n"
                "If you did not request this, ignore it."
            ),
            parse_mode="Markdown"
        )
        
        logger.info(f"✅ Magic link sent to Telegram user {user_id}")
        return {"status": "ok", "message": "Magic Link sent to Telegram"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Magic Link Request Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# -------------------------------------------------------------
# 5. LEGACY ALIASES  (auth.js uses /request-code & /verify-code)
# -------------------------------------------------------------
@router.post("/request-code", dependencies=[Depends(RateLimiter(times=3, seconds=60))])
async def request_code_alias(req: LoginRequest, request: Request):
    """Alias for /request-login-link — used by auth.js OTP flow."""
    return await request_magic_link(req, request)


class VerifyCodeRequest(BaseModel):
    user_id: int
    code: str

@router.post("/verify-code", dependencies=[Depends(RateLimiter(times=10, seconds=60))])
async def verify_code_alias(req: VerifyCodeRequest, request: Request):
    """Alias for /verify-magic-token — used by auth.js OTP flow."""
    # Map the `code` field to the `token` field expected by the new implementation
    adapted_req = VerifyTokenRequest(token=req.code)
    return await verify_magic_token(adapted_req, request)

@router.post("/verify-magic-token", dependencies=[Depends(RateLimiter(times=10, seconds=60))])
async def verify_magic_token(req: VerifyTokenRequest, request: Request):
    """
    Verify Magic Token -> Create DB Session -> Return Token
    """
    try:
        db = get_db()
        
        # Find token record
        logger.info(f"🔍 Token verification attempt")
        record = await db.one_time_keys.find_one({"otp": req.token})
        
        if not record:
            logger.warning(f"❌ Invalid token verification attempt")
            raise HTTPException(status_code=401, detail="Invalid token")
             
        if record["expires_at"] < datetime.utcnow():
            logger.warning(f"⏰ Expired token verification attempt")
            await db.one_time_keys.delete_one({"_id": record["_id"]})
            raise HTTPException(status_code=401, detail="Token expired")
            
        user_id = record["user_id"]
        logger.info(f"✅ Token valid for user: {user_id}")
        
        # Consume token
        await db.one_time_keys.delete_one({"_id": record["_id"]})
            
        # Create Session
        session_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(session_token.encode()).hexdigest()
        expiry = datetime.utcnow() + timedelta(hours=SESSION_TIMEOUT_HOURS)
        
        await db.sessions.insert_one({
            "token_hash": token_hash,
            "user_id": user_id,
            "created_at": datetime.utcnow(),
            "expires_at": expiry,
            "ip": request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip() if request.client else "unknown" 
        })
        
        logger.info(f"✅ Session created for user {user_id}")
        
        return {
            "status": "ok",
            "token": session_token,
            "user_id": user_id
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Verify Token Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server Error")

# -------------------------------------------------------------
# 3. AUTH DEPENDENCY (Middleware)
# -------------------------------------------------------------
async def get_current_user(request: Request) -> int:
    """
    Dependency to protect routes. 
    Reads 'Authorization: Bearer <token>' -> Checks DB -> Returns User ID
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Token")
        
    try:
        token = auth_header.split(" ")[1]
        if not token:
            raise ValueError()
    except (IndexError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token format")
    
    try:
        db = get_db()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        session = await db.sessions.find_one({
            "token_hash": token_hash,
            "expires_at": {"$gt": datetime.utcnow()} # Not expired
        })
        
        if not session:
            raise HTTPException(status_code=401, detail="Invalid/Expired Session")
            
        return session["user_id"]
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Session Check Error (DB down?): {e}", exc_info=True)
        # S002 & T003: Do not mask infrastructure failures as auth failures
        raise HTTPException(status_code=500, detail="Internal Server Error")


# -------------------------------------------------------------
# 4. ADMIN DEPENDENCY (Middleware)
# -------------------------------------------------------------
async def get_current_admin(user_id: int = Depends(get_current_user)) -> int:
    """
    Dependency to protect admin routes.
    Ensures the user is an admin.
    """
    from config.settings import get_admin_ids
    if user_id not in get_admin_ids():
        logger.warning(f"🚫 Unauthorized admin access attempt by {user_id}")
        raise HTTPException(status_code=403, detail="Not authorized")
    return user_id