"""
core/security.py — Consolidated security primitives.

Provides:
  - EncryptionManager  : Fernet-based symmetric encryption for credentials
  - TokenGenerator     : Cryptographically secure token generation
  - URLValidator       : SSRF-safe URL validation (HTTPS-only)
  - FilenameValidator  : Path-traversal safe filename sanitizer
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

from core.exceptions import (
    ConfigurationError,
    InvalidFilenameError,
    InvalidURLError,
)

logger = logging.getLogger("filebot.security")

# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


class EncryptionManager:
    """Fernet-based symmetric encryption for storing sensitive credentials.

    Usage:
        manager = EncryptionManager.from_env()
        blob = manager.encrypt({"password": "hunter2"})
        data = manager.decrypt(blob)
    """

    def __init__(self, key: bytes) -> None:
        self._cipher = Fernet(key)

    # --- factory -----------------------------------------------------------

    @classmethod
    def from_env(cls, env_var: str = "ENCRYPTION_KEY") -> "EncryptionManager":
        """Build from environment variable (raises ConfigurationError if missing)."""
        raw = os.getenv(env_var, "")
        if not raw:
            raise ConfigurationError(
                env_var,
                'ENCRYPTION_KEY not set. Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"',
            )
        try:
            key_bytes = raw.encode() if isinstance(raw, str) else raw
            Fernet(key_bytes)  # validate key format before storing
            return cls(key_bytes)
        except Exception as exc:
            raise ConfigurationError(
                env_var, f"Invalid Fernet key format: {exc}"
            ) from exc

    @classmethod
    def from_key(cls, key: str) -> "EncryptionManager":
        """Build from an explicit string key."""
        try:
            key_bytes = key.encode()
            return cls(key_bytes)
        except Exception as exc:
            raise ConfigurationError("ENCRYPTION_KEY", f"Invalid key: {exc}") from exc

    # --- operations --------------------------------------------------------

    def encrypt(self, data: Dict[str, Any]) -> str:
        """Serialise dict → encrypt → return base64 token string."""
        try:
            payload = json.dumps(data, ensure_ascii=False).encode()
            return self._cipher.encrypt(payload).decode()
        except Exception as exc:
            logger.error("Encryption failed: %s", exc)
            raise

    def decrypt(self, token: str) -> Dict[str, Any]:
        """Decrypt base64 token → deserialise and return dict."""
        try:
            raw = self._cipher.decrypt(token.encode())
            return json.loads(raw.decode())
        except InvalidToken as exc:
            logger.warning("Decryption failed: invalid or corrupted token")
            raise ValueError("Invalid or corrupted encryption token") from exc
        except Exception as exc:
            logger.error("Decryption failed: %s", exc)
            raise


# Singleton (initialised lazily)
_encryption_manager: EncryptionManager | None = None


def get_encryption_manager() -> EncryptionManager:
    """Return the process-wide EncryptionManager singleton."""
    global _encryption_manager
    if _encryption_manager is None:
        _encryption_manager = EncryptionManager.from_env()
    return _encryption_manager


def encrypt_credentials(credentials: Dict[str, Any]) -> str:
    return get_encryption_manager().encrypt(credentials)


def decrypt_credentials(token: str) -> Dict[str, Any]:
    return get_encryption_manager().decrypt(token)


def encrypt_token(token: str) -> str:
    """Encrypt a simple token string for URL-safe storage."""
    return get_encryption_manager().encrypt({"token": token})


def decrypt_token(encrypted: str) -> str:
    """Decrypt an encrypted token string."""
    data = get_encryption_manager().decrypt(encrypted)
    return data.get("token", "")


# ---------------------------------------------------------------------------
# Token Generation
# ---------------------------------------------------------------------------


class TokenGenerator:
    """Factory for cryptographically secure tokens."""

    @staticmethod
    def url_safe(nbytes: int = 32) -> str:
        """Return a URL-safe base64 token (default 256-bit entropy)."""
        return secrets.token_urlsafe(nbytes)

    @staticmethod
    def hex(nbytes: int = 32) -> str:
        """Return a 64-character hex token."""
        return secrets.token_hex(nbytes)

    @staticmethod
    def otp(digits: int = 6) -> str:
        """Return a numeric OTP code of `digits` length."""
        range_start = 10 ** (digits - 1)
        range_end = (10**digits) - 1
        return str(secrets.randbelow(range_end - range_start) + range_start)


# ---------------------------------------------------------------------------
# URL Validation — SSRF Protection
# ---------------------------------------------------------------------------

# RFC 1918 / RFC 4193 / RFC 5737 private ranges
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),  # loopback IPv4
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("0.0.0.0/8"),  # "this network"
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT (shared address space)
    ipaddress.ip_network("198.18.0.0/15"),  # Benchmark testing
]

_BLOCKED_SCHEMES = frozenset(
    {"javascript", "data", "file", "ftp", "gopher", "dict", "ldap", "sftp"}
)
_URL_RE = re.compile(r"^https://[^\s/$.?#][^\s]*$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Thread pool for DNS resolution (keeps event loop unblocked)
# ---------------------------------------------------------------------------
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="dns-check"
)
_DNS_RESOLVE_TIMEOUT = 3.0  # seconds


def _sync_ip_is_private(host: str) -> bool:
    """Synchronous DNS check — ONLY call inside an executor thread."""
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        pass  # host is a domain name — resolve via DNS

    try:
        for _fam, _type, _proto, _canon, sockaddr in socket.getaddrinfo(host, None):
            try:
                addr = ipaddress.ip_address(sockaddr[0])
                if any(addr in net for net in _PRIVATE_NETWORKS):
                    return True
            except ValueError:
                continue
    except Exception:
        pass  # DNS failure → treat as routable (caller may still reject)

    return False


async def _async_ip_is_private(host: str) -> bool:
    """Async SSRF check — delegates blocking DNS to a thread pool with a hard timeout.

    Never blocks the event loop regardless of DNS server latency.
    """
    # Fast path: literal IP (no DNS needed, always sync-safe)
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        pass

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_DNS_EXECUTOR, _sync_ip_is_private, host),
            timeout=_DNS_RESOLVE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "DNS resolution timed out for host %r — treating as private (fail-safe)",
            host,
        )
        return True  # fail-secure: block on timeout
    except Exception as exc:
        logger.debug("DNS check failed for %r: %s — treating as routable", host, exc)
        return False


def validate_url(url: str) -> Tuple[bool, str]:
    """Synchronous URL validation (scheme + format only, no DNS lookup).

    Use ``validate_url_async`` for full SSRF protection including DNS resolution.
    This sync variant is kept for backwards-compatibility with non-async callers
    (e.g., message handlers that cannot await). It validates everything *except*
    private-IP resolution — callers should prefer ``validate_url_async`` where possible.
    """
    if not url or not url.strip():
        return False, "Empty URL"

    url = url.strip()

    scheme = url.split(":")[0].lower()
    if scheme in _BLOCKED_SCHEMES:
        return False, f"Blocked URL scheme: '{scheme}'"
    if scheme != "https":
        return False, "Only HTTPS URLs are accepted"

    if not _URL_RE.match(url):
        return False, "Invalid URL format"

    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname or ""
    except Exception:
        return False, "Could not parse URL"

    if not host:
        return False, "URL contains no hostname"

    if any(c in host for c in ("\x00", "\n", "\r", " ")):
        return False, "Hostname contains illegal characters"

    # Synchronous literal-IP SSRF check (no DNS, always safe to call from sync ctx)
    try:
        addr = ipaddress.ip_address(host)
        if any(addr in net for net in _PRIVATE_NETWORKS):
            return False, f"SSRF blocked: '{host}' is a private or reserved address"
    except ValueError:
        pass  # hostname — DNS check requires the async variant

    return True, ""


async def validate_url_async(url: str) -> Tuple[bool, str]:
    """Full async SSRF-safe URL validation including DNS resolution.

    Preferred over ``validate_url`` in async contexts — resolves hostnames via
    a bounded thread pool so the event loop is never blocked.
    """
    ok, reason = validate_url(url)  # fast sync checks first
    if not ok:
        return ok, reason

    try:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
    except Exception:
        return False, "Could not parse URL"

    if await _async_ip_is_private(host):
        return (
            False,
            f"SSRF blocked: '{host}' resolves to a private or reserved address",
        )

    return True, ""


# ---------------------------------------------------------------------------
# Filename Sanitisation
# ---------------------------------------------------------------------------

_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._\-]")
_DANGEROUS_SEQUENCES = ("..", "/", "\\", "\x00", "\n", "\r")


def sanitize_filename(filename: str) -> str:
    """Strip path traversal, null bytes, and illegal characters from *filename*.

    Returns a safe filename ≤ 255 characters. Never raises; returns a
    fallback sentinel when the result would be empty.
    """
    if not filename:
        return "unnamed_file"
    # Strip directory components
    filename = os.path.basename(filename)
    # Replace every unsafe character with underscore
    clean = _SAFE_FILENAME_RE.sub("_", filename)
    # Collapse consecutive underscores and strip leading/trailing dots+underscores
    clean = re.sub(r"_+", "_", clean).strip("._")
    return clean[:255] or "unnamed_file"


def validate_filename(filename: str) -> None:
    """Raise ``InvalidFilenameError`` if *filename* contains dangerous sequences.

    This is intentionally strict: prefer sanitize_filename() for display,
    and validate_filename() as a last-line guard before filesystem access.
    """
    if not filename or len(filename) > 255:
        raise InvalidFilenameError(filename, "Filename length must be 1–255 characters")
    for seq in _DANGEROUS_SEQUENCES:
        if seq in filename:
            raise InvalidFilenameError(
                filename, f"Forbidden sequence in filename: {repr(seq)}"
            )


def safe_path(base_dir: str, filename: str) -> str:
    """Resolve *filename* relative to *base_dir* and assert it stays inside.

    Raises ``ValueError`` on any path-traversal attempt (including %2e%2e and
    symlink-based escapes).
    """
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    # target must start with base (be inside the tree) or equal base itself
    if base not in target.parents and base != target:
        raise ValueError(
            f"Path traversal blocked: {filename!r} would escape {base_dir!r}"
        )
    return str(target)
