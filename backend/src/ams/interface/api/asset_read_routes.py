"""Thin authenticated API routes for Asset and Custody read models."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from ams.application.asset_queries import AssetReadService
from ams.domain.identity import CurrentActor
from ams.interface.api.asset_read_models import (
    AssetBrowsePageResponse,
    AssetDetailResponse,
    HeldAssetPageResponse,
)
from ams.interface.api.dependencies import get_asset_read_service, get_current_actor

router = APIRouter(prefix="/assets", tags=["Assets"])


@router.get("", response_model=AssetBrowsePageResponse)
async def list_assets(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[AssetReadService, Depends(get_asset_read_service)],
    search: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    category: Annotated[UUID | None, Query()] = None,
    team: Annotated[UUID | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AssetBrowsePageResponse:
    """Return the authenticated Asset catalog with composable filters."""

    result = await service.list_assets(
        actor,
        search=search,
        category_id=category,
        team_id=team,
        page=page,
        page_size=page_size,
    )
    return AssetBrowsePageResponse.from_page(result)


@router.get("/held", response_model=HeldAssetPageResponse)
async def list_held_assets(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[AssetReadService, Depends(get_asset_read_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> HeldAssetPageResponse:
    """Return current pending and active Custodies owned by the actor."""

    result = await service.list_held(actor, page=page, page_size=page_size)
    return HeldAssetPageResponse.from_page(result)


@router.get("/{asset_id}", response_model=AssetDetailResponse)
async def get_asset_detail(
    asset_id: UUID,
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[AssetReadService, Depends(get_asset_read_service)],
) -> AssetDetailResponse:
    """Return one Asset and its current pending/active Custody summaries."""

    result = await service.get_detail(asset_id, actor)
    return AssetDetailResponse.from_view(result)
