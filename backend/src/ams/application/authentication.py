"""Authentication ports, DTOs, and session orchestration."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ams.domain.authentication import (
    AccountInactiveError,
    InvalidCredentialsError,
    TokenExpiredError,
    UserIdentity,
)
from ams.domain.identity import CurrentActor
from ams.domain.repositories import IUserRepository


@dataclass(frozen=True, slots=True)
class TokenPair:
    """A short-lived access token and a rotating refresh token."""

    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Verified token identity used by the application layer."""

    subject: UUID
    session_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Successful login result consumed later by the API DTO mapper."""

    user: UserIdentity
    tokens: TokenPair


class IIdentityProvider(ABC):
    """Authenticate credentials without exposing hashing details to use cases."""

    @abstractmethod
    async def authenticate(self, login: str, password: str) -> UserIdentity:
        """Return an identity or raise ``InvalidCredentialsError``."""


class IPasswordHasher(ABC):
    """Hash and verify local passwords using a replaceable safe implementation."""

    @abstractmethod
    def hash(self, password: str) -> str:
        """Return an encoded, salted password hash."""

    @abstractmethod
    def verify(self, password: str, encoded_hash: str) -> bool:
        """Return whether a password matches without leaking malformed-hash errors."""


@dataclass(frozen=True, slots=True)
class RefreshSession:
    """Persisted refresh-token state; only a digest, never the token, is stored."""

    id: UUID
    user_id: UUID
    token_digest: str
    expires_at: datetime
    created_at: datetime


class IRefreshSessionRepository(ABC):
    """Persist refresh sessions and perform atomic rotation/revocation."""

    @abstractmethod
    async def add(self, session: RefreshSession) -> None:
        """Persist a newly issued refresh session."""

    @abstractmethod
    async def is_active(self, session_id: UUID, token_digest: str, now: datetime) -> bool:
        """Return whether the exact unexpired session is active."""

    @abstractmethod
    async def rotate(
        self,
        current_session_id: UUID,
        current_token_digest: str,
        replacement: RefreshSession,
        now: datetime,
    ) -> bool:
        """Atomically revoke the current session and store its replacement once."""

    @abstractmethod
    async def revoke(self, session_id: UUID, token_digest: str, now: datetime) -> bool:
        """Atomically revoke one matching active session."""


class ITokenService(ABC):
    """Issue, validate, rotate, and revoke signed bearer-session tokens."""

    @abstractmethod
    async def issue(self, user_id: UUID) -> TokenPair:
        """Create a new access/refresh token pair for a user."""

    @abstractmethod
    async def verify_access_token(self, token: str) -> TokenClaims:
        """Verify an access token's signature, claims, issuer, audience, and expiry."""

    @abstractmethod
    async def verify_refresh_token(self, token: str) -> TokenClaims:
        """Verify a refresh token and its persisted active session."""

    @abstractmethod
    async def rotate_refresh_token(self, token: str) -> TokenPair:
        """Consume a refresh token exactly once and issue a replacement pair."""

    @abstractmethod
    async def revoke_refresh_token(self, token: str) -> None:
        """Revoke a refresh token or raise ``TokenExpiredError``."""


class SessionService:
    """Coordinate login and session lifecycle against current account state."""

    def __init__(
        self,
        identity_provider: IIdentityProvider,
        token_service: ITokenService,
        user_repository: IUserRepository,
    ) -> None:
        self._identity_provider = identity_provider
        self._token_service = token_service
        self._user_repository = user_repository

    async def login(self, login: str, password: str) -> LoginResult:
        """Authenticate local credentials and create a new session."""

        user = await self._identity_provider.authenticate(login, password)
        self._assert_active(user)
        tokens = await self._token_service.issue(user.id)
        return LoginResult(user=user, tokens=tokens)

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Rotate a valid refresh token after rechecking the account state."""

        claims = await self._token_service.verify_refresh_token(refresh_token)
        user = await self._load_session_user(claims.subject)
        self._assert_active(user)
        return await self._token_service.rotate_refresh_token(refresh_token)

    async def logout(self, refresh_token: str) -> None:
        """Revoke the supplied refresh session."""

        await self._token_service.revoke_refresh_token(refresh_token)

    async def get_current_actor(self, access_token: str) -> CurrentActor:
        """Resolve a token to fresh roles/team memberships from persistence."""

        claims = await self._token_service.verify_access_token(access_token)
        user = await self._load_session_user(claims.subject)
        self._assert_active(user)
        return CurrentActor(
            id=user.id,
            roles=user.roles,
            team_ids=user.team_ids,
            is_active=user.is_active,
            display_name=user.display_name,
        )

    async def _load_session_user(self, user_id: UUID) -> UserIdentity:
        user = await self._user_repository.get_identity(user_id)
        if user is None:
            raise TokenExpiredError("Session is no longer valid.")
        return user

    @staticmethod
    def _assert_active(user: UserIdentity) -> None:
        if not user.is_active:
            raise AccountInactiveError("Account is inactive.")


__all__ = [
    "IIdentityProvider",
    "IPasswordHasher",
    "IRefreshSessionRepository",
    "ITokenService",
    "LoginResult",
    "RefreshSession",
    "SessionService",
    "TokenClaims",
    "TokenPair",
    "InvalidCredentialsError",
]
