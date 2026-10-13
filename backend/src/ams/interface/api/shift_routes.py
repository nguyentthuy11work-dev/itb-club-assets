"""Thin HTTP routes for assigned Shift execution and Event Day summaries."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query

from ams.application.shift_execution import ShiftExecutionService
from ams.domain.identity import CurrentActor
from ams.interface.api.dependencies import get_current_actor, get_shift_execution_service
from ams.interface.api.shift_models import (
    EventDayExecutionSummaryResponse,
    ShiftExecutionResponse,
    ShiftOpeningDeviationRequest,
    ShiftPageResponse,
    ShiftQuantitiesRequest,
)

router = APIRouter(tags=["Shift Execution"])
IDEMPOTENCY_HEADER = Header(alias="Idempotency-Key", min_length=1, max_length=255)


@router.get("/shifts", response_model=ShiftPageResponse)
async def list_shifts(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ShiftExecutionService, Depends(get_shift_execution_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="page_size", ge=1, le=100)] = 20,
) -> ShiftPageResponse:
    """List only Shifts assigned to the current active Shift Lead."""

    result = await service.list(actor, offset=(page - 1) * page_size, limit=page_size)
    return ShiftPageResponse.from_result(result)


@router.get("/shifts/{shift_id}", response_model=ShiftExecutionResponse)
async def get_shift(
    shift_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ShiftExecutionService, Depends(get_shift_execution_service)],
) -> ShiftExecutionResponse:
    """Return one complete assigned Shift snapshot."""

    return ShiftExecutionResponse.from_result(await service.get(shift_id, actor))


@router.post("/shifts/{shift_id}/open", response_model=ShiftExecutionResponse)
async def open_shift(
    shift_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ShiftExecutionService, Depends(get_shift_execution_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> ShiftExecutionResponse:
    """Enter OPENING after schedule and predecessor gates pass."""

    return ShiftExecutionResponse.from_result(
        await service.open(shift_id, actor, idempotency_key)
    )


@router.post("/shifts/{shift_id}/confirm-received", response_model=ShiftExecutionResponse)
async def confirm_shift_received(
    shift_id: UUID,
    request: ShiftQuantitiesRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ShiftExecutionService, Depends(get_shift_execution_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> ShiftExecutionResponse:
    """Confirm exact opening quantities and enter ACTIVE."""

    return ShiftExecutionResponse.from_result(
        await service.confirm_received(shift_id, request.to_commands(), actor, idempotency_key)
    )


@router.post(
    "/shifts/{shift_id}/report-opening-deviation",
    response_model=ShiftExecutionResponse,
)
async def report_shift_opening_deviation(
    shift_id: UUID,
    request: ShiftOpeningDeviationRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ShiftExecutionService, Depends(get_shift_execution_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> ShiftExecutionResponse:
    """Record opening mismatches and notify the Event creator."""

    return ShiftExecutionResponse.from_result(
        await service.report_opening_deviation(
            shift_id,
            request.to_commands(),
            request.reason,
            actor,
            idempotency_key,
        )
    )


@router.post("/shifts/{shift_id}/close", response_model=ShiftExecutionResponse)
async def close_shift(
    shift_id: UUID,
    request: ShiftQuantitiesRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ShiftExecutionService, Depends(get_shift_execution_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> ShiftExecutionResponse:
    """Close an active Shift using complete non-negative actual quantities."""

    return ShiftExecutionResponse.from_result(
        await service.close(shift_id, request.to_commands(), actor, idempotency_key)
    )


@router.get(
    "/event-days/{event_day_id}/summary",
    response_model=EventDayExecutionSummaryResponse,
)
async def get_event_day_summary(
    event_day_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ShiftExecutionService, Depends(get_shift_execution_service)],
) -> EventDayExecutionSummaryResponse:
    """Return progress and final totals to HR or Asset Team Leads."""

    return EventDayExecutionSummaryResponse.from_result(
        await service.get_summary(event_day_id, actor)
    )
