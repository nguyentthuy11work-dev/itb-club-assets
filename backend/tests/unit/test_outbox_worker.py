"""Unit tests for F03.04 email rendering, delivery, retry, and deduplication."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import cast
from uuid import UUID

from ams.application.notifications import (
    AlwaysEligibleOutboxEventPolicy,
    CurrentStateReminderEligibilityPolicy,
    EmailDeliveryError,
    EmailMessage,
    IEmailSender,
    IWorkerMetrics,
    OutboxWorker,
    PayloadEmailRenderer,
)
from ams.application.reliability import (
    ClaimedOutboxEvent,
    IClock,
    IIdempotencyRepository,
    INotificationRepository,
    IOutboxRepository,
    Notification,
    NotificationStatus,
    OutboxEvent,
    OutboxLeaseLostError,
)
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import AssetCondition, Custody
from ams.domain.repositories import (
    IAssetRepository,
    ICustodyEvidenceRepository,
    ICustodyRepository,
)
from ams.domain.tickets import (
    BorrowTicket,
    ITicketRepository,
    TicketItem,
    TicketMethod,
    TicketStatus,
)

EVENT_ID = UUID("00000000-0000-4000-8000-000000001001")
RECIPIENT_ID = UUID("00000000-0000-4000-8000-000000001002")
NOTIFICATION_ID = UUID("00000000-0000-4000-8000-000000001003")
NOW = datetime(2026, 10, 4, 14, 0, tzinfo=UTC)
ASSET_ID = UUID("00000000-0000-4000-8000-000000001004")
ASSIGNER_ID = UUID("00000000-0000-4000-8000-000000001005")


class FixedClock(IClock):
    """Return one deterministic worker instant."""

    def now(self) -> datetime:
        """Return the configured UTC instant."""

        return NOW


class FakeEmailSender(IEmailSender):
    """Record provider calls and optionally reject delivery."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> str:
        """Record a call and return an ID or simulate provider failure."""

        self.messages.append(message)
        if self.fail:
            raise RuntimeError("provider secret detail")
        return "provider-message-1"


