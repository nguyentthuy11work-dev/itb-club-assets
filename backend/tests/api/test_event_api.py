"""HTTP contract tests for F07 Event Day routes without PostgreSQL."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient

from ams.application.authentication import SessionService
from ams.application.event_days import ConfigureEventDayCommand, EventDayResult, EventDayService
from ams.domain.events import EventDay, EventScheduleConflictError, Shift, ShiftAsset
from ams.domain.identity import CurrentActor, OperationalRole
from ams.interface.api.app import create_app
from ams.interface.api.dependencies import get_current_actor, get_event_day_service

ACTOR_ID = UUID("00000000-0000-4000-8000-000000007301")
EVENT_ID = UUID("00000000-0000-4000-8000-000000007302")
SHIFT_ID = UUID("00000000-0000-4000-8000-000000007303")
SHIFT_LEAD_ID = UUID("00000000-0000-4000-8000-000000007304")
ASSET_ID = UUID("00000000-0000-4000-8000-000000007305")
START = datetime(2030, 1, 2, 4, 0, tzinfo=UTC)


class UnusedSessionServiceFactory:
    """Satisfy application composition while auth is overridden."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder service."""

        yield cast(SessionService, object())


class FakeEventDayService:
    """Record typed route inputs and return one deterministic aggregate."""

    def __init__(self) -> None:
        self.command: ConfigureEventDayCommand | None = None
        self.reject_update = False

    async def create(
        self,
        command: ConfigureEventDayCommand,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> EventDayResult:
        self.command = command
        return EventDayResult(_event(command.name))

    async def get(self, event_day_id: UUID, actor: CurrentActor) -> EventDayResult:
        return EventDayResult(_event("Event"))

    async def update(
        self,
        event_day_id: UUID,
        command: ConfigureEventDayCommand,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> EventDayResult:
        if self.reject_update:
            raise EventScheduleConflictError("An Event Day cannot change after a Shift begins.")
        self.command = command
        return EventDayResult(_event(command.name))


class EventApiTests(unittest.IsolatedAsyncioTestCase):
    """Verify strict parsing, response aliases, headers, and stable errors."""

    async def asyncSetUp(self) -> None:
        service = FakeEventDayService()
        self.service = service
        application = create_app(UnusedSessionServiceFactory())
        application.dependency_overrides[get_current_actor] = lambda: CurrentActor(
            ACTOR_ID, frozenset({OperationalRole.TEAM_LEAD_ASSET})
        )
        application.dependency_overrides[get_event_day_service] = lambda: cast(
            EventDayService, service
        )
        self.client = AsyncClient(
            transport=ASGITransport(app=application, raise_app_exceptions=False),
            base_url="https://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_create_get_and_update_use_canonical_contract(self) -> None:
        created = await self.client.post(
            "/api/v1/event-days",
            headers={"Idempotency-Key": "create"},
            json=_payload(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["eventDate"], "2030-01-02")
        self.assertEqual(created.json()["shifts"][0]["assets"][0]["quotaQuantity"], 3)
        assert self.service.command is not None
        self.assertEqual(self.service.command.shifts[0].shift_lead_user_id, SHIFT_LEAD_ID)

        read = await self.client.get(f"/api/v1/event-days/{EVENT_ID}")
        self.assertEqual(read.status_code, 200, read.text)

        payload = _payload()
        payload["name"] = "Updated"
        updated = await self.client.patch(
            f"/api/v1/event-days/{EVENT_ID}",
            headers={"Idempotency-Key": "update"},
            json=payload,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["name"], "Updated")

    async def test_validation_and_schedule_conflict_use_standard_errors(self) -> None:
        missing_header = await self.client.post("/api/v1/event-days", json=_payload())
        self.assertEqual(missing_header.status_code, 422)
        self.assertEqual(missing_header.json()["error"]["code"], "VALIDATION_ERROR")

        invalid = _payload()
        invalid["unknown"] = True
        response = await self.client.post(
            "/api/v1/event-days",
            headers={"Idempotency-Key": "invalid"},
            json=invalid,
        )
        self.assertEqual(response.status_code, 422)

        self.service.reject_update = True
        response = await self.client.patch(
            f"/api/v1/event-days/{EVENT_ID}",
            headers={"Idempotency-Key": "conflict"},
            json=_payload(),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "EVENT_SCHEDULE_CONFLICT")


def _event(name: str) -> EventDay:
    return EventDay(
        id=EVENT_ID,
        name=name,
        event_date=date(2030, 1, 2),
        created_by_user_id=ACTOR_ID,
        shifts=(
            Shift(
                id=SHIFT_ID,
                sequence=1,
                name="Morning",
                starts_at=START,
                ends_at=datetime(2030, 1, 2, 5, 0, tzinfo=UTC),
                shift_lead_user_id=SHIFT_LEAD_ID,
                assets=(ShiftAsset(UUID(int=7306), ASSET_ID, 3),),
            ),
        ),
    )


def _payload() -> dict[str, object]:
    return {
        "name": "Event",
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
