"""PostgreSQL integration tests for the F03.04 leased email outbox worker."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.notifications import (
    AlwaysEligibleOutboxEventPolicy,
    EmailMessage,
    IEmailSender,
    OutboxWorker,
    PayloadEmailRenderer,
)
from ams.application.reliability import IClock, OutboxEvent, OutboxLeaseLostError
from ams.infrastructure.reliability_persistence import SqlAlchemyUnitOfWork

USER_ID = UUID("00000000-0000-4000-8000-000000001101")
EVENT_ID = UUID("00000000-0000-4000-8000-000000001102")
AGGREGATE_ID = UUID("00000000-0000-4000-8000-000000001103")
NOW = datetime(2026, 10, 4, 15, 0, tzinfo=UTC)


class MutableClock(IClock):
    """Provide deterministic, advanceable UTC worker time."""

    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        """Return the current test instant."""

        return self._value

    def advance(self, duration: timedelta) -> None:
        """Move test time forward by a positive duration."""

        self._value += duration


class ScriptedEmailSender(IEmailSender):
    """Record provider calls and fail a configured number of initial attempts."""

    def __init__(self, failures: int = 0) -> None:
        self._failures = failures
        self.messages: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> str:
        """Return a stable provider ID after any scripted failures."""

        self.messages.append(message)
        if len(self.messages) <= self._failures:
            raise RuntimeError("private provider failure")
        return f"provider-{len(self.messages)}"


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class OutboxWorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Prove PostgreSQL claim exclusion, audit deduplication, and durable retry."""

    async def asyncSetUp(self) -> None:
        """Create the session factory and recipient fixture."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES (:id, 'outbox.worker', 'worker@example.test',
                            'Outbox Worker', 'hash')
                    """
                ),
                {"id": USER_ID},
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        """Remove committed worker fixtures and dispose the engine."""

        await self._cleanup()
        await self.engine.dispose()

    async def test_two_workers_create_one_delivery_audit(self) -> None:
        """Concurrent workers claim once, send once, and persist one logical audit."""

        await self._insert_event()
        clock = MutableClock(NOW)
        sender = ScriptedEmailSender()
        first = self._worker("worker-1", sender, clock)
        second = self._worker("worker-2", sender, clock)

        claimed = await asyncio.gather(first.run_once(), second.run_once())

        self.assertEqual(sorted(claimed), [0, 1])
        self.assertEqual(len(sender.messages), 1)
        row = await self._state()
        self.assertEqual(
            row,
            ("DELIVERED", 1, None, 1, "DELIVERED", 1, "provider-1"),
        )

    async def test_provider_retry_preserves_one_logical_notification(self) -> None:
        """A failed provider attempt retries later against the same audit row."""

        await self._insert_event()
        clock = MutableClock(NOW)
        sender = ScriptedEmailSender(failures=1)
        worker = self._worker("retry-worker", sender, clock)

        self.assertEqual(await worker.run_once(), 1)
        failed = await self._state()
        self.assertEqual(failed[0:2], ("PENDING", 1))
        self.assertEqual(failed[3:6], (1, "FAILED", 1))

        clock.advance(timedelta(seconds=30))
        self.assertEqual(await worker.run_once(), 1)

        delivered = await self._state()
        self.assertEqual(
            delivered,
            ("DELIVERED", 2, None, 1, "DELIVERED", 2, "provider-2"),
        )
        self.assertEqual(len(sender.messages), 2)

    async def test_skip_locked_claim_and_lease_ownership(self) -> None:
        """A locked claim is skipped and only its live owner may complete it."""

        await self._insert_event()
        lease_end = NOW + timedelta(minutes=2)
        async with SqlAlchemyUnitOfWork(self.session_maker) as first:
            claimed = await first.outbox.claim_due(
                now=NOW,
                worker_id="lease-owner",
                lease_expires_at=lease_end,
                limit=1,
            )
            self.assertEqual(len(claimed), 1)
            async with SqlAlchemyUnitOfWork(self.session_maker) as second:
                skipped = await second.outbox.claim_due(
                    now=NOW,
                    worker_id="other-worker",
                    lease_expires_at=lease_end,
                    limit=1,
                )
                await second.commit()
            self.assertEqual(skipped, ())
            await first.commit()

        with self.assertRaises(OutboxLeaseLostError):
            async with SqlAlchemyUnitOfWork(self.session_maker) as stale:
                await stale.outbox.mark_delivered(
                    EVENT_ID,
                    worker_id="other-worker",
                    completed_at=NOW + timedelta(seconds=1),
                )

        async with SqlAlchemyUnitOfWork(self.session_maker) as owner:
            await owner.outbox.mark_delivered(
                EVENT_ID,
                worker_id="lease-owner",
                completed_at=NOW + timedelta(seconds=1),
            )
            await owner.commit()

    def _worker(
        self,
        worker_id: str,
        sender: ScriptedEmailSender,
        clock: MutableClock,
    ) -> OutboxWorker:
        return OutboxWorker(
            lambda: SqlAlchemyUnitOfWork(self.session_maker),
            sender,
            PayloadEmailRenderer(),
            clock,
            worker_id=worker_id,
            eligibility_policy=AlwaysEligibleOutboxEventPolicy(),
        )

    async def _insert_event(self) -> None:
        event = OutboxEvent(
            id=EVENT_ID,
            event_type="CUSTODY_PENDING_REMINDER",
            aggregate_type="CUSTODY",
            aggregate_id=AGGREGATE_ID,
            payload={
                "emails": [
                    {
                        "recipientId": str(USER_ID),
                        "address": "worker@example.test",
                        "subject": "Custody confirmation pending",
                        "body": "Please confirm custody receipt.",
                    }
                ]
            },
            scheduled_at=NOW,
            created_at=NOW,
        )
        async with SqlAlchemyUnitOfWork(self.session_maker) as unit_of_work:
            await unit_of_work.outbox.add(event)
            await unit_of_work.commit()

    async def _state(self) -> tuple[str, int, str | None, int, str, int, str | None]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT event.status, event.attempt_count, event.lease_owner,
                           COUNT(notification.id), MIN(notification.status),
                           MIN(notification.attempt_count),
                           MIN(notification.provider_message_id)
                    FROM outbox_events AS event
                    LEFT JOIN notifications AS notification
                      ON notification.outbox_event_id = event.id
                    WHERE event.id = :event_id
                    GROUP BY event.id
                    """
                ),
                {"event_id": EVENT_ID},
            )
            row = result.one()
            return (
                str(row[0]),
                int(row[1]),
                None if row[2] is None else str(row[2]),
                int(row[3]),
                str(row[4]),
                int(row[5]),
                None if row[6] is None else str(row[6]),
            )

    async def _cleanup(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text("DELETE FROM notifications WHERE outbox_event_id = :id"),
                {"id": EVENT_ID},
            )
            await session.execute(
                text("DELETE FROM outbox_events WHERE id = :id"),
                {"id": EVENT_ID},
            )
            await session.execute(
                text("DELETE FROM users WHERE id = :id"),
                {"id": USER_ID},
            )
            await session.commit()
