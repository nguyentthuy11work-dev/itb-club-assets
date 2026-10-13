"""Environment-backed application settings."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ams.infrastructure.authentication import TokenSettings


class ApiSettings(BaseSettings):
    """Validate required production configuration from ``AMS_*`` variables."""

    model_config = SettingsConfigDict(
        env_prefix="AMS_",
        extra="ignore",
        frozen=True,
    )

    database_url: str
    jwt_secret: SecretStr
    jwt_issuer: str = "ams-api"
    jwt_audience: str = "ams-web"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    refresh_cookie_secure: bool = True
    cors_allowed_origins: tuple[str, ...] = ()
    log_level: str = "INFO"
    evidence_storage_root: Path = Path(".private/evidence")

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_cors_origins(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        """Forbid wildcard credential sharing and malformed browser origins."""

        for origin in origins:
            if origin == "*":
                raise ValueError("Wildcard CORS origins are forbidden with credentials.")
            if not origin.startswith(("http://", "https://")):
                raise ValueError("CORS origins must use http or https.")
        return origins

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, level: str) -> str:
        """Normalize supported structured-log levels."""

        normalized = level.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Unsupported log level.")
        return normalized

    def to_token_settings(self) -> TokenSettings:
        """Map environment configuration to the infrastructure token adapter."""

        return TokenSettings(
            secret=self.jwt_secret.get_secret_value(),
            issuer=self.jwt_issuer,
            audience=self.jwt_audience,
            access_ttl=timedelta(minutes=self.access_token_minutes),
            refresh_ttl=timedelta(days=self.refresh_token_days),
        )
