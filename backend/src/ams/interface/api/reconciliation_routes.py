"""Thin HTTP routes for F09 reconciliation commands and reads."""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from ams.application.evidence import EvidenceFileInput
from ams.application.reconciliations import ReconciliationService
from ams.domain.identity import CurrentActor
from ams.interface.api.dependencies import (
    get_current_actor,
    get_reconciliation_service,
)
from ams.interface.api.reconciliation_models import (
    ConfirmationRequestResponse,
    ConfirmReconciliationMetadataRequest,
    OpenReconciliationRequest,
    ReconciliationItemPageResponse,
    ReconciliationItemResponse,
    ReconciliationResponse,
    ReminderResponse,
    ResolveReconciliationItemRequest,
)

router = APIRouter(tags=["Reconciliation"])
IDEMPOTENCY_HEADER = Header(alias="Idempotency-Key", min_length=1, max_length=255)
UPLOAD_CHUNK_SIZE = 64 * 1024


@router.post(
    "/reconciliations",
    response_model=ReconciliationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def open_reconciliation(
    request: OpenReconciliationRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ReconciliationService, Depends(get_reconciliation_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> ReconciliationResponse:
    """Open one immutable active-Custody snapshot as HR."""

    return ReconciliationResponse.from_result(
        await service.open(request.reconciliation_date, actor, idempotency_key)
    )


@router.get("/reconciliations/me", response_model=ReconciliationItemPageResponse)
async def list_my_reconciliation_items(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ReconciliationService, Depends(get_reconciliation_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="page_size", ge=1, le=100)] = 20,
) -> ReconciliationItemPageResponse:
    """Return only snapshot items assigned to the current Custodian."""

    return ReconciliationItemPageResponse.from_result(
        await service.list_mine(actor, offset=(page - 1) * page_size, limit=page_size)
    )


@router.get("/reconciliations/{reconciliation_id}", response_model=ReconciliationResponse)
async def get_reconciliation(
    reconciliation_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ReconciliationService, Depends(get_reconciliation_service)],
) -> ReconciliationResponse:
    """Return one complete reconciliation snapshot to HR."""

    return ReconciliationResponse.from_result(await service.get(reconciliation_id, actor))


@router.post(
    "/reconciliations/{reconciliation_id}/request-confirmation",
    response_model=ConfirmationRequestResponse,
)
async def request_reconciliation_confirmation(
    reconciliation_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ReconciliationService, Depends(get_reconciliation_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> ConfirmationRequestResponse:
    """Queue one deduplicated confirmation request per snapshot custodian."""

    return ConfirmationRequestResponse.from_result(
        await service.request_confirmation(reconciliation_id, actor, idempotency_key)
    )


@router.post(
    "/reconciliations/{reconciliation_id}/close",
    response_model=ReconciliationResponse,
)
async def close_reconciliation(
    reconciliation_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ReconciliationService, Depends(get_reconciliation_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> ReconciliationResponse:
    """Close only after every item is confirmed or resolved."""

    return ReconciliationResponse.from_result(
        await service.close(reconciliation_id, actor, idempotency_key)
    )


@router.post(
    "/reconciliation-items/{item_id}/confirm",
    response_model=ReconciliationItemResponse,
)
async def confirm_reconciliation_item(
    item_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ReconciliationService, Depends(get_reconciliation_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
    metadata: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
) -> ReconciliationItemResponse:
    """Confirm one assigned item with one to three private evidence images."""

    parsed = _parse_metadata(metadata)
    evidence = tuple(
        EvidenceFileInput(
            content=_upload_content(upload),
            declared_content_type=upload.content_type or "",
        )
        for upload in files
    )
    return ReconciliationItemResponse.from_result(
        await service.confirm_item(
            item_id, parsed.to_command(), evidence, actor, idempotency_key
        )
    )


@router.post(
    "/reconciliation-items/{item_id}/resolve",
    response_model=ReconciliationItemResponse,
)
async def resolve_reconciliation_item(
    item_id: UUID,
    request: ResolveReconciliationItemRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ReconciliationService, Depends(get_reconciliation_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> ReconciliationItemResponse:
    """Resolve one discrepancy as HR with an immutable explanation."""

    return ReconciliationItemResponse.from_result(
        await service.resolve_item(item_id, request.resolution_note, actor, idempotency_key)
    )


@router.post(
    "/reconciliation-items/{item_id}/remind",
    response_model=ReminderResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def remind_reconciliation_item(
    item_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[ReconciliationService, Depends(get_reconciliation_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> ReminderResponse:
    """Queue a reminder only while the item remains pending."""

    return ReminderResponse.from_result(
        await service.remind_item(item_id, actor, idempotency_key)
    )


async def _upload_content(upload: UploadFile) -> AsyncIterable[bytes]:
    while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
        yield chunk


def _parse_metadata(value: str) -> ConfirmReconciliationMetadataRequest:
    try:
        return ConfirmReconciliationMetadataRequest.model_validate_json(value)
    except ValidationError as exception:
        raise RequestValidationError(exception.errors()) from exception
