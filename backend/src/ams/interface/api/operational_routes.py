"""Thin routes for transaction history and operational dashboards."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from ams.application.operational_queries import OperationalReadService
from ams.domain.identity import CurrentActor
from ams.interface.api.dependencies import get_current_actor, get_operational_read_service
from ams.interface.api.operational_models import (
    AssetTransactionPageResponse,
    HrDashboardResponse,
    MemberDashboardResponse,
)

router = APIRouter(tags=["Operations"])


@router.get("/assets/{asset_id}/transactions", response_model=AssetTransactionPageResponse)
async def list_asset_transactions(
    asset_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[OperationalReadService, Depends(get_operational_read_service)],
    from_time: Annotated[datetime | None, Query(alias="from")] = None,
    to_time: Annotated[datetime | None, Query(alias="to")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AssetTransactionPageResponse:
    """Return authorized history for one Asset."""

    page = await service.list_asset_transactions(
        actor,
        asset_id,
        from_time=from_time,
        to_time=to_time,
        offset=offset,
        limit=limit,
    )
    return AssetTransactionPageResponse.from_page(page)


@router.get("/hr/asset-transactions", response_model=AssetTransactionPageResponse)
async def list_hr_asset_transactions(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[OperationalReadService, Depends(get_operational_read_service)],
    asset_id: Annotated[UUID | None, Query(alias="assetId")] = None,
    from_time: Annotated[datetime | None, Query(alias="from")] = None,
    to_time: Annotated[datetime | None, Query(alias="to")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AssetTransactionPageResponse:
    """Return globally filtered immutable Asset history to HR."""

    page = await service.list_hr_transactions(
        actor,
        asset_id=asset_id,
        from_time=from_time,
        to_time=to_time,
        offset=offset,
        limit=limit,
    )
    return AssetTransactionPageResponse.from_page(page)


@router.get("/dashboard", response_model=MemberDashboardResponse)
async def get_member_dashboard(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[OperationalReadService, Depends(get_operational_read_service)],
) -> MemberDashboardResponse:
    """Return the actor's current stats and typed work queue."""

    return MemberDashboardResponse.from_view(await service.get_member_dashboard(actor))


@router.get("/hr/dashboard", response_model=HrDashboardResponse)
async def get_hr_dashboard(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[OperationalReadService, Depends(get_operational_read_service)],
) -> HrDashboardResponse:
    """Return club-wide operational stats and HR intervention work."""

    return HrDashboardResponse.from_view(await service.get_hr_dashboard(actor))
