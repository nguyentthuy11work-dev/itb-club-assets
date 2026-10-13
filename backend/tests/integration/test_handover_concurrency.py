"""PostgreSQL snapshot/stale and simultaneous Handover confirmation proofs."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.handovers import HandoverService
from ams.application.idempotency import IdempotentCommandExecutor
from ams.application.reliability import IClock
from ams.domain.handovers import HandoverStatus
from ams.domain.identity import CurrentActor, OperationalRole
from ams.infrastructure.reliability_persistence import SqlAlchemyUnitOfWork

OLD_HR_ID = UUID("00000000-0000-4000-8000-000000013201")
NEW_HR_ID = UUID("00000000-0000-4000-8000-000000013202")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000013203")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000013204")
STALE_ASSET_ID = UUID("00000000-0000-4000-8000-000000013205")
START_ASSET_ID = UUID("00000000-0000-4000-8000-000000013206")
END_ASSET_ID = UUID("00000000-0000-4000-8000-000000013207")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000013208")
TERM_ID = UUID("00000000-0000-4000-8000-000000013209")
NOW = datetime(2030, 2, 2, 3, 0, tzinfo=UTC)
TERM_START_UTC = datetime(2029, 12, 31, 17, 0, tzinfo=UTC)
TERM_END_UTC = datetime(2030, 1, 31, 16, 59, 59, tzinfo=UTC)


class FixedClock(IClock):
    """Return one deterministic post-term snapshot time."""

    def now(self) -> datetime:
        """Return fixed UTC time."""

        return NOW


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class HandoverConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """Exercise production snapshot queries, row locks, and immutable persistence."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        await self._seed()

    async def asyncTearDown(self) -> None:
        await self._cleanup()
        await self.engine.dispose()

    async def test_boundaries_excluded_event_snapshot_and_confirmation_race(self) -> None:
        """Flag stale correctly, freeze values, and lock once under simultaneous sides."""

        service = self._service()
        old_actor = CurrentActor(OLD_HR_ID, frozenset({OperationalRole.HR}))
        new_actor = CurrentActor(NEW_HR_ID, frozenset({OperationalRole.HR}))
        created = await service.create(TERM_ID, old_actor, "create-handover-race")
        by_asset = {item.asset_id: item for item in created.handover.items}
        self.assertTrue(by_asset[STALE_ASSET_ID].is_stale)
        self.assertFalse(by_asset[START_ASSET_ID].is_stale)
        self.assertFalse(by_asset[END_ASSET_ID].is_stale)

        async with self.session_maker.begin() as session:
            await session.execute(
                text(
                    """
                    UPDATE assets SET name = 'Changed later', total_quantity = 3,
                                      available_quantity = 3
                    WHERE id = :asset
                    """
                ),
                {"asset": STALE_ASSET_ID},
            )
            await session.execute(
                text(
                    """
                    UPDATE custodies SET on_hand_quantity = 1
                    WHERE id = :custody
                    """
                ),
                {"custody": CUSTODY_ID},
            )
        frozen = await service.get(created.handover.id, old_actor)
        frozen_item = next(
            item for item in frozen.handover.items if item.asset_id == STALE_ASSET_ID
        )
        self.assertEqual(frozen_item.asset_name, "Stale Camera")
        self.assertEqual(frozen_item.total_quantity, 2)
        self.assertEqual(frozen_item.custodies[0].on_hand_quantity, 2)

        old_result, new_result = await asyncio.gather(
            service.confirm_old(created.handover.id, old_actor, "confirm-old-race"),
            service.confirm_new(created.handover.id, new_actor, "confirm-new-race"),
        )
        self.assertIn(
            HandoverStatus.LOCKED,
            {old_result.handover.status, new_result.handover.status},
        )
        final = await service.get(created.handover.id, new_actor)
        self.assertEqual(final.handover.status, HandoverStatus.LOCKED)
        self.assertIsNotNone(final.handover.old_confirmed_at)
        self.assertIsNotNone(final.handover.new_confirmed_at)

    def _service(self) -> HandoverService:
        def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(self.session_maker)

        clock = FixedClock()
        return HandoverService(
            IdempotentCommandExecutor(unit_of_work_factory, clock),
            unit_of_work_factory,
            clock,
        )

    async def _seed(self) -> None:
        async with self.session_maker.begin() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:old, 'hc.old', 'hc.old@example.test', 'Old HR', 'hash'),
                        (:new, 'hc.new', 'hc.new@example.test', 'New HR', 'hash'),
                        (:custodian, 'hc.c', 'hc.c@example.test', 'Custodian', 'hash')
                    """
                ),
                {"old": OLD_HR_ID, "new": NEW_HR_ID, "custodian": CUSTODIAN_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'HANDOVER_RACE', 'Handover race')
                    """
                ),
                {"id": CATEGORY_ID},
            )
            for asset_id, name, created_at in (
                (STALE_ASSET_ID, "Stale Camera", datetime(2029, 1, 1, tzinfo=UTC)),
                (START_ASSET_ID, "Start Camera", TERM_START_UTC),
                (END_ASSET_ID, "End Camera", TERM_END_UTC),
            ):
                await session.execute(
                    text(
                        """
                        INSERT INTO assets (
                            id, category_id, name, source, total_quantity,
                            available_quantity, created_by_user_id, created_at, updated_at
                        ) VALUES (:id, :category, :name, 'PURCHASE', 2, 2, :old,
                                  :created_at, :created_at)
                        """
                    ),
                    {
                        "id": asset_id,
                        "category": CATEGORY_ID,
                        "name": name,
                        "old": OLD_HR_ID,
                        "created_at": created_at,
                    },
                )
            await session.execute(
                text(
                    """
                    INSERT INTO custodies (
                        id, asset_id, custodian_user_id, assigned_by_user_id, status,
                        on_hand_quantity, condition, assigned_at, confirmed_at,
                        confirmed_by_user_id, created_at, updated_at
                    ) VALUES (
                        :id, :asset, :custodian, :old, 'ACTIVE', 2, 'GOOD',
                        :assigned, :assigned, :custodian, :assigned, :assigned
                    )
                    """
                ),
                {
                    "id": CUSTODY_ID,
                    "asset": STALE_ASSET_ID,
                    "custodian": CUSTODIAN_ID,
                    "old": OLD_HR_ID,
                    "assigned": datetime(2029, 1, 2, tzinfo=UTC),
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO outbox_events (
                        event_type, aggregate_type, aggregate_id, payload, status,
                        scheduled_at, next_attempt_at, created_at, updated_at
                    ) VALUES (
                        'RECONCILIATION_REMINDER', 'ASSET', :asset, '{}', 'PENDING',
                        :inside_term, :inside_term, :inside_term, :inside_term
                    )
                    """
                ),
                {"asset": STALE_ASSET_ID, "inside_term": TERM_START_UTC},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO terms (
                        id, code, name, starts_on, ends_on, old_hr_user_id,
                        new_hr_user_id, created_by_user_id, created_at, updated_at
                    ) VALUES (
                        :id, '2029-2030', 'Term 2029-2030', '2030-01-01',
                        '2030-01-31', :old, :new, :old, :now, :now
                    )
                    """
                ),
                {"id": TERM_ID, "old": OLD_HR_ID, "new": NEW_HR_ID, "now": NOW},
            )

    async def _cleanup(self) -> None:
        async with self.session_maker.begin() as session:
            # Locked records deliberately reject normal deletion. Test teardown
            # bypasses user triggers only for these deterministic fixture IDs.
            await session.execute(text("SET LOCAL session_replication_role = replica"))
            params = {
                "old": OLD_HR_ID,
                "new": NEW_HR_ID,
                "custodian": CUSTODIAN_ID,
                "category": CATEGORY_ID,
                "term": TERM_ID,
                "stale": STALE_ASSET_ID,
                "start": START_ASSET_ID,
                "end": END_ASSET_ID,
            }
            for statement in (
                "DELETE FROM notifications WHERE recipient_id IN (:old, :new)",
                "DELETE FROM handover_items WHERE handover_id IN "
                "(SELECT id FROM handovers WHERE term_id = :term)",
                "DELETE FROM outbox_events WHERE aggregate_id IN "
                "(SELECT id FROM handovers WHERE term_id = :term)",
                "DELETE FROM handovers WHERE term_id = :term",
                "DELETE FROM terms WHERE id = :term",
                "DELETE FROM idempotency_records WHERE actor_id IN (:old, :new)",
                "DELETE FROM outbox_events WHERE aggregate_id IN (:stale, :start, :end)",
                "DELETE FROM custodies WHERE asset_id IN (:stale, :start, :end)",
                "DELETE FROM assets WHERE id IN (:stale, :start, :end)",
                "DELETE FROM asset_categories WHERE id = :category",
                "DELETE FROM users WHERE id IN (:old, :new, :custodian)",
            ):
                await session.execute(text(statement), params)
