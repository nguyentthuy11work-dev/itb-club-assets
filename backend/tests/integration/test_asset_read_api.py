"""PostgreSQL API tests for F02.06 Asset and current-Custody read models."""

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
from ams.domain.identity import CurrentActor
from ams.interface.api.app import SqlAlchemyAssetReadServiceFactory, create_app
from ams.interface.api.dependencies import get_current_actor

HR_ID = UUID("00000000-0000-4000-8000-000000002101")
ACTOR_ID = UUID("00000000-0000-4000-8000-000000002102")
OTHER_ID = UUID("00000000-0000-4000-8000-000000002103")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000002104")
TEAM_ID = UUID("00000000-0000-4000-8000-000000002112")
ASSET_ID = UUID("00000000-0000-4000-8000-000000002105")
OTHER_ASSET_ID = UUID("00000000-0000-4000-8000-000000002106")
ACTIVE_CUSTODY_ID = UUID("00000000-0000-4000-8000-000000002107")
PENDING_CUSTODY_ID = UUID("00000000-0000-4000-8000-000000002108")
OTHER_PENDING_CUSTODY_ID = UUID("00000000-0000-4000-8000-000000002109")
CLOSED_CUSTODY_ID = UUID("00000000-0000-4000-8000-000000002110")
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000002111")
MISSING_ASSET_ID = UUID("00000000-0000-4000-8000-000000002199")


