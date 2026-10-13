"""Time-controlled PostgreSQL tests for the 24-hour pending-Custody reminder."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ams.application.notifications import (
    EmailMessage,
    IEmailSender,
    OutboxWorker,
    PayloadEmailRenderer,
)
from ams.application.reliability import IClock, OutboxEvent
from ams.infrastructure.reliability_persistence import SqlAlchemyUnitOfWork

HR_ID = UUID("00000000-0000-4000-8000-000000002201")
CUSTODIAN_ID = UUID("00000000-0000-4000-8000-000000002202")
CATEGORY_ID = UUID("00000000-0000-4000-8000-000000002203")
ASSET_ID = UUID("00000000-0000-4000-8000-000000002204")
CUSTODY_ID = UUID("00000000-0000-4000-8000-000000002205")
EVENT_ID = UUID("00000000-0000-4000-8000-000000002206")
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000002207")
ASSIGNED_AT = datetime(2026, 10, 4, 2, 0, tzinfo=UTC)
THRESHOLD = ASSIGNED_AT + timedelta(hours=24)


class MutableClock(IClock):
    """Provide deterministic worker time around the reminder threshold."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        """Return the current controlled UTC instant."""

        return self.value


class RecordingEmailSender(IEmailSender):
    """Capture reminder emails without contacting an external provider."""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> str:
        """Record one message and return a deterministic provider ID."""

        self.messages.append(message)
        return f"reminder-{len(self.messages)}"


@unittest.skipUnless(os.getenv("AMS_DATABASE_URL"), "AMS_DATABASE_URL is required")
class PendingCustodyReminderTests(unittest.IsolatedAsyncioTestCase):
    """Verify timing, recipient count, single delivery, and confirmation suppression."""

    async def asyncSetUp(self) -> None:
        """Insert one pending Custody and its scheduled durable reminder."""

        self.engine = create_async_engine(os.environ["AMS_DATABASE_URL"], pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        await self._cleanup()
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, username, email, display_name, password_hash)
                    VALUES
                        (:hr, 'reminder.hr', 'reminder.hr@example.test',
                         'Reminder HR', 'hash'),
                        (:custodian, 'reminder.owner', 'reminder.owner@example.test',
                         'Reminder Owner', 'hash')
                    """
                ),
                {"hr": HR_ID, "custodian": CUSTODIAN_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO asset_categories (id, code, name)
                    VALUES (:id, 'REMINDER_CATEGORY', 'Reminder Category')
                    """
                ),
                {"id": CATEGORY_ID},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO assets (
                        id, category_id, name, source, total_quantity,
                        available_quantity, created_by_user_id, created_at, updated_at
                    ) VALUES (
                        :id, :category, 'Reminder Camera', 'DONATION', 1, 0, :hr,
                        :assigned_at, :assigned_at
                    )
                    """
                ),
                {
                    "id": ASSET_ID,
                    "category": CATEGORY_ID,
                    "hr": HR_ID,
                    "assigned_at": ASSIGNED_AT,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO custodies (
                        id, asset_id, custodian_user_id, assigned_by_user_id,
                        status, on_hand_quantity, condition, assigned_at,
                        created_at, updated_at
                    ) VALUES (
                        :id, :asset, :custodian, :hr, 'PENDING', 1, 'GOOD',
                        :assigned_at, :assigned_at, :assigned_at
                    )
                    """
                ),
                {
                    "id": CUSTODY_ID,
                    "asset": ASSET_ID,
                    "custodian": CUSTODIAN_ID,
                    "hr": HR_ID,
                    "assigned_at": ASSIGNED_AT,
                },
            )
            await session.commit()
        await self._insert_reminder()

    async def asyncTearDown(self) -> None:
        """Delete reminder fixtures and release the PostgreSQL engine."""

        await self._cleanup()
        await self.engine.dispose()

    async def test_no_early_delivery_then_exactly_one_delivery_at_threshold(self) -> None:
        """The reminder waits 24 hours, then emails HR and assignee only once."""

        clock = MutableClock(THRESHOLD - timedelta(seconds=1))
        sender = RecordingEmailSender()
        worker = self._worker(clock, sender)

        self.assertEqual(await worker.run_once(), 0)
        self.assertEqual(sender.messages, [])

        clock.value = THRESHOLD
        self.assertEqual(await worker.run_once(), 1)
        self.assertEqual(await worker.run_once(), 0)

        self.assertEqual(len(sender.messages), 2)
        self.assertEqual(
            {message.recipient_id for message in sender.messages},
            {HR_ID, CUSTODIAN_ID},
        )
        self.assertEqual(await self._state(), ("DELIVERED", 1, 2))

    async def test_confirmation_before_threshold_suppresses_all_delivery(self) -> None:
        """An active Custody makes its due reminder terminal without email audits."""

        await self._confirm_custody()
        sender = RecordingEmailSender()
        worker = self._worker(MutableClock(THRESHOLD), sender)

        self.assertEqual(await worker.run_once(), 1)

        self.assertEqual(sender.messages, [])
        self.assertEqual(await self._state(), ("DELIVERED", 1, 0))

    def _worker(self, clock: MutableClock, sender: RecordingEmailSender) -> OutboxWorker:
        return OutboxWorker(
            lambda: SqlAlchemyUnitOfWork(self.session_maker),
            sender,
            PayloadEmailRenderer(),
            clock,
            worker_id="custody-reminder-worker",
        )

    async def _insert_reminder(self) -> None:
        event = OutboxEvent(
            id=EVENT_ID,
            event_type="CUSTODY_PENDING_REMINDER",
            aggregate_type="CUSTODY",
            aggregate_id=CUSTODY_ID,
            payload={
                "assetId": str(ASSET_ID),
                "custodyId": str(CUSTODY_ID),
                "emails": [
                    {
                        "recipientId": str(HR_ID),
                        "address": "reminder.hr@example.test",
                        "subject": "Asset custody confirmation pending",
                        "body": "Custody has been pending for 24 hours.",
                    },
                    {
                        "recipientId": str(CUSTODIAN_ID),
                        "address": "reminder.owner@example.test",
                        "subject": "Asset custody confirmation pending",
                        "body": "Custody has been pending for 24 hours.",
                    },
                ],
            },
            scheduled_at=THRESHOLD,
            created_at=ASSIGNED_AT,
        )
        async with SqlAlchemyUnitOfWork(self.session_maker) as unit_of_work:
            await unit_of_work.outbox.add(event)
            await unit_of_work.commit()

    async def _confirm_custody(self) -> None:
        async with self.session_maker() as session:
            await session.execute(
                text(
                    """
                    UPDATE custodies
                    SET status = 'ACTIVE',
                        confirmed_at = :confirmed_at,
                        confirmed_by_user_id = :custodian,
                        updated_at = :confirmed_at
                    WHERE id = :id
                    """
                ),
                {
                    "confirmed_at": ASSIGNED_AT + timedelta(hours=1),
                    "custodian": CUSTODIAN_ID,
                    "id": CUSTODY_ID,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO custody_evidence (
                        id, custody_id, uploaded_by_user_id, evidence_type, object_key,
                        content_type, content_sha256, size_bytes
                    ) VALUES (
                        :id, :custody, :custodian, 'ASSIGNMENT_CONFIRMATION',
                        'objects/reminder-proof', 'image/png', :sha, 16
                    )
                    """
                ),
                {
                    "id": EVIDENCE_ID,
                    "custody": CUSTODY_ID,
                    "custodian": CUSTODIAN_ID,
                    "sha": "b" * 64,
                },
            )
            await session.commit()

    async def _state(self) -> tuple[str, int, int]:
        async with self.session_maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT event.status, event.attempt_count, COUNT(notification.id)
                    FROM outbox_events AS event
                    LEFT JOIN notifications AS notification
                      ON notification.outbox_event_id = event.id
                    WHERE event.id = :id
                    GROUP BY event.id
                    """
                ),
                {"id": EVENT_ID},
            )
            row = result.one()
            return str(row[0]), int(row[1]), int(row[2])

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
                text("DELETE FROM custody_evidence WHERE id = :id"),
                {"id": EVIDENCE_ID},
            )
            await session.execute(
                text("DELETE FROM custodies WHERE id = :id"),
                {"id": CUSTODY_ID},
            )
            await session.execute(
                text("DELETE FROM assets WHERE id = :id"),
                {"id": ASSET_ID},
            )
            await session.execute(
                text("DELETE FROM asset_categories WHERE id = :id"),
                {"id": CATEGORY_ID},
            )
            await session.execute(
                text("DELETE FROM users WHERE id IN (:hr, :custodian)"),
                {"hr": HR_ID, "custodian": CUSTODIAN_ID},
            )
            await session.commit()
