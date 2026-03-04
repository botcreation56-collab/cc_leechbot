"""
tests/unit/test_security.py — Unit tests for core/security.py

Covers:
  - sanitize_filename: valid paths, unicode, RTL chars, path traversal payloads
  - validate_url: HTTPS accept, HTTP/scheme rejection, SSRF literal IP blocking
  - validate_url_async: async SSRF checks with mocked DNS
  - validate_filename: length limits, forbidden sequences
  - EncryptionManager: round-trip encrypt/decrypt
"""

import asyncio
import pytest
from unittest.mock import patch, MagicMock

from core.security import (
    sanitize_filename,
    validate_filename,
    validate_url,
    validate_url_async,
    EncryptionManager,
)
from core.exceptions import InvalidFilenameError, ConfigurationError


# ===========================================================================
# sanitize_filename
# ===========================================================================

class TestSanitizeFilename:

    def test_normal_name(self):
        assert sanitize_filename("my_video.mp4") == "my_video.mp4"

    def test_empty_returns_sentinel(self):
        assert sanitize_filename("") == "unnamed_file"
        assert sanitize_filename(None) == "unnamed_file"  # type: ignore

    def test_path_traversal_stripped(self):
        result = sanitize_filename("../../etc/passwd")
        assert ".." not in result
        assert "/" not in result
        assert result != ""

    def test_windows_path_traversal(self):
        result = sanitize_filename("..\\..\\Windows\\System32\\cmd.exe")
        assert ".." not in result
        assert "\\" not in result

    def test_null_byte_removed(self):
        result = sanitize_filename("evil\x00file.mp4")
        assert "\x00" not in result

    def test_unicode_kanji(self):
        # Non-ASCII characters are replaced with underscores, not crashing
        result = sanitize_filename("映画.mp4")
        assert isinstance(result, str)
        assert len(result) > 0
        assert result != "unnamed_file"

    def test_rtl_characters(self):
        # Right-to-left override characters should be sanitized away
        result = sanitize_filename("\u202eevil.mp4")
        assert "\u202e" not in result

    def test_emoji_in_filename(self):
        result = sanitize_filename("🎬movie.mp4")
        assert isinstance(result, str)

    def test_max_length_255(self):
        long_name = "a" * 300 + ".mp4"
        result = sanitize_filename(long_name)
        assert len(result) <= 255

    def test_leading_dash_kept(self):
        # Dashes are valid in filenames
        result = sanitize_filename("-video.mp4")
        assert isinstance(result, str)

    def test_command_injection_attempt(self):
        result = sanitize_filename("--output=/etc/passwd")
        assert "/" not in result
        # At minimum it survives without crashing
        assert isinstance(result, str)


# ===========================================================================
# validate_filename
# ===========================================================================

class TestValidateFilename:

    def test_valid_filename_passes(self):
        validate_filename("good_file.mp4")  # must not raise

    def test_empty_raises(self):
        with pytest.raises(InvalidFilenameError):
            validate_filename("")

    def test_too_long_raises(self):
        with pytest.raises(InvalidFilenameError):
            validate_filename("a" * 256)

    def test_path_traversal_raises(self):
        with pytest.raises(InvalidFilenameError):
            validate_filename("../../etc/passwd")

    def test_null_byte_raises(self):
        with pytest.raises(InvalidFilenameError):
            validate_filename("evil\x00.txt")

    def test_slash_raises(self):
        with pytest.raises(InvalidFilenameError):
            validate_filename("dir/file.mp4")

    def test_backslash_raises(self):
        with pytest.raises(InvalidFilenameError):
            validate_filename("dir\\file.mp4")


# ===========================================================================
# validate_url (synchronous, no DNS)
# ===========================================================================

