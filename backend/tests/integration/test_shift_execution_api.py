"""PostgreSQL/API lifecycle proof for F08 Shift execution and Event summary."""

from __future__ import annotations

import asyncio
import os
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ams.application.authentication import SessionService
from ams.application.idempotency import IdempotentCommandExecutor
from ams.application.reliability import IClock
from ams.application.shift_execution import ShiftExecutionService
from ams.domain.identity import CurrentActor, OperationalRole
from ams.infrastructure.reliability_persistence import SqlAlchemyUnitOfWork
from ams.interface.api.app import create_app
from ams.interface.api.dependencies import get_current_actor

TEAM_LEAD_ID = UUID("00000000-0000-4000-8000-000000008101")
SHIFT_LEAD_ID = UUID("00000000-0000-4000-8000-000000008102")
OTHER_LEAD_ID = UUID("00000000-0000-4000-8000-000000008103")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000008104")
ASSET_ID = UUID("00000000-0000-4000-8000-000000008105")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000008111")
EVENT_ID = UUID("00000000-0000-4000-8000-000000008106")
FIRST_SHIFT_ID = UUID("00000000-0000-4000-8000-000000008107")
SECOND_SHIFT_ID = UUID("00000000-0000-4000-8000-000000008108")
FIRST_ITEM_ID = UUID("00000000-0000-4000-8000-000000008109")
SECOND_ITEM_ID = UUID("00000000-0000-4000-8000-000000008110")


class UnusedSessionServiceFactory:
    """Satisfy composition while tests override actor resolution."""

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SessionService]:
        """Yield an unused placeholder service."""

        yield cast(SessionService, object())


class MutableClock(IClock):
    """Expose deterministic lifecycle instants to production services."""

    def __init__(self) -> None:
        self.value = datetime(2030, 1, 2, 3, 59, tzinfo=UTC)

    def now(self) -> datetime:
        """Return the current controlled instant."""

        return self.value


