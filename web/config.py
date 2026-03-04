"""
Web admin configuration
"""

import os
from pathlib import Path


# Directories
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

# Ensure directories exist
STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)

# Files
LOGIN_PAGE = STATIC_DIR / "pages" / "login.html"
DASHBOARD_PAGE = STATIC_DIR / "pages" / "dashboard.html"
WATCH_PAGE = STATIC_DIR / "pages" / "watch.html"
API_DOC_PAGE = STATIC_DIR / "pages" / "api-doc.html"
ERROR_PAGE = STATIC_DIR / "pages" / "error.html"

# Settings
SESSION_TIMEOUT = 24 * 60 * 60  # 24 hours
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100MB
