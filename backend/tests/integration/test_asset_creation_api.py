"""PostgreSQL API integration tests for F02.04 Asset creation."""

from __future__ import annotations

import os
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.authentication import SessionService
from ams.domain.identity import CurrentActor, OperationalRole
from ams.interface.api.app import (
    SqlAlchemyAssetCreationServiceFactory,
    create_app,
)
from ams.interface.api.dependencies import get_current_actor

HR_ID = UUID("00000000-0000-4000-8000-000000001601")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000001602")
OTHER_CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000001603")
TEAM_ID = UUID("00000000-0000-4000-8000-000000001604")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000001605")


class UnusedSessionServiceFactory:
    """Satisfy composition while authentication is dependency-overridden."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder service."""

        yield cast(SessionService, object())


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class AssetCreationApiTests(unittest.IsolatedAsyncioTestCase):
    """Verify HR authorization, input rejection, atomic output, and retry behavior."""

    async def asyncSetUp(self) -> None:
        """Create database fixtures and an API using production command adapters."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:hr, 'create.hr', 'create.hr@example.test', 'Create HR', 'hash'),
                        (:first, 'create.first', 'create.first@example.test',
                         'First Custodian', 'hash'),
                        (:second, 'create.second', 'create.second@example.test',
                         'Second Custodian', 'hash')
                    """
                ),
                {
                    "hr": HR_ID,
                    "first": CUSTODIAN_ID,
                    "second": OTHER_CUSTODIAN_ID,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO teams (id, name, code)
                    VALUES (:id, 'Creation Team', 'CREATION_TEAM')
                    """
                ),
                {"id": TEAM_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'CREATION_CATEGORY', 'Creation Category')
                    """
                ),
                {"id": CATEGORY_ID},
            )
            await session.commit()

        self.actor = CurrentActor(
            HR_ID,
            frozenset({OperationalRole.HR}),
            is_active=True,
            display_name="Create HR",
        )
        application = create_app(
            UnusedSessionServiceFactory(),
            asset_creation_service_factory=SqlAlchemyAssetCreationServiceFactory(
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
        """Close HTTP/database resources and delete committed fixtures."""

        await self.client.aclose()
        await self._cleanup()
        await self.engine.dispose()

    async def test_hr_creation_and_exact_retry_return_one_atomic_effect(self) -> None:
        """Success persists Asset/Custodies/event once and retry returns identical JSON."""

        first = await self._create("asset-request")
        retry = await self._create("asset-request")

        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json(), first.json())
        body = first.json()
        self.assertEqual(len(body["custodies"]), 2)
        self.assertEqual({item["status"] for item in body["custodies"]}, {"PENDING"})
        UUID(body["assetId"])
        counts = await self._counts()
        self.assertEqual(counts, (1, 2, 3, 1))
        payload = await self._event_payload()
        emails = payload["emails"]
        custody_ids = payload["custodyIds"]
        assert isinstance(emails, list)
        assert isinstance(custody_ids, list)
        self.assertEqual(len(emails), 2)
        self.assertEqual(len(custody_ids), 2)
        reminders = await self._reminder_rows()
        self.assertEqual(len(reminders), 2)
        for custody_id, scheduled_at, created_at, reminder_payload in reminders:
            self.assertIn(str(custody_id), custody_ids)
            self.assertEqual(scheduled_at - created_at, timedelta(hours=24))
            reminder_emails = reminder_payload["emails"]
            assert isinstance(reminder_emails, list)
            self.assertEqual(
                {item["recipientId"] for item in reminder_emails},
                {
                    str(HR_ID),
                    str(
                        next(
                            item["custodianUserId"]
                            for item in body["custodies"]
                            if item["id"] == str(custody_id)
                        )
                    ),
                },
            )

        changed_payload = self._payload()
        changed_payload["totalQuantity"] = 4
        conflict = await self.client.post(
            "/api/v1/hr/assets",
            headers={"Idempotency-Key": "asset-request"},
            json=changed_payload,
        )
        self._assert_error(conflict, 409, "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(await self._counts(), counts)

    async def test_non_hr_is_forbidden_without_persistence(self) -> None:
        """The route rejects an authenticated ordinary member before command execution."""

        self.actor = CurrentActor(HR_ID, frozenset(), is_active=True)

        response = await self._create("forbidden-request")

        self._assert_error(response, 403, "AUTHORIZATION_DENIED")
        self.assertEqual(await self._counts(), (0, 0, 0, 0))

    async def test_invalid_total_allocation_rolls_back_idempotency_and_business_rows(
        self,
    ) -> None:
        """Allocations above total return a domain 422 and leave no partial record."""

        payload = self._payload()
        payload["totalQuantity"] = 2

        response = await self.client.post(
            "/api/v1/hr/assets",
            headers={"Idempotency-Key": "invalid-request"},
            json=payload,
        )

        self._assert_error(response, 422, "ALLOCATION_QUANTITY_MISMATCH")
        self.assertEqual(await self._counts(), (0, 0, 0, 0))

    async def _create(self, key: str) -> Response:
        return await self.client.post(
            "/api/v1/hr/assets",
            headers={"Idempotency-Key": key},
            json=self._payload(),
        )

    @staticmethod
    def _payload() -> dict[str, object]:
        return {
            "categoryId": str(CATEGORY_ID),
            "responsibleTeamId": str(TEAM_ID),
            "name": "API Camera",
            "description": "Camera registered through the API",
            "source": "DONATION",
            "totalQuantity": 3,
            "allocations": [
                {
                    "custodianUserId": str(CUSTODIAN_ID),
                    "quantity": 2,
                    "condition": "GOOD",
                    "conditionNote": "New",
                },
                {
                    "custodianUserId": str(OTHER_CUSTODIAN_ID),
                    "quantity": 1,
                    "condition": "DAMAGED",
                    "conditionNote": "Scratched case",
                },
            ],
        }

    async def _counts(self) -> tuple[int, int, int, int]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM assets WHERE created_by_user_id = :hr),
                        (SELECT COUNT(*) FROM custodies WHERE assigned_by_user_id = :hr),
                        (SELECT COUNT(*) FROM outbox_events
                         WHERE (
                             event_type = 'CUSTODY_ASSIGNED'
                             AND aggregate_type = 'ASSET'
                             AND aggregate_id IN (
                                 SELECT id FROM assets WHERE created_by_user_id = :hr
                             )
                         ) OR (
                             event_type = 'CUSTODY_PENDING_REMINDER'
                             AND aggregate_type = 'CUSTODY'
                             AND aggregate_id IN (
                                 SELECT id FROM custodies WHERE assigned_by_user_id = :hr
                             )
                         )),
                        (SELECT COUNT(*) FROM idempotency_records
                         WHERE actor_id = :hr AND command_name = 'CREATE_ASSET')
                    """
                ),
                {"hr": HR_ID},
            )
            row = result.one()
            return int(row[0]), int(row[1]), int(row[2]), int(row[3])

    async def _event_payload(self) -> dict[str, object]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT payload
                    FROM outbox_events
                    WHERE event_type = 'CUSTODY_ASSIGNED'
                      AND aggregate_type = 'ASSET'
                      AND aggregate_id IN (
                          SELECT id FROM assets WHERE created_by_user_id = :hr
                      )
                    """
                ),
                {"hr": HR_ID},
            )
            value = result.scalar_one()
            assert isinstance(value, dict)
            return value

    async def _reminder_rows(
        self,
    ) -> list[tuple[UUID, datetime, datetime, dict[str, object]]]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT aggregate_id, scheduled_at, created_at, payload
                    FROM outbox_events
                    WHERE event_type = 'CUSTODY_PENDING_REMINDER'
                      AND aggregate_type = 'CUSTODY'
                      AND aggregate_id IN (
                          SELECT id FROM custodies WHERE assigned_by_user_id = :hr
                      )
                    ORDER BY aggregate_id
                    """
                ),
                {"hr": HR_ID},
            )
            return [
                (row.aggregate_id, row.scheduled_at, row.created_at, dict(row.payload))
                for row in result
            ]

    async def _cleanup(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    DELETE FROM notifications
                    WHERE outbox_event_id IN (
                        SELECT id FROM outbox_events
                        WHERE (
                            event_type = 'CUSTODY_ASSIGNED'
                            AND aggregate_type = 'ASSET'
                            AND aggregate_id IN (
                                SELECT id FROM assets WHERE created_by_user_id = :hr
                            )
                        ) OR (
                            event_type = 'CUSTODY_PENDING_REMINDER'
                            AND aggregate_type = 'CUSTODY'
                            AND aggregate_id IN (
                                SELECT id FROM custodies WHERE assigned_by_user_id = :hr
                            )
                        )
                    )
                    """
                ),
                {"hr": HR_ID},
            )
            await session.execute(
                text(
                    """
                    DELETE FROM outbox_events
                    WHERE (
                        event_type = 'CUSTODY_ASSIGNED'
                        AND aggregate_type = 'ASSET'
                        AND aggregate_id IN (
                            SELECT id FROM assets WHERE created_by_user_id = :hr
                        )
                    ) OR (
                        event_type = 'CUSTODY_PENDING_REMINDER'
                        AND aggregate_type = 'CUSTODY'
                        AND aggregate_id IN (
                            SELECT id FROM custodies WHERE assigned_by_user_id = :hr
                        )
                    )
                    """
                ),
                {"hr": HR_ID},
            )
            await session.execute(
                text("DELETE FROM idempotency_records WHERE actor_id = :id"),
                {"id": HR_ID},
            )
            await session.execute(
                text("DELETE FROM custodies WHERE assigned_by_user_id = :id"),
                {"id": HR_ID},
            )
            await session.execute(
                text("DELETE FROM assets WHERE created_by_user_id = :id"),
                {"id": HR_ID},
            )
            await session.execute(
                text("DELETE FROM asset_categories WHERE id = :id"),
                {"id": CATEGORY_ID},
            )
            await session.execute(
                text("DELETE FROM teams WHERE id = :id"),
                {"id": TEAM_ID},
            )
            await session.execute(
                text("DELETE FROM users WHERE id IN (:hr, :first, :second)"),
                {
                    "hr": HR_ID,
                    "first": CUSTODIAN_ID,
                    "second": OTHER_CUSTODIAN_ID,
                },
            )
            await session.commit()

    def _assert_error(self, response: Response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        UUID(response.json()["traceId"])
