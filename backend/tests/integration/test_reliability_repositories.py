"""Integration tests for F03.02 transactional reliability repositories."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.reliability import (
    IdempotencyRecord,
    IdempotencyStatus,
    Notification,
    OutboxEvent,
    ReliabilityPersistenceError,
)
from ams.domain.assets import Asset, Custody, CustodyAllocation
from ams.infrastructure.reliability_persistence import SqlAlchemyUnitOfWork

HR_ID = UUID("00000000-0000-4000-8000-000000000601")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000000602")
MISSING_USER_ID = UUID("00000000-0000-4000-8000-000000000603")
TEAM_ID = UUID("00000000-0000-4000-8000-000000000604")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000000605")
ASSET_ID = UUID("00000000-0000-4000-8000-000000000606")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000000607")
EVENT_ID = UUID("00000000-0000-4000-8000-000000000608")
NOTIFICATION_ID = UUID("00000000-0000-4000-8000-000000000609")
IDEMPOTENCY_ID = UUID("00000000-0000-4000-8000-000000000610")
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class ReliabilityRepositoryTests(unittest.IsolatedAsyncioTestCase):
    """Prove aggregate and reliability records share one transaction."""

    async def asyncSetUp(self) -> None:
        """Create the session factory and committed reference rows."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:hr, 'reliable.hr', 'reliable.hr@example.test', 'Reliable HR', 'hash'),
                        (:custodian, 'reliable.custodian', 'reliable.custodian@example.test',
                         'Reliable Custodian', 'hash')
                    """
                ),
                {"hr": HR_ID, "custodian": CUSTODIAN_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO teams (id, name, code)
                    VALUES (:id, 'Reliable Team', 'RELIABLE_TEAM')
                    """
                ),
                {"id": TEAM_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'RELIABLE_CATEGORY', 'Reliable Category')
                    """
                ),
                {"id": CATEGORY_ID},
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        """Remove committed fixtures and dispose the engine."""

        await self._cleanup()
        await self.engine.dispose()

    async def test_business_outbox_notification_and_idempotency_commit_together(self) -> None:
        """A successful transaction persists every business and reliability row."""

        record = self._idempotency()
        record.succeed(
            response_status_code=201,
            response_body={"assetId": str(ASSET_ID)},
            completed_at=NOW + timedelta(seconds=1),
        )
        async with SqlAlchemyUnitOfWork(self.session_maker) as unit_of_work:
            await unit_of_work.assets.add(self._asset())
            await unit_of_work.custodies.add(self._custody(CUSTODIAN_ID))
            await unit_of_work.outbox.add(self._event())
            await unit_of_work.notifications.add(self._notification())
            await unit_of_work.idempotency.add(record)
            await unit_of_work.commit()

        self.assertEqual(await self._counts(), (1, 1, 1, 1, 1))
        async with SqlAlchemyUnitOfWork(self.session_maker) as unit_of_work:
            loaded = await unit_of_work.idempotency.get_for_update(
                HR_ID, "CREATE_ASSET", "reliable-request"
            )
        self.assertEqual(loaded, record)

    async def test_business_failure_rolls_back_outbox_and_idempotency(self) -> None:
        """A failed Custody FK prevents publication and command persistence."""

        with self.assertRaises(ReliabilityPersistenceError):
            async with SqlAlchemyUnitOfWork(self.session_maker) as unit_of_work:
                await unit_of_work.assets.add(self._asset())
                await unit_of_work.custodies.add(self._custody(MISSING_USER_ID))
                await unit_of_work.outbox.add(self._event())
                await unit_of_work.idempotency.add(self._idempotency())
                await unit_of_work.commit()

        self.assertEqual(await self._counts(), (0, 0, 0, 0, 0))

    async def test_context_without_commit_suppresses_every_staged_record(self) -> None:
        """Repositories never commit business changes or publication themselves."""

        async with SqlAlchemyUnitOfWork(self.session_maker) as unit_of_work:
            await unit_of_work.assets.add(self._asset())
            await unit_of_work.custodies.add(self._custody(CUSTODIAN_ID))
            await unit_of_work.outbox.add(self._event())
            await unit_of_work.idempotency.add(self._idempotency())

        self.assertEqual(await self._counts(), (0, 0, 0, 0, 0))

    async def test_idempotency_save_stages_replay_response_until_commit(self) -> None:
        """An in-progress command becomes replayable through explicit save and commit."""

        async with SqlAlchemyUnitOfWork(self.session_maker) as unit_of_work:
            await unit_of_work.idempotency.add(self._idempotency())
            await unit_of_work.commit()

        async with SqlAlchemyUnitOfWork(self.session_maker) as unit_of_work:
            record = await unit_of_work.idempotency.get_for_update(
                HR_ID, "CREATE_ASSET", "reliable-request"
            )
            assert record is not None
            record.succeed(
                response_status_code=201,
                response_body={"assetId": str(ASSET_ID)},
                completed_at=NOW + timedelta(seconds=1),
            )
            await unit_of_work.idempotency.save(record)

        async with SqlAlchemyUnitOfWork(self.session_maker) as unit_of_work:
            unchanged = await unit_of_work.idempotency.get_for_update(
                HR_ID, "CREATE_ASSET", "reliable-request"
            )
        assert unchanged is not None
        self.assertEqual(unchanged.status, IdempotencyStatus.IN_PROGRESS)

        async with SqlAlchemyUnitOfWork(self.session_maker) as unit_of_work:
            record = await unit_of_work.idempotency.get_for_update(
                HR_ID, "CREATE_ASSET", "reliable-request"
            )
            assert record is not None
            record.succeed(
                response_status_code=201,
                response_body={"assetId": str(ASSET_ID)},
                completed_at=NOW + timedelta(seconds=1),
            )
            await unit_of_work.idempotency.save(record)
            await unit_of_work.commit()

        async with SqlAlchemyUnitOfWork(self.session_maker) as unit_of_work:
            changed = await unit_of_work.idempotency.get_for_update(
                HR_ID, "CREATE_ASSET", "reliable-request"
            )
        assert changed is not None
        self.assertEqual(changed.status, IdempotencyStatus.SUCCEEDED)

    async def _counts(self) -> tuple[int, int, int, int, int]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM assets WHERE id = :asset_id),
                        (SELECT COUNT(*) FROM custodies WHERE id = :custody_id),
                        (SELECT COUNT(*) FROM outbox_events WHERE id = :event_id),
                        (SELECT COUNT(*) FROM notifications WHERE id = :notification_id),
                        (SELECT COUNT(*) FROM idempotency_records WHERE id = :idempotency_id)
                    """
                ),
                {
                    "asset_id": ASSET_ID,
                    "custody_id": CUSTODY_ID,
                    "event_id": EVENT_ID,
                    "notification_id": NOTIFICATION_ID,
                    "idempotency_id": IDEMPOTENCY_ID,
                },
            )
            row = result.one()
            return (
                int(row[0]),
                int(row[1]),
                int(row[2]),
                int(row[3]),
                int(row[4]),
            )

    async def _cleanup(self) -> None:
        async with self.session_maker() as session:
            for statement, parameters in (
                (
                    "DELETE FROM notifications WHERE id = :id",
                    {"id": NOTIFICATION_ID},
                ),
                ("DELETE FROM outbox_events WHERE id = :id", {"id": EVENT_ID}),
                (
                    "DELETE FROM idempotency_records WHERE id = :id",
                    {"id": IDEMPOTENCY_ID},
                ),
                ("DELETE FROM custodies WHERE id = :id", {"id": CUSTODY_ID}),
                ("DELETE FROM assets WHERE id = :id", {"id": ASSET_ID}),
                (
                    "DELETE FROM asset_categories WHERE id = :id",
                    {"id": CATEGORY_ID},
                ),
                ("DELETE FROM teams WHERE id = :id", {"id": TEAM_ID}),
                (
                    "DELETE FROM users WHERE id IN (:hr, :custodian)",
                    {"hr": HR_ID, "custodian": CUSTODIAN_ID},
                ),
            ):
                await session.execute(text(statement), parameters)
            await session.commit()

    def _asset(self) -> Asset:
        return Asset.create(
            asset_id=ASSET_ID,
            category_id=CATEGORY_ID,
            responsible_team_id=TEAM_ID,
            name="Reliable Camera",
            source="DONATION",
            total_quantity=2,
            allocations=(CustodyAllocation(CUSTODIAN_ID, 2),),
            created_by_user_id=HR_ID,
            created_at=NOW,
        )

    def _custody(self, custodian_id: UUID) -> Custody:
        return Custody.create_pending(
            custody_id=CUSTODY_ID,
            asset_id=ASSET_ID,
            custodian_user_id=custodian_id,
            assigned_by_user_id=HR_ID,
            quantity=2,
            assigned_at=NOW,
        )

    def _event(self) -> OutboxEvent:
        return OutboxEvent(
            id=EVENT_ID,
            event_type="CUSTODY_ASSIGNED",
            aggregate_type="CUSTODY",
            aggregate_id=CUSTODY_ID,
            payload={"custodyId": str(CUSTODY_ID)},
            scheduled_at=NOW,
            created_at=NOW,
        )

    def _notification(self) -> Notification:
        return Notification(
            id=NOTIFICATION_ID,
            outbox_event_id=EVENT_ID,
            recipient_id=CUSTODIAN_ID,
            recipient_address="reliable.custodian@example.test",
            created_at=NOW,
        )

    def _idempotency(self) -> IdempotencyRecord:
        return IdempotencyRecord(
            id=IDEMPOTENCY_ID,
            actor_id=HR_ID,
            command_name="CREATE_ASSET",
            idempotency_key="reliable-request",
            request_fingerprint="d" * 64,
            status=IdempotencyStatus.IN_PROGRESS,
            expires_at=NOW + timedelta(days=1),
            created_at=NOW,
        )
