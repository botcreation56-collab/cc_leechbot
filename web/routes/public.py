"""
Public API routes - watch pages, downloads, documentation
"""

import logging
from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import FileResponse
from pydantic import BaseModel

from bot.database import get_db, update_user, get_config, get_user
from config.settings import get_settings
from datetime import datetime, timedelta
from pathlib import Path
from fastapi.templating import Jinja2Templates
from fastapi import Request

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "static" / "pages"))

logger = logging.getLogger(__name__)

router = APIRouter()
settings = get_settings()

from fastapi.responses import RedirectResponse

@router.get("/")
@router.head("/")
async def serve_root():
    """Serve the login page directly at the root path."""
    login_page = Path(__file__).parent.parent / "static" / "pages" / "login.html"
    return FileResponse(str(login_page))

@router.get("/favicon.ico", include_in_schema=False)
@router.head("/favicon.ico", include_in_schema=False)
async def favicon_noop():
    """Handle favicon requests with No Content to prevent 404 logs."""
    return Response(status_code=204)


class FileMetadata(BaseModel):
    """File metadata"""
    file_id: str
    filename: str
    size: int
    url: str


from fastapi import Cookie, Response

@router.get("/api/verify_link/{file_id}/{token}")
async def verify_and_set_cookie(file_id: str, token: str, response: Response, action: str = "stream"):
    """
    Validates the token and sets a secure HTTP-only cookie, then redirects to the clean URL.
    This entirely hides the token from the user's address bar.
    """
    try:
        db = get_db()
        
        # Verify Token — only accept stream-purpose tokens
        token_doc = await db.one_time_keys.find_one({
            "otp": token,
            "purpose": "stream",
            "used": False,
            "expires_at": {"$gt": datetime.utcnow()},
        })
        
        if not token_doc:
            raise HTTPException(status_code=401, detail="Invalid or expired link token")

        # Set secure HTTP-only cookie (valid for 6 hours)
        response.set_cookie(
            key="stream_auth_token",
            value=token,
            httponly=True,
            secure=settings.ENVIRONMENT.lower() == "production",
            samesite="lax",
            max_age=21600 # 6 hours
        )
        
        # Redirect to the clean URL
        if action == "watch":
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/api/watch/{file_id}", headers=response.headers)
        elif action == "download":
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/api/download/{file_id}", headers=response.headers)
            
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/api/stream/{file_id}", headers=response.headers)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Token verification error") # Sanitized log
        raise HTTPException(status_code=500, detail="Server Error")


@router.get("/api/watch/{file_id}")
async def watch_file_endpoint(file_id: str, stream_auth_token: str | None = Cookie(None)):
    """
    Get watch page for file with clean URL.
    Requires stream_auth_token cookie.
    """
    try:
        from pathlib import Path
        db = get_db()
        
        # Validate Cookie
        if not stream_auth_token:
            raise HTTPException(status_code=401, detail="Unauthorized. Missing streaming cookie.")
            
        token_doc = await db.one_time_keys.find_one({
            "otp": stream_auth_token,
            "expires_at": {"$gt": datetime.utcnow()},
        })
        if not token_doc:
            raise HTTPException(status_code=401, detail="Unauthorized. Expired or invalid cookie.")

        # Get file metadata
        file_doc = await db.cloud_files.find_one({"file_id": file_id})
        if not file_doc:
            raise HTTPException(status_code=404, detail="File not found")
        
        # Check Visibility — owners can always access their own private files
        visibility = file_doc.get("visibility", "public")
        requester_id = token_doc.get("user_id")
        file_owner_id = file_doc.get("user_id") or file_doc.get("owner_id")
        is_owner = (requester_id and file_owner_id and int(requester_id) == int(file_owner_id))
        if visibility == "private" and not is_owner:
            raise HTTPException(status_code=403, detail="🔒 Private File. Access Denied.")

        # Stream Size Limits
        user_id = token_doc.get("user_id")
        user = await get_user(user_id) if user_id else None
        plan = user.get("plan", "free") if user else "free"
        
        config = await get_config()
        max_size_mb = config.get("pro_max_file_size", 4000) if plan == "pro" else config.get("max_file_size", 2000)
        max_size_bytes = max_size_mb * 1024 * 1024
        
        file_size = file_doc.get("size", 0)
        if file_size > max_size_bytes:
            # Render a custom error if we want, or just raise HTML
            raise HTTPException(
                status_code=403, 
                detail=f"File exceeds stream limit ({max_size_mb}MB for {plan.title()} plan)."
            )

        # Log cleanly
        logger.info(f"✅ Watch page accessed: {file_id}")
        
        # Return watch page
        static_dir = Path(__file__).parent.parent / "static"
        watch_page = static_dir / "pages" / "watch.html"
        if watch_page.exists():
            return FileResponse(str(watch_page))
        
        return {
            "file_id": file_id,
            "filename": file_doc.get("filename", "file"),
            "message": "Watch page"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Watch file error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/download/{file_id}")
async def download_page_endpoint(file_id: str, stream_auth_token: str | None = Cookie(None)):
    """
    Serve Buffer Page (3s wait) with clean URL.
    """
    if not stream_auth_token:
        raise HTTPException(status_code=401, detail="Unauthorized. Missing streaming cookie.")
        
    from starlette.requests import Request
    
    # Clean stream URL
    stream_url = f"/api/stream/{file_id}"
    
    static_dir = Path(__file__).parent.parent / "static" / "pages" / "download_buffer.html"
    if static_dir.exists():
        content = static_dir.read_text(encoding="utf-8")
        content = content.replace("{{ stream_url }}", stream_url)
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content)
    
    return {"message": "Buffer template missing, redirecting...", "url": stream_url}


