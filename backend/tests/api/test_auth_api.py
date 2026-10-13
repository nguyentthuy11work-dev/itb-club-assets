"""API contract tests for the F01.04 authentication boundary."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from argon2 import PasswordHasher
from fastapi import Depends
from httpx import ASGITransport, AsyncClient, Response
from structlog.contextvars import get_contextvars

from ams.application.authentication import SessionService
from ams.domain.authentication import LocalCredential, UserIdentity
from ams.domain.identity import CurrentActor, OperationalRole
from ams.infrastructure.authentication import (
    Argon2PasswordHasher,
    JwtTokenService,
    LocalIdentityProvider,
    TokenSettings,
)
from ams.infrastructure.memory import (
    InMemoryRefreshSessionRepository,
    InMemoryUserRepository,
)
from ams.interface.api.app import create_app
from ams.interface.api.dependencies import RequireRoles

USER_ID = UUID("00000000-0000-4000-8000-000000000003")
TEAM_ID = UUID("00000000-0000-4000-8000-000000000010")
SECRET = "test-only-secret-that-is-at-least-32-bytes-long"
PASSWORD = "correct horse battery staple"
HR_REQUIRED = RequireRoles(OperationalRole.HR)


class StaticSessionServiceFactory:
    """Yield one injected service while retaining in-memory state across requests."""

    def __init__(self, service: SessionService) -> None:
        self._service = service

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield the configured service as a request context."""

        yield self._service


