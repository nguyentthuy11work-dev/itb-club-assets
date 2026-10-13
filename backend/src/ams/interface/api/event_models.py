"""Strict API contracts for Event Day configuration."""

from __future__ import annotations

from datetime import date, datetime
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from ams.application.event_days import (
    ConfigureEventDayCommand,
    ConfigureShiftAssetCommand,
    ConfigureShiftCommand,
    EventDayResult,
)
from ams.domain.events import EventDayStatus, ShiftStatus
from ams.interface.api.models import StrictApiModel


class ShiftAssetConfigurationRequest(StrictApiModel):
    """One Asset quota selected for a configured Shift."""

    asset_id: UUID = Field(alias="assetId", strict=False)
    quota_quantity: int = Field(alias="quotaQuantity", ge=0)


class ShiftConfigurationRequest(StrictApiModel):
    """One ordered Shift schedule and assignment."""

    id: UUID | None = Field(default=None, strict=False)
    sequence: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=200, pattern=r".*\S.*")
    starts_at: datetime = Field(alias="startsAt", strict=False)
    ends_at: datetime = Field(alias="endsAt", strict=False)
    shift_lead_user_id: UUID = Field(alias="shiftLeadUserId", strict=False)
    assets: list[ShiftAssetConfigurationRequest] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_time_order(self) -> Self:
        """Reject an empty or reversed schedule at the transport boundary."""

        if self.ends_at <= self.starts_at:
            raise ValueError("endsAt must follow startsAt.")
        return self


class ConfigureEventDayRequest(StrictApiModel):
    """Complete Event configuration for create or safe replacement."""

    name: str = Field(min_length=1, max_length=200, pattern=r".*\S.*")
    event_date: date = Field(alias="eventDate", strict=False)
    shifts: list[ShiftConfigurationRequest] = Field(min_length=1)

    def to_command(self) -> ConfigureEventDayCommand:
        """Map transport values to the framework-independent application command."""

        return ConfigureEventDayCommand(
            name=self.name,
            event_date=self.event_date,
            shifts=tuple(
                ConfigureShiftCommand(
                    id=shift.id,
                    sequence=shift.sequence,
                    name=shift.name,
                    starts_at=shift.starts_at,
                    ends_at=shift.ends_at,
                    shift_lead_user_id=shift.shift_lead_user_id,
                    assets=tuple(
                        ConfigureShiftAssetCommand(item.asset_id, item.quota_quantity)
                        for item in shift.assets
                    ),
                )
                for shift in self.shifts
            ),
        )


class ShiftAssetConfigurationResponse(StrictApiModel):
    """Operational quota snapshot with no core-stock side effect."""

    asset_id: UUID = Field(serialization_alias="assetId")
    quota_quantity: int = Field(serialization_alias="quotaQuantity")
    carry_over_quantity: int = Field(serialization_alias="carryOverQuantity")
    expected_open_quantity: int = Field(serialization_alias="expectedOpenQuantity")


class ShiftConfigurationResponse(StrictApiModel):
    """Configured Shift safe for Team Lead and HR reads."""

    id: UUID
    sequence: int
    name: str
    starts_at: datetime = Field(serialization_alias="startsAt")
    ends_at: datetime = Field(serialization_alias="endsAt")
    shift_lead_user_id: UUID = Field(serialization_alias="shiftLeadUserId")
    status: ShiftStatus
    assets: list[ShiftAssetConfigurationResponse]


class EventDayResponse(StrictApiModel):
    """Complete Event Day configuration contract."""

    id: UUID
    name: str
    event_date: date = Field(serialization_alias="eventDate")
    status: EventDayStatus
    core_stock_changed: bool = Field(serialization_alias="coreStockChanged")
    created_by_user_id: UUID = Field(serialization_alias="createdByUserId")
    shifts: list[ShiftConfigurationResponse]

    @classmethod
    def from_result(cls, result: EventDayResult) -> EventDayResponse:
        """Map Domain configuration without exposing persistence details."""

        event_day = result.event_day
        return cls(
            id=event_day.id,
            name=event_day.name,
            event_date=event_day.event_date,
            status=event_day.status,
            core_stock_changed=False,
            created_by_user_id=event_day.created_by_user_id,
            shifts=[
                ShiftConfigurationResponse(
                    id=shift.id,
                    sequence=shift.sequence,
                    name=shift.name,
                    starts_at=shift.starts_at,
                    ends_at=shift.ends_at,
                    shift_lead_user_id=shift.shift_lead_user_id,
                    status=shift.status,
                    assets=[
                        ShiftAssetConfigurationResponse(
                            asset_id=item.asset_id,
                            quota_quantity=item.quota_quantity,
                            carry_over_quantity=item.carry_over_quantity,
                            expected_open_quantity=item.expected_open_quantity,
                        )
                        for item in shift.assets
                    ],
                )
                for shift in event_day.shifts
            ],
        )
