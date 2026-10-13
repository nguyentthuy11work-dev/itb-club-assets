"""PostgreSQL concurrency proofs for F09 resolve/close and one-open policies."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.evidence import EvidenceStorageService
from ams.application.idempotency import IdempotentCommandExecutor
from ams.application.reconciliations import ReconciliationService
from ams.application.reliability import IClock
from ams.domain.identity import CurrentActor, OperationalRole
from ams.domain.reconciliations import InvalidReconciliationTransitionError
from ams.infrastructure.reliability_persistence import SqlAlchemyUnitOfWork
from ams.infrastructure.storage import LocalFileStorage

HR_ID = UUID("00000000-0000-4000-8000-000000009301")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000009302")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000009303")
ASSET_ID = UUID("00000000-0000-4000-8000-000000009304")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000009305")
SESSION_ID = UUID("00000000-0000-4000-8000-000000009306")
ITEM_ID = UUID("00000000-0000-4000-8000-000000009307")
NOW = datetime(2030, 1, 2, 3, 0, tzinfo=UTC)


class FixedClock(IClock):
    """Return one deterministic transition timestamp."""

    def now(self) -> datetime:
        """Return the configured UTC instant."""

        return NOW


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class ReconciliationConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real row/advisory locks through production repositories."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.temporary = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temporary.name)
        await self._cleanup()
        await self._seed_discrepancy()
        self.actor = CurrentActor(HR_ID, frozenset({OperationalRole.HR}))
        self.service = self._service()

    async def asyncTearDown(self) -> None:
        await self._cleanup()
        await self.engine.dispose()
        self.temporary.cleanup()

    async def test_concurrent_resolve_and_close_cannot_bypass_discrepancy(self) -> None:
        """Serialize both commands on the session root and reach a legal final state."""

        resolved, closed = await asyncio.gather(
            self.service.resolve_item(ITEM_ID, "Recounted", self.actor, "resolve-race"),
            self.service.close(SESSION_ID, self.actor, "close-race"),
            return_exceptions=True,
        )
        self.assertFalse(isinstance(resolved, Exception), repr(resolved))
        if isinstance(closed, Exception):
            closed = await self.service.close(SESSION_ID, self.actor, "close-after-resolve")
        self.assertFalse(isinstance(closed, Exception), repr(closed))

        async with self.session_maker() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT sessions.status AS session_status, items.status AS item_status
                        FROM reconciliation_sessions AS sessions
                        JOIN reconciliation_items AS items
                          ON items.reconciliation_id = sessions.id
                        WHERE sessions.id = :session_id AND items.id = :item_id
                        """
                    ),
                    {"session_id": SESSION_ID, "item_id": ITEM_ID},
                )
            ).one()
        self.assertEqual(str(row.session_status), "CLOSED")
        self.assertEqual(str(row.item_status), "RESOLVED")

    async def test_concurrent_open_commands_create_only_one_session(self) -> None:
        """Serialize open commands with the advisory lock and partial unique index."""

        async with self.session_maker.begin() as session:
            await session.execute(
                text("DELETE FROM reconciliation_items WHERE reconciliation_id = :id"),
                {"id": SESSION_ID},
            )
            await session.execute(
                text("DELETE FROM reconciliation_sessions WHERE id = :id"),
                {"id": SESSION_ID},
            )
        first, second = await asyncio.gather(
            self._service().open(date(2030, 1, 3), self.actor, "open-a"),
            self._service().open(date(2030, 1, 4), self.actor, "open-b"),
            return_exceptions=True,
        )
        successes = [value for value in (first, second) if not isinstance(value, Exception)]
        conflicts = [
            value
            for value in (first, second)
            if isinstance(value, InvalidReconciliationTransitionError)
        ]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)

    def _service(self) -> ReconciliationService:
        def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(self.session_maker)

        clock = FixedClock()
        return ReconciliationService(
            IdempotentCommandExecutor(unit_of_work_factory, clock),
            unit_of_work_factory,
            EvidenceStorageService(LocalFileStorage(self.storage_root)),
            clock,
        )

    async def _seed_discrepancy(self) -> None:
        async with self.session_maker.begin() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:hr, 'recon.race.hr', 'recon.race.hr@example.test', 'HR', 'hash'),
                        (:custodian, 'recon.race.c', 'recon.race.c@example.test', 'C', 'hash')
                    """
                ),
                {"hr": HR_ID, "custodian": CUSTODIAN_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'RECON_RACE', 'Reconciliation race')
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
                    ) VALUES (:id, :category, 'Camera', 'PURCHASE', 2, 2, :hr)
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
                    ) VALUES (
                        :id, :asset, :custodian, :hr, 'ACTIVE', 2, 'GOOD',
                        :now, :now, :custodian
                    )
                    """
                ),
                {
                    "id": CUSTODY_ID,
                    "asset": ASSET_ID,
                    "custodian": CUSTODIAN_ID,
                    "hr": HR_ID,
                    "now": NOW,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO reconciliation_sessions (
                        id, reconciliation_date, opened_by_user_id, opened_at, updated_at
                    ) VALUES (:id, '2030-01-03', :hr, :now, :now)
                    """
                ),
                {"id": SESSION_ID, "hr": HR_ID, "now": NOW},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO reconciliation_items (
                        id, reconciliation_id, custody_id, asset_id, custodian_user_id,
                        asset_name_snapshot, expected_quantity, expected_condition,
                        snapshot_at, status, actual_quantity, actual_condition,
                        discrepancy_reason, confirmed_by_user_id, confirmed_at,
                        created_at, updated_at
                    ) VALUES (
                        :id, :session, :custody, :asset, :custodian,
                        'Camera', 2, 'GOOD', :now, 'DISCREPANCY', 1, 'GOOD',
                        'Missing', :custodian, :now, :now, :now
                    )
                    """
                ),
                {
                    "id": ITEM_ID,
                    "session": SESSION_ID,
                    "custody": CUSTODY_ID,
                    "asset": ASSET_ID,
                    "custodian": CUSTODIAN_ID,
                    "now": NOW,
                },
            )

    async def _cleanup(self) -> None:
        async with self.session_maker.begin() as session:
            for statement in (
                """
                DELETE FROM reconciliation_item_evidence
                WHERE uploaded_by_user_id IN (:hr, :custodian)
                """,
                """
                DELETE FROM reconciliation_items
                WHERE reconciliation_id IN (
                    SELECT id FROM reconciliation_sessions WHERE opened_by_user_id = :hr
                )
                """,
                "DELETE FROM reconciliation_sessions WHERE opened_by_user_id = :hr",
                "DELETE FROM idempotency_records WHERE actor_id = :hr",
                "DELETE FROM outbox_events WHERE aggregate_id = :session",
                "DELETE FROM custodies WHERE id = :custody",
                "DELETE FROM assets WHERE id = :asset",
                "DELETE FROM asset_categories WHERE id = :category",
                "DELETE FROM users WHERE id IN (:hr, :custodian)",
            ):
                await session.execute(
                    text(statement),
                    {
                        "hr": HR_ID,
                        "custodian": CUSTODIAN_ID,
                        "session": SESSION_ID,
                        "custody": CUSTODY_ID,
                        "asset": ASSET_ID,
                        "category": CATEGORY_ID,
                    },
                )
