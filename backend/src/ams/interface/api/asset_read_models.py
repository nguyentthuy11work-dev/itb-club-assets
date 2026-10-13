"""Strict HTTP response models for Asset and Custody queries."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from ams.application.asset_queries import AssetBrowsePage, HeldAssetPage
from ams.domain.asset_views import (
    AssetBrowseView,
    AssetCustodyView,
    AssetDetailView,
    EligibleCustodyView,
    HeldAssetView,
)
from ams.domain.assets import AssetCondition, CustodyStatus
from ams.interface.api.models import StrictApiModel


class HeldAssetResponse(StrictApiModel):
    """One pending or active Custody owned by the current actor."""

    custody_id: UUID = Field(serialization_alias="custodyId")
    asset_id: UUID = Field(serialization_alias="assetId")
    name: str
    status: CustodyStatus
    on_hand_quantity: int = Field(serialization_alias="onHandQuantity")
    reserved_quantity: int = Field(serialization_alias="reservedQuantity")
    temporarily_loaned_quantity: int = Field(serialization_alias="temporarilyLoanedQuantity")
    lendable_quantity: int = Field(serialization_alias="lendableQuantity")
    condition: AssetCondition
    assigned_at: datetime = Field(serialization_alias="assignedAt")
    confirmed_at: datetime | None = Field(serialization_alias="confirmedAt")

    @classmethod
    def from_view(cls, view: HeldAssetView) -> HeldAssetResponse:
        """Map a framework-independent held-Asset projection."""

        return cls(
            custody_id=view.custody_id,
            asset_id=view.asset_id,
            name=view.asset_name,
            status=view.status,
            on_hand_quantity=view.on_hand_quantity,
            reserved_quantity=view.reserved_quantity,
            temporarily_loaned_quantity=view.temporarily_loaned_quantity,
            lendable_quantity=view.lendable_quantity,
            condition=view.condition,
            assigned_at=view.assigned_at,
            confirmed_at=view.confirmed_at,
        )


class HeldAssetPageResponse(StrictApiModel):
    """Canonical paginated response for the current actor's Custodies."""

    items: list[HeldAssetResponse]
    page: int
    page_size: int = Field(serialization_alias="pageSize")
    total: int

    @classmethod
    def from_page(cls, result: HeldAssetPage) -> HeldAssetPageResponse:
        """Map the application page to the shared API envelope."""

        return cls(
            items=[HeldAssetResponse.from_view(item) for item in result.items],
            page=result.page,
            page_size=result.page_size,
            total=result.total,
        )


class EligibleCustodyResponse(StrictApiModel):
    """Borrow-eligible source Custody without private evidence metadata."""

    source_custody_id: UUID = Field(serialization_alias="sourceCustodyId")
    lendable_quantity: int = Field(serialization_alias="lendableQuantity")

    @classmethod
    def from_view(cls, view: EligibleCustodyView) -> EligibleCustodyResponse:
        """Map one eligible source Custody projection."""

        return cls(
            source_custody_id=view.source_custody_id,
            lendable_quantity=view.lendable_quantity,
        )


class AssetBrowseResponse(StrictApiModel):
    """One public Asset catalog row."""

    id: UUID
    category_id: UUID = Field(serialization_alias="categoryId")
    responsible_team_id: UUID | None = Field(serialization_alias="responsibleTeamId")
    name: str
    description: str | None
    source: str
    total_quantity: int = Field(serialization_alias="totalQuantity")
    available_quantity: int = Field(serialization_alias="availableQuantity")
    eligible_custodies: list[EligibleCustodyResponse] = Field(
        serialization_alias="eligibleCustodies"
    )

    @classmethod
    def from_view(cls, view: AssetBrowseView) -> AssetBrowseResponse:
        """Map an Asset catalog projection without persistence metadata."""

        return cls(
            id=view.id,
            category_id=view.category_id,
            responsible_team_id=view.responsible_team_id,
            name=view.name,
            description=view.description,
            source=view.source,
            total_quantity=view.total_quantity,
            available_quantity=view.available_quantity,
            eligible_custodies=[
                EligibleCustodyResponse.from_view(item) for item in view.eligible_custodies
            ],
        )


class AssetBrowsePageResponse(StrictApiModel):
    """Canonical paginated response for the filtered Asset catalog."""

    items: list[AssetBrowseResponse]
    page: int
    page_size: int = Field(serialization_alias="pageSize")
    total: int

    @classmethod
    def from_page(cls, result: AssetBrowsePage) -> AssetBrowsePageResponse:
        """Map the application result to the shared page envelope."""

        return cls(
            items=[AssetBrowseResponse.from_view(item) for item in result.items],
            page=result.page,
            page_size=result.page_size,
            total=result.total,
        )


class AssetCustodyResponse(StrictApiModel):
    """Current physical-Custody summary safe for Asset detail."""

    custody_id: UUID = Field(serialization_alias="custodyId")
    custodian_user_id: UUID = Field(serialization_alias="custodianUserId")
    status: CustodyStatus
    on_hand_quantity: int = Field(serialization_alias="onHandQuantity")
    reserved_quantity: int = Field(serialization_alias="reservedQuantity")
    temporarily_loaned_quantity: int = Field(serialization_alias="temporarilyLoanedQuantity")
    lendable_quantity: int = Field(serialization_alias="lendableQuantity")
    condition: AssetCondition
    condition_note: str | None = Field(serialization_alias="conditionNote")
    assigned_at: datetime = Field(serialization_alias="assignedAt")
    confirmed_at: datetime | None = Field(serialization_alias="confirmedAt")
    evidence_count: int = Field(serialization_alias="evidenceCount")

    @classmethod
    def from_view(cls, view: AssetCustodyView) -> AssetCustodyResponse:
        """Map a Custody view without exposing evidence object metadata."""

        return cls(
            custody_id=view.custody_id,
            custodian_user_id=view.custodian_user_id,
            status=view.status,
            on_hand_quantity=view.on_hand_quantity,
            reserved_quantity=view.reserved_quantity,
            temporarily_loaned_quantity=view.temporarily_loaned_quantity,
            lendable_quantity=view.lendable_quantity,
            condition=view.condition,
            condition_note=view.condition_note,
            assigned_at=view.assigned_at,
            confirmed_at=view.confirmed_at,
            evidence_count=view.evidence_count,
        )


class AssetDetailResponse(StrictApiModel):
    """Asset detail with pending and active Custody UUIDs and quantities."""

    id: UUID
    category_id: UUID = Field(serialization_alias="categoryId")
    responsible_team_id: UUID | None = Field(serialization_alias="responsibleTeamId")
    name: str
    description: str | None
    source: str
    total_quantity: int = Field(serialization_alias="totalQuantity")
    available_quantity: int = Field(serialization_alias="availableQuantity")
    custodies: list[AssetCustodyResponse]

    @classmethod
    def from_view(cls, view: AssetDetailView) -> AssetDetailResponse:
        """Map Asset detail while retaining only safe evidence counts."""

        return cls(
            id=view.id,
            category_id=view.category_id,
            responsible_team_id=view.responsible_team_id,
            name=view.name,
            description=view.description,
            source=view.source,
            total_quantity=view.total_quantity,
            available_quantity=view.available_quantity,
            custodies=[AssetCustodyResponse.from_view(item) for item in view.custodies],
        )
