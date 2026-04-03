from fastapi import APIRouter, HTTPException, Depends, Request, Response
from pydantic import BaseModel, Field
import logging
from datetime import datetime, timedelta
import uuid
import secrets
import hashlib

from database import get_user, create_one_time_key, get_db
from web.utils.rate_limiter import RateLimiter
from web.utils.csrf import BruteForceProtection, CSRFProtector, generate_csrf_token
from config.settings import get_settings

router = APIRouter()
logger = logging.getLogger(__name__)

# Constants
SESSION_TIMEOUT_HOURS = 24
settings = get_settings()
COOKIE_SECURE = settings.ENVIRONMENT.lower() == "production"
COOKIE_MAX_AGE = 60 * 60 * 24  # 24 hours


# Models
class LoginRequest(BaseModel):
    telegram_id: int = Field(..., alias="user_id")

    class Config:
        populate_by_name = True


class VerifyTokenRequest(BaseModel):
    token: str


# -------------------------------------------------------------
# 1. REQUEST MAGIC LINK
# -------------------------------------------------------------
@router.post(
    "/request-login-link", dependencies=[Depends(RateLimiter(times=1, seconds=60))]
)
async def request_magic_link(req: LoginRequest, request: Request):
    """
    Generate a Magic Link and send via Telegram Bot.
    """
    try:
        user_id = req.telegram_id

        # Check brute force protection
        allowed, delay, msg = BruteForceProtection.check(f"request_{user_id}")
        if not allowed:
            logger.warning(f"🚫 Brute force blocked for user {user_id}: {msg}")
            raise HTTPException(status_code=429, detail=msg)

        logger.info(f"🔍 Magic link request for user: {user_id}")

        # Check if user exists
        user = await get_user(user_id)
        if not user:
            BruteForceProtection.record_failure(f"request_{user_id}")
            logger.error(f"❌ User {user_id} not found in database")
            raise HTTPException(
                status_code=404, detail="User not found. Start bot first."
            )

        logger.info(f"✅ User {user_id} found in database")

        # Cooldown: reject if a valid, unused token already exists for this user.
        db = get_db()
        existing_token = await db.one_time_keys.find_one(
            {
                "user_id": user_id,
                "purpose": "magic_login",
                "used": {"$ne": True},
                "expires_at": {"$gt": datetime.utcnow()},
            }
        )
        if existing_token:
            raise HTTPException(
                status_code=429,
                detail="A login link was already sent. Please check Telegram or wait for it to expire.",
            )

        # Generate Magic Token
        magic_token = str(uuid.uuid4())
        expires_at = datetime.utcnow() + timedelta(minutes=10)
        client_ip = (
            request.headers.get(
                "X-Forwarded-For", request.client.host if request.client else "unknown"
            )
            .split(",")[0]
            .strip()
        )
        logger.info(
            f"🔑 Generated token for user {user_id} from IP {client_ip} (expires: {expires_at})"
        )

        logger.info(f"💾 Attempting to store token in database...")
        success = await create_one_time_key(
            user_id, magic_token, expires_at, purpose="magic_login"
        )

        if not success:
            logger.error(f"❌ create_one_time_key returned False for user {user_id}")
            raise HTTPException(
                status_code=500, detail="Server error. Please try again."
            )

        logger.info(f"✅ Token stored successfully in database")

        # Send via Bot
        bot = None
        if hasattr(request.app.state, "bot"):
            bot = request.app.state.bot

        if not bot:
            logger.error("❌ Bot instance not found in app.state")
            raise HTTPException(status_code=503, detail="Bot service not ready")

        logger.info(f"🤖 Bot instance found, preparing to send message")

        # Construct Link - Use signed token reference instead of exposing actual token in URL
        scheme = request.url.scheme
        netloc = request.url.netloc

        # Prefer HTTPS if not localhost
        if "localhost" not in netloc and scheme == "http":
            scheme = "https"

        origin = f"{scheme}://{netloc}"

        # Create a signed reference that can be validated server-side
        token_signature = hashlib.sha256(magic_token.encode()).hexdigest()[:16]
        files_link = f"{origin}/login.html?token_sig={token_signature}"

        # Store signature for secure lookup
        await db.one_time_keys.update_one(
            {"otp": magic_token}, {"$set": {"token_sig": token_signature}}
        )

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
            parse_mode="Markdown",
        )

        logger.info(f"✅ Magic link sent to Telegram user {user_id}")

        # Clear brute force on successful request
        BruteForceProtection.record_success(f"request_{user_id}")

        return {"status": "ok", "message": "Magic Link sent to Telegram"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Magic Link Request Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server error. Please try again.")


# -------------------------------------------------------------
# LEGACY ALIASES  (auth.js uses /request-code & /verify-code)
# -------------------------------------------------------------
@router.post("/request-code", dependencies=[Depends(RateLimiter(times=1, seconds=60))])
async def request_code_alias(req: LoginRequest, request: Request):
    """Alias for /request-login-link — used by auth.js OTP flow."""
    return await request_magic_link(req, request)


class VerifyCodeRequest(BaseModel):
    user_id: int
    code: str


@router.post("/verify-code", dependencies=[Depends(RateLimiter(times=5, seconds=300))])
async def verify_code_alias(
    req: VerifyCodeRequest, request: Request, response: Response
):
    """Alias for /verify-magic-token — used by auth.js OTP flow."""
    allowed, delay, msg = BruteForceProtection.check(f"verify_{req.user_id}")
    if not allowed:
        logger.warning(f"🚫 Brute force blocked for verify: {msg}")
        raise HTTPException(status_code=429, detail=msg)

    adapted_req = VerifyTokenRequest(token=req.code)
    try:
        return await verify_magic_token(adapted_req, request, response)
    except HTTPException:
        BruteForceProtection.record_failure(f"verify_{req.user_id}")
        raise


@router.post(
    "/verify-magic-token", dependencies=[Depends(RateLimiter(times=5, seconds=300))]
)
async def verify_magic_token(
    req: VerifyTokenRequest, request: Request, response: Response
):
    """
    Verify Magic Token -> Create DB Session -> Return Token with secure cookie
    """
    try:
        db = get_db()

        # Support both direct token and signature lookup
        record = None
        if len(req.token) > 20:
            # Looks like a full magic token, find by otp
            record = await db.one_time_keys.find_one(
                {"otp": req.token, "purpose": "magic_login"}
            )
        else:
            # Try signature lookup
            record = await db.one_time_keys.find_one(
                {"token_sig": req.token, "purpose": "magic_login"}
            )

        client_ip = (
            request.headers.get(
                "X-Forwarded-For", request.client.host if request.client else "unknown"
            )
            .split(",")[0]
            .strip()
        )

        if not record:
            logger.warning(
                f"🚫 Invalid token verification attempt from IP: {client_ip}"
            )
            raise HTTPException(status_code=401, detail="Invalid token")

        if record["expires_at"] < datetime.utcnow():
            logger.warning(
                f"⏰ Expired token verification attempt from IP: {client_ip}"
            )
            await db.one_time_keys.delete_one({"_id": record["_id"]})
            raise HTTPException(status_code=401, detail="Token expired")

        user_id = record["user_id"]
        logger.info(f"✅ Token valid for user: {user_id}")

        # Consume token atomically
        await db.one_time_keys.delete_one({"_id": record["_id"]})

        # Create Session
        session_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(session_token.encode()).hexdigest()
        expiry = datetime.utcnow() + timedelta(hours=SESSION_TIMEOUT_HOURS)
        csrf_token = generate_csrf_token(session_token)

        await db.sessions.insert_one(
            {
                "token_hash": token_hash,
                "user_id": user_id,
                "created_at": datetime.utcnow(),
                "expires_at": expiry,
                "ip": request.headers.get("X-Forwarded-For", request.client.host)
                .split(",")[0]
                .strip()
                if request.client
                else "unknown",
            }
        )

        logger.info(f"✅ Session created for user {user_id}")

        # Set secure HTTP-only cookie
        response.set_cookie(
            key="filebot_session",
            value=session_token,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="strict",
            max_age=COOKIE_MAX_AGE,
            path="/",
        )

        # Also return token for API clients (non-browser)
        return {
            "status": "ok",
            "token": session_token,
            "user_id": user_id,
            "csrf_token": csrf_token,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Verify Token Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server error. Please try again.")


# -------------------------------------------------------------
# LOGOUT
# -------------------------------------------------------------
@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout and invalidate session."""
    token = None

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
    else:
        token = request.cookies.get("filebot_session")

    if token:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        db = get_db()
        await db.sessions.delete_one({"token_hash": token_hash})

    response.delete_cookie(key="filebot_session", path="/")
    return {"status": "ok", "message": "Logged out"}


# -------------------------------------------------------------
# GET CSRF TOKEN
# -------------------------------------------------------------
@router.get("/csrf-token")
async def get_csrf_token(request: Request):
    """Get CSRF token for current session."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = auth_header.split(" ")[1]
    csrf_token = generate_csrf_token(token)
    return {"csrf_token": csrf_token}


# -------------------------------------------------------------
# AUTH DEPENDENCY (Middleware)
# -------------------------------------------------------------
async def get_current_user(request: Request) -> int:
    """
    Dependency to protect routes.
    Reads 'Authorization: Bearer <token>' or cookie -> Checks DB -> Returns User ID
    """
    # Try Authorization header first
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
    else:
        # Fall back to cookie
        token = request.cookies.get("filebot_session")

    if not token:
        raise HTTPException(status_code=401, detail="Missing Token")

    try:
        db = get_db()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        session = await db.sessions.find_one(
            {
                "token_hash": token_hash,
                "expires_at": {"$gt": datetime.utcnow()},
            }
        )

        if not session:
            raise HTTPException(status_code=401, detail="Invalid/Expired Session")

        return session["user_id"]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Session Check Error (DB down?): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error")


# -------------------------------------------------------------
# ADMIN DEPENDENCY (Middleware)
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