class UnusedSessionServiceFactory:
    """Satisfy application composition while actor resolution is overridden."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder."""

        yield cast(SessionService, object())


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class AssetReadApiTests(unittest.IsolatedAsyncioTestCase):
    """Prove actor scope, stable pagination, current states, and privacy."""

    async def asyncSetUp(self) -> None:
        """Insert deterministic current and historical Custody fixtures."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO teams (id, name, code)
                    VALUES (:id, 'Read Team', 'READ_TEAM')
                    """
                ),
                {"id": TEAM_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:hr, 'read.hr', 'read.hr@example.test', 'Read HR', 'hash'),
                        (:actor, 'read.actor', 'read.actor@example.test', 'Read Actor', 'hash'),
                        (:other, 'read.other', 'read.other@example.test', 'Read Other', 'hash')
                    """
                ),
                {"hr": HR_ID, "actor": ACTOR_ID, "other": OTHER_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'READ_CATEGORY', 'Read Category')
                    """
                ),
                {"id": CATEGORY_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO assets (
                        id, category_id, responsible_team_id, name, description, source,
                        total_quantity, available_quantity, created_by_user_id
                    ) VALUES
                        (:asset, :category, :team, 'Read Camera', 'Private-safe detail',
                         'DONATION', 5, 2, :hr),
                        (:other_asset, :category, NULL, 'Read Tripod', NULL,
                         'PURCHASE', 1, 0, :hr)
                    """
                ),
                {
                    "asset": ASSET_ID,
                    "other_asset": OTHER_ASSET_ID,
                    "category": CATEGORY_ID,
                    "team": TEAM_ID,
                    "hr": HR_ID,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO custodies (
                        id, asset_id, custodian_user_id, assigned_by_user_id, status,
                        on_hand_quantity, reserved_quantity, temporarily_loaned_quantity,
                        condition, condition_note, assigned_at, confirmed_at,
                        confirmed_by_user_id, closed_at
                    ) VALUES
                        (:active, :asset, :actor, :hr, 'ACTIVE', 3, 1, 0,
                         'GOOD', NULL, '2026-10-01T00:00:00Z',
                         '2026-10-01T01:00:00Z', :actor, NULL),
                        (:other_pending, :asset, :other, :hr, 'PENDING', 2, 0, 0,
                         'DAMAGED', 'Scratch', '2026-10-02T00:00:00Z', NULL, NULL, NULL),
                        (:pending, :other_asset, :actor, :hr, 'PENDING', 1, 0, 0,
                         'GOOD', NULL, '2026-10-03T00:00:00Z', NULL, NULL, NULL),
                        (:closed, :other_asset, :other, :hr, 'CLOSED', 0, 0, 0,
                         'GOOD', NULL, '2026-09-01T00:00:00Z',
                         '2026-09-01T01:00:00Z', :other, '2026-09-02T00:00:00Z')
                    """
                ),
                {
                    "active": ACTIVE_CUSTODY_ID,
                    "other_pending": OTHER_PENDING_CUSTODY_ID,
                    "pending": PENDING_CUSTODY_ID,
                    "closed": CLOSED_CUSTODY_ID,
                    "asset": ASSET_ID,
                    "other_asset": OTHER_ASSET_ID,
                    "actor": ACTOR_ID,
                    "other": OTHER_ID,
                    "hr": HR_ID,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO custody_evidence (
                        id, custody_id, uploaded_by_user_id, evidence_type, object_key,
                        content_type, content_sha256, size_bytes, note
                    ) VALUES (
                        :id, :custody, :actor, 'ASSIGNMENT_CONFIRMATION',
                        'objects/private-read-key', 'image/png', :sha, 32, 'private note'
                    )
                    """
                ),
                {
                    "id": EVIDENCE_ID,
                    "custody": ACTIVE_CUSTODY_ID,
                    "actor": ACTOR_ID,
                    "sha": "a" * 64,
                },
            )
            await session.commit()

        self.actor = CurrentActor(ACTOR_ID, frozenset(), is_active=True)
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
        """Close API resources and remove all deterministic fixtures."""

        await self.client.aclose()
        await self._cleanup()
        await self.engine.dispose()

    async def test_held_assets_are_actor_scoped_and_stably_paginated(self) -> None:
        """Only current actor Custodies appear, newest assignment first."""

        first = await self.client.get("/api/v1/assets/held?page=1&page_size=1")
        second = await self.client.get("/api/v1/assets/held?page=2&page_size=1")

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["total"], 2)
        self.assertEqual(first.json()["pageSize"], 1)
        self.assertEqual(first.json()["items"][0]["custodyId"], str(PENDING_CUSTODY_ID))
        self.assertEqual(first.json()["items"][0]["lendableQuantity"], 0)
        self.assertEqual(second.json()["items"][0]["custodyId"], str(ACTIVE_CUSTODY_ID))
        self.assertEqual(second.json()["items"][0]["lendableQuantity"], 2)
        self.assertNotIn(str(CLOSED_CUSTODY_ID), first.text + second.text)
        self.assertNotIn(str(OTHER_PENDING_CUSTODY_ID), first.text + second.text)

    async def test_detail_contains_current_custodies_without_private_storage(self) -> None:
        """Detail includes current Custodies and count, never private metadata."""

        response = await self.client.get(f"/api/v1/assets/{ASSET_ID}")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["id"], str(ASSET_ID))
        self.assertEqual(body["availableQuantity"], 2)
        self.assertEqual(
            {item["custodyId"] for item in body["custodies"]},
            {str(ACTIVE_CUSTODY_ID), str(OTHER_PENDING_CUSTODY_ID)},
        )
        active = next(item for item in body["custodies"] if item["status"] == "ACTIVE")
        self.assertEqual(active["evidenceCount"], 1)
        self.assertNotIn("objects/private-read-key", response.text)
        self.assertNotIn("a" * 64, response.text)
        self.assertNotIn("private note", response.text)

    async def test_browse_filters_paginates_and_exposes_only_eligible_custodies(self) -> None:
        """Browse composes filters and includes zero-stock Assets without private evidence."""

        first = await self.client.get("/api/v1/assets?page=1&page_size=1")
        second = await self.client.get("/api/v1/assets?page=2&page_size=1")
        filtered = await self.client.get(
            "/api/v1/assets",
            params={
                "search": "camera",
                "category": str(CATEGORY_ID),
                "team": str(TEAM_ID),
            },
        )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["total"], 2)
        self.assertEqual(first.json()["pageSize"], 1)
        self.assertEqual(first.json()["items"][0]["id"], str(ASSET_ID))
        self.assertEqual(
            first.json()["items"][0]["eligibleCustodies"],
            [
                {
                    "sourceCustodyId": str(ACTIVE_CUSTODY_ID),
                    "lendableQuantity": 2,
                }
            ],
        )
        self.assertEqual(second.json()["items"][0]["id"], str(OTHER_ASSET_ID))
        self.assertEqual(second.json()["items"][0]["availableQuantity"], 0)
        self.assertEqual(second.json()["items"][0]["eligibleCustodies"], [])
        self.assertEqual(filtered.status_code, 200, filtered.text)
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(filtered.json()["items"][0]["responsibleTeamId"], str(TEAM_ID))
        combined = first.text + second.text + filtered.text
        self.assertNotIn(str(PENDING_CUSTODY_ID), combined)
        self.assertNotIn(str(OTHER_PENDING_CUSTODY_ID), combined)
        self.assertNotIn("objects/private-read-key", combined)
        self.assertNotIn("a" * 64, combined)
        self.assertNotIn("private note", combined)

    async def test_browse_rejects_invalid_filters_and_treats_wildcards_literally(self) -> None:
        """Typed filters reject malformed values and search cannot inject LIKE wildcards."""

        invalid_team = await self.client.get("/api/v1/assets?team=not-a-uuid")
        invalid_search = await self.client.get("/api/v1/assets?search=")
        literal_wildcard = await self.client.get("/api/v1/assets?search=%25")

        self._assert_error(invalid_team, 422, "VALIDATION_ERROR")
        self._assert_error(invalid_search, 422, "VALIDATION_ERROR")
        self.assertEqual(literal_wildcard.status_code, 200, literal_wildcard.text)
        self.assertEqual(literal_wildcard.json()["total"], 0)

    async def test_query_rejections_use_stable_errors(self) -> None:
        """Missing, invalid pagination, and inactive identity paths remain standardized."""

        missing = await self.client.get(f"/api/v1/assets/{MISSING_ASSET_ID}")
        self._assert_error(missing, 404, "ASSET_NOT_FOUND")

        invalid = await self.client.get("/api/v1/assets/held?page_size=101")
        self._assert_error(invalid, 422, "VALIDATION_ERROR")

        self.actor = CurrentActor(ACTOR_ID, frozenset(), is_active=False)
        inactive = await self.client.get("/api/v1/assets/held")
        self._assert_error(inactive, 403, "AUTHORIZATION_DENIED")

    async def _cleanup(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text("DELETE FROM custody_evidence WHERE id = :id"),
                {"id": EVIDENCE_ID},
            )
            await session.execute(
                text("DELETE FROM custodies WHERE id = ANY(:ids)"),
                {
                    "ids": [
                        ACTIVE_CUSTODY_ID,
                        PENDING_CUSTODY_ID,
                        OTHER_PENDING_CUSTODY_ID,
                        CLOSED_CUSTODY_ID,
                    ]
                },
            )
            await session.execute(
                text("DELETE FROM assets WHERE id = ANY(:ids)"),
                {"ids": [ASSET_ID, OTHER_ASSET_ID]},
            )
            await session.execute(
                text("DELETE FROM asset_categories WHERE id = :id"),
                {"id": CATEGORY_ID},
            )
            await session.execute(text("DELETE FROM teams WHERE id = :id"), {"id": TEAM_ID})
            await session.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"),
                {"ids": [HR_ID, ACTOR_ID, OTHER_ID]},
            )
            await session.commit()

    def _assert_error(self, response: Response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        UUID(response.json()["traceId"])