@router.get("/api/stream/{file_id}")
async def stream_file_endpoint(file_id: str, stream_auth_token: str | None = Cookie(None)):
    """
    Actual File Stream / Redirect with clean URL.
    Token is validated via secure HTTP-only cookie.
    """
    try:
        db = get_db()
        file_doc = await db.cloud_files.find_one({"file_id": file_id})

        if not file_doc:
            raise HTTPException(status_code=404, detail="File not found")

        # --- COOKIE VALIDATION (must happen before visibility check) ---
        if not stream_auth_token:
            raise HTTPException(status_code=401, detail="Unauthorized. Missing streaming cookie.")

        # Fix 5: enforce used=False so replayed cookies are rejected
        token_doc = await db.one_time_keys.find_one({
            "otp": stream_auth_token,
            "used": False,
            "expires_at": {"$gt": datetime.utcnow()},
        })
        if not token_doc:
            raise HTTPException(status_code=401, detail="Invalid or expired stream session")

        # Mark token as consumed (find_one already confirmed used=False, so always mark now)
        await db.one_time_keys.update_one(
            {"_id": token_doc["_id"]},
            {"$set": {"used": True}}
        )
        # --- END COOKIE VALIDATION ---

        # Visibility check — owners can always access their own private files
        requester_id = token_doc.get("user_id")
        file_owner_id = file_doc.get("user_id") or file_doc.get("owner_id")
        is_owner = (requester_id and file_owner_id and int(requester_id) == int(file_owner_id))
        if file_doc.get("visibility") == "private" and not is_owner:
            raise HTTPException(status_code=403, detail="🔒 Private File. Access Denied.")

        # Stream Size Limits
        user_id = token_doc.get("user_id")
        user = await get_user(user_id) if user_id else None
        plan = user.get("plan", "free") if user else "free"
        
        config = await get_config()
        max_size_mb = config.get("pro_max_file_size", 4000) if plan == "pro" else config.get("max_file_size", 2000)
        max_size_bytes = max_size_mb * 1024 * 1024
        
        file_size = file_doc.get("size", 0)
        if file_size > max_size_bytes:
            raise HTTPException(
                status_code=403, 
                detail=f"Stream blocked: File ({file_size/(1024*1024):.1f}MB) exceeds stream limit ({max_size_mb}MB for {plan.title()} plan)."
            )

        cloud_url = file_doc.get("cloud_url", "")
        filename  = file_doc.get("filename", "file")

        from urllib.parse import quote
        content_disposition = f"attachment; filename*=UTF-8''{quote(filename)}"

        if cloud_url and cloud_url.startswith("http"):
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=cloud_url)

        # Stream blocks disabled per User request -> relying on natural UI / Ext Players

        # Speed limits
        # Free: 1.2 Mbps = 153600 bytes/s
        # Pro: Unlimited (passing 0 or large number)
        speed_limit = 0 if plan == "pro" else 153600

        from fastapi.responses import RedirectResponse
        telegram_file_id = file_doc.get("file_id")
        
        # Don't add limit query param if unlimited
        if speed_limit > 0:
            return RedirectResponse(url=f"/stream/{telegram_file_id}?limit={speed_limit}")
        return RedirectResponse(url=f"/stream/{telegram_file_id}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Stream configuration error") # Sanitized log
        raise HTTPException(status_code=500, detail="Server Error")


