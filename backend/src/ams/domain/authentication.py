"""Authentication domain records and stable application-facing errors."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ams.domain.identity import OperationalRole


class AuthenticationError(Exception):
    """Base class for expected authentication failures."""

    code = "AUTHENTICATION_FAILED"


class InvalidCredentialsError(AuthenticationError):
    """Raised when a local username/email and password pair is invalid."""

    code = "INVALID_CREDENTIALS"


class TokenExpiredError(AuthenticationError):
    """Raised when a token expired, was revoked, or no longer identifies a session."""

    code = "TOKEN_EXPIRED"


class AccountInactiveError(AuthenticationError):
    """Raised when valid credentials identify a disabled account."""

    code = "ACCOUNT_INACTIVE"


@dataclass(frozen=True, slots=True)
class UserIdentity:
    """Current persisted identity data without credential material."""

    id: UUID
    username: str
    email: str
    display_name: str
    roles: frozenset[OperationalRole]
    team_ids: frozenset[UUID]
    is_active: bool


@dataclass(frozen=True, slots=True)
class LocalCredential:
    """A local identity plus the encoded password verifier loaded for authentication."""

    user: UserIdentity
    password_hash: str
