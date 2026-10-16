"""API contract tests for member role administration."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient, Response

from ams.application.authentication import SessionService
from ams.application.member_roles import MemberRoleService, MemberSummary
from ams.domain.authentication import LocalCredential, UserIdentity
from ams.domain.identity import OperationalRole
from ams.infrastructure.authentication import (
    Argon2PasswordHasher,
    JwtTokenService,
    LocalIdentityProvider,
    TokenSettings,
)
from ams.infrastructure.memory import InMemoryRefreshSessionRepository, InMemoryUserRepository
from ams.interface.api.app import create_app

ADMIN_ID = UUID("00000000-0000-4000-8000-000000000001")
HR_ID = UUID("00000000-0000-4000-8000-000000000002")
MEMBER_ID = UUID("00000000-0000-4000-8000-000000000003")
SECRET = "test-only-secret-that-is-at-least-32-bytes-long"
PASSWORD = "correct horse battery staple"


class InMemoryMemberRoleRepository:
    """Role store shared by the fake auth repository's users."""

    def __init__(self, members: dict[UUID, MemberSummary]) -> None:
        self.members = members

    async def list_members(self, *, offset: int, limit: int) -> tuple[list[MemberSummary], int]:
        items = sorted(self.members.values(), key=lambda member: member.display_name)
        return items[offset : offset + limit], len(items)

    async def get_for_update(self, member_id: UUID) -> MemberSummary | None:
        return self.members.get(member_id)

    async def replace_roles(
        self, member_id: UUID, roles: frozenset[OperationalRole], granted_by: UUID
    ) -> None:
        member = self.members[member_id]
        self.members[member_id] = MemberSummary(
            member.id, member.username, member.email, member.display_name, roles, True
        )


class StaticFactory:
    """Yield one injected service as a request context."""

    def __init__(self, service: object) -> None:
        self._service = service

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[object]:
        yield self._service


class MemberRoleApiTests(unittest.IsolatedAsyncioTestCase):
    """Verify listing, replacement and anti-escalation over HTTP."""

    async def asyncSetUp(self) -> None:
        hasher = Argon2PasswordHasher(
            PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
        )
        people = {
            ADMIN_ID: ("admin", frozenset({OperationalRole.SYSTEM_ADMIN})),
            HR_ID: ("hr", frozenset({OperationalRole.HR})),
            MEMBER_ID: ("member", frozenset()),
        }
        credentials = []
        members = {}
        for user_id, (username, roles) in people.items():
            identity = UserIdentity(
                user_id,
                username,
                f"{username}@example.test",
                username.title(),
                roles,
                frozenset(),
                True,
            )
            credentials.append(LocalCredential(identity, hasher.hash(PASSWORD)))
            members[user_id] = MemberSummary(
                user_id, username, identity.email, identity.display_name, roles, True
            )
        users = InMemoryUserRepository(tuple(credentials))
        session_service = SessionService(
            LocalIdentityProvider(users, hasher),
            JwtTokenService(
                TokenSettings(secret=SECRET),
                InMemoryRefreshSessionRepository(),
                lambda: datetime.now(UTC),
            ),
            users,
        )
        self.repository = InMemoryMemberRoleRepository(members)
        application = create_app(
            StaticFactory(session_service),  # type: ignore[arg-type]
            member_role_service_factory=StaticFactory(  # type: ignore[arg-type]
                MemberRoleService(self.repository)
            ),
        )
        self.client = AsyncClient(
            transport=ASGITransport(app=application), base_url="https://testserver"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def _headers(self, username: str) -> dict[str, str]:
        login = await self.client.post(
            "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
        )
        return {"Authorization": f"Bearer {login.json()['accessToken']}"}

    async def _put_roles(self, as_user: str, member_id: UUID, roles: list[str]) -> Response:
        return await self.client.put(
            f"/api/v1/hr/members/{member_id}/roles",
            json={"roles": roles},
            headers=await self._headers(as_user),
        )

    async def test_admin_lists_members_with_roles(self) -> None:
        response = await self.client.get(
            "/api/v1/hr/members", headers=await self._headers("admin")
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["items"][0]["roles"], ["SYSTEM_ADMIN"])

    async def test_plain_member_cannot_list(self) -> None:
        response = await self.client.get(
            "/api/v1/hr/members", headers=await self._headers("member")
        )

        self.assertEqual(response.status_code, 403)

    async def test_admin_grants_elevated_role(self) -> None:
        response = await self._put_roles("admin", MEMBER_ID, ["HR", "SHIFT_LEAD"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["roles"], ["HR", "SHIFT_LEAD"])
        self.assertEqual(
            self.repository.members[MEMBER_ID].roles,
            frozenset({OperationalRole.HR, OperationalRole.SHIFT_LEAD}),
        )

    async def test_hr_grants_only_non_elevated_roles(self) -> None:
        allowed = await self._put_roles("hr", MEMBER_ID, ["TEAM_HEAD"])
        denied = await self._put_roles("hr", MEMBER_ID, ["HR"])

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "ROLE_ASSIGNMENT_FORBIDDEN")
        self.assertEqual(
            self.repository.members[MEMBER_ID].roles, frozenset({OperationalRole.TEAM_HEAD})
        )

    async def test_self_edit_and_unknown_member_are_rejected(self) -> None:
        self_edit = await self._put_roles("admin", ADMIN_ID, [])
        unknown = await self._put_roles(
            "admin", UUID("00000000-0000-4000-8000-000000000099"), []
        )

        self.assertEqual(self_edit.status_code, 403)
        self.assertEqual(unknown.status_code, 404)