class FakeMetrics(IWorkerMetrics):
    """Collect worker metric names."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def increment(self, metric_name: str) -> None:
        """Record one metric increment."""

        self.names.append(metric_name)


class FakeOutboxRepository(IOutboxRepository):
    """Lease a configured event once and record terminal decisions."""

    def __init__(self, event: ClaimedOutboxEvent, *, lose_lease: bool = False) -> None:
        self.event = event
        self.lose_lease = lose_lease
        self.claimed = False
        self.delivered: list[UUID] = []
        self.retries: list[tuple[UUID, datetime, str, bool]] = []

    async def add(self, event: OutboxEvent) -> None:
        """Reject unused event creation in this worker fake."""

        raise AssertionError("not used")

    async def claim_due(
        self,
        *,
        now: datetime,
        worker_id: str,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        """Return the event to only the first claim."""

        if self.claimed:
            return ()
        self.claimed = True
        return (self.event,)

    async def mark_delivered(
        self, event_id: UUID, *, worker_id: str, completed_at: datetime
    ) -> None:
        """Record successful terminal state."""

        self.delivered.append(event_id)

    async def mark_retry(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        failed_at: datetime,
        next_attempt_at: datetime,
        error: str,
        dead_letter: bool,
    ) -> None:
        """Record retry/dead-letter state."""

        if self.lose_lease:
            raise OutboxLeaseLostError("lease expired")
        self.retries.append((event_id, next_attempt_at, error, dead_letter))


class FakeNotificationRepository(INotificationRepository):
    """Keep logical delivery audits in a shared in-memory dictionary."""

    def __init__(self) -> None:
        self.records: dict[tuple[UUID, UUID], Notification] = {}

    async def add(self, notification: Notification) -> None:
        """Add one logical event-recipient delivery row."""

        key = (notification.outbox_event_id, notification.recipient_id)
        if key in self.records:
            raise AssertionError("duplicate logical notification")
        self.records[key] = notification

    async def get_for_update(
        self, outbox_event_id: UUID, recipient_id: UUID
    ) -> Notification | None:
        """Return the current logical delivery row."""

        return self.records.get((outbox_event_id, recipient_id))

    async def save(self, notification: Notification) -> None:
        """Retain the mutated delivery row."""

        self.records[(notification.outbox_event_id, notification.recipient_id)] = notification


class DisappearingNotificationRepository(FakeNotificationRepository):
    """Simulate an audit row disappearing after provider acceptance."""

    def __init__(self) -> None:
        super().__init__()
        self._reads = 0

    async def get_for_update(
        self, outbox_event_id: UUID, recipient_id: UUID
    ) -> Notification | None:
        """Hide the row on the second read to exercise defensive failure handling."""

        self._reads += 1
        if self._reads == 2:
            return None
        return await super().get_for_update(outbox_event_id, recipient_id)


class FakeCustodyRepository(ICustodyRepository):
    """Expose one current Custody to scheduled-event eligibility checks."""

    def __init__(self, custody: Custody | None) -> None:
        self.custody = custody

    async def get_by_id(self, custody_id: UUID) -> Custody | None:
        """Return the configured Custody."""

        return self.custody

    async def get_for_update(self, custody_id: UUID) -> Custody | None:
        """Return the configured Custody under the fake transaction lock."""

        return self.custody

    async def add(self, custody: Custody) -> None:
        """Reject unused creation behavior."""

        raise AssertionError("not used")

    async def get_many_for_update(self, custody_ids: tuple[UUID, ...]) -> tuple[Custody, ...]:
        """Reject unused bulk locking in worker policy."""

        raise AssertionError("not used")

    async def get_current_for_update(
        self, asset_id: UUID, custodian_user_id: UUID
    ) -> Custody | None:
        """Reject unused current-Custody locking in worker policy."""

        raise AssertionError("not used")

    async def save(self, custody: Custody) -> None:
        """Reject unused update behavior."""

        raise AssertionError("not used")


class FakeTicketRepository:
    """Expose one Ticket to scheduled-event eligibility checks."""

    def __init__(self, ticket: BorrowTicket | None) -> None:
        self.ticket = ticket

    async def get_for_update(self, ticket_id: UUID) -> BorrowTicket | None:
        del ticket_id
        return self.ticket


class FakeUnitOfWork(IUnitOfWork):
    """Expose shared worker repositories with no-op transaction methods."""

    def __init__(
        self,
        outbox: FakeOutboxRepository,
        notifications: FakeNotificationRepository,
        custodies: FakeCustodyRepository | None = None,
        ticket: BorrowTicket | None = None,
    ) -> None:
        self.outbox = outbox
        self.notifications = notifications
        self.assets = cast(IAssetRepository, object())
        self.custodies: ICustodyRepository = custodies or cast(ICustodyRepository, object())
        self.custody_evidence = cast(ICustodyEvidenceRepository, object())
        self.idempotency = cast(IIdempotencyRepository, object())
        self.tickets = cast(ITicketRepository, FakeTicketRepository(ticket))

    async def __aenter__(self) -> FakeUnitOfWork:
        """Open the fake transaction."""

        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the fake transaction."""

    async def commit(self) -> None:
        """Accept the fake transaction commit."""

    async def rollback(self) -> None:
        """Accept the fake transaction rollback."""


class FakeUnitOfWorkFactory:
    """Create fresh wrappers around shared fake repositories."""

    def __init__(
        self,
        outbox: FakeOutboxRepository,
        notifications: FakeNotificationRepository,
        custodies: FakeCustodyRepository | None = None,
        ticket: BorrowTicket | None = None,
    ) -> None:
        self._outbox = outbox
        self._notifications = notifications
        self._custodies = custodies
        self._ticket = ticket

    def __call__(self) -> IUnitOfWork:
        """Return a fresh fake Unit of Work."""

        return FakeUnitOfWork(self._outbox, self._notifications, self._custodies, self._ticket)


