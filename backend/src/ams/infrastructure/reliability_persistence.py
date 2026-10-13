"""SQLAlchemy reliability repositories sharing the business transaction."""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Result
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.dml import Update

from ams.application.reliability import (
    ClaimedOutboxEvent,
    IdempotencyRecord,
    IdempotencyStatus,
    IEventReminderRepository,
    IIdempotencyRepository,
    INotificationRepository,
    IOutboxRepository,
    ITicketReminderRepository,
    Notification,
    NotificationStatus,
    OutboxEvent,
    OutboxLeaseLostError,
    ReliabilityPersistenceError,
)
from ams.application.unit_of_work import IUnitOfWork
from ams.infrastructure.asset_import_persistence import SqlAlchemyAssetImportRepository
from ams.infrastructure.asset_persistence import (
    AssetPersistenceBase,
    SqlAlchemyAssetRepository,
    SqlAlchemyCustodyEvidenceRepository,
    SqlAlchemyCustodyRepository,
)
from ams.infrastructure.event_persistence import SqlAlchemyEventDayRepository
from ams.infrastructure.persistence import SqlAlchemyUserRepository
from ams.infrastructure.ticket_persistence import SqlAlchemyTicketRepository


class OutboxEventModel(AssetPersistenceBase):
    """SQLAlchemy mapping for durable outbox events."""

    __tablename__ = "outbox_events"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100))
    aggregate_type: Mapped[str] = mapped_column(String(100))
    aggregate_id: Mapped[UUID]
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(2000))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NotificationModel(AssetPersistenceBase):
    """SQLAlchemy mapping for one logical email-delivery audit."""

    __tablename__ = "notifications"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    outbox_event_id: Mapped[UUID] = mapped_column(ForeignKey("outbox_events.id"))
    recipient_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    channel: Mapped[str] = mapped_column(String(16))
    recipient_address: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(16))
    attempt_count: Mapped[int] = mapped_column(Integer)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    last_error: Mapped[str | None] = mapped_column(String(2000))
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IdempotencyRecordModel(AssetPersistenceBase):
    """SQLAlchemy mapping for actor-scoped idempotency state."""

    __tablename__ = "idempotency_records"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    command_name: Mapped[str] = mapped_column(String(160))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    response_status_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SqlAlchemyOutboxRepository(IOutboxRepository):
    """Stage durable events in the current business transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: OutboxEvent) -> None:
        """Stage and flush a pending event without committing."""

        self._session.add(
            OutboxEventModel(
                id=event.id,
                event_type=event.event_type,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                payload=dict(event.payload),
                status="PENDING",
                scheduled_at=event.scheduled_at,
                next_attempt_at=event.scheduled_at,
                attempt_count=0,
                max_attempts=8,
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
                completed_at=None,
                created_at=event.created_at,
                updated_at=event.created_at,
            )
        )
        try:
            await self._session.flush()
        except SQLAlchemyError as exception:
            raise ReliabilityPersistenceError(
                "Outbox persistence operation failed."
            ) from exception

    async def claim_due(
        self,
        *,
        now: datetime,
        worker_id: str,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        """Lease due/recoverable events atomically with skip-locked selection."""

        try:
            await self._session.execute(
                text(
                    """
                    UPDATE outbox_events
                    SET status = 'DEAD_LETTER',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        last_error = 'Worker lease expired after final attempt.',
                        completed_at = :now,
                        updated_at = :now
                    WHERE status = 'PROCESSING'
                      AND lease_expires_at <= :now
                      AND attempt_count >= max_attempts
                    """
                ),
                {"now": now},
            )
            result = await self._session.execute(
                text(
                    """
                    WITH candidates AS (
                        SELECT id
                        FROM outbox_events
                        WHERE (
                            (status = 'PENDING' AND next_attempt_at <= :now)
                            OR (status = 'PROCESSING' AND lease_expires_at <= :now)
                        )
                          AND scheduled_at <= :now
                          AND attempt_count < max_attempts
                        ORDER BY next_attempt_at, scheduled_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT :limit
                    )
                    UPDATE outbox_events AS event
                    SET status = 'PROCESSING',
                        attempt_count = event.attempt_count + 1,
                        lease_owner = :worker_id,
                        lease_expires_at = :lease_expires_at,
                        updated_at = :now
                    FROM candidates
                    WHERE event.id = candidates.id
                    RETURNING event.id, event.event_type, event.aggregate_type,
                              event.aggregate_id, event.payload, event.attempt_count,
                              event.max_attempts, event.created_at
                    """
                ),
                {
                    "now": now,
                    "worker_id": worker_id,
                    "lease_expires_at": lease_expires_at,
                    "limit": limit,
                },
            )
        except SQLAlchemyError as exception:
            raise ReliabilityPersistenceError("Outbox claim failed.") from exception
        return tuple(
            ClaimedOutboxEvent(
                id=row.id,
                event_type=str(row.event_type),
                aggregate_type=str(row.aggregate_type),
                aggregate_id=row.aggregate_id,
                payload=dict(row.payload),
                attempt_count=int(row.attempt_count),
                max_attempts=int(row.max_attempts),
                created_at=row.created_at,
            )
            for row in result
        )

    async def mark_delivered(
        self, event_id: UUID, *, worker_id: str, completed_at: datetime
    ) -> None:
        """Stage delivered terminal state and release the worker lease."""

        await self._execute_owned_update(
            update(OutboxEventModel)
            .where(
                OutboxEventModel.id == event_id,
                OutboxEventModel.status == "PROCESSING",
                OutboxEventModel.lease_owner == worker_id,
                OutboxEventModel.lease_expires_at > completed_at,
            )
            .values(
                status="DELIVERED",
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
                completed_at=completed_at,
                updated_at=completed_at,
            )
        )

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
        """Release the lease into pending retry or terminal dead-letter state."""

        await self._execute_owned_update(
            update(OutboxEventModel)
            .where(
                OutboxEventModel.id == event_id,
                OutboxEventModel.status == "PROCESSING",
                OutboxEventModel.lease_owner == worker_id,
                OutboxEventModel.lease_expires_at > failed_at,
            )
            .values(
                status="DEAD_LETTER" if dead_letter else "PENDING",
                next_attempt_at=next_attempt_at,
                lease_owner=None,
                lease_expires_at=None,
                last_error=error,
                completed_at=failed_at if dead_letter else None,
                updated_at=failed_at,
            )
        )

    async def _execute_owned_update(self, statement: Update) -> None:
        try:
            result = await self._session.execute(statement.returning(OutboxEventModel.id))
        except SQLAlchemyError as exception:
            raise ReliabilityPersistenceError("Outbox update failed.") from exception
        if result.scalar_one_or_none() is None:
            raise OutboxLeaseLostError("Outbox event lease is no longer owned.")


class SqlAlchemyTicketReminderRepository(ITicketReminderRepository):
    """Upsert future Ticket reminder slots without duplicating worker-visible events."""

    EVENT_TYPES = (
        "TICKET_DUE_3_DAYS",
        "TICKET_DUE_1_DAY",
        "TICKET_OVERDUE",
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_unattempted(
        self, ticket_id: UUID, reminders: tuple[OutboxEvent, ...]
    ) -> None:
        """Update one row per desired slot and remove obsolete unattempted rows."""

        try:
            result = await self._session.execute(
                select(OutboxEventModel)
                .where(
                    OutboxEventModel.aggregate_type == "BORROW_TICKET",
                    OutboxEventModel.aggregate_id == ticket_id,
                    OutboxEventModel.event_type.in_(self.EVENT_TYPES),
                )
                .order_by(OutboxEventModel.event_type, OutboxEventModel.id)
                .with_for_update()
            )
            existing_by_type: dict[str, list[OutboxEventModel]] = {}
            for model in result.scalars():
                existing_by_type.setdefault(model.event_type, []).append(model)

            desired_types = {reminder.event_type for reminder in reminders}
            editable_by_type: dict[str, OutboxEventModel] = {}
            for event_type, models in existing_by_type.items():
                editable = [
                    model
                    for model in models
                    if model.status == "PENDING" and model.attempt_count == 0
                ]
                if event_type not in desired_types:
                    for model in editable:
                        await self._session.delete(model)
                    continue
                worker_visible = [model for model in models if model not in editable]
                if worker_visible:
                    for model in editable:
                        await self._session.delete(model)
                    continue
                if editable:
                    editable_by_type[event_type] = editable[0]
                    for duplicate in editable[1:]:
                        await self._session.delete(duplicate)

            for reminder in reminders:
                existing_models = existing_by_type.get(reminder.event_type, [])
                existing_model = editable_by_type.get(reminder.event_type)
                if not existing_models:
                    self._session.add(self._to_model(reminder))
                elif existing_model is not None:
                    existing_model.payload = dict(reminder.payload)
                    existing_model.scheduled_at = reminder.scheduled_at
                    existing_model.next_attempt_at = reminder.scheduled_at
                    existing_model.updated_at = reminder.created_at
            await self._session.flush()
        except SQLAlchemyError as exception:
            raise ReliabilityPersistenceError(
                "Ticket reminder persistence operation failed."
            ) from exception

    async def cancel_unattempted(self, ticket_id: UUID) -> None:
        """Delete worker-unseen Ticket reminders after terminal transitions."""

        try:
            await self._session.execute(
                delete(OutboxEventModel).where(
                    OutboxEventModel.aggregate_type == "BORROW_TICKET",
                    OutboxEventModel.aggregate_id == ticket_id,
                    OutboxEventModel.event_type.in_(self.EVENT_TYPES),
                    OutboxEventModel.status == "PENDING",
                    OutboxEventModel.attempt_count == 0,
                )
            )
        except SQLAlchemyError as exception:
            raise ReliabilityPersistenceError(
                "Ticket reminder cancellation failed."
            ) from exception

    @staticmethod
    def _to_model(event: OutboxEvent) -> OutboxEventModel:
        return OutboxEventModel(
            id=event.id,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            payload=dict(event.payload),
            status="PENDING",
            scheduled_at=event.scheduled_at,
            next_attempt_at=event.scheduled_at,
            attempt_count=0,
            max_attempts=8,
            lease_owner=None,
            lease_expires_at=None,
            last_error=None,
            completed_at=None,
            created_at=event.created_at,
            updated_at=event.created_at,
        )


class SqlAlchemyEventReminderRepository(IEventReminderRepository):
    """Replace unsent Shift reminder slots while preserving delivery history."""

    EVENT_TYPES = ("SHIFT_OPEN_REMINDER", "SHIFT_CLOSE_REMINDER")

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_unattempted(
        self, shift_id: UUID, reminders: tuple[OutboxEvent, ...]
    ) -> None:
        """Upsert one unsent row per desired open/close reminder type."""

        try:
            result = await self._session.execute(
                select(OutboxEventModel)
                .where(
                    OutboxEventModel.aggregate_type == "SHIFT",
                    OutboxEventModel.aggregate_id == shift_id,
                    OutboxEventModel.event_type.in_(self.EVENT_TYPES),
                )
                .order_by(OutboxEventModel.event_type, OutboxEventModel.id)
                .with_for_update()
            )
            existing_by_type: dict[str, list[OutboxEventModel]] = {}
            for model in result.scalars():
                existing_by_type.setdefault(model.event_type, []).append(model)
            desired = {event.event_type: event for event in reminders}
            for event_type, models in existing_by_type.items():
                editable = [
                    model
                    for model in models
                    if model.status == "PENDING" and model.attempt_count == 0
                ]
                worker_visible = [model for model in models if model not in editable]
                event = desired.pop(event_type, None)
                if event is None or worker_visible:
                    for model in editable:
                        await self._session.delete(model)
                    continue
                if editable:
                    model = editable[0]
                    model.payload = dict(event.payload)
                    model.scheduled_at = event.scheduled_at
                    model.next_attempt_at = event.scheduled_at
                    model.updated_at = event.created_at
                    for duplicate in editable[1:]:
                        await self._session.delete(duplicate)
                else:
                    self._session.add(SqlAlchemyTicketReminderRepository._to_model(event))
            for event in desired.values():
                self._session.add(SqlAlchemyTicketReminderRepository._to_model(event))
            await self._session.flush()
        except SQLAlchemyError as exception:
            raise ReliabilityPersistenceError(
                "Event reminder persistence operation failed."
            ) from exception

    async def cancel_unattempted(self, shift_id: UUID) -> None:
        """Delete only reminder rows no worker has observed."""

        try:
            await self._session.execute(
                delete(OutboxEventModel).where(
                    OutboxEventModel.aggregate_type == "SHIFT",
                    OutboxEventModel.aggregate_id == shift_id,
                    OutboxEventModel.event_type.in_(self.EVENT_TYPES),
                    OutboxEventModel.status == "PENDING",
                    OutboxEventModel.attempt_count == 0,
                )
            )
        except SQLAlchemyError as exception:
            raise ReliabilityPersistenceError(
                "Event reminder cancellation failed."
            ) from exception


class SqlAlchemyNotificationRepository(INotificationRepository):
    """Stage deduplicated email-delivery audit rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, notification: Notification) -> None:
        """Stage one pending email notification without committing."""

        self._session.add(
            NotificationModel(
                id=notification.id,
                outbox_event_id=notification.outbox_event_id,
                recipient_id=notification.recipient_id,
                channel="EMAIL",
                recipient_address=notification.recipient_address.strip(),
                status=notification.status.value,
                attempt_count=notification.attempt_count,
                provider_message_id=notification.provider_message_id,
                last_error=notification.last_error,
                last_attempted_at=notification.last_attempted_at,
                delivered_at=notification.delivered_at,
                created_at=notification.created_at,
                updated_at=notification.created_at,
            )
        )

    async def get_for_update(
        self, outbox_event_id: UUID, recipient_id: UUID
    ) -> Notification | None:
        """Lock and load one logical EMAIL delivery record."""

        try:
            result = await self._session.execute(
                select(NotificationModel)
                .where(
                    NotificationModel.outbox_event_id == outbox_event_id,
                    NotificationModel.recipient_id == recipient_id,
                    NotificationModel.channel == "EMAIL",
                )
                .with_for_update()
            )
        except SQLAlchemyError as exception:
            raise ReliabilityPersistenceError(
                "Notification persistence operation failed."
            ) from exception
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return Notification(
            id=model.id,
            outbox_event_id=model.outbox_event_id,
            recipient_id=model.recipient_id,
            recipient_address=model.recipient_address,
            created_at=model.created_at,
            status=NotificationStatus(model.status),
            attempt_count=model.attempt_count,
            provider_message_id=model.provider_message_id,
            last_error=model.last_error,
            last_attempted_at=model.last_attempted_at,
            delivered_at=model.delivered_at,
        )

    async def save(self, notification: Notification) -> None:
        """Stage delivery audit fields without committing."""

        try:
            await self._session.execute(
                update(NotificationModel)
                .where(NotificationModel.id == notification.id)
                .values(
                    status=notification.status.value,
                    attempt_count=notification.attempt_count,
                    provider_message_id=notification.provider_message_id,
                    last_error=notification.last_error,
                    last_attempted_at=notification.last_attempted_at,
                    delivered_at=notification.delivered_at,
                    updated_at=func.now(),
                )
            )
        except SQLAlchemyError as exception:
            raise ReliabilityPersistenceError(
                "Notification persistence operation failed."
            ) from exception


