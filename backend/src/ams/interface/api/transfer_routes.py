"""Thin HTTP routes for standalone two-party Custody Transfers."""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from ams.application.evidence import EvidenceFileInput
from ams.application.transfers import TransferService
from ams.domain.identity import CurrentActor
from ams.domain.transfers import TransferStatus
from ams.interface.api.dependencies import get_current_actor, get_transfer_service
from ams.interface.api.transfer_models import (
    ConfirmTransferMetadataRequest,
    CreateLinkedTransferRequest,
    CreateTransferRequest,
    TransferPageResponse,
    TransferResponse,
)

router = APIRouter(tags=["Custody Transfers"])
IDEMPOTENCY_HEADER = Header(alias="Idempotency-Key", min_length=1, max_length=255)
UPLOAD_CHUNK_SIZE = 64 * 1024


@router.post("/transfers", response_model=TransferResponse, status_code=status.HTTP_201_CREATED)
async def create_transfer(
    request: CreateTransferRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TransferService, Depends(get_transfer_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> TransferResponse:
    """Reserve source quantity and create a Transfer as HR."""

    return TransferResponse.from_result(
        await service.create(request.to_command(), actor, idempotency_key)
    )


@router.post(
    "/reconciliation-items/{item_id}/transfer",
    response_model=TransferResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_reconciliation_transfer(
    item_id: UUID,
    request: CreateLinkedTransferRequest,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TransferService, Depends(get_transfer_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
) -> TransferResponse:
    """Create a Transfer explicitly linked to a Reconciliation item."""

    return TransferResponse.from_result(
        await service.create(request.to_command(item_id), actor, idempotency_key)
    )


@router.get("/transfers", response_model=TransferPageResponse)
async def list_transfers(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TransferService, Depends(get_transfer_service)],
    transfer_status: Annotated[TransferStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="page_size", ge=1, le=100)] = 20,
) -> TransferPageResponse:
    """List HR-wide or participant-owned Transfers."""

    return TransferPageResponse.from_result(
        await service.list(
            actor,
            status=transfer_status,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
    )


@router.post("/transfers/{transfer_id}/confirm-current", response_model=TransferResponse)
async def confirm_transfer_current(
    transfer_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TransferService, Depends(get_transfer_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
    metadata: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
) -> TransferResponse:
    """Confirm the current-custodian side with evidence."""

    parsed = _parse_metadata(metadata)
    return TransferResponse.from_result(
        await service.confirm_current(
            transfer_id,
            parsed.to_command(),
            _evidence_inputs(files),
            actor,
            idempotency_key,
        )
    )


@router.post("/transfers/{transfer_id}/confirm-target", response_model=TransferResponse)
async def confirm_transfer_target(
    transfer_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[TransferService, Depends(get_transfer_service)],
    idempotency_key: Annotated[str, IDEMPOTENCY_HEADER],
    metadata: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
) -> TransferResponse:
    """Confirm the target-custodian side in either confirmation order."""

    parsed = _parse_metadata(metadata)
    return TransferResponse.from_result(
        await service.confirm_target(
            transfer_id,
            parsed.to_command(),
            _evidence_inputs(files),
            actor,
            idempotency_key,
        )
    )


def _evidence_inputs(files: list[UploadFile]) -> tuple[EvidenceFileInput, ...]:
    return tuple(
        EvidenceFileInput(
            content=_upload_content(upload),
            declared_content_type=upload.content_type or "",
        )
        for upload in files
    )


async def _upload_content(upload: UploadFile) -> AsyncIterable[bytes]:
    while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
        yield chunk


def _parse_metadata(value: str) -> ConfirmTransferMetadataRequest:
    try:
        return ConfirmTransferMetadataRequest.model_validate_json(value)
    except ValidationError as exception:
        raise RequestValidationError(exception.errors()) from exception
