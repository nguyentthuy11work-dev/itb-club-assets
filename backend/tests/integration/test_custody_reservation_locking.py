"""PostgreSQL concurrency tests for F05.03 deterministic Custody locking."""

from __future__ import annotations

import asyncio
import os
import unittest
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.domain.assets import InsufficientLendableQuantityError
from ams.infrastructure.asset_persistence import SqlAlchemyCustodyRepository

HR_ID = UUID("00000000-0000-4000-8000-000000005301")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000005302")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000005303")
ASSET_ID = UUID("00000000-0000-4000-8000-000000005304")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000005305")
OTHER_CUSTODY_ID = UUID("00000000-0000-4000-8000-000000005306")
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000005307")
OTHER_EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000005308")


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class CustodyReservationLockingTests(unittest.IsolatedAsyncioTestCase):
    """Prove lock order, duplicate normalization, and concurrent reservation safety."""

    async def asyncSetUp(self) -> None:
        """Insert two active source Custodies with deterministic quantities."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:hr, 'lock.hr', 'lock.hr@example.test', 'Lock HR', 'hash'),
                        (:custodian, 'lock.custodian', 'lock.custodian@example.test',
                         'Lock Custodian', 'hash')
                    """
                ),
                {"hr": HR_ID, "custodian": CUSTODIAN_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'LOCK_CATEGORY', 'Lock Category')
                    """
                ),
                {"id": CATEGORY_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO assets (
                        id, category_id, name, source, total_quantity,
                        available_quantity, created_by_user_id
                    ) VALUES (:id, :category, 'Lock Camera', 'PURCHASE', 10, 10, :hr)
                    """
                ),
                {"id": ASSET_ID, "category": CATEGORY_ID, "hr": HR_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO custodies (
                        id, asset_id, custodian_user_id, assigned_by_user_id, status,
                        on_hand_quantity, condition, assigned_at, confirmed_at,
                        confirmed_by_user_id
                    ) VALUES
                        (:first, :asset, :custodian, :hr, 'ACTIVE', 5, 'GOOD',
                         '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z', :custodian),
                        (:second, :asset, :hr, :hr, 'ACTIVE', 5, 'GOOD',
                         '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z', :hr)
                    """
                ),
                {
                    "first": CUSTODY_ID,
                    "second": OTHER_CUSTODY_ID,
                    "asset": ASSET_ID,
                    "custodian": CUSTODIAN_ID,
                    "hr": HR_ID,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO custody_evidence (
                        id, custody_id, uploaded_by_user_id, evidence_type, object_key,
                        content_type, content_sha256, size_bytes
                    ) VALUES
                        (:first, :first_custody, :custodian, 'ASSIGNMENT_CONFIRMATION',
                         'lock/first', 'image/png', :first_sha, 100),
                        (:second, :second_custody, :hr, 'ASSIGNMENT_CONFIRMATION',
                         'lock/second', 'image/png', :second_sha, 100)
                    """
                ),
                {
                    "first": EVIDENCE_ID,
                    "second": OTHER_EVIDENCE_ID,
                    "first_custody": CUSTODY_ID,
                    "second_custody": OTHER_CUSTODY_ID,
                    "custodian": CUSTODIAN_ID,
                    "hr": HR_ID,
                    "first_sha": "a" * 64,
                    "second_sha": "b" * 64,
                },
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        """Remove committed fixtures and release database resources."""

        await self._cleanup()
        await self.engine.dispose()

    async def test_bulk_lock_normalizes_duplicates_and_orders_uuid_ascending(self) -> None:
        """Callers cannot alter lock order or lock the same source row twice."""

        async with self.session_maker() as session, session.begin():
            rows = await SqlAlchemyCustodyRepository(session).get_many_for_update(
                (OTHER_CUSTODY_ID, CUSTODY_ID, OTHER_CUSTODY_ID)
            )

        self.assertEqual([row.id for row in rows], [CUSTODY_ID, OTHER_CUSTODY_ID])

    async def test_repeated_concurrent_reservations_allow_exactly_one_winner(self) -> None:
        """Competing reservations serialize and the loser sees deterministic insufficiency."""

        for iteration in range(5):
            await self._reset_reservation()
            outcomes = await asyncio.gather(
                self._reserve_four(f"worker-{iteration}-a"),
                self._reserve_four(f"worker-{iteration}-b"),
            )

            self.assertEqual(sorted(outcomes), [False, True])
            async with self.session_maker() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT reserved_quantity, temporarily_loaned_quantity,
                               on_hand_quantity - reserved_quantity
                                   - temporarily_loaned_quantity AS lendable_quantity
                        FROM custodies
                        WHERE id = :id
                        """
                    ),
                    {"id": CUSTODY_ID},
                )
                self.assertEqual(tuple(result.one()), (4, 0, 1))

    async def _reserve_four(self, worker_id: str) -> bool:
        del worker_id
        async with self.session_maker() as session:
            repository = SqlAlchemyCustodyRepository(session)
            try:
                async with session.begin():
                    rows = await repository.get_many_for_update((CUSTODY_ID, CUSTODY_ID))
                    self.assertEqual(len(rows), 1)
                    rows[0].reserve(4)
                    await repository.save(rows[0])
                return True
            except InsufficientLendableQuantityError:
                await session.rollback()
                return False

    async def _reset_reservation(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    UPDATE custodies
                    SET reserved_quantity = 0,
                        temporarily_loaned_quantity = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                    """
                ),
                {"id": CUSTODY_ID},
            )
            await session.commit()

    async def _cleanup(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text("DELETE FROM custody_evidence WHERE id = ANY(:ids)"),
                {"ids": [EVIDENCE_ID, OTHER_EVIDENCE_ID]},
            )
            await session.execute(
                text("DELETE FROM custodies WHERE id = ANY(:ids)"),
                {"ids": [CUSTODY_ID, OTHER_CUSTODY_ID]},
            )
            await session.execute(text("DELETE FROM assets WHERE id = :id"), {"id": ASSET_ID})
            await session.execute(
                text("DELETE FROM asset_categories WHERE id = :id"), {"id": CATEGORY_ID}
            )
            await session.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"),
                {"ids": [HR_ID, CUSTODIAN_ID]},
            )
            await session.commit()