class SqlAlchemyIdempotencyRepository(IIdempotencyRepository):
    """Lock and persist canonical command replay records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire(self, candidate: IdempotencyRecord) -> IdempotencyRecord:
        """Insert a candidate or wait for and lock the concurrent winning record."""

        await self._execute(
            insert(IdempotencyRecordModel)
            .values(
                id=candidate.id,
                actor_id=candidate.actor_id,
                command_name=candidate.command_name,
                idempotency_key=candidate.idempotency_key,
                request_fingerprint=candidate.request_fingerprint,
                status=candidate.status.value,
                response_status_code=candidate.response_status_code,
                response_body=(
                    dict(candidate.response_body)
                    if candidate.response_body is not None
                    else None
                ),
                completed_at=candidate.completed_at,
                expires_at=candidate.expires_at,
                created_at=candidate.created_at,
                updated_at=candidate.created_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "actor_id",
                    "command_name",
                    "idempotency_key",
                ]
            )
        )
        record = await self.get_for_update(
            candidate.actor_id,
            candidate.command_name,
            candidate.idempotency_key,
        )
        if record is None:
            raise ReliabilityPersistenceError(
                "Idempotency acquisition did not produce a record."
            )
        return record

    async def get_for_update(
        self, actor_id: UUID, command_name: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        """Lock and load one actor-command-key record."""

        result = await self._execute(
            select(IdempotencyRecordModel)
            .where(
                IdempotencyRecordModel.actor_id == actor_id,
                IdempotencyRecordModel.command_name == command_name,
                IdempotencyRecordModel.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        model = result.scalar_one_or_none()
        return None if model is None else self._to_entity(model)

    async def add(self, record: IdempotencyRecord) -> None:
        """Stage an idempotency record without committing."""

        self._session.add(
            IdempotencyRecordModel(
                id=record.id,
                actor_id=record.actor_id,
                command_name=record.command_name,
                idempotency_key=record.idempotency_key,
                request_fingerprint=record.request_fingerprint,
                status=record.status.value,
                response_status_code=record.response_status_code,
                response_body=(
                    dict(record.response_body) if record.response_body is not None else None
                ),
                completed_at=record.completed_at,
                expires_at=record.expires_at,
                created_at=record.created_at,
                updated_at=record.created_at,
            )
        )

    async def save(self, record: IdempotencyRecord) -> None:
        """Stage response replay fields without committing."""

        await self._execute(
            update(IdempotencyRecordModel)
            .where(IdempotencyRecordModel.id == record.id)
            .values(
                status=record.status.value,
                response_status_code=record.response_status_code,
                response_body=(
                    dict(record.response_body) if record.response_body is not None else None
                ),
                completed_at=record.completed_at,
                updated_at=func.now(),
            )
        )

    async def _execute(self, statement: Executable) -> Result[Any]:
        try:
            return await self._session.execute(statement)
        except SQLAlchemyError as exception:
            raise ReliabilityPersistenceError(
                "Idempotency persistence operation failed."
            ) from exception

    @staticmethod
    def _to_entity(model: IdempotencyRecordModel) -> IdempotencyRecord:
        return IdempotencyRecord(
            id=model.id,
            actor_id=model.actor_id,
            command_name=model.command_name,
            idempotency_key=model.idempotency_key,
            request_fingerprint=model.request_fingerprint,
            status=IdempotencyStatus(model.status),
            response_status_code=model.response_status_code,
            response_body=model.response_body,
            completed_at=model.completed_at,
            expires_at=model.expires_at,
            created_at=model.created_at,
        )


class SqlAlchemyUnitOfWork(IUnitOfWork):
    """Own one transaction spanning business and reliability repositories."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker
        self._session: AsyncSession | None = None
        self._committed = False

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        """Open one session and wire every repository to it."""

        self._session = self._session_maker()
        self.assets = SqlAlchemyAssetRepository(self._session)
        self.custodies = SqlAlchemyCustodyRepository(self._session)
        self.custody_evidence = SqlAlchemyCustodyEvidenceRepository(self._session)
        self.outbox = SqlAlchemyOutboxRepository(self._session)
        self.notifications = SqlAlchemyNotificationRepository(self._session)
        self.idempotency = SqlAlchemyIdempotencyRepository(self._session)
        self.users = SqlAlchemyUserRepository(self._session)
        self.asset_imports = SqlAlchemyAssetImportRepository(self._session)
        self.tickets = SqlAlchemyTicketRepository(self._session)
        self.ticket_reminders = SqlAlchemyTicketReminderRepository(self._session)
        self.event_days = SqlAlchemyEventDayRepository(self._session)
        self.event_reminders = SqlAlchemyEventReminderRepository(self._session)
        from ams.infrastructure.shift_persistence import (
            SqlAlchemyShiftExecutionRepository,
        )

        self.shifts = SqlAlchemyShiftExecutionRepository(self._session)
        from ams.infrastructure.reconciliation_persistence import (
            SqlAlchemyReconciliationRepository,
        )

        self.reconciliations = SqlAlchemyReconciliationRepository(self._session)
        from ams.infrastructure.handover_persistence import SqlAlchemyHandoverRepository
        from ams.infrastructure.transfer_persistence import SqlAlchemyTransferRepository

        self.transfers = SqlAlchemyTransferRepository(self._session)
        self.handovers = SqlAlchemyHandoverRepository(self._session)
        self._committed = False
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back failed/uncommitted work and close the session."""

        if self._session is None:
            return
        if exception is not None or not self._committed:
            await self._session.rollback()
        await self._session.close()
        self._session = None

    async def commit(self) -> None:
        """Commit aggregate, idempotency, and outbox changes atomically."""

        session = self._require_session()
        try:
            await session.commit()
            self._committed = True
        except SQLAlchemyError as exception:
            await session.rollback()
            raise ReliabilityPersistenceError(
                "Transactional reliability commit failed."
            ) from exception

    async def rollback(self) -> None:
        """Discard every staged business and reliability change."""

        await self._require_session().rollback()
        self._committed = False

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("Unit of Work is not active.")
        return self._session
