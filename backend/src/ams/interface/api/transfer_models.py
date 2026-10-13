"""Strict transport contracts for standalone Custody Transfers."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from ams.application.transfers import (
    ConfirmTransferMetadata,
    CreateTransferCommand,
    TransferPage,
    TransferResult,
)
from ams.domain.assets import AssetCondition
from ams.domain.transfers import CustodyTransfer, TransferStatus
from ams.interface.api.models import StrictApiModel


class CreateTransferRequest(StrictApiModel):
    """HR request for one source-Custody quantity transfer."""

    source_custody_id: UUID = Field(alias="sourceCustodyId", strict=False)
    target_user_id: UUID = Field(alias="targetUserId", strict=False)
    quantity: int = Field(gt=0)
    reconciliation_item_id: UUID | None = Field(
        default=None, alias="reconciliationItemId", strict=False
    )

    def to_command(self) -> CreateTransferCommand:
        """Map strict transport values to the application command."""

        return CreateTransferCommand(
            self.source_custody_id,
            self.target_user_id,
            self.quantity,
            self.reconciliation_item_id,
        )


class CreateLinkedTransferRequest(StrictApiModel):
    """HR request whose Reconciliation item comes from the route."""

    source_custody_id: UUID = Field(alias="sourceCustodyId", strict=False)
    target_user_id: UUID = Field(alias="targetUserId", strict=False)
    quantity: int = Field(gt=0)

    def to_command(self, item_id: UUID) -> CreateTransferCommand:
        """Map the route-owned item link into the application command."""

        return CreateTransferCommand(
            self.source_custody_id,
            self.target_user_id,
            self.quantity,
            item_id,
        )


class ConfirmTransferMetadataRequest(StrictApiModel):
    """JSON metadata part accompanying one side's evidence."""

    note: str | None = Field(default=None, max_length=1000)

    def to_command(self) -> ConfirmTransferMetadata:
        """Map validated metadata into the application value."""

        return ConfirmTransferMetadata(self.note)


class TransferResponse(StrictApiModel):
    """Public Transfer state without private evidence metadata."""

    id: UUID
    source_custody_id: UUID = Field(serialization_alias="sourceCustodyId")
    asset_id: UUID = Field(serialization_alias="assetId")
    current_custodian_user_id: UUID = Field(serialization_alias="currentCustodianUserId")
    target_custodian_user_id: UUID = Field(serialization_alias="targetCustodianUserId")
    quantity: int
    condition_snapshot: AssetCondition = Field(serialization_alias="conditionSnapshot")
    created_by_user_id: UUID = Field(serialization_alias="createdByUserId")
    created_at: datetime = Field(serialization_alias="createdAt")
    reconciliation_item_id: UUID | None = Field(serialization_alias="reconciliationItemId")
    status: TransferStatus
    current_confirmed_at: datetime | None = Field(serialization_alias="currentConfirmedAt")
    target_confirmed_at: datetime | None = Field(serialization_alias="targetConfirmedAt")
    target_custody_id: UUID | None = Field(serialization_alias="targetCustodyId")
    completed_at: datetime | None = Field(serialization_alias="completedAt")

    @classmethod
    def from_transfer(cls, transfer: CustodyTransfer) -> TransferResponse:
        """Map Domain state without exposing object keys or hashes."""

        return cls(
            id=transfer.id,
            source_custody_id=transfer.source_custody_id,
            asset_id=transfer.asset_id,
            current_custodian_user_id=transfer.current_custodian_user_id,
            target_custodian_user_id=transfer.target_custodian_user_id,
            quantity=transfer.quantity,
            condition_snapshot=transfer.condition_snapshot,
            created_by_user_id=transfer.created_by_user_id,
            created_at=transfer.created_at,
            reconciliation_item_id=transfer.reconciliation_item_id,
            status=transfer.status,
            current_confirmed_at=transfer.current_confirmed_at,
            target_confirmed_at=transfer.target_confirmed_at,
            target_custody_id=transfer.target_custody_id,
            completed_at=transfer.completed_at,
        )

    @classmethod
    def from_result(cls, result: TransferResult) -> TransferResponse:
        """Map one application command result."""

        return cls.from_transfer(result.transfer)


class TransferPageResponse(StrictApiModel):
    """Canonical ownership-scoped Transfer page."""

    items: list[TransferResponse]
    page: int
    page_size: int = Field(serialization_alias="pageSize")
    total: int

    @classmethod
    def from_result(cls, result: TransferPage) -> TransferPageResponse:
        """Map offset paging into the canonical page contract."""

        return cls(
            items=[TransferResponse.from_transfer(item) for item in result.items],
            page=(result.offset // result.limit) + 1,
            page_size=result.limit,
            total=result.total,
        )
