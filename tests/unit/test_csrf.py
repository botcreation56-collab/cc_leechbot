"""
tests/unit/test_csrf.py — Unit tests for web/utils/csrf.py

Covers:
  - CSRFProtector: token generation and validation
  - BruteForceProtection: attack detection, delays, lockouts
  - generate_csrf_token / validate_csrf_token helpers
"""

import pytest
import time

from web.utils.csrf import (
    CSRFProtector,
    BruteForceProtection,
    generate_csrf_token,
    validate_csrf_token,
)


class TestCSRFProtector:
    """Tests for CSRF token generation and validation."""

    def test_generate_token_returns_string(self):
        token = CSRFProtector.generate_token("session_123")
        assert isinstance(token, str)
        assert len(token) > 20

    def test_generate_token_unique(self):
        t1 = CSRFProtector.generate_token("session_123")
        t2 = CSRFProtector.generate_token("session_123")
        assert t1 != t2

    def test_validate_token_valid(self):
        session_id = "test_session_456"
        token = CSRFProtector.generate_token(session_id)
        assert CSRFProtector.validate_token(session_id, token) is True

    def test_validate_token_invalid(self):
        session_id = "test_session_789"
        CSRFProtector.generate_token(session_id)
        assert CSRFProtector.validate_token(session_id, "wrong_token") is False

    def test_validate_token_empty_session(self):
        assert CSRFProtector.validate_token("", "any_token") is False

    def test_validate_token_none_session(self):
        assert CSRFProtector.validate_token(None, "any_token") is False  # type: ignore

    def test_validate_token_empty_token(self):
        assert CSRFProtector.validate_token("session", "") is False

    def test_validate_token_wrong_session(self):
        CSRFProtector.generate_token("session_a")
        assert CSRFProtector.validate_token("session_b", "any_token") is False

    def test_get_token_existing(self):
        session_id = "get_token_session"
        token = CSRFProtector.generate_token(session_id)
        assert CSRFProtector.get_token(session_id) == token

    def test_get_token_nonexistent(self):
        assert CSRFProtector.get_token("nonexistent_session") is None


class TestBruteForceProtection:
    """Tests for brute force protection mechanism."""

    def setup_method(self):
        BruteForceProtection._attempts.clear()
        BruteForceProtection._lockouts.clear()
        BruteForceProtection._progressive_delays.clear()

    def test_check_allowed_initially(self):
        allowed, delay, msg = BruteForceProtection.check("user_123")
        assert allowed is True
        assert delay is None
        assert msg is None

    def test_record_failure_increments(self):
        identifier = "bf_test_1"
        BruteForceProtection.record_failure(identifier)
        allowed, _, _ = BruteForceProtection.check(identifier)
        assert allowed is False

    def test_max_attempts_then_lockout(self):
        identifier = "bf_test_2"
        for _ in range(BruteForceProtection.MAX_ATTEMPTS):
            BruteForceProtection.record_failure(identifier)

        allowed, _, msg = BruteForceProtection.check(identifier)
        assert allowed is False
        assert "Locked" in msg or "locked" in msg.lower()

    def test_record_success_clears(self):
        identifier = "bf_test_3"
        BruteForceProtection.record_failure(identifier)
        BruteForceProtection.record_failure(identifier)
        BruteForceProtection.record_success(identifier)

        allowed, _, _ = BruteForceProtection.check(identifier)
        assert allowed is True

    def test_progressive_delay_enforced(self):
        identifier = "bf_test_4"
        BruteForceProtection.record_failure(identifier)
        BruteForceProtection.record_failure(identifier)

        allowed, delay, msg = BruteForceProtection.check(identifier)
        assert allowed is False
        assert delay is not None
        assert delay > 0

    def test_lockout_duration(self):
        identifier = "bf_test_5"
        for _ in range(BruteForceProtection.MAX_ATTEMPTS):
            BruteForceProtection.record_failure(identifier)

        _, _, msg = BruteForceProtection.check(identifier)
        assert "15 minutes" in msg or "seconds" in msg


class TestCSRFHelpers:
    """Tests for the public helper functions."""

    def test_generate_csrf_token_from_session_token(self):
        session_token = "my_secret_session_token_12345"
        csrf = generate_csrf_token(session_token)
        assert isinstance(csrf, str)
        assert len(csrf) > 20

    def test_validate_csrf_token_valid(self):
        session_token = "my_secret_session_token_67890"
        csrf = generate_csrf_token(session_token)
        assert validate_csrf_token(session_token, csrf) is True

    def test_validate_csrf_token_invalid(self):
        session_token = "my_secret_session_token_abcde"
        csrf = generate_csrf_token(session_token)
        assert validate_csrf_token(session_token, "wrong_csrf") is False

    def test_validate_csrf_token_different_session(self):
        csrf = generate_csrf_token("session_a")
        assert validate_csrf_token("session_b", csrf) is False
