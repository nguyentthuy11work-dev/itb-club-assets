"""Thin API route for server-scoped Team Head Asset reads."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ams.application.asset_queries import AssetReadService
from ams.domain.identity import CurrentActor
from ams.interface.api.asset_read_models import AssetBrowsePageResponse
from ams.interface.api.dependencies import get_asset_read_service, get_current_actor

router = APIRouter(prefix="/teams/me/assets", tags=["Assets"])


@router.get("", response_model=AssetBrowsePageResponse)
async def list_my_team_assets(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    service: Annotated[AssetReadService, Depends(get_asset_read_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AssetBrowsePageResponse:
    """Return only Assets in the current Team Head's membership scope."""

    result = await service.list_my_team_assets(actor, page=page, page_size=page_size)
    return AssetBrowsePageResponse.from_page(result)
