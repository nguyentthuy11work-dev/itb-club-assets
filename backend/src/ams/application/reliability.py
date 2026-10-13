"""Reliability records and ports used by transactional application services."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID


class ReliabilityError(Exception):
    """Base class for expected reliability-boundary failures."""


class ReliabilityPersistenceError(ReliabilityError):
    """Sanitized persistence failure from a reliability adapter."""

    code = "RELIABILITY_PERSISTENCE_FAILED"


class OutboxLeaseLostError(ReliabilityError):
    """The worker no longer owns a live lease for an outbox event."""

    code = "OUTBOX_LEASE_LOST"


class IdempotencyStatus(StrEnum):
    """Persisted command execution states."""

    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"


class NotificationStatus(StrEnum):
    """Logical email delivery states."""

    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """A durable business event staged in the business transaction."""

    id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    payload: Mapping[str, object]
    scheduled_at: datetime
    created_at: datetime

    def __post_init__(self) -> None:
        """Reject events that cannot satisfy the V3 schema contract."""

        if not self.event_type.strip() or not self.aggregate_type.strip():
            raise ReliabilityError("Outbox event and aggregate types must not be blank.")
        _assert_utc(self.scheduled_at, "Outbox schedule")
        _assert_utc(self.created_at, "Outbox creation time")


@dataclass(slots=True)
class Notification:
    """One logical email delivery audit record for an outbox recipient."""

    id: UUID
    outbox_event_id: UUID
    recipient_id: UUID
    recipient_address: str
    created_at: datetime
    status: NotificationStatus = NotificationStatus.PENDING
    attempt_count: int = 0
    provider_message_id: str | None = None
    last_error: str | None = None
    last_attempted_at: datetime | None = None
    delivered_at: datetime | None = None

    def __post_init__(self) -> None:
        """Require a non-blank address and normalized creation time."""

        if not self.recipient_address.strip():
            raise ReliabilityError("Notification recipient address must not be blank.")
        _assert_utc(self.created_at, "Notification creation time")
        self._validate_state()

    def mark_delivered(self, *, provider_message_id: str, attempted_at: datetime) -> None:
        """Record a successful provider delivery attempt."""

        if self.status is NotificationStatus.DELIVERED:
            return
        if not provider_message_id.strip():
            raise ReliabilityError("Delivered notification requires a provider message ID.")
        self._assert_attempt_time(attempted_at)
        self.status = NotificationStatus.DELIVERED
        self.attempt_count += 1
        self.provider_message_id = provider_message_id
        self.last_error = None
        self.last_attempted_at = attempted_at
        self.delivered_at = attempted_at

    def mark_failed(self, *, attempted_at: datetime, error: str) -> None:
        """Record a sanitized failed attempt while retaining the logical row."""

        if self.status is NotificationStatus.DELIVERED:
            raise ReliabilityError("A delivered notification cannot fail later.")
        if not error.strip():
            raise ReliabilityError("Failed notification requires a sanitized error.")
        self._assert_attempt_time(attempted_at)
        self.status = NotificationStatus.FAILED
        self.attempt_count += 1
        self.provider_message_id = None
        self.last_error = error
        self.last_attempted_at = attempted_at
        self.delivered_at = None

    def _assert_attempt_time(self, attempted_at: datetime) -> None:
        _assert_utc(attempted_at, "Notification attempt time")
        if attempted_at < self.created_at:
            raise ReliabilityError("Notification attempt cannot precede creation.")

    def _validate_state(self) -> None:
        if self.attempt_count < 0:
            raise ReliabilityError("Notification attempts cannot be negative.")
        for value, label in (
            (self.last_attempted_at, "Notification attempt time"),
            (self.delivered_at, "Notification delivery time"),
        ):
            if value is not None:
                _assert_utc(value, label)
                if value < self.created_at:
                    raise ReliabilityError(f"{label} cannot precede creation.")
        if self.status is NotificationStatus.PENDING:
            valid = self.attempt_count == 0 and self.delivered_at is None
        elif self.status is NotificationStatus.DELIVERED:
            valid = (
                self.attempt_count > 0
                and self.delivered_at is not None
                and self.last_attempted_at is not None
            )
        else:
            valid = (
                self.attempt_count > 0
                and self.delivered_at is None
                and self.last_attempted_at is not None
                and bool(self.last_error)
            )
        if not valid:
            raise ReliabilityError("Notification state metadata is inconsistent.")


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    """One leased event returned to a worker after an atomic database claim."""

    id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    payload: Mapping[str, object]
    attempt_count: int
    max_attempts: int
    created_at: datetime


@dataclass(slots=True)
class IdempotencyRecord:
    """Actor-scoped command fingerprint and optional replay response."""

    id: UUID
    actor_id: UUID
    command_name: str
    idempotency_key: str
    request_fingerprint: str
    status: IdempotencyStatus
    expires_at: datetime
    created_at: datetime
    response_status_code: int | None = None
    response_body: Mapping[str, object] | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate fingerprint, timestamps, and state-dependent response data."""

        if not self.command_name.strip() or not self.idempotency_key.strip():
            raise ReliabilityError("Idempotency command and key must not be blank.")
        if len(self.command_name) > 160 or len(self.idempotency_key) > 255:
            raise ReliabilityError("Idempotency command or key exceeds its maximum length.")
        if len(self.request_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.request_fingerprint
        ):
            raise ReliabilityError("Idempotency fingerprint must be lowercase SHA-256.")
        _assert_utc(self.created_at, "Idempotency creation time")
        _assert_utc(self.expires_at, "Idempotency expiry")
        if self.expires_at <= self.created_at:
            raise ReliabilityError("Idempotency expiry must follow creation time.")
        if self.completed_at is not None:
            _assert_utc(self.completed_at, "Idempotency completion time")
        self._validate_state()

    def succeed(
        self,
        *,
        response_status_code: int,
        response_body: Mapping[str, object],
        completed_at: datetime,
    ) -> None:
        """Store the canonical successful response exactly once."""

        if self.status is not IdempotencyStatus.IN_PROGRESS:
            raise ReliabilityError("Only an in-progress command can succeed.")
        if not 200 <= response_status_code <= 299:
            raise ReliabilityError("A successful command requires a 2xx response status.")
        _assert_utc(completed_at, "Idempotency completion time")
        if completed_at < self.created_at:
            raise ReliabilityError("Idempotency completion cannot precede creation.")
        self.status = IdempotencyStatus.SUCCEEDED
        self.response_status_code = response_status_code
        self.response_body = dict(response_body)
        self.completed_at = completed_at

    def _validate_state(self) -> None:
        response_is_empty = (
            self.response_status_code is None
            and self.response_body is None
            and self.completed_at is None
        )
        response_is_complete = (
            self.response_status_code is not None
            and 200 <= self.response_status_code <= 299
            and self.response_body is not None
            and self.completed_at is not None
            and self.completed_at >= self.created_at
        )
        if self.status is IdempotencyStatus.IN_PROGRESS and not response_is_empty:
            raise ReliabilityError("In-progress idempotency records cannot contain a response.")
        if self.status is IdempotencyStatus.SUCCEEDED and not response_is_complete:
            raise ReliabilityError("Succeeded idempotency records require a complete response.")


