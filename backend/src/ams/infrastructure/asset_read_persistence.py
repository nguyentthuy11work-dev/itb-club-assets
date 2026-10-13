"""SQLAlchemy query adapter for Asset and current-Custody read models."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Result
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.elements import ColumnElement

from ams.application.asset_queries import AssetReadPersistenceError
from ams.domain.asset_views import (
    AssetBrowseView,
    AssetCustodyView,
    AssetDetailView,
    EligibleCustodyView,
    HeldAssetView,
    IAssetReadRepository,
)
from ams.domain.assets import AssetCondition, CustodyStatus
from ams.infrastructure.asset_persistence import (
    AssetModel,
    CustodyEvidenceModel,
    CustodyModel,
)

CURRENT_CUSTODY_STATUSES = (
    CustodyStatus.PENDING.value,
    CustodyStatus.ACTIVE.value,
)


class SqlAlchemyAssetReadRepository(IAssetReadRepository):
    """Build read-only projections directly from the Flyway-owned schema."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_held(
        self, actor_id: UUID, *, offset: int, limit: int
    ) -> tuple[tuple[HeldAssetView, ...], int]:
        """Return a stable page ordered by newest assignment then Custody UUID."""

        filters = (
            CustodyModel.custodian_user_id == actor_id,
            CustodyModel.status.in_(CURRENT_CUSTODY_STATUSES),
        )
        total_result = await self._execute(select(func.count(CustodyModel.id)).where(*filters))
        rows = await self._execute(
            select(CustodyModel, AssetModel.name)
            .join(AssetModel, AssetModel.id == CustodyModel.asset_id)
            .where(*filters)
            .order_by(CustodyModel.assigned_at.desc(), CustodyModel.id)
            .offset(offset)
            .limit(limit)
        )
        items = tuple(self._to_held(model, asset_name) for model, asset_name in rows.all())
        return items, int(total_result.scalar_one())

    async def get_detail(self, asset_id: UUID) -> AssetDetailView | None:
        """Return an Asset and only its pending or active Custody summaries."""

        asset_result = await self._execute(select(AssetModel).where(AssetModel.id == asset_id))
        asset = asset_result.scalar_one_or_none()
        if asset is None:
            return None

        evidence_count = (
            select(func.count(CustodyEvidenceModel.id))
            .where(CustodyEvidenceModel.custody_id == CustodyModel.id)
            .correlate(CustodyModel)
            .scalar_subquery()
        )
        custody_result = await self._execute(
            select(CustodyModel, evidence_count)
            .where(
                CustodyModel.asset_id == asset_id,
                CustodyModel.status.in_(CURRENT_CUSTODY_STATUSES),
            )
            .order_by(CustodyModel.assigned_at, CustodyModel.id)
        )
        custodies = tuple(
            self._to_asset_custody(model, int(count)) for model, count in custody_result.all()
        )
        return AssetDetailView(
            id=asset.id,
            category_id=asset.category_id,
            responsible_team_id=asset.responsible_team_id,
            name=asset.name,
            description=asset.description,
            source=asset.source,
            total_quantity=asset.total_quantity,
            available_quantity=asset.available_quantity,
            custodies=custodies,
        )

    async def list_assets(
        self,
        *,
        search: str | None,
        category_id: UUID | None,
        team_id: UUID | None,
        offset: int,
        limit: int,
    ) -> tuple[tuple[AssetBrowseView, ...], int]:
        """Return filtered Assets ordered case-insensitively by name then UUID."""

        filters: list[ColumnElement[bool]] = []
        if search is not None:
            filters.append(AssetModel.name.ilike(self._contains_pattern(search), escape="\\"))
        if category_id is not None:
            filters.append(AssetModel.category_id == category_id)
        if team_id is not None:
            filters.append(AssetModel.responsible_team_id == team_id)

        return await self._list_assets(filters, offset=offset, limit=limit)

    async def list_for_teams(
        self,
        team_ids: frozenset[UUID],
        *,
        offset: int,
        limit: int,
    ) -> tuple[tuple[AssetBrowseView, ...], int]:
        """Return Assets classified to the supplied server-derived Team scope."""

        filters: list[ColumnElement[bool]] = [AssetModel.responsible_team_id.in_(team_ids)]
        return await self._list_assets(filters, offset=offset, limit=limit)

    async def _list_assets(
        self,
        filters: list[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[tuple[AssetBrowseView, ...], int]:
        total_result = await self._execute(select(func.count(AssetModel.id)).where(*filters))
        asset_result = await self._execute(
            select(AssetModel)
            .where(*filters)
            .order_by(func.lower(AssetModel.name), AssetModel.id)
            .offset(offset)
            .limit(limit)
        )
        assets = tuple(asset_result.scalars())
        eligible_by_asset: dict[UUID, list[EligibleCustodyView]] = {
            asset.id: [] for asset in assets
        }
        if assets:
            lendable = (
                CustodyModel.on_hand_quantity
                - CustodyModel.reserved_quantity
                - CustodyModel.temporarily_loaned_quantity
            )
            custody_result = await self._execute(
                select(CustodyModel.asset_id, CustodyModel.id, lendable)
                .where(
                    CustodyModel.asset_id.in_(eligible_by_asset),
                    CustodyModel.status == CustodyStatus.ACTIVE.value,
                    lendable > 0,
                )
                .order_by(CustodyModel.asset_id, CustodyModel.id)
            )
            for asset_id, custody_id, lendable_quantity in custody_result.all():
                eligible_by_asset[asset_id].append(
                    EligibleCustodyView(
                        source_custody_id=custody_id,
                        lendable_quantity=int(lendable_quantity),
                    )
                )

        items = tuple(
            AssetBrowseView(
                id=asset.id,
                category_id=asset.category_id,
                responsible_team_id=asset.responsible_team_id,
                name=asset.name,
                description=asset.description,
                source=asset.source,
                total_quantity=asset.total_quantity,
                available_quantity=asset.available_quantity,
                eligible_custodies=tuple(eligible_by_asset[asset.id]),
            )
            for asset in assets
        )
        return items, int(total_result.scalar_one())

    async def _execute(self, statement: Executable) -> Result[Any]:
        try:
            return await self._session.execute(statement)
        except SQLAlchemyError as exception:
            raise AssetReadPersistenceError("Asset read operation failed.") from exception

    @staticmethod
    def _to_held(model: CustodyModel, asset_name: str) -> HeldAssetView:
        status = CustodyStatus(model.status)
        return HeldAssetView(
            custody_id=model.id,
            asset_id=model.asset_id,
            asset_name=asset_name,
            status=status,
            on_hand_quantity=model.on_hand_quantity,
            reserved_quantity=model.reserved_quantity,
            temporarily_loaned_quantity=model.temporarily_loaned_quantity,
            lendable_quantity=SqlAlchemyAssetReadRepository._lendable(model, status),
            condition=AssetCondition(model.condition),
            assigned_at=model.assigned_at,
            confirmed_at=model.confirmed_at,
        )

    @staticmethod
    def _to_asset_custody(model: CustodyModel, evidence_count: int) -> AssetCustodyView:
        status = CustodyStatus(model.status)
        return AssetCustodyView(
            custody_id=model.id,
            custodian_user_id=model.custodian_user_id,
            status=status,
            on_hand_quantity=model.on_hand_quantity,
            reserved_quantity=model.reserved_quantity,
            temporarily_loaned_quantity=model.temporarily_loaned_quantity,
            lendable_quantity=SqlAlchemyAssetReadRepository._lendable(model, status),
            condition=AssetCondition(model.condition),
            condition_note=model.condition_note,
            assigned_at=model.assigned_at,
            confirmed_at=model.confirmed_at,
            evidence_count=evidence_count,
        )

    @staticmethod
    def _lendable(model: CustodyModel, status: CustodyStatus) -> int:
        if status is not CustodyStatus.ACTIVE:
            return 0
        return (
            model.on_hand_quantity - model.reserved_quantity - model.temporarily_loaned_quantity
        )

    @staticmethod
    def _contains_pattern(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"
