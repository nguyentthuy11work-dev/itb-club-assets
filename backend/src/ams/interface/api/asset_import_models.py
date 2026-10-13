"""Strict API responses for baseline Asset import preview and confirmation."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field

from ams.application.asset_imports import (
    AssetImportConfirmationResult,
    AssetImportPreviewResult,
    AssetImportRowResult,
)
from ams.interface.api.models import StrictApiModel


class AssetImportRowResponse(StrictApiModel):
    """One source row's reviewed validity and deterministic errors."""

    row_number: int = Field(serialization_alias="rowNumber")
    valid: bool
    errors: list[str]

    @classmethod
    def from_result(cls, result: AssetImportRowResult) -> AssetImportRowResponse:
        """Map one application row outcome."""

        return cls(
            row_number=result.row_number,
            valid=result.valid,
            errors=list(result.errors),
        )


class AssetImportPreviewResponse(StrictApiModel):
    """Reviewed preview persisted for later explicit confirmation."""

    import_id: UUID = Field(serialization_alias="importId")
    status: Literal["PREVIEWED"]
    valid_row_count: int = Field(serialization_alias="validRowCount")
    invalid_row_count: int = Field(serialization_alias="invalidRowCount")
    rows: list[AssetImportRowResponse]

    @classmethod
    def from_result(cls, result: AssetImportPreviewResult) -> AssetImportPreviewResponse:
        """Map a typed preview without exposing normalized persistence data."""

        return cls(
            import_id=result.import_id,
            status="PREVIEWED",
            valid_row_count=result.valid_row_count,
            invalid_row_count=result.invalid_row_count,
            rows=[AssetImportRowResponse.from_result(row) for row in result.rows],
        )


class AssetImportConfirmationResponse(StrictApiModel):
    """Completed valid-row import and unchanged rejected-row outcomes."""

    import_id: UUID = Field(serialization_alias="importId")
    status: Literal["COMPLETED"]
    imported_asset_count: int = Field(serialization_alias="importedAssetCount")
    rejected_rows: list[AssetImportRowResponse] = Field(serialization_alias="rejectedRows")

    @classmethod
    def from_result(
        cls, result: AssetImportConfirmationResult
    ) -> AssetImportConfirmationResponse:
        """Map confirmation output without exposing idempotency internals."""

        return cls(
            import_id=result.import_id,
            status="COMPLETED",
            imported_asset_count=result.imported_asset_count,
            rejected_rows=[
                AssetImportRowResponse.from_result(row) for row in result.rejected_rows
            ],
        )
