"""PostgreSQL API tests for F04.02 Responsible Team classification."""

from __future__ import annotations

import os
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.authentication import SessionService
from ams.domain.identity import CurrentActor, OperationalRole
from ams.interface.api.app import SqlAlchemyResponsibleTeamServiceFactory, create_app
from ams.interface.api.dependencies import get_current_actor

HR_ID = UUID("00000000-0000-4000-8000-000000004301")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000004302")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000004303")
ASSET_ID = UUID("00000000-0000-4000-8000-000000004304")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000004305")
TEAM_ID = UUID("00000000-0000-4000-8000-000000004306")
INACTIVE_TEAM_ID = UUID("00000000-0000-4000-8000-000000004307")
MISSING_ID = UUID("00000000-0000-4000-8000-000000004399")


class UnusedSessionServiceFactory:
    """Satisfy application composition while actor resolution is overridden."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder."""

        yield cast(SessionService, object())


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class ResponsibleTeamApiTests(unittest.IsolatedAsyncioTestCase):
    """Prove authorization, references, nullable assignment, and Custody isolation."""

    async def asyncSetUp(self) -> None:
        """Insert one Asset, one Custody, and active/inactive Team fixtures."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:hr, 'team.hr', 'team.hr@example.test', 'Team HR', 'hash'),
                        (:custodian, 'team.custodian', 'team.custodian@example.test',
                         'Team Custodian', 'hash')
                    """
                ),
                {"hr": HR_ID, "custodian": CUSTODIAN_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO teams (id, name, code, is_active)
                    VALUES
                        (:active, 'Classification Team', 'CLASS_TEAM', TRUE),
                        (:inactive, 'Inactive Classification Team',
                         'INACTIVE_CLASS_TEAM', FALSE)
                    """
                ),
                {"active": TEAM_ID, "inactive": INACTIVE_TEAM_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'CLASS_CATEGORY', 'Classification Category')
                    """
                ),
                {"id": CATEGORY_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO assets (
                        id, category_id, name, source, total_quantity, available_quantity,
                        created_by_user_id
                    ) VALUES (:id, :category, 'Classification Camera', 'PURCHASE', 3, 2, :hr)
                    """
                ),
                {"id": ASSET_ID, "category": CATEGORY_ID, "hr": HR_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO custodies (
                        id, asset_id, custodian_user_id, assigned_by_user_id, status,
                        on_hand_quantity, reserved_quantity, temporarily_loaned_quantity,
                        condition, assigned_at, confirmed_at, confirmed_by_user_id
                    ) VALUES (
                        :id, :asset, :custodian, :hr, 'ACTIVE', 3, 1, 0, 'GOOD',
                        '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z', :custodian
                    )
                    """
                ),
                {
                    "id": CUSTODY_ID,
                    "asset": ASSET_ID,
                    "custodian": CUSTODIAN_ID,
                    "hr": HR_ID,
                },
            )
            await session.commit()

        self.actor = CurrentActor(HR_ID, frozenset({OperationalRole.HR}), is_active=True)
        application = create_app(
            UnusedSessionServiceFactory(),
            responsible_team_service_factory=SqlAlchemyResponsibleTeamServiceFactory(
                self.session_maker
            ),
        )

        async def current_actor_override() -> CurrentActor:
            return self.actor

        application.dependency_overrides[get_current_actor] = current_actor_override
        self.client = AsyncClient(
            transport=ASGITransport(app=application, raise_app_exceptions=False),
            base_url="https://testserver",
        )

    async def asyncTearDown(self) -> None:
        """Close resources and remove deterministic fixtures."""

        await self.client.aclose()
        await self._cleanup()
        await self.engine.dispose()

    async def test_hr_assigns_and_clears_team_without_custody_mutation(self) -> None:
        """Nullable classification changes only the Asset responsible_team_id column."""

        custody_before = await self._custody_snapshot()
        assigned = await self._patch(ASSET_ID, TEAM_ID)
        custody_after_assign = await self._custody_snapshot()
        cleared = await self._patch(ASSET_ID, None)

        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.assertEqual(
            assigned.json(), {"id": str(ASSET_ID), "responsibleTeamId": str(TEAM_ID)}
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertEqual(cleared.json(), {"id": str(ASSET_ID), "responsibleTeamId": None})
        self.assertEqual(await self._responsible_team_id(), None)
        self.assertEqual(custody_after_assign, custody_before)
        self.assertEqual(await self._custody_snapshot(), custody_before)

    async def test_non_hr_is_denied_without_asset_or_custody_mutation(self) -> None:
        """Route and application authorization reject an ordinary member."""

        custody_before = await self._custody_snapshot()
        self.actor = CurrentActor(HR_ID, frozenset(), is_active=True)

        response = await self._patch(ASSET_ID, TEAM_ID)

        self._assert_error(response, 403, "AUTHORIZATION_DENIED")
        self.assertEqual(await self._responsible_team_id(), None)
        self.assertEqual(await self._custody_snapshot(), custody_before)

    async def test_unknown_asset_and_unavailable_team_use_stable_not_found_errors(self) -> None:
        """Missing Asset and absent/inactive Team references are rejected before save."""

        missing_asset = await self._patch(MISSING_ID, TEAM_ID)
        missing_team = await self._patch(ASSET_ID, MISSING_ID)
        inactive_team = await self._patch(ASSET_ID, INACTIVE_TEAM_ID)

        self._assert_error(missing_asset, 404, "ASSET_NOT_FOUND")
        self._assert_error(missing_team, 404, "TEAM_NOT_FOUND")
        self._assert_error(inactive_team, 404, "TEAM_NOT_FOUND")
        self.assertEqual(await self._responsible_team_id(), None)

    async def _patch(self, asset_id: UUID, team_id: UUID | None) -> Response:
        return await self.client.patch(
            f"/api/v1/hr/assets/{asset_id}/responsible-team",
            json={"responsibleTeamId": str(team_id) if team_id is not None else None},
        )

    async def _responsible_team_id(self) -> UUID | None:
        async with self.session_maker() as session:
            result = await session.execute(
                text("SELECT responsible_team_id FROM assets WHERE id = :id"),
                {"id": ASSET_ID},
            )
            return cast(UUID | None, result.scalar_one())

    async def _custody_snapshot(self) -> tuple[object, ...]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT asset_id, custodian_user_id, assigned_by_user_id, status,
                           on_hand_quantity, reserved_quantity, temporarily_loaned_quantity,
                           condition, condition_note, assigned_at, confirmed_at,
                           confirmed_by_user_id, closed_at, created_at, updated_at
                    FROM custodies
                    WHERE id = :id
                    """
                ),
                {"id": CUSTODY_ID},
            )
            return tuple(result.one())

    async def _cleanup(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text("DELETE FROM custodies WHERE id = :id"), {"id": CUSTODY_ID}
            )
            await session.execute(text("DELETE FROM assets WHERE id = :id"), {"id": ASSET_ID})
            await session.execute(
                text("DELETE FROM asset_categories WHERE id = :id"), {"id": CATEGORY_ID}
            )
            await session.execute(
                text("DELETE FROM teams WHERE id = ANY(:ids)"),
                {"ids": [TEAM_ID, INACTIVE_TEAM_ID]},
            )
            await session.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"),
                {"ids": [HR_ID, CUSTODIAN_ID]},
            )
            await session.commit()

    def _assert_error(self, response: Response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        UUID(response.json()["traceId"])
