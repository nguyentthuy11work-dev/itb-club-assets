"""Strict transport contracts for F09 reconciliation workflows."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import Field

from ams.application.reconciliations import (
    ConfirmationRequestResult,
    ConfirmReconciliationMetadata,
    ReconciliationItemPage,
    ReconciliationItemResult,
    ReconciliationResult,
    ReminderResult,
)
from ams.domain.assets import AssetCondition
from ams.domain.reconciliations import (
    ReconciliationItem,
    ReconciliationItemStatus,
    ReconciliationSession,
    ReconciliationStatus,
)
from ams.interface.api.models import StrictApiModel


class OpenReconciliationRequest(StrictApiModel):
    """Selected club-local business date for a new session."""

    reconciliation_date: date = Field(alias="reconciliationDate", strict=False)


class ConfirmReconciliationMetadataRequest(StrictApiModel):
    """JSON metadata part submitted alongside evidence images."""

    actual_quantity: int = Field(alias="actualQuantity", ge=0)
    condition: AssetCondition
    condition_note: str | None = Field(default=None, alias="conditionNote", max_length=1000)
    discrepancy_reason: str | None = Field(
        default=None, alias="discrepancyReason", max_length=1000
    )

    def to_command(self) -> ConfirmReconciliationMetadata:
        """Map strict transport values into the application command."""

        return ConfirmReconciliationMetadata(
            self.actual_quantity,
            self.condition,
            self.condition_note,
            self.discrepancy_reason,
        )


class ResolveReconciliationItemRequest(StrictApiModel):
    """Required HR resolution explanation."""

    resolution_note: str = Field(
        alias="resolutionNote", min_length=1, max_length=1000, pattern=r".*\S.*"
    )


class ReconciliationItemResponse(StrictApiModel):
    """Expected snapshot and optional confirmation/resolution state."""

    id: UUID
    reconciliation_id: UUID = Field(serialization_alias="reconciliationId")
    custody_id: UUID = Field(serialization_alias="custodyId")
    asset_id: UUID = Field(serialization_alias="assetId")
    custodian_user_id: UUID = Field(serialization_alias="custodianUserId")
    asset_name: str = Field(serialization_alias="assetName")
    expected_quantity: int = Field(serialization_alias="expectedQuantity")
    expected_condition: AssetCondition = Field(serialization_alias="expectedCondition")
    snapshot_at: datetime = Field(serialization_alias="snapshotAt")
    status: ReconciliationItemStatus
    actual_quantity: int | None = Field(serialization_alias="actualQuantity")
    actual_condition: AssetCondition | None = Field(serialization_alias="actualCondition")
    condition_note: str | None = Field(serialization_alias="conditionNote")
    discrepancy_reason: str | None = Field(serialization_alias="discrepancyReason")
    confirmed_at: datetime | None = Field(serialization_alias="confirmedAt")
    resolution_note: str | None = Field(serialization_alias="resolutionNote")
    resolved_at: datetime | None = Field(serialization_alias="resolvedAt")
    last_reminded_at: datetime | None = Field(serialization_alias="lastRemindedAt")

    @classmethod
    def from_item(cls, item: ReconciliationItem) -> ReconciliationItemResponse:
        """Map Domain state without exposing private evidence metadata."""

        return cls(
            id=item.id,
            reconciliation_id=item.reconciliation_id,
            custody_id=item.custody_id,
            asset_id=item.asset_id,
            custodian_user_id=item.custodian_user_id,
            asset_name=item.asset_name,
            expected_quantity=item.expected_quantity,
            expected_condition=item.expected_condition,
            snapshot_at=item.snapshot_at,
            status=item.status,
            actual_quantity=item.actual_quantity,
            actual_condition=item.actual_condition,
            condition_note=item.condition_note,
            discrepancy_reason=item.discrepancy_reason,
            confirmed_at=item.confirmed_at,
            resolution_note=item.resolution_note,
            resolved_at=item.resolved_at,
            last_reminded_at=item.last_reminded_at,
        )

    @classmethod
    def from_result(cls, result: ReconciliationItemResult) -> ReconciliationItemResponse:
        """Map one item command result."""

        return cls.from_item(result.item)


class ReconciliationResponse(StrictApiModel):
    """Complete immutable session snapshot and mutable workflow state."""

    id: UUID
    reconciliation_date: date = Field(serialization_alias="reconciliationDate")
    status: ReconciliationStatus
    opened_by_user_id: UUID = Field(serialization_alias="openedByUserId")
    opened_at: datetime = Field(serialization_alias="openedAt")
    temporary_loan_ticket_count: int = Field(serialization_alias="temporaryLoanTicketCount")
    confirmation_requested_at: datetime | None = Field(
        serialization_alias="confirmationRequestedAt"
    )
    closed_by_user_id: UUID | None = Field(serialization_alias="closedByUserId")
    closed_at: datetime | None = Field(serialization_alias="closedAt")
    items: list[ReconciliationItemResponse]

    @classmethod
    def from_session(cls, session: ReconciliationSession) -> ReconciliationResponse:
        """Map one session without persistence details."""

        return cls(
            id=session.id,
            reconciliation_date=session.reconciliation_date,
            status=session.status,
            opened_by_user_id=session.opened_by_user_id,
            opened_at=session.opened_at,
            temporary_loan_ticket_count=session.temporary_loan_ticket_count,
            confirmation_requested_at=session.confirmation_requested_at,
            closed_by_user_id=session.closed_by_user_id,
            closed_at=session.closed_at,
            items=[ReconciliationItemResponse.from_item(item) for item in session.items],
        )

    @classmethod
    def from_result(cls, result: ReconciliationResult) -> ReconciliationResponse:
        """Map a session command/read result."""

        return cls.from_session(result.session)


class ReconciliationItemPageResponse(StrictApiModel):
    """Paginated current-custodian work queue."""

    items: list[ReconciliationItemResponse]
    page: int
    page_size: int = Field(serialization_alias="pageSize")
    total: int

    @classmethod
    def from_result(cls, result: ReconciliationItemPage) -> ReconciliationItemPageResponse:
        """Map offset paging into the canonical public page shape."""

        return cls(
            items=[ReconciliationItemResponse.from_item(item) for item in result.items],
            page=(result.offset // result.limit) + 1,
            page_size=result.limit,
            total=result.total,
        )


class ConfirmationRequestResponse(StrictApiModel):
    """Queued request summary."""

    id: UUID
    requested_at: datetime = Field(serialization_alias="requestedAt")
    email_recipients: int = Field(serialization_alias="emailRecipients")

    @classmethod
    def from_result(cls, result: ConfirmationRequestResult) -> ConfirmationRequestResponse:
        """Map a request-confirmation result."""

        return cls(
            id=result.reconciliation_id,
            requested_at=result.requested_at,
            email_recipients=result.email_recipients,
        )


class ReminderResponse(StrictApiModel):
    """Accepted item-reminder response."""

    id: UUID
    email_queued: bool = Field(serialization_alias="emailQueued")

    @classmethod
    def from_result(cls, result: ReminderResult) -> ReminderResponse:
        """Map a reminder result."""

        return cls(id=result.item_id, email_queued=result.email_queued)
