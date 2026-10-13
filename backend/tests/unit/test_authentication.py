"""Unit tests for the F01.03 local authentication and session lifecycle."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from argon2 import PasswordHasher

from ams.application.authentication import SessionService
from ams.domain.authentication import (
    AccountInactiveError,
    InvalidCredentialsError,
    LocalCredential,
    TokenExpiredError,
    UserIdentity,
)
from ams.domain.identity import OperationalRole
from ams.domain.repositories import IUserRepository
from ams.infrastructure.authentication import (
    Argon2PasswordHasher,
    JwtTokenService,
    LocalIdentityProvider,
    TokenSettings,
)
from ams.infrastructure.memory import InMemoryRefreshSessionRepository

USER_ID = UUID("00000000-0000-4000-8000-000000000003")
TEAM_ID = UUID("00000000-0000-4000-8000-000000000010")
SECRET = "test-only-secret-that-is-at-least-32-bytes-long"
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


class MutableClock:
    """Deterministic timezone-aware clock for token tests."""

    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        """Return the configured instant."""

        return self.current


class FakeUserRepository(IUserRepository):
    """Case-insensitive identity repository used at the application boundary."""

    def __init__(self, credential: LocalCredential | None) -> None:
        self.credential = credential

    async def get_local_credential(self, login: str) -> LocalCredential | None:
        """Find the fixture by username or email."""

        if self.credential is None:
            return None
        user = self.credential.user
        if login.casefold() in {user.username.casefold(), user.email.casefold()}:
            return self.credential
        return None

    async def get_identity(self, user_id: UUID) -> UserIdentity | None:
        """Return the fixture's current identity."""

        if self.credential is None or self.credential.user.id != user_id:
            return None
        return self.credential.user


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise credential verification and every session-service outcome."""

    def setUp(self) -> None:
        """Build the adapters with deterministic token time."""

        self.clock = MutableClock()
        self.password_hasher = Argon2PasswordHasher(
            PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
        )
        self.user = UserIdentity(
            id=USER_ID,
            username="member.one",
            email="member.one@example.test",
            display_name="Member One",
            roles=frozenset({OperationalRole.HR}),
            team_ids=frozenset({TEAM_ID}),
            is_active=True,
        )
        self.password_hash = self.password_hasher.hash("correct horse battery staple")
        self.repository = FakeUserRepository(LocalCredential(self.user, self.password_hash))
        self.sessions = InMemoryRefreshSessionRepository()
        self.token_service = JwtTokenService(
            TokenSettings(secret=SECRET), self.sessions, self.clock
        )
        self.provider = LocalIdentityProvider(self.repository, self.password_hasher)
        self.service = SessionService(self.provider, self.token_service, self.repository)

    async def test_login_returns_identity_and_configured_token_expirations(self) -> None:
        """A valid local credential creates a refresh-backed session."""

        result = await self.service.login("  MEMBER.ONE  ", "correct horse battery staple")

        self.assertEqual(result.user, self.user)
        self.assertEqual(result.tokens.access_expires_at, NOW + timedelta(minutes=15))
        self.assertEqual(result.tokens.refresh_expires_at, NOW + timedelta(days=7))
        claims = await self.token_service.verify_access_token(result.tokens.access_token)
        self.assertEqual(claims.subject, USER_ID)

    async def test_login_rejects_unknown_blank_and_wrong_credentials_generically(self) -> None:
        """All credential failures use the stable code and generic message."""

        attempts = [
            ("missing", "correct horse battery staple"),
            ("member.one", "wrong"),
            ("", "wrong"),
            ("member.one", ""),
        ]
        for login, password in attempts:
            with self.subTest(login=login, password_present=bool(password)):
                with self.assertRaises(InvalidCredentialsError) as caught:
                    await self.service.login(login, password)
                self.assertEqual(caught.exception.code, "INVALID_CREDENTIALS")
                self.assertEqual(str(caught.exception), "Username or password is invalid.")

    async def test_inactive_account_cannot_login(self) -> None:
        """Credential validity never overrides account inactivity."""

        inactive = UserIdentity(
            id=self.user.id,
            username=self.user.username,
            email=self.user.email,
            display_name=self.user.display_name,
            roles=self.user.roles,
            team_ids=self.user.team_ids,
            is_active=False,
        )
        self.repository.credential = LocalCredential(
            inactive, self.password_hasher.hash("correct horse battery staple")
        )

        with self.assertRaises(AccountInactiveError) as caught:
            await self.service.login("member.one", "correct horse battery staple")
        self.assertEqual(caught.exception.code, "ACCOUNT_INACTIVE")

    async def test_refresh_rotates_once_and_rechecks_account(self) -> None:
        """A refresh token is single-use and inactive users cannot rotate it."""

        login = await self.service.login("member.one", "correct horse battery staple")
        replacement = await self.service.refresh(login.tokens.refresh_token)
        replacement_claims = await self.token_service.verify_refresh_token(
            replacement.refresh_token
        )
        self.assertEqual(replacement_claims.subject, USER_ID)

        with self.assertRaises(TokenExpiredError):
            await self.service.refresh(login.tokens.refresh_token)

        inactive = UserIdentity(
            id=self.user.id,
            username=self.user.username,
            email=self.user.email,
            display_name=self.user.display_name,
            roles=self.user.roles,
            team_ids=self.user.team_ids,
            is_active=False,
        )
        self.repository.credential = LocalCredential(inactive, self.password_hash)
        with self.assertRaises(AccountInactiveError):
            await self.service.refresh(replacement.refresh_token)

    async def test_logout_revokes_refresh_token_once(self) -> None:
        """Logout prevents token reuse and repeated logout reports an invalid session."""

        login = await self.service.login("member.one", "correct horse battery staple")
        await self.service.logout(login.tokens.refresh_token)

        with self.assertRaises(TokenExpiredError) as caught:
            await self.service.logout(login.tokens.refresh_token)
        self.assertEqual(caught.exception.code, "TOKEN_EXPIRED")

    async def test_current_actor_uses_fresh_repository_roles_and_teams(self) -> None:
        """Access claims identify a user but never become an authorization snapshot."""

        login = await self.service.login("member.one", "correct horse battery staple")
        changed = UserIdentity(
            id=self.user.id,
            username=self.user.username,
            email=self.user.email,
            display_name=self.user.display_name,
            roles=frozenset({OperationalRole.TEAM_HEAD}),
            team_ids=frozenset(),
            is_active=True,
        )
        self.repository.credential = LocalCredential(changed, self.password_hash)

        actor = await self.service.get_current_actor(login.tokens.access_token)
        self.assertEqual(actor.roles, frozenset({OperationalRole.TEAM_HEAD}))
        self.assertEqual(actor.team_ids, frozenset())

    async def test_missing_or_inactive_token_subject_is_rejected(self) -> None:
        """Deleted and disabled accounts invalidate otherwise signed access tokens."""

        login = await self.service.login("member.one", "correct horse battery staple")
        self.repository.credential = None
        with self.assertRaises(TokenExpiredError):
            await self.service.get_current_actor(login.tokens.access_token)

        self.repository.credential = LocalCredential(
            UserIdentity(
                id=self.user.id,
                username=self.user.username,
                email=self.user.email,
                display_name=self.user.display_name,
                roles=self.user.roles,
                team_ids=self.user.team_ids,
                is_active=False,
            ),
            "unused",
        )
        with self.assertRaises(AccountInactiveError):
            await self.service.get_current_actor(login.tokens.access_token)

    async def test_expired_and_wrong_type_tokens_are_rejected(self) -> None:
        """Expiration and token-purpose checks use the stable token error."""

        login = await self.service.login("member.one", "correct horse battery staple")
        with self.assertRaises(TokenExpiredError):
            await self.token_service.verify_access_token(login.tokens.refresh_token)

        self.clock.current = NOW + timedelta(minutes=15)
        with self.assertRaises(TokenExpiredError) as caught:
            await self.token_service.verify_access_token(login.tokens.access_token)
        self.assertEqual(caught.exception.code, "TOKEN_EXPIRED")

    async def test_tampered_issuer_and_malformed_tokens_are_rejected(self) -> None:
        """Signature, issuer, audience, and required claims are verified."""

        bad_issuer_token = jwt.encode(
            {
                "sub": str(USER_ID),
                "jti": "00000000-0000-4000-8000-000000000099",
                "type": "access",
                "iss": "another-service",
                "aud": "ams-web",
                "iat": NOW,
                "exp": NOW + timedelta(minutes=5),
            },
            SECRET,
            algorithm="HS256",
        )
        for token in (bad_issuer_token, "not-a-jwt"):
            with self.subTest(token=token[:12]), self.assertRaises(TokenExpiredError):
                await self.token_service.verify_access_token(token)

    async def test_concurrent_refresh_allows_one_rotation(self) -> None:
        """The repository's atomic consume operation prevents replay races."""

        login = await self.service.login("member.one", "correct horse battery staple")
        results = await asyncio.gather(
            self.service.refresh(login.tokens.refresh_token),
            self.service.refresh(login.tokens.refresh_token),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(result, Exception) for result in results), 1)
        self.assertEqual(sum(isinstance(result, TokenExpiredError) for result in results), 1)


