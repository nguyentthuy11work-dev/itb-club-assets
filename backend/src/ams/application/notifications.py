"""Leased outbox worker and email delivery application ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import structlog

from ams.application.event_reminders import EVENT_REMINDER_TYPES
from ams.application.reliability import (
    ClaimedOutboxEvent,
    IClock,
    Notification,
    NotificationStatus,
    OutboxLeaseLostError,
    ReliabilityError,
)
from ams.application.ticket_reminders import (
    REMINDER_EVENT_TYPES,
    TERMINAL_TICKET_STATUSES,
)
from ams.application.unit_of_work import IUnitOfWork
from ams.domain.assets import CustodyStatus
from ams.domain.events import ShiftStatus
from ams.domain.tickets import TicketMethod


class EmailDeliveryError(ReliabilityError):
    """Sanitized email-provider or payload delivery failure."""

    code = "EMAIL_DELIVERY_FAILED"


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """Provider-neutral email generated from one business event recipient."""

    recipient_id: UUID
    recipient_address: str
    subject: str
    body: str


class IEmailSender(ABC):
    """Deliver email through the configured external provider."""

    @abstractmethod
    async def send(self, message: EmailMessage) -> str:
        """Return the provider message ID after accepted delivery."""


class IEmailRenderer(ABC):
    """Convert a typed outbox payload into recipient-specific emails."""

    @abstractmethod
    def render(self, event: ClaimedOutboxEvent) -> tuple[EmailMessage, ...]:
        """Return at least one validated email or raise a safe error."""


class IWorkerMetrics(ABC):
    """Record low-cardinality worker outcomes."""

    @abstractmethod
    def increment(self, metric_name: str) -> None:
        """Increment one configured worker counter."""


class IUnitOfWorkFactory(Protocol):
    """Create one fresh generalized Unit of Work."""

    def __call__(self) -> IUnitOfWork:
        """Return a fresh Unit of Work instance."""


class IOutboxEventEligibilityPolicy(ABC):
    """Decide whether a claimed scheduled event still requires side effects."""

    @abstractmethod
    async def is_eligible(self, event: ClaimedOutboxEvent) -> bool:
        """Return whether the claimed event should reach its delivery adapter."""


class AlwaysEligibleOutboxEventPolicy(IOutboxEventEligibilityPolicy):
    """Allow every event for generic worker contract tests or specialized deployments."""

    async def is_eligible(self, event: ClaimedOutboxEvent) -> bool:
        """Return true without consulting business state."""

        return True


class CurrentStateReminderEligibilityPolicy(IOutboxEventEligibilityPolicy):
    """Suppress stale Custody and Borrow Ticket reminders from aggregate state."""

    def __init__(self, unit_of_work_factory: IUnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def is_eligible(self, event: ClaimedOutboxEvent) -> bool:
        """Check current locked aggregate state for reminder events only."""

        if event.event_type == "CUSTODY_PENDING_REMINDER":
            if event.aggregate_type != "CUSTODY":
                return False
            async with self._unit_of_work_factory() as unit_of_work:
                custody = await unit_of_work.custodies.get_for_update(event.aggregate_id)
                eligible = custody is not None and custody.status is CustodyStatus.PENDING
                await unit_of_work.commit()
            return eligible
        if event.event_type in REMINDER_EVENT_TYPES:
            if event.aggregate_type != "BORROW_TICKET":
                return False
            async with self._unit_of_work_factory() as unit_of_work:
                ticket = await unit_of_work.tickets.get_for_update(event.aggregate_id)
                eligible = (
                    ticket is not None
                    and ticket.method is TicketMethod.TEMPORARY
                    and ticket.status not in TERMINAL_TICKET_STATUSES
                )
                await unit_of_work.commit()
            return eligible
        if event.event_type in EVENT_REMINDER_TYPES:
            if event.aggregate_type != "SHIFT":
                return False
            async with self._unit_of_work_factory() as unit_of_work:
                shift = await unit_of_work.event_days.get_shift_for_update(event.aggregate_id)
                eligible = shift is not None and (
                    (
                        event.event_type == "SHIFT_OPEN_REMINDER"
                        and shift.status is ShiftStatus.SCHEDULED
                    )
                    or (
                        event.event_type == "SHIFT_CLOSE_REMINDER"
                        and shift.status is not ShiftStatus.CLOSED
                    )
                )
                await unit_of_work.commit()
            return eligible
        return True


class PayloadEmailRenderer(IEmailRenderer):
    """Render the canonical ``payload.emails[]`` outbox contract."""

    def render(self, event: ClaimedOutboxEvent) -> tuple[EmailMessage, ...]:
        """Validate IDs, addresses, subject, and body from persisted JSON."""

        raw_emails = event.payload.get("emails")
        if not isinstance(raw_emails, list) or not raw_emails:
            raise EmailDeliveryError("Outbox event has no email recipients.")
        messages: list[EmailMessage] = []
        for raw_email in raw_emails:
            if not isinstance(raw_email, Mapping):
                raise EmailDeliveryError("Outbox email payload is invalid.")
            try:
                recipient_id = UUID(str(raw_email["recipientId"]))
                address = str(raw_email["address"]).strip()
                subject = str(raw_email["subject"]).strip()
                body = str(raw_email["body"]).strip()
            except (KeyError, ValueError) as exception:
                raise EmailDeliveryError("Outbox email payload is invalid.") from exception
            if not address or not subject or not body:
                raise EmailDeliveryError("Outbox email payload is invalid.")
            messages.append(EmailMessage(recipient_id, address, subject, body))
        return tuple(messages)


class NullWorkerMetrics(IWorkerMetrics):
    """No-op metrics adapter for deployments without a metrics backend."""

    def increment(self, metric_name: str) -> None:
        """Accept a metric outcome without external side effects."""


class OutboxWorker:
    """Claim due events, deliver deduplicated email, and schedule bounded retries."""

    def __init__(
        self,
        unit_of_work_factory: IUnitOfWorkFactory,
        email_sender: IEmailSender,
        renderer: IEmailRenderer,
        clock: IClock,
        *,
        worker_id: str,
        metrics: IWorkerMetrics | None = None,
        eligibility_policy: IOutboxEventEligibilityPolicy | None = None,
        id_factory: Callable[[], UUID] = uuid4,
        lease_duration: timedelta = timedelta(minutes=2),
        base_retry_delay: timedelta = timedelta(seconds=30),
        max_retry_delay: timedelta = timedelta(hours=1),
        batch_size: int = 20,
    ) -> None:
        if not worker_id.strip() or len(worker_id) > 160 or batch_size <= 0:
            raise ValueError("Worker ID must be non-blank and batch size positive.")
        if (
            lease_duration <= timedelta(0)
            or base_retry_delay <= timedelta(0)
            or max_retry_delay < base_retry_delay
        ):
            raise ValueError("Worker lease and retry delay must be positive.")
        self._unit_of_work_factory = unit_of_work_factory
        self._email_sender = email_sender
        self._renderer = renderer
        self._clock = clock
        self._worker_id = worker_id
        self._metrics = metrics or NullWorkerMetrics()
        self._eligibility_policy = eligibility_policy or (
            CurrentStateReminderEligibilityPolicy(unit_of_work_factory)
        )
        self._id_factory = id_factory
        self._lease_duration = lease_duration
        self._base_retry_delay = base_retry_delay
        self._max_retry_delay = max_retry_delay
        self._batch_size = batch_size
        self._logger = structlog.get_logger("ams.outbox.worker")

    async def run_once(self) -> int:
        """Claim one batch and process each event independently."""

        claimed_at = self._clock.now()
        async with self._unit_of_work_factory() as unit_of_work:
            events = await unit_of_work.outbox.claim_due(
                now=claimed_at,
                worker_id=self._worker_id,
                lease_expires_at=claimed_at + self._lease_duration,
                limit=self._batch_size,
            )
            await unit_of_work.commit()
        for event in events:
            self._metrics.increment("outbox_claimed")
            await self._process_event(event)
        return len(events)

    async def _process_event(self, event: ClaimedOutboxEvent) -> None:
        try:
            if not await self._eligibility_policy.is_eligible(event):
                completed_at = self._clock.now()
                async with self._unit_of_work_factory() as unit_of_work:
                    await unit_of_work.outbox.mark_delivered(
                        event.id,
                        worker_id=self._worker_id,
                        completed_at=completed_at,
                    )
                    await unit_of_work.commit()
                self._metrics.increment("outbox_ineligible")
                self._logger.info("outbox_event_ineligible", outbox_event_id=str(event.id))
                return
            messages = self._renderer.render(event)
            for message in messages:
                await self._deliver(event, message)
            completed_at = self._clock.now()
            async with self._unit_of_work_factory() as unit_of_work:
                await unit_of_work.outbox.mark_delivered(
                    event.id,
                    worker_id=self._worker_id,
                    completed_at=completed_at,
                )
                await unit_of_work.commit()
            self._metrics.increment("outbox_delivered")
            self._logger.info("outbox_event_delivered", outbox_event_id=str(event.id))
        except Exception as exception:
            try:
                await self._schedule_failure(event, exception)
            except OutboxLeaseLostError:
                self._metrics.increment("outbox_lease_lost")
                self._logger.warning(
                    "outbox_event_lease_lost",
                    outbox_event_id=str(event.id),
                    worker_id=self._worker_id,
                )

    async def _deliver(self, event: ClaimedOutboxEvent, message: EmailMessage) -> None:
        notification = await self._ensure_notification(event, message)
        if notification.status is NotificationStatus.DELIVERED:
            return
        attempted_at = self._clock.now()
        try:
            provider_message_id = await self._email_sender.send(message)
        except Exception as exception:
            await self._record_failed_notification(
                event.id,
                message.recipient_id,
                attempted_at,
            )
            raise EmailDeliveryError("Email provider rejected delivery.") from exception
        async with self._unit_of_work_factory() as unit_of_work:
            current = await unit_of_work.notifications.get_for_update(
                event.id, message.recipient_id
            )
            if current is None:
                raise EmailDeliveryError("Notification audit record is unavailable.")
            current.mark_delivered(
                provider_message_id=provider_message_id,
                attempted_at=attempted_at,
            )
            await unit_of_work.notifications.save(current)
            await unit_of_work.commit()

    async def _ensure_notification(
        self, event: ClaimedOutboxEvent, message: EmailMessage
    ) -> Notification:
        async with self._unit_of_work_factory() as unit_of_work:
            current = await unit_of_work.notifications.get_for_update(
                event.id, message.recipient_id
            )
            if current is None:
                current = Notification(
                    id=self._id_factory(),
                    outbox_event_id=event.id,
                    recipient_id=message.recipient_id,
                    recipient_address=message.recipient_address,
                    created_at=self._clock.now(),
                )
                await unit_of_work.notifications.add(current)
            await unit_of_work.commit()
            return current

    async def _record_failed_notification(
        self,
        event_id: UUID,
        recipient_id: UUID,
        attempted_at: datetime,
    ) -> None:
        async with self._unit_of_work_factory() as unit_of_work:
            current = await unit_of_work.notifications.get_for_update(event_id, recipient_id)
            if current is not None:
                current.mark_failed(
                    attempted_at=attempted_at,
                    error="Email delivery failed.",
                )
                await unit_of_work.notifications.save(current)
            await unit_of_work.commit()

    async def _schedule_failure(self, event: ClaimedOutboxEvent, exception: Exception) -> None:
        failed_at = self._clock.now()
        dead_letter = event.attempt_count >= event.max_attempts
        retry_delay = min(
            self._base_retry_delay * (2 ** max(event.attempt_count - 1, 0)),
            self._max_retry_delay,
        )
        async with self._unit_of_work_factory() as unit_of_work:
            await unit_of_work.outbox.mark_retry(
                event.id,
                worker_id=self._worker_id,
                failed_at=failed_at,
                next_attempt_at=failed_at + retry_delay,
                error="Outbox event processing failed.",
                dead_letter=dead_letter,
            )
            await unit_of_work.commit()
        metric = "outbox_dead_letter" if dead_letter else "outbox_retry"
        self._metrics.increment(metric)
        self._logger.warning(
            "outbox_event_failed",
            outbox_event_id=str(event.id),
            attempt_count=event.attempt_count,
            dead_letter=dead_letter,
            exception_type=type(exception).__name__,
        )
