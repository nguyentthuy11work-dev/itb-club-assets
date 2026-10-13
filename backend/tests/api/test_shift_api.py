"""HTTP contract tests for F08 Shift execution routes without PostgreSQL."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient

from ams.application.authentication import SessionService
from ams.application.shift_execution import (
    ShiftExecutionService,
    ShiftPage,
    ShiftQuantityCommand,
    ShiftResult,
)
from ams.domain.events import EventDayStatus, ShiftStatus
from ams.domain.identity import CurrentActor, OperationalRole
from ams.domain.shift_execution import (
    EventDayAssetSummary,
    EventDayExecutionSummary,
    ShiftExecution,
    ShiftExecutionItem,
    ShiftScheduleUnavailableError,
)
from ams.interface.api.app import create_app
from ams.interface.api.dependencies import get_current_actor, get_shift_execution_service

ACTOR_ID = UUID("00000000-0000-4000-8000-000000008301")
EVENT_ID = UUID("00000000-0000-4000-8000-000000008302")
SHIFT_ID = UUID("00000000-0000-4000-8000-000000008303")
ITEM_ID = UUID("00000000-0000-4000-8000-000000008304")
ASSET_ID = UUID("00000000-0000-4000-8000-000000008305")


class UnusedSessionServiceFactory:
    """Satisfy application composition while auth is overridden."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder service."""

        yield cast(SessionService, object())


class FakeShiftService:
    """Record parsed route input and return deterministic F08 results."""

    def __init__(self) -> None:
        self.quantities: tuple[ShiftQuantityCommand, ...] = ()
        self.reason: str | None = None
        self.reject_open = False

    async def list(self, actor: CurrentActor, *, offset: int = 0, limit: int = 20) -> ShiftPage:
        return ShiftPage((_shift(),), 1, offset, limit)

    async def get(self, shift_id: UUID, actor: CurrentActor) -> ShiftResult:
        return ShiftResult(_shift())

    async def open(
        self, shift_id: UUID, actor: CurrentActor, idempotency_key: str
    ) -> ShiftResult:
        if self.reject_open:
            raise ShiftScheduleUnavailableError("A Shift cannot open before its start.")
        return ShiftResult(_shift(status=ShiftStatus.OPENING))

    async def confirm_received(
        self,
        shift_id: UUID,
        quantities: tuple[ShiftQuantityCommand, ...],
        actor: CurrentActor,
        idempotency_key: str,
    ) -> ShiftResult:
        self.quantities = quantities
        return ShiftResult(_shift(status=ShiftStatus.ACTIVE, opening=3))

    async def report_opening_deviation(
        self,
        shift_id: UUID,
        quantities: tuple[ShiftQuantityCommand, ...],
        reason: str,
        actor: CurrentActor,
        idempotency_key: str,
    ) -> ShiftResult:
        self.quantities = quantities
        self.reason = reason
        return ShiftResult(
            _shift(
                status=ShiftStatus.ACTIVE_WITH_DEVIATION,
                opening=2,
                reason=reason,
            )
        )

    async def close(
        self,
        shift_id: UUID,
        quantities: tuple[ShiftQuantityCommand, ...],
        actor: CurrentActor,
        idempotency_key: str,
    ) -> ShiftResult:
        self.quantities = quantities
        return ShiftResult(_shift(status=ShiftStatus.CLOSED, opening=3, closing=1))

    async def get_summary(
        self, event_day_id: UUID, actor: CurrentActor
    ) -> EventDayExecutionSummary:
        return EventDayExecutionSummary(
            EVENT_ID,
            EventDayStatus.COMPLETED,
            1,
            1,
            (EventDayAssetSummary(ASSET_ID, 3, 2, 0, 1),),
        )


