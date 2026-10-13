"""Strict request and response contracts for the Borrow Ticket lifecycle."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from ams.application.ticket_commands import TicketActionResult
from ams.application.ticket_queries import TicketPage
from ams.application.tickets import (
    CreateTicketAllocationCommand,
    CreateTicketsCommand,
    CreateTicketsResult,
)
from ams.domain.ticket_views import TicketDetailView, TicketListItemView
from ams.domain.tickets import TicketMethod, TicketStatus
from ams.interface.api.models import StrictApiModel


class TicketAllocationRequest(StrictApiModel):
    """One allocation against an explicit server-issued source Custody ID."""

    source_custody_id: UUID = Field(alias="sourceCustodyId", strict=False)
    quantity: int = Field(gt=0)
    method: TicketMethod = Field(strict=False)
    due_date: date | None = Field(default=None, alias="dueDate", strict=False)

    @model_validator(mode="after")
    def validate_due_date_policy(self) -> Self:
        """Require a due date only for temporary borrowing."""

        if self.method is TicketMethod.TEMPORARY and self.due_date is None:
            raise ValueError("Temporary Ticket allocations require dueDate.")
        if self.method is TicketMethod.CUSTODY_TRANSFER and self.due_date is not None:
            raise ValueError("Custody transfer allocations forbid dueDate.")
        return self


class TicketAssetRequest(StrictApiModel):
    """Allocation rows selected for one Asset."""

    asset_id: UUID = Field(alias="assetId", strict=False)
    allocations: list[TicketAllocationRequest] = Field(min_length=1)


class CreateTicketsRequest(StrictApiModel):
    """Nested Asset selections submitted for deterministic server-side grouping."""

    items: list[TicketAssetRequest] = Field(min_length=1)

    def to_command(self) -> CreateTicketsCommand:
        """Flatten transport grouping into framework-independent allocations."""

        return CreateTicketsCommand(
            allocations=tuple(
                CreateTicketAllocationCommand(
                    asset_id=item.asset_id,
                    source_custody_id=allocation.source_custody_id,
                    quantity=allocation.quantity,
                    method=allocation.method,
                    due_date=allocation.due_date,
                )
                for item in self.items
                for allocation in item.allocations
            )
        )


class CreatedTicketResponse(StrictApiModel):
    """Identity and initial state of one successfully created Ticket group."""

    id: UUID
    status: Literal["PENDING"] = "PENDING"


class RejectedTicketGroupResponse(StrictApiModel):
    """Source rows and stable reason for one rejected creation group."""

    source_custody_ids: list[UUID] = Field(serialization_alias="sourceCustodyIds")
    reason_code: str = Field(serialization_alias="reasonCode")


class CreateTicketsResponse(StrictApiModel):
    """Partial-success result containing every created and rejected group."""

    created_tickets: list[CreatedTicketResponse] = Field(serialization_alias="createdTickets")
    rejected_groups: list[RejectedTicketGroupResponse] = Field(
        serialization_alias="rejectedGroups"
    )

    @classmethod
    def from_result(cls, result: CreateTicketsResult) -> CreateTicketsResponse:
        """Map an application result without exposing replay internals."""

        return cls(
            created_tickets=[
                CreatedTicketResponse(id=item.id) for item in result.created_tickets
            ],
            rejected_groups=[
                RejectedTicketGroupResponse(
                    source_custody_ids=list(item.source_custody_ids),
                    reason_code=item.reason_code,
                )
                for item in result.rejected_groups
            ],
        )


class TicketReasonRequest(StrictApiModel):
    """Required non-blank reason for reject and cancellation commands."""

    reason: str = Field(min_length=1, max_length=1000, pattern=r".*\S.*")


class ResolveCancellationRequest(StrictApiModel):
    """Custodian decision for an approved-stage cancellation request."""

    approve: bool


class TicketEvidenceMetadataRequest(StrictApiModel):
    """Optional note included as the JSON part of multipart actions."""

    note: str | None = Field(default=None, max_length=1000)


class TicketActionResponse(StrictApiModel):
    """Ticket identity and state after a successful explicit transition."""

    ticket_id: UUID = Field(serialization_alias="ticketId")
    status: TicketStatus

    @classmethod
    def from_result(cls, result: TicketActionResult) -> TicketActionResponse:
        """Map a lifecycle result without exposing replay internals."""

        return cls(ticket_id=result.ticket_id, status=result.status)


class TicketListItemResponse(StrictApiModel):
    """Ownership-safe Ticket list row."""

    id: UUID
    borrower_user_id: UUID = Field(serialization_alias="borrowerUserId")
    borrower_name: str = Field(serialization_alias="borrowerName")
    source_custodian_user_id: UUID = Field(serialization_alias="sourceCustodianUserId")
    source_custodian_name: str = Field(serialization_alias="sourceCustodianName")
    method: TicketMethod
    status: TicketStatus
    due_date: date | None = Field(serialization_alias="dueDate")
    total_quantity: int = Field(serialization_alias="totalQuantity")
    asset_names: list[str] = Field(serialization_alias="assetNames")
    created_at: datetime = Field(serialization_alias="createdAt")
    updated_at: datetime = Field(serialization_alias="updatedAt")

    @classmethod
    def from_view(cls, view: TicketListItemView) -> TicketListItemResponse:
        """Map a list projection to its public shape."""

        return cls(
            id=view.id,
            borrower_user_id=view.borrower_user_id,
            borrower_name=view.borrower_name,
            source_custodian_user_id=view.source_custodian_user_id,
            source_custodian_name=view.source_custodian_name,
            method=view.method,
            status=view.status,
            due_date=view.due_date,
            total_quantity=view.total_quantity,
            asset_names=list(view.asset_names),
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class TicketPageResponse(StrictApiModel):
    """Shared pagination envelope for Ticket lists."""

    items: list[TicketListItemResponse]
    total: int
    offset: int
    limit: int

    @classmethod
    def from_page(cls, page: TicketPage) -> TicketPageResponse:
        """Map a Ticket page into the transport envelope."""

        return cls(
            items=[TicketListItemResponse.from_view(item) for item in page.items],
            total=page.total,
            offset=page.offset,
            limit=page.limit,
        )


class TicketItemResponse(StrictApiModel):
    """One normalized Ticket item."""

    id: UUID
    asset_id: UUID = Field(serialization_alias="assetId")
    asset_name: str = Field(serialization_alias="assetName")
    source_custody_id: UUID = Field(serialization_alias="sourceCustodyId")
    quantity: int


class TicketEvidenceResponse(StrictApiModel):
    """Permitted evidence metadata without a private storage key."""

    id: UUID
    uploaded_by_user_id: UUID = Field(serialization_alias="uploadedByUserId")
    evidence_type: str = Field(serialization_alias="evidenceType")
    content_type: str = Field(serialization_alias="contentType")
    size_bytes: int = Field(serialization_alias="sizeBytes")
    note: str | None
    created_at: datetime = Field(serialization_alias="createdAt")


class TicketEventResponse(StrictApiModel):
    """Actor-attributed immutable Ticket transition history."""

    id: UUID
    actor_user_id: UUID = Field(serialization_alias="actorUserId")
    actor_name: str = Field(serialization_alias="actorName")
    event_type: str = Field(serialization_alias="eventType")
    from_status: TicketStatus | None = Field(serialization_alias="fromStatus")
    to_status: TicketStatus = Field(serialization_alias="toStatus")
    note: str | None
    occurred_at: datetime = Field(serialization_alias="occurredAt")


class TicketDetailResponse(StrictApiModel):
    """Ticket summary, normalized items, safe evidence, and immutable history."""

    ticket: TicketListItemResponse
    items: list[TicketItemResponse]
    evidence: list[TicketEvidenceResponse]
    events: list[TicketEventResponse]

    @classmethod
    def from_view(cls, view: TicketDetailView) -> TicketDetailResponse:
        """Map a complete detail projection without exposing storage keys."""

        return cls(
            ticket=TicketListItemResponse.from_view(view.summary),
            items=[
                TicketItemResponse(
                    id=item.id,
                    asset_id=item.asset_id,
                    asset_name=item.asset_name,
                    source_custody_id=item.source_custody_id,
                    quantity=item.quantity,
                )
                for item in view.items
            ],
            evidence=[
                TicketEvidenceResponse(
                    id=item.id,
                    uploaded_by_user_id=item.uploaded_by_user_id,
                    evidence_type=item.evidence_type,
                    content_type=item.content_type,
                    size_bytes=item.size_bytes,
                    note=item.note,
                    created_at=item.created_at,
                )
                for item in view.evidence
            ],
            events=[
                TicketEventResponse(
                    id=item.id,
                    actor_user_id=item.actor_user_id,
                    actor_name=item.actor_name,
                    event_type=item.event_type,
                    from_status=item.from_status,
                    to_status=item.to_status,
                    note=item.note,
                    occurred_at=item.occurred_at,
                )
                for item in view.events
            ],
        )
