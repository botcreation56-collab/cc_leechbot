"""
bot/services/_gdrive.py — Direct Google Drive API integration.

Uploads files directly to GDrive without storing on Render disk.
Uses Google API for direct streaming upload.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime

import httpx

logger = logging.getLogger("filebot.services.gdrive")

GOOGLE_DRIVE_API = "https://www.googleapis.com/upload/drive/v3"
GOOGLE_DRIVE_API_V3 = "https://www.googleapis.com/drive/v3"


class GDriveService:
    """Direct Google Drive API operations without local storage."""

    _folder_ids: Dict[str, str] = {}
    _access_token: Optional[str] = None

    @classmethod
    async def get_access_token(cls) -> Optional[str]:
        """Get valid access token, refreshing if needed."""
        if cls._access_token:
            return cls._access_token

        from database.gdrive import get_gdrive_config

        config = await get_gdrive_config()
        if not config:
            # Try env vars as fallback
            from config.settings import get_settings

            settings = get_settings()
            if not settings.GDRIVE_REFRESH_TOKEN:
                logger.warning("GDrive: No credentials configured")
                return None

            access_token = await cls.refresh_access_token(
                settings.GDRIVE_REFRESH_TOKEN,
                settings.GDRIVE_CLIENT_ID,
                settings.GDRIVE_CLIENT_SECRET,
            )
        else:
            access_token = await cls.refresh_access_token(
                config.get("refresh_token"),
                config.get("client_id"),
                config.get("client_secret"),
            )

        if access_token:
            cls._access_token = access_token

        return access_token

    @classmethod
    async def setup_folders(cls) -> bool:
        """
        Setup folder structure on GDrive:
        - bot_name/temp/  (for processing)
        - bot_name/Free/  (free user uploads)
        - bot_name/Pro/   (pro user uploads)

        Stores folder IDs in config for later use.
        """
        try:
            from config.settings import get_settings
            from database.gdrive import save_gdrive_folder_ids

            settings = get_settings()
            access_token = await cls.get_access_token()
            if not access_token:
                logger.error("GDrive: Cannot setup folders - no access token")
                return False

            # Try database first, then fallback to env
            from database.gdrive import get_gdrive_config

            db_config = await get_gdrive_config()
            root_folder_id = (
                db_config.get("root_folder_id")
                if db_config
                else settings.GDRIVE_ROOT_FOLDER_ID
            )
            bot_name = settings.BOT_NAME or "CCLeechBot"

            folder_structure = {
                "temp": f"{bot_name}/temp",
                "free": f"{bot_name}/Free",
                "pro": f"{bot_name}/Pro",
            }

            new_folder_ids = {}

            for key, folder_path in folder_structure.items():
                parts = folder_path.split("/")
                parent_id = root_folder_id if root_folder_id else None

                for i, folder_name in enumerate(parts):
                    folder_id = await cls._find_or_create_folder(
                        access_token, folder_name, parent_id
                    )
                    if not folder_id:
                        logger.error(f"GDrive: Failed to create/find {folder_path}")
                        return False

                    parent_id = folder_id

                    # Store folder ID for plan folders
                    if key == "temp" and i == 1:
                        new_folder_ids["temp"] = folder_id
                    elif key == "free" and i == 1:
                        new_folder_ids["free"] = folder_id
                    elif key == "pro" and i == 1:
                        new_folder_ids["pro"] = folder_id

            cls._folder_ids = new_folder_ids
            logger.info(f"✅ GDrive folders setup complete: {new_folder_ids}")

            # Store folder IDs in database
            from database.gdrive import save_gdrive_folder_ids

            await save_gdrive_folder_ids(
                temp_id=new_folder_ids.get("temp"),
                free_id=new_folder_ids.get("free"),
                pro_id=new_folder_ids.get("pro"),
            )

            return True

        except Exception as e:
            logger.error(f"GDrive folder setup error: {e}")
            return False

    @classmethod
    async def _find_or_create_folder(
        cls, access_token: str, name: str, parent_id: Optional[str]
    ) -> Optional[str]:
        """Find existing folder or create new one."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # First, try to find existing folder
                query = (
                    f"name='{name}' and mimeType='application/vnd.google-apps.folder'"
                )
                if parent_id:
                    query += f" and '{parent_id}' in parents"
                else:
                    query += " and 'root' in parents"

                response = await client.get(
                    f"{GOOGLE_DRIVE_API_V3}/files",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={
                        "q": query,
                        "fields": "files(id,name)",
                        "spaces": "drive",
                    },
                )

                if response.status_code == 200:
                    files = response.json().get("files", [])
                    if files:
                        logger.info(
                            f"GDrive: Found existing folder '{name}': {files[0]['id']}"
                        )
                        return files[0]["id"]

                # Create new folder
                metadata = {
                    "name": name,
                    "mimeType": "application/vnd.google-apps.folder",
                }
                if parent_id:
                    metadata["parents"] = [parent_id]

                response = await client.post(
                    f"{GOOGLE_DRIVE_API_V3}/files",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=metadata,
                    params={"fields": "id"},
                )

                if response.status_code in (200, 201):
                    folder_id = response.json().get("id")
                    logger.info(f"GDrive: Created folder '{name}': {folder_id}")
                    return folder_id

                logger.error(
                    f"GDrive: Failed to create folder '{name}': {response.text}"
                )
                return None

        except Exception as e:
            logger.error(f"GDrive find/create folder error: {e}")
            return None

    @classmethod
    async def get_folder_id(cls, plan: str = "free") -> Optional[str]:
        """Get folder ID for a plan (free/pro)."""
        if not cls._folder_ids:
            # Try to load from config
            from database.config import get_config

            config = await get_config()
            folders = config.get("gdrive_folders", {})
            cls._folder_ids = folders

        return cls._folder_ids.get(plan.lower(), cls._folder_ids.get("free"))

    @classmethod
    async def get_folder_id(cls, plan: str = "free") -> Optional[str]:
        """Get folder ID for a plan (free/pro)."""
        if not cls._folder_ids:
            # Try to load from database
            from database.gdrive import get_gdrive_folder_ids

            folders = await get_gdrive_folder_ids()
            if folders:
                cls._folder_ids = folders

        return cls._folder_ids.get(plan.lower(), cls._folder_ids.get("free"))

    @classmethod
    async def get_temp_folder_id(cls) -> Optional[str]:
        """Get temp folder ID for processing files."""
        if not cls._folder_ids:
            from database.gdrive import get_gdrive_folder_ids

            folders = await get_gdrive_folder_ids()
            if folders:
                cls._folder_ids = folders

        return cls._folder_ids.get("temp")

    @classmethod
    async def upload_direct(
        cls,
        bot,
        file_id: str,
        filename: str,
        user_plan: str = "free",
    ) -> Optional[Dict[str, Any]]:
        """
        Upload file directly from Telegram to GDrive.
        Uses folder structure based on user plan.
        """
        try:
            access_token = await cls.get_access_token()
            if not access_token:
                logger.error("GDrive: No access token for upload")
                return None

            folder_id = await cls.get_folder_id(user_plan)
            if not folder_id:
                logger.error(f"GDrive: No folder ID for plan {user_plan}")
                return None

            logger.info(f"GDrive: Starting direct upload for {filename} to {user_plan}")

            # Get file info from Telegram
            tg_file = await bot.get_file(file_id)
            file_size = tg_file.file_size or 0

            # Step 1: Create resumable upload session
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            metadata = {
                "name": filename,
                "parents": [folder_id],
            }

            async with httpx.AsyncClient(timeout=60) as client:
                # Initiate resumable session
                init_response = await client.post(
                    f"{GOOGLE_DRIVE_API}/files",
                    headers=headers,
                    json=metadata,
                    params={"uploadType": "resumable"},
                )

                if init_response.status_code != 200:
                    logger.error(f"GDrive init failed: {init_response.text}")
                    return None

                upload_url = init_response.headers.get("location")
                if not upload_url:
                    logger.error("GDrive: No upload URL in response")
                    return None

                # Step 2: Upload file content
                file_content = await tg_file.download()

                headers = {
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(file_content)),
                }
                response = await client.put(
                    upload_url, content=file_content, headers=headers
                )

                if response.status_code not in (200, 201):
                    logger.error(f"GDrive upload failed: {response.status_code}")
                    return None

                result = response.json()

                # Step 3: Make file public and get shareable link
                file_id_gdrive = result.get("id")
                public_url = await cls.make_public(file_id_gdrive, access_token)

                return {
                    "file_id": file_id_gdrive,
                    "filename": filename,
                    "size": file_size,
                    "url": public_url,
                    "web_view_link": result.get("webViewLink"),
                    "folder": user_plan,
                }

        except Exception as e:
            logger.error(f"GDrive upload error: {e}")
            return None

    @classmethod
    async def make_public(cls, file_id: str, access_token: str) -> Optional[str]:
        """Make file publicly accessible and return shareable link."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Set permissions for public access
                permissions = {
                    "role": "reader",
                    "type": "anyone",
                }
                response = await client.post(
                    f"{GOOGLE_DRIVE_API_V3}/files/{file_id}/permissions",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=permissions,
                )

                if response.status_code in (200, 201):
                    # Get the sharing link
                    link_response = await client.get(
                        f"{GOOGLE_DRIVE_API_V3}/files/{file_id}",
                        headers={"Authorization": f"Bearer {access_token}"},
                        params={"fields": "webContentLink,webViewLink"},
                    )
                    if link_response.status_code == 200:
                        data = link_response.json()
                        return data.get("webContentLink") or data.get("webViewLink")
                return None
        except Exception as e:
            logger.error(f"GDrive make public error: {e}")
            return None

    @classmethod
    async def create_folder(
        cls, name: str, parent_id: str = None, access_token: str = None
    ) -> Optional[str]:
        """Create a folder in GDrive and return its ID."""
        try:
            if not access_token:
                access_token = await cls.get_access_token()
            if not access_token:
                return None

            metadata = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id] if parent_id else [],
            }

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{GOOGLE_DRIVE_API_V3}/files",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=metadata,
                    params={"fields": "id"},
                )

                if response.status_code in (200, 201):
                    return response.json().get("id")
                return None
        except Exception as e:
            logger.error(f"GDrive create folder error: {e}")
            return None

    @classmethod
    async def list_files(
        cls, folder_id: str, access_token: str = None
    ) -> Optional[list]:
        """List files in a GDrive folder."""
        try:
            if not access_token:
                access_token = await cls.get_access_token()
            if not access_token:
                return None

            async with httpx.AsyncClient(timeout=30) as client:
                query = f"'{folder_id}' in parents and trashed=false"
                response = await client.get(
                    f"{GOOGLE_DRIVE_API_V3}/files",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={
                        "q": query,
                        "fields": "files(id,name,mimeType,size,webViewLink)",
                    },
                )

                if response.status_code == 200:
                    return response.json().get("files", [])
                return None
        except Exception as e:
            logger.error(f"GDrive list files error: {e}")
            return None

    @classmethod
    async def delete_file(cls, file_id: str, access_token: str = None) -> bool:
        """Delete a file from GDrive."""
        try:
            if not access_token:
                access_token = await cls.get_access_token()
            if not access_token:
                return False

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.delete(
                    f"{GOOGLE_DRIVE_API_V3}/files/{file_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                return response.status_code in (200, 204)
        except Exception as e:
            logger.error(f"GDrive delete error: {e}")
            return False

    @classmethod
    async def download_to_local(
        cls, file_id: str, local_path: str, access_token: str = None
    ) -> Optional[str]:
        """Download a file from GDrive to local disk."""
        try:
            if not access_token:
                access_token = await cls.get_access_token()
            if not access_token:
                return None

            async with httpx.AsyncClient(timeout=3600) as client:
                response = await client.get(
                    f"{GOOGLE_DRIVE_API_V3}/files/{file_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"alt": "media"},
                )

                if response.status_code == 200:
                    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
                    with open(local_path, "wb") as f:
                        f.write(response.content)
                    return local_path
                return None
        except Exception as e:
            logger.error(f"GDrive download error: {e}")
            return None

    @classmethod
    async def refresh_access_token(
        cls, refresh_token: str, client_id: str, client_secret: str
    ) -> Optional[str]:
        """Get new access token using refresh token."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )

                if response.status_code == 200:
                    return response.json().get("access_token")
                return None
        except Exception as e:
            logger.error(f"GDrive token refresh error: {e}")
            return None

    @classmethod
    async def is_configured(cls) -> bool:
        """Check if GDrive is properly configured (DB or env)."""
        # Check database first
        from database.gdrive import get_gdrive_config

        db_config = await get_gdrive_config()
        if db_config and db_config.get("refresh_token"):
            return True

        # Fallback to env vars
        from config.settings import get_settings

        settings = get_settings()
        return bool(
            settings.GDRIVE_CLIENT_ID
            and settings.GDRIVE_CLIENT_SECRET
            and settings.GDRIVE_REFRESH_TOKEN
        )