class ShiftServiceFactory:
    """Build production F08 service adapters around one controlled clock."""

    def __init__(
        self, session_maker: async_sessionmaker[AsyncSession], clock: MutableClock
    ) -> None:
        self._session_maker = session_maker
        self._clock = clock

    def __call__(self) -> ShiftExecutionService:
        """Return a service whose idempotency and business logic share one clock."""

        def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(self._session_maker)

        return ShiftExecutionService(
            IdempotentCommandExecutor(unit_of_work_factory, self._clock),
            unit_of_work_factory,
            self._clock,
        )


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class ShiftExecutionApiTests(unittest.IsolatedAsyncioTestCase):
    """Exercise locking, formulas, audit, summary gating, and stock isolation."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.clock = MutableClock()
        await self._cleanup()
        await self._seed()
        self.actor = CurrentActor(SHIFT_LEAD_ID, frozenset({OperationalRole.SHIFT_LEAD}))
        application = create_app(
            UnusedSessionServiceFactory(),
            shift_execution_service_factory=ShiftServiceFactory(self.session_maker, self.clock),
        )

        async def current_actor_override() -> CurrentActor:
            return self.actor

        application.dependency_overrides[get_current_actor] = current_actor_override
        self.client = AsyncClient(
            transport=ASGITransport(app=application, raise_app_exceptions=False),
            base_url="https://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self._cleanup()
        await self.engine.dispose()

    async def test_two_shift_carry_over_deviation_summary_and_stock_isolation(self) -> None:
        listed = await self.client.get("/api/v1/shifts")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["total"], 2)

        early = await self._post(FIRST_SHIFT_ID, "open", "early-open")
        self._assert_error(early, 409, "SHIFT_SCHEDULE_UNAVAILABLE")

        self.clock.value = datetime(2030, 1, 2, 4, 0, tzinfo=UTC)
        blocked = await self._post(SECOND_SHIFT_ID, "open", "blocked-next")
        self._assert_error(blocked, 409, "SHIFT_SCHEDULE_UNAVAILABLE")
        opened = await self._post(FIRST_SHIFT_ID, "open", "open-first")
        replay = await self._post(FIRST_SHIFT_ID, "open", "open-first")
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(replay.json(), opened.json())
        self.assertEqual(opened.json()["status"], "OPENING")

        confirmed = await self._post(
            FIRST_SHIFT_ID, "confirm-received", "confirm-first", self._payload(3)
        )
        self.assertEqual(confirmed.json()["status"], "ACTIVE")
        closed = await self._post(FIRST_SHIFT_ID, "close", "close-first", self._payload(1))
        self.assertEqual(closed.json()["items"][0]["consumedQuantity"], 2)

        self.actor = CurrentActor(TEAM_LEAD_ID, frozenset({OperationalRole.TEAM_LEAD_ASSET}))
        partial = await self.client.get(f"/api/v1/event-days/{EVENT_ID}/summary")
        self.assertFalse(partial.json()["complete"])
        self.assertIsNone(partial.json()["items"])

        self.actor = CurrentActor(SHIFT_LEAD_ID, frozenset({OperationalRole.SHIFT_LEAD}))
        self.clock.value = datetime(2030, 1, 2, 5, 0, tzinfo=UTC)
        second = await self._post(SECOND_SHIFT_ID, "open", "open-second")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["items"][0]["carryOverQuantity"], 1)
        self.assertEqual(second.json()["items"][0]["expectedOpenQuantity"], 3)
        deviation = await self._post(
            SECOND_SHIFT_ID,
            "report-opening-deviation",
            "deviation-second",
            {**self._payload(2), "reason": "One cup missing"},
        )
        self.assertEqual(deviation.json()["status"], "ACTIVE_WITH_DEVIATION")
        await self._post(SECOND_SHIFT_ID, "close", "close-second", self._payload(0))

        self.actor = CurrentActor(TEAM_LEAD_ID, frozenset({OperationalRole.HR}))
        summary = await self.client.get(f"/api/v1/event-days/{EVENT_ID}/summary")
        self.assertEqual(summary.status_code, 200, summary.text)
        body = summary.json()
        self.assertTrue(body["complete"])
        self.assertEqual(body["status"], "COMPLETED")
        self.assertEqual(body["items"][0]["totalQuotaQuantity"], 5)
        self.assertEqual(body["items"][0]["totalConsumedQuantity"], 5)
        self.assertEqual(body["items"][0]["openingVarianceQuantity"], -1)
        self.assertEqual(body["items"][0]["finalCarryOverQuantity"], 0)
        self.assertFalse(body["coreStockChanged"])
        self.assertEqual(await self._asset_quantities(), (10, 7))
        self.assertEqual(await self._custody_quantities(), (3, 1, 1))
        self.assertEqual(await self._event_count(), 6)
        self.assertEqual(await self._deviation_outbox_count(), 1)

    async def test_wrong_assignee_and_concurrent_duplicate_close(self) -> None:
        self.actor = CurrentActor(OTHER_LEAD_ID, frozenset({OperationalRole.SHIFT_LEAD}))
        denied = await self._post(FIRST_SHIFT_ID, "open", "wrong-lead")
        self._assert_error(denied, 403, "SHIFT_ACTION_FORBIDDEN")

        self.actor = CurrentActor(SHIFT_LEAD_ID, frozenset({OperationalRole.SHIFT_LEAD}))
        self.clock.value = datetime(2030, 1, 2, 4, 0, tzinfo=UTC)
        await self._post(FIRST_SHIFT_ID, "open", "open")
        await self._post(FIRST_SHIFT_ID, "confirm-received", "confirm", self._payload(3))
        responses = await asyncio.gather(
            self._post(FIRST_SHIFT_ID, "close", "close-a", self._payload(1)),
            self._post(FIRST_SHIFT_ID, "close", "close-b", self._payload(1)),
        )
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        conflict = next(response for response in responses if response.status_code == 409)
        self.assertEqual(conflict.json()["error"]["code"], "INVALID_SHIFT_TRANSITION")
        self.assertEqual(await self._shift_event_count(FIRST_SHIFT_ID, "SHIFT_CLOSED"), 1)

    async def _seed(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:team, 'f08.team', 'f08.team@example.test', 'Team Lead', 'hash'),
                        (:lead, 'f08.lead', 'f08.lead@example.test', 'Shift Lead', 'hash'),
                        (:other, 'f08.other', 'f08.other@example.test', 'Other Lead', 'hash')
                    """
                ),
                {"team": TEAM_LEAD_ID, "lead": SHIFT_LEAD_ID, "other": OTHER_LEAD_ID},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO user_roles (user_id, role_id, granted_by_user_id)
                    SELECT actor_id, roles.id, :team
                    FROM (
                        VALUES (CAST(:lead AS UUID)), (CAST(:other AS UUID))
                    ) AS actors(actor_id)
                    CROSS JOIN roles
                    WHERE roles.code = 'SHIFT_LEAD'
                    """
                ),
                {"team": TEAM_LEAD_ID, "lead": SHIFT_LEAD_ID, "other": OTHER_LEAD_ID},
            )
            await connection.execute(
                text(
                    "INSERT INTO asset_categories (id, code, name) VALUES (:id, 'F08', 'F08')"
                ),
                {"id": CATEGORY_ID},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO assets (
                        id, category_id, name, source, total_quantity,
                        available_quantity, created_by_user_id
                    ) VALUES (:id, :category, 'Cups', 'PURCHASE', 10, 7, :creator)
                    """
                ),
                {"id": ASSET_ID, "category": CATEGORY_ID, "creator": TEAM_LEAD_ID},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO custodies (
                        id, asset_id, custodian_user_id, assigned_by_user_id,
                        status, on_hand_quantity, reserved_quantity,
                        temporarily_loaned_quantity, confirmed_at,
                        confirmed_by_user_id
                    ) VALUES (
                        :id, :asset, :custodian, :assigner, 'ACTIVE', 3, 1, 1,
                        CURRENT_TIMESTAMP, :custodian
                    )
                    """
                ),
                {
                    "id": CUSTODY_ID,
                    "asset": ASSET_ID,
                    "custodian": SHIFT_LEAD_ID,
                    "assigner": TEAM_LEAD_ID,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO event_days (id, name, event_date, created_by_user_id)
                    VALUES (:id, 'F08 Event', '2030-01-02', :creator)
                    """
                ),
                {"id": EVENT_ID, "creator": TEAM_LEAD_ID},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO shifts (
                        id, event_day_id, sequence, name, starts_at, ends_at,
                        shift_lead_user_id
                    ) VALUES
                        (:first, :event, 1, 'Morning', '2030-01-02T04:00:00Z',
                         '2030-01-02T05:00:00Z', :lead),
                        (:second, :event, 2, 'Afternoon', '2030-01-02T05:00:00Z',
                         '2030-01-02T06:00:00Z', :lead)
                    """
                ),
                {
                    "first": FIRST_SHIFT_ID,
                    "second": SECOND_SHIFT_ID,
                    "event": EVENT_ID,
                    "lead": SHIFT_LEAD_ID,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO shift_assets (
                        id, shift_id, asset_id, quota_quantity,
                        carry_over_quantity, expected_open_quantity
                    ) VALUES
                        (:first_item, :first, :asset, 3, 0, 3),
                        (:second_item, :second, :asset, 2, 0, 2)
                    """
                ),
                {
                    "first_item": FIRST_ITEM_ID,
                    "second_item": SECOND_ITEM_ID,
                    "first": FIRST_SHIFT_ID,
                    "second": SECOND_SHIFT_ID,
                    "asset": ASSET_ID,
                },
            )

    async def _cleanup(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM notifications WHERE recipient_id IN (:a, :b, :c)"),
                {"a": TEAM_LEAD_ID, "b": SHIFT_LEAD_ID, "c": OTHER_LEAD_ID},
            )
            await connection.execute(
                text("DELETE FROM outbox_events WHERE aggregate_id IN (:a, :b)"),
                {"a": FIRST_SHIFT_ID, "b": SECOND_SHIFT_ID},
            )
            await connection.execute(
                text("DELETE FROM idempotency_records WHERE actor_id IN (:a, :b)"),
                {"a": SHIFT_LEAD_ID, "b": OTHER_LEAD_ID},
            )
            await connection.execute(
                text("DELETE FROM shift_events WHERE shift_id IN (:a, :b)"),
                {"a": FIRST_SHIFT_ID, "b": SECOND_SHIFT_ID},
            )
            await connection.execute(
                text("DELETE FROM shift_assets WHERE shift_id IN (:a, :b)"),
                {"a": FIRST_SHIFT_ID, "b": SECOND_SHIFT_ID},
            )
            await connection.execute(
                text("DELETE FROM shifts WHERE event_day_id = :id"), {"id": EVENT_ID}
            )
            await connection.execute(
                text("DELETE FROM event_days WHERE id = :id"), {"id": EVENT_ID}
            )
            await connection.execute(
                text("DELETE FROM custodies WHERE id = :id"), {"id": CUSTODY_ID}
            )
            await connection.execute(
                text("DELETE FROM assets WHERE id = :id"), {"id": ASSET_ID}
            )
            await connection.execute(
                text("DELETE FROM asset_categories WHERE id = :id"), {"id": CATEGORY_ID}
            )
            await connection.execute(
                text("DELETE FROM user_roles WHERE user_id IN (:a, :b, :c)"),
                {"a": TEAM_LEAD_ID, "b": SHIFT_LEAD_ID, "c": OTHER_LEAD_ID},
            )
            await connection.execute(
                text("DELETE FROM users WHERE id IN (:a, :b, :c)"),
                {"a": TEAM_LEAD_ID, "b": SHIFT_LEAD_ID, "c": OTHER_LEAD_ID},
            )

    async def _post(
        self,
        shift_id: UUID,
        action: str,
        key: str,
        payload: dict[str, object] | None = None,
    ) -> Response:
        return await self.client.post(
            f"/api/v1/shifts/{shift_id}/{action}",
            headers={"Idempotency-Key": key},
            json=payload,
        )

    @staticmethod
    def _payload(quantity: int) -> dict[str, object]:
        return {"items": [{"assetId": str(ASSET_ID), "actualQuantity": quantity}]}

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

    async def _event_count(self) -> int:
        async with self.engine.connect() as connection:
            return int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM shift_events
                            WHERE shift_id IN (:first, :second)
                            """
                        ),
                        {"first": FIRST_SHIFT_ID, "second": SECOND_SHIFT_ID},
                    )
                ).scalar_one()
            )

    async def _custody_quantities(self) -> tuple[int, int, int]:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT on_hand_quantity, reserved_quantity,
                               temporarily_loaned_quantity
                        FROM custodies WHERE id = :id
                        """
                    ),
                    {"id": CUSTODY_ID},
                )
            ).one()
        return int(row[0]), int(row[1]), int(row[2])

    async def _shift_event_count(self, shift_id: UUID, event_type: str) -> int:
        async with self.engine.connect() as connection:
            return int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM shift_events
                            WHERE shift_id = :shift AND event_type = :event_type
                            """
                        ),
                        {"shift": shift_id, "event_type": event_type},
                    )
                ).scalar_one()
            )

    async def _deviation_outbox_count(self) -> int:
        async with self.engine.connect() as connection:
            return int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM outbox_events
                            WHERE aggregate_id = :shift
                              AND event_type = 'SHIFT_OPENING_DEVIATION_REPORTED'
                            """
                        ),
                        {"shift": SECOND_SHIFT_ID},
                    )
                ).scalar_one()
            )

    def _assert_error(self, response: Response, status_code: int, code: str) -> None:
        self.assertEqual(response.status_code, status_code, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