class ShiftApiTests(unittest.IsolatedAsyncioTestCase):
    """Verify canonical aliases, strict requests, headers, and stable errors."""

    async def asyncSetUp(self) -> None:
        self.service = FakeShiftService()
        application = create_app(UnusedSessionServiceFactory())
        application.dependency_overrides[get_current_actor] = lambda: CurrentActor(
            ACTOR_ID, frozenset({OperationalRole.SHIFT_LEAD})
        )
        application.dependency_overrides[get_shift_execution_service] = lambda: cast(
            ShiftExecutionService, self.service
        )
        self.client = AsyncClient(
            transport=ASGITransport(app=application, raise_app_exceptions=False),
            base_url="https://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_list_read_and_all_commands_use_canonical_contract(self) -> None:
        listed = await self.client.get("/api/v1/shifts?page=1&page_size=10")
        detail = await self.client.get(f"/api/v1/shifts/{SHIFT_ID}")
        opened = await self.client.post(
            f"/api/v1/shifts/{SHIFT_ID}/open", headers={"Idempotency-Key": "open"}
        )
        confirmed = await self.client.post(
            f"/api/v1/shifts/{SHIFT_ID}/confirm-received",
            headers={"Idempotency-Key": "confirm"},
            json=_payload(3),
        )
        deviation = await self.client.post(
            f"/api/v1/shifts/{SHIFT_ID}/report-opening-deviation",
            headers={"Idempotency-Key": "deviation"},
            json={**_payload(2), "reason": "One missing"},
        )
        closed = await self.client.post(
            f"/api/v1/shifts/{SHIFT_ID}/close",
            headers={"Idempotency-Key": "close"},
            json=_payload(1),
        )
        summary = await self.client.get(f"/api/v1/event-days/{EVENT_ID}/summary")

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["pageSize"], 10)
        self.assertEqual(detail.json()["items"][0]["expectedOpenQuantity"], 3)
        self.assertEqual(opened.json()["status"], "OPENING")
        self.assertEqual(confirmed.json()["status"], "ACTIVE")
        self.assertEqual(deviation.json()["status"], "ACTIVE_WITH_DEVIATION")
        self.assertEqual(self.service.reason, "One missing")
        self.assertEqual(closed.json()["items"][0]["consumedQuantity"], 2)
        self.assertFalse(closed.json()["coreStockChanged"])
        self.assertTrue(summary.json()["complete"])
        self.assertEqual(summary.json()["items"][0]["totalConsumedQuantity"], 2)

    async def test_headers_validation_and_schedule_error_are_normalized(self) -> None:
        missing_header = await self.client.post(f"/api/v1/shifts/{SHIFT_ID}/open")
        self.assertEqual(missing_header.status_code, 422)
        self.assertEqual(missing_header.json()["error"]["code"], "VALIDATION_ERROR")

        invalid = await self.client.post(
            f"/api/v1/shifts/{SHIFT_ID}/confirm-received",
            headers={"Idempotency-Key": "invalid"},
            json={"items": [{"assetId": str(ASSET_ID), "actualQuantity": -1}]},
        )
        self.assertEqual(invalid.status_code, 422)

        self.service.reject_open = True
        conflict = await self.client.post(
            f"/api/v1/shifts/{SHIFT_ID}/open",
            headers={"Idempotency-Key": "conflict"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "SHIFT_SCHEDULE_UNAVAILABLE")


def _shift(
    *,
    status: ShiftStatus = ShiftStatus.SCHEDULED,
    opening: int | None = None,
    closing: int | None = None,
    reason: str | None = None,
) -> ShiftExecution:
    return ShiftExecution(
        SHIFT_ID,
        EVENT_ID,
        "Event",
        ACTOR_ID,
        1,
        "Morning",
        datetime(2030, 1, 2, 4, 0, tzinfo=UTC),
        datetime(2030, 1, 2, 5, 0, tzinfo=UTC),
        ACTOR_ID,
        (
            ShiftExecutionItem(
                ITEM_ID,
                ASSET_ID,
                3,
                opening_actual_quantity=opening,
                closing_actual_quantity=closing,
                consumed_quantity=None if closing is None else 3 - closing,
                discrepancy_reason=reason,
            ),
        ),
        status,
    )


def _payload(value: int) -> dict[str, object]:
    return {"items": [{"assetId": str(ASSET_ID), "actualQuantity": value}]}