@dataclass(frozen=True, slots=True)
class StagedFile:
    """Validated private file staged outside permanent resource ownership."""

    staging_key: str
    content_type: str
    content_sha256: str
    size_bytes: int


class IOutboxRepository(ABC):
    """Persist business events without delivering them in the request."""

    @abstractmethod
    async def add(self, event: OutboxEvent) -> None:
        """Stage an event without committing."""

    @abstractmethod
    async def claim_due(
        self,
        *,
        now: datetime,
        worker_id: str,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        """Atomically lease due events using skip-locked semantics."""

    @abstractmethod
    async def mark_delivered(
        self, event_id: UUID, *, worker_id: str, completed_at: datetime
    ) -> None:
        """Stage successful terminal event state."""

    @abstractmethod
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
        """Release an event for retry or stage terminal dead-letter state."""


class IEventReminderRepository(ABC):
    """Maintain the two worker-unseen reminder slots for one Shift."""

    @abstractmethod
    async def replace_unattempted(
        self, shift_id: UUID, reminders: tuple[OutboxEvent, ...]
    ) -> None:
        """Replace unsent open/close slots while preserving attempted history."""

    @abstractmethod
    async def cancel_unattempted(self, shift_id: UUID) -> None:
        """Remove worker-unseen reminder slots for a removed Shift."""


class ITicketReminderRepository(ABC):
    """Replace or cancel unattempted durable reminders for one Borrow Ticket."""

    @abstractmethod
    async def replace_unattempted(
        self, ticket_id: UUID, reminders: tuple[OutboxEvent, ...]
    ) -> None:
        """Upsert desired reminder slots and remove obsolete unattempted slots."""

    @abstractmethod
    async def cancel_unattempted(self, ticket_id: UUID) -> None:
        """Remove every pending reminder that has never reached a worker."""


class INotificationRepository(ABC):
    """Persist one deduplicated delivery audit per event recipient and channel."""

    @abstractmethod
    async def add(self, notification: Notification) -> None:
        """Stage a pending email-delivery record without committing."""

    @abstractmethod
    async def get_for_update(
        self, outbox_event_id: UUID, recipient_id: UUID
    ) -> Notification | None:
        """Lock one logical email delivery audit row."""

    @abstractmethod
    async def save(self, notification: Notification) -> None:
        """Stage delivery-attempt state without committing."""


class IIdempotencyRepository(ABC):
    """Persist and lock actor-scoped command execution records."""

    @abstractmethod
    async def acquire(self, candidate: IdempotencyRecord) -> IdempotencyRecord:
        """Insert a candidate or lock and return the concurrent existing record."""

    @abstractmethod
    async def get_for_update(
        self, actor_id: UUID, command_name: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        """Lock and return the exact command key within the transaction."""

    @abstractmethod
    async def add(self, record: IdempotencyRecord) -> None:
        """Stage a new in-progress record without committing."""

    @abstractmethod
    async def save(self, record: IdempotencyRecord) -> None:
        """Stage replay-response state without committing."""


class IFileStorage(ABC):
    """Private evidence staging and ownership boundary."""

    @abstractmethod
    async def stage(
        self, content: AsyncIterable[bytes], declared_content_type: str
    ) -> StagedFile:
        """Validate and stage one stream under an opaque temporary key."""

    @abstractmethod
    async def finalize(self, staging_key: str, owner_type: str, owner_id: UUID) -> str:
        """Move staged content under permanent private resource ownership."""

    @abstractmethod
    async def delete(self, object_key: str) -> None:
        """Delete staged or permanent private content idempotently."""


class IClock(ABC):
    """Provide deterministic UTC time to application services and schedulers."""

    @abstractmethod
    def now(self) -> datetime:
        """Return the current timezone-aware UTC instant."""


def _assert_utc(value: datetime, label: str) -> None:
    if value.utcoffset() != timedelta(0):
        raise ReliabilityError(f"{label} must be timezone-aware UTC.")
