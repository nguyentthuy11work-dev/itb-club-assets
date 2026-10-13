"""Thin routes for Event Day configuration and reads."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status

from ams.application.event_days import EventDayService
from ams.domain.identity import CurrentActor
from ams.interface.api.dependencies import get_current_actor, get_event_day_service
from ams.interface.api.event_models import ConfigureEventDayRequest, EventDayResponse

IDEMPOTENCY_HEADER = Header(alias="Idempotency-Key", min_length=1, max_length=255)

router = APIRouter(prefix="/event-days", tags=["Event Days"])


@router.post("", response_model=EventDayResponse, status_code=status.HTTP_201_CREATED)
async def create_event_day(
    request: ConfigureEventDayRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[EventDayService, Depends(get_event_day_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> EventDayResponse:
    """Create a future ordered Event Day as an Asset Team Lead."""

    return EventDayResponse.from_result(
        await service.create(request.to_command(), actor, idempotency_key)
    )


@router.get("/{event_day_id}", response_model=EventDayResponse)
async def get_event_day(
    event_day_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[EventDayService, Depends(get_event_day_service)],
) -> EventDayResponse:
    """Read complete Event configuration as Asset Team Lead or HR."""

    return EventDayResponse.from_result(await service.get(event_day_id, actor))


@router.patch("/{event_day_id}", response_model=EventDayResponse)
async def update_event_day(
    event_day_id: UUID,
    request: ConfigureEventDayRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[EventDayService, Depends(get_event_day_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> EventDayResponse:
    """Replace an unstarted Event schedule and synchronize unsent reminders."""

    return EventDayResponse.from_result(
        await service.update(event_day_id, request.to_command(), actor, idempotency_key)
    )
