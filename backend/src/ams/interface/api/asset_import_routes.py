"""Thin multipart preview and explicit-confirmation routes for baseline Assets."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, UploadFile

from ams.application.asset_imports import (
    MAX_IMPORT_FILE_SIZE_BYTES,
    AssetImportFile,
    AssetImportService,
    UnsupportedImportFileError,
)
from ams.domain.identity import CurrentActor, OperationalRole
from ams.interface.api.asset_import_models import (
    AssetImportConfirmationResponse,
    AssetImportPreviewResponse,
)
from ams.interface.api.dependencies import (
    RequireRoles,
    get_asset_import_service,
)

IMPORT_UPLOAD_CHUNK_SIZE = 64 * 1024
HR_REQUIRED = RequireRoles(OperationalRole.HR)

router = APIRouter(prefix="/hr/assets/imports", tags=["Assets"])


@router.post("", response_model=AssetImportPreviewResponse)
async def preview_asset_import(
    actor: Annotated[CurrentActor, Depends(HR_REQUIRED)],
    service: Annotated[AssetImportService, Depends(get_asset_import_service)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ],
    file: Annotated[UploadFile, File()],
) -> AssetImportPreviewResponse:
    """Parse and persist a row-level preview without creating any Asset."""

    result = await service.preview(
        AssetImportFile(
            filename=file.filename or "",
            content_type=file.content_type or "",
            content=await _read_upload(file),
        ),
        actor,
        idempotency_key,
    )
    return AssetImportPreviewResponse.from_result(result)


@router.post("/{import_id}/confirm", response_model=AssetImportConfirmationResponse)
async def confirm_asset_import(
    import_id: UUID,
    actor: Annotated[CurrentActor, Depends(HR_REQUIRED)],
    service: Annotated[AssetImportService, Depends(get_asset_import_service)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ],
) -> AssetImportConfirmationResponse:
    """Atomically persist only valid rows from the actor's reviewed preview."""

    result = await service.confirm(import_id, actor, idempotency_key)
    return AssetImportConfirmationResponse.from_result(result)


async def _read_upload(file: UploadFile) -> bytes:
    """Read upload in bounded chunks; parser applies the canonical total limit."""

    content = bytearray()
    while chunk := await file.read(IMPORT_UPLOAD_CHUNK_SIZE):
        if len(content) + len(chunk) > MAX_IMPORT_FILE_SIZE_BYTES:
            raise UnsupportedImportFileError("Import file exceeds the 5 MB limit.")
        content.extend(chunk)
    return bytes(content)
