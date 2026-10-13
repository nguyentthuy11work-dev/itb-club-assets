"""Strict transport contracts for term Handover snapshots."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import Field

from ams.application.handovers import HandoverResult
from ams.domain.assets import AssetCondition, CustodyStatus
from ams.domain.handovers import Handover, HandoverStatus
from ams.interface.api.models import StrictApiModel


class CreateHandoverRequest(StrictApiModel):
    """Select one preconfigured term for immutable snapshot creation."""

    term_id: UUID = Field(alias="termId", strict=False)


class HandoverCustodyResponse(StrictApiModel):
    """Current Custody data frozen under one Asset item."""

    custody_id: UUID = Field(serialization_alias="custodyId")
    custodian_user_id: UUID = Field(serialization_alias="custodianUserId")
    status: CustodyStatus
    on_hand_quantity: int = Field(serialization_alias="onHandQuantity")
    reserved_quantity: int = Field(serialization_alias="reservedQuantity")
    temporarily_loaned_quantity: int = Field(serialization_alias="temporarilyLoanedQuantity")
    condition: AssetCondition
    condition_note: str | None = Field(serialization_alias="conditionNote")


class HandoverItemResponse(StrictApiModel):
    """One immutable Asset snapshot with stale-activity result."""

    id: UUID
    handover_id: UUID = Field(serialization_alias="handoverId")
    asset_id: UUID = Field(serialization_alias="assetId")
    asset_name: str = Field(serialization_alias="assetName")
    total_quantity: int = Field(serialization_alias="totalQuantity")
    available_quantity: int = Field(serialization_alias="availableQuantity")
    condition: AssetCondition
    custodies: list[HandoverCustodyResponse]
    is_stale: bool = Field(serialization_alias="isStale")
    last_qualifying_transaction_at: datetime | None = Field(
        serialization_alias="lastQualifyingTransactionAt"
    )
    snapshot_at: datetime = Field(serialization_alias="snapshotAt")


class HandoverResponse(StrictApiModel):
    """Complete term snapshot and independent confirmation timestamps."""

    id: UUID
    term_id: UUID = Field(serialization_alias="termId")
    term_code: str = Field(serialization_alias="termCode")
    term_name: str = Field(serialization_alias="termName")
    term_starts_on: date = Field(serialization_alias="termStartsOn")
    term_ends_on: date = Field(serialization_alias="termEndsOn")
    old_hr_user_id: UUID = Field(serialization_alias="oldHrUserId")
    new_hr_user_id: UUID = Field(serialization_alias="newHrUserId")
    created_by_user_id: UUID = Field(serialization_alias="createdByUserId")
    created_at: datetime = Field(serialization_alias="createdAt")
    status: HandoverStatus
    old_confirmed_at: datetime | None = Field(serialization_alias="oldConfirmedAt")
    new_confirmed_at: datetime | None = Field(serialization_alias="newConfirmedAt")
    locked_at: datetime | None = Field(serialization_alias="lockedAt")
    items: list[HandoverItemResponse]

    @classmethod
    def from_result(cls, result: HandoverResult) -> HandoverResponse:
        """Map Domain snapshots without exposing private evidence or ORM state."""

        return cls.from_handover(result.handover)

    @classmethod
    def from_handover(cls, handover: Handover) -> HandoverResponse:
        """Map one complete Handover aggregate."""

        return cls(
            id=handover.id,
            term_id=handover.term_id,
            term_code=handover.term_code,
            term_name=handover.term_name,
            term_starts_on=handover.term_starts_on,
            term_ends_on=handover.term_ends_on,
            old_hr_user_id=handover.old_hr_user_id,
            new_hr_user_id=handover.new_hr_user_id,
            created_by_user_id=handover.created_by_user_id,
            created_at=handover.created_at,
            status=handover.status,
            old_confirmed_at=handover.old_confirmed_at,
            new_confirmed_at=handover.new_confirmed_at,
            locked_at=handover.locked_at,
            items=[
                HandoverItemResponse(
                    id=item.id,
                    handover_id=item.handover_id,
                    asset_id=item.asset_id,
                    asset_name=item.asset_name,
                    total_quantity=item.total_quantity,
                    available_quantity=item.available_quantity,
                    condition=item.condition,
                    custodies=[
                        HandoverCustodyResponse(
                            custody_id=value.custody_id,
                            custodian_user_id=value.custodian_user_id,
                            status=value.status,
                            on_hand_quantity=value.on_hand_quantity,
                            reserved_quantity=value.reserved_quantity,
                            temporarily_loaned_quantity=value.temporarily_loaned_quantity,
                            condition=value.condition,
                            condition_note=value.condition_note,
                        )
                        for value in item.custodies
                    ],
                    is_stale=item.is_stale,
                    last_qualifying_transaction_at=item.last_qualifying_transaction_at,
                    snapshot_at=item.snapshot_at,
                )
                for item in handover.items
            ],
        )
