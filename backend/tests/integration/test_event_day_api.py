"""PostgreSQL/API integration proof for F07 Event Day configuration."""

from __future__ import annotations

import os
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.authentication import SessionService
from ams.domain.identity import CurrentActor, OperationalRole
from ams.interface.api.app import SqlAlchemyEventDayServiceFactory, create_app
from ams.interface.api.dependencies import get_current_actor

TEAM_LEAD_ID = UUID("00000000-0000-4000-8000-000000007101")
SHIFT_LEAD_ID = UUID("00000000-0000-4000-8000-000000007102")
OUTSIDER_ID = UUID("00000000-0000-4000-8000-000000007103")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000007104")
ASSET_ID = UUID("00000000-0000-4000-8000-000000007105")


class UnusedSessionServiceFactory:
    """Satisfy composition while tests override actor resolution."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder service."""

        yield cast(SessionService, object())


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class EventDayApiTests(unittest.IsolatedAsyncioTestCase):
    """Exercise role checks, persistence, replay, replacement, and reminder slots."""

    async def asyncSetUp(self) -> None:
        """Seed reference identities/Asset and wire production Event adapters."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        await self._seed()
        self.actor = CurrentActor(TEAM_LEAD_ID, frozenset({OperationalRole.TEAM_LEAD_ASSET}))
        application = create_app(
            UnusedSessionServiceFactory(),
            event_day_service_factory=SqlAlchemyEventDayServiceFactory(self.session_maker),
        )

        async def current_actor_override() -> CurrentActor:
            return self.actor

        application.dependency_overrides[get_current_actor] = current_actor_override
        self.client = AsyncClient(
            transport=ASGITransport(app=application, raise_app_exceptions=False),
            base_url="https://testserver",
        )

    async def asyncTearDown(self) -> None:
        """Remove all deterministic fixture records and close resources."""

        await self.client.aclose()
        await self._cleanup()
        await self.engine.dispose()

    async def test_create_read_update_and_reminder_replacement(self) -> None:
        """Configuration is idempotent and only unseen reminder slots move."""

        created = await self.client.post(
            "/api/v1/event-days",
            headers={"Idempotency-Key": "event-create"},
            json=self._payload(),
        )
        replay = await self.client.post(
            "/api/v1/event-days",
            headers={"Idempotency-Key": "event-create"},
            json=self._payload(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(replay.json(), created.json())
        body = created.json()
        event_day_id = UUID(body["id"])
        shift_id = UUID(body["shifts"][0]["id"])
        self.assertFalse(body["coreStockChanged"])
        self.assertEqual(await self._asset_quantities(), (10, 7))
        reminders = await self._reminders(shift_id)
        self.assertEqual(
            [(row[1], row[2]) for row in reminders],
            [
                ("SHIFT_CLOSE_REMINDER", "2030-01-02T04:30:00+00:00"),
                ("SHIFT_OPEN_REMINDER", "2030-01-02T03:30:00+00:00"),
            ],
        )
        await self._mark_delivered(reminders[1][0])

        payload = self._payload()
        payload["name"] = "Updated Event"
        shift = cast(dict[str, object], cast(list[object], payload["shifts"])[0])
        shift["id"] = str(shift_id)
        shift["startsAt"] = "2030-01-02T05:00:00Z"
        shift["endsAt"] = "2030-01-02T06:00:00Z"
        updated = await self.client.patch(
            f"/api/v1/event-days/{event_day_id}",
            headers={"Idempotency-Key": "event-update"},
            json=payload,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["name"], "Updated Event")
        after = await self._reminders(shift_id)
        self.assertEqual(len(after), 2)
        self.assertEqual(after[0][2], "2030-01-02T05:30:00+00:00")
        self.assertEqual(after[1][0], reminders[1][0])

        read = await self.client.get(f"/api/v1/event-days/{event_day_id}")
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json(), updated.json())
        self.assertEqual(await self._asset_quantities(), (10, 7))

    async def test_authorization_validation_and_reference_errors_are_stable(self) -> None:
        """Role, schedule, and unavailable reference failures never persist Event rows."""

        self.actor = CurrentActor(OUTSIDER_ID, frozenset())
        denied = await self.client.post(
            "/api/v1/event-days",
            headers={"Idempotency-Key": "denied"},
            json=self._payload(),
        )
        self._assert_error(denied, 403, "AUTHORIZATION_DENIED")

        self.actor = CurrentActor(TEAM_LEAD_ID, frozenset({OperationalRole.TEAM_LEAD_ASSET}))
        invalid = self._payload()
        cast(dict[str, object], cast(list[object], invalid["shifts"])[0])[
            "endsAt"
        ] = "2030-01-02T03:00:00Z"
        response = await self.client.post(
            "/api/v1/event-days",
            headers={"Idempotency-Key": "invalid-time"},
            json=invalid,
        )
        self._assert_error(response, 422, "VALIDATION_ERROR")

        missing = self._payload()
        cast(dict[str, object], cast(list[object], missing["shifts"])[0])["shiftLeadUserId"] = (
            str(UUID(int=9999))
        )
        response = await self.client.post(
            "/api/v1/event-days",
            headers={"Idempotency-Key": "missing-lead"},
            json=missing,
        )
        self._assert_error(response, 404, "EVENT_REFERENCE_UNAVAILABLE")

    @staticmethod
    def _payload() -> dict[str, object]:
        return {
            "name": "Tet Booth",
            "eventDate": "2030-01-02",
            "shifts": [
                {
                    "sequence": 1,
                    "name": "Morning",
                    "startsAt": "2030-01-02T04:00:00Z",
                    "endsAt": "2030-01-02T05:00:00Z",
                    "shiftLeadUserId": str(SHIFT_LEAD_ID),
                    "assets": [{"assetId": str(ASSET_ID), "quotaQuantity": 3}],
                }
            ],
        }

    async def _seed(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:team_lead, 'event.teamlead', 'event.teamlead@example.test',
                         'Event Team Lead', 'hash'),
                        (:shift_lead, 'event.shiftlead', 'event.shiftlead@example.test',
                         'Event Shift Lead', 'hash'),
                        (:outsider, 'event.outsider', 'event.outsider@example.test',
                         'Event Outsider', 'hash')
                    """
                ),
                {
                    "team_lead": TEAM_LEAD_ID,
                    "shift_lead": SHIFT_LEAD_ID,
                    "outsider": OUTSIDER_ID,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO user_roles (user_id, role_id, granted_by_user_id)
                    SELECT :shift_lead, id, :team_lead FROM roles WHERE code = 'SHIFT_LEAD'
                    """
                ),
                {"shift_lead": SHIFT_LEAD_ID, "team_lead": TEAM_LEAD_ID},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:category_id, 'EVENT_TEST', 'Event Test')
                    """
                ),
                {"category_id": CATEGORY_ID},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO assets (
                        id, category_id, name, source, total_quantity,
                        available_quantity, created_by_user_id
                    ) VALUES (
                        :asset_id, :category_id, 'Event Cups', 'PURCHASE', 10, 7, :creator
                    )
                    """
                ),
                {
                    "asset_id": ASSET_ID,
                    "category_id": CATEGORY_ID,
                    "creator": TEAM_LEAD_ID,
                },
            )

    async def _cleanup(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM notifications WHERE recipient_id IN (:a, :b, :c)"),
                {"a": TEAM_LEAD_ID, "b": SHIFT_LEAD_ID, "c": OUTSIDER_ID},
            )
            await connection.execute(
                text("DELETE FROM outbox_events WHERE aggregate_type = 'SHIFT'")
            )
            await connection.execute(
                text("DELETE FROM idempotency_records WHERE actor_id IN (:a, :c)"),
                {"a": TEAM_LEAD_ID, "c": OUTSIDER_ID},
            )
            await connection.execute(text("DELETE FROM shift_events"))
            await connection.execute(text("DELETE FROM shift_assets"))
            await connection.execute(text("DELETE FROM shifts"))
            await connection.execute(text("DELETE FROM event_days"))
            await connection.execute(
                text("DELETE FROM assets WHERE id = :id"), {"id": ASSET_ID}
            )
            await connection.execute(
                text("DELETE FROM asset_categories WHERE id = :id"), {"id": CATEGORY_ID}
            )
            await connection.execute(
                text("DELETE FROM user_roles WHERE user_id IN (:a, :b, :c)"),
                {"a": TEAM_LEAD_ID, "b": SHIFT_LEAD_ID, "c": OUTSIDER_ID},
            )
            await connection.execute(
                text("DELETE FROM users WHERE id IN (:a, :b, :c)"),
                {"a": TEAM_LEAD_ID, "b": SHIFT_LEAD_ID, "c": OUTSIDER_ID},
            )

    async def _asset_quantities(self) -> tuple[int, int]:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT total_quantity, available_quantity FROM assets WHERE id = :id"
                    ),
                    {"id": ASSET_ID},
                )
            ).one()
        return int(row[0]), int(row[1])

    async def _reminders(self, shift_id: UUID) -> list[tuple[UUID, str, str]]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT id, event_type, scheduled_at
                        FROM outbox_events
                        WHERE aggregate_type = 'SHIFT' AND aggregate_id = :shift_id
                        ORDER BY event_type, id
                        """
                    ),
                    {"shift_id": shift_id},
                )
            ).all()
        return [(row[0], str(row[1]), row[2].astimezone(UTC).isoformat()) for row in rows]

    async def _mark_delivered(self, event_id: UUID) -> None:
        timestamp = datetime(2030, 1, 1, tzinfo=UTC)
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE outbox_events
                    SET status = 'DELIVERED', completed_at = :timestamp, updated_at = :timestamp
                    WHERE id = :event_id
                    """
                ),
                {"event_id": event_id, "timestamp": timestamp},
            )

    def _assert_error(self, response: Response, status_code: int, code: str) -> None:
        self.assertEqual(response.status_code, status_code, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertIn("traceId", response.json())
