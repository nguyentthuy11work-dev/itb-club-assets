"""Strict API contracts for HR Asset registration and classification."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field

from ams.application.asset_creation import (
    CreateAssetAllocationCommand,
    CreateAssetCommand,
    CreateAssetResult,
)
from ams.application.responsible_team import ResponsibleTeamUpdateResult
from ams.domain.assets import AssetCondition
from ams.interface.api.models import StrictApiModel


class CustodyAllocationRequest(StrictApiModel):
    """One requested initial pending Custody allocation."""

    custodian_user_id: UUID = Field(alias="custodianUserId", strict=False)
    quantity: int = Field(gt=0)
    condition: AssetCondition = Field(default=AssetCondition.GOOD, strict=False)
    condition_note: str | None = Field(default=None, alias="conditionNote", max_length=1000)

    def to_command(self) -> CreateAssetAllocationCommand:
        """Map validated transport input to an application command value."""

        return CreateAssetAllocationCommand(
            custodian_user_id=self.custodian_user_id,
            quantity=self.quantity,
            condition=self.condition,
            condition_note=self.condition_note,
        )


class CreateAssetRequest(StrictApiModel):
    """HR request for one Asset and its initial physical allocations."""

    category_id: UUID = Field(alias="categoryId", strict=False)
    responsible_team_id: UUID | None = Field(
        default=None, alias="responsibleTeamId", strict=False
    )
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    source: str = Field(min_length=1, max_length=255)
    total_quantity: int = Field(alias="totalQuantity", gt=0)
    allocations: list[CustodyAllocationRequest] = Field(min_length=1)

    def to_command(self) -> CreateAssetCommand:
        """Map strict JSON input into the framework-independent command."""

        return CreateAssetCommand(
            category_id=self.category_id,
            responsible_team_id=self.responsible_team_id,
            name=self.name,
            description=self.description,
            source=self.source,
            total_quantity=self.total_quantity,
            allocations=tuple(item.to_command() for item in self.allocations),
        )


class CreatedCustodyResponse(StrictApiModel):
    """One pending Custody created by Asset registration."""

    id: UUID
    custodian_user_id: UUID = Field(serialization_alias="custodianUserId")
    quantity: int
    status: Literal["PENDING"] = "PENDING"


class CreateAssetResponse(StrictApiModel):
    """Created Asset identity and all pending physical allocations."""

    asset_id: UUID = Field(serialization_alias="assetId")
    custodies: list[CreatedCustodyResponse]

    @classmethod
    def from_result(cls, result: CreateAssetResult) -> CreateAssetResponse:
        """Map the application output without exposing idempotency internals."""

        return cls(
            asset_id=result.asset_id,
            custodies=[
                CreatedCustodyResponse(
                    id=custody.id,
                    custodian_user_id=custody.custodian_user_id,
                    quantity=custody.quantity,
                )
                for custody in result.custodies
            ],
        )


class ResponsibleTeamUpdateRequest(StrictApiModel):
    """HR request to assign or clear one Asset's Team classification."""

    responsible_team_id: UUID | None = Field(alias="responsibleTeamId", strict=False)


class ResponsibleTeamUpdateResponse(StrictApiModel):
    """Asset identity and its current Responsible Team classification."""

    id: UUID
    responsible_team_id: UUID | None = Field(serialization_alias="responsibleTeamId")

    @classmethod
    def from_result(cls, result: ResponsibleTeamUpdateResult) -> ResponsibleTeamUpdateResponse:
        """Map the application update result to the frozen API contract."""

        return cls(id=result.asset_id, responsible_team_id=result.responsible_team_id)