class AuthenticationApiTests(unittest.IsolatedAsyncioTestCase):
    """Verify frozen auth responses, cookies, and the 401/403 boundary."""

    async def asyncSetUp(self) -> None:
        """Create a real auth service behind in-memory persistence adapters."""

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
        self.users = InMemoryUserRepository(
            (LocalCredential(self.user, self.password_hasher.hash(PASSWORD)),)
        )
        self.token_service = JwtTokenService(
            TokenSettings(secret=SECRET),
            InMemoryRefreshSessionRepository(),
            lambda: datetime.now(UTC),
        )
        service = SessionService(
            LocalIdentityProvider(self.users, self.password_hasher),
            self.token_service,
            self.users,
        )
        application = create_app(StaticSessionServiceFactory(service))

        @application.get("/api/v1/test/hr")
        async def hr_only(
            actor: Annotated[CurrentActor, Depends(HR_REQUIRED)],
        ) -> dict[str, str]:
            """Test-only protected endpoint registered on this app instance."""

            return {"id": str(actor.id)}

        @application.get("/api/v1/test/context")
        async def request_context(
            actor: Annotated[CurrentActor, Depends(HR_REQUIRED)],
        ) -> dict[str, str]:
            """Expose selected test-only context fields after authentication."""

            context = get_contextvars()
            return {
                "actorId": str(context["actor_id"]),
                "traceId": str(context["trace_id"]),
                "id": str(actor.id),
            }

        self.client = AsyncClient(
            transport=ASGITransport(app=application), base_url="https://testserver"
        )

    async def asyncTearDown(self) -> None:
        """Close the asynchronous API test client."""

        await self.client.aclose()

    async def test_login_and_me_follow_frozen_contract(self) -> None:
        """Login returns access/user JSON and keeps refresh credentials HttpOnly."""

        login = await self._login()
        body = login.json()

        self.assertEqual(login.status_code, 200)
        self.assertEqual(set(body), {"accessToken", "expiresAt", "user"})
        self.assertEqual(
            body["user"],
            {
                "id": str(USER_ID),
                "name": "Member One",
                "roles": ["HR"],
                "teamIds": [str(TEAM_ID)],
            },
        )
        self.assertNotIn("refreshToken", body)
        cookie = login.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=strict", cookie)

        me = await self.client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {body['accessToken']}"},
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json(), body["user"])

    async def test_refresh_rotates_cookie_and_logout_revokes_it(self) -> None:
        """The browser cookie supports rotation and cannot be reused after logout."""

        first_access_token = (await self._login()).json()["accessToken"]
        refresh = await self.client.post("/api/v1/auth/refresh")

        self.assertEqual(refresh.status_code, 200)
        self.assertNotEqual(refresh.json()["accessToken"], first_access_token)
        self.assertEqual(set(refresh.json()), {"accessToken", "expiresAt"})

        logout = await self.client.post("/api/v1/auth/logout")
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(logout.json(), {"status": "LOGGED_OUT"})

        rejected = await self.client.post("/api/v1/auth/refresh")
        self._assert_error(rejected, 401, "TOKEN_EXPIRED")

    async def test_missing_or_invalid_authentication_is_401(self) -> None:
        """Missing bearer/cookie and bad credentials are unauthenticated, not forbidden."""

        missing_bearer = await self.client.get("/api/v1/auth/me")
        self._assert_error(missing_bearer, 401, "TOKEN_EXPIRED")
        self.assertEqual(missing_bearer.headers["www-authenticate"], "Bearer")

        bad_login = await self.client.post(
            "/api/v1/auth/login",
            json={"username": "member.one", "password": "wrong"},
        )
        self._assert_error(bad_login, 401, "INVALID_CREDENTIALS")

        missing_cookie = await self.client.post("/api/v1/auth/refresh")
        self._assert_error(missing_cookie, 401, "TOKEN_EXPIRED")

    async def test_authenticated_inactive_or_unauthorized_actor_is_403(self) -> None:
        """Valid identity with failed account/role policy is forbidden, not 401."""

        access_token = (await self._login()).json()["accessToken"]
        inactive = UserIdentity(
            id=self.user.id,
            username=self.user.username,
            email=self.user.email,
            display_name=self.user.display_name,
            roles=self.user.roles,
            team_ids=self.user.team_ids,
            is_active=False,
        )
        self.users.upsert(LocalCredential(inactive, self.password_hasher.hash(PASSWORD)))
        inactive_response = await self.client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self._assert_error(inactive_response, 403, "ACCOUNT_INACTIVE")
        self.assertNotIn("www-authenticate", inactive_response.headers)

        ordinary = UserIdentity(
            id=self.user.id,
            username=self.user.username,
            email=self.user.email,
            display_name=self.user.display_name,
            roles=frozenset(),
            team_ids=self.user.team_ids,
            is_active=True,
        )
        self.users.upsert(LocalCredential(ordinary, self.password_hasher.hash(PASSWORD)))
        forbidden = await self.client.get(
            "/api/v1/test/hr",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self._assert_error(forbidden, 403, "AUTHORIZATION_DENIED")

    async def test_request_models_are_strict_and_forbid_unknown_fields(self) -> None:
        """Malformed or expanded login payloads are rejected before the router runs."""

        response = await self.client.post(
            "/api/v1/auth/login",
            json={"username": "member.one", "password": PASSWORD, "role": "HR"},
        )
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["error"]["code"], "VALIDATION_ERROR")
        self.assertEqual(
            body["error"]["details"]["issues"],
            [{"location": ["body", "role"], "type": "extra_forbidden"}],
        )
        UUID(body["traceId"])
        self.assertEqual(response.headers["x-trace-id"], body["traceId"])

    async def test_authenticated_actor_is_bound_to_request_trace_context(self) -> None:
        """Authentication enriches the same isolated context established by middleware."""

        access_token = (await self._login()).json()["accessToken"]
        response = await self.client.get(
            "/api/v1/test/context",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["actorId"], str(USER_ID))
        self.assertEqual(response.json()["id"], str(USER_ID))
        self.assertEqual(response.json()["traceId"], response.headers["x-trace-id"])

    async def _login(self) -> Response:
        return await self.client.post(
            "/api/v1/auth/login",
            json={"username": "member.one", "password": PASSWORD},
        )

    def _assert_error(self, response: Response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status)
        body = response.json()
        self.assertEqual(body["error"]["code"], code)
        self.assertEqual(body["error"]["details"], {})
        UUID(body["traceId"])
        self.assertEqual(response.headers["x-trace-id"], body["traceId"])