class PayloadEmailRendererTests(unittest.TestCase):
    """Validate the persisted payload-to-email boundary."""

    def test_valid_payload_renders_email(self) -> None:
        """Canonical recipient fields produce one provider-neutral message."""

        messages = PayloadEmailRenderer().render(self._event())
        self.assertEqual(
            messages,
            (
                EmailMessage(
                    RECIPIENT_ID,
                    "recipient@example.test",
                    "Custody assigned",
                    "Please confirm receipt.",
                ),
            ),
        )

    def test_invalid_payloads_are_rejected_safely(self) -> None:
        """Missing, malformed, and blank email payloads never reach a provider."""

        payloads: tuple[dict[str, object], ...] = (
            {},
            {"emails": []},
            {"emails": ["bad"]},
            {"emails": [{"recipientId": "bad"}]},
            {
                "emails": [
                    {
                        "recipientId": str(RECIPIENT_ID),
                        "address": " ",
                        "subject": "subject",
                        "body": "body",
                    }
                ]
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(EmailDeliveryError):
                PayloadEmailRenderer().render(self._event(payload=payload))

    @staticmethod
    def _event(payload: dict[str, object] | None = None) -> ClaimedOutboxEvent:
        return _claimed_event(payload=payload)


class CurrentStateReminderEligibilityPolicyTests(unittest.IsolatedAsyncioTestCase):
    """Verify scheduled reminders consult current Custody state."""

    async def test_non_reminder_and_malformed_reminder_are_classified(self) -> None:
        """Ordinary events pass while malformed Custody reminder identity is suppressed."""

        policy = self._policy(None)
        self.assertTrue(await AlwaysEligibleOutboxEventPolicy().is_eligible(_claimed_event()))
        self.assertTrue(await policy.is_eligible(_claimed_event()))
        self.assertFalse(
            await policy.is_eligible(
                _claimed_event(
                    event_type="CUSTODY_PENDING_REMINDER",
                    aggregate_type="ASSET",
                )
            )
        )

    async def test_only_existing_pending_custody_is_eligible(self) -> None:
        """Missing and active Custodies suppress delivery; pending remains eligible."""

        event = _claimed_event(
            event_type="CUSTODY_PENDING_REMINDER",
            aggregate_type="CUSTODY",
            aggregate_id=EVENT_ID,
        )
        self.assertFalse(await self._policy(None).is_eligible(event))
        pending = _pending_custody()
        self.assertTrue(await self._policy(pending).is_eligible(event))
        pending.confirm(
            actor_id=RECIPIENT_ID,
            evidence_ids=(NOTIFICATION_ID,),
            confirmed_at=NOW,
        )
        self.assertFalse(await self._policy(pending).is_eligible(event))

    async def test_ticket_reminders_require_active_temporary_ticket(self) -> None:
        """Malformed, missing, transfer, and terminal Ticket reminders are suppressed."""

        event = _claimed_event(
            event_type="TICKET_DUE_1_DAY",
            aggregate_type="BORROW_TICKET",
            aggregate_id=EVENT_ID,
        )
        self.assertFalse(await self._ticket_policy(None).is_eligible(event))
        self.assertFalse(
            await self._ticket_policy(_ticket(TicketStatus.PENDING)).is_eligible(
                _claimed_event(
                    event_type="TICKET_DUE_1_DAY",
                    aggregate_type="TICKET",
                    aggregate_id=EVENT_ID,
                )
            )
        )
        self.assertTrue(
            await self._ticket_policy(_ticket(TicketStatus.BORROWING)).is_eligible(event)
        )
        self.assertFalse(
            await self._ticket_policy(_ticket(TicketStatus.CLOSED)).is_eligible(event)
        )
        self.assertFalse(
            await self._ticket_policy(
                _ticket(TicketStatus.PENDING, method=TicketMethod.CUSTODY_TRANSFER)
            ).is_eligible(event)
        )

    @staticmethod
    def _policy(custody: Custody | None) -> CurrentStateReminderEligibilityPolicy:
        outbox = FakeOutboxRepository(_claimed_event())
        factory = FakeUnitOfWorkFactory(
            outbox,
            FakeNotificationRepository(),
            FakeCustodyRepository(custody),
        )
        return CurrentStateReminderEligibilityPolicy(factory)

    @staticmethod
    def _ticket_policy(
        ticket: BorrowTicket | None,
    ) -> CurrentStateReminderEligibilityPolicy:
        factory = FakeUnitOfWorkFactory(
            FakeOutboxRepository(_claimed_event()),
            FakeNotificationRepository(),
            ticket=ticket,
        )
        return CurrentStateReminderEligibilityPolicy(factory)


class OutboxWorkerTests(unittest.IsolatedAsyncioTestCase):
    """Exercise success, retry, dead-letter, and delivery deduplication."""

    async def test_success_delivers_once_and_marks_event(self) -> None:
        """A claimed event creates one audit, sends once, and becomes delivered."""

        worker, outbox, notifications, sender, metrics = self._worker()
        self.assertEqual(await worker.run_once(), 1)
        self.assertEqual(await worker.run_once(), 0)

        record = notifications.records[(EVENT_ID, RECIPIENT_ID)]
        self.assertEqual(record.status, NotificationStatus.DELIVERED)
        self.assertEqual(len(sender.messages), 1)
        self.assertEqual(outbox.delivered, [EVENT_ID])
        self.assertEqual(metrics.names, ["outbox_claimed", "outbox_delivered"])

    async def test_provider_failure_records_audit_and_exponential_retry(self) -> None:
        """Provider failure updates one audit and schedules a sanitized retry."""

        worker, outbox, notifications, _, metrics = self._worker(fail_sender=True)
        self.assertEqual(await worker.run_once(), 1)

        record = notifications.records[(EVENT_ID, RECIPIENT_ID)]
        self.assertEqual(record.status, NotificationStatus.FAILED)
        self.assertEqual(record.last_error, "Email delivery failed.")
        self.assertEqual(
            outbox.retries,
            [
                (
                    EVENT_ID,
                    NOW + timedelta(seconds=30),
                    "Outbox event processing failed.",
                    False,
                )
            ],
        )
        self.assertEqual(metrics.names, ["outbox_claimed", "outbox_retry"])

    async def test_final_attempt_moves_event_to_dead_letter(self) -> None:
        """The configured final failed attempt cannot be retried forever."""

        worker, outbox, _, _, metrics = self._worker(
            fail_sender=True,
            event=_claimed_event(attempt_count=3, max_attempts=3),
        )
        await worker.run_once()
        self.assertTrue(outbox.retries[0][3])
        self.assertEqual(metrics.names[-1], "outbox_dead_letter")

    async def test_already_delivered_notification_is_not_sent_twice(self) -> None:
        """Lease recovery skips recipients whose logical audit is already delivered."""

        worker, outbox, notifications, sender, _ = self._worker()
        record = Notification(
            NOTIFICATION_ID,
            EVENT_ID,
            RECIPIENT_ID,
            "recipient@example.test",
            NOW,
        )
        record.mark_delivered(provider_message_id="existing", attempted_at=NOW)
        notifications.records[(EVENT_ID, RECIPIENT_ID)] = record

        await worker.run_once()

        self.assertEqual(sender.messages, [])
        self.assertEqual(outbox.delivered, [EVENT_ID])

    async def test_missing_audit_after_provider_acceptance_schedules_retry(self) -> None:
        """A vanished audit cannot make an event appear successfully delivered."""

        outbox = FakeOutboxRepository(_claimed_event())
        notifications = DisappearingNotificationRepository()
        worker = OutboxWorker(
            FakeUnitOfWorkFactory(outbox, notifications),
            FakeEmailSender(),
            PayloadEmailRenderer(),
            FixedClock(),
            worker_id="worker-1",
            id_factory=lambda: NOTIFICATION_ID,
        )

        await worker.run_once()

        self.assertEqual(len(outbox.retries), 1)

    async def test_lost_lease_is_observable_and_not_rescheduled(self) -> None:
        """A stale worker cannot overwrite state owned by a replacement worker."""

        outbox = FakeOutboxRepository(_claimed_event(payload={}), lose_lease=True)
        metrics = FakeMetrics()
        worker = OutboxWorker(
            FakeUnitOfWorkFactory(outbox, FakeNotificationRepository()),
            FakeEmailSender(),
            PayloadEmailRenderer(),
            FixedClock(),
            worker_id="stale-worker",
            metrics=metrics,
        )

        await worker.run_once()

        self.assertEqual(metrics.names, ["outbox_claimed", "outbox_lease_lost"])

    async def test_ineligible_reminder_completes_without_email_audit(self) -> None:
        """A confirmed Custody turns its claimed reminder into a no-op terminal event."""

        custody = _pending_custody()
        custody.confirm(
            actor_id=RECIPIENT_ID,
            evidence_ids=(NOTIFICATION_ID,),
            confirmed_at=NOW,
        )
        event = _claimed_event(
            event_type="CUSTODY_PENDING_REMINDER",
            aggregate_type="CUSTODY",
            aggregate_id=EVENT_ID,
        )
        outbox = FakeOutboxRepository(event)
        notifications = FakeNotificationRepository()
        sender = FakeEmailSender()
        metrics = FakeMetrics()
        worker = OutboxWorker(
            FakeUnitOfWorkFactory(
                outbox,
                notifications,
                FakeCustodyRepository(custody),
            ),
            sender,
            PayloadEmailRenderer(),
            FixedClock(),
            worker_id="worker-1",
            metrics=metrics,
        )

        self.assertEqual(await worker.run_once(), 1)
        self.assertEqual(sender.messages, [])
        self.assertEqual(notifications.records, {})
        self.assertEqual(outbox.delivered, [EVENT_ID])
        self.assertEqual(metrics.names, ["outbox_claimed", "outbox_ineligible"])

    def test_invalid_worker_configuration_is_rejected(self) -> None:
        """Worker identity, batch, lease, and retry configuration are bounded."""

        outbox = FakeOutboxRepository(_claimed_event())
        notifications = FakeNotificationRepository()
        factory = FakeUnitOfWorkFactory(outbox, notifications)
        with self.assertRaises(ValueError):
            self._configured_worker(factory, worker_id=" ")
        with self.assertRaises(ValueError):
            self._configured_worker(factory, batch_size=0)
        with self.assertRaises(ValueError):
            self._configured_worker(factory, lease_duration=timedelta(0))
        with self.assertRaises(ValueError):
            self._configured_worker(factory, base_retry_delay=timedelta(0))
        with self.assertRaises(ValueError):
            self._configured_worker(factory, worker_id="w" * 161)
        with self.assertRaises(ValueError):
            self._configured_worker(
                factory,
                base_retry_delay=timedelta(minutes=2),
                max_retry_delay=timedelta(minutes=1),
            )

    @staticmethod
    def _configured_worker(
        factory: FakeUnitOfWorkFactory,
        *,
        worker_id: str = "worker",
        batch_size: int = 20,
        lease_duration: timedelta = timedelta(minutes=2),
        base_retry_delay: timedelta = timedelta(seconds=30),
        max_retry_delay: timedelta = timedelta(hours=1),
    ) -> OutboxWorker:
        return OutboxWorker(
            factory,
            FakeEmailSender(),
            PayloadEmailRenderer(),
            FixedClock(),
            worker_id=worker_id,
            batch_size=batch_size,
            lease_duration=lease_duration,
            base_retry_delay=base_retry_delay,
            max_retry_delay=max_retry_delay,
        )

    def _worker(
        self,
        *,
        fail_sender: bool = False,
        event: ClaimedOutboxEvent | None = None,
    ) -> tuple[
        OutboxWorker,
        FakeOutboxRepository,
        FakeNotificationRepository,
        FakeEmailSender,
        FakeMetrics,
    ]:
        outbox = FakeOutboxRepository(event or _claimed_event())
        notifications = FakeNotificationRepository()
        sender = FakeEmailSender(fail=fail_sender)
        metrics = FakeMetrics()
        worker = OutboxWorker(
            FakeUnitOfWorkFactory(outbox, notifications),
            sender,
            PayloadEmailRenderer(),
            FixedClock(),
            worker_id="worker-1",
            metrics=metrics,
            id_factory=lambda: NOTIFICATION_ID,
        )
        return worker, outbox, notifications, sender, metrics


def _claimed_event(
    *,
    payload: dict[str, object] | None = None,
    attempt_count: int = 1,
    max_attempts: int = 3,
    event_type: str = "CUSTODY_ASSIGNED",
    aggregate_type: str = "CUSTODY",
    aggregate_id: UUID = EVENT_ID,
) -> ClaimedOutboxEvent:
    return ClaimedOutboxEvent(
        id=EVENT_ID,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=(
            payload
            if payload is not None
            else {
                "emails": [
                    {
                        "recipientId": str(RECIPIENT_ID),
                        "address": "recipient@example.test",
                        "subject": "Custody assigned",
                        "body": "Please confirm receipt.",
                    }
                ]
            }
        ),
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        created_at=NOW,
    )


def _pending_custody() -> Custody:
    return Custody.create_pending(
        custody_id=EVENT_ID,
        asset_id=ASSET_ID,
        custodian_user_id=RECIPIENT_ID,
        assigned_by_user_id=ASSIGNER_ID,
        quantity=1,
        assigned_at=NOW,
        condition=AssetCondition.GOOD,
    )


def _ticket(
    status: TicketStatus,
    *,
    method: TicketMethod = TicketMethod.TEMPORARY,
) -> BorrowTicket:
    return BorrowTicket(
        id=EVENT_ID,
        borrower_user_id=RECIPIENT_ID,
        source_custodian_user_id=ASSIGNER_ID,
        method=method,
        status=status,
        due_date=date(2026, 10, 10) if method is TicketMethod.TEMPORARY else None,
        items=(TicketItem(ASSET_ID, NOTIFICATION_ID, 1),),
    )