@router.get("/api/api-doc")
async def api_documentation():
    """
    Get API documentation
    
    Returns:
        Documentation
    """
    try:
        doc = {
            "title": "FileBot API Documentation",
            "version": "1.0",
            "endpoints": [
                {
                    "method": "POST",
                    "path": "/auth/request-code",
                    "description": "Request one-time login code",
                    "params": ["user_id"]
                },
                {
                    "method": "POST",
                    "path": "/auth/verify-code",
                    "description": "Verify code and get JWT token",
                    "params": ["user_id", "code"]
                },
                {
                    "method": "GET",
                    "path": "/api/watch/{file_id}",
                    "description": "Watch page for file",
                    "params": ["file_id"]
                },
                {
                    "method": "GET",
                    "path": "/api/download/{file_id}/{token}",
                    "description": "Download file",
                    "params": ["file_id", "token"]
                }
            ],
            "limits": {
                "free_user": {
                    "max_file_size": "5GB",
                    "daily_limit": 5,
                    "retention": "7 days"
                },
                "pro_user": {
                    "max_file_size": "10GB",
                    "daily_limit": "Unlimited",
                    "retention": "28 days"
                }
            }
        }
        
        logger.info("✅ API documentation retrieved")
        return doc
    
    except Exception as e:
        logger.error(f"❌ API doc error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# REMOVED: Conflicting /health endpoint - now in main.py with better diagnostics
# @router.get("/health")
# async def health_check():
#     """Health check endpoint"""
#     return {"status": "healthy"}


@router.get("/verify")
async def verify_priority_endpoint(user: int, token: str):
    """
    Handle Shortener Verification Callback.

    Security model:
    - Requires ?user=<telegram_id>&token=<hmac_token>
    - Token is validated against the one_time_keys collection
    - Without a valid token, any caller could grant arbitrary users priority
      simply by knowing (or guessing) their Telegram ID.
    """
    try:
        if not user or user <= 0:
            raise HTTPException(status_code=400, detail="Missing or invalid user ID")
        if not token or len(token) < 8:
            raise HTTPException(status_code=400, detail="Missing or invalid token")

        db = get_db()

        # Validate the token belongs to this user, is the correct purpose, and is still valid
        token_doc = await db.one_time_keys.find_one({
            "user_id": user,
            "otp": token,
            "purpose": "priority_verify",
            "used": False,
            "expires_at": {"$gt": datetime.utcnow()},
        })

        if not token_doc:
            logger.warning(f"🚫 /verify called with invalid token for user_id={user}")
            raise HTTPException(status_code=401, detail="Invalid or expired verification token")

        # Consume the token (one-time use)
        await db.one_time_keys.update_one(
            {"_id": token_doc["_id"]},
            {"$set": {"used": True}}
        )

        # Grant priority for 1 hour
        priority_until = datetime.utcnow() + timedelta(hours=1)
        await update_user(user, {"priority_until": priority_until})

        logger.info(f"✅ Priority granted to user {user} until {priority_until}")

        # Serve success page
        success_page = Path(__file__).parent.parent / "static" / "pages" / "success.html"
        if success_page.exists():
            return FileResponse(str(success_page))
        return {"status": "ok", "message": "Priority granted for 1 hour."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ /verify error: {e}")
        raise HTTPException(status_code=500, detail="Verification failed")

@router.get("/queue-bypassed")
async def queue_bypassed_endpoint(request: Request, token: str, bot: str):
    """
    Renders the success page after a user completes the shortener link.
    Injects the bot username and bypass token so the HTML button can direct
    the user back to the Telegram bot with the exact start payload.
    """
    return templates.TemplateResponse("success.html", {
        "request": request,
        "bot_username": bot,
        "startid": token
    })

@router.get("/api/rclone/auth")
async def rclone_auth_redirect(user_id: int, client_id: str, client_secret: str):
    """
    Step 1: Redirect user to Google OAuth page.
    We pass user_id, client_id, and client_secret in the 'state' to recover them in the callback.
    """
    import json
    import base64
    from urllib.parse import quote
    from bot.database import get_config
    
    state_data = {
        "u": user_id,
        "i": client_id,
        "s": client_secret
    }
    state = base64.urlsafe_b64encode(json.dumps(state_data).encode()).decode()
    
    # Redirect URI must match what's configured in Google Cloud Console
    config = await get_config() or {}
    base_url = (config.get("webhook_url") or "").rstrip("/").replace("/webhook/telegram", "")
    if not base_url:
        from web.core.config import settings
        base_url = (settings.WEBHOOK_URL or "").rstrip("/").replace("/webhook/telegram", "")
        
    redirect_uri = f"{base_url}/api/rclone/callback"
    
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={client_id}"
        f"&redirect_uri={quote(redirect_uri)}"
        "&response_type=code"
        "&scope=https://www.googleapis.com/auth/drive"
        "&access_type=offline"
        "&prompt=consent"
        f"&state={state}"
    )
    
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=auth_url)

