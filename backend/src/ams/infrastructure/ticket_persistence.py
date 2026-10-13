"""SQLAlchemy mappings and repository for the Flyway V4 Ticket schema."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.engine import Result
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.base import Executable

from ams.application.tickets import TicketPersistenceError
from ams.domain.tickets import (
    BorrowTicket,
    ITicketRepository,
    TicketEvent,
    TicketEvidence,
    TicketItem,
    TicketMethod,
    TicketStatus,
)
from ams.infrastructure.asset_persistence import AssetPersistenceBase

TICKET_METHOD_ENUM = ENUM(TicketMethod, name="ticket_method", create_type=False)
TICKET_STATUS_ENUM = ENUM(TicketStatus, name="ticket_status", create_type=False)


class BorrowTicketModel(AssetPersistenceBase):
    """Mapping for one grouped Borrow Ticket."""

    __tablename__ = "borrow_tickets"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    borrower_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    source_custodian_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    method: Mapped[TicketMethod] = mapped_column(TICKET_METHOD_ENUM)
    status: Mapped[TicketStatus] = mapped_column(TICKET_STATUS_ENUM)
    due_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TicketItemModel(AssetPersistenceBase):
    """Mapping for one source-Custody Ticket item."""

    __tablename__ = "ticket_items"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("borrow_tickets.id"))
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    source_custody_id: Mapped[UUID] = mapped_column(ForeignKey("custodies.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TicketEvidenceModel(AssetPersistenceBase):
    """Mapping for private Ticket evidence metadata."""

    __tablename__ = "ticket_evidence"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("borrow_tickets.id"))
    uploaded_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    evidence_type: Mapped[str] = mapped_column(String(32))
    object_key: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(32))
    content_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TicketEventModel(AssetPersistenceBase):
    """Mapping for immutable Ticket transition history."""

    __tablename__ = "ticket_events"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("borrow_tickets.id"))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[TicketStatus | None] = mapped_column(TICKET_STATUS_ENUM)
    to_status: Mapped[TicketStatus] = mapped_column(TICKET_STATUS_ENUM)
    note: Mapped[str | None] = mapped_column(String(1000))
    event_metadata: Mapped[dict[str, object]] = mapped_column("metadata", JSON)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SqlAlchemyTicketRepository(ITicketRepository):
    """Persist Ticket aggregates and immutable evidence/event children."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, ticket: BorrowTicket, *, created_at: datetime) -> None:
        """Stage one Ticket and all normalized items without committing."""

        self._session.add(
            BorrowTicketModel(
                id=ticket.id,
                borrower_user_id=ticket.borrower_user_id,
                source_custodian_user_id=ticket.source_custodian_user_id,
                method=ticket.method,
                status=ticket.status,
                due_date=ticket.due_date,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        for item in ticket.items:
            self._session.add(
                TicketItemModel(
                    id=uuid5(ticket.id, str(item.source_custody_id)),
                    ticket_id=ticket.id,
                    asset_id=item.asset_id,
                    source_custody_id=item.source_custody_id,
                    quantity=item.quantity,
                    created_at=created_at,
                )
            )

    async def get_for_update(self, ticket_id: UUID) -> BorrowTicket | None:
        """Lock and rehydrate one Ticket, its items, and evidence references."""

        result = await self._execute(
            select(BorrowTicketModel).where(BorrowTicketModel.id == ticket_id).with_for_update()
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None
        item_result = await self._execute(
            select(TicketItemModel)
            .where(TicketItemModel.ticket_id == ticket_id)
            .order_by(TicketItemModel.source_custody_id)
        )
        evidence_result = await self._execute(
            select(TicketEvidenceModel.id, TicketEvidenceModel.evidence_type)
            .where(TicketEvidenceModel.ticket_id == ticket_id)
            .order_by(TicketEvidenceModel.created_at, TicketEvidenceModel.id)
        )
        evidence: dict[str, list[UUID]] = {}
        for evidence_id, evidence_type in evidence_result.all():
            evidence.setdefault(evidence_type, []).append(evidence_id)
        return BorrowTicket(
            id=model.id,
            borrower_user_id=model.borrower_user_id,
            source_custodian_user_id=model.source_custodian_user_id,
            method=model.method,
            status=model.status,
            due_date=model.due_date,
            items=tuple(
                TicketItem(row.asset_id, row.source_custody_id, row.quantity)
                for row in item_result.scalars()
            ),
            pickup_evidence_ids=tuple(evidence.get("PICKUP", [])),
            borrower_return_evidence_ids=tuple(evidence.get("BORROWER_RETURN", [])),
            custodian_return_evidence_ids=tuple(evidence.get("CUSTODIAN_RETURN", [])),
        )

    async def save(self, ticket: BorrowTicket, *, updated_at: datetime) -> None:
        """Stage current Ticket status without committing."""

        await self._execute(
            update(BorrowTicketModel)
            .where(BorrowTicketModel.id == ticket.id)
            .values(status=ticket.status, updated_at=updated_at)
        )

    async def add_event(self, event: TicketEvent) -> None:
        """Stage one immutable Ticket event."""

        self._session.add(
            TicketEventModel(
                id=event.id,
                ticket_id=event.ticket_id,
                actor_user_id=event.actor_user_id,
                event_type=event.event_type,
                from_status=event.from_status,
                to_status=event.to_status,
                note=event.note,
                event_metadata={},
                occurred_at=event.occurred_at,
            )
        )

    async def add_evidence(self, evidence: TicketEvidence) -> None:
        """Stage one immutable private evidence row."""

        self._session.add(
            TicketEvidenceModel(
                id=evidence.id,
                ticket_id=evidence.ticket_id,
                uploaded_by_user_id=evidence.uploaded_by_user_id,
                evidence_type=evidence.evidence_type,
                object_key=evidence.object_key,
                content_type=evidence.content_type,
                content_sha256=evidence.content_sha256,
                size_bytes=evidence.size_bytes,
                note=evidence.note,
                created_at=evidence.created_at,
            )
        )

    async def _execute(self, statement: Executable) -> Result[Any]:
        try:
            return await self._session.execute(statement)
        except SQLAlchemyError as exception:
            raise TicketPersistenceError("Ticket persistence operation failed.") from exception
