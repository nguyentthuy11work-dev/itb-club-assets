"""SQLAlchemy adapter for durable baseline-Asset import previews."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from ams.application.reliability import ReliabilityPersistenceError
from ams.domain.asset_imports import (
    AssetImport,
    AssetImportRow,
    AssetImportStatus,
    BaselineAssetData,
    IAssetImportRepository,
)
from ams.domain.assets import AssetCondition
from ams.infrastructure.asset_persistence import AssetPersistenceBase


class AssetImportModel(AssetPersistenceBase):
    """Mapping for the Flyway-owned ``asset_imports`` table."""

    __tablename__ = "asset_imports"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16))
    source_filename: Mapped[str] = mapped_column(String(255))
    source_content_type: Mapped[str] = mapped_column(String(100))
    source_sha256: Mapped[str] = mapped_column(String(64))
    valid_row_count: Mapped[int] = mapped_column(Integer)
    invalid_row_count: Mapped[int] = mapped_column(Integer)
    rows: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SqlAlchemyAssetImportRepository(IAssetImportRepository):
    """Persist reviewed row snapshots and lock confirmation lifecycle changes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, asset_import: AssetImport) -> None:
        """Stage one preview and all normalized/error rows."""

        self._session.add(
            AssetImportModel(
                id=asset_import.id,
                created_by_user_id=asset_import.created_by_user_id,
                status=asset_import.status.value,
                source_filename=asset_import.source_filename,
                source_content_type=asset_import.source_content_type,
                source_sha256=asset_import.source_sha256,
                valid_row_count=asset_import.valid_row_count,
                invalid_row_count=asset_import.invalid_row_count,
                rows=[self._row_to_json(row) for row in asset_import.rows],
                expires_at=asset_import.expires_at,
                confirmed_at=asset_import.confirmed_at,
                created_at=asset_import.created_at,
                updated_at=asset_import.created_at,
            )
        )

    async def get_for_update(self, import_id: UUID) -> AssetImport | None:
        """Lock and rehydrate one preview for explicit confirmation."""

        try:
            model = await self._session.get(
                AssetImportModel,
                import_id,
                with_for_update=True,
            )
            return None if model is None else self._to_entity(model)
        except (SQLAlchemyError, KeyError, TypeError, ValueError) as exception:
            raise ReliabilityPersistenceError(
                "Asset import preview could not be loaded."
            ) from exception

    async def save(self, asset_import: AssetImport) -> None:
        """Stage terminal completion fields without replacing reviewed rows."""

        try:
            await self._session.execute(
                update(AssetImportModel)
                .where(AssetImportModel.id == asset_import.id)
                .values(
                    status=asset_import.status.value,
                    confirmed_at=asset_import.confirmed_at,
                    updated_at=asset_import.confirmed_at,
                )
            )
        except SQLAlchemyError as exception:
            raise ReliabilityPersistenceError(
                "Asset import preview could not be updated."
            ) from exception

    @staticmethod
    def _row_to_json(row: AssetImportRow) -> dict[str, object]:
        data: dict[str, object] | None = None
        if row.data is not None:
            data = {
                "name": row.data.name,
                "description": row.data.description,
                "source": row.data.source,
                "totalQuantity": row.data.total_quantity,
                "categoryId": str(row.data.category_id),
                "responsibleTeamId": (
                    str(row.data.responsible_team_id)
                    if row.data.responsible_team_id is not None
                    else None
                ),
                "custodianUserId": str(row.data.custodian_user_id),
                "condition": row.data.condition.value,
                "conditionNote": row.data.condition_note,
            }
        return {
            "rowNumber": row.row_number,
            "data": data,
            "errors": list(row.errors),
        }

    @staticmethod
    def _to_entity(model: AssetImportModel) -> AssetImport:
        rows: list[AssetImportRow] = []
        for raw in model.rows:
            raw_data = raw["data"]
            data = None
            if raw_data is not None:
                team_id = raw_data["responsibleTeamId"]
                data = BaselineAssetData(
                    name=str(raw_data["name"]),
                    description=(
                        None
                        if raw_data["description"] is None
                        else str(raw_data["description"])
                    ),
                    source=str(raw_data["source"]),
                    total_quantity=int(raw_data["totalQuantity"]),
                    category_id=UUID(str(raw_data["categoryId"])),
                    responsible_team_id=(None if team_id is None else UUID(str(team_id))),
                    custodian_user_id=UUID(str(raw_data["custodianUserId"])),
                    condition=AssetCondition(str(raw_data["condition"])),
                    condition_note=(
                        None
                        if raw_data["conditionNote"] is None
                        else str(raw_data["conditionNote"])
                    ),
                )
            errors = raw["errors"]
            if not isinstance(errors, list):
                raise TypeError
            rows.append(
                AssetImportRow(
                    row_number=int(raw["rowNumber"]),
                    data=data,
                    errors=tuple(str(error) for error in errors),
                )
            )
        return AssetImport(
            id=model.id,
            created_by_user_id=model.created_by_user_id,
            status=AssetImportStatus(model.status),
            source_filename=model.source_filename,
            source_content_type=model.source_content_type,
            source_sha256=model.source_sha256,
            rows=tuple(rows),
            expires_at=model.expires_at,
            created_at=model.created_at,
            confirmed_at=model.confirmed_at,
        )
