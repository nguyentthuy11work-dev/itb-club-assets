"""Strict transport contracts for Shift execution and Event Day summaries."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from ams.application.shift_execution import ShiftPage, ShiftQuantityCommand, ShiftResult
from ams.domain.events import EventDayStatus, ShiftStatus
from ams.domain.shift_execution import EventDayExecutionSummary, ShiftExecution
from ams.interface.api.models import StrictApiModel


class ShiftActualQuantityRequest(StrictApiModel):
    """One complete opening or closing actual quantity."""

    asset_id: UUID = Field(alias="assetId", strict=False)
    actual_quantity: int = Field(alias="actualQuantity", ge=0)

    def to_command(self) -> ShiftQuantityCommand:
        """Map transport values into the application command."""

        return ShiftQuantityCommand(self.asset_id, self.actual_quantity)


class ShiftQuantitiesRequest(StrictApiModel):
    """Complete quantity set for every configured Shift Asset."""

    items: list[ShiftActualQuantityRequest] = Field(min_length=1)

    def to_commands(self) -> tuple[ShiftQuantityCommand, ...]:
        """Map the complete request while retaining duplicate detection downstream."""

        return tuple(item.to_command() for item in self.items)


class ShiftOpeningDeviationRequest(ShiftQuantitiesRequest):
    """Complete opening quantities and a non-blank mismatch explanation."""

    reason: str = Field(min_length=1, max_length=1000, pattern=r".*\S.*")


class ShiftExecutionItemResponse(StrictApiModel):
    """One operational Asset snapshot and its formula outputs."""

    id: UUID
    asset_id: UUID = Field(serialization_alias="assetId")
    quota_quantity: int = Field(serialization_alias="quotaQuantity")
    carry_over_quantity: int = Field(serialization_alias="carryOverQuantity")
    expected_open_quantity: int = Field(serialization_alias="expectedOpenQuantity")
    opening_actual_quantity: int | None = Field(serialization_alias="openingActualQuantity")
    closing_actual_quantity: int | None = Field(serialization_alias="closingActualQuantity")
    consumed_quantity: int | None = Field(serialization_alias="consumedQuantity")
    discrepancy_reason: str | None = Field(serialization_alias="discrepancyReason")


class ShiftExecutionResponse(StrictApiModel):
    """Complete assigned Shift state returned by reads and commands."""

    id: UUID
    event_day_id: UUID = Field(serialization_alias="eventDayId")
    event_day_name: str = Field(serialization_alias="eventDayName")
    sequence: int
    name: str
    starts_at: datetime = Field(serialization_alias="startsAt")
    ends_at: datetime = Field(serialization_alias="endsAt")
    shift_lead_user_id: UUID = Field(serialization_alias="shiftLeadUserId")
    status: ShiftStatus
    core_stock_changed: bool = Field(serialization_alias="coreStockChanged")
    items: list[ShiftExecutionItemResponse]

    @classmethod
    def from_shift(cls, shift: ShiftExecution) -> ShiftExecutionResponse:
        """Map a Domain execution snapshot without exposing persistence details."""

        return cls(
            id=shift.id,
            event_day_id=shift.event_day_id,
            event_day_name=shift.event_day_name,
            sequence=shift.sequence,
            name=shift.name,
            starts_at=shift.starts_at,
            ends_at=shift.ends_at,
            shift_lead_user_id=shift.shift_lead_user_id,
            status=shift.status,
            core_stock_changed=False,
            items=[
                ShiftExecutionItemResponse(
                    id=item.id,
                    asset_id=item.asset_id,
                    quota_quantity=item.quota_quantity,
                    carry_over_quantity=item.carry_over_quantity,
                    expected_open_quantity=item.expected_open_quantity,
                    opening_actual_quantity=item.opening_actual_quantity,
                    closing_actual_quantity=item.closing_actual_quantity,
                    consumed_quantity=item.consumed_quantity,
                    discrepancy_reason=item.discrepancy_reason,
                )
                for item in shift.items
            ],
        )

    @classmethod
    def from_result(cls, result: ShiftResult) -> ShiftExecutionResponse:
        """Map an application result into the stable response contract."""

        return cls.from_shift(result.shift)


class ShiftPageResponse(StrictApiModel):
    """Paginated work queue for the current Shift Lead."""

    items: list[ShiftExecutionResponse]
    page: int
    page_size: int = Field(serialization_alias="pageSize")
    total: int

    @classmethod
    def from_result(cls, result: ShiftPage) -> ShiftPageResponse:
        """Map offset-based application paging to the public page contract."""

        return cls(
            items=[ShiftExecutionResponse.from_shift(item) for item in result.items],
            page=(result.offset // result.limit) + 1,
            page_size=result.limit,
            total=result.total,
        )


class EventDayAssetSummaryResponse(StrictApiModel):
    """Final totals for one Asset across all closed Shifts."""

    asset_id: UUID = Field(serialization_alias="assetId")
    total_quota_quantity: int = Field(serialization_alias="totalQuotaQuantity")
    total_consumed_quantity: int = Field(serialization_alias="totalConsumedQuantity")
    opening_variance_quantity: int = Field(serialization_alias="openingVarianceQuantity")
    final_carry_over_quantity: int = Field(serialization_alias="finalCarryOverQuantity")


class EventDayExecutionSummaryResponse(StrictApiModel):
    """Progress response whose final items remain absent until all Shifts close."""

    event_day_id: UUID = Field(serialization_alias="eventDayId")
    status: EventDayStatus
    complete: bool
    total_shift_count: int = Field(serialization_alias="totalShiftCount")
    closed_shift_count: int = Field(serialization_alias="closedShiftCount")
    core_stock_changed: bool = Field(serialization_alias="coreStockChanged")
    items: list[EventDayAssetSummaryResponse] | None

    @classmethod
    def from_result(cls, result: EventDayExecutionSummary) -> EventDayExecutionSummaryResponse:
        """Map progress and conditionally available final aggregates."""

        return cls(
            event_day_id=result.event_day_id,
            status=result.status,
            complete=result.complete,
            total_shift_count=result.total_shift_count,
            closed_shift_count=result.closed_shift_count,
            core_stock_changed=False,
            items=(
                None
                if result.items is None
                else [
                    EventDayAssetSummaryResponse(
                        asset_id=item.asset_id,
                        total_quota_quantity=item.total_quota_quantity,
                        total_consumed_quantity=item.total_consumed_quantity,
                        opening_variance_quantity=item.opening_variance_quantity,
                        final_carry_over_quantity=item.final_carry_over_quantity,
                    )
                    for item in result.items
                ]
            ),
        )
