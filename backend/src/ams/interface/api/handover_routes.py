"""Thin HTTP routes for immutable term Handover records."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status

from ams.application.handovers import HandoverService
from ams.domain.identity import CurrentActor
from ams.interface.api.dependencies import get_current_actor, get_handover_service
from ams.interface.api.handover_models import CreateHandoverRequest, HandoverResponse

router = APIRouter(prefix="/handovers", tags=["Handovers"])
IDEMPOTENCY_HEADER = Header(alias="Idempotency-Key", min_length=1, max_length=255)


@router.post("", response_model=HandoverResponse, status_code=status.HTTP_201_CREATED)
async def create_handover(
    request: CreateHandoverRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[HandoverService, Depends(get_handover_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> HandoverResponse:
    """Create the selected term's single immutable Asset snapshot."""

    return HandoverResponse.from_result(
        await service.create(request.term_id, actor, idempotency_key)
    )


@router.get("/{handover_id}", response_model=HandoverResponse)
async def get_handover(
    handover_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[HandoverService, Depends(get_handover_service)],
) -> HandoverResponse:
    """Return a complete snapshot to either configured HR actor."""

    return HandoverResponse.from_result(await service.get(handover_id, actor))


@router.get("/{handover_id}/export", response_class=Response)
async def export_handover(
    handover_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[HandoverService, Depends(get_handover_service)],
) -> Response:
    """Download a bounded synchronous CSV without private evidence references."""

    document = await service.export_csv(handover_id, actor)
    return Response(
        content=document.content,
        media_type=document.media_type,
        headers={"Content-Disposition": f'attachment; filename="{document.filename}"'},
    )


@router.post("/{handover_id}/confirm-old", response_model=HandoverResponse)
async def confirm_handover_old(
    handover_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[HandoverService, Depends(get_handover_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> HandoverResponse:
    """Confirm the configured old-term HR side."""

    return HandoverResponse.from_result(
        await service.confirm_old(handover_id, actor, idempotency_key)
    )


@router.post("/{handover_id}/confirm-new", response_model=HandoverResponse)
async def confirm_handover_new(
    handover_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[HandoverService, Depends(get_handover_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> HandoverResponse:
    """Confirm the configured new-term HR side in either order."""

    return HandoverResponse.from_result(
        await service.confirm_new(handover_id, actor, idempotency_key)
    )
