"""PostgreSQL API tests for F04.03 Team Head Asset read scope."""

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
from ams.interface.api.app import SqlAlchemyAssetReadServiceFactory, create_app
from ams.interface.api.dependencies import get_current_actor

ACTOR_ID = UUID("00000000-0000-4000-8000-000000004401")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000004402")
TEAM_ID = UUID("00000000-0000-4000-8000-000000004403")
OTHER_TEAM_ID = UUID("00000000-0000-4000-8000-000000004404")
TEAM_ASSET_ID = UUID("00000000-0000-4000-8000-000000004405")
OTHER_TEAM_ASSET_ID = UUID("00000000-0000-4000-8000-000000004406")
UNASSIGNED_ASSET_ID = UUID("00000000-0000-4000-8000-000000004407")


class UnusedSessionServiceFactory:
    """Satisfy composition while actor resolution is overridden."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder."""

        yield cast(SessionService, object())


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class TeamAssetReadApiTests(unittest.IsolatedAsyncioTestCase):
    """Prove Team membership scope and explicit role authorization in PostgreSQL."""

    async def asyncSetUp(self) -> None:
        """Insert Assets classified to two Teams plus one unassigned Asset."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES (
                        :id, 'scope.actor', 'scope.actor@example.test', 'Scope Actor', 'hash'
                    )
                    """
                ),
                {"id": ACTOR_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO teams (id, name, code)
                    VALUES
                        (:team, 'Scoped Team', 'SCOPED_TEAM'),
                        (:other, 'Other Scoped Team', 'OTHER_SCOPED_TEAM')
                    """
                ),
                {"team": TEAM_ID, "other": OTHER_TEAM_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'SCOPE_CATEGORY', 'Scope Category')
                    """
                ),
                {"id": CATEGORY_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO assets (
                        id, category_id, responsible_team_id, name, source,
                        total_quantity, available_quantity, created_by_user_id
                    ) VALUES
                        (:team_asset, :category, :team, 'Scoped Camera',
                         'PURCHASE', 2, 0, :actor),
                        (:other_asset, :category, :other_team, 'Foreign Camera',
                         'PURCHASE', 2, 0, :actor),
                        (:unassigned, :category, NULL, 'Unassigned Camera',
                         'PURCHASE', 2, 0, :actor)
                    """
                ),
                {
                    "team_asset": TEAM_ASSET_ID,
                    "other_asset": OTHER_TEAM_ASSET_ID,
                    "unassigned": UNASSIGNED_ASSET_ID,
                    "category": CATEGORY_ID,
                    "team": TEAM_ID,
                    "other_team": OTHER_TEAM_ID,
                    "actor": ACTOR_ID,
                },
            )
            await session.commit()

        self.actor = CurrentActor(
            ACTOR_ID,
            frozenset({OperationalRole.TEAM_HEAD}),
            team_ids=frozenset({TEAM_ID}),
            is_active=True,
        )
        application = create_app(
            UnusedSessionServiceFactory(),
            asset_read_service_factory=SqlAlchemyAssetReadServiceFactory(self.session_maker),
        )

        async def current_actor_override() -> CurrentActor:
            return self.actor

        application.dependency_overrides[get_current_actor] = current_actor_override
        self.client = AsyncClient(
            transport=ASGITransport(app=application, raise_app_exceptions=False),
            base_url="https://testserver",
        )

    async def asyncTearDown(self) -> None:
        """Close API resources and remove deterministic fixtures."""

        await self.client.aclose()
        await self._cleanup()
        await self.engine.dispose()

    async def test_team_head_sees_only_current_membership_scope(self) -> None:
        """A crafted Team query cannot widen the server-derived membership scope."""

        response = await self.client.get(
            "/api/v1/teams/me/assets",
            params={"team": str(OTHER_TEAM_ID), "page": 1, "page_size": 20},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(
            [item["id"] for item in response.json()["items"]], [str(TEAM_ASSET_ID)]
        )
        self.assertNotIn(str(OTHER_TEAM_ASSET_ID), response.text)
        self.assertNotIn(str(UNASSIGNED_ASSET_ID), response.text)

    async def test_hr_does_not_inherit_team_head_scope(self) -> None:
        """HR role and a Team membership do not imply Team Head endpoint access."""

        self.actor = CurrentActor(
            ACTOR_ID,
            frozenset({OperationalRole.HR}),
            team_ids=frozenset({TEAM_ID}),
            is_active=True,
        )

        response = await self.client.get("/api/v1/teams/me/assets")

        self._assert_error(response, 403, "TEAM_SCOPE_REQUIRED")

    async def test_team_head_without_membership_has_no_scope(self) -> None:
        """A Team Head role without current memberships cannot read classified Assets."""

        self.actor = CurrentActor(
            ACTOR_ID,
            frozenset({OperationalRole.TEAM_HEAD}),
            team_ids=frozenset(),
            is_active=True,
        )

        response = await self.client.get("/api/v1/teams/me/assets")

        self._assert_error(response, 403, "TEAM_SCOPE_REQUIRED")

    async def _cleanup(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text("DELETE FROM assets WHERE id = ANY(:ids)"),
                {"ids": [TEAM_ASSET_ID, OTHER_TEAM_ASSET_ID, UNASSIGNED_ASSET_ID]},
            )
            await session.execute(
                text("DELETE FROM asset_categories WHERE id = :id"), {"id": CATEGORY_ID}
            )
            await session.execute(
                text("DELETE FROM teams WHERE id = ANY(:ids)"),
                {"ids": [TEAM_ID, OTHER_TEAM_ID]},
            )
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": ACTOR_ID})
            await session.commit()

    def _assert_error(self, response: Response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        UUID(response.json()["traceId"])