class TestValidateUrl:

    def test_valid_https_url(self):
        ok, reason = validate_url("https://example.com/file.mp4")
        assert ok is True
        assert reason == ""

    def test_http_rejected(self):
        ok, reason = validate_url("http://example.com/file.mp4")
        assert ok is False
        assert "HTTPS" in reason

    def test_javascript_scheme_rejected(self):
        ok, reason = validate_url("javascript:alert(1)")
        assert ok is False
        assert "javascript" in reason.lower()

    def test_file_scheme_rejected(self):
        ok, reason = validate_url("file:///etc/passwd")
        assert ok is False

    def test_ftp_scheme_rejected(self):
        ok, reason = validate_url("ftp://example.com/file.mp4")
        assert ok is False

    def test_empty_url_rejected(self):
        ok, reason = validate_url("")
        assert ok is False
        assert "Empty" in reason

    def test_none_rejected(self):
        ok, reason = validate_url(None)  # type: ignore
        assert ok is False

    def test_literal_loopback_ip_blocked(self):
        ok, reason = validate_url("https://127.0.0.1/evil")
        assert ok is False
        assert "SSRF" in reason

    def test_literal_private_ip_blocked(self):
        ok, reason = validate_url("https://192.168.1.1/admin")
        assert ok is False
        assert "SSRF" in reason

    def test_aws_metadata_ip_blocked(self):
        ok, reason = validate_url("https://169.254.169.254/latest/meta-data/")
        assert ok is False
        assert "SSRF" in reason

    def test_ipv6_loopback_blocked(self):
        ok, reason = validate_url("https://[::1]/evil")
        assert ok is False

    def test_hostname_passes_sync(self):
        # Sync variant cannot do DNS — public hostnames pass at this stage
        ok, reason = validate_url("https://cdn.example.com/large_file.mkv")
        assert ok is True

    def test_url_with_null_byte_in_hostname_rejected(self):
        # Null byte in the hostname is explicitly blocked
        ok, reason = validate_url("https://evil\x00.com/file")
        assert ok is False

    def test_url_with_newline_in_hostname_rejected(self):
        # Newline in the hostname must be rejected to prevent header injection
        ok, reason = validate_url("https://evil\n.com/file")
        assert ok is False


# ===========================================================================
# validate_url_async (DNS included, mocked)
# ===========================================================================

class TestValidateUrlAsync:

    @pytest.mark.asyncio
    async def test_valid_url_passes(self):
        with patch("core.security._async_ip_is_private", return_value=False):
            ok, reason = await validate_url_async("https://cdn.example.com/video.mp4")
        assert ok is True

    @pytest.mark.asyncio
    async def test_private_hostname_blocked(self):
        with patch("core.security._async_ip_is_private", return_value=True):
            ok, reason = await validate_url_async("https://internal-service.local/api")
        assert ok is False
        assert "SSRF" in reason

    @pytest.mark.asyncio
    async def test_invalid_scheme_fast_rejected_without_dns(self):
        # DNS should never be reached for clearly invalid schemes
        with patch("core.security._async_ip_is_private") as mock_dns:
            ok, reason = await validate_url_async("http://example.com/file")
        mock_dns.assert_not_called()
        assert ok is False


# ===========================================================================
# EncryptionManager
# ===========================================================================

class TestEncryptionManager:

    def _manager(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        return EncryptionManager.from_key(key)

    def test_round_trip(self):
        mgr = self._manager()
        payload = {"password": "super_secret", "token": "abc123"}
        blob = mgr.encrypt(payload)
        result = mgr.decrypt(blob)
        assert result == payload

    def test_tampered_blob_raises(self):
        mgr = self._manager()
        blob = mgr.encrypt({"x": 1})
        with pytest.raises(ValueError):
            mgr.decrypt(blob[:-4] + "AAAA")

    def test_from_env_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        with pytest.raises(ConfigurationError):
            EncryptionManager.from_env()

    def test_from_env_invalid_key_raises(self, monkeypatch):
        monkeypatch.setenv("ENCRYPTION_KEY", "this-is-not-a-valid-fernet-key")
        with pytest.raises(ConfigurationError):
            EncryptionManager.from_env()
