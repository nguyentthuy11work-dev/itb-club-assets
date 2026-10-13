"""Thin multipart API routes for assigned Custody confirmation."""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from ams.application.custody_confirmation import (
    ConfirmCustodyMetadata,
    CustodyConfirmationService,
)
from ams.application.evidence import EvidenceFileInput
from ams.domain.identity import CurrentActor
from ams.interface.api.custody_models import (
    ConfirmCustodyMetadataRequest,
    ConfirmCustodyResponse,
)
from ams.interface.api.dependencies import (
    get_current_actor,
    get_custody_confirmation_service,
)

UPLOAD_CHUNK_SIZE = 64 * 1024

router = APIRouter(prefix="/custodies", tags=["Custodies"])


@router.post("/{custody_id}/confirm", response_model=ConfirmCustodyResponse)
async def confirm_custody(
    custody_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[CustodyConfirmationService, Depends(get_custody_confirmation_service)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ],
    metadata: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
) -> ConfirmCustodyResponse:
    """Confirm an assigned pending Custody with one to three private images."""

    parsed_metadata = _parse_metadata(metadata)
    evidence_files = tuple(
        EvidenceFileInput(
            content=_upload_content(upload),
            declared_content_type=upload.content_type or "",
        )
        for upload in files
    )
    result = await service.confirm(
        custody_id=custody_id,
        metadata=ConfirmCustodyMetadata(note=parsed_metadata.note),
        files=evidence_files,
        actor=actor,
        idempotency_key=idempotency_key,
    )
    return ConfirmCustodyResponse.from_result(result)


async def _upload_content(upload: UploadFile) -> AsyncIterable[bytes]:
    while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
        yield chunk


def _parse_metadata(value: str) -> ConfirmCustodyMetadataRequest:
    """Validate the required JSON multipart part through the strict API model."""

    try:
        return ConfirmCustodyMetadataRequest.model_validate_json(value)
    except ValidationError as exception:
        raise RequestValidationError(exception.errors()) from exception
