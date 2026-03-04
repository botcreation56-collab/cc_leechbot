"""
tests/conftest.py — Shared pytest fixtures for unit and integration tests.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── Make the project root importable without installing the package ──────────
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Event-loop policy: use a fresh loop per test module (avoids loop reuse errors)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


# ---------------------------------------------------------------------------
# Isolated temporary directory
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    """An isolated temporary directory, auto-cleaned after each test."""
    return tmp_path


# ---------------------------------------------------------------------------
# Minimal settings stub (avoids touching real env vars)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_settings(monkeypatch):
    """Patch environment variables so Settings can be imported without a real .env file."""
    monkeypatch.setenv("BOT_TOKEN", "1234567890:AAFakeTokenForTesting")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017/test_filebot")
    monkeypatch.setenv("ENCRYPTION_KEY", "Rz3l7XkYqP1mB2wNvJcH5dGsT8oFaE6iUyQbMnVhLA0=")
    monkeypatch.setenv("WEBHOOK_SECRET", "test_secret_token_abc123")
    monkeypatch.setenv("ADMIN_IDS", "123456789")
    monkeypatch.setenv("DOMAIN", "https://example.ngrok.io")
    monkeypatch.setenv("API_ID", "12345")
    monkeypatch.setenv("API_HASH", "abc123def456abc123def456abc123de")
    yield


# ---------------------------------------------------------------------------
# Mock asyncio subprocess
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_process():
    """A mock asyncio.subprocess.Process that exits cleanly."""
    proc = AsyncMock()
    proc.returncode = 0
    proc.stdout = AsyncMock()
    proc.stderr = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    return proc
