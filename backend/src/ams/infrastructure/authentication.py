"""Local-password and JWT authentication adapters."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ams.application.authentication import (
    IIdentityProvider,
    IPasswordHasher,
    IRefreshSessionRepository,
    ITokenService,
    RefreshSession,
    TokenClaims,
    TokenPair,
)
from ams.domain.authentication import InvalidCredentialsError, TokenExpiredError, UserIdentity
from ams.domain.repositories import IUserRepository


@dataclass(frozen=True, slots=True)
class TokenSettings:
    """Validated JWT/session settings supplied from environment configuration."""

    secret: str
    issuer: str = "ams-api"
    audience: str = "ams-web"
    access_ttl: timedelta = timedelta(minutes=15)
    refresh_ttl: timedelta = timedelta(days=7)
    algorithm: str = "HS256"

    def __post_init__(self) -> None:
        """Reject unsafe or ambiguous token configuration at startup."""

        if len(self.secret.encode("utf-8")) < 32:
            raise ValueError("JWT secret must contain at least 32 bytes.")
        if not self.issuer.strip() or not self.audience.strip():
            raise ValueError("JWT issuer and audience must not be blank.")
        if self.access_ttl <= timedelta(0) or self.refresh_ttl <= self.access_ttl:
            raise ValueError("Refresh TTL must be longer than a positive access TTL.")
        if self.algorithm != "HS256":
            raise ValueError("Only the configured HS256 algorithm is supported.")


class Argon2PasswordHasher(IPasswordHasher):
    """Hash local passwords with Argon2id using library-maintained safe defaults."""

    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher()

    def hash(self, password: str) -> str:
        """Return an encoded Argon2id hash; blank passwords are rejected."""

        if not password:
            raise ValueError("Password must not be blank.")
        return self._hasher.hash(password)

    def verify(self, password: str, encoded_hash: str) -> bool:
        """Verify a hash and collapse mismatch/malformed encodings to ``False``."""

        try:
            return self._hasher.verify(encoded_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False


class LocalIdentityProvider(IIdentityProvider):
    """Authenticate approved local credentials through an injected user repository."""

    def __init__(self, users: IUserRepository, password_hasher: IPasswordHasher) -> None:
        self._users = users
        self._password_hasher = password_hasher
        self._dummy_password_hash = password_hasher.hash("ams-authentication-dummy-password")

    async def authenticate(self, login: str, password: str) -> UserIdentity:
        """Return the matched identity with one generic failure for either credential."""

        normalized_login = login.strip()
        if not normalized_login or not password:
            raise InvalidCredentialsError("Username or password is invalid.")

        credential = await self._users.get_local_credential(normalized_login)
        encoded_hash = (
            credential.password_hash if credential is not None else self._dummy_password_hash
        )
        password_matches = self._password_hasher.verify(password, encoded_hash)
        if credential is None or not password_matches:
            raise InvalidCredentialsError("Username or password is invalid.")
        return credential.user


class JwtTokenService(ITokenService):
    """Issue signed JWT access tokens and persisted, rotating refresh sessions."""

    def __init__(
        self,
        settings: TokenSettings,
        sessions: IRefreshSessionRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))

    async def issue(self, user_id: UUID) -> TokenPair:
        """Create and persist a new refresh session with a related access token."""

        now = self._now()
        session_id = uuid4()
        pair, session = self._build_pair(user_id, session_id, now)
        await self._sessions.add(session)
        return pair

    async def verify_access_token(self, token: str) -> TokenClaims:
        """Verify a signed access JWT and return typed claims."""

        return self._decode(token, expected_type="access")

    async def verify_refresh_token(self, token: str) -> TokenClaims:
        """Verify a refresh JWT and confirm its exact digest remains active."""

        claims = self._decode(token, expected_type="refresh")
        active = await self._sessions.is_active(
            claims.session_id, self._digest(token), self._now()
        )
        if not active:
            raise TokenExpiredError("Refresh token has expired.")
        return claims

    async def rotate_refresh_token(self, token: str) -> TokenPair:
        """Atomically consume an active refresh session and issue its successor."""

        claims = self._decode(token, expected_type="refresh")
        now = self._now()
        replacement_id = uuid4()
        pair, replacement = self._build_pair(claims.subject, replacement_id, now)
        rotated = await self._sessions.rotate(
            claims.session_id,
            self._digest(token),
            replacement,
            now,
        )
        if not rotated:
            raise TokenExpiredError("Refresh token has expired.")
        return pair

    async def revoke_refresh_token(self, token: str) -> None:
        """Revoke the exact active refresh token without accepting access tokens."""

        claims = self._decode(token, expected_type="refresh")
        revoked = await self._sessions.revoke(
            claims.session_id, self._digest(token), self._now()
        )
        if not revoked:
            raise TokenExpiredError("Session is no longer valid.")

    def _build_pair(
        self, user_id: UUID, session_id: UUID, now: datetime
    ) -> tuple[TokenPair, RefreshSession]:
        access_expires_at = now + self._settings.access_ttl
        refresh_expires_at = now + self._settings.refresh_ttl
        access_token = self._encode(user_id, session_id, "access", now, access_expires_at)
        refresh_token = self._encode(user_id, session_id, "refresh", now, refresh_expires_at)
        pair = TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
        )
        session = RefreshSession(
            id=session_id,
            user_id=user_id,
            token_digest=self._digest(refresh_token),
            expires_at=refresh_expires_at,
            created_at=now,
        )
        return pair, session

    def _encode(
        self,
        user_id: UUID,
        session_id: UUID,
        token_type: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> str:
        payload = {
            "sub": str(user_id),
            "jti": str(session_id),
            "type": token_type,
            "iss": self._settings.issuer,
            "aud": self._settings.audience,
            "iat": issued_at,
            "exp": expires_at,
        }
        return jwt.encode(payload, self._settings.secret, algorithm=self._settings.algorithm)

    def _decode(self, token: str, expected_type: str) -> TokenClaims:
        try:
            raw = jwt.decode(
                token,
                self._settings.secret,
                algorithms=[self._settings.algorithm],
                issuer=self._settings.issuer,
                audience=self._settings.audience,
                options={
                    "require": ["sub", "jti", "type", "iss", "aud", "iat", "exp"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
            if raw["type"] != expected_type:
                raise TokenExpiredError("Token type is invalid for this operation.")
            subject = UUID(str(raw["sub"]))
            session_id = UUID(str(raw["jti"]))
            issued_at = datetime.fromtimestamp(float(raw["iat"]), UTC)
            expires_at = datetime.fromtimestamp(float(raw["exp"]), UTC)
        except TokenExpiredError:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise TokenExpiredError("Token is invalid.") from exc

        now = self._now()
        if issued_at > now:
            raise TokenExpiredError("Token is not yet valid.")
        if expires_at <= now:
            raise TokenExpiredError("Token has expired.")
        return TokenClaims(subject=subject, session_id=session_id, expires_at=expires_at)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Authentication clock must return a timezone-aware datetime.")
        return now.astimezone(UTC)

    def _digest(self, token: str) -> str:
        return hmac.new(
            self._settings.secret.encode("utf-8"),
            token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
