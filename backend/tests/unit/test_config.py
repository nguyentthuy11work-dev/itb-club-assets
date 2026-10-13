"""Unit tests for strict environment-backed API configuration."""

from __future__ import annotations

import unittest

from pydantic import SecretStr, ValidationError

from ams.infrastructure.config import ApiSettings

DATABASE_URL = "postgresql+asyncpg://ams:secret@localhost/ams"
JWT_SECRET = SecretStr("test-only-secret-that-is-at-least-32-bytes-long")


class ApiSettingsTests(unittest.TestCase):
    """Verify secure CORS and logging configuration boundaries."""

    def test_explicit_http_origins_and_log_level_are_normalized(self) -> None:
        """Valid browser origins remain explicit and log levels become uppercase."""

        settings = ApiSettings(
            database_url=DATABASE_URL,
            jwt_secret=JWT_SECRET,
            cors_allowed_origins=("http://localhost:5173",),
            log_level="warning",
        )

        self.assertEqual(settings.cors_allowed_origins, ("http://localhost:5173",))
        self.assertEqual(settings.log_level, "WARNING")

    def test_wildcard_and_non_http_origins_are_rejected(self) -> None:
        """Credentialed CORS cannot use wildcard or non-browser origin formats."""

        for origin in ("*", "localhost:5173"):
            with self.subTest(origin=origin), self.assertRaises(ValidationError):
                ApiSettings(
                    database_url=DATABASE_URL,
                    jwt_secret=JWT_SECRET,
                    cors_allowed_origins=(origin,),
                )

    def test_unknown_log_level_is_rejected(self) -> None:
        """A typo cannot silently disable or over-enable structured logging."""

        with self.assertRaises(ValidationError):
            ApiSettings(
                database_url=DATABASE_URL,
                jwt_secret=JWT_SECRET,
                log_level="verbose",
            )