class PasswordAndConfigurationTests(unittest.TestCase):
    """Verify password storage and startup safety checks."""

    def test_argon2id_hash_is_salted_and_verifies_without_plaintext(self) -> None:
        """Passwords use Argon2id and identical inputs receive distinct hashes."""

        hasher = Argon2PasswordHasher(
            PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
        )
        first = hasher.hash("a strong local password")
        second = hasher.hash("a strong local password")

        self.assertTrue(first.startswith("$argon2id$"))
        self.assertNotEqual(first, second)
        self.assertNotIn("a strong local password", first)
        self.assertTrue(hasher.verify("a strong local password", first))
        self.assertFalse(hasher.verify("wrong", first))
        self.assertFalse(hasher.verify("anything", "malformed"))

    def test_blank_password_and_unsafe_token_settings_are_rejected(self) -> None:
        """Adapters fail closed for blank secrets/passwords and unsafe TTLs."""

        with self.assertRaises(ValueError):
            Argon2PasswordHasher(
                PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
            ).hash("")
        invalid_settings = [
            {"secret": "too-short"},
            {"secret": SECRET, "issuer": " "},
            {"secret": SECRET, "audience": ""},
            {"secret": SECRET, "access_ttl": timedelta(0)},
            {
                "secret": SECRET,
                "access_ttl": timedelta(days=8),
                "refresh_ttl": timedelta(days=7),
            },
            {"secret": SECRET, "algorithm": "none"},
        ]
        for kwargs in invalid_settings:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TokenSettings(**kwargs)  # type: ignore[arg-type]

    def test_naive_clock_is_rejected(self) -> None:
        """Token timestamps must always be timezone-aware UTC values."""

        service = JwtTokenService(
            TokenSettings(secret=SECRET),
            InMemoryRefreshSessionRepository(),
            lambda: datetime(2026, 9, 18),
        )
        with self.assertRaises(ValueError):
            asyncio.run(service.issue(USER_ID))