@router.get("/api/rclone/callback")
async def rclone_callback(request: Request, code: str = None, state: str = None, error: str = None):
    """
    Step 2: Receive code from Google, exchange for Refresh Token, and save.
    """
    if error:
        return {"error": f"OAuth Error: {error}"}
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    import json
    import base64
    import httpx
    try:
        # Decode state (adds padding missing from URLs)
        try:
            padded_state = state + "=" * ((4 - len(state) % 4) % 4)
            state_data = json.loads(base64.urlsafe_b64decode(padded_state.encode()).decode())
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid state parameter (CSRF protection blocked request)")
            
        user_id = state_data.get("u")
        client_id = state_data.get("i")
        client_secret = state_data.get("s")
        plan = state_data.get("p", "user")
        max_users = state_data.get("m", 1)
        concurrency = state_data.get("c", 4)
        
        if not user_id or not client_id or not client_secret:
             raise HTTPException(status_code=400, detail="Malformed state payload")
        
        # Exchange code for token
        from bot.database import get_config
        config = await get_config() or {}
        base_url = (config.get("webhook_url") or "").rstrip("/").replace("/webhook/telegram", "")
        if not base_url:
            from web.core.config import settings
            base_url = (settings.WEBHOOK_URL or "").rstrip("/").replace("/webhook/telegram", "")
            
        redirect_uri = f"{base_url}/api/rclone/callback"
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code"
                }
            )
            token_data = resp.json()
            
        if "refresh_token" not in token_data:
            logger.error(f"❌ No refresh token in Google response: {token_data}")
            return {"error": "Failed to get refresh token. Did you enable 'Offline Access' and remove any existing access for this app?"}

        refresh_token = token_data["refresh_token"]
        
        # Generate Rclone config
        remote_name = f"user_{user_id}_gdrive"
        config_snippet = (
            f"[{remote_name}]\n"
            f"type = drive\n"
            f"client_id = {client_id}\n"
            f"client_secret = {client_secret}\n"
            f"scope = drive\n"
            f"token = {{\"access_token\":\"{token_data.get('access_token','')}\",\"token_type\":\"Bearer\",\"refresh_token\":\"{refresh_token}\",\"expiry\":\"\"}}\n"
        )
        
        # Save to DB
        from bot.database import add_rclone_config
        config_id = await add_rclone_config(
            name=remote_name,
            service="gdrive",
            plan=plan,
            max_users=max_users,
            concurrency=concurrency,
            credentials=config_snippet,
            admin_id=user_id
        )
        
        if config_id:
            # Update the config snippet to reflect the assigned DB ID as its internal section name
            from infrastructure.database._legacy_bot._rclone import update_rclone_config
            updated_snippet = config_snippet.replace(f"[{remote_name}]", f"[{config_id}]")
            await update_rclone_config(config_id, {"credentials": updated_snippet})

            # Notify user via bot — show them the config and a confirm button
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            bot = request.app.state.bot
            
            if plan == "user":
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "☁️ Use as Default Upload Destination",
                        callback_data=f"us_set_rclone_dest_{config_id}"
                    )],
                    [InlineKeyboardButton("⚙️ My Settings", callback_data="us_settings")]
                ])
                
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"✅ **Google Drive Connected!**\n\n"
                        f"Your Rclone remote has been created and saved.\n\n"
                        f"📋 **Remote Name:** `{config_id}`\n\n"
                        f"```\n{updated_snippet.strip()}\n```\n\n"
                        "You can copy the config above to use with `rclone` locally.\n"
                        "Or tap the button below to use this Drive as your default upload destination."
                    ),
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )
            else:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚙️ Back to Admin Rclone", callback_data="admin_rclone")]
                ])
                
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"✅ **Global Google Drive Connected!**\n\n"
                        f"The Admin Rclone remote for the `{plan}` plan has been successfully created and saved.\n\n"
                        f"📋 **Remote ID:** `{config_id}`\n\n"
                        f"```\n{updated_snippet.strip()}\n```\n\n"
                        "You can now manage this remote in the Admin -> Cloud -> Rclone menu."
                    ),
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )

            try:
                bot_user = await bot.get_me()
                bot_username = bot_user.username
            except Exception:
                bot_username = ""

            return templates.TemplateResponse("success.html", {
                "request": request,
                "icon": "☁️",
                "heading": "Drive Connected!",
                "message": "Your Rclone remote has been successfully created.<br>You can now safely close this window.",
                "bot_username": bot_username,
                "btn_text": "Return to Telegram"
            })

        else:
            return {"error": "Failed to save rclone config to database."}

    except Exception as e:
        logger.error(f"❌ Rclone OAuth Callback Error: {e}", exc_info=True)
        return {"error": f"Internal Server Error: {str(e)}"}
